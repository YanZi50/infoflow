"""工具箱-字幕包装：ASS 模板生成 / SRT 解析 / 烧录。

输入：① 语音识别索引（segments）或 外部 SRT/ASS 文件
样式模板（docs/工具箱设计方案.md 第 5 节）：
  minimal  极简白字（细描边，底部）
  outline  描边黄字（粗黑描边）
  bubble   气泡条（半透明圆角背景盒 + 白字）
  danmaku  弹幕风（顶部横向滚动）

所有 ffmpeg 调用走 toolbox/engine.py 封装（防注入/超时/取消）。
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Callable, Optional

from toolbox import engine

STYLE_NAMES = ["minimal", "outline", "bubble", "danmaku"]
DEFAULT_STYLE = "minimal"

# ASS 颜色为 BGR（&HBBGGRR）
_STYLES: dict[str, dict] = {
    "minimal": {
        "fontsize": 52,
        "primary": "&H00FFFFFF",     # 白
        "outline_color": "&H00181818",  # 深灰描边
        "borderstyle": 1, "outline_w": 2, "shadow": 1,
        "alignment": 2,              # 底部居中
    },
    "outline": {
        "fontsize": 54,
        "primary": "&H0000FFFF",     # 黄（BGR：FFFF00 → 00FFFF）
        "outline_color": "&H00000000",  # 黑
        "borderstyle": 1, "outline_w": 3, "shadow": 2,
        "alignment": 2,
    },
    "bubble": {
        "fontsize": 48,
        "primary": "&H00FFFFFF",
        "outline_color": "&H00000000",
        "back": "&HA0000000",        # 半透明黑背景盒
        "borderstyle": 3, "outline_w": 1, "shadow": 0,
        "alignment": 2,
    },
    "danmaku": {
        "fontsize": 46,
        "primary": "&H00FFFFFF",
        "outline_color": "&H00000000",
        "borderstyle": 1, "outline_w": 2, "shadow": 0,
        "alignment": 8,              # 顶部居中
    },
}

_PLAYRES = (1280, 720)


class SubtitleError(Exception):
    """字幕处理错误，message 面向用户。"""


def _ts(seconds: float) -> str:
    """秒 → ASS 时间轴 0:00:00.00（统一按厘秒整数计算，杜绝进位 bug）。"""
    if seconds < 0:
        seconds = 0
    total_cs = int(round(float(seconds) * 100))
    h = total_cs // 360000
    m = (total_cs % 360000) // 6000
    s = (total_cs % 6000) // 100
    cs = total_cs % 100
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    """ASS 文本转义：花括号、反斜杠及换行（防样式标签注入/破坏）。"""
    text = (text or "").replace("\\", "\\\\")
    text = text.replace("{", "\\{").replace("}", "\\}")
    text = text.replace("\n", " ")
    text = text.replace("\r", "")
    return text


def segments_from_index(video_path: str) -> list[dict]:
    """从① 语音识别索引读取句时间轴；未索引/无文本 → SubtitleError。"""
    from toolbox import store
    item = store.load_item(video_path)
    if item is None:
        raise SubtitleError("该视频尚未建立识别索引（请先到「语音识别与索引」处理）")
    segs = [s for s in item.get("segments") or [] if (s.get("text") or "").strip()]
    if not segs:
        raise SubtitleError("索引中没有识别到文本（无声/纯音乐素材）")
    return segs


def parse_srt(text: str) -> list[dict]:
    """解析 SRT 文本 → [{start, end, text}]。宽松解析，坏块跳过。"""
    segs: list[dict] = []
    blocks = re.split(r"\r?\n\r?\n", text.strip())
    time_re = re.compile(
        r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
        r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
    )
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        # SRT 结构：序号行 / 时间轴行 / 文本行；时间轴行含 -->
        time_idx = None
        for idx, ln in enumerate(lines):
            if "-->" in ln:
                time_idx = idx
                break
        if time_idx is None:
            continue
        m = time_re.search(lines[time_idx])
        if not m:
            continue
        def to_s(g):
            h, mi, s, ms = int(m.group(g)), int(m.group(g + 1)), int(m.group(g + 2)), int(m.group(g + 3))
            if len(str(ms)) == 1:
                ms *= 100
            elif len(str(ms)) == 2:
                ms *= 10
            return h * 3600 + mi * 60 + s + ms / 1000.0
        start = to_s(1)
        end = to_s(5)
        text = " ".join(lines[time_idx + 1:]).strip()
        if text and end > start:
            segs.append({"start": round(start, 2), "end": round(end, 2), "text": text})
    if not segs:
        raise SubtitleError("SRT 解析失败：未找到有效字幕块")
    return segs


def parse_ass(text: str) -> list[dict]:
    """解析 ASS 文本 → [{start, end, text}]（仅取 Dialogue 行，忽略样式标签）。"""
    segs: list[dict] = []
    for ln in text.splitlines():
        if not ln.startswith("Dialogue:"):
            continue
        parts = ln.split(",", 9)
        if len(parts) < 10:
            continue
        start = parts[1].strip()
        end = parts[2].strip()
        txt = parts[9].strip()
        if not txt:
            continue
        def p(t):
            h, m, s = t.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
        try:
            s0, e0 = p(start), p(end)
        except Exception:
            continue
        # 去 ASS 样式标签 {\...}
        plain = re.sub(r"\{[^}]*\}", "", txt).replace(r"\N", " ").replace(r"\n", " ").strip()
        if plain and e0 > s0:
            segs.append({"start": round(s0, 2), "end": round(e0, 2), "text": plain})
    if not segs:
        raise SubtitleError("ASS 解析失败：未找到有效 Dialogue 行")
    return segs


def _style_block(style: str) -> str:
    cfg = dict(_STYLES.get(style, _STYLES[DEFAULT_STYLE]))
    fontsize = cfg.pop("fontsize")
    alignment = cfg.pop("alignment")
    primary = cfg.pop("primary")
    back = cfg.pop("back", "&H00000000")
    borderstyle = cfg.pop("borderstyle")
    outline_w = cfg.pop("outline_w")
    shadow = cfg.pop("shadow")
    outline_color = cfg.pop("outline_color")
    return (
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Sub,Microsoft YaHei,{fontsize},{primary},&H00FFFFFF,{outline_color},{back},"
        f"0,0,0,0,100,100,0,0,{borderstyle},{outline_w},{shadow},{alignment},30,30,30,1\n"
    )


def build_ass(segments: list[dict], style: str = DEFAULT_STYLE) -> str:
    """根据句时间轴生成 ASS 内容字符串。"""
    if style not in _STYLES:
        raise SubtitleError(f"未知字幕样式：{style}")
    w, h = _PLAYRES
    header = (
        "[Script Info]\n"
        f"ScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n"
        "Collisions: Normal\n\n"
    ) + _style_block(style) + "\n[Events]\n"
    header += "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    lines: list[str] = []
    for i, seg in enumerate(segments):
        start = _ts(float(seg["start"]))
        end = _ts(float(seg["end"]))
        text = ass_escape(seg["text"])
        if style == "danmaku":
            lane = 40 + (i % 5) * 100          # 顶部 5 条弹幕道
            speed = max(8, min(22, len(seg["text"]) * 2))
            lines.append(
                f"Dialogue: 0,{start},{end},Sub,,0,0,0,,"
                f"{{\\move({w + 80},{lane},-{80 + len(seg['text']) * 26},{lane})\\fad(150,150)}}{text}"
            )
        else:
            lines.append(f"Dialogue: 0,{start},{end},Sub,,0,0,0,,{text}")
    return header + "\n".join(lines)


def burn(video_path: str, ass_content: str, dst: str,
         cancel: Optional[Callable[[], bool]] = None) -> None:
    """把 ASS 烧录到视频（重编码），dst 为输出 mp4。

    Windows 盘符冒号（D:）会被 ass= 滤镜误解析为选项分隔符，
    因此把 ASS 写到输出目录、滤镜用相对文件名引用（cwd 指向输出目录），彻底绕开路径转义。
    """
    out_dir = Path(dst).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    ass_rel = "_tbx_subtitle.ass"
    ass_path = out_dir / ass_rel
    ass_path.write_text(ass_content, encoding="utf-8-sig")
    try:
        engine.run_ffmpeg([
            "-y", "-i", video_path, "-vf", f"ass={ass_rel}",
            "-c:v", "libx264", "-preset", "medium",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", dst,
        ], cancel, cwd=str(out_dir))
    finally:
        try:
            ass_path.unlink()
        except OSError:
            pass


def process_video(video_path: str, segments: list[dict], style: str,
                  out_dir: str, cancel: Optional[Callable[[], bool]] = None) -> str:
    """单视频字幕烧录 → 返回输出路径。"""
    ass = build_ass(segments, style)
    stem = Path(video_path).stem
    dst = engine._unique_dst(out_dir, f"{stem}_字幕", ".mp4")
    burn(video_path, ass, dst, cancel)
    return dst


def index_burn_videos(folder: str, style: str, out_dir: str,
                      cancel: Optional[Callable[[], bool]] = None,
                      progress: Optional[Callable[[int, int, str, str], None]] = None) -> dict:
    """批量：文件夹内每个视频用①索引字幕烧录。未索引/无声 → failed 记录。"""
    files = engine.collect_files(folder, engine.KIND_VIDEO)
    total = len(files)
    if total == 0:
        raise SubtitleError("文件夹内没有视频文件")
    ok = skipped = failed = 0
    errors: list[dict] = []
    for i, f in enumerate(files, 1):
        if cancel is not None and cancel():
            if progress:
                progress(i, total, f, "stop")
            break
        try:
            segs = segments_from_index(f)
            if not segs:
                skipped += 1
                if progress:
                    progress(i, total, f, "skip")
                continue
            process_video(f, segs, style, out_dir, cancel)
            ok += 1
            if progress:
                progress(i, total, f, "ok")
        except SubtitleError as e:
            if "取消" in str(e):
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
