"""工具箱-媒体工具：FFmpeg 参数化封装。

设计约束（见 docs/工具箱设计方案.md 第 8 节）：
- 所有 ffmpeg 调用统一走本模块，禁止散落裸调
- 全部使用 list 参数（不经 shell），路径作为独立参数 → 天然防命令注入
- filter 表达式内路径必须转义（escape_filter_path）
- 失败抛出 MediaError，附带 stderr 供诊断
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v", ".ts", ".mpeg", ".mpg", ".wmv", ".3gp"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma", ".opus"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
ALL_EXTS = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS

TOOL_NAMES = {"transcode", "compress", "extract_frames", "audio", "clip", "concat"}
KIND_VIDEO = "video"
KIND_AUDIO = "audio"
KIND_ALL = "all"


class MediaError(Exception):
    """媒体处理错误，message 面向用户。"""


def ffmpeg_path() -> str:
    """获取 ffmpeg 可执行文件（复用现有基础设施，不重新下载）。"""
    try:
        from video_engine import _ffmpeg
        return _ffmpeg()
    except Exception:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()


def ffprobe_path() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffprobe_exe()
    except Exception:
        return ""


def escape_filter_path(path: str) -> str:
    """filter 表达式内路径转义（冒号/引号/反斜杠）。"""
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def collect_files(folder: str, kind: str = KIND_VIDEO) -> list[str]:
    """收集文件夹内匹配类型的文件，按文件名排序（稳定顺序）。"""
    exts = VIDEO_EXTS if kind == KIND_VIDEO else AUDIO_EXTS if kind == KIND_AUDIO else ALL_EXTS
    folder = Path(folder)
    if not folder.is_dir():
        raise MediaError(f"文件夹不存在：{folder}")
    files = [str(p) for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts]
    files.sort(key=lambda p: p.lower())
    return files


def _probe_info(path: str) -> str:
    """用 ffmpeg 自身解析媒体信息（imageio_ffmpeg 不保证带 ffprobe）。

    返回 stderr 文本（ffmpeg -i 无输出参数时退出码非 0，但 stderr 含流信息）。
    """
    ff = ffmpeg_path()
    if not ff or not os.path.isfile(ff):
        return ""
    try:
        out = subprocess.run(
            [ff, "-hide_banner", "-i", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return out.stderr or ""
    except Exception:
        return ""


def probe_duration(path: str) -> float:
    """探测视频/音频时长（秒）。失败返回 0。"""
    txt = _probe_info(path)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", txt)
    if not m:
        return 0.0
    try:
        h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        return h * 3600 + mi * 60 + s
    except Exception:
        return 0.0


def _probe_size(path: str) -> tuple[int, int]:
    """探测视频宽高，失败返回 (0, 0)。便携版无 ffprobe 时用 ffmpeg -i 解析。"""
    ff = ffprobe_path()
    if ff:
        try:
            out = subprocess.run(
                [ff, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height",
                 "-of", "csv=s=x:p=0", path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if out.returncode == 0 and out.stdout.strip():
                w, h = out.stdout.strip().split("x", 1)
                return int(w), int(h)
        except Exception:
            pass
    # 兜底：ffmpeg -i stderr 里的分辨率
    txt = _probe_info(path)
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", txt)
    if m:
        try:
            return int(m.group(1)), int(m.group(2))
        except Exception:
            pass
    return 0, 0


def has_audio(path: str) -> bool:
    """探测文件是否含音频流（whisper 前必须先查，无音轨直接跳过，避免内部崩溃）。"""
    txt = _probe_info(path)
    return bool(re.search(r"Audio:\s*\w+", txt))


def _unique_dst(folder: str, stem: str, suffix: str) -> str:
    """生成不冲突的输出路径：name_1.mp4 递增。"""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    base = folder / f"{stem}{suffix}"
    if not base.exists():
        return str(base)
    i = 1
    while True:
        cand = folder / f"{stem}_{i}{suffix}"
        if not cand.exists():
            return str(cand)
        i += 1


def _run(args: list[str], cancel: Optional[Callable[[], bool]] = None,
         timeout: float = 3600.0, cwd: Optional[str] = None) -> str:
    """执行 ffmpeg（list 参数，无 shell）。返回 stderr 文本；失败抛 MediaError。

    注意：stderr 写临时文件而非 PIPE——ffmpeg 进度输出量大，PIPE 不持续读会填满缓冲阻塞进程。
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    fd, err_path = tempfile.mkstemp(suffix=".log", prefix="tbx_ff_")
    os.close(fd)
    try:
        with open(err_path, "wb") as err_fh:
            proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=err_fh,
                creationflags=flags, cwd=cwd,
            )
            start = time.time()
            try:
                while proc.poll() is None:
                    if cancel is not None and cancel():
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        raise MediaError("已取消")
                    if timeout and time.time() - start > timeout:
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        raise MediaError("处理超时（超过 %d 秒），已终止" % int(timeout))
                    time.sleep(0.15)
            except BaseException:
                if proc.poll() is None:
                    proc.kill()
                raise
        with open(err_path, "r", encoding="utf-8", errors="replace") as fh:
            stderr_text = fh.read()
        if proc.returncode != 0:
            tail = "\n".join(stderr_text.strip().splitlines()[-6:])
            raise MediaError(f"FFmpeg 执行失败（返回码 {proc.returncode}）：\n{tail}")
        return stderr_text
    finally:
        try:
            os.unlink(err_path)
        except OSError:
            pass


