"""工具箱-AI 配音：sherpa-onnx 离线中文 TTS（vits 模型）。

- 音色：female（vits-icefall-zh-aishell3，女声多音色）/ male（vits-zh-hf-fanchen-wnj，男声）
- 语速：0.5~2.0（内部映射 length_scale）
- 懒加载 sherpa_onnx：依赖缺失时仅此模块不可用，不影响其他功能（模块化设计，可整体弃用）
- 模型目录：models/tts/<voice_dir>/（随项目/便携版预置）

依赖：pip install sherpa-onnx（自带 onnxruntime 兼容 1.30）
"""

from __future__ import annotations

import os
import threading
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from toolbox.models import models_dir

_LOCK = threading.Lock()
_SYNTHESIZERS: dict = {}
_SAMPLE_RATES: dict = {}

VOICES: dict = {
    "female": {
        "name": "中文女声（Piper-huayan）",
        "dir": "vits-piper-zh_CN-huayan-medium",
        "onnx": "zh_CN-huayan-medium.onnx",
        "sid": None,
        "engine": "piper",
        "espeak_dir": "espeak-ng-data",   # piper 音素数据（随包预置）
    },
    "male": {
        "name": "中文男声（fanchen-wnj）",
        "dir": "vits-zh-hf-fanchen-wnj",
        "onnx": "vits-zh-hf-fanchen-wnj.onnx",
        "sid": None,
        "engine": "vits",
    },
}

MAX_SEG_CHARS = 200  # 超长文本按此长度分段合成后拼接


class TtsError(Exception):
    """配音错误，message 面向用户。"""


def tts_dir() -> Path:
    return models_dir() / "tts"


def deps_ok() -> bool:
    """sherpa-onnx 是否可用（未安装时 TTS 不可用，不影响其他模块）。"""
    try:
        import sherpa_onnx  # noqa: F401
        return True
    except Exception:
        return False


def voice_ready(voice: str) -> bool:
    cfg = VOICES.get(voice)
    if not cfg:
        return False
    d = tts_dir() / cfg["dir"]
    ok = (d / cfg["onnx"]).is_file() and (d / "tokens.txt").is_file()
    if cfg.get("engine") == "vits":
        ok = ok and (d / "lexicon.txt").is_file()
    elif cfg.get("engine") == "piper":
        ok = ok and (tts_dir() / cfg.get("espeak_dir", "") / "phontab").is_file()
    return ok


def available_voices() -> list[str]:
    return [v for v in VOICES if voice_ready(v)]


def _get_synthesizer(voice: str, speed: float):
    """按 音色+语速 缓存合成器（模型加载一次；语速变化重建轻量）。"""
    key = (voice, round(float(speed), 2))
    if key in _SYNTHESIZERS:
        return _SYNTHESIZERS[key], _SAMPLE_RATES[key]
    with _LOCK:
        if key in _SYNTHESIZERS:
            return _SYNTHESIZERS[key], _SAMPLE_RATES[key]
        if not voice_ready(voice):
            raise TtsError(f"音色「{VOICES[voice]['name']}」模型缺失：请确认 models/tts/{VOICES[voice]['dir']} 完整")
        try:
            import sherpa_onnx
        except Exception as e:  # noqa: BLE001
            raise TtsError(f"缺少 sherpa-onnx 依赖：{e}") from e
        cfg = VOICES[voice]
        d = tts_dir() / cfg["dir"]
        length_scale = 1.0 / float(speed)
        # Piper 系列（中文女声）本质是 VITS：lexicon 留空、data_dir 指向 espeak-ng-data
        is_piper = cfg.get("engine") == "piper"
        model_cfg = sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=str(d / cfg["onnx"]),
                lexicon="" if is_piper else str(d / "lexicon.txt"),
                tokens=str(d / "tokens.txt"),
                data_dir=str(tts_dir() / cfg["espeak_dir"]) if is_piper else "",
                length_scale=length_scale,
                noise_scale=0.667,
                noise_scale_w=0.8,
            ),
        )
        tts_config = sherpa_onnx.OfflineTtsConfig(
            model=model_cfg,
            rule_fsts="",
        )
        tts = sherpa_onnx.OfflineTts(tts_config)
        _SYNTHESIZERS[key] = tts
        _SAMPLE_RATES[key] = int(tts.sample_rate)
        return tts, _SAMPLE_RATES[key]


