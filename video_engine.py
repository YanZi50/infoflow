import base64
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from imageio_ffmpeg import get_ffmpeg_exe

import subtitle_plugin

from video_core import (
    VIDEO_EXTS, AUDIO_EXTS, IMAGE_EXTS,
    scan_videos, scan_audio, media_fingerprint, dedupe_by_fp,
    dedupe_delta, build_combinations, DEFAULT_TRANSITIONS,
    pick_transition, build_middle_pools, pick_middle_sequence,
    middle_display, pick_bgm, render_output_name, _unique_output_name,
    resolve_duration_mode, resolve_resolution,
)


class MediaError(Exception):
    pass


class CancelledError(Exception):
    pass


def _ffmpeg() -> str:
    candidates = []
    if getattr(sys, "frozen", False):
        # 便携版：优先 exe 旁 bin\ffmpeg.exe（用户数据区，规避部分环境对
        # PyInstaller _internal 目录内可执行文件的访问限制）
        candidates.append(Path(sys.executable).resolve().parent / "bin" / "ffmpeg.exe")
        candidates.append(Path(sys._MEIPASS) / "bin" / "ffmpeg.exe")
    candidates.append(Path(__file__).resolve().parent / "bin" / "ffmpeg.exe")
    try:
        candidates.append(Path(get_ffmpeg_exe()))
    except Exception:
        pass
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise MediaError("找不到 FFmpeg。")


def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def cache_dir() -> Path:
    path = _app_root() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file_fingerprint(path: str, extra: str = "") -> str:
    st = os.stat(path)
    payload = f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}|{extra}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _norm_cache_path(
    src: str, width: int, height: int, has_audio: bool, normalize_audio: bool, fit_mode: str = "fit",
    volume: float = 1.0,
) -> Path:
    # normv2：音频处理加入 aresample=async=1:first_pts=0 强制音画对齐，旧缓存（normv1）作废
    # fit_mode：画面适配模式（fit/blur/crop），"fit"（黑边）省略后缀以复用历史缓存
    mode = "" if fit_mode == "fit" else f"|{fit_mode}"
    vol = "" if abs(volume - 1.0) < 1e-6 else f"|v{volume:g}"
    key = _file_fingerprint(src, f"normv2|{width}x{height}|{has_audio}|{normalize_audio}{mode}{vol}")
    folder = cache_dir() / "norm"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{key}.mp4"


def _thumb_cache_path(src: str, max_width: int) -> Path:
    key = _file_fingerprint(src, f"thumb|{max_width}")
    folder = cache_dir() / "thumbs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{key}.jpg"


def parse_duration(value: str) -> float:
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", value)
    if not match:
        return 0.0
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


# 素材探测缓存：path -> (文件mtime, 探测时间戳, 结果)。文件变动或超时后自动失效。
_probe_cache: dict[str, tuple[float, float, dict]] = {}
_PROBE_TTL = 300.0  # 5 分钟内同路径不重复跑 ffmpeg 探测
_probe_lock = threading.Lock()


def probe_media(path: str, require_video: bool = True) -> dict:
    """探测媒体信息。require_video=False 时纯音频文件（如 BGM）也算正常。
    带进程内缓存：同路径 5 分钟内且文件未变动时直接返回缓存，避免几百条素材重复探测。"""
    try:
        st = os.stat(path)
        mtime = st.st_mtime
        now = time.time()
    except OSError:
        return {"ok": False, "error": "文件不存在"}
    with _probe_lock:
        # 缓存 key 必须区分 require_video：否则"先按音频标准探测 ok=True、再按视频标准探测"会命中错误缓存，
        # 导致音频文件缩略图/健康判断错乱（显示损坏或无法抽帧）
        hit = _probe_cache.get((path, require_video))
        if hit and hit[0] == mtime and now - hit[1] < _PROBE_TTL:
            return hit[2]
    cmd = [
        _ffmpeg(),
        "-hide_banner",
        "-i",
        path,
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    text = proc.stderr or ""
    duration = parse_duration(text)
    has_audio = bool(re.search(r"Stream #\d+:\d+[^\n]*Audio:", text))
    has_video = bool(re.search(r"Stream #\d+:\d+[^\n]*Video:", text))
    width = height = 0
    vm = re.search(r"Video:\s*\S+.*?(\d{2,5})x(\d{2,5})", text)
    if vm:
        width, height = int(vm.group(1)), int(vm.group(2))
    audio_start = 0.0
    audio_duration = 0.0
    am = re.search(
        r"Stream #\d+:\d+[^\n]*Audio:(?:(?!Stream #)[\s\S])*?Start:\s*([\d.]+)[^\n]*Duration:\s*([\d.]+)",
        text,
    )
    if am:
        audio_start = float(am.group(1))
        audio_duration = float(am.group(2))
    result = {
        "duration": duration or 1.0,
        "has_audio": has_audio,
        "has_video": has_video,
        "width": width,
        "height": height,
        "audio_start": audio_start,
        "audio_duration": audio_duration,
        "ok": bool(duration > 0.05 and (has_video if require_video else (has_video or has_audio))),
    }
    with _probe_lock:
        _probe_cache[(path, require_video)] = (mtime, now, result)
    return result


def escape_filter_path(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def run_ffmpeg(
    args: list[str],
    cancel_event,
    pause_event,
    log: Optional[Callable[[str], None]] = None,
    cwd=None,
) -> None:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if log:
        log(" ".join(str(a) for a in args))
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        cwd=cwd,
    )
    try:
        while proc.poll() is None:
            if cancel_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise CancelledError("任务已取消")
            while pause_event.is_set() and not cancel_event.is_set():
                time.sleep(0.2)
            time.sleep(0.1)
    except BaseException:
        if proc.poll() is None:
            proc.kill()
        raise
    if proc.returncode != 0:
        raise MediaError(f"FFmpeg 执行失败，返回码 {proc.returncode}")


def _filter_scale_pad(width: int, height: int, fit_mode: str = "fit") -> str:
    """画面适配滤镜链。

    fit（默认）：等比缩放 + 黑边补齐（letterbox）
    blur：背景铺满 + 高斯模糊填充，前景等比居中（信息流标准观感）
    crop：等比放大铺满 + 居中裁剪（无黑边，可能裁掉边缘内容）
    """
    if fit_mode == "blur":
        return (
            f"[0:v]split=2[bgv][fgv];"
            f"[bgv]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=20:2[bg];"
            f"[fgv]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,settb=AVTB,setsar=1,fps=30,format=yuv420p[out]"
        )
    if fit_mode == "crop":
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps=30,format=yuv420p"
        )
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps=30,format=yuv420p"
    )


def normalize_clip(
    src: str,
    dst: str,
    width: int,
    height: int,
    duration: float,
    has_audio: bool,
    cancel_event,
    pause_event,
    log: Optional[Callable[[str], None]] = None,
    normalize_audio: bool = False,
    fit_mode: str = "fit",
    encode_accel: str = "auto",
    volume: float = 1.0,
) -> None:
    cached = _norm_cache_path(src, width, height, has_audio, normalize_audio, fit_mode, volume)
    if cached.exists() and cached.stat().st_size > 0:
        shutil.copy2(cached, dst)
        if log:
            log(f"命中归一化缓存：{Path(src).name}")
        return

    tmp_cache = cached.with_name(
        f"{cached.stem}.{os.getpid()}.{threading.get_ident()}.mp4"
    )
    is_blur = fit_mode == "blur"
    vf = _filter_scale_pad(width, height, fit_mode)
    args = [_ffmpeg(), "-y", "-i", src]
    if not has_audio:
        args += [
            "-f",
            "lavfi",
            "-t",
            f"{duration:.3f}",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
        ]
    if is_blur:
        # 模糊填充使用带标签的复杂滤镜图，必须走 -filter_complex
        args += ["-filter_complex", vf, "-map", "[out]"]
    else:
        args += ["-vf", vf, "-map", "0:v:0"]
    if has_audio:
        args += ["-map", "0:a:0"]
        # 音画对齐：
        # 1) asetpts=PTS-STARTPTS：把音频流起点归零（原素材音频 PTS 偏移/录制缺口
        #    导致声音比画面晚开始，内容整体平移对齐，这是音画不同步的主因）；
        # 2) aresample=async=1:first_pts=0：修正采样节奏漂移/微小抖动。
        af = "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0"
        if normalize_audio:
            af = f"loudnorm=I=-16:TP=-1.5:LRA=11,{af}"
        if abs(volume - 1.0) >= 1e-6:
            # 素材音量系数：放在归一化之后叠加，用户可在归一化基础上微调
            af = f"volume={volume:g},{af}"
        args += ["-af", af]
    else:
        args += ["-map", "1:a:0"]
    args += [
        *_vcodec_args(encode_accel, 18),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-video_track_timescale",
        "15360",
        "-shortest",
        tmp_cache,
    ]
    run_ffmpeg(args, cancel_event, pause_event, log)
    os.replace(tmp_cache, cached)
    shutil.copy2(cached, dst)


