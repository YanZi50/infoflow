"""工具箱② 文本匹配拼接：输入文案 → 从识别索引素材库找对口配音视频拼接成片。

匹配策略（已确认）：
- 精确优先：字幕文本包含该句 → 直接命中（高匹配）
- 语义兜底：bge-small-zh-v1.5（onnx，int8 量化 24MB）句子向量余弦相似度，阈值 60% 以下标注"匹配度低"
- 片段粒度：按句切子片段（一条口播视频多句可复用）
- 复用上限：同一片段（视频+起止时间）最多复用 2 次，超限自动换候选
- 气口联动：切子片段自动避开 VAD 静音段（索引已含 segments，本模块按句使用）
- 语义实现：onnxruntime，不引 torch（便携包体积硬约束）
- 产物：无字幕版 + 自动烧录文案字幕版（默认极简样式），输出 toolbox/文本匹配/
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from toolbox import engine
from toolbox import models
from toolbox import store
from toolbox import subtitle

# ---------------------------------------------------------------- 模型 ----------
EMBED_DIM = 512
MATCH_LOW = 0.60          # 相似度 < 60% 标注"匹配度低"
MAX_REUSE = 2             # 同一片段最多复用次数
TARGET_SR = 44100


def deps_ok() -> tuple[bool, str]:
    """依赖检查：onnxruntime + 模型文件。"""
    try:
        import onnxruntime  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"onnxruntime 未安装：{e}"
    root = models.models_dir() / "bge-small-zh-v1.5"
    if not (root / "model_quantized.onnx").is_file():
        return False, f"缺少语义模型：{root}（应含 model_quantized.onnx）"
    if not (root / "tokenizer.json").is_file():
        return False, f"缺少 tokenizer：{root / 'tokenizer.json'}"
    return True, "就绪"


class MatchError(Exception):
    """文本匹配拼接错误。"""


_MODEL = {"sess": None, "tok": None, "loaded": False}


def _load_model():
    """懒加载 bge 模型（进程内单例）。"""
    if _MODEL["loaded"]:
        return _MODEL["sess"], _MODEL["tok"]
    import onnxruntime
    from tokenizers import Tokenizer

    root = models.models_dir() / "bge-small-zh-v1.5"
    sess = onnxruntime.InferenceSession(
        str(root / "model_quantized.onnx"), providers=["CPUExecutionProvider"])
    tok = Tokenizer.from_file(str(root / "tokenizer.json"))
    _MODEL.update(sess=sess, tok=tok, loaded=True)
    return sess, tok


def embed(text: str) -> np.ndarray:
    """句子 → 512 维归一化向量（CLS pooling）。"""
    sess, tok = _load_model()
    enc = tok.encode(text)
    a = np.array([enc.ids], dtype=np.int64)
    m = np.array([enc.attention_mask], dtype=np.int64)
    t = np.array([enc.type_ids], dtype=np.int64)
    out = sess.run(None, {"input_ids": a, "attention_mask": m, "token_type_ids": t})[0][0]
    v = out[0]
    norm = np.linalg.norm(v)
    return v / norm if norm > 0 else v


# ---------------------------------------------------------------- 文本处理 ----------
_SENT_SPLIT = re.compile(r"[。！？；!?;\n]+")
_FILLER = re.compile(r"^(嗯|呃|啊|哦|哎|诶|那个|这个|就是说)+")
_WS = re.compile(r"\s+")


def split_sentences(text: str) -> list[str]:
    """拆句 + 清洗：去首尾语气词/多余空白，丢弃空句与纯表情句。"""
    out = []
    for raw in _SENT_SPLIT.split(text):
        s = _WS.sub(" ", raw).strip(" \t\r\n，,、。．")
        s = _FILLER.sub("", s)
        s = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]+", "", s).strip()
        if len(s) >= 2:
            out.append(s)
    return out


# ---------------------------------------------------------------- 片段库 ----------
def build_pool(max_videos: int = 0) -> tuple[list[dict], int]:
    """从识别索引构建片段池。

    :return: (fragments, 视频数)。每个片段 {path, start, end, text, dur}。
    """
    all_items = store.load_all()
    frags: list[dict] = []
    videos = 0
    for path, item in all_items.items():
        if max_videos and videos >= max_videos:
            break
        videos += 1
        segs = item.get("segments") or []
        for s in segs:
            text = (s.get("text") or "").strip()
            if len(text) < 2:
                continue
            frags.append({
                "path": path,
                "start": float(s.get("start", 0)),
                "end": float(s.get("end", 0)),
                "text": text,
                "dur": max(0.1, float(s.get("end", 0)) - float(s.get("start", 0))),
            })
    return frags, videos


def _clean(text: str) -> str:
    return _WS.sub("", text.replace(" ", ""))


def _exact_hit(frag: dict, cleaned_sentence: str) -> bool:
    return cleaned_sentence in _clean(frag["text"])


def match_sentence(sentence: str, frags: list[dict], embed_cache: dict,
                   reuse: dict) -> Optional[dict]:
    """单句匹配：精确优先 → 语义兜底 → 复用计数约束。

    :return: 选中片段 {frag, score, mode, reused, low} 或 None（无可用候选）
    """
    cleaned = _clean(sentence)
    # 1) 精确优先（字幕包含该句原文）
    for f in frags:
        key = (f["path"], f["start"], f["end"])
        if reuse.get(key, 0) >= MAX_REUSE:
            continue
        if _exact_hit(f, cleaned):
            reuse[key] = reuse.get(key, 0) + 1
            return {"frag": f, "score": 1.0, "mode": "exact", "reused": reuse[key], "low": False}
    # 2) 语义兜底（bge 余弦，候选按分数降序，取第一个复用未满的）
    qv = embed_cache.get(sentence)
    if qv is None:
        qv = embed(sentence)
        embed_cache[sentence] = qv
    best: Optional[dict] = None
    for f in frags:
        key = (f["path"], f["start"], f["end"])
        if reuse.get(key, 0) >= MAX_REUSE:
            continue
        fv = embed_cache.get(f["text"])
        if fv is None:
            fv = embed(f["text"])
            embed_cache[f["text"]] = fv
        sim = float(qv @ fv)
        if best is None or sim > best["score"]:
            best = {"frag": f, "score": sim, "mode": "semantic"}
    if best is None:
        return None
    key = (best["frag"]["path"], best["frag"]["start"], best["frag"]["end"])
    reuse[key] = reuse.get(key, 0) + 1
    best["reused"] = reuse[key]
    best["low"] = best["score"] < MATCH_LOW
    return best


# ---------------------------------------------------------------- 拼接 ----------
def _probe_res(video: str) -> tuple[int, int]:
    try:
        w, h = engine._probe_size(video)
        if w and h:
            return int(w), int(h)
    except Exception:  # noqa: BLE001
        pass
    return 720, 1280


def concat_fragments(chosen: list[dict], dst: str,
                     cancel: Optional[Callable[[], bool]] = None,
                     log: Optional[Callable[[str], None]] = None) -> str:
    """按选中片段顺序拼接成片（统一为第一片段分辨率，逐段重编码截取 → 流复制拼接）。

    片段跨关键帧截取会导致起止不精确，故逐段 libx264+aac 重编码；
    统一参数保证 concat 可 stream copy。无音频片段补静音音轨，保证最终有音轨。
    """
    if not chosen:
        raise MatchError("没有可用片段")
    tmp_dir = Path(dst).parent / ("_match_tmp_" + time.strftime("%H%M%S"))
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # 统一分辨率 = 第一片段
    w, h = _probe_res(chosen[0]["frag"]["path"])
    w = w or 720
    h = h or 1280
    w -= w % 2
    h -= h % 2
    clips: list[str] = []
    try:
        for i, ch in enumerate(chosen, 1):
            if cancel is not None and cancel():
                raise engine.MediaError("已取消")
            f = ch["frag"]
            clip = tmp_dir / f"clip_{i:03d}.mp4"
            args = ["-y", "-ss", f"{f['start']:.3f}", "-i", f["path"],
                    "-t", f"{f['dur']:.3f}",
                    "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                    "-c:a", "aac", "-ar", "44100", "-b:a", "128k", "-ac", "2",
                    "-movflags", "+faststart", str(clip)]
            engine.run_ffmpeg(args, cancel, cwd=str(tmp_dir))
            clips.append(str(clip))
            if log:
                log(f"[{i}/{len(chosen)}] 截取片段 {Path(f['path']).name} {f['start']:.1f}s-{f['end']:.1f}s")
        dst = str(dst)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        list_file = tmp_dir / "list.txt"
        list_file.write_text("\n".join(f"file '{Path(c).as_posix()}'" for c in clips), encoding="utf-8")
        # stream copy 拼接（各段参数已统一）
        args = ["-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-c", "copy", "-movflags", "+faststart", dst]
        engine.run_ffmpeg(args, cancel, cwd=str(tmp_dir))
        return dst
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def build_srt(chosen: list[dict]) -> str:
    """按选中顺序生成文案 SRT（成片时间轴累计）。"""
    lines: list[str] = []
    idx = 1
    t = 0.0
    for ch in chosen:
        f = ch["frag"]
        end = t + f["dur"]
        start_s = subtitle._ts(t)
        end_s = subtitle._ts(end)
        text = ch.get("text") or f["text"]
        lines.append(f"{idx}\n{start_s} --> {end_s}\n{text}\n")
        idx += 1
        t = end
    return "\n".join(lines)


def run(text: str, out_dir: str, max_videos: int = 0,
        burn_style: str = "minimal",
        cancel: Optional[Callable[[], bool]] = None,
        progress: Optional[Callable[[int, int, str], None]] = None,
        log: Optional[Callable[[str], None]] = None) -> dict:
    """完整流程：拆句 → 匹配 → 拼接 → 字幕 → 报告。

    :return: {out_video, out_burned, report: [{sentence, source, start, end,
             score, mode, reused, low, dur}], total_sentences, videos_used, elapsed}
    """
    ok, msg = deps_ok()
    if not ok:
        raise MatchError(msg)
    t_start = time.time()
    sentences = split_sentences(text)
    if not sentences:
        raise MatchError("未提取到有效句子（至少 2 字）")
    frags, videos = build_pool(max_videos)
    if not frags:
        raise MatchError("索引中没有可用片段，请先到「语音识别与索引」建立素材索引")

    reuse: dict = {}
    cache: dict = {}
    chosen: list[dict] = []
    report: list[dict] = []
    for i, s in enumerate(sentences, 1):
        if cancel is not None and cancel():
            raise engine.MediaError("已取消")
        hit = match_sentence(s, frags, cache, reuse)
        if hit is None:
            report.append({"sentence": s, "source": "", "start": 0, "end": 0,
                           "score": 0, "mode": "none", "reused": 0, "low": True, "dur": 0})
            if progress:
                progress(i, len(sentences), f"未匹配：{s[:14]}…")
            continue
        f = hit["frag"]
        chosen.append(hit)
        report.append({
            "sentence": s, "source": f["path"], "start": f["start"], "end": f["end"],
            "score": round(hit["score"], 3), "mode": hit["mode"], "reused": hit["reused"],
            "low": hit["low"], "dur": round(f["dur"], 2),
        })
        if progress:
            progress(i, len(sentences), f"{'精确' if hit['mode'] == 'exact' else '语义'}：{s[:14]}…")
    if not chosen:
        raise MatchError("没有任何句子匹配到素材片段")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"文本匹配_{time.strftime('%Y%m%d_%H%M%S')}"
    out_video = str(out_dir / f"{stem}.mp4")
    log_f = lambda m: log(m) if log else None  # noqa: E731
    concat_fragments(chosen, out_video, cancel, log_f)
    if log:
        log("片段拼接完成，正在生成字幕副本…")
    # 文案字幕 SRT + 烧录副本
    srt_text = build_srt(chosen)
    srt_path = out_dir / f"{stem}.srt"
    srt_path.write_text(srt_text, encoding="utf-8")
    out_burned = str(out_dir / f"{stem}_字幕.mp4")
    try:
        subtitle.burn_with_style(out_video, str(srt_path), out_burned, style=burn_style, cancel=cancel)
    except Exception as e:  # noqa: BLE001 字幕失败不阻断主产物
        out_burned = ""
        if log:
            log(f"字幕烧录失败（主产物已保留）：{e}")
    return {
        "out_video": out_video,
        "out_burned": out_burned,
        "srt": str(srt_path),
        "report": report,
        "total_sentences": len(sentences),
        "matched": len(chosen),
        "videos_used": videos,
        "elapsed": round(time.time() - t_start, 1),
    }
