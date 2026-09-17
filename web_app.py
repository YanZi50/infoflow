# 便携版隔离：打包运行时剔除外部 PYTHONPATH 注入（如豆包 python-packages），
# 确保模块一律从 _internal 打包副本加载，避免引用开发机路径导致换机不可用。
import sys as _sys

if getattr(_sys, "frozen", False):
    _ppath = [p for p in _sys.path if p and "python-packages" in p]
    for _p in _ppath:
        _sys.path.remove(_p)

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
import base64
import urllib.request
from dataclasses import asdict
from typing import Optional
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from config_store import load_last_config, save_last_config
from history_store import clear_history, list_history, save_history
from template_store import delete_template, list_templates, load_template, save_template
from platform_presets import apply_preset, get_presets
import subtitle_plugin
from toolbox import engine as toolbox_engine
from toolbox.engine import TOOL_NAMES

from video_engine import (
    BatchResult,
    JobConfig,
    MediaError,
    dedupe_by_fp,
    find_similar_outputs,
    get_thumbnail,
    media_fingerprint,
    precheck_materials,
    process_batch,
    process_failed_items,
    probe_media,
    scan_audio,
    scan_videos,
)


HOST = "127.0.0.1"
PORT = 8765
if getattr(sys, "frozen", False):
    WEB_DIR = Path(sys._MEIPASS) / "web"
    UPLOAD_ROOT = Path(sys.executable).resolve().parent / "uploads"
    PREVIEW_DIR = Path(sys.executable).resolve().parent / "previews"
    STATE_DIR = Path(sys.executable).resolve().parent / "state"
else:
    WEB_DIR = Path(__file__).parent / "web"
    UPLOAD_ROOT = Path(__file__).parent / "uploads"
    PREVIEW_DIR = Path(__file__).parent / "previews"
    STATE_DIR = Path(__file__).parent / "state"

# 运行中任务快照：服务被强杀/重启后，据此恢复"上次任务中断"，支持断点续跑
SNAPSHOT_FILE = STATE_DIR / "last_task.json"

# 素材扫描缓存：folder -> (目录mtime, 文件列表)。目录变动才重扫，避免重复 listdir+指纹。
_SCAN_CACHE: dict[str, tuple[float, list[str]]] = {}
_SCAN_LOCK = threading.Lock()


def _with_sizes(items: list) -> list:
    """为成功产物补充文件大小（生成结果列表展示用）。"""
    out = []
    for it in items or []:
        d = dict(it)
        try:
            p = d.get("output") or ""
            if p and os.path.isfile(p):
                d["size"] = os.path.getsize(p)
        except OSError:
            pass
        out.append(d)
    return out


def _scan_cached(folder: str, kind: str) -> list[str]:
    """带目录 mtime 缓存的扫描：目录未变动时直接返回缓存列表。"""
    if not folder:
        return []
    try:
        st = os.stat(folder)
        mtime = st.st_mtime
    except OSError:
        return []
    with _SCAN_LOCK:
        hit = _SCAN_CACHE.get((folder, kind))
        if hit and hit[0] == mtime:
            return hit[1]
    files = scan_videos(folder) if kind == "video" else scan_audio(folder)
    with _SCAN_LOCK:
        _SCAN_CACHE[(folder, kind)] = (mtime, files)
    return files


def _fmt_size(n: int) -> str:
    n = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_eta(seconds: float) -> str:
    s = int(max(1, round(seconds)))
    if s < 60:
        return f"约 {s} 秒"
    if s < 3600:
        return f"约 {s // 60} 分 {s % 60} 秒"
    return f"约 {s // 3600} 时 {(s % 3600) // 60} 分"


# ---------------------------------------------------------------------------
# 下载白名单：download / zip 只允许访问被登记的输出目录
# ---------------------------------------------------------------------------
ALLOWED_DIRS: set[str] = set()
ALLOWED_LOCK = threading.Lock()


def register_allowed_dir(path: str) -> None:
    if not path:
        return
    real = os.path.realpath(path)
    with ALLOWED_LOCK:
        ALLOWED_DIRS.add(real)


def is_allowed_dir(path: str) -> bool:
    real = os.path.realpath(path)
    with ALLOWED_LOCK:
        for base in ALLOWED_DIRS:
            if real == base or real.startswith(base + os.sep):
                return True
    return False


def register_standard_dirs() -> None:
    register_allowed_dir(str(PREVIEW_DIR))
    register_allowed_dir(str(UPLOAD_ROOT))


# ---------------------------------------------------------------------------
# 运行中任务快照（断点续跑）：任务启动时写盘，正常结束/取消/异常时删除；
# 只有进程被强杀/崩溃时残留，下次启动据此恢复"上次任务中断"
# ---------------------------------------------------------------------------
def _save_snapshot(config: JobConfig, label: str, total: int) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_FILE.write_text(
            json.dumps(
                {"config": asdict(config), "label": label, "total": total, "started_at": time.time()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass  # 快照失败不阻塞任务


def _clear_snapshot() -> None:
    try:
        if SNAPSHOT_FILE.exists():
            SNAPSHOT_FILE.unlink()
    except Exception:
        pass


def _load_snapshot() -> dict | None:
    try:
        if not SNAPSHOT_FILE.exists():
            return None
        data = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        return {"config": JobConfig(**data["config"]), "label": data.get("label", "任务"), "total": data.get("total", 0)}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 应用状态
# ---------------------------------------------------------------------------
class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.paused = False
        self.logs: list[str] = []
        self.current = 0
        self.total = 0
        self.result: BatchResult | None = None
        self.error: str | None = None
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_config: JobConfig | None = None
        self.last_failed_items: list[dict] = []
        self.started_at: float | None = None
        self.samples: list[tuple[float, int]] = []
        self.interrupted: dict | None = None
        self.similar_pairs: list[dict] = []  # 本次任务输出疑似重复对
        self.deduping: bool = False          # 产物查重是否进行中（异步，生成完成即返回，查重后台跑）
        self.dedup_progress: dict | None = None  # 查重进度 {"stage", "done", "total"}（None=未在查重）
        self.last_speed: float | None = None  # 最近一次任务实测速度（条/秒，含并发）
        self.queue: list[dict] = []          # 待执行队列 [{id, label, payload}]
        self.queue_seq: int = 0
        self.update_info: dict | None = None  # 版本更新检查结果（失败保持 None，静默）
        self.update_download: dict | None = None  # 更新包下载状态 {"stage","done","total","error"}（None=未下载）
        # 工具箱任务状态（docs/工具箱设计方案.md）
        self.toolbox: dict = {
            "running": False,
            "stage": "idle",          # idle/running/done/cancelled/error
            "tool": "",
            "current": 0,
            "total": 0,
            "current_file": "",
            "results": [],            # [{name, status: ok|fail, detail}]
            "cancel": False,
            "out_dir": "",
            "error": None,
        }
        # 工具箱-字幕包装任务状态
        self.toolbox_sub: dict = {
            "running": False,
            "stage": "idle",          # idle/running/done/cancelled/error
            "mode": "",               # folder / file
            "style": "minimal",
            "current": 0,
            "total": 0,
            "current_file": "",
            "ok": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],             # [{name, error}]
            "cancel": False,
            "out_dir": "",
            "error": None,
        }
        # 工具箱-语音识别与索引任务状态
        self.toolbox_asr: dict = {
            "running": False,
            "stage": "idle",          # idle/running/done/cancelled/error
            "folder": "",
            "model": "small",
            "current": 0,
            "total": 0,
            "current_file": "",
            "done": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],             # [{name, error}]
            "cancel": False,
            "error": None,
        }
        # 工具箱-剪气口任务状态
        self.toolbox_vad: dict = {
            "running": False,
            "stage": "idle",          # idle/running/done/cancelled/error
            "mode": "",               # folder / file
            "sensitivity": 0.5,
            "min_silence": 0.6,
            "keep_pad": 0.3,
            "current": 0,
            "total": 0,
            "current_file": "",
            "ok": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],             # [{name, error}]
            "cancel": False,
            "out_dir": "",
            "error": None,
        }

    def add_log(self, message: str) -> None:
        with self.lock:
            self.logs.append(message)
            self.logs = self.logs[-1000:]

    def set_progress(self, current: int, total: int) -> None:
        with self.lock:
            self.current = current
            self.total = total
            if self.started_at is not None:
                self.samples.append((time.time(), current))
                self.samples = self.samples[-12:]

    def begin(self, total: int) -> None:
        with self.lock:
            self.started_at = time.time()
            self.samples = [(time.time(), 0)]
            self.current = 0
            self.total = total

    def end(self) -> None:
        with self.lock:
            self.started_at = None
            self.samples = []

    def _compute_rate(self, samples: list[tuple[float, int]]) -> float | None:
        if len(samples) < 2:
            return None
        ts0, done0 = samples[0]
        ts1, done1 = samples[-1]
        dt = ts1 - ts0
        if dt < 0.5 or done1 < done0:
            return None
        return (done1 - done0) / dt

    def eta_seconds(self) -> float | None:
        with self.lock:
            if self.started_at is None or self.total <= 0 or self.current >= self.total:
                return None
            samples = list(self.samples)
            current = self.current
            total = self.total
        rate = self._compute_rate(samples)
        if not rate or rate <= 0:
            return None
        return (total - current) / rate

    def speed_per_sec(self) -> float | None:
        with self.lock:
            if self.started_at is None:
                return None
            samples = list(self.samples)
        return self._compute_rate(samples)

    def status_dict(self) -> dict:
        with self.lock:
            data = {
                "running": self.running,
                "paused": self.paused,
                "current": self.current,
                "total": self.total,
                "logs": list(self.logs),
                "success": self.result.success if self.result else 0,
                "skipped": self.result.skipped if self.result else 0,
                "failed": self.result.failed if self.result else 0,
                "unfinished": max(0, self.total - (self.result.success + self.result.skipped + self.result.failed)) if self.result else 0,
                "cancelled": self.result.cancelled if self.result else False,
                "deduping": self.deduping,
                "dedup_progress": self.dedup_progress,
                "update_download": self.update_download,
                "toolbox": dict(self.toolbox),
                "toolbox_asr": dict(self.toolbox_asr),
                "toolbox_sub": dict(self.toolbox_sub),
                "toolbox_vad": dict(self.toolbox_vad),
                "portable": bool(getattr(sys, "frozen", False)),
                "failed_items": self.result.failed_items if self.result else [],
                "success_items": _with_sizes(self.result.success_items) if self.result else [],
                "error": self.error,
                "interrupted": self.interrupted,
                "last_config": self._last_config_preview(),
            }
            samples = list(self.samples)
            started_at = self.started_at
            current = self.current
            total = self.total
        data["eta_seconds"] = None
        data["speed_per_sec"] = None
        if started_at is not None and total > 0 and current < total:
            rate = self._compute_rate(samples)
            if rate and rate > 0:
                data["speed_per_sec"] = rate
                data["eta_seconds"] = (total - current) / rate
        return data

    def _last_config_preview(self) -> dict | None:
        """本次/上次任务的参数快照（前端展示用，精选字段）。"""
        cfg = self.last_config
        if cfg is None:
            return None
        return {
            "count": cfg.count,
            "workers": cfg.workers,
            "resolution": cfg.resolution,
            "duration_mode": cfg.duration_mode,
            "fit_mode": cfg.fit_mode,
            "transition_mode": cfg.transition_mode,
            "bgm_mode": cfg.bgm_mode,
            "use_watermark": cfg.use_watermark,
            "watermark_mode": cfg.watermark_mode,
            "watermark_path": cfg.watermark_path,
            "dedupe_level": cfg.dedupe_level,
            "dedupe_versions": cfg.dedupe_versions,
            "output_name_template": cfg.output_name_template,
            "head_folder": cfg.head_folder,
            "tail_folder": cfg.tail_folder,
            "output_folder": cfg.output_folder,
            "middle_pools": list(cfg.middle_pools or []),
        }