def _prefilter_strings(delta: dict, i: int, width: int = 0, height: int = 0) -> tuple[str, str]:
    """为第 i 个输入生成差异化前置滤镜（视频/音频）。
    返回 (vf, af)；无扰动时返回 ('', '')。
    注：入点偏移不走滤镜（trim+xfade 在 ffmpeg 7.1 报 -22），改由输入级 -ss 实现。"""
    if not delta:
        return "", ""
    vf_parts: list[str] = []
    af_parts: list[str] = []
    if delta.get("speed"):
        vf_parts.append(f"setpts=PTS/{delta['speed']:.4f},fps=30")
        af_parts.append(f"atempo={delta['speed']:.4f}")
    if delta.get("mirror"):
        vf_parts.append("hflip")
    if delta.get("noise"):
        vf_parts.append(f"noise=alls={delta['noise']}:allf=t+u")
    if delta.get("pitch"):
        pf = float(delta["pitch"])
        af_parts.append(f"asetrate={int(48000 * pf)},aresample=48000,atempo={1.0 / pf:.4f}")
    if delta.get("visual"):
        vf_parts.append(
            f"eq=brightness={delta['brightness']:.4f}:contrast={delta['contrast']:.4f}:saturation={delta['saturation']:.4f}"
        )
        zoom = float(delta.get("zoom") or 1.0)
        if zoom > 1.0:
            # crop 宽高必须为偶数（yuv420p），否则 ffmpeg 报 -22
            x_even = int(delta.get("crop_x", 0)) & ~1
            y_even = int(delta.get("crop_y", 0)) & ~1
            if width > 0 and height > 0:
                # 已知目标尺寸：放大后精确裁回目标分辨率（1080x1920 等），无浮点误差
                vf_parts.append(
                    f"scale=iw*{zoom:.4f}:ih*{zoom:.4f},"
                    f"crop={width}:{height}:{x_even}:{y_even},setsar=1"
                )
            else:
                # 兜底：按比例裁回（可能有 ±1px 舍入）
                vf_parts.append(
                    f"crop=trunc(iw/{zoom:.4f}/2)*2:trunc(ih/{zoom:.4f}/2)*2:{x_even}:{y_even},"
                    f"scale=trunc(iw*{zoom:.4f}/2)*2:trunc(ih*{zoom:.4f}/2)*2,setsar=1"
                )
    return ",".join(vf_parts), ",".join(af_parts)


def concat_two(
    head: str,
    tail: str,
    dst: str,
    head_duration: float,
    tail_duration: float,
    cancel_event,
    pause_event,
    transition_type: Optional[str] = None,
    transition_duration: float = 0.5,
    log: Optional[Callable[[str], None]] = None,
    delta: Optional[dict] = None,
    ss: Optional[list[float]] = None,
    encode_accel: str = "auto",
    width: int = 0,
    height: int = 0,
) -> None:
    offsets = ss or [0.0, 0.0]
    args = [_ffmpeg(), "-y"]
    for off, path in zip(offsets, [head, tail]):
        if off > 0:
            args += ["-ss", f"{off:.3f}"]
        args += ["-i", path]
    pre = []
    v0, a0 = _prefilter_strings(delta or {}, 0, width, height)
    v1, a1 = _prefilter_strings(delta or {}, 1, width, height)
    src_v = ["0:v", "1:v"]
    src_a = ["0:a", "1:a"]
    if v0:
        pre.append(f"[0:v]{v0}[v0p]")
        src_v[0] = "v0p"
    if a0:
        pre.append(f"[0:a]{a0}[a0p]")
        src_a[0] = "a0p"
    if v1:
        pre.append(f"[1:v]{v1}[v1p]")
        src_v[1] = "v1p"
    if a1:
        pre.append(f"[1:a]{a1}[a1p]")
        src_a[1] = "a1p"
    can_transition = (
        transition_type
        and head_duration >= transition_duration + 0.2
        and tail_duration >= transition_duration + 0.2
    )
    if can_transition:
        duration = min(transition_duration, head_duration - 0.2, tail_duration - 0.2)
        offset = head_duration - duration
        # xfade 在 ffmpeg 7.1 会把输出自动协商为 yuv444p，末尾强制回 yuv420p
        # （体积小、兼容性好），否则成片体积明显变大。
        fc = (
            f"[{src_v[0]}][{src_v[1]}]xfade=transition={transition_type}:duration={duration:.3f}:offset={offset:.3f}[vraw];"
            f"[{src_a[0]}][{src_a[1]}]acrossfade=d={duration:.3f}:c1=tri:c2=tri[a];"
            f"[vraw]format=yuv420p[v]"
        )
    else:
        fc = f"[{src_v[0]}][{src_a[0]}][{src_v[1]}][{src_a[1]}]concat=n=2:v=1:a=1[v][a]"
    if pre:
        fc = ";".join(pre) + ";" + fc
    args += [
        "-filter_complex",
        fc,
        "-map",
        "[v]",
        "-map",
        "[a]",
        *_vcodec_args(encode_accel, 20),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        dst,
    ]
    run_ffmpeg(args, cancel_event, pause_event, log)


def concat_three(
    first: str,
    second: str,
    third: str,
    dst: str,
    durations: list[float],
    cancel_event,
    pause_event,
    transition_type: Optional[str] = None,
    transition_duration: float = 0.5,
    log: Optional[Callable[[str], None]] = None,
    encode_accel: str = "auto",
) -> None:
    can_transition = (
        transition_type
        and len(durations) == 3
        and all(d >= transition_duration + 0.2 for d in durations)
    )
    if can_transition:
        d = min(transition_duration, *(x - 0.2 for x in durations))
        o1 = durations[0] - d
        o2 = durations[0] + durations[1] - 2 * d
        fc = (
            f"[0:v][1:v]xfade=transition={transition_type}:duration={d:.3f}:offset={o1:.3f}[v1];"
            f"[v1][2:v]xfade=transition={transition_type}:duration={d:.3f}:offset={o2:.3f}[vraw];"
            f"[0:a][1:a]acrossfade=d={d:.3f}:c1=tri:c2=tri[a1];"
            f"[a1][2:a]acrossfade=d={d:.3f}:c1=tri:c2=tri[a];"
            f"[vraw]format=yuv420p[v]"
        )
    else:
        fc = "[0:v][0:a][1:v][1:a][2:v][2:a]concat=n=3:v=1:a=1[v][a]"
    args = [
        _ffmpeg(),
        "-y",
        "-i",
        first,
        "-i",
        second,
        "-i",
        third,
        "-filter_complex",
        fc,
        "-map",
        "[v]",
        "-map",
        "[a]",
        *_vcodec_args(encode_accel, 20),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        dst,
    ]
    run_ffmpeg(args, cancel_event, pause_event, log)