def _resolve_resolution(res: str, src_path: str) -> str:
    """把分辨率参数解析为 scale 滤镜值；'原始' 返回空串（不缩放）。"""
    res = (res or "").strip()
    if not res or res in ("原始", "跟随原始", "source"):
        return ""
    m = re.match(r"^(\d{2,5})\s*[xX*]\s*(\d{2,5})$", res)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        return f"{w}:{h}"
    presets = {
        "1080x1920": "1080:1920", "1920x1080": "1920:1080",
        "720x1280": "720:1280", "1280x720": "1280:720",
        "540x960": "540:960", "960x540": "960:540",
    }
    if res in presets:
        return presets[res]
    raise MediaError(f"无法识别的分辨率：{res}（示例：1080x1920）")


def run_ffmpeg(args: list[str], cancel: Optional[Callable[[], bool]] = None,
               timeout: float = 3600.0, cwd: Optional[str] = None) -> str:
    """通用 ffmpeg 执行入口（list 参数、无 shell、防注入）。

    供工具箱其他模块（字幕烧录/剪气口等）复用；禁止在 engine.py 之外裸调 subprocess。
    """
    ff = ffmpeg_path()
    if not os.path.isfile(ff):
        raise MediaError("未找到 FFmpeg，无法处理")
    return _run([ff] + args, cancel, timeout, cwd=cwd)


def run_tool(tool: str, src: str, dst: str, params: dict,
             cancel: Optional[Callable[[], bool]] = None) -> None:
    """执行单个媒体工具。

    :param tool: transcode / compress / extract_frames / audio / clip / concat
    :param src: 源文件路径（concat 时为已写好的列表文件）
    :param dst: 输出文件路径
    :param params: 工具参数（见各函数）
    """
    if tool not in TOOL_NAMES:
        raise MediaError(f"未知工具：{tool}")
    src_path = Path(src)
    if not src_path.is_file():
        raise MediaError(f"源文件不存在：{src}")
    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    ff = ffmpeg_path()
    if not os.path.isfile(ff):
        raise MediaError("未找到 FFmpeg，无法处理")

    if tool == "transcode":
        _do_transcode(ff, str(src_path), str(dst_path), params, cancel)
    elif tool == "compress":
        _do_compress(ff, str(src_path), str(dst_path), params, cancel)
    elif tool == "extract_frames":
        _do_extract_frames(ff, str(src_path), str(dst_path), params, cancel)
    elif tool == "audio":
        _do_audio(ff, str(src_path), str(dst_path), params, cancel)
    elif tool == "clip":
        _do_clip(ff, str(src_path), str(dst_path), params, cancel)
    elif tool == "concat":
        _do_concat(ff, str(src_path), str(dst_path), params, cancel)


def _vcodec_args(params: dict) -> list[str]:
    enc = (params.get("encoder") or "h264").lower()
    if enc in ("h265", "hevc"):
        vcodec, vopt = "libx265", ["-crf", str(_clamp_int(params.get("crf"), 18, 28, 23)), "-preset", "medium"]
    else:
        vcodec, vopt = "libx264", ["-crf", str(_clamp_int(params.get("crf"), 18, 28, 23)), "-preset", "medium"]
    if params.get("bitrate"):
        vopt = ["-b:v", f"{_clamp_int(params['bitrate'], 100, 100000, 4000)}k"]
    return ["-c:v", vcodec] + vopt


def _do_transcode(ff, src, dst, params, cancel):
    out_fmt = (params.get("format") or "mp4").lstrip(".")
    vf = []
    res = _resolve_resolution(params.get("resolution") or "", src)
    if res:
        vf.append(f"scale={res}:force_original_aspect_ratio=decrease,pad={res}:(ow-iw)/2:(oh-ih)/2")
    args = [ff, "-y", "-i", src]
    if vf:
        args += ["-vf", ",".join(vf)]
    args += _vcodec_args(params)
    fps = params.get("fps")
    if fps:
        args += ["-r", str(fps)]
    args += ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    _run(args + [dst], cancel)