def _split_text(text: str) -> list[str]:
    """按句读/长度切分，避免单次合成过长。"""
    text = (text or "").strip()
    if not text:
        raise TtsError("请输入要合成的文本")
    pieces: list[str] = []
    cur = ""
    for ch in text.replace("\r", ""):
        cur += ch
        if len(cur) >= MAX_SEG_CHARS or ch in "。！？；\n，":
            if ch in "，\n" and len(cur) < MAX_SEG_CHARS * 0.6:
                continue
            pieces.append(cur.strip())
            cur = ""
    if cur.strip():
        pieces.append(cur.strip())
    return [p for p in pieces if p]


def synthesize(text: str, voice: str = "female", speed: float = 1.0,
               out_path: str | Path = None,
               cancel: Optional[Callable[[], bool]] = None,
               progress: Optional[Callable[[int], None]] = None,
               service_url: str = "") -> tuple[str, int]:
    """合成文本为 wav，返回 (输出路径, 采样率)。

    支持长文本（自动分段合成拼接）；语速 0.5~2.0（内部 length_scale=1/speed）。
    progress(i) 在每段合成完成后回调（i 从 1 起）。
    service_url 非空时走远程配音服务（OpenAI 兼容 /v1/audio/speech，如本地部署的 VoxCPM），
    留空=本地 sherpa-onnx 离线合成（模块化：远程服务不可用时本地照常工作）。
    """
    voice = voice if voice in VOICES else "female"
    speed = max(0.5, min(2.0, float(speed)))
    if out_path is None:
        out_path = str(Path.cwd() / f"tts_{voice}_{int(os.getpid())}.wav")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if service_url and str(service_url).strip():
        return _synthesize_remote(text, voice, out_path, str(service_url).strip(),
                                  cancel=cancel, progress=progress)

    pieces = _split_text(text)
    tts, sr = _get_synthesizer(voice, speed)

    all_samples: list[np.ndarray] = []
    for i, piece in enumerate(pieces):
        if cancel is not None and cancel():
            raise TtsError("已取消")
        audio = tts.generate(piece, sid=VOICES[voice]["sid"] or 0, speed=speed)
        samples = getattr(audio, "samples", audio)
        if samples is None or len(samples) == 0:
            raise TtsError(f"第 {i + 1} 段合成失败：{piece[:20]}…")
        arr = np.asarray(samples)
        if arr.dtype.kind == "f":      # sherpa 1.13.8 返回 float（-1~1），需放大为 int16
            arr = np.clip(arr, -1.0, 1.0) * 32767.0
        all_samples.append(arr.astype(np.int16))
        if progress is not None:
            progress(i + 1)
    merged = np.concatenate(all_samples) if len(all_samples) > 1 else all_samples[0]

    tmp = out_path.with_suffix(".tmp" + out_path.suffix)
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(merged.tobytes())
    os.replace(str(tmp), str(out_path))
    return str(out_path), sr


def _synthesize_remote(text: str, voice: str, out_path: Path,
                       service_url: str,
                       cancel: Optional[Callable[[], bool]] = None,
                       progress: Optional[Callable[[int], None]] = None) -> tuple[str, int]:
    """远程配音服务（OpenAI 兼容 /v1/audio/speech，如 VoxCPM/vLLM-Omni）。

    返回 wav 字节直写文件；采样率以服务端返回为准（保存后由 ffprobe 探测）。
    """
    import json as _json
    import urllib.request

    if cancel is not None and cancel():
        raise TtsError("已取消")
    url = service_url.rstrip("/") + "/v1/audio/speech"
    payload = {"model": "VoxCPM2", "input": text, "voice": voice, "response_format": "wav"}
    req = urllib.request.Request(
        url, data=_json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = resp.read()
    except Exception as e:  # noqa: BLE001
        raise TtsError(f"远程配音服务连接失败：{e}") from e
    if not data:
        raise TtsError("远程配音服务返回空音频")
    tmp = out_path.with_suffix(".tmp" + out_path.suffix)
    tmp.write_bytes(data)
    os.replace(str(tmp), str(out_path))
    # 探测采样率（服务端可能返回任意采样率 wav）
    sr = 16000
    try:
        import wave
        with wave.open(str(out_path), "rb") as w:
            sr = w.getframerate()
    except Exception:  # noqa: BLE001 非标准 wav 头时回退 16k
        pass
    if progress is not None:
        progress(1)
    return str(out_path), int(sr)