def concat_chain(
    clips: list[str],
    dst: str,
    durations: list[float],
    cancel_event,
    pause_event,
    transition_type: Optional[str] = None,
    transition_duration: float = 0.5,
    log: Optional[Callable[[str], None]] = None,
    delta: Optional[dict] = None,
    ss: Optional[list[float]] = None,
    encode_accel: str = "auto",
    width: int = 0,
    height: int = 0,
) -> None:
    """通用 N 片段拼接：转场可用时链式 xfade，否则 concat 滤镜硬接。
    delta：差异化参数（画面微调），并入拼接链不增加转码次数。
    ss：每个输入的入点偏移（输入级 -ss，兼容 xfade）。"""
    n = len(clips)
    if n < 2:
        raise MediaError("拼接至少需要两个片段。")
    if n == 2:
        concat_two(clips[0], clips[1], dst, durations[0], durations[1],
                   cancel_event, pause_event, transition_type, transition_duration, log, delta, ss,
                   encode_accel=encode_accel, width=width, height=height)
        return
    inputs: list[str] = []
    for i, clip in enumerate(clips):
        if ss and i < len(ss) and ss[i] > 0:
            inputs += ["-ss", f"{ss[i]:.3f}"]
        inputs += ["-i", clip]
    # 差异化前置滤镜：每个输入流独立处理，输出替换原引用标签
    pre: list[str] = []
    src_v = [f"{i}:v" for i in range(n)]
    src_a = [f"{i}:a" for i in range(n)]
    for i in range(n):
        vf, af = _prefilter_strings(delta or {}, i, width, height)
        if vf:
            pre.append(f"[{i}:v]{vf}[v{i}p]")
            src_v[i] = f"v{i}p"
        if af:
            pre.append(f"[{i}:a]{af}[a{i}p]")
            src_a[i] = f"a{i}p"
    can_transition = (
        transition_type
        and len(durations) == n
        and all(d >= transition_duration + 0.2 for d in durations)
    )
    if can_transition:
        d = min(transition_duration, *(x - 0.2 for x in durations))
        # 链式 xfade：第 i 个 xfade 的输入是前 i 段拼接输出，其时长已减去 i 个转场重叠，
        # offset 必须用递推的"当前拼接输出时长 - d"，否则 offset 超出输入时长会被 ffmpeg 截断成片。
        v_parts: list[str] = []
        acc = durations[0]
        for i in range(n - 1):
            off = acc - d
            out_label = "[v]" if i == n - 2 else f"[v{i + 1}]"
            if i == 0:
                v_parts.append(f"[{src_v[0]}][{src_v[1]}]xfade=transition={transition_type}:duration={d:.3f}:offset={off:.3f}{out_label}")
            else:
                v_parts.append(f"[v{i}][{src_v[i + 1]}]xfade=transition={transition_type}:duration={d:.3f}:offset={off:.3f}{out_label}")
            acc = off + durations[i + 1]
        a_parts = [f"[{src_a[0]}][{src_a[1]}]acrossfade=d={d:.3f}:c1=tri:c2=tri[a1]"]
        for i in range(1, n - 1):
            out_label = "[a]" if i == n - 2 else f"[a{i + 1}]"
            a_parts.append(f"[a{i}][{src_a[i + 1]}]acrossfade=d={d:.3f}:c1=tri:c2=tri{out_label}")
        # xfade 在 ffmpeg 7.1 会把输出自动协商为 yuv444p，末尾强制回 yuv420p
        fc = ";".join(v_parts + a_parts) + ";[v]format=yuv420p[v]"
    else:
        streams = []
        for i in range(n):
            streams += [f"[{src_v[i]}]", f"[{src_a[i]}]"]
        fc = "".join(streams) + f"concat=n={n}:v=1:a=1[v][a]"
    if pre:
        fc = ";".join(pre) + ";" + fc
    args = (
        [_ffmpeg(), "-y"]
        + inputs
        + [
            "-filter_complex",
            fc,
            "-map",
            "[v]",
            "-map",
            "[a]",
            *_vcodec_args(encode_accel, 20),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            dst,
        ]
    )
    run_ffmpeg(args, cancel_event, pause_event, log)


def concat_copy(clips: list[str], dst: str, cancel_event, pause_event, log: Optional[Callable[[str], None]] = None) -> None:
    """无转场且素材已归一化时，用 concat demuxer 流复制拼接，避免重编码。"""
    tempdir = Path(tempfile.mkdtemp(prefix="sppj_concat_"))
    try:
        list_file = tempdir / "list.txt"
        lines = [f"file '{p.replace(chr(39), chr(39) + chr(92) + chr(39))}'" for p in clips]
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        args = [
            _ffmpeg(),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            dst,
        ]
        run_ffmpeg(args, cancel_event, pause_event, log)
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)


def trim_duration(
    src: str,
    dst: str,
    limit: Optional[float],
    cancel_event,
    pause_event,
    log: Optional[Callable[[str], None]] = None,
) -> None:
    if not limit:
        shutil.copyfile(src, dst)
        return
    args = [_ffmpeg(), "-y", "-i", src, "-t", f"{limit:.3f}", "-c", "copy", dst]
    run_ffmpeg(args, cancel_event, pause_event, log)


def mix_bgm(
    src: str,
    bgm: str,
    dst: str,
    volume: float,
    cancel_event,
    pause_event,
    log: Optional[Callable[[str], None]] = None,
    fade: bool = False,
    ducking: bool = False,
    bgm_duration: float = 0.0,
    audio_volume: float = 1.0,
    bgm_shift: float = 0.0,
    encode_accel: str = "auto",
) -> None:
    shift = max(0.0, float(bgm_shift or 0.0))
    if shift > 0:
        bgm_chain = f"[1:a]atrim=start={shift:.3f},asetpts=PTS-STARTPTS,volume={volume:.2f}"
    else:
        bgm_chain = f"[1:a]volume={volume:.2f}"
    if fade:
        fade_duration = min(1.2, max(0.1, bgm_duration * 0.2)) if bgm_duration > 0 else 1.0
        fade_start = max(0.0, bgm_duration - fade_duration) if bgm_duration > 0 else 0.0
        bgm_chain += f",afade=t=in:d={fade_duration:.3f},afade=t=out:st={fade_start:.3f}:d={fade_duration:.3f}"
    bgm_chain += "[bgm]"
    if ducking:
        fc = (
            f"{bgm_chain};"
            f"[0:a]volume={audio_volume:.2f},asplit=2[voice][mixvoice];"
            "[bgm][voice]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=200[duckbgm];"
            "[mixvoice][duckbgm]amix=inputs=2:duration=first:dropout_transition=3[a]"
        )
    else:
        fc = f"{bgm_chain};[0:a]volume={audio_volume:.2f}[voice];[voice][bgm]amix=inputs=2:duration=first:dropout_transition=3[a]"
    args = [
        _ffmpeg(),
        "-y",
        "-i",
        src,
        "-stream_loop",
        "-1",
        "-i",
        bgm,
        "-filter_complex",
        fc,
        "-map",
        "0:v",
        "-map",
        "[a]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        dst,
    ]
    run_ffmpeg(args, cancel_event, pause_event, log)


def apply_audio_volume(
    src: str,
    dst: str,
    volume: float,
    cancel_event,
    pause_event,
    log: Optional[Callable[[str], None]] = None,
) -> None:
    """无 BGM 时调整原声音量：视频流流复制，仅重编码音频，速度快。"""
    args = [_ffmpeg(), "-y", "-i", src, "-c:v", "copy", "-af", f"volume={volume:.2f}", "-c:a", "aac", "-b:a", "192k", dst]
    run_ffmpeg(args, cancel_event, pause_event, log)


def replace_audio_with_voiceover(
    src: str,
    voice_wav: str,
    dst: str,
    cancel_event,
    pause_event,
    log: Optional[Callable[[str], None]] = None,
) -> None:
    """口播配音替代原声：保留成片画面，音轨换成配音（对齐成片时长）。

    - 配音短于成片：尾部补静音；配音长于成片：截断到成片时长。
    - 视频流流复制，仅重编码音频，速度快。
    """
    dur = probe_media(src)["duration"] or 0.0
    dur = max(dur, 0.1)
    fc = (
        f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,apad,"
        f"atrim=0:{dur:.3f}[a]"
    )
    args = [
        _ffmpeg(), "-y",
        "-i", src, "-i", voice_wav,
        "-filter_complex", fc,
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        dst,
    ]
    run_ffmpeg(args, cancel_event, pause_event, log)


def generate_bgm_wav(dst: str, duration: float = 32.0, sample_rate: int = 44100) -> None:
    duration = max(duration, 8.0)
    t = np.arange(int(sample_rate * duration), dtype=np.float64) / sample_rate
    chords = [
        [261.63, 329.63, 392.00, 493.88],
        [220.00, 261.63, 329.63, 440.00],
        [174.61, 220.00, 261.63, 349.23],
        [196.00, 246.94, 293.66, 392.00],
    ]
    chord_seconds = 2.0
    audio = np.zeros(t.shape[0], dtype=np.float64)
    for idx, chord in enumerate(chords):
        start = idx * chord_seconds
        end = min(start + chord_seconds, duration)
        mask = (t >= start) & (t < end)
        if not np.any(mask):
            continue
        local_t = t[mask] - start
        seg = np.zeros_like(local_t)
        for i, freq in enumerate(chord):
            detune = 1.0 + (i % 2) * 0.0015
            seg += np.sin(2 * math.pi * freq * detune * local_t)
            seg += 0.35 * np.sin(2 * math.pi * freq * 2 * local_t)
            seg += 0.12 * np.sin(2 * math.pi * freq * 3 * local_t)
        attack = np.minimum(local_t / 0.35, 1.0)
        release = np.minimum((chord_seconds - local_t) / 0.5, 1.0)
        envelope = np.clip(attack, 0, 1) * np.clip(release, 0, 1)
        audio[mask] = seg * envelope
    audio = audio / (np.max(np.abs(audio)) + 1e-9) * 0.28
    stereo = np.column_stack((audio, audio * 0.97))
    pcm = (np.clip(stereo, -1, 1) * 32767).astype("<i2")
    with wave.open(dst, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())


