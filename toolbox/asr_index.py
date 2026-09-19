"""工具箱-语音识别与索引：批量识别 / 断点续建 / 增量 / 进度。

- 识别引擎：faster-whisper（small 默认 / large-v3 精确模式），模型进程内单例
- 断点续建：已存在且模型一致的索引自动跳过，只识别新增/缺失项
- 顺带产出：句级时间轴（segments）+ 时长，供字幕包装/文本匹配/剪气口复用
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from toolbox import engine
from toolbox import models
from toolbox import store

WHISPER_SIZES = ["tiny", "base", "small", "medium", "large-v3"]
DEFAULT_MODEL = "small"


def transcribe_one(video_path: str, model_name: str = DEFAULT_MODEL,
                   cancel: Optional[Callable[[], bool]] = None) -> dict:
    """识别单个视频 → 索引条目。

    失败抛 engine.MediaError（含取消）。返回：
    {path, model, duration, segments: [{start, end, text}], ts}
    """
    if not Path(video_path).is_file():
        raise engine.MediaError(f"文件不存在：{video_path}")
    # 无音轨视频（纯画面素材）直接返回空索引，避免 whisper 内部崩溃（tuple index out of range）
    if not engine.has_audio(video_path):
        return {
            "path": video_path,
            "model": model_name,
            "duration": round(engine.probe_duration(video_path), 2),
            "segments": [],
            "ts": time.time(),
        }
    model = models.get_whisper(model_name)
    segments_iter, info = model.transcribe(
        video_path, vad_filter=True, beam_size=5, language=None,
        initial_prompt="以下是普通话的句子，使用简体中文输出。",
    )
    segs: list[dict] = []
    for s in segments_iter:
        if cancel is not None and cancel():
            raise engine.MediaError("已取消")
        text = (s.text or "").strip()
        if text:
            segs.append({"start": round(float(s.start), 2), "end": round(float(s.end), 2), "text": text})
    duration = float(info.duration or 0) or engine.probe_duration(video_path)
    return {
        "path": video_path,
        "model": model_name,
        "duration": round(duration, 2),
        "segments": segs,
        "ts": time.time(),
    }


def index_folder(folder: str, model_name: str = DEFAULT_MODEL,
                 cancel: Optional[Callable[[], bool]] = None,
                 progress: Optional[Callable[[int, int, str, str], None]] = None) -> dict:
    """批量索引文件夹（断点续建 + 增量）。

    :param progress: (当前序号, 总数, 文件路径, 状态 ok/skip/fail/stop)
    :return: {total, done, skipped, failed, errors:[{name, error}]}
    """
    files = engine.collect_files(folder, engine.KIND_VIDEO)
    total = len(files)
    done = skipped = failed = 0
    errors: list[dict] = []
    for i, f in enumerate(files, 1):
        if cancel is not None and cancel():
            if progress:
                progress(i, total, f, "stop")
            break
        existing = store.load_item(f)
        if existing is not None and existing.get("model") == model_name:
            skipped += 1
            if progress:
                progress(i, total, f, "skip")
            continue
        try:
            item = transcribe_one(f, model_name, cancel)
            store.save_item(f, item)
            done += 1
            if progress:
                progress(i, total, f, "ok")
        except engine.MediaError as e:
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
    return {"total": total, "done": done, "skipped": skipped, "failed": failed, "errors": errors}
