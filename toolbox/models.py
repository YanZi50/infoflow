"""工具箱-模型管理：模型下载/路径/校验。

- faster-whisper 模型（small / large-v3）：首次使用时从 HuggingFace 下载（自动走 hf-mirror 镜像）
- 模型缓存目录：<程序根>/models/<模型名>
- 后续 bge（语义匹配）、silero-vad（剪气口）模型也统一登记在这里
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

_LOCK = threading.Lock()


def data_root() -> Path:
    """程序数据根目录（与 history/configs 同级）：便携版=exe 目录，开发版=项目目录。"""
    try:
        from video_engine import _app_root
        return Path(_app_root())
    except Exception:
        return Path(__file__).resolve().parent.parent


def models_dir() -> Path:
    d = data_root() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def set_hf_mirror() -> None:
    """HuggingFace 走国内镜像（可被用户环境变量覆盖）。"""
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")


def whisper_cached(name: str) -> bool:
    """该 whisper 模型是否已下载完成（目录内有 config.json 即视为已缓存）。"""
    try:
        base = models_dir() / f"models--Systran--faster-whisper-{name}"
        if base.is_dir():
            for snap in base.rglob("config.json"):
                return True
    except Exception:
        pass
    return False


def get_whisper(name: str):
    """获取（必要时下载）whisper 模型，进程内单例缓存。"""
    name = (name or "small").strip()
    if name not in {"tiny", "base", "small", "medium", "large-v3"}:
        name = "small"
    with _LOCK:
        if name in _MODELS:
            return _MODELS[name]
        from faster_whisper import WhisperModel
        set_hf_mirror()
        model = WhisperModel(name, device="cpu", compute_type="int8",
                             download_root=str(models_dir()))
        _MODELS[name] = model
        return model


_MODELS: dict = {}