def burn_subtitles(
    src: str,
    srt: str,
    dst: str,
    cancel_event,
    pause_event,
    log: Optional[Callable[[str], None]] = None,
    encode_accel: str = "auto",
) -> None:
    escaped = escape_filter_path(srt)
    style = (
        "FontName=Microsoft YaHei,FontSize=16,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,"
        "BorderStyle=1,Outline=1,Shadow=0,Alignment=2,MarginV=28"
    )
    vf = f"subtitles=filename='{escaped}':force_style='{style}'"
    args = [
        _ffmpeg(),
        "-y",
        "-i",
        src,
        "-vf",
        vf,
        *_vcodec_args(encode_accel, 20),
        "-c:a",
        "copy",
        dst,
    ]
    run_ffmpeg(args, cancel_event, pause_event, log)


def apply_watermark(
    src: str,
    watermark: str,
    dst: str,
    width: int,
    height: int,
    cancel_event,
    pause_event,
    log: Optional[Callable[[str], None]] = None,
    mode: str = "铺满全屏",
    position: str = "右下角",
    scale: float = 0.15,
    opacity: float = 0.6,
    encode_accel: str = "auto",
) -> None:
    if mode == "角落水印":
        target_w = max(40, int(width * max(0.05, min(0.6, scale))))
        opacity = max(0.05, min(1.0, opacity))
        margin = max(12, int(width * 0.02))
        pos_map = {
            "右下角": f"main_w-overlay_w-{margin}:main_h-overlay_h-{margin}",
            "右上角": f"main_w-overlay_w-{margin}:{margin}",
            "左下角": f"{margin}:main_h-overlay_h-{margin}",
            "左上角": f"{margin}:{margin}",
        }
        pos = pos_map.get(position, pos_map["右下角"])
        fc = (
            f"[1:v]scale={target_w}:-2,format=rgba,colorchannelmixer=aa={opacity:.2f}[wm];"
            f"[0:v][wm]overlay={pos}:format=auto:shortest=1[v]"
        )
    else:
        fc = (
            f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}[wm];"
            "[0:v][wm]overlay=0:0:format=auto:shortest=1[v]"
        )
    args = [
        _ffmpeg(),
        "-y",
        "-i",
        src,
        "-loop",
        "1",
        "-i",
        watermark,
        "-filter_complex",
        fc,
        "-map",
        "[v]",
        "-map",
        "0:a?",
        *_vcodec_args(encode_accel, 20),
        "-c:a",
        "copy",
        dst,
    ]
    run_ffmpeg(args, cancel_event, pause_event, log)


def _gen_noise_png(seed: int) -> str:
    """生成一张 120x120 浅灰噪点 PNG（隐式水印-跳动模式用）。
    灰度范围 235-255（接近白），叠在画面上只是轻微提亮，肉眼几乎不可见。"""
    import random as _r
    import tempfile as _tf
    from PIL import Image
    rng = _r.Random(seed)
    img = Image.new("RGBA", (120, 120))
    px = img.load()
    for x in range(120):
        for y in range(120):
            g = rng.randint(235, 255)
            px[x, y] = (g, g, g, 255)
    path = os.path.join(_tf.gettempdir(), f"imwm_{seed & 0x7FFFFFFF}.png")
    img.save(path)
    return path


def apply_im_watermark(src: str, dst: str, width: int, height: int,
                       config, cancel_event, pause_event,
                       log, seed: int) -> None:
    """隐式去重水印：让每帧像素不同，防平台逐帧判重。

    - static：直接用 ffmpeg noise 滤镜加全局微噪点（每帧不同，肉眼几乎不可见）
    - random：小灰度图在画面上跳动（frame=每帧随机 / 2s=每2秒跳）
    透明度 1%-100% 映射到 noise 强度 / overlay alpha。
    """
    opacity = max(0.0001, min(1.0, config.im_wm_opacity))
    if config.im_wm_motion == "random":
        # 跳动模式：小灰度图 overlay，alpha 由透明度控制
        if config.im_wm_source == "custom" and config.im_wm_image and os.path.isfile(config.im_wm_image):
            wm = config.im_wm_image
        else:
            wm = _gen_noise_png(seed)
        if config.im_wm_density == "frame":
            x_expr = "random(1)*(W-w)"
            y_expr = "random(1)*(H-h)"
        else:
            # 平滑漂移：位置沿正弦曲线缓慢移动，每2秒左右换方向
            x_expr = "(W-w)/2 + sin(t*0.7)*(W-w)/2"
            y_expr = "(H-h)/2 + cos(t*0.9)*(H-h)/2"
        fc = (
            f"[1:v]scale=120:-2,format=rgba,colorchannelmixer=aa={opacity:.3f}[wm];"
            f"[0:v][wm]overlay=x='{x_expr}':y='{y_expr}':shortest=1[v]"
        )
        args = [
            _ffmpeg(), "-y", "-i", src,
            "-loop", "1", "-i", wm,
            "-filter_complex", fc,
            "-map", "[v]", "-map", "0:a?",
            *_vcodec_args(config.encode_accel, 20),
            "-c:a", "copy",
            dst,
        ]
    else:
        # 静止模式：直接 noise 滤镜，强度由透明度映射（1%-100% → alls 1-20）
        alls = max(1, int(round(opacity * 20)))
        fc = f"[0:v]noise=alls={alls}:allf=t+u[v]"
        args = [
            _ffmpeg(), "-y", "-i", src,
            "-filter_complex", fc,
            "-map", "[v]", "-map", "0:a?",
            *_vcodec_args(config.encode_accel, 20),
            "-c:a", "copy",
            dst,
        ]
    run_ffmpeg(args, cancel_event, pause_event, log)


# 画面滤镜清单：name -> 生成 ffmpeg 滤镜串的 lambda（s = 强度 0-1）
_FILTER_MAP = {
    "warm":     lambda s: f"colorbalance=rs={0.3*s:.3f}:gs={0.1*s:.3f}:bs={-0.3*s:.3f}",
    "cool":     lambda s: f"colorbalance=rs={-0.3*s:.3f}:gs={-0.1*s:.3f}:bs={0.3*s:.3f}",
    "contrast": lambda s: f"curves=preset=increase_contrast:s={s:.3f}",
    "desat":    lambda s: f"hue=s={max(0, 1-0.5*s):.3f}",
    "vintage":  lambda s: f"colorbalance=rs={0.15*s:.3f}:bs={-0.15*s:.3f},curves=medium_contrast",
    "sharpen":  lambda s: f"unsharp=5:5:{0.8*s:.3f}",
    "soft":     lambda s: f"boxblur={max(0.1, s):.2f}:{max(0.1, s):.2f}",
    "vignette": lambda s: f"vignette=PI/{max(1, 5/s):.2f}",
}
FILTER_NAMES = list(_FILTER_MAP.keys())


def _scan_luts() -> list:
    """扫描 luts/ 文件夹下所有 .cube 文件。"""
    lut_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "luts")
    if not os.path.isdir(lut_dir):
        return []
    return [os.path.join(lut_dir, f) for f in os.listdir(lut_dir) if f.lower().endswith(".cube")]


def apply_video_filter(src: str, dst: str, config, cancel_event, pause_event, log, seed: int) -> None:
    """画面滤镜：内置预设 或 LUT 文件调色，让每条成片观感不同。强度 0.1-100。

    filter_mode:
      - random: 从 luts/ 文件夹随机选一个 .cube
      - builtin: 用 filter_name 指定的内置预设
      - custom:  用 filter_name 指定的 .cube 文件路径
    """
    import random as _r
    s = max(0.1, min(100.0, config.filter_strength)) / 100.0
    fc = None
    cwd = None
    import shutil as _sh
    def _lut_fc(lut_path):
        nonlocal cwd
        # ffmpeg lut3d 在 Windows 对中文路径支持差，复制到临时英文名再用
        import tempfile as _tf
        tmp = os.path.join(_tf.gettempdir(), f"lut_{abs(hash(lut_path)) & 0xFFFFFF}.cube")
        try:
            _sh.copy2(lut_path, tmp)
        except Exception:
            return None
        cwd = os.path.dirname(tmp)
        return f"lut3d=file={os.path.basename(tmp)}:interp=trilinear"
    if config.filter_mode == "random":
        luts = _scan_luts()
        if luts:
            lut_path = _r.Random(seed).choice(luts)
            fc = _lut_fc(lut_path)
    elif config.filter_mode == "custom" and config.filter_lut_path.lower().endswith(".cube") and os.path.isfile(config.filter_lut_path):
        fc = _lut_fc(config.filter_lut_path)
    else:
        # builtin 模式
        name = config.filter_name if config.filter_name in _FILTER_MAP else "warm"
        fc = _FILTER_MAP[name](s)
    if not fc:
        # 没有可用 LUT，直接复制
        args = [_ffmpeg(), "-y", "-i", src, "-c", "copy", dst]
        run_ffmpeg(args, cancel_event, pause_event, log)
        return
    args = [
        _ffmpeg(), "-y", "-i", src,
        "-vf", fc,
        *_vcodec_args(config.encode_accel, 20),
        "-c:a", "copy",
        dst,
    ]
    run_ffmpeg(args, cancel_event, pause_event, log, cwd=cwd)


