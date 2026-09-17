"""工具箱-剪气口：silero-vad 静音段检测 + ffmpeg 拼接去停顿。

- 输入：口播/人声视频（音乐、纯画面素材请勿使用——无语音时无法判定）
- 参数：灵敏度（VAD 阈值）、最短静音（>=此长度才剪，默认 0.6s）、保留句间留白（默认 0.3s）
- 实现：silero-vad（onnx）逐帧说话概率 → 合并/过滤得语音段 → 保留前后留白 → ffmpeg 拼接
- 所有 ffmpeg 调用走 toolbox/engine.py 封装；trim 时间值为纯数字，无注入风险

依赖：onnxruntime + silero-vad（pip install silero-vad，模型约 2.2MB）
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from toolbox import engine
from toolbox.models import models_dir

SR = 16000
WINDOW = 512

_SESSION = None


class VadError(Exception):
    """剪气口错误，message 面向用户。"""


def vad_model_path() -> str:
    """silero-vad onnx 模型路径（缓存到 models/，缺失时从已装包提取）。"""
    cached = models_dir() / "silero_vad.onnx"
    if cached.is_file():
        return str(cached)
    try:
        from silero_vad import data as svd
        src = Path(svd.__file__).parent / "silero_vad.onnx"
        if src.is_file():
            shutil.copy2(str(src), cached)
            return str(cached)
    except Exception:
        pass
    raise VadError("缺少 silero-vad 模型：请先安装依赖（pip install silero-vad）")


def _session():
    global _SESSION
    if _SESSION is None:
        import onnxruntime
        so = onnxruntime.SessionOptions()
        so.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
        _SESSION = onnxruntime.InferenceSession(vad_model_path(), sess_options=so,
                                                providers=["CPUExecutionProvider"])
    return _SESSION


def _extract_wav(video_path: str, wav_path: str,
                 cancel: Optional[Callable[[], bool]] = None) -> None:
    """提取 16k 单声道 pcm wav（剪气口前置）。"""
    engine.run_ffmpeg(["-y", "-i", video_path, "-vn", "-ac", "1", "-ar", str(SR),
                       "-c:a", "pcm_s16le", wav_path], cancel)


def _read_wav(path: str) -> np.ndarray:
    import wave
    with wave.open(path, "rb") as w:
        assert w.getframerate() == SR and w.getnchannels() == 1
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    return data


def vad_speech_segments(audio: np.ndarray, threshold: float = 0.5,
                        min_speech: float = 0.25, min_silence: float = 0.3) -> list[tuple[float, float]]:
    """silero-vad 逐帧说话概率 → 语音段时间轴（已合并小间隔、过滤短段）。

    :param threshold: 说话概率阈值（灵敏度，0.3~0.7）
    :param min_speech: 最短语音段（秒），过短丢弃
    :param min_silence: 最短静音（秒），间隔小于此值合并为同一段
    """
    sess = _session()
    sr_in = np.array([SR], dtype=np.int64)
    state = np.zeros((2, 1, 128), dtype=np.float32)
    context = np.zeros(64, dtype=np.float32)   # context_size=64 (16k)，与官方 OnnxWrapper 一致
    probs: list[float] = []
    n = len(audio)
    # 逐窗口推理：输入 = context(64) + 窗口(512) = 576；state 由模型管理
    for step in range(0, n, WINDOW):
        frame = audio[step:step + WINDOW]
        if len(frame) < WINDOW:
            frame = np.pad(frame, (0, WINDOW - len(frame)))
        chunk = np.concatenate([context, frame])
        out, state = sess.run(None, {
            "input": chunk[None, :].astype(np.float32),
            "state": state,
            "sr": sr_in,
        })
        probs.append(float(out[0][0]))
        context = chunk[-64:]

    # 1) 滞后阈值切分语音段（进入需 >= threshold，退出需 < threshold-0.15，抑制抖动）
    neg_threshold = max(threshold - 0.15, 0.01)
    raw: list[tuple[float, float]] = []
    triggered = False
    start = 0.0
    win_t = WINDOW / SR
    for i, p in enumerate(probs):
        t = i * win_t
        if not triggered and p >= threshold:
            triggered = True
            start = t
        elif triggered and p < neg_threshold:
            triggered = False
            raw.append((start, t))
    if triggered:
        raw.append((start, n / SR))

    # 2) 合并小间隔 + 丢弃短段
    merged: list[tuple[float, float]] = []
    for s, e in raw:
        if merged and s - merged[-1][1] < min_silence:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return [(s, e) for s, e in merged if e - s >= min_speech]


def cut_pauses(video_path: str, out_dir: str,
               sensitivity: float = 0.5, min_silence: float = 0.6, keep_pad: float = 0.3,
               cancel: Optional[Callable[[], bool]] = None) -> str:
    """剪掉口播视频中的长静音，输出拼接后的连续视频。

    :param sensitivity: VAD 灵敏度（0.3~0.7，越低越敏感）
    :param min_silence: 最短静音长度（秒），达到才剪
    :param keep_pad: 保留句间留白（秒），每段前后各保留
    :return: 输出文件路径
    """
    if not Path(video_path).is_file():
        raise VadError(f"文件不存在：{video_path}")
    if not engine.has_audio(video_path):
        raise VadError("该视频没有音轨（纯画面素材），无法剪气口")
    sensitivity = max(0.2, min(0.9, float(sensitivity)))
    min_silence = max(0.1, min(10.0, float(min_silence)))
    keep_pad = max(0.0, min(3.0, float(keep_pad)))

    tmp_wav = str(Path(out_dir) / "_vad_tmp.wav")
    os.makedirs(out_dir, exist_ok=True)
    try:
        _extract_wav(video_path, tmp_wav, cancel)
        if cancel and cancel():
            raise VadError("已取消")
        audio = _read_wav(tmp_wav)
    finally:
        try:
            os.unlink(tmp_wav)
        except OSError:
            pass

    segs = vad_speech_segments(audio, sensitivity, min_speech=0.25, min_silence=min_silence)
    if not segs:
        raise VadError("未检测到语音（音乐/纯噪声/音量过低？），请确认素材是口播人声")

    # 每段前后保留留白（pad），重叠段合并
    dur = len(audio) / SR
    padded: list[tuple[float, float]] = []
    for s, e in segs:
        ps, pe = max(0.0, s - keep_pad), min(dur, e + keep_pad)
        if padded and ps <= padded[-1][1]:
            padded[-1] = (padded[-1][0], max(padded[-1][1], pe))
        else:
            padded.append((ps, pe))

    stem = Path(video_path).stem
    dst = engine._unique_dst(out_dir, f"{stem}_剪气口", ".mp4")

    # 若最终只有一段且覆盖全片 → 无静音可剪，直接复制
    if len(padded) == 1 and padded[0][0] <= 0.05 and padded[0][1] >= dur - 0.05:
        engine.run_ffmpeg(["-y", "-i", video_path, "-c", "copy", dst], cancel)
        return dst

    _concat_segments(video_path, padded, dst, cancel)
    return dst


def _concat_segments(src: str, segs: list[tuple[float, float]], dst: str,
                     cancel: Optional[Callable[[], bool]] = None) -> None:
    """多段 trim + concat（filter_complex 数字时间值，无路径注入面）。"""
    n = len(segs)
    parts: list[str] = []
    for i, (s, e) in enumerate(segs):
        parts.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
    fc = ";".join(parts)
    # concat 输入必须 v0,a0,v1,a1... 交替（否则 Media type mismatch）
    fc += ";" + "".join(f"[v{i}][a{i}]" for i in range(n))
    fc += f"concat=n={n}:v=1:a=1[v][a]"
    engine.run_ffmpeg([
        "-y", "-i", src, "-filter_complex", fc,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", dst,
    ], cancel)


def cut_batch(folder: str, out_dir: str,
              sensitivity: float = 0.5, min_silence: float = 0.6, keep_pad: float = 0.3,
              cancel: Optional[Callable[[], bool]] = None,
              progress: Optional[Callable[[int, int, str, str], None]] = None) -> dict:
    """批量剪气口：文件夹内所有视频。"""
    files = engine.collect_files(folder, engine.KIND_VIDEO)
    total = len(files)
    if total == 0:
        raise VadError("文件夹内没有视频文件")
    ok = skipped = failed = 0
    errors: list[dict] = []
    for i, f in enumerate(files, 1):
        if cancel is not None and cancel():
            if progress:
                progress(i, total, f, "stop")
            break
        try:
            cut_pauses(f, out_dir, sensitivity, min_silence, keep_pad, cancel)
            ok += 1
            if progress:
                progress(i, total, f, "ok")
        except VadError as e:
            if "取消" in str(e):
                if progress:
                    progress(i, total, f, "stop")
                break
            failed += 1
            errors.append({"name": Path(f).name, "error": str(e)})
            if progress:
                progress(i, total, f, "fail")
        except Exception as e:  # noqa: BLE001
            failed += 1
            errors.append({"name": Path(f).name, "error": f"未知错误：{e}"})
            if progress:
                progress(i, total, f, "fail")
    return {"total": total, "ok": ok, "skipped": skipped, "failed": failed, "errors": errors}