def _do_compress(ff, src, dst, params, cancel):
    crf = _clamp_int(params.get("crf"), 18, 28, 23)
    max_side = int(params.get("max_side") or 0)
    args = [ff, "-y", "-i", src]
    if max_side > 0:
        args += ["-vf", f"scale='min({max_side},iw)':'min({max_side},ih)':force_original_aspect_ratio=decrease"]
    args += ["-c:v", "libx264", "-crf", str(crf), "-preset", "slow",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    _run(args + [dst], cancel)


def _do_extract_frames(ff, src, dst, params, cancel):
    """抽帧：dst 为输出目录，参数 mode: 'per_second'|'per_n_seconds', value: 数字, img: jpg/png"""
    mode = params.get("mode") or "per_second"
    value = float(params.get("value") or 1)
    if value <= 0:
        raise MediaError("抽帧间隔必须大于 0")
    img = (params.get("img") or "jpg").lstrip(".")
    if mode == "per_n_seconds":
        fps_expr = f"1/{value}"
    else:
        fps_expr = str(value)
    os.makedirs(dst, exist_ok=True)
    pattern = os.path.join(dst, "%04d." + img)
    _run([ff, "-y", "-i", src, "-vf", f"fps={fps_expr}", pattern], cancel)


def _do_audio(ff, src, dst, params, cancel):
    action = params.get("action") or "extract"
    out_fmt = (params.get("format") or "mp3").lstrip(".")
    if action == "extract":
        codec = {"mp3": "libmp3lame", "wav": "pcm_s16le", "m4a": "aac", "flac": "flac", "ogg": "libvorbis"}.get(out_fmt, "libmp3lame")
        args = [ff, "-y", "-i", src, "-vn"]
        if codec == "libmp3lame":
            args += ["-b:a", "192k"]
        elif codec == "aac":
            args += ["-b:a", "192k"]
        args += ["-c:a", codec]
        _run(args + [dst], cancel)
    else:
        raise MediaError(f"未知音频动作：{action}")


def _do_clip(ff, src, dst, params, cancel):
    start = float(params.get("start") or 0)
    end = params.get("end")
    dur = params.get("duration")
    if start < 0:
        raise MediaError("起始时间不能为负")
    args = [ff, "-y", "-ss", f"{start:.2f}"]
    if end:
        if float(end) <= start:
            raise MediaError("结束时间必须大于起始时间")
        args += ["-to", f"{float(end):.2f}"]
    elif dur:
        if float(dur) <= 0:
            raise MediaError("时长必须大于 0")
        args += ["-t", f"{float(dur):.2f}"]
    args += ["-i", src, "-c:v", "libx264", "-preset", "medium", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    _run(args + [dst], cancel)


def _do_concat(ff, list_file, dst, params, cancel):
    """list_file 为 ffconcat 格式列表（已由调用方写入）。重编码保证任意源可合并。"""
    vf = []
    res = _resolve_resolution(params.get("resolution") or "", list_file)
    if res:
        vf.append(f"scale={res}:force_original_aspect_ratio=decrease,pad={res}:(ow-iw)/2:(oh-ih)/2")
    args = [ff, "-y", "-f", "concat", "-safe", "0", "-i", list_file]
    if vf:
        args += ["-vf", ",".join(vf)]
    args += ["-c:v", "libx264", "-preset", "medium", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    _run(args + [dst], cancel)


def _concat_escape(path: str) -> str:
    """ffconcat 引号内路径转义：统一正斜杠 + 转义单引号（\\'）。

    注意：冒号在 ffconcat 引号路径内**不需要**转义（\\: 是 filter 语法，用于此处会报 Invalid argument）。
    """
    p = path.replace("\\", "/")
    return p.replace("'", "\\'")


def build_concat_list(files: list[str], list_file: str) -> None:
    """写入 ffconcat 列表文件。

    ffconcat 规范：路径含空格/特殊字符时必须用单引号包裹（引号内转义 \\'）。
    """
    lines = ["ffconcat version 1.0"]
    for f in files:
        lines.append("file '" + _concat_escape(f) + "'")
    Path(list_file).write_text("\n".join(lines), encoding="utf-8")


def _clamp_int(v, lo, hi, default):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def suffix_for(tool: str, params: dict, src: str) -> str:
    """根据工具与参数推导输出后缀（扩展名）。"""
    if tool == "transcode":
        return "." + (params.get("format") or "mp4").lstrip(".")
    if tool == "compress":
        return "." + Path(src).suffix.lstrip(".") if Path(src).suffix else ".mp4"
    if tool in ("clip", "concat"):
        return ".mp4"
    if tool == "extract_frames":
        return ""  # 输出为目录内 %04d.jpg
    if tool == "audio":
        return "." + (params.get("format") or "mp3").lstrip(".")
    return ".mp4"