# 缩略图抽帧限流：素材库几百条缩略图并发请求时，同时最多 3 路 ffmpeg 抽帧
_THUMB_LIMIT = threading.Semaphore(3)


def get_thumbnail(path: str, max_width: int = 320) -> Optional[str]:
    """抽取素材缩略图（带缓存），失败返回 None。
    未命中缓存时并发抽帧限流（同时最多 3 路），避免素材库几百条缩略图同时请求时线程/内存爆炸。"""
    if not Path(path).exists():
        return None
    cached = _thumb_cache_path(path, max_width)
    if cached.exists() and cached.stat().st_size > 0:
        return str(cached)
    with _THUMB_LIMIT:
        return _render_thumbnail(path, max_width, cached)
def _render_thumbnail(path: str, max_width: int, cached: Path) -> Optional[str]:
    """实际抽帧渲染缩略图（调用方需已持有 _THUMB_LIMIT 额度）。"""
    info = probe_media(path)
    if not info.get("ok"):
        return None
    seek = min(0.5, info["duration"] / 3.0)
    tmp = cached.with_name(f"{cached.stem}.{os.getpid()}.{threading.get_ident()}.tmp.jpg")
    args = [
        _ffmpeg(),
        "-y",
        "-ss",
        f"{seek:.3f}",
        "-i",
        path,
        "-frames:v",
        "1",
        "-vf",
        f"scale='min({max_width},iw)':-2",
        "-q:v",
        "4",
        tmp,
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            timeout=30,
        )
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            return None
        os.replace(tmp, cached)
        return str(cached)
    except Exception:
        tmp.unlink(missing_ok=True)
        return None


def has_nvenc() -> bool:
    """探测当前 ffmpeg 是否支持 h264_nvenc（结果缓存）。"""
    if not hasattr(has_nvenc, "_cache"):
        try:
            p = subprocess.run(
                [_ffmpeg(), "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=30,
            )
            has_nvenc._cache = "h264_nvenc" in (p.stdout or "")
        except Exception:
            has_nvenc._cache = False
    return has_nvenc._cache


def _vcodec_args(accel: str, crf: int) -> list:
    """按加速模式返回视频编码参数（accel: cpu / nvenc / auto）。
    NVENC 用恒定质量(CQ)模式，qp 从 x264 crf 近似映射（qp≈crf+2）。"""
    use_nvenc = accel == "nvenc" or (accel == "auto" and has_nvenc())
    if use_nvenc:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "constqp", "-qp", str(min(31, crf + 2))]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf)]


@dataclass
class JobConfig:
    head_folder: str
    tail_folder: str
    fixed_head: Optional[str] = None
    fixed_tail: Optional[str] = None
    output_folder: str = ""
    count: int = 10
    resolution: str = "1080x1920"
    duration_mode: str = "不限制"
    use_watermark: bool = False
    watermark_path: str = ""
    use_transition: bool = False
    transition_mode: str = "不使用"
    transition_type: str = "fade"
    transition_duration: float = 0.5
    transition_types: list[str] = field(default_factory=list)
    bgm_mode: str = "不使用"
    bgm_path: str = ""
    bgm_folder: str = ""
    fixed_bgm: Optional[str] = None
    bgm_volume: float = 0.2
    audio_volume: float = 1.0
    material_volumes: dict = field(default_factory=dict)  # 素材路径 -> 音量系数（≠1.0 时生效）
    normalize_audio: bool = False
    bgm_fade: bool = False
    bgm_ducking: bool = False
    fit_mode: str = "fit"
    output_name_template: str = "output_{序号}_{开头}_{结尾}"
    random_seed: int = 20260905
    dedupe_enabled: bool = True
    middle_folder: str = ""
    fixed_middle: Optional[str] = None
    middle_items: list[str] = field(default_factory=list)
    middle_count: Optional[int] = None
    middle_pools: list[dict] = field(default_factory=list)
    use_subtitle: str = ""   # 字幕样式：''=关 / minimal / outline / bubble / danmaku
    voiceover_wav: str = ""  # 口播配音（WAV 路径）：替代成片原声；空=不启用
    dedupe_level: str = "off"
    dedupe_options: dict = field(default_factory=lambda: {"visual": True, "segment": True, "audio": True})
    dedupe_versions: int = 1
    watermark_mode: str = "铺满全屏"
    watermark_position: str = "右下角"
    watermark_scale: float = 0.15
    watermark_opacity: float = 0.6
    # 隐式去重水印（去重模块：几乎透明的跳动图案，让每帧像素不同）
    im_wm_enabled: bool = False
    im_wm_source: str = "auto"
    im_wm_image: str = ""
    im_wm_motion: str = "static"
    im_wm_density: str = "frame"
    im_wm_opacity: float = 0.05
    # 画面滤镜（去重模块：随机或自定义调色，每条成片不同观感）
    filter_enabled: bool = False
    filter_mode: str = "random"
    filter_name: str = "warm"
    filter_lut_path: str = ""
    filter_strength: float = 20.0
    workers: int = 2
    encode_accel: str = "auto"


@dataclass
class BatchResult:
    success: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)
    failed_items: list[dict] = field(default_factory=list)
    success_items: list[dict] = field(default_factory=list)


def precheck_materials(config: "JobConfig") -> dict:
    """批量预检素材，返回每类素材的健康状态，坏文件提前标出。"""
    middle_pool_files: list[str] = []
    for pool in build_middle_pools(config):
        middle_pool_files.extend(pool.get("files") or [])
    middle_files: list[str] = []
    for p in middle_pool_files:
        if p not in middle_files:
            middle_files.append(p)
    groups: dict[str, list[str]] = {
        "head": scan_videos(config.head_folder),
        "tail": scan_videos(config.tail_folder),
        "middle": middle_files,
        "bgm": scan_audio(config.bgm_folder) if config.bgm_folder else [],
    }
    out: dict[str, list[dict]] = {}
    for kind, files in groups.items():
        items = []
        is_audio = kind == "bgm"
        for path in files:
            try:
                info = probe_media(path, require_video=not is_audio)
                items.append(
                    {
                        "path": path,
                        "name": Path(path).name,
                        "ok": info.get("ok", True),
                        "duration": round(info.get("duration", 0.0), 2),
                        "has_audio": info.get("has_audio", False),
                        "width": info.get("width", 0),
                        "height": info.get("height", 0),
                        "error": "" if info.get("ok", True) else ("无法读取音频" if is_audio else "无法读取视频流"),
                    }
                )
            except Exception as exc:
                items.append(
                    {
                        "path": path,
                        "name": Path(path).name,
                        "ok": False,
                        "duration": 0.0,
                        "has_audio": False,
                        "width": 0,
                        "height": 0,
                        "error": str(exc),
                    }
                )
        out[kind] = items
    return out


def _process_one_item(
    idx: int,
    head: str,
    tail: str,
    middle_items: list[str],
    final_path: Path,
    config: "JobConfig",
    width: int,
    height: int,
    duration_limit: Optional[float],
    bgm_files: list[str],
    cancel_event,
    pause_event,
    log: Callable[[str], None],
    retry_count: int,
    path_locks: dict,
) -> dict:
    """处理单个组合，返回结果字典。由 worker 线程调用。"""
    if cancel_event.is_set():
        return {"index": idx, "state": "cancelled"}
    while pause_event.is_set() and not cancel_event.is_set():
        time.sleep(0.2)
    if cancel_event.is_set():
        return {"index": idx, "state": "cancelled"}

    if final_path.exists() and final_path.stat().st_size > 0:
        log(f"[{idx}] 已存在，跳过：{final_path}")
        return {"index": idx, "state": "skipped", "output": str(final_path)}

    lock = path_locks.setdefault(str(final_path), threading.Lock())
    with lock:
        if final_path.exists() and final_path.stat().st_size > 0:
            log(f"[{idx}] 已存在，跳过：{final_path}")
            return {"index": idx, "state": "skipped", "output": str(final_path)}
        middle_desc = " + ".join(Path(p).name for p in middle_items)
        if middle_desc:
            middle_desc = " + " + middle_desc
        log(f"[{idx}] 开始生成：{Path(head).name}{middle_desc} + {Path(tail).name}")
        last_error = None
        for attempt in range(retry_count + 1):
            if cancel_event.is_set():
                return {"index": idx, "state": "cancelled"}
            try:
                delta = dedupe_delta(config.dedupe_level, config.dedupe_options, config.random_seed + idx)
                _process_one_combo(
                    head,
                    tail,
                    final_path,
                    middle_items,
                    pick_transition(config, idx),
                    pick_bgm(config, idx, bgm_files),
                    width,
                    height,
                    duration_limit,
                    config,
                    cancel_event,
                    pause_event,
                    log,
                    delta,
                )
                log(f"[{idx}] 完成：{final_path}")
                return {
                    "index": idx, "state": "success", "head": head, "tail": tail,
                    "middle": middle_display(middle_items),
                    "middle_files": middle_items,
                    "output": str(final_path),
                }
            except CancelledError:
                return {"index": idx, "state": "cancelled"}
            except Exception as exc:
                last_error = exc
                log(f"[{idx}] 第 {attempt + 1} 次失败：{exc}")
                if attempt < retry_count:
                    time.sleep(1)
        return {
            "index": idx, "state": "failed", "head": head, "tail": tail,
            "middle": middle_display(middle_items),
            "middle_files": middle_items,
            "error": str(last_error),
        }