def _run_ps_file(script_body: str, timeout: int = 300) -> str:
    """执行 PowerShell 脚本（.ps1 文件方式，UTF-8 BOM 保证中文脚本正确读取）。
    结果以 Base64(UTF-8) 输出（纯 ASCII 不受控制台代码页影响），此处解码返回真实字符串。"""
    fd, tmp = tempfile.mkstemp(suffix=".ps1", prefix="sppj_dlg_")
    with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
        f.write(script_body)
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", tmp],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        b64 = (proc.stdout or "").strip()
        if not b64:
            return ""
        try:
            return base64.b64decode(b64).decode("utf-8")
        except Exception:
            return ""
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _toolbox_worker(tool: str, files: list[str], params: dict, out_dir: str) -> None:
    """工具箱后台线程：逐文件处理，渐进更新 STATE.toolbox。"""
    results: list[dict] = []
    total = len(files)
    try:
        if tool == "concat":
            # 合并：全部文件按文件名顺序拼为一条视频
            list_file = os.path.join(tempfile.gettempdir(), f"toolbox_concat_{time.time_ns()}.txt")
            toolbox_engine.build_concat_list(files, list_file)
            suffix = toolbox_engine.suffix_for(tool, params, files[0])
            dst = toolbox_engine._unique_dst(out_dir, "合并_" + time.strftime("%Y%m%d_%H%M%S"), suffix)
            try:
                STATE.toolbox["current"] = 0
                STATE.toolbox["current_file"] = f"共 {total} 个文件"
                toolbox_engine.run_tool(tool, list_file, dst, params, lambda: STATE.toolbox.get("cancel"))
                results.append({"name": "合并结果", "status": "ok", "detail": Path(dst).name})
            except toolbox_engine.MediaError as e:
                results.append({"name": "合并结果", "status": "fail", "detail": str(e)})
            finally:
                try:
                    os.unlink(list_file)
                except OSError:
                    pass
            STATE.toolbox["results"] = results
            STATE.toolbox["current"] = total
            STATE.toolbox["total"] = total
            STATE.toolbox["stage"] = "done" if not STATE.toolbox.get("cancel") else "cancelled"
            return
        for i, src in enumerate(files, 1):
            if STATE.toolbox.get("cancel"):
                STATE.toolbox["stage"] = "cancelled"
                break
            STATE.toolbox["current"] = i
            STATE.toolbox["current_file"] = Path(src).name
            try:
                if tool == "extract_frames":
                    sub = Path(src).stem
                    dst_dir = Path(out_dir) / sub
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    toolbox_engine.run_tool(tool, src, str(dst_dir), params, lambda: STATE.toolbox.get("cancel"))
                    results.append({"name": Path(src).name, "status": "ok", "detail": f"已输出 → {sub}/"})
                else:
                    suffix = toolbox_engine.suffix_for(tool, params, src)
                    dst = toolbox_engine._unique_dst(out_dir, Path(src).stem, suffix)
                    toolbox_engine.run_tool(tool, src, dst, params, lambda: STATE.toolbox.get("cancel"))
                    results.append({"name": Path(src).name, "status": "ok", "detail": Path(dst).name})
            except toolbox_engine.MediaError as e:
                results.append({"name": Path(src).name, "status": "fail", "detail": str(e)})
            except Exception as e:  # noqa: BLE001
                results.append({"name": Path(src).name, "status": "fail", "detail": f"未知错误：{e}"})
            STATE.toolbox["results"] = list(results)
        STATE.toolbox["total"] = total
        STATE.toolbox["current"] = min(STATE.toolbox.get("current") or 0, total)
        if STATE.toolbox.get("stage") != "cancelled":
            STATE.toolbox["stage"] = "done"
    except Exception as e:  # noqa: BLE001
        STATE.toolbox["error"] = str(e)
        STATE.toolbox["stage"] = "error"
    finally:
        STATE.toolbox["running"] = False


def _toolbox_sub_worker(target: str, style: str, out_dir: str,
                        sub_file: Optional[str]) -> None:
    """字幕包装后台线程：folder 批量（索引字幕）或 file 单文件（外部字幕/索引）。"""
    from toolbox import subtitle

    def progress(i: int, total: int, f: str, status: str) -> None:
        STATE.toolbox_sub["current"] = i
        STATE.toolbox_sub["total"] = total
        STATE.toolbox_sub["current_file"] = Path(f).name
        STATE.toolbox_sub["last_status"] = status

    STATE.toolbox_sub["last_status"] = "running"
    try:
        if sub_file:
            ext = Path(sub_file).suffix.lower()
            text = Path(sub_file).read_text(encoding="utf-8", errors="replace")
            segs = subtitle.parse_srt(text) if ext == ".srt" else subtitle.parse_ass(text)
            dst = subtitle.process_video(target, segs, style, out_dir,
                                         lambda: STATE.toolbox_sub.get("cancel"))
            STATE.toolbox_sub.update({"ok": 1, "skipped": 0, "failed": 0, "errors": [],
                                      "total": 1, "current": 1, "current_file": Path(target).name})
            STATE.toolbox_sub["stage"] = "done"
        else:
            res = subtitle.index_burn_videos(target, style, out_dir,
                                             lambda: STATE.toolbox_sub.get("cancel"), progress)
            STATE.toolbox_sub.update({
                "ok": res["ok"], "skipped": res["skipped"], "failed": res["failed"],
                "errors": res["errors"], "total": res["total"],
            })
            STATE.toolbox_sub["stage"] = "cancelled" if STATE.toolbox_sub.get("cancel") else "done"
    except subtitle.SubtitleError as e:
        STATE.toolbox_sub["error"] = str(e)
        STATE.toolbox_sub["stage"] = "error"
    except Exception as e:  # noqa: BLE001
        STATE.toolbox_sub["error"] = str(e)
        STATE.toolbox_sub["stage"] = "error"
    finally:
        STATE.toolbox_sub["running"] = False


def _toolbox_vad_worker(target: str, out_dir: str, sensitivity: float,
                        min_silence: float, keep_pad: float, single_file: bool) -> None:
    """剪气口后台线程：folder 批量 / file 单文件。"""
    from toolbox import vad_cut

    def progress(i: int, total: int, f: str, status: str) -> None:
        STATE.toolbox_vad["current"] = i
        STATE.toolbox_vad["total"] = total
        STATE.toolbox_vad["current_file"] = Path(f).name
        STATE.toolbox_vad["last_status"] = status

    STATE.toolbox_vad["last_status"] = "running"
    try:
        if single_file:
            dst = vad_cut.cut_pauses(target, out_dir, sensitivity, min_silence, keep_pad,
                                     lambda: STATE.toolbox_vad.get("cancel"))
            STATE.toolbox_vad.update({"ok": 1, "skipped": 0, "failed": 0, "errors": [],
                                      "total": 1, "current": 1, "current_file": Path(target).name})
            STATE.toolbox_vad["stage"] = "done"
        else:
            res = vad_cut.cut_batch(target, out_dir, sensitivity, min_silence, keep_pad,
                                    lambda: STATE.toolbox_vad.get("cancel"), progress)
            STATE.toolbox_vad.update({
                "ok": res["ok"], "skipped": res["skipped"], "failed": res["failed"],
                "errors": res["errors"], "total": res["total"],
            })
            STATE.toolbox_vad["stage"] = "cancelled" if STATE.toolbox_vad.get("cancel") else "done"
    except vad_cut.VadError as e:
        STATE.toolbox_vad["error"] = str(e)
        STATE.toolbox_vad["stage"] = "error"
    except Exception as e:  # noqa: BLE001
        STATE.toolbox_vad["error"] = str(e)
        STATE.toolbox_vad["stage"] = "error"
    finally:
        STATE.toolbox_vad["running"] = False


def _toolbox_asr_worker(folder: str, model: str) -> None:
    """语音识别索引后台线程：断点续建 + 增量，进度渐进更新 STATE.toolbox_asr。"""
    from toolbox import asr_index

    def progress(i: int, total: int, f: str, status: str) -> None:
        STATE.toolbox_asr["current"] = i
        STATE.toolbox_asr["total"] = total
        STATE.toolbox_asr["current_file"] = Path(f).name
        STATE.toolbox_asr["last_status"] = status

    STATE.toolbox_asr["last_status"] = "loading"
    try:
        res = asr_index.index_folder(folder, model,
                                     lambda: STATE.toolbox_asr.get("cancel"), progress)
        STATE.toolbox_asr.update({
            "done": res["done"], "skipped": res["skipped"], "failed": res["failed"],
            "errors": res["errors"], "total": res["total"],
        })
        STATE.toolbox_asr["stage"] = "cancelled" if STATE.toolbox_asr.get("cancel") else "done"
    except Exception as e:  # noqa: BLE001
        STATE.toolbox_asr["error"] = str(e)
        STATE.toolbox_asr["stage"] = "error"
    finally:
        STATE.toolbox_asr["running"] = False


