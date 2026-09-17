"""工具箱-索引存储：原子写、损坏单条自愈（不整库崩）。

索引条目以「视频路径的 sha1 前 16 位」命名存于 <程序根>/index/<hash>.json，
条目内记录原始 path 用于一致性校验（哈希冲突/文件错位视为损坏，返回 None）。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from toolbox.models import data_root


def index_root() -> Path:
    d = data_root() / "index"
    d.mkdir(parents=True, exist_ok=True)
    return d


def item_path(video_path: str) -> Path:
    h = hashlib.sha1(video_path.encode("utf-8")).hexdigest()[:16]
    return index_root() / f"{h}.json"


def load_item(video_path: str) -> dict | None:
    """读取单条索引；不存在/损坏/路径不一致 → None（自愈：调用方会重建该条）。"""
    p = item_path(video_path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("path") != video_path:
            return None
        if not isinstance(data.get("segments"), list):
            return None
        return data
    except Exception:
        return None


def save_item(video_path: str, data: dict) -> None:
    p = item_path(video_path)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=None), encoding="utf-8")
    os.replace(tmp, p)  # 原子替换


def remove_item(video_path: str) -> None:
    try:
        item_path(video_path).unlink(missing_ok=True)
    except OSError:
        pass


def load_all() -> dict[str, dict]:
    """载入全部有效索引：path -> item（用于匹配/统计）。损坏条目跳过。"""
    out: dict[str, dict] = {}
    for p in index_root().glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data.get("path"), str) and isinstance(data.get("segments"), list):
                out[data["path"]] = data
        except Exception:
            continue
    return out


def stats() -> dict:
    """索引统计：条数、模型分布、总时长。"""
    items = load_all()
    by_model: dict[str, int] = {}
    total_dur = 0.0
    for it in items.values():
        m = it.get("model") or "unknown"
        by_model[m] = by_model.get(m, 0) + 1
        total_dur += float(it.get("duration") or 0)
    return {
        "count": len(items),
        "by_model": by_model,
        "total_duration": round(total_dur, 1),
    }


def clear_index() -> int:
    """清空全部索引，返回删除条数。"""
    n = 0
    for p in index_root().glob("*.json"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n