def process_batch(
    config: JobConfig,
    cancel_event,
    pause_event,
    log: Optional[Callable[[str], None]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    retry_count: int = 2,
    skip_existing: bool = True,
) -> BatchResult:
    result = BatchResult()
    head_files = dedupe_by_fp(scan_videos(config.head_folder))
    tail_files = dedupe_by_fp(scan_videos(config.tail_folder))
    middle_pools = build_middle_pools(config)
    bgm_files = dedupe_by_fp(scan_audio(config.bgm_folder)) if config.bgm_folder else []
    if not head_files:
        raise MediaError("开头文件夹中没有找到视频文件。")
    if not tail_files:
        raise MediaError("结尾文件夹中没有找到视频文件。")

    if config.fixed_head and config.fixed_head not in head_files:
        raise MediaError("固定开头不在开头文件夹中，请重新选择。")
    if config.fixed_tail and config.fixed_tail not in tail_files:
        raise MediaError("固定结尾不在结尾文件夹中，请重新选择。")
    for pool in middle_pools:
        missing_items = [p for p in (pool.get("items") or []) if p not in pool.get("files", [])]
        if missing_items:
            raise MediaError(
                f"中间素材池 {Path(pool['folder']).name} 中的以下固定素材不在该文件夹中："
                f"{'、'.join(Path(p).name for p in missing_items)}"
            )
    if config.bgm_mode == "音乐文件夹固定" and config.fixed_bgm and config.fixed_bgm not in bgm_files:
        raise MediaError("固定 BGM 不在音乐文件夹中，请重新选择。")

    count = max(1, min(200, int(config.count) * max(1, int(config.dedupe_versions or 1))))
    if config.fixed_head and config.fixed_tail:
        count = 1
    combos = build_combinations(
        head_files,
        tail_files,
        config.fixed_head,
        config.fixed_tail,
        count,
        config.random_seed,
        config.dedupe_enabled,
    )
    if not combos:
        raise MediaError("没有可生成的素材组合。")
    if len(combos) < count:
        logger = _make_task_logger(log)
        logger(f"素材组合数不足：设定 {count} 条，可用组合仅 {len(combos)} 条，已按全部可用组合生成（不会重复出片）。")

    output_dir = Path(config.output_folder or Path(config.head_folder).parent / "output")
    output_dir.mkdir(parents=True, exist_ok=True)
    width, height = resolve_resolution(config.resolution)
    duration_limit = resolve_duration_mode(config.duration_mode)

    logger = _make_task_logger(log)
    task_id = time.strftime("%Y%m%d_%H%M%S")
    logger(f"任务 {task_id} 开始，共 {len(combos)} 条，并发 {config.workers}")

    workers = max(1, min(8, int(config.workers or 1)))
    path_locks: dict = {}
    done_count = 0
    done_lock = threading.Lock()

    def on_done() -> None:
        nonlocal done_count
        with done_lock:
            done_count += 1
            current = done_count
        if progress:
            progress(current, len(combos))

    if workers <= 1:
        for idx, (head, tail) in enumerate(combos, 1):
            if cancel_event.is_set():
                result.cancelled = True
                break
            middle_items = pick_middle_sequence(middle_pools, idx, config.random_seed, exclude=[head, tail])
            first_middle = middle_items[0] if middle_items else None
            final_name = render_output_name(config.output_name_template, idx, head, tail, first_middle)
            final_path = output_dir / final_name
            if skip_existing and final_path.exists() and final_path.stat().st_size > 0:
                result.skipped += 1
                on_done()
                continue
            item = _process_one_item(
                idx, head, tail, middle_items, final_path, config, width, height,
                duration_limit, bgm_files, cancel_event, pause_event, logger,
                retry_count, path_locks,
            )
            _collect_item(result, item)
            on_done()
    else:
        tasks = []
        for idx, (head, tail) in enumerate(combos, 1):
            middle_items = pick_middle_sequence(middle_pools, idx, config.random_seed, exclude=[head, tail])
            first_middle = middle_items[0] if middle_items else None
            final_name = render_output_name(config.output_name_template, idx, head, tail, first_middle)
            final_path = output_dir / final_name
            tasks.append((idx, head, tail, middle_items, final_path))

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for idx, head, tail, middle_items, final_path in tasks:
                if cancel_event.is_set():
                    result.cancelled = True
                    break
                if skip_existing and final_path.exists() and final_path.stat().st_size > 0:
                    result.skipped += 1
                    on_done()
                    continue
                fut = executor.submit(
                    _process_one_item,
                    idx, head, tail, middle_items, final_path, config, width, height,
                    duration_limit, bgm_files, cancel_event, pause_event, logger,
                    retry_count, path_locks,
                )
                futures[fut] = idx
            for fut in as_completed(futures):
                # 取消后不再收集/计数剩余已提交任务（避免空转把进度推满），线程池退出时自动等待其快速返回
                if cancel_event.is_set():
                    result.cancelled = True
                    break
                try:
                    item = fut.result()
                except Exception as exc:
                    item = {"index": futures[fut], "state": "failed", "error": str(exc)}
                _collect_item(result, item)
                on_done()

    if progress:
        # 用实际完成数收尾：正常完成时=总数(100%)；取消/中断时停在已完成数，避免虚报 100%
        progress(done_count, len(combos))
    logger(
        f"任务 {task_id} 结束：成功 {result.success}，跳过 {result.skipped}，失败 {result.failed}"
    )
    return result


def _collect_item(result: BatchResult, item: dict) -> None:
    state = item.get("state")
    if state == "success":
        result.success += 1
        result.success_items.append(
            {
                "index": item["index"],
                "head": item.get("head", ""),
                "tail": item.get("tail", ""),
                "middle": item.get("middle"),
                "middle_files": item.get("middle_files") or [],
                "output": item.get("output", ""),
            }
        )
    elif state == "failed":
        result.failed += 1
        message = f"[{item['index']}] 最终失败：{item.get('error', '')}"
        result.errors.append(message)
        result.failed_items.append(
            {
                "index": item["index"],
                "head": item.get("head", ""),
                "tail": item.get("tail", ""),
                "middle": item.get("middle"),
                "middle_files": item.get("middle_files") or [],
                "error": item.get("error", ""),
            }
        )
    elif state == "skipped":
        result.skipped += 1
    elif state == "cancelled":
        result.cancelled = True


def _process_one_combo(
    head: str,
    tail: str,
    final_path: Path,
    middle_items: list[str],
    transition_type: Optional[str],
    selected_bgm: Optional[str],
    width: int,
    height: int,
    duration_limit: Optional[float],
    config: JobConfig,
    cancel_event,
    pause_event,
    log: Callable[[str], None],
    delta: Optional[dict] = None,
) -> None:
    tempdir = Path(tempfile.mkdtemp(prefix="sppj_"))
    try:
        head_info = probe_media(head)
        tail_info = probe_media(tail)
        head_norm = str(tempdir / "head_norm.mp4")
        tail_norm = str(tempdir / "tail_norm.mp4")
        concat_path = str(tempdir / "concat.mp4")
        current = concat_path

        normalize_clip(
            head, head_norm, width, height, head_info["duration"], head_info["has_audio"],
            cancel_event, pause_event, log, config.normalize_audio, config.fit_mode, encode_accel=config.encode_accel,
            volume=float(config.material_volumes.get(head, 1.0)),
        )
        normalize_clip(
            tail, tail_norm, width, height, tail_info["duration"], tail_info["has_audio"],
            cancel_event, pause_event, log, config.normalize_audio, config.fit_mode, encode_accel=config.encode_accel,
            volume=float(config.material_volumes.get(tail, 1.0)),
        )

        clips = [head_norm]
        # 转场 offset 必须以归一化后文件的实际时长为依据（-shortest 等会使
        # 原素材 probe 时长与归一化文件存在偏差），否则 xfade 过渡点与
        # acrossfade 边界错位、音画不同步且随片段数累积。
        durations = [probe_media(head_norm)["duration"]]
        for i, middle in enumerate(middle_items, 1):
            middle_info = probe_media(middle)
            middle_norm = str(tempdir / f"middle_norm_{i}.mp4")
            normalize_clip(
                middle, middle_norm, width, height, middle_info["duration"], middle_info["has_audio"],
                cancel_event, pause_event, log, config.normalize_audio, config.fit_mode, encode_accel=config.encode_accel,
                volume=float(config.material_volumes.get(middle, 1.0)),
            )
            clips.append(middle_norm)
            durations.append(probe_media(middle_norm)["duration"])
        clips.append(tail_norm)
        durations.append(probe_media(tail_norm)["duration"])

        n_clips = len(clips)
        # 差异化：入点偏移（输入级 -ss）会缩短各片段实际时长，xfade 计算须用裁剪后时长
        offset = float((delta or {}).get("offset") or 0.0)
        speed = float((delta or {}).get("speed") or 1.0)
        eff_durations = [max(0.3, d - offset) / speed for d in durations]
        ss_offsets = [offset] * n_clips if offset > 0 else None
        # 深度差异化下转场随机化（类型+时长），打破剪辑序列指纹（参数在 _process_one_item 生成）
        t_type = (delta or {}).get("transition") or transition_type
        t_duration = float((delta or {}).get("transition_duration") or config.transition_duration)
        if n_clips == 2:
            if t_type or delta:
                concat_two(
                    clips[0], clips[1], concat_path,
                    eff_durations[0], eff_durations[1],
                    cancel_event, pause_event, t_type, t_duration, log, delta, ss_offsets, encode_accel=config.encode_accel,
                    width=width, height=height,
                )
            else:
                # 无转场且素材已统一归一化，使用流复制快路径
                concat_copy(clips, concat_path, cancel_event, pause_event, log)
        else:
            concat_chain(
                clips, concat_path, eff_durations,
                cancel_event, pause_event, t_type, t_duration, log, delta, ss_offsets, encode_accel=config.encode_accel,
                width=width, height=height,
            )

        total_duration = sum(eff_durations)
        if duration_limit and total_duration > duration_limit:
            trimmed = str(tempdir / "trimmed.mp4")
            trim_duration(concat_path, trimmed, duration_limit, cancel_event, pause_event, log)
            current = trimmed

        # 口播配音替代原声：在 BGM 混音前替换音轨，配音短补静音、长截断，画面时长不变
        if config.voiceover_wav and os.path.isfile(config.voiceover_wav):
            voiced = str(tempdir / "with_voiceover.mp4")
            log("正在应用口播配音（替代原声）...")
            replace_audio_with_voiceover(
                current, config.voiceover_wav, voiced,
                cancel_event, pause_event, log,
            )
            current = voiced

        # 原声音量：有 BGM 时在 mix_bgm 原声链处理；无 BGM 时单独重编码音频调整
        if config.bgm_mode == "不使用" and abs(config.audio_volume - 1.0) > 0.001:
            volumed = str(tempdir / "with_volume.mp4")
            apply_audio_volume(current, volumed, config.audio_volume, cancel_event, pause_event, log)
            current = volumed

        if config.bgm_mode in {"本地导入", "算法生成", "音乐文件夹固定", "音乐文件夹随机"}:
            if config.bgm_mode == "算法生成":
                bgm = str(tempdir / "bgm.wav")
                log("正在生成背景音乐...")
                generate_bgm_wav(bgm, max(32.0, total_duration))
                bgm_duration = max(32.0, total_duration)
            else:
                bgm = selected_bgm or config.bgm_path
                if not bgm:
                    raise MediaError("没有可用的 BGM 文件")
                bgm_info = probe_media(bgm)
                bgm_duration = bgm_info["duration"]
            mixed = str(tempdir / "with_bgm.mp4")
            bgm_shift = 0.0
            if (delta or {}).get("bgm_shift_seed") is not None and bgm_duration > total_duration + 1:
                shift_rng = random.Random(delta["bgm_shift_seed"])
                bgm_shift = round(shift_rng.uniform(0.0, bgm_duration - total_duration), 3)
            mix_bgm(
                current, bgm, mixed, float(config.bgm_volume),
                cancel_event, pause_event, log,
                fade=config.bgm_fade, ducking=config.bgm_ducking, bgm_duration=bgm_duration,
                audio_volume=float(config.audio_volume),
                bgm_shift=bgm_shift,
            encode_accel=config.encode_accel,
            )
            current = mixed

        if config.use_subtitle:
            if not subtitle_plugin.available():
                raise subtitle_plugin.SubtitleUnavailableError(
                    "自动字幕需要 faster-whisper。请运行：pip install faster-whisper"
                )
            srt_path = str(tempdir / "subtitle.srt")
            subtitle_plugin.generate_subtitles(head, tail, head_info["duration"], srt_path)
            subtitled = str(tempdir / "with_subtitle.mp4")
            # 样式烧录复用工具箱模板（docs/工具箱设计方案.md：生成页联动）
            from toolbox.subtitle import burn_with_style
            burn_with_style(
                current, srt_path, subtitled, config.use_subtitle,
                cancel=lambda: cancel_event.is_set(),
                encode_accel=config.encode_accel,
            )
            current = subtitled

        if config.filter_enabled:
            filtered = str(tempdir / "filtered.mp4")
            _fseed = (config.random_seed & 0x7FFFFFFF) ^ (hash(str(final_path)) & 0x7FFFFFFF)
            apply_video_filter(current, filtered, config, cancel_event, pause_event, log, _fseed)
            current = filtered

        if config.im_wm_enabled:
            im_wmed = str(tempdir / "with_im_wm.mp4")
            _seed = (config.random_seed & 0x7FFFFFFF) ^ (hash(str(final_path)) & 0x7FFFFFFF)
            apply_im_watermark(current, im_wmed, width, height, config,
                               cancel_event, pause_event, log, _seed)
            current = im_wmed

        if config.use_watermark:
            if not config.watermark_path:
                raise MediaError("已勾选水印，但未选择水印图片。")
            watermarked = str(tempdir / "with_watermark.mp4")
            apply_watermark(
                current, config.watermark_path, watermarked, width, height,
                cancel_event, pause_event, log,
                mode=config.watermark_mode, position=config.watermark_position,
                scale=config.watermark_scale, opacity=config.watermark_opacity,
            encode_accel=config.encode_accel,
            )
            current = watermarked

        shutil.move(current, final_path)
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)