def run_folder_dialog(description: str) -> str:
    script = f"""
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.WindowState = 'Minimized'
$owner.Show()
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = '{description}'
$d.ShowNewFolderButton = $true
$result = $d.ShowDialog($owner)
$owner.Close()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {{
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($d.SelectedPath)
    [Convert]::ToBase64String($bytes)
}}
"""
    return _run_ps_file(script)


def run_file_dialog(description: str, filter_spec: str = "图片文件|*.png;*.jpg;*.jpeg;*.webp") -> str:
    """选择单个文件（如水印图片），返回真实本地路径。"""
    script = f"""
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.WindowState = 'Minimized'
$owner.Show()
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Title = '{description}'
$d.Filter = '{filter_spec}'
$result = $d.ShowDialog($owner)
$owner.Close()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {{
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($d.FileName)
    [Convert]::ToBase64String($bytes)
}}
"""
    return _run_ps_file(script)


STATE = AppState()
SELECT_LOCK = threading.Lock()


def _safe_float(value, default: float, lo: float | None = None, hi: float | None = None) -> float:
    try:
        v = float(value)
    except Exception:
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _safe_int(value, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        v = int(value)
    except Exception:
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _safe_choice(value, allowed, default):
    """枚举白名单兜底：坏配置（GBK 写入的 '???' 等）回退默认，避免坏值进入生成流程。"""
    return value if value in allowed else default


def _run_dedupe_async(out_paths: list[str]) -> None:
    """后台查重：生成完成后静默比对产物相似度，完成后推送结果与日志。
    注意：线程内已在 STATE.lock 保护下直接操作 logs（不能再调 add_log 嵌套加锁，会死锁）。"""
    STATE.deduping = True
    STATE.dedup_progress = {"stage": "prepare", "done": 0, "total": len(out_paths or [])}

    def progress(done: int, total: int, stage: str) -> None:
        STATE.dedup_progress = {"stage": stage, "done": done, "total": total}

    try:
        pairs = find_similar_outputs(out_paths, progress=progress)
        with STATE.lock:
            if not STATE.deduping:
                return  # 期间启动了新任务（状态被复位），放弃旧批次查重结果
            STATE.similar_pairs = pairs
            STATE.logs.append(f"查重完成：发现 {len(pairs)} 对疑似重复输出" if pairs else "查重完成：未发现疑似重复")
            STATE.logs = STATE.logs[-1000:]
    except Exception as exc:  # noqa: BLE001
        with STATE.lock:
            if not STATE.deduping:
                return
            STATE.similar_pairs = []
            STATE.logs.append(f"产物查重失败：{exc}")
            STATE.logs = STATE.logs[-1000:]
    finally:
        STATE.deduping = False
        STATE.dedup_progress = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: ANN001
        return

    # ----------------------------------------------------------------
    # 基础工具
    # ----------------------------------------------------------------
    def _send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path, mime: str, as_attachment: str | None = None, cache_seconds: int | None = None) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(404, "not found")
            return
        size = path.stat().st_size
        # 支持 Range 请求（视频/音频 seek、断点续传）：无 Range 时整体返回
        start, end = 0, size - 1
        status = 200
        range_header = self.headers.get("Range", "")
        if range_header.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if m:
                s_raw, e_raw = m.group(1), m.group(2)
                if s_raw:
                    start = int(s_raw)
                if e_raw:
                    end = int(e_raw)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    return
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if as_attachment:
            ascii_name = as_attachment.encode("ascii", "ignore").decode() or "download"
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{ascii_name}"',
            )
        elif cache_seconds is not None:
            # 不可变资源（缩略图等）：长缓存，避免页面刷新重复请求
            self.send_header("Cache-Control", f"public, max-age={int(cache_seconds)}")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ----------------------------------------------------------------
    # GET 路由
    # ----------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route == "/":
            self._serve_file("index.html")
            return
        if route == "/style.css":
            self._serve_file("style.css", "text/css; charset=utf-8")
            return
        if route == "/app.js":
            self._serve_file("app.js", "application/javascript; charset=utf-8")
            return
        if route == "/options.js":
            self._serve_file("options.js", "application/javascript; charset=utf-8")
            return
        if route == "/util.js":
            self._serve_file("util.js", "application/javascript; charset=utf-8")
            return
        if route == "/api.js":
            self._serve_file("api.js", "application/javascript; charset=utf-8")
            return
        if route == "/vendor/vue.global.prod.js":
            self._serve_file("vendor/vue.global.prod.js", "application/javascript; charset=utf-8")
            return
        if route == "/api/platform_presets":
            self._send_json({"presets": get_presets()})
            return
        if route == "/api/options":
            from video_core import options_payload
            self._send_json(options_payload())
            return
        if route == "/api/platform_presets/apply":
            name = (query.get("name") or [""])[0]
            self._send_json(apply_preset(name))
            return
        if route == "/api/templates":
            self._send_json({"templates": list_templates()})
            return
        if route == "/api/history/clear":
            self._send_json({"ok": True, "removed": clear_history()})
            return
        if route == "/api/history":
            self._send_json({"history": list_history()})
            return
        if route == "/api/config/load":
            self._send_json(load_last_config())
            return
        if route == "/api/status":
            self._send_json(STATE.status_dict())
            return
        if route == "/api/toolbox/status":
            tb = dict(STATE.toolbox)
            tb["results"] = list(tb.get("results") or [])
            self._send_json(tb)
            return
        if route == "/api/toolbox/asr/status":
            from toolbox.asr_index import DEFAULT_MODEL
            from toolbox import models
            from toolbox import store
            st = dict(STATE.toolbox_asr)
            st["errors"] = list(st.get("errors") or [])
            st["index_stats"] = store.stats()
            st["models_cached"] = {m: models.whisper_cached(m) for m in ["tiny", "small", "large-v3"]}
            st["default_model"] = DEFAULT_MODEL
            st["download"] = models.download_state()
            self._send_json(st)
            return
        if route == "/api/toolbox/asr/stats":
            from toolbox import store
            self._send_json({"ok": True, **store.stats()})
            return
        if route == "/api/toolbox/sub/status":
            from toolbox.subtitle import STYLE_NAMES, DEFAULT_STYLE
            st = dict(STATE.toolbox_sub)
            st["errors"] = list(st.get("errors") or [])
            st["style_names"] = STYLE_NAMES
            st["default_style"] = DEFAULT_STYLE
            self._send_json(st)
            return
        if route == "/api/toolbox/vad/status":
            self._send_json(dict(STATE.toolbox_vad))
            return
        if route == "/api/ping":
            self._send_json({"ok": True})
            return
        if route == "/api/update":
            info = dict(STATE.update_info or {})  # 副本，避免污染缓存
            # 附带下载状态，前端一次拿到
            info["download"] = STATE.update_download
            self._send_json(info)
            return
        if route == "/api/similar":
            self._send_json({"ok": True, "pairs": STATE.similar_pairs})
            return
        if route == "/api/queue/list":
            self._send_json({"ok": True, "queue": STATE.queue, "running": STATE.running})
            return
        if route == "/api/debug":
            import video_engine
            self._send_json({
                "frozen": getattr(sys, "frozen", False),
                "executable": sys.executable,
                "meipass": getattr(sys, "_MEIPASS", None),
                "web_dir": str(WEB_DIR),
                "engine_file": video_engine.__file__,
                "app_root": str(video_engine._app_root()),
                "cache_dir": str(video_engine.cache_dir()),
                "ffmpeg": video_engine._ffmpeg(),
            })
            return
        if route == "/api/scan":
            head = (query.get("head") or [""])[0]
            tail = (query.get("tail") or [""])[0]
            middle = (query.get("middle") or [""])[0]
            bgm = (query.get("bgm") or [""])[0]
            head_files = _scan_cached(head, "video")
            tail_files = _scan_cached(tail, "video")
            middle_files = _scan_cached(middle, "video")
            bgm_files = _scan_cached(bgm, "audio")
            self._send_json(
                {
                    "head": [{"name": Path(p).name, "path": p, "fp": media_fingerprint(p)} for p in head_files],
                    "tail": [{"name": Path(p).name, "path": p, "fp": media_fingerprint(p)} for p in tail_files],
                    "middle": [{"name": Path(p).name, "path": p, "fp": media_fingerprint(p)} for p in middle_files],
                    "bgm": [{"name": Path(p).name, "path": p, "fp": media_fingerprint(p)} for p in bgm_files],
                }
            )
            return
        if route == "/api/material_detail":
            path = (query.get("path") or [""])[0]
            kind = (query.get("kind") or [""])[0]
            if not path or not os.path.isfile(path):
                self._send_json({"ok": False, "error": "文件不存在"}, 404)
                return
            # BGM 是纯音频文件（无视频流），按音频标准判定健康
            info = probe_media(path, require_video=(kind != "bgm"))
            self._send_json({"ok": True, "path": path, **info})
            return
        if route == "/api/thumb":
            path = (query.get("path") or [""])[0]
            thumb = get_thumbnail(path) if path else None
            if not thumb:
                self.send_error(404, "no thumbnail")
                return
            self._send_file(Path(thumb), "image/jpeg", cache_seconds=86400)
            return
        if route == "/api/select_folder":
            name = (query.get("name") or ["head"])[0]
            desc = {
                "head": "选择开头素材文件夹",
                "tail": "选择结尾素材文件夹",
                "middle": "选择中间素材文件夹",
                "output": "选择输出路径",
                "bgm": "选择音乐文件夹",
                "toolbox": "选择要处理的素材文件夹",
                "toolbox_out": "选择工具箱输出目录",
                "toolbox_sub_folder": "选择要烧录字幕的素材文件夹",
                "toolbox_sub_out": "选择字幕输出目录",
                "toolbox_vad_in": "选择要剪气口的素材文件夹",
                "toolbox_vad_out": "选择剪气口输出目录",
            }.get(name, "选择文件夹")
            if not SELECT_LOCK.acquire(blocking=False):
                self._send_json({"busy": True, "path": ""})
                return
            try:
                path = run_folder_dialog(desc)
                self._send_json({"path": path, "busy": False})
            finally:
                SELECT_LOCK.release()
            return
        if route == "/api/select_toolbox_file":
            kind = (query.get("kind") or ["video"])[0]
            if not SELECT_LOCK.acquire(blocking=False):
                self._send_json({"busy": True, "path": ""})
                return
            try:
                if kind == "sub":
                    path = run_file_dialog("选择字幕文件（SRT/ASS）", "字幕文件|*.srt;*.ass")
                else:
                    path = run_file_dialog("选择视频文件", "视频文件|*.mp4;*.mov;*.mkv;*.webm;*.avi;*.flv;*.m4v")
                if path:
                    register_allowed_dir(Path(path).resolve().parent)
                self._send_json({"path": path, "busy": False})
            finally:
                SELECT_LOCK.release()
            return
        if route == "/api/select_watermark":
            if not SELECT_LOCK.acquire(blocking=False):
                self._send_json({"busy": True, "path": ""})
                return
            try:
                path = run_file_dialog("选择水印图片")
                if path:
                    allowed = [Path(path).resolve()]
                    register_allowed_dir(allowed[0].parent)
                self._send_json({"path": path, "busy": False})
            finally:
                SELECT_LOCK.release()
            return
        if route == "/api/list_output":
            folder = (query.get("folder") or [""])[0]
            self._list_output(folder)
            return
        if route == "/api/open_folder":
            folder = (query.get("folder") or [""])[0]
            if not folder or not os.path.isdir(folder):
                self._send_json({"ok": False, "error": "输出目录不存在"}, 404)
                return
            try:
                os.startfile(folder)  # 资源管理器中打开，不弹黑窗
                self._send_json({"ok": True})
            except Exception as e:  # noqa: BLE001
                self._send_json({"ok": False, "error": str(e)})
        if route == "/api/download":
            folder = (query.get("folder") or [""])[0]
            name = (query.get("name") or [""])[0]
            self._download_file(folder, name)
            return
        self._send_json({"error": "not found"}, 404)

    # ----------------------------------------------------------------
    # POST 路由
    # ----------------------------------------------------------------
    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        if route == "/api/update/download":
            info = STATE.update_info or {}
            url = info.get("download_url")
            if STATE.running:
                self._send_json({"ok": False, "error": "任务运行中，请生成结束后再更新"})
                return
            if STATE.update_download and STATE.update_download.get("stage") in ("downloading", "ready"):
                self._send_json({"ok": False, "error": "更新已在下载/已就绪"})
                return
            if not url:
                self._send_json({"ok": False, "error": "暂无可用更新包（请到 GitHub Releases 手动下载）"})
                return
            _download_update_async(url)
            self._send_json({"ok": True})
            return
        if route == "/api/templates/save":
            payload = self._read_json()
            name = str(payload.get("name", "")).strip()
            if not name:
                self._send_json({"ok": False, "error": "模板名称不能为空"}, 400)
                return
            try:
                path = save_template(name, payload.get("config", {}))
                self._send_json({"ok": True, "path": str(path)})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        if route == "/api/templates/load":
            payload = self._read_json()
            name = str(payload.get("name", "")).strip()
            self._send_json(load_template(name))
            return
        if route == "/api/templates/delete":
            payload = self._read_json()
            name = str(payload.get("name", "")).strip()
            self._send_json({"ok": delete_template(name)})
            return
        if route == "/api/config/save":
            payload = self._read_json()
            path = save_last_config(payload)
            self._send_json({"ok": True, "path": str(path)})
            return
        if route == "/api/precheck":
            payload = self._read_json()
            config = self._make_config(payload)
            if isinstance(config, str):
                self._send_json({"ok": False, "error": config}, 400)
                return
            report = precheck_materials(config)
            bad = [
                {"kind": kind, "name": item["name"], "error": item["error"]}
                for kind, items in report.items()
                for item in items
                if not item["ok"]
            ]
            self._send_json({"ok": True, "report": report, "bad": bad})
            return
        if route == "/api/health_check":
            payload = self._read_json()
            config = self._make_config(payload)
            if isinstance(config, str):
                # 未选择素材/输出路径：逐项列出错误（体检面板可见），而非整体 400
                miss: list[dict] = []
                if not str(payload.get("head_folder") or "").strip():
                    miss.append({"level": "error", "scope": "素材", "msg": "未选择开头素材路径"})
                if not str(payload.get("tail_folder") or "").strip():
                    miss.append({"level": "error", "scope": "素材", "msg": "未选择结尾素材路径"})
                if not str(payload.get("output_folder") or "").strip():
                    miss.append({"level": "error", "scope": "输出目录", "msg": "未选择输出目录"})
                if miss:
                    self._send_json({"ok": False, "items": miss, "errors": miss, "warns": [], "report": {}, "eta_seconds": None, "actual_count": 0, "max_combos": 0})
                    return
                self._send_json({"ok": False, "error": config}, 400)
                return
            result = self._health_check(config)
            self._send_json({"ok": True, **result})
            return
        if route == "/api/zip":
            payload = self._read_json()
            folder = str(payload.get("folder", "")).strip()
            self._zip_output(folder)
            return
        if route == "/api/preview":
            self._preview()
            return
        if route == "/api/start":
            self._start()
            return
        if route == "/api/retry_failed":
            self._retry_failed()
            return
        if route == "/api/resume":
            self._resume()
            return
        if route == "/api/discard_interrupted":
            self._discard_interrupted()
            return
        if route == "/api/cancel":
            STATE.cancel_event.set()
            STATE.add_log("已请求取消")
            self._send_json({"ok": True})
            return
        if route == "/api/toolbox/run":
            self._toolbox_run()
            return
        if route == "/api/toolbox/cancel":
            STATE.toolbox["cancel"] = True
            self._send_json({"ok": True})
            return
        if route == "/api/toolbox/asr/run":
            self._toolbox_asr_run()
            return
        if route == "/api/toolbox/asr/cancel":
            STATE.toolbox_asr["cancel"] = True
            self._send_json({"ok": True})
            return
        if route == "/api/toolbox/asr/clear":
            from toolbox import store
            n = store.clear_index()
            self._send_json({"ok": True, "removed": n})
            return
        if route == "/api/toolbox/sub/run":
            self._toolbox_sub_run()
            return
        if route == "/api/toolbox/sub/run_file":
            self._toolbox_sub_run_file()
            return
        if route == "/api/toolbox/sub/cancel":
            STATE.toolbox_sub["cancel"] = True
            self._send_json({"ok": True})
            return
        if route == "/api/toolbox/vad/status":
            self._send_json(dict(STATE.toolbox_vad))
            return
        if route == "/api/toolbox/vad/run":
            self._toolbox_vad_run()
            return
        if route == "/api/toolbox/vad/run_file":
            self._toolbox_vad_run_file()
            return
        if route == "/api/toolbox/vad/cancel":
            STATE.toolbox_vad["cancel"] = True
            self._send_json({"ok": True})
            return
        if route == "/api/toolbox/clear_results":
            STATE.toolbox["results"] = []
            STATE.toolbox["stage"] = "idle"
            self._send_json({"ok": True})
            return
        if route == "/api/shutdown":
            # 便携版"退出程序"：先返回响应，再在独立线程中关闭服务（shutdown 需在 serve_forever 线程外调用）
            self._send_json({"ok": True, "msg": "程序已退出，可关闭本页面"})
            threading.Timer(0.8, self.server.shutdown).start()
            return
        if route == "/api/queue/add":
            self._queue_add()
            return
        if route == "/api/queue/remove":
            self._queue_remove()
        if route == "/api/queue/reorder":
            self._queue_reorder()
            return
        if route == "/api/queue/clear":
            STATE.queue = []
            self._send_json({"ok": True})
            return
        if route == "/api/pause":
            self._toggle_pause()
            self._send_json({"paused": STATE.paused})
            return
        self._send_json({"error": "not found"}, 404)

    # ----------------------------------------------------------------
    # 工具箱：媒体工具（docs/工具箱设计方案.md 第 7 节）
    # ----------------------------------------------------------------
    def _toolbox_run(self) -> None:
        if STATE.toolbox.get("running"):
            self._send_json({"ok": False, "error": "已有工具箱任务正在运行"}, 409)
            return
        payload = self._read_json()
        tool = str(payload.get("tool") or "").strip()
        folder = str(payload.get("folder") or "").strip()
        out_dir = str(payload.get("out_dir") or "").strip()
        params = payload.get("params") or {}
        kind = str(payload.get("kind") or "video")

        if tool not in TOOL_NAMES:
            self._send_json({"ok": False, "error": f"未知工具：{tool}"}, 400)
            return
        if not folder or not os.path.isdir(folder):
            self._send_json({"ok": False, "error": "请先选择有效的输入文件夹"}, 400)
            return
        if not out_dir:
            self._send_json({"ok": False, "error": "请选择输出目录"}, 400)
            return
        try:
            files = toolbox_engine.collect_files(folder, kind)
        except toolbox_engine.MediaError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return
        if tool == "concat" and len(files) < 2:
            self._send_json({"ok": False, "error": "合并至少需要 2 个视频文件"}, 400)
            return
        if not files:
            self._send_json({"ok": False, "error": "所选文件夹中没有可处理的文件"}, 400)
            return

        STATE.toolbox.update({
            "running": True, "stage": "running", "tool": tool,
            "current": 0, "total": len(files), "current_file": "",
            "results": [], "cancel": False, "out_dir": out_dir, "error": None,
        })
        threading.Thread(target=_toolbox_worker, args=(tool, files, params, out_dir), daemon=True).start()
        self._send_json({"ok": True, "total": len(files)})

    # ----------------------------------------------------------------
    # 工具箱：语音识别与索引（docs/工具箱设计方案.md 第 3 节）
    # ----------------------------------------------------------------
    def _toolbox_asr_run(self) -> None:
        if STATE.toolbox_asr.get("running"):
            self._send_json({"ok": False, "error": "已有索引任务正在运行"}, 409)
            return
        payload = self._read_json()
        folder = str(payload.get("folder") or "").strip()
        model = str(payload.get("model") or "small").strip()
        if not folder or not os.path.isdir(folder):
            self._send_json({"ok": False, "error": "请先选择有效的素材文件夹"}, 400)
            return
        from toolbox.asr_index import WHISPER_SIZES
        if model not in WHISPER_SIZES:
            model = "small"
        STATE.toolbox_asr.update({
            "running": True, "stage": "running", "folder": folder, "model": model,
            "current": 0, "total": 0, "current_file": "", "done": 0,
            "skipped": 0, "failed": 0, "errors": [], "cancel": False, "error": None,
        })
        threading.Thread(target=_toolbox_asr_worker, args=(folder, model), daemon=True).start()
        self._send_json({"ok": True, "model": model})

    # ----------------------------------------------------------------
    # 工具箱：字幕包装（docs/工具箱设计方案.md 第 5 节）
    # ----------------------------------------------------------------
    def _toolbox_sub_run(self) -> None:
        """批量模式：文件夹内视频用①索引字幕烧录。"""
        if STATE.toolbox_sub.get("running"):
            self._send_json({"ok": False, "error": "已有字幕任务正在运行"}, 409)
            return
        payload = self._read_json()
        folder = str(payload.get("folder") or "").strip()
        style = str(payload.get("style") or "minimal").strip()
        if not folder or not os.path.isdir(folder):
            self._send_json({"ok": False, "error": "请先选择有效的素材文件夹"}, 400)
            return
        from toolbox.subtitle import STYLE_NAMES
        if style not in STYLE_NAMES:
            style = "minimal"
        import video_engine
        out_dir = str(payload.get("out_dir") or "").strip() or os.path.join(video_engine._app_root(), "toolbox_export", "字幕包装")
        STATE.toolbox_sub.update({
            "running": True, "stage": "running", "mode": "folder", "style": style,
            "current": 0, "total": 0, "current_file": "", "ok": 0, "skipped": 0,
            "failed": 0, "errors": [], "cancel": False, "out_dir": out_dir, "error": None,
        })
        threading.Thread(target=_toolbox_sub_worker, args=(folder, style, out_dir, None), daemon=True).start()
        self._send_json({"ok": True, "out_dir": out_dir})

    def _toolbox_sub_run_file(self) -> None:
        """单文件模式：视频 + 外部 SRT/ASS（无字幕文件则用①索引）。"""
        if STATE.toolbox_sub.get("running"):
            self._send_json({"ok": False, "error": "已有字幕任务正在运行"}, 409)
            return
        payload = self._read_json()
        video = str(payload.get("video") or "").strip()
        sub_file = str(payload.get("sub_file") or "").strip()
        style = str(payload.get("style") or "minimal").strip()
        if not video or not os.path.isfile(video):
            self._send_json({"ok": False, "error": "请选择有效的视频文件"}, 400)
            return
        if sub_file and not os.path.isfile(sub_file):
            self._send_json({"ok": False, "error": "字幕文件不存在"}, 400)
            return
        from toolbox.subtitle import STYLE_NAMES
        if style not in STYLE_NAMES:
            style = "minimal"
        import video_engine
        out_dir = str(payload.get("out_dir") or "").strip() or os.path.join(video_engine._app_root(), "toolbox_export", "字幕包装")
        STATE.toolbox_sub.update({
            "running": True, "stage": "running", "mode": "file", "style": style,
            "current": 0, "total": 1, "current_file": Path(video).name, "ok": 0, "skipped": 0,
            "failed": 0, "errors": [], "cancel": False, "out_dir": out_dir, "error": None,
        })
        threading.Thread(target=_toolbox_sub_worker,
                         args=(video, style, out_dir, sub_file or None), daemon=True).start()
        self._send_json({"ok": True, "out_dir": out_dir})

    def _toolbox_vad_run(self) -> None:
        """批量模式：文件夹内视频剪气口。"""
        if STATE.toolbox_vad.get("running"):
            self._send_json({"ok": False, "error": "已有剪气口任务正在运行"}, 409)
            return
        payload = self._read_json()
        folder = str(payload.get("folder") or "").strip()
        if not folder or not os.path.isdir(folder):
            self._send_json({"ok": False, "error": "请先选择有效的素材文件夹"}, 400)
            return
        sensitivity = float(payload.get("sensitivity") or 0.5)
        min_silence = float(payload.get("min_silence") or 0.6)
        keep_pad = float(payload.get("keep_pad") or 0.3)
        import video_engine
        out_dir = str(payload.get("out_dir") or "").strip() or os.path.join(video_engine._app_root(), "toolbox_export", "剪气口")
        STATE.toolbox_vad.update({
            "running": True, "stage": "running", "mode": "folder",
            "sensitivity": sensitivity, "min_silence": min_silence, "keep_pad": keep_pad,
            "current": 0, "total": 0, "current_file": "", "ok": 0, "skipped": 0,
            "failed": 0, "errors": [], "cancel": False, "out_dir": out_dir, "error": None,
        })
        threading.Thread(target=_toolbox_vad_worker,
                         args=(folder, out_dir, sensitivity, min_silence, keep_pad, False), daemon=True).start()
        self._send_json({"ok": True, "out_dir": out_dir})

    def _toolbox_vad_run_file(self) -> None:
        """单文件模式：单个视频剪气口。"""
        if STATE.toolbox_vad.get("running"):
            self._send_json({"ok": False, "error": "已有剪气口任务正在运行"}, 409)
            return
        payload = self._read_json()
        video = str(payload.get("video") or "").strip()
        if not video or not os.path.isfile(video):
            self._send_json({"ok": False, "error": "请选择有效的视频文件"}, 400)
            return
        sensitivity = float(payload.get("sensitivity") or 0.5)
        min_silence = float(payload.get("min_silence") or 0.6)
        keep_pad = float(payload.get("keep_pad") or 0.3)
        import video_engine
        out_dir = str(payload.get("out_dir") or "").strip() or os.path.join(video_engine._app_root(), "toolbox_export", "剪气口")
        STATE.toolbox_vad.update({
            "running": True, "stage": "running", "mode": "file",
            "sensitivity": sensitivity, "min_silence": min_silence, "keep_pad": keep_pad,
            "current": 0, "total": 1, "current_file": Path(video).name, "ok": 0, "skipped": 0,
            "failed": 0, "errors": [], "cancel": False, "out_dir": out_dir, "error": None,
        })
        threading.Thread(target=_toolbox_vad_worker,
                         args=(video, out_dir, sensitivity, min_silence, keep_pad, True), daemon=True).start()
        self._send_json({"ok": True, "out_dir": out_dir})

    # ----------------------------------------------------------------
    # 素材与文件
    # ----------------------------------------------------------------
    def _list_output(self, folder: str) -> None:
        path = Path(folder)
        if not path.exists() or not path.is_dir():
            self._send_json({"files": []})
            return
        files = sorted(
            [p for p in path.iterdir() if p.is_file() and p.suffix.lower() == ".mp4"],
            key=lambda p: p.name,
        )
        encoded = quote(str(path))
        self._send_json(
            {
                "files": [
                    {
                        "name": p.name,
                        "size": p.stat().st_size,
                        "url": f"/api/download?folder={encoded}&name={quote(p.name)}",
                    }
                    for p in files
                ]
            }
        )

    def _download_file(self, folder: str, name: str) -> None:
        path = Path(folder) / Path(name).name
        if not is_allowed_dir(folder):
            self.send_error(403, "forbidden")
            return
        self._send_file(path, "video/mp4", as_attachment=Path(name).name)

    def _zip_output(self, folder: str) -> None:
        if not folder or not is_allowed_dir(folder):
            self._send_json({"ok": False, "error": "输出目录未登记或不存在"}, 403)
            return
        src = Path(folder)
        if not src.exists() or not src.is_dir():
            self._send_json({"ok": False, "error": "输出目录不存在"}, 404)
            return
        tmp = Path(tempfile_dir())
        tmp.mkdir(parents=True, exist_ok=True)
        zip_path = tmp / f"output_{time.strftime('%Y%m%d_%H%M%S')}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
            for p in sorted(src.rglob("*.mp4")):
                if p.is_file():
                    zf.write(p, p.relative_to(src).as_posix())
        self._send_file(zip_path, "application/zip", as_attachment=zip_path.name)

    def _serve_file(self, filename: str, mime: str = "text/html; charset=utf-8") -> None:
        path = WEB_DIR / filename
        if not path.exists():
            self.send_error(404, "not found")
            return
        self._send_file(path, mime)

    # ----------------------------------------------------------------
    # 任务控制
    # ----------------------------------------------------------------
    def _make_config(self, payload: dict) -> JobConfig | str:
        head_folder = str(payload.get("head_folder", "")).strip()
        tail_folder = str(payload.get("tail_folder", "")).strip()
        output_folder = str(payload.get("output_folder", "")).strip()
        if not head_folder or not tail_folder:
            return "请填写开头文件夹和结尾文件夹"
        if not os.path.isdir(head_folder) or not os.path.isdir(tail_folder):
            return "素材文件夹不存在，请检查路径"
        if not output_folder:
            return "请选择输出路径"
        count = _safe_int(payload.get("count", 10), 10, 1, 200)

        use_watermark = bool(payload.get("use_watermark", False))
        raw_sub = payload.get("use_subtitle", "")
        if raw_sub is True:
            use_subtitle = "minimal"      # 旧版 bool 配置 → 保持白字样式
        elif raw_sub is False or raw_sub is None:
            use_subtitle = ""
        else:
            use_subtitle = str(raw_sub).strip()
        if use_subtitle and not subtitle_plugin.available():
            return "自动字幕需要 faster-whisper，请先运行：pip install faster-whisper"
        watermark_path = str(payload.get("watermark_path", "")).strip()
        if use_watermark and not watermark_path:
            return "已勾选水印，请填写水印图片路径"

        bgm_mode = _safe_choice(str(payload.get("bgm_mode", "不使用")), {"不使用", "本地导入", "音乐文件夹固定", "音乐文件夹随机"}, "不使用")
        bgm_path = str(payload.get("bgm_path", "")).strip()
        bgm_folder = str(payload.get("bgm_folder", "")).strip()
        fixed_bgm = str(payload.get("fixed_bgm") or "").strip() or None
        if bgm_mode == "本地导入" and not bgm_path:
            return "已选择本地导入 BGM，请填写音乐文件路径"
        if bgm_mode in {"音乐文件夹固定", "音乐文件夹随机"} and not bgm_folder:
            return "请选择音乐文件夹"
        if bgm_mode == "音乐文件夹固定" and not fixed_bgm:
            return "请选择固定 BGM"

        middle_folder = str(payload.get("middle_folder", "")).strip()
        fixed_middle = str(payload.get("fixed_middle") or "").strip() or None
        middle_items = [str(x).strip() for x in payload.get("middle_items", []) if str(x).strip()]
        middle_items = middle_items[:10]
        middle_count_raw = payload.get("middle_count")
        middle_count = None if middle_count_raw is None else _safe_int(middle_count_raw, 1, 0, 10)

        # 多中间素材池（用户可添加多个池，按池顺序插入头尾之间）
        middle_pools_raw = payload.get("middle_pools")
        middle_pools: list[dict] = []
        if isinstance(middle_pools_raw, list):
            for pool in middle_pools_raw[:5]:
                pool_folder = str(pool.get("folder", "")).strip()
                if not pool_folder:
                    continue
                if not os.path.isdir(pool_folder):
                    return f"中间素材池文件夹不存在：{pool_folder}"
                pool_items = [str(x).strip() for x in pool.get("items", []) if str(x).strip()][:10]
                pool_count_raw = pool.get("count")
                pool_count = None if pool_count_raw is None else _safe_int(pool_count_raw, 1, 0, 10)
                middle_pools.append({"folder": pool_folder, "items": pool_items, "count": pool_count})
        if middle_pools:
            # 多池模式下，旧字段取第一个池的值（保持兼容），引擎优先使用 middle_pools
            middle_folder = middle_pools[0]["folder"]
            middle_items = middle_pools[0]["items"]
            middle_count = middle_pools[0]["count"]
            fixed_middle = None
        else:
            if fixed_middle and not middle_items:
                middle_items = [fixed_middle]  # 兼容旧配置
            if middle_items and not middle_folder:
                return "已勾选固定中间素材，请先填写中间素材文件夹"
            if middle_count and not middle_folder:
                return "已设置随机中间片段，请先填写中间素材文件夹"

        return JobConfig(
            head_folder=head_folder,
            tail_folder=tail_folder,
            fixed_head=str(payload.get("fixed_head") or "").strip() or None,
            fixed_tail=str(payload.get("fixed_tail") or "").strip() or None,
            output_folder=output_folder,
            count=count,
            resolution=str(payload.get("resolution", "1080x1920")),
            duration_mode=_safe_choice(str(payload.get("duration_mode", "不限制")), {"不限制", "15s", "25s", "30s"}, "不限制"),
            use_watermark=use_watermark,
            watermark_path=watermark_path,
            use_transition=bool(payload.get("use_transition", False)),
            transition_mode=_safe_choice(str(payload.get("transition_mode", "不使用")), {"不使用", "固定", "随机"}, "不使用"),
            transition_type=str(payload.get("transition_type", "fade")),
            transition_duration=_safe_float(payload.get("transition_duration", 0.5), 0.5, 0.1, 2.0),
            transition_types=[str(x) for x in payload.get("transition_types", [])],
            bgm_mode=bgm_mode,
            bgm_path=bgm_path,
            bgm_volume=_safe_float(payload.get("bgm_volume", 0.2), 0.2, 0.0, 2.0),
            audio_volume=_safe_float(payload.get("audio_volume", 1.0), 1.0, 0.0, 2.0),
            material_volumes=dict(payload.get("material_volumes") or {}),
            bgm_folder=bgm_folder,
            fixed_bgm=fixed_bgm,
            normalize_audio=bool(payload.get("normalize_audio", False)),
            dedupe_level=_safe_choice(str(payload.get("dedupe_level") or "off"), {"off", "light", "deep"}, "off"),
            dedupe_options=dict(payload.get("dedupe_options") or {"visual": True, "segment": True, "audio": True}),
            dedupe_versions=max(1, min(5, int(payload.get("dedupe_versions") or 1))),
            bgm_fade=bool(payload.get("bgm_fade", False)),
            bgm_ducking=bool(payload.get("bgm_ducking", False)),
            fit_mode=_safe_choice(str(payload.get("fit_mode", "fit")), {"fit", "blur", "crop"}, "fit"),
            output_name_template=str(payload.get("output_name_template", "output_{序号}_{开头}_{结尾}")),
            random_seed=_safe_int(payload.get("random_seed", 20260905), 20260905),
            dedupe_enabled=bool(payload.get("dedupe_enabled", True)),
            middle_folder=middle_folder,
            fixed_middle=fixed_middle,
            middle_items=middle_items,
            middle_count=middle_count,
            middle_pools=middle_pools,
            use_subtitle=use_subtitle,
            watermark_mode=_safe_choice(str(payload.get("watermark_mode", "铺满全屏")), {"铺满全屏", "角落水印"}, "铺满全屏"),
            watermark_position=_safe_choice(str(payload.get("watermark_position", "右下角")), {"右下角", "右上角", "左下角", "左上角"}, "右下角"),
            watermark_scale=_safe_float(payload.get("watermark_scale", 0.15), 0.15, 0.05, 0.6),
            watermark_opacity=_safe_float(payload.get("watermark_opacity", 0.6), 0.6, 0.05, 1.0),
            workers=_safe_int(payload.get("workers", 2), 2, 1, 8),
            encode_accel=_safe_choice(str(payload.get("encode_accel") or "auto"), {"auto", "nvenc", "cpu"}, "auto"),
        )

    def _run_task(self, config: JobConfig, label: str, mode: str, failed_items: list[dict] | None = None,
                  skip_existing: bool = True, record_last: bool = True, record_history: bool = True) -> None:
        STATE.cancel_event.clear()
        STATE.pause_event.clear()
        # 预览为临时任务：不覆盖 last_config（重试/断点续跑/刷新恢复仍用上次正式任务的配置），避免"本次生成参数"被预览污染
        if record_last:
            STATE.last_config = config
        STATE.last_failed_items = []
        STATE.result = None
        STATE.error = None
        STATE.logs = []
        STATE.paused = False
        STATE.running = True
        STATE.interrupted = None
        _clear_snapshot()  # 新任务开始，放弃旧的中断快照
        # 注意：总数/日志在 run_one_batch 内统一计算（与实际生成条数一致），这里不再重复 begin/log

        def log(message: str) -> None:
            STATE.add_log(message)

        def progress(current: int, total_: int) -> None:
            STATE.set_progress(current, total_)

        def run_one_batch(cfg: JobConfig, lbl: str, m: str, f_items: list[dict]) -> bool:
            """执行一批任务，返回是否继续处理队列（False 表示被取消/异常终止）。"""
            # 日志/进度总数与实际生成条数保持一致（与 build_combinations 输出一致）
            if m == "retry":
                total = len(f_items or [])
            else:
                total = self._actual_total(cfg)
            STATE.begin(total)
            STATE.add_log(f"{lbl}开始，共 {total} 条")
            if m == "batch":
                _save_snapshot(cfg, lbl, total)
            try:
                if m == "retry":
                    result = process_failed_items(
                        cfg, f_items or [], STATE.cancel_event, STATE.pause_event,
                        log=log, progress=progress,
                    )
                else:
                    result = process_batch(
                        cfg, STATE.cancel_event, STATE.pause_event, log=log, progress=progress,
                        skip_existing=skip_existing,
                    )
                STATE.result = result
                STATE.last_failed_items = list(result.failed_items)
                # 产物感知哈希查重：改为后台异步，生成完成立即返回，查重结果稍后推送到页面
                STATE.similar_pairs = []
                if result.success and not result.cancelled:
                    out_paths = [it.get("output") for it in result.success_items if it.get("output")]
                    if out_paths:
                        threading.Thread(target=_run_dedupe_async, args=(out_paths,), daemon=True).start()
                record = {
                    "type": m,
                    "config": asdict(cfg),
                    "success": result.success,
                    "skipped": result.skipped,
                    "failed": result.failed,
                    "cancelled": result.cancelled,
                    "elapsed_sec": round(max(0, time.time() - (STATE.started_at or time.time())), 1),
                }
                save_history(record) if record_history else None
                # 记录最近任务实测速度（条/秒，含并发），供下次预检估算生成时间
                if result.success and STATE.started_at:
                    elapsed = time.time() - STATE.started_at
                    if elapsed > 1:
                        STATE.last_speed = result.success / elapsed
                return not result.cancelled
            except MediaError as exc:
                STATE.error = str(exc)
                STATE.add_log(str(exc))
                return False
            except Exception as exc:
                STATE.error = f"程序异常：{exc}"
                STATE.add_log(STATE.error)
                return False
            finally:
                STATE.end()
                _clear_snapshot()

        def worker() -> None:
            cur_cfg, cur_label, cur_mode, cur_failed = config, label, mode, failed_items
            try:
                keep_going = True
                while keep_going:
                    keep_going = run_one_batch(cur_cfg, cur_label, cur_mode, cur_failed)
                    if not keep_going or not STATE.queue:
                        break
                    if STATE.cancel_event.is_set():
                        break
                    nxt = STATE.queue.pop(0)
                    raw = nxt["payload"]
                    nxt_cfg = self._make_config(raw)
                    if isinstance(nxt_cfg, str):
                        STATE.add_log(f"队列：跳过「{nxt['label']}」({nxt_cfg})")
                        continue
                    cur_cfg, cur_label, cur_mode, cur_failed = nxt_cfg, nxt["label"], "batch", []
                    STATE.add_log(f"队列：开始下一批「{cur_label}」")
            finally:
                STATE.running = False

        STATE.worker = threading.Thread(target=worker, daemon=True)
        STATE.worker.start()

    def _start(self) -> None:
        if STATE.running:
            self._send_json({"ok": False, "error": "任务正在运行"})
            return
        payload = self._read_json()
        config = self._make_config(payload)
        if isinstance(config, str):
            self._send_json({"ok": False, "error": config})
            return
        STATE.similar_pairs = []  # 新任务开始，清除上次任务的疑似重复提示
        STATE.deduping = False    # 复位查重状态（旧查重线程发现被复位会放弃写入）
        register_allowed_dir(config.output_folder)
        total = self._actual_total(config)
        self._run_task(config, "任务", "batch")
        self._send_json({"ok": True, "total": total})

    def _resume(self) -> None:
        if STATE.running:
            self._send_json({"ok": False, "error": "任务正在运行"})
            return
        if not STATE.interrupted or not STATE.last_config:
            self._send_json({"ok": False, "error": "没有可继续的任务"})
            return
        config = STATE.last_config
        register_allowed_dir(config.output_folder)
        self._run_task(config, "继续任务", "batch")  # skip_existing 默认开启，已完成输出自动跳过
        self._send_json({"ok": True})

    def _discard_interrupted(self) -> None:
        _clear_snapshot()
        STATE.interrupted = None
        self._send_json({"ok": True})

    def _queue_add(self) -> None:
        """加入队列：保存配置快照，当前任务完成后自动执行。"""
        payload = self._read_json()
        config = self._make_config(payload)
        if isinstance(config, str):
            self._send_json({"ok": False, "error": config})
            return
        label = str(payload.get("label") or f"批次 {STATE.queue_seq + 1}").strip()
        STATE.queue_seq += 1
        STATE.queue.append({"id": STATE.queue_seq, "label": label, "payload": payload})
        STATE.add_log(f"已加入队列（{len(STATE.queue)} 项等待）：{label}")
        self._send_json({"ok": True, "queue_len": len(STATE.queue)})

    def _queue_remove(self) -> None:
        payload = self._read_json()
        qid = payload.get("id")
        STATE.queue = [q for q in STATE.queue if q.get("id") != qid]
        self._send_json({"ok": True})

    def _queue_reorder(self) -> None:
        """按前端拖拽结果重排队列：ids 为新的顺序（id 列表）。"""
        payload = self._read_json()
        ids = payload.get("ids") or []
        by_id = {q.get("id"): q for q in STATE.queue}
        ordered = []
        for qid in ids:
            if qid in by_id:
                ordered.append(by_id.pop(qid))
        ordered.extend(by_id.values())  # 容错：未在 ids 中的排后面
        STATE.queue = ordered
        self._send_json({"ok": True, "queue_len": len(STATE.queue)})

    # ----------------------------------------------------------------
    # 开始前全局体检（素材健康 / 输出目录 / 磁盘空间 / 组合数 / FFmpeg）
    # ----------------------------------------------------------------
    def _health_check(self, config: JobConfig) -> dict:
        items: list[dict] = []

        def add(level: str, scope: str, msg: str) -> None:
            items.append({"level": level, "scope": scope, "msg": msg})

        # 1 素材健康
        report = precheck_materials(config)
        bad = [
            {"kind": k, "name": it["name"], "error": it["error"]}
            for k, its in report.items()
            for it in its
            if not it["ok"]
        ]
        if bad:
            names = "、".join(b["name"] for b in bad[:3])
            add("error", "素材", f"{len(bad)} 个素材无法读取：{names}{'…' if len(bad) > 3 else ''}")
        else:
            add("ok", "素材", "全部素材可正常读取")

        # 2 输出目录
        if not (config.output_folder or "").strip():
            add("error", "输出目录", "未选择输出目录")
        else:
            out = Path(config.output_folder)
            if not out.exists():
                add("warn", "输出目录", "输出目录不存在，生成时自动创建")
            elif not os.access(out, os.W_OK):
                add("error", "输出目录", "输出目录不可写，请更换位置")
            else:
                add("ok", "输出目录", "输出目录可写")

        # 3 磁盘空间（粗估输出体积 vs 剩余空间）
        try:
            drive = os.path.splitdrive(str(out))[0] + os.sep
            usage = shutil.disk_usage(drive if os.path.isdir(drive) else ".")
            need = self._estimate_output_bytes(config, report)
            free = usage.free
            if need > 0:
                if free < need:
                    add("error", "磁盘空间", f"剩余 {_fmt_size(free)}，预估输出 {_fmt_size(need)}，空间不足")
                elif free < need * 2:
                    add("warn", "磁盘空间", f"剩余 {_fmt_size(free)}，预估输出 {_fmt_size(need)}，建议先清理空间")
                else:
                    add("ok", "磁盘空间", f"剩余 {_fmt_size(free)}，预估输出 {_fmt_size(need)}")
        except Exception:
            pass

        # 4 组合数提示（提示与实际生成条数完全一致；固定头/尾时组合不足不看去重开关都会少出）
        try:
            combos = self._count_combos(config)
            versions = max(1, int(getattr(config, "dedupe_versions", 1) or 1))
            total = config.count * versions
            actual = self._actual_total(config)
            dedupe_on = bool(getattr(config, "dedupe_enabled", True))
            fixed_one = bool(config.fixed_head or config.fixed_tail)
            if config.count > combos and (dedupe_on or fixed_one):
                if fixed_one:
                    add("warn", "生成数量", f"请求 {config.count} 条 × {versions} 版 = 共 {total} 条，固定素材后仅 {combos} 种不同组合，实际将生成 {actual} 条（不重复出片，避免平台判重）")
                else:
                    add("warn", "生成数量", f"请求 {config.count} 条 × {versions} 版 = 共 {total} 条，素材最多 {combos} 种不同组合，实际将生成 {actual} 条（不重复出片，避免平台判重）；关闭去重可凑满 {total} 条（可能重复）")
            else:
                add("ok", "生成数量", f"{config.count} 条 × {versions} 版 = 共 {total} 条，实际将生成 {actual} 条")
        except Exception:
            pass

        # 5 FFmpeg 可用性
        if not self._ffmpeg_ok():
            add("error", "引擎", "FFmpeg 不可用，无法生成")
        else:
            add("ok", "引擎", "FFmpeg 可用")

        # 5.5 固定素材提示：各库当前是固定还是随机
        try:
            fixed_msgs: list[str] = []
            fixed_msgs.append(f"开头：{'已固定 ' + Path(config.fixed_head).name if config.fixed_head else '未固定（随机抽取）'}")
            fixed_msgs.append(f"结尾：{'已固定 ' + Path(config.fixed_tail).name if config.fixed_tail else '未固定（随机抽取）'}")
            pools = getattr(config, "middle_pools", None) or []
            if pools:
                for pi, p in enumerate(pools[:5], start=1):
                    its = p.get("items") or []
                    if its:
                        names = "、".join(Path(x).name for x in its[:3])
                        fixed_msgs.append(f"池{pi}：已固定 {names}{'…' if len(its) > 3 else ''}")
                    else:
                        fixed_msgs.append(f"池{pi}：未勾选（随机 {int(p.get('count', 1) or 1)} 条）")
            else:
                fixed_msgs.append("中间：未选择素材池")
            bgm_mode = getattr(config, "bgm_mode", "") or ""
            if bgm_mode in ("", "不使用"):
                fixed_msgs.append("背景音乐：未使用")
            elif bgm_mode == "音乐文件夹固定" and getattr(config, "fixed_bgm", ""):
                fixed_msgs.append(f"背景音乐：已固定 {Path(config.fixed_bgm).name}")
            else:
                fixed_msgs.append(f"背景音乐：{bgm_mode}")
            add("ok", "固定素材", "；".join(fixed_msgs))
        except Exception as exc:
            STATE.add_log(f"固定素材提示失败：{exc}")

        # 6 预计生成时间：优先用最近任务实测速度（条/秒含并发），无历史时按编码方式保守估算
        eta_seconds: int = 0
        actual_total = self._actual_total(config)
        try:
            total_items = actual_total
            workers = max(1, int(getattr(config, "workers", 1) or 1))
            accel = getattr(config, "encode_accel", "auto") or "auto"
            per_item = 2.5 if accel == "nvenc" else (12.0 if accel == "cpu" else 6.0)  # 秒/条
            speed = STATE.last_speed or (workers / per_item)
            eta_seconds = int(total_items / max(0.1, speed)) + 5
            add("ok", "预计时间", f"共 {total_items} 条，预计生成 {_fmt_eta(eta_seconds)}")
        except Exception as exc:
            STATE.add_log(f"预计时间计算失败：{exc}")

        errors = [i for i in items if i["level"] == "error"]
        warns = [i for i in items if i["level"] == "warn"]
        try:
            max_combos = self._count_combos(config)
        except Exception:
            max_combos = int(config.count)
        return {"ok": not errors, "items": items, "errors": errors, "warns": warns, "report": report, "eta_seconds": eta_seconds, "actual_count": actual_total, "max_combos": max_combos}

    def _ffmpeg_ok(self) -> bool:
        try:
            from video_engine import _ffmpeg
            exe = _ffmpeg()
            return bool(exe) and os.path.exists(str(exe))
        except Exception:
            return False

    def _count_combos(self, config: JobConfig) -> int:
        # 与生成端完全一致：先按指纹去重再计数（重复拷贝算 1 个）
        head = len(dedupe_by_fp(scan_videos(config.head_folder)))
        tail = len(dedupe_by_fp(scan_videos(config.tail_folder)))
        if config.fixed_head and config.fixed_tail:
            return 1
        if config.fixed_head:
            return tail
        if config.fixed_tail:
            return head
        return head * tail

    def _actual_total(self, config: JobConfig) -> int:
        """与生成端 build_combinations 完全一致的最终出片数：
        固定头尾=1；固定头/固定尾=min(请求, 去重后另一端素材数)（引擎固定分支不看去重开关、不凑满）；
        都未固定：去重开=min(请求,组合)；去重关=请求（可重复）。"""
        if config.fixed_head and config.fixed_tail:
            return 1
        req = int(config.count) * max(1, int(getattr(config, "dedupe_versions", 1) or 1))
        head_n = len(dedupe_by_fp(scan_videos(config.head_folder)))
        tail_n = len(dedupe_by_fp(scan_videos(config.tail_folder)))
        if config.fixed_head:
            return min(req, tail_n)
        if config.fixed_tail:
            return min(req, head_n)
        combos = head_n * tail_n
        if getattr(config, "dedupe_enabled", True):
            return min(req, combos)
        return req

    def _estimate_output_bytes(self, config: JobConfig, report: dict) -> int:
        """粗估输出体积：count × 单条时长 × 码率系数（保守估算，MB 级偏差可接受）。"""
        durations = [
            it["duration"] or 0
            for kind in ("head", "tail", "middle")
            for it in report.get(kind, [])
            if it.get("ok")
        ]
        if not durations:
            return 0
        avg = sum(durations) / len(durations)
        per_clip = avg * 2.5  # 开头 + 中间 + 结尾的保守倍数
        height = 0
        for kind in ("head", "tail", "middle"):
            for it in report.get(kind, []):
                if it.get("ok"):
                    height = max(height, it.get("height") or 0)
        if height >= 1920:
            rate = 1.2
        elif height >= 1080:
            rate = 0.7
        elif height >= 720:
            rate = 0.4
        else:
            rate = 0.25
        return int(config.count * per_clip * rate * 1024 * 1024)

    def _preview(self) -> None:
        if STATE.running:
            self._send_json({"ok": False, "error": "任务正在运行"})
            return
        payload = self._read_json()
        config = self._make_config(payload)
        if isinstance(config, str):
            self._send_json({"ok": False, "error": config})
            return
        config.count = 1
        config.output_folder = str(PREVIEW_DIR)
        # 预览命名 = 预览 + 时间戳（模板 {日期}_{时间} 自动追加序号，同秒也不冲突）；与素材名解耦
        config.output_name_template = "preview_{日期}_{时间}"
        # 预览只作为参数配置的试片：保留最近 5 个预览文件便于对比参数效果。
        # 生成前清理到 4 个旧文件（本次将生成 1 个 → 恰好 5 个），避免"旧5+新1=6"
        try:
            old_files = sorted(PREVIEW_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in old_files[4:]:
                old.unlink(missing_ok=True)
        except Exception:
            pass
        register_allowed_dir(str(PREVIEW_DIR))
        self._run_task(config, "预览", "preview", skip_existing=False, record_last=False, record_history=False)
        self._send_json({"ok": True})

    def _retry_failed(self) -> None:
        if STATE.running:
            self._send_json({"ok": False, "error": "任务正在运行"})
            return
        config = STATE.last_config
        failed_items = STATE.last_failed_items
        if not config or not failed_items:
            self._send_json({"ok": False, "error": "没有可重试的失败项"})
            return
        register_allowed_dir(config.output_folder)
        self._run_task(config, f"重试 {len(failed_items)} 个失败项", "retry", failed_items)
        self._send_json({"ok": True})

    def _toggle_pause(self) -> None:
        if not STATE.running:
            STATE.paused = False
            return
        if STATE.paused:
            STATE.pause_event.clear()
            STATE.paused = False
            STATE.add_log("已继续")
        else:
            STATE.pause_event.set()
            STATE.paused = True
            STATE.add_log("已暂停，当前素材处理完成后暂停")


def tempfile_dir() -> str:
    import tempfile as _tf
    return _tf.gettempdir()


def _read_local_version() -> str:
    """读取本地版本号（version.txt）。便携版打包时随 --add-data 放入 _internal；开发版在仓库根。"""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        candidates.append(Path(meipass) / "version.txt")
    candidates.append(Path(__file__).resolve().parent / "version.txt")
    for p in candidates:
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8-sig").strip()[:32] or "dev"
        except Exception:
            pass
    return "dev"


def _parse_version(v: str) -> tuple:
    """解析语义化版本号 vX.Y.Z → (X, Y, Z)；解析失败（如旧版 hash）返回 (0,0,0) 视为最低。"""
    s = str(v or "").strip().lower().lstrip("v")
    nums = []
    for part in re.split(r"[._\-]", s)[:3]:
        if part.isdigit():
            nums.append(int(part))
        else:
            break
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def _is_newer(remote: str, local: str) -> bool:
    """远端版本是否高于本地（按数字比较，防 v1.10.0 被字符串误判小于 v1.9.9）。"""
    return _parse_version(remote) > _parse_version(local)


def _system_proxy() -> dict | None:
    """读取 Windows 系统代理（注册表 ProxyEnable/ProxyServer），返回 ProxyHandler 参数。
    国内网络访问 GitHub 常需走代理，urllib 不自动读系统代理，这里显式接管；无代理返回 None（直连）。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        if enable and server:
            server = str(server).strip()
            if server:
                return {"http": "http://" + server, "https": "http://" + server}
    except Exception:
        pass
    return None


def _url_opener() -> urllib.request.OpenerDirector:
    proxy = _system_proxy()
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler(proxy))
    return urllib.request.build_opener()


def _fetch_update_info() -> dict:
    """读取 GitHub 远端版本信息（方案 B：半自动更新）：
    优先查最新 Release（tag=版本号，zip 资产 → 可下载），失败回退 version.txt（仅提示无下载）。"""
    import json
    import urllib.request

    local = _read_local_version()
    base = {"current": local, "latest": local, "has_update": False,
            "url": "https://github.com/YanZi50/infoflow", "download_url": None, "has_release": False}
    # 1) GitHub Releases API
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/YanZi50/infoflow/releases/latest",
            headers={"User-Agent": "sppj-update-check/1.0", "Accept": "application/vnd.github+json"},
        )
        with _url_opener().open(req, timeout=10) as resp:
            rel = json.loads(resp.read().decode("utf-8"))
        tag = str(rel.get("tag_name") or "").strip()[:32]
        if tag:
            url = ""
            for asset in rel.get("assets") or []:
                if str(asset.get("name", "")).lower().endswith(".zip"):
                    url = str(asset.get("browser_download_url") or "")
                    break
            base["latest"] = tag
            base["has_update"] = bool(local and _is_newer(tag, local))
            base["download_url"] = url or None
            base["has_release"] = True
            base["release_url"] = str(rel.get("html_url") or "https://github.com/YanZi50/infoflow/releases")
            return base
    except Exception:
        pass
    # 2) 回退：version.txt 对比（无 zip 资产时只提示，不能自动下载）
    try:
        import base64
        for url in (
            "https://api.github.com/repos/YanZi50/infoflow/contents/version.txt",
            "https://raw.githubusercontent.com/YanZi50/infoflow/master/version.txt",
        ):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "sppj-update-check/1.0"})
                with _url_opener().open(req, timeout=8) as resp:
                    data = resp.read()
                if url.startswith("https://api.github.com"):
                    obj = json.loads(data.decode("utf-8"))
                    raw = base64.b64decode(obj.get("content") or "").decode("utf-8-sig")
                else:
                    raw = data.decode("utf-8-sig")
                remote = raw.strip()[:32] or None
                if remote:
                    base["latest"] = remote
                    base["has_update"] = bool(local and remote and _is_newer(remote, local))
                break
            except Exception:
                continue
    except Exception:
        pass
    return base


def _check_update_async() -> None:
    """后台静默检查 GitHub 最新版本。任何失败都不打扰使用（离线/网络异常时保持 None）。"""

    def run() -> None:
        try:
            STATE.update_info = _fetch_update_info()
        except Exception:
            pass  # 静默失败：不影响任何现有功能

    threading.Thread(target=run, daemon=True).start()


def _update_install_root() -> Path:
    """更新解压/脚本落点：便携版（frozen）解压到程序目录下 _update_new；开发版解压到系统临时目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(tempfile.gettempdir())


def _download_update_async(download_url: str) -> None:
    """后台下载新版 zip → 解压到 _update_new → 生成《一键替换更新.bat》。失败置 failed 并给出原因。"""

    def run() -> None:
        import shutil
        import zipfile
        try:
            STATE.update_download = {"stage": "downloading", "done": 0, "total": 0, "error": None}
            root = _update_install_root()
            dl_dir = root / "_update_tmp"
            dl_dir.mkdir(parents=True, exist_ok=True)
            zip_path = dl_dir / "sppj_update.zip"
            # 流式下载（带进度）
            req = urllib.request.Request(download_url, headers={"User-Agent": "sppj-update-check/1.0"})
            with _url_opener().open(req, timeout=60) as resp, zip_path.open("wb") as f:
                total = int(resp.headers.get("Content-Length") or 0)
                STATE.update_download = {"stage": "downloading", "done": 0, "total": total, "error": None}
                done = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        STATE.update_download = {"stage": "downloading", "done": done, "total": total, "error": None}
            if zip_path.stat().st_size == 0:
                raise RuntimeError("下载的更新包为空")
            # 解压到 _update_new（覆盖旧的）
            new_dir = root / "_update_new"
            if new_dir.exists():
                shutil.rmtree(new_dir, ignore_errors=True)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(new_dir)
            # 便携版：zip 顶层可能是单个程序目录，把内容提升到 _update_new 根
            if getattr(sys, "frozen", False):
                subs = [p for p in new_dir.iterdir() if p.is_dir()] if new_dir.exists() else []
                if len(subs) == 1 and not (new_dir / "信息流素材一键拼接.exe").exists():
                    inner = subs[0]
                    tmp2 = root / "_update_new2"
                    if tmp2.exists():
                        shutil.rmtree(tmp2, ignore_errors=True)
                    inner.rename(tmp2)
                    shutil.rmtree(new_dir, ignore_errors=True)
                    tmp2.rename(new_dir)
            # 生成一键替换脚本
            if getattr(sys, "frozen", False):
                _write_update_script(root, new_dir)
            STATE.update_download = {"stage": "ready", "done": 0, "total": 0, "error": None}
            try:
                shutil.rmtree(dl_dir, ignore_errors=True)
            except Exception:
                pass
        except Exception as exc:
            STATE.update_download = {"stage": "failed", "done": 0, "total": 0, "error": str(exc)[:200]}

    threading.Thread(target=run, daemon=True).start()


def _write_update_script(root: Path, new_dir: Path) -> None:
    """生成《一键替换更新.bat》：关旧程序 → 只覆盖程序文件（保留用户数据目录）→ 重启。"""
    exe_name = "信息流素材一键拼接.exe"
    script = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "setlocal\r\n"
        f"set \"APP_DIR=%~dp0\"\r\n"
        f"set \"NEW=%APP_DIR%_update_new\"\r\n"
        f"echo 正在关闭旧版本程序...\r\n"
        f"taskkill /IM \"{exe_name}\" /F >nul 2>&1\r\n"
        "timeout /t 2 /nobreak >nul\r\n"
        "echo 正在替换程序文件（保留历史/配置/日志等个人数据）...\r\n"
        "robocopy \"%NEW%\" \"%APP_DIR%\" /E /IS /IT /XD history configs logs previews cache state _update_new _update_tmp >nul\r\n"
        f"rmdir /s /q \"%NEW%\" >nul 2>&1\r\n"
        f"echo 更新完成，正在启动...\r\n"
        f"start \"\" \"%APP_DIR%{exe_name}\"\r\n"
        "exit\r\n"
    )
    try:
        (root / "一键替换更新.bat").write_text(script, encoding="utf-8")
    except Exception:
        pass


