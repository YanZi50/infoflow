"""工具箱-模型管理：模型下载/路径/校验。

- faster-whisper 模型（small / large-v3）：首次使用时从 HuggingFace 下载（自动走 hf-mirror 镜像）
- 模型缓存目录：<程序根>/models/<模型名>
- 后续 bge（语义匹配）、silero-vad（剪气口）模型也统一登记在这里
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

_LOCK = threading.Lock()

# 模型下载进度状态（供前端轮询）：active/n/file/n/total/done/error
_DL: dict = {"active": False, "model": "", "file": "", "n": 0, "total": 0, "done": False, "error": None}


def download_state() -> dict:
    """当前模型下载进度快照（复制返回，避免外部篡改）。"""
    return dict(_DL)


def data_root() -> Path:
    """程序数据根目录（与 history/configs 同级）：便携版=exe 目录，开发版=项目目录。"""
    try:
        from video_engine import _app_root
        return Path(_app_root())
    except Exception:
        return Path(__file__).resolve().parent.parent


def models_dir() -> Path:
    """模型目录：便携版优先用打包内置的 _internal/models（随 exe 分发、目录可写），
    开发版用 <程序根>/models。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "models"
        if bundled.is_dir():
            return bundled
    d = data_root() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def set_hf_mirror() -> None:
    """HuggingFace 走国内镜像（可被用户环境变量覆盖）。"""
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")


def whisper_cached(name: str) -> bool:
    """该 whisper 模型是否已就绪：优先本地预置目录（models/faster-whisper-<name>/），
    其次 huggingface 缓存（models/models--Systran--faster-whisper-<name>/）。"""
    name = (name or "small").strip()
    try:
        local = models_dir() / f"faster-whisper-{name}"
        if (local / "model.bin").is_file() and (local / "config.json").is_file():
            return True
        base = models_dir() / f"models--Systran--faster-whisper-{name}"
        if base.is_dir():
            for snap in base.rglob("config.json"):
                return True
    except Exception:
        pass
    return False


def get_whisper(name: str):
    """获取（必要时下载）whisper 模型，进程内单例缓存。

    优先加载本地预置模型（models/faster-whisper-<name>/，随便携版打包）；
    缺失时才从 HuggingFace 下载（期间通过 download_state() 暴露进度）。
    """
    name = (name or "small").strip()
    if name not in {"tiny", "base", "small", "medium", "large-v3"}:
        name = "small"
    with _LOCK:
        if name in _MODELS:
            return _MODELS[name]
        from faster_whisper import WhisperModel
        local = models_dir() / f"faster-whisper-{name}"
        if (local / "model.bin").is_file() and (local / "config.json").is_file():
            model = WhisperModel(str(local), device="cpu", compute_type="int8")
            _MODELS[name] = model
            return model
        set_hf_mirror()
        _DL.update({"active": True, "model": name, "file": "", "n": 0, "total": 0, "done": False, "error": None})
        restore = _patch_tqdm()
        try:
            model = WhisperModel(name, device="cpu", compute_type="int8",
                                 download_root=str(models_dir()))
            _MODELS[name] = model
            _DL["done"] = True
            return model
        except Exception as e:
            _DL["error"] = str(e)
            raise
        finally:
            _DL["active"] = False
            if restore is not None:
                restore()


def _patch_tqdm():
    """拦截 tqdm 进度（huggingface_hub 下载用 tqdm 显示字节数），写入 _DL。

    仅在本进程内、下载期间生效；结束后恢复原始实现，避免污染其他用法。
    """
    try:
        from tqdm.std import tqdm as _tqdm
    except Exception:
        return None
    orig = _tqdm.update

    def update(self, n=1):
        try:
            if self.total and self.n is not None:
                _DL["n"] = int(self.n)
                _DL["total"] = int(self.total)
                d = (self.desc or "").strip()
                if d:
                    _DL["file"] = d
        except Exception:
            pass
        return orig(self, n)

    _tqdm.update = update
    return lambda: setattr(_tqdm, "update", orig)


_MODELS: dict = {}