def _make_task_logger(
    callback: Optional[Callable[[str], None]],
) -> Callable[[str], None]:
    log_dir = _app_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"task_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}.log"
    # 自动清理旧日志：保留最近 50 个（本次将新建 1 个 → 清到 49 旧），避免长期使用无限增长
    try:
        old = sorted(log_dir.glob("task_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in old[49:]:
            stale.unlink(missing_ok=True)
    except Exception:
        pass
    lock = threading.Lock()

    def write(message: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        if callback:
            callback(line)
        try:
            with lock:
                with log_file.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass

    return write


def process_failed_items(
    config: JobConfig,
    failed_items: list[dict],
    cancel_event,
    pause_event,
    log: Optional[Callable[[str], None]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    retry_count: int = 2,
) -> BatchResult:
    result = BatchResult()
    if not failed_items:
        return result
    output_dir = Path(config.output_folder or Path(config.head_folder).parent / "output")
    output_dir.mkdir(parents=True, exist_ok=True)
    width, height = resolve_resolution(config.resolution)
    duration_limit = resolve_duration_mode(config.duration_mode)
    bgm_files = scan_audio(config.bgm_folder) if config.bgm_folder else []
    logger = _make_task_logger(log)
    logger(f"开始重试失败项，共 {len(failed_items)} 条")

    for pos, item in enumerate(failed_items, 1):
        idx = int(item.get("index", pos))
        head = str(item.get("head", ""))
        tail = str(item.get("tail", ""))
        middle_files_retry = [str(p) for p in (item.get("middle_files") or []) if str(p)]
        if not middle_files_retry and item.get("middle"):
            # 兼容旧记录：middle 是单条路径
            m = str(item.get("middle") or "")
            if m and not m.startswith("、") and "、" not in m:
                middle_files_retry = [m]
        if progress:
            progress(pos, len(failed_items))
        if cancel_event.is_set():
            result.cancelled = True
            break
        while pause_event.is_set() and not cancel_event.is_set():
            time.sleep(0.2)
        first_middle = middle_files_retry[0] if middle_files_retry else None
        final_path = output_dir / render_output_name(config.output_name_template, idx, head, tail, first_middle)
        if final_path.exists():
            final_path.unlink(missing_ok=True)
        middle_desc = " + ".join(Path(p).name for p in middle_files_retry)
        if middle_desc:
            middle_desc = " + " + middle_desc
        logger(f"[{pos}/{len(failed_items)}] 重试：{Path(head).name}{middle_desc} + {Path(tail).name}")
        last_error = None
        for attempt in range(retry_count + 1):
            try:
                _process_one_combo(
                    head, tail, final_path, middle_files_retry,
                    pick_transition(config, idx), pick_bgm(config, idx, bgm_files),
                    width, height, duration_limit, config,
                    cancel_event, pause_event, logger,
                )
                result.success += 1
                result.success_items.append(
                    {
                        "index": idx, "head": head, "tail": tail,
                        "middle": middle_display(middle_files_retry),
                        "middle_files": middle_files_retry,
                        "output": str(final_path),
                    }
                )
                last_error = None
                logger(f"[{pos}/{len(failed_items)}] 重试完成：{final_path}")
                break
            except CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                logger(f"[{pos}/{len(failed_items)}] 第 {attempt + 1} 次失败：{exc}")
                if attempt < retry_count:
                    time.sleep(1)
        else:
            result.failed += 1
            message = f"[{pos}/{len(failed_items)}] 最终失败：{last_error}"
            result.errors.append(message)
            result.failed_items.append(
                {
                    "index": idx, "head": head, "tail": tail,
                    "middle": middle_display(middle_files_retry),
                    "middle_files": middle_files_retry,
                    "error": str(last_error),
                }
            )
            logger(message)

    if progress:
        # 收尾用实际处理到的位置：正常完成=总数；取消/中断停在已处理数
        progress(pos, len(failed_items))
    return result

def _frame_dhash(gray9x8: np.ndarray) -> np.ndarray:
    """8x9 灰度帧的 dHash（64bit）：比较相邻列像素，得到 8x8 差异位。"""
    return (gray9x8[:, 1:] > gray9x8[:, :-1]).flatten().astype(np.uint8)


def extract_frame_hashes(path: str, frame_times: list[float]) -> list[np.ndarray]:
    """按给定时间点抽帧（9x8 gray raw，每帧 64bit dHash），返回 dHash 列表。
    独立函数：供指纹/查重/缩略图等复用（后期生成时落帧缓存也走这里）。"""
    tempdir = Path(tempfile.mkdtemp(prefix="sppj_fp_"))
    bits: list[np.ndarray] = []
    try:
        for i, t in enumerate(frame_times):
            raw = tempdir / f"f{i}.raw"
            args = [
                _ffmpeg(), "-y",
                "-ss", f"{t:.3f}",
                "-i", path,
                "-frames:v", "1",
                "-f", "rawvideo", "-pix_fmt", "gray", "-s", "9x8",
                str(raw),
            ]
            # 隐藏窗口：便携版下避免查重抽帧弹出黑窗
            subprocess.run(args, capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if raw.exists() and raw.stat().st_size == 72:  # 9*8
                data = np.frombuffer(raw.read_bytes(), dtype=np.uint8).reshape(8, 9)
                bits.append(_frame_dhash(data))
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)
    return bits


def fingerprint_video(path: str, frames: int = 3) -> Optional[np.ndarray]:
    """抽取 frames 个均匀时间点帧（每帧 64bit dHash），拼接成视频指纹。
    失败（无法探测/抽帧失败）返回 None。"""
    try:
        dur = probe_media(path)["duration"]
    except Exception:
        return None
    if not dur or dur <= 0:
        return None
    times = [dur * (i + 1) / (frames + 1) for i in range(frames)]
    bits = extract_frame_hashes(path, times)
    if not bits:
        return None
    return np.concatenate(bits)


def _frame_cache_path(src: str) -> Path:
    key = _file_fingerprint(src, "frames")
    folder = cache_dir() / "frames"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{key}.json"


def save_frame_cache(src: str, frames: int = 2) -> Optional[Path]:
    """抽帧并存盘（供查重/未来复用）。返回缓存路径，失败返回 None。
    文件变化（size/mtime）时指纹键自动变化，旧缓存自然失效。"""
    try:
        dur = probe_media(src)["duration"]
    except Exception:
        return None
    if not dur or dur <= 0:
        return None
    times = [dur * (i + 1) / (frames + 1) for i in range(frames)]
    bits = extract_frame_hashes(src, times)
    if not bits:
        return None
    arr = np.concatenate(bits)  # frames*8 字节（每帧 64bit dHash）
    payload = base64.b64encode(arr.tobytes()).decode("ascii")
    p = _frame_cache_path(src)
    p.write_text(json.dumps({"frames": frames, "b64": payload}), encoding="utf-8")
    return p


def load_frame_cache(src: str) -> Optional[np.ndarray]:
    """读帧缓存（未命中/损坏返回 None）。文件变化后键不同，自动视为未命中。"""
    p = _frame_cache_path(src)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        arr = np.frombuffer(base64.b64decode(data["b64"]), dtype=np.uint8)
        return arr.reshape(-1)
    except Exception:
        return None


def _cleanup_frame_cache(max_age_days: int = 7) -> None:
    """低频清理孤儿帧缓存：仅清理超过 N 天未更新的帧文件（产物早已删除/更替）。"""
    try:
        folder = cache_dir() / "frames"
        if not folder.exists():
            return
        cutoff = time.time() - max_age_days * 86400
        for p in folder.glob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:
        pass


# 查重指纹进程内缓存：path -> (文件mtime, 指纹)。断点续跑/同批重复查重直接命中，不重复抽帧。
_fp_cache: dict[str, tuple[float, Optional[np.ndarray]]] = {}
_fp_cache_lock = threading.Lock()
_last_fp_cleanup: float = 0.0


def _fingerprint_cached(path: str, frames: int = 2) -> Optional[np.ndarray]:
    global _last_fp_cleanup
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    with _fp_cache_lock:
        hit = _fp_cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    # 落盘缓存优先（服务重启后仍命中，零 ffmpeg 启动）
    fp = load_frame_cache(path)
    if fp is None:
        fp = fingerprint_video(path, frames=frames)
        if fp is not None:
            try:
                save_frame_cache(path, frames=frames)
            except Exception:
                pass
    with _fp_cache_lock:
        _fp_cache[path] = (mtime, fp)
    # 低频清理孤儿帧缓存（每天最多一次）
    if time.time() - _last_fp_cleanup > 86400:
        _last_fp_cleanup = time.time()
        _cleanup_frame_cache()
    return fp


def find_similar_outputs(outputs: list[str], threshold: float = 0.88,
                         progress: Optional[Callable[[int, int, str], None]] = None) -> list[dict]:
    """对输出文件两两比对感知哈希，返回疑似重复对 [{a, b, sim}]（按相似度降序）。
    threshold=0.88 表示两文件指纹差异 <12%（画面高度一致才报疑似重复）。
    并发抽帧（最多 4 路）+ 进程内缓存，避免逐条串行启动 ffmpeg。
    progress(done, total, stage)：stage='fingerprint' 抽帧阶段 / 'compare' 比对阶段。"""
    candidates = [p for p in outputs if p and os.path.isfile(p)]
    fps: dict[str, np.ndarray] = {}
    if candidates:
        from concurrent.futures import ThreadPoolExecutor
        done = 0
        with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as ex:
            for p, fp in zip(candidates, ex.map(_fingerprint_cached, candidates)):
                done += 1
                if progress:
                    progress(done, len(candidates), "fingerprint")
                if fp is not None:
                    fps[p] = fp
    paths = list(fps)
    total_pairs = len(paths) * (len(paths) - 1) // 2
    pairs: list[dict] = []
    checked = 0
    for i in range(len(paths)):
        for j in range(i + 1, len(paths)):
            checked += 1
            if progress:
                progress(checked, total_pairs, "compare")
            total = fps[paths[i]].size
            dist = int(np.count_nonzero(fps[paths[i]] ^ fps[paths[j]]))
            sim = 1.0 - dist / total
            if sim >= threshold:
                pairs.append({
                    "a": Path(paths[i]).name,
                    "b": Path(paths[j]).name,
                    "sim": round(sim, 3),
                })
    pairs.sort(key=lambda x: -x["sim"])
    return pairs
