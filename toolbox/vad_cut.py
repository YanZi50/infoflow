"""工具箱-剪气口：silero-vad 静音段检测 + ffmpeg 拼接去停顿。

- 输入：口播/人声视频（音乐、纯画面素材请勿使用——无语音时无法判定）
- 参数（5 项）：灵敏度、最小静音、前垫片、后垫片、最长静音上限
- 切分规则（前后端一致，见 compute_cuts）：
    静音间隔 < min_silence        → 合并为同一语音段
    min_silence ≤ 间隔 ≤ max_silence → 作为剪切点
    间隔 > max_silence            → 视为有意停顿，合并保留（不剪）
- analyze_pauses：预览模式，VAD 只推理一次返回 probs+waveform，前端本地重算切分点（拖动参数零延迟）
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


def _load_audio(video_path: str, tmp_dir: str,
                cancel: Optional[Callable[[], bool]] = None) -> np.ndarray:
    """提取并读取 16k 单声道音频（调用方负责清理 tmp_wav）。"""
    tmp_wav = str(Path(tmp_dir) / "_vad_tmp.wav")
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        _extract_wav(video_path, tmp_wav, cancel)
        if cancel and cancel():
            raise VadError("已取消")
        return _read_wav(tmp_wav)
    finally:
        try:
            os.unlink(tmp_wav)
        except OSError:
            pass


def vad_probs(audio: np.ndarray) -> list[float]:
    """silero-vad 逐帧说话概率（每 512 样本 / 32ms 一点），不做任何切分。"""
    sess = _session()
    sr_in = np.array([SR], dtype=np.int64)
    state = np.zeros((2, 1, 128), dtype=np.float32)
    context = np.zeros(64, dtype=np.float32)   # context_size=64 (16k)，与官方 OnnxWrapper 一致
    probs: list[float] = []
    n = len(audio)
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
    return probs


def segments_from_probs(probs: list[float], threshold: float = 0.5,
                        min_speech: float = 0.25) -> list[tuple[float, float]]:
    """概率序列 → 原始语音段（阈值滞后切分 + 短段过滤，不做间隔合并）。

    :param threshold: 说话概率阈值（灵敏度，0.2~0.9，越低越敏感）
    :param min_speech: 最短语音段（秒），过短丢弃
    """
    n = len(probs)
    win_t = WINDOW / SR
    neg_threshold = max(threshold - 0.15, 0.01)
    raw: list[tuple[float, float]] = []
    triggered = False
    start = 0.0
    for i, p in enumerate(probs):
        t = i * win_t
        if not triggered and p >= threshold:
            triggered = True
            start = t
        elif triggered and p < neg_threshold:
            triggered = False
            raw.append((start, t))
    if triggered:
        raw.append((start, n * win_t))
    return [(s, e) for s, e in raw if e - s >= min_speech]


def compute_cuts(segs: list[tuple[float, float]], dur: float,
                 min_silence: float = 0.6, max_silence: float = 5.0,
                 pad_before: float = 0.3, pad_after: float = 0.3) -> list[tuple[float, float]]:
    """统一切分规则（后端正式剪 & 前端预览重算必须一致）。

    :param segs: 原始语音段（segments_from_probs 输出，未做间隔合并）
    :param dur: 音频总时长（秒）
    :param min_silence: 最短静音（秒），间隔达到才剪
    :param max_silence: 最长静音上限（秒），间隔超过视为有意停顿不剪（合并保留）
    :param pad_before: 剪切点前保留留白（秒）
    :param pad_after: 剪切点后保留留白（秒）
    :return: 保留段列表 [(start, end), ...]
    """
    # 1) 间隔合并/切分
    merged: list[tuple[float, float]] = []
    for s, e in segs:
        if merged:
            gap = s - merged[-1][1]
            if gap < min_silence or gap > max_silence:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        else:
            merged.append((s, e))
    # 2) 前后留白（pad）+ 重叠合并
    padded: list[tuple[float, float]] = []
    for s, e in merged:
        ps, pe = max(0.0, s - pad_before), min(dur, e + pad_after)
        if padded and ps <= padded[-1][1]:
            padded[-1] = (padded[-1][0], max(padded[-1][1], pe))
        else:
            padded.append((ps, pe))
    return padded


def analyze_pauses(video_path: str, sensitivity: float = 0.5, min_silence: float = 0.6,
                   max_silence: float = 5.0, pad_before: float = 0.3, pad_after: float = 0.3,
                   cancel: Optional[Callable[[], bool]] = None) -> dict:
    """预览模式：只分析不输出，返回前端重算所需数据。

    - VAD 只推理一次；切分点由前端本地重算（拖动参数零延迟）
    - waveform：min-max 包络降采样 ~600 点，供 canvas 画波形
    - cuts：按当前参数算好的保留段（初始展示用，前端拖动参数后本地重算）
    """
    if not Path(video_path).is_file():
        raise VadError(f"文件不存在：{video_path}")
    if not engine.has_audio(video_path):
        raise VadError("该视频没有音轨（纯画面素材），无法剪气口")

    audio = _load_audio(video_path, str(Path(video_path).parent), cancel)
    dur = len(audio) / SR
    probs = vad_probs(audio)
    segs = segments_from_probs(probs, sensitivity, min_speech=0.25)
    cuts = compute_cuts(segs, dur, min_silence, max_silence, pad_before, pad_after)

    # min-max 包络降采样（画波形用）
    n_bins = 600
    wf: list[float] = []
    if len(audio):
        step = max(1, len(audio) // n_bins)
        for i in range(0, len(audio), step):
            wf.append(float(np.max(np.abs(audio[i:i + step]))))
    if not wf:
        wf = [0.0]
    return {"dur": dur, "probs": probs, "waveform": wf, "sr": SR, "win": WINDOW / SR,
            "cuts": cuts, "segs": segs}


def cut_pauses(video_path: str, out_dir: str,
               sensitivity: float = 0.5, min_silence: float = 0.6,
               pad_before: float = 0.3, pad_after: float = 0.3, max_silence: float = 5.0,
               cancel: Optional[Callable[[], bool]] = None) -> str:
    """剪掉口播视频中的长静音，输出拼接后的连续视频。

    :param sensitivity: VAD 灵敏度（0.2~0.9，越低越敏感）
    :param min_silence: 最短静音长度（秒），达到才剪
    :param pad_before: 剪切点前保留留白（秒）
    :param pad_after: 剪切点后保留留白（秒）
    :param max_silence: 最长静音上限（秒），超过视为有意停顿不剪
    :return: 输出文件路径
    """
    sensitivity = max(0.2, min(0.9, float(sensitivity)))
    min_silence = max(0.1, min(10.0, float(min_silence)))
    pad_before = max(0.0, min(3.0, float(pad_before)))
    pad_after = max(0.0, min(3.0, float(pad_after)))
    max_silence = max(1.0, min(30.0, float(max_silence)))

    audio = _load_audio(video_path, out_dir, cancel)
    dur = len(audio) / SR
    probs = vad_probs(audio)
    segs = segments_from_probs(probs, sensitivity, min_speech=0.25)
    if not segs:
        raise VadError("未检测到语音（音乐/纯噪声/音量过低？），请确认素材是口播人声")
    padded = compute_cuts(segs, dur, min_silence, max_silence, pad_before, pad_after)

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


def _preview_audio_segments(video_path: str, mode: str, sensitivity: float, min_silence: float,
                            max_silence: float, pad_before: float, pad_after: float,
                            cancel: Optional[Callable[[], bool]] = None) -> list[tuple[float, float]]:
    """试听用片段：
    - orig: 第一处剪切点附近的静音片段（前 2s ~ 后 2s，约 4s），听"被剪掉的停顿"
    - cut:  前 3 个保留段各取开头 2s 拼接（约 6s），听"剪后效果"
    """
    audio = _load_audio(video_path, str(Path(video_path).parent), cancel)
    dur = len(audio) / SR
    probs = vad_probs(audio)
    segs = segments_from_probs(probs, sensitivity, min_speech=0.25)
    cuts = compute_cuts(segs, dur, min_silence, max_silence, pad_before, pad_after)

    if mode == "orig":
        # 找第一个真正的剪切点（相邻保留段之间的空隙）
        for i in range(1, len(cuts)):
            gap_s, gap_e = cuts[i - 1][1], cuts[i][0]
            if gap_e - gap_s >= min_silence:
                mid = (gap_s + gap_e) / 2
                s, e = max(0.0, mid - 2.0), min(dur, mid + 2.0)
                return [(s, e)]
        return []
    # cut 模式：前 3 个保留段各取 2s
    out: list[tuple[float, float]] = []
    for s, e in cuts[:3]:
        out.append((s, min(e, s + 2.0)))
    return out


def preview_audio(video_path: str, out_mp3: str, mode: str = "cut", sensitivity: float = 0.5,
                  min_silence: float = 0.6, max_silence: float = 5.0,
                  pad_before: float = 0.3, pad_after: float = 0.3,
                  cancel: Optional[Callable[[], bool]] = None) -> str:
    """生成试听音频（mp3）到 out_mp3，返回路径。mode: orig=被剪停顿 / cut=剪后效果。"""
    segs = _preview_audio_segments(video_path, mode, sensitivity, min_silence,
                                   max_silence, pad_before, pad_after, cancel)
    if not segs:
        raise VadError("未找到可试听片段")
    if len(segs) == 1:
        s, e = segs[0]
        engine.run_ffmpeg(["-y", "-i", video_path, "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}",
                           "-vn", "-c:a", "libmp3lame", "-q:a", "5", out_mp3], cancel)
    else:
        _concat_audio_segments(video_path, segs, out_mp3, cancel)
    return out_mp3


def _concat_audio_segments(src: str, segs: list[tuple[float, float]], dst: str,
                           cancel: Optional[Callable[[], bool]] = None) -> None:
    """多段音频拼接为 mp3（试听用，不重编码视频）。"""
    n = len(segs)
    parts = [f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]"
             for i, (s, e) in enumerate(segs)]
    fc = ";".join(parts)
    fc += ";" + "".join(f"[a{i}]" for i in range(n))
    fc += f"concat=n={n}:v=0:a=1[a]"
    engine.run_ffmpeg(["-y", "-i", src, "-filter_complex", fc, "-map", "[a]",
                       "-c:a", "libmp3lame", "-q:a", "5", dst], cancel)


def cut_batch(folder: str, out_dir: str,
              sensitivity: float = 0.5, min_silence: float = 0.6,
              pad_before: float = 0.3, pad_after: float = 0.3, max_silence: float = 5.0,
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
            cut_pauses(f, out_dir, sensitivity, min_silence, pad_before, pad_after,
                       max_silence, cancel)
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