def _pick_free_port(start: int = 8765, tries: int = 30) -> int:
    """8765 被占用（重复启动/残留进程）时自动找下一个空闲端口，避免启动即崩溃无提示。"""
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, p))
                return p
            except OSError:
                continue
    return start


def _open_browser_when_ready(url: str) -> None:
    """HTTP 服务就绪后才打开浏览器，避免便携版解包期间先弹出"无法访问此网页"。"""
    import urllib.request

    for _ in range(120):  # 最多等 60 秒
        try:
            with urllib.request.urlopen(url + "/api/ping", timeout=0.5):
                __import__("webbrowser").open(url)
                return
        except Exception:
            time.sleep(0.5)


def main() -> None:
    register_standard_dirs()
    global PORT
    PORT = _pick_free_port(PORT)
    # 断点续跑：上次任务被强杀/重启时快照残留，恢复为"可继续"状态
    snap = _load_snapshot()
    if snap:
        STATE.interrupted = {"exists": True, "label": snap["label"], "total": snap["total"]}
        STATE.last_config = snap["config"]
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"网页版已启动：{url}")
    if getattr(sys, "frozen", False):
        # 便携版：等服务就绪后自动打开默认浏览器（不再固定 1 秒）
        threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()
    _check_update_async()  # 版本更新静默检查（失败不影响使用）
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
