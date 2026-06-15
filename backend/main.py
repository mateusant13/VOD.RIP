"""Kick & Twitch Downloader — FastAPI server with yt-dlp backend."""

import asyncio
import json
import logging
import os
import platform
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from models.schemas import (
    AppSettings,
    DownloadRequest,
    OpenFolderRequest,
    PreviewQualityUpdateRequest,
    PreviewSessionCreateRequest,
    PreviewSessionResponse,
    SettingsUpdate,
)
from services.os_services import (
    _NO_WINDOW,
    open_file_or_folder,
    pick_folder as os_pick_folder,
    sanitize_filename_component,
)
from services._app_state import set_download_manager
from services.preview_service import (
    create_session,
    delete_session,
    proxy_master,
    proxy_playlist,
    proxy_segment,
    resolve_upstream,
    session_active_height,
    session_quality_labels,
    session_variant_heights,
    set_session_prefer_height,
    _is_playlist_url,
)
from services.channel_cache import get_cached, make_channel_cache_key, set_cached
from services.download_manager import DownloadManager
from services.settings import SettingsManager

logger = logging.getLogger(__name__)

# Per-thread COM apartment for SHOpenFolderAndSelectItems (avoid init/uninit each click).
_shell_com_local = threading.local() if os.name == "nt" else None

from services.ytdlp_service import detect_platform, get_video_info, is_clip_url
from services.twitch_gql_service import (
    get_clip_info_sync as twitch_get_clip_info_sync,
    get_video_info_sync as twitch_get_video_info_sync,
    list_channel_clips_sync as twitch_list_channel_clips_sync,
    list_channel_videos_sync as twitch_list_channel_videos_sync,
)
from services.kick_api_service import (
    get_clip_info_sync as kick_get_clip_info_sync,
    get_video_info_sync as kick_get_video_info_sync,
    list_channel_clips_sync as kick_list_channel_clips_sync,
    list_channel_videos_sync as kick_list_channel_videos_sync,
)
# A small helper for shaping per-platform error messages so the UI gets
# something readable instead of a raw Playwright traceback.
def _normalize_err(msg: str, limit: int = 200) -> str:
    if not msg:
        return ""
    msg = str(msg).strip()
    return msg if len(msg) <= limit else msg[: limit - 3] + "..."


def _format_platform_error(exc: BaseException) -> str:
    """Human-readable per-platform error (Playwright may raise empty NotImplementedError)."""
    msg = str(exc).strip()
    if msg:
        return msg
    name = type(exc).__name__
    if name == "NotImplementedError":
        return (
            "Playwright subprocess failed (Windows event loop). "
            "Restart the backend; if using dev mode, ensure Kick runs in a worker thread."
        )
    return name
# Path sanitization is now handled by ``os_services.sanitize_filename_component``
# which only strips Windows-forbidden characters on Windows.
# (imported at the top of the file)
def _safe_makedirs(path: Path) -> Path:
    """`mkdir(parents=True, exist_ok=True)` with a friendlier fallback.
    If the user-configured `temp_folder` or `Path.home()` produces a path
    that the OS can't create (e.g. invalid characters, denied ACL on
    `Users\\.cache`), we fall back to a tmpdir we know we can write to.
    Without this, a single bad path crashes the whole download with a
    raw `OSError` that the user has no way to act on.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "KickDownloader"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
app = FastAPI(title="Kick & Twitch Downloader", version="1.0.45")
settings_mgr = SettingsManager()
download_mgr = DownloadManager(max_workers=4)
download_mgr.apply_settings(settings_mgr)
set_download_manager(download_mgr)
# Metadata fetches use their own pool so hung yt-dlp downloads
# cannot starve /api/info/* and /api/channel/videos.
INFO_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="info")
CHANNEL_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="channel")
# Native OS actions (Explorer, folder picker) — keep off the default pool so
# downloads/metadata work cannot queue "show in folder" behind long tasks.
OS_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="os")

# Mount static files
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ==================== ROUTES ====================

@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve bundled UI when ``KICK_SERVE_UI=1``; otherwise redirect to Vite (dev)."""
    index_file = Path(__file__).parent / "static" / "index.html"
    serve_ui = os.environ.get("KICK_SERVE_UI", "").strip() == "1"
    ui_url = os.environ.get("KICK_UI_URL", "http://localhost:5173").strip()
    if not serve_ui:
        return HTMLResponse(
            f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="0;url={ui_url}">
<title>VOD.RIP 🪦</title></head>
<body style="font-family:system-ui;background:#09090b;color:#fafafa;padding:2rem">
<p>Redirecting to the UI at <a href="{ui_url}" style="color:#53fc18">{ui_url}</a>…</p>
<p style="color:#a1a1aa;font-size:0.875rem">API is on this port ({os.environ.get("PORT", "7897")}).
Run <code>npm run dev</code> for API + UI, or set <code>KICK_SERVE_UI=1</code> after <code>npm run build-copy</code>.</p>
</body></html>""",
            headers={"Cache-Control": "no-store"},
        )
    if index_file.exists():
        content = index_file.read_text(encoding="utf-8")
        return HTMLResponse(content, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    return HTMLResponse(
        "<h1>Kick & Twitch Downloader</h1>"
        "<p>Frontend not found. Run <code>npm run build-copy</code> then set <code>KICK_SERVE_UI=1</code>, "
        f"or open <a href=\"{ui_url}\">{ui_url}</a>.</p>"
    )


# --- Settings ---

@app.get("/api/settings", response_model=AppSettings)
async def get_settings():
    return settings_mgr.get()


@app.get("/api/system/gpu-encoder")
async def system_gpu_encoder():
    """GPU vendor + recommended H.264 encoder (for Settings auto mode)."""
    from services.gpu_detect import get_encoder_detection
    from services.ytdlp_service import _resolve_ffmpeg_exe

    ffmpeg_bin = _resolve_ffmpeg_exe(settings_mgr.get().ffmpeg_path or None)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        INFO_EXECUTOR, lambda: get_encoder_detection(ffmpeg_bin, fresh=True)
    )


@app.post("/api/settings", response_model=AppSettings)
async def update_settings(update: SettingsUpdate):
    current = settings_mgr.get()
    if update.download_threads is not None:
        current.download_threads = max(1, min(16, update.download_threads))
    if update.max_cache_mb is not None:
        current.max_cache_mb = max(50, min(2000, update.max_cache_mb))
    if update.video_encoder is not None:
        from services.ytdlp_service import normalize_video_encoder_setting

        current.video_encoder = normalize_video_encoder_setting(update.video_encoder)
    if update.throttle_kib is not None:
        current.throttle_kib = update.throttle_kib
    if update.ffmpeg_path is not None:
        current.ffmpeg_path = update.ffmpeg_path
    if update.download_folder is not None:
        current.download_folder = update.download_folder.strip()
        if current.download_folder:
            current.download_folder_confirmed = True
    if update.download_folder_confirmed is not None:
        current.download_folder_confirmed = update.download_folder_confirmed
    if update.temp_folder is not None:
        current.temp_folder = update.temp_folder
    if update.oauth is not None:
        current.oauth = update.oauth
    if update.quality is not None:
        current.quality = update.quality
    if update.panel_layout is not None:
        current.panel_layout = update.panel_layout
    if update.window_geometry is not None:
        current.window_geometry = update.window_geometry
    if update.saved_channels is not None:
        current.saved_channels = update.saved_channels
    if update.channel_kick_enabled is not None:
        current.channel_kick_enabled = bool(update.channel_kick_enabled)
    if update.channel_twitch_enabled is not None:
        current.channel_twitch_enabled = bool(update.channel_twitch_enabled)
    if update.channel_content_filter is not None:
        filt = (update.channel_content_filter or "vods").strip().lower()
        current.channel_content_filter = "clips" if filt == "clips" else "vods"
    if update.mp4_faststart is not None:
        current.mp4_faststart = bool(update.mp4_faststart)
    settings_mgr.save(current)
    download_mgr.apply_settings(settings_mgr)
    return current


def _download_dir(opts: AppSettings) -> Path:
    folder = (opts.download_folder or "").strip()
    if folder:
        return Path(folder)
    return Path.home() / "Downloads"


def _vod_id_from_url(url: str) -> str:
    platform = detect_platform(url)
    if platform == "Twitch":
        m = re.search(r"/videos/(\d+)", url)
        return m.group(1) if m else ""
    if platform == "Kick":
        m = re.search(
            r"/videos/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            url,
            re.I,
        )
        return m.group(1)[:8] if m else ""
    return ""


def _channel_slug_from_url(url: str) -> str:
    lowered = (url or "").lower()
    m = re.search(r"kick\.com/([^/?#]+)", lowered, re.I)
    if m and m.group(1).lower() not in ("videos", "clips"):
        return m.group(1)
    m = re.search(r"twitch\.tv/([^/?#]+)", lowered)
    if m and m.group(1).lower() not in ("videos", "clip", "directory", "clips"):
        return m.group(1)
    return ""


def _resolve_output_file_override(
    req: DownloadRequest, opts: AppSettings, default_path: str
) -> str:
    raw = (req.output_file or "").strip()
    if not raw:
        return default_path
    if os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":"):
        return raw
    base = _download_dir(opts)
    stem = sanitize_filename_component(Path(raw).stem, fallback="clip")
    return str(base / f"{stem}.mp4")




def _clip_duration_tag(seconds: Optional[float]) -> str:
    """Filesystem-safe clip length tag, e.g. clip_1m10s or clip_70s."""
    if seconds is None or seconds <= 0:
        return "clip"
    sec = max(1, int(round(seconds)))
    minutes, secs = divmod(sec, 60)
    if minutes > 0:
        return f"clip_{minutes}m{secs}s"
    return f"clip_{secs}s"


def _trim_range_tag(crop_start: Optional[float], crop_end: Optional[float]) -> str:
    """Filesystem-safe trim tag like ``00m12s-02m30s`` (start-end)."""
    def _fmt(sec: float) -> str:
        sec = max(0, int(round(sec)))
        m, s = divmod(sec, 60)
        if m > 0:
            return f"{m:02d}m{s:02d}s"
        return f"{s:02d}s"

    if crop_start is None and crop_end is None:
        return ""
    start = _fmt(crop_start or 0.0)
    end = _fmt(crop_end if crop_end is not None else (crop_start or 0.0) + 1)
    return f"{start}-{end}"


def _build_output_path(req: DownloadRequest, opts: AppSettings, meta: dict) -> str:
    if req.output_file:
        return req.output_file
    base = _download_dir(opts)
    title = meta.get("title") or detect_platform(req.url).lower()
    platform = detect_platform(req.url).lower()
    vod_id = _vod_id_from_url(req.url)
    duration = meta.get("duration")
    parts: list[str] = [sanitize_filename_component(str(title), fallback="video")]
    dur_tag = _clip_duration_tag(duration) if duration else ""
    if dur_tag:
        parts.append(dur_tag)
    parts.append(platform)
    if vod_id:
        parts.append(vod_id)
    stem = " - ".join([parts[0]] + parts[1:])
    if req.crop_start is not None and req.crop_end is not None:
        tag = _trim_range_tag(req.crop_start, req.crop_end)
        if tag:
            stem = f"{stem} [{tag}]"
    stem = sanitize_filename_component(stem, fallback="video")
    return str(base / f"{stem}.mp4")


def _build_clip_output_path(req: DownloadRequest, opts: AppSettings, meta: dict) -> str:
    base = _download_dir(opts)
    clipper = (
        meta.get("channel")
        or meta.get("uploader")
        or _channel_slug_from_url(req.url)
        or "channel"
    )
    title = meta.get("title") or "clip"
    duration = meta.get("duration")
    parts: list[str] = [
        sanitize_filename_component(clipper, fallback="channel"),
        sanitize_filename_component(title, fallback="clip"),
        _clip_duration_tag(duration) if duration else "clip",
    ]
    if req.crop_start is not None and req.crop_end is not None:
        tag = _trim_range_tag(req.crop_start, req.crop_end)
        if tag:
            parts.append(f"[{tag}]")
    stem = " - ".join(parts)
    stem = sanitize_filename_component(stem, fallback="clip")
    default_path = str(base / f"{stem}.mp4")
    return _resolve_output_file_override(req, opts, default_path)


def _pick_folder_sync() -> tuple[Optional[str], Optional[str]]:
    """Show the native folder picker using os_services (cross-platform)."""
    return os_pick_folder()


def _allow_foreground() -> None:
    """Best-effort unlock so Explorer may take focus after a user click."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(0xFFFFFFFF)
    except Exception:
        pass


def _focus_explorer_window(folder_path: str, item_name: Optional[str] = None) -> bool:
    """Bring the Explorer window showing ``folder_path`` to the foreground.

    Returns True if a matching window was found and focused, False otherwise.
    Best-effort UX — failures (including "no matching window") return False
    rather than raising, so the caller can decide whether to spawn a new
    Explorer process.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        SW_RESTORE = 9
        SW_SHOW = 5
        GW_OWNER = 4
        GA_ROOT = 2
        ASFW_ANY = 0xFFFFFFFF
        VK_MENU = 0x12
        KEYEVENTF_KEYUP = 0x0002

        folder_norm = os.path.normcase(os.path.abspath(folder_path))
        folder_base = (os.path.basename(folder_norm.rstrip("\\/")) or folder_norm).lower()

        # Build a set of "this might be a match" patterns. Modern Windows
        # titles look like ``foo - File Explorer`` or just ``foo``; we
        # used to only match the literal base name, which silently missed
        # everything that had the explorer suffix appended.
        needles: set[str] = {folder_base}
        if item_name:
            needles.add(os.path.normcase(item_name).lower())

        def _title_matches(tv: str) -> bool:
            tv = tv.lower()
            for n in needles:
                if not n:
                    continue
                if tv == n or tv.startswith(f"{n} -") or tv.startswith(n):
                    return True
            return False

        captured: list[int] = []

        def _cb(hwnd, _lparam):
            if captured:
                return False
            if not user32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(256)
            if user32.GetClassNameW(hwnd, cls, 256) == 0:
                return True
            if cls.value not in ("CabinetWClass", "ExploreWClass"):
                return True
            # Skip owned popups (preview pane, etc).
            if user32.GetWindow(hwnd, GW_OWNER):
                return True
            title = ctypes.create_unicode_buffer(512)
            n = user32.GetWindowTextW(hwnd, title, 512)
            if n <= 0:
                return True
            if _title_matches(title.value):
                captured.append(hwnd)
                return False
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        hwnd = captured[0] if captured else 0
        if not hwnd:
            return False
        # Climb to the root window (CabinetWClass is a child of the
        # frame on some builds), otherwise SetForegroundWindow fails.
        root = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
        # Force-foreground dance: SetForegroundWindow is gated by
        # Windows' "the calling thread cannot steal focus" rule. We
        # briefly attach our thread input to the foreground thread so
        # the call is allowed.
        try:
            user32.AllowSetForegroundWindow(ASFW_ANY)
        except Exception:
            pass
        try:
            fg = user32.GetForegroundWindow()
            fg_thread = user32.GetWindowThreadProcessId(fg, 0)
            my_thread = kernel32.GetCurrentThreadId()
            attached = False
            if fg_thread and fg_thread != my_thread:
                user32.AttachThreadInput(fg_thread, my_thread, True)
                attached = True
            try:
                # Brief Alt tap unlocks foreground on some Win10/11 builds.
                user32.keybd_event(VK_MENU, 0, 0, 0)
                user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
                user32.ShowWindow(root, SW_RESTORE)
                user32.ShowWindow(root, SW_SHOW)
                user32.BringWindowToTop(root)
                if hasattr(user32, "SwitchToThisWindow"):
                    user32.SwitchToThisWindow(root, True)
                user32.SetForegroundWindow(root)
            finally:
                if attached:
                    user32.AttachThreadInput(fg_thread, my_thread, False)
        except Exception:
            user32.ShowWindow(root, SW_RESTORE)
            user32.SetForegroundWindow(root)
        return True
    except Exception:
        logger.debug("Could not focus Explorer window", exc_info=True)
        return False


def _nudge_explorer_foreground(
    folder_path: str,
    item_name: Optional[str] = None,
    *,
    attempts: int = 1,
    delay: float = 0.0,
) -> None:
    """Raise Explorer after reveal — keep short (no multi-second poll)."""
    for i in range(max(1, attempts)):
        if _focus_explorer_window(folder_path, item_name):
            return
        if i + 1 < attempts and delay > 0:
            time.sleep(delay)


def _ensure_shell_com() -> bool:
    """Initialize COM once per thread for shell reveal calls."""
    if os.name != "nt" or _shell_com_local is None:
        return False
    if getattr(_shell_com_local, "ready", False):
        return True
    try:
        import ctypes
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2)
        if hr in (0, 1):
            _shell_com_local.ready = True
            return True
    except Exception:
        logger.debug("CoInitializeEx failed", exc_info=True)
    return False


def _shell_reveal_via_pidl(path: str) -> bool:
    """Reveal a path with ``SHOpenFolderAndSelectItems`` (shell foreground path)."""
    if os.name != "nt":
        return False
    if not _ensure_shell_com():
        return False
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.windll.shell32

        if not getattr(shell32, "_vodrip_pidl_configured", False):
            shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
            shell32.ILCreateFromPathW.restype = ctypes.c_void_p
            shell32.ILFree.argtypes = [ctypes.c_void_p]
            shell32.SHOpenFolderAndSelectItems.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_void_p,
                ctypes.c_ulong,
            ]
            shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
            shell32._vodrip_pidl_configured = True  # type: ignore[attr-defined]

        _allow_foreground()
        abspath = os.path.abspath(path)
        pidl = shell32.ILCreateFromPathW(abspath)
        if not pidl:
            return False
        try:
            result = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
            return result == 0
        finally:
            shell32.ILFree(pidl)
    except Exception:
        logger.debug("SHOpenFolderAndSelectItems failed", exc_info=True)
        return False


def _explorer_select_arg(path: str) -> str:
    """Build ``explorer.exe /select,…`` argument with proper quoting.

    Commas and spaces in VOD titles break the unquoted ``/select,path``
    form and Explorer often falls back to the user profile (Documents).
    """
    escaped = path.replace('"', '\\"')
    return f'/select,"{escaped}"'


def _reveal_path_windows(target: str) -> None:
    """Reveal a file in Explorer and bring the window forward."""
    _allow_foreground()
    abspath = os.path.abspath(target)
    if os.path.isfile(abspath):
        folder = os.path.dirname(abspath)
        item = os.path.basename(abspath)
        reveal_target = abspath
    elif os.path.isdir(abspath):
        folder = abspath
        item = None
        reveal_target = abspath
    else:
        parent = os.path.dirname(abspath)
        if parent and os.path.isdir(parent):
            folder = parent
            item = None
            reveal_target = parent
        else:
            return

    # Shell API — same call as Explorer's "Show in folder"; foregrounds itself.
    if _shell_reveal_via_pidl(reveal_target):
        _nudge_explorer_foreground(folder, item)
        return

    # Fallback: explorer.exe, then a brief focus nudge (not a multi-second poll).
    if item and os.path.isfile(abspath):
        subprocess.Popen(
            ["explorer.exe", _explorer_select_arg(abspath)],
            creationflags=_NO_WINDOW,
        )
    else:
        subprocess.Popen(["explorer.exe", folder], creationflags=_NO_WINDOW)
    _nudge_explorer_foreground(folder, item, attempts=10, delay=0.05)


def _open_folder_sync(path: str) -> None:
    """Reveal a file in Explorer/Finder, or open its parent folder."""
    raw = (path or "").strip()
    if not raw:
        raise ValueError("path is required")
    p = Path(raw).expanduser()
    # Validation already ensured the file or parent folder exists; only
    # wait briefly for a file that was created milliseconds ago.
    if not p.exists():
        for _ in range(2):
            time.sleep(0.05)
            if p.exists():
                break
    if p.exists():
        target = str(p.resolve())
        if os.name == "nt":
            _reveal_path_windows(target)
        else:
            open_file_or_folder(target, reveal=p.is_file())
        return

    parent = p.parent.resolve()
    if not parent.is_dir():
        raise FileNotFoundError(f"Folder does not exist: {parent}")
    folder = str(parent)
    if os.name == "nt":
        _reveal_path_windows(folder)
    else:
        open_file_or_folder(folder)


def _validate_open_folder_path(path: str) -> str:
    """Return normalized path if the file or its parent folder exists."""
    raw = (path or "").strip()
    if not raw:
        raise ValueError("path is required")
    p = Path(raw).expanduser()
    if not p.is_absolute() and not (len(raw) > 1 and raw[1] == ":"):
        p = _download_dir(settings_mgr.get()) / p
    if p.exists():
        return str(p.resolve())
    parent = p.parent.resolve()
    if parent.is_dir():
        return str(p.resolve())
    raise FileNotFoundError(f"Folder does not exist: {parent}")


@app.post("/api/pick-folder")
async def pick_folder():
    path, err = await asyncio.get_running_loop().run_in_executor(
        OS_EXECUTOR, _pick_folder_sync
    )
    if path:
        current = settings_mgr.get()
        current.download_folder = path
        current.download_folder_confirmed = True
        settings_mgr.save(current)
    return {"path": path, "error": err}


def _preview_session_response(session) -> PreviewSessionResponse:
    master = f"/api/preview/hls/{session.session_id}/master.m3u8"
    if session.kind == "progressive":
        playback = f"/api/preview/hls/{session.session_id}/stream.mp4"
    else:
        playback = master
    return PreviewSessionResponse(
        session_id=session.session_id,
        master_url=master,
        playback_url=playback,
        kind=session.kind,
        variant_heights=session_variant_heights(session),
        quality_labels=session_quality_labels(session),
        active_height=session_active_height(session),
    )


@app.post("/api/preview/session")
async def preview_create_session(req: PreviewSessionCreateRequest):
    if req.crop_end <= req.crop_start:
        raise HTTPException(status_code=400, detail="End must be after start")
    opts = settings_mgr.get()
    preview_url = (req.url or "").strip()
    try:
        from services.kick_models import canonical_kick_clip_url, extract_clip_id

        if "kick.com" in preview_url.lower() and extract_clip_id(preview_url):
            preview_url = canonical_kick_clip_url(preview_url)
    except ValueError:
        pass
    try:
        session = await asyncio.get_running_loop().run_in_executor(
            INFO_EXECUTOR,
            lambda: create_session(
                preview_url,
                req.crop_start,
                req.crop_end,
                oauth=opts.oauth or None,
                prefer_height=req.prefer_height,
            ),
        )
        return _preview_session_response(session)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/preview/session/{session_id}/quality")
async def preview_set_quality(session_id: str, req: PreviewQualityUpdateRequest):
    try:
        session = await asyncio.get_running_loop().run_in_executor(
            INFO_EXECUTOR,
            lambda: set_session_prefer_height(session_id, req.prefer_height),
        )
        return _preview_session_response(session)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _preview_apply_prefer_height(session_id: str, prefer_height: Optional[int]) -> None:
    if not prefer_height or prefer_height <= 0:
        return
    try:
        await asyncio.get_running_loop().run_in_executor(
            INFO_EXECUTOR,
            lambda: set_session_prefer_height(session_id, prefer_height),
        )
    except ValueError:
        pass


async def _preview_master_response(
    session_id: str,
    range_header: Optional[str],
    prefer_height: Optional[int] = None,
) -> Response:
    if prefer_height:
        await _preview_apply_prefer_height(session_id, prefer_height)
    try:
        data, ctype, extra_headers, status = await asyncio.get_running_loop().run_in_executor(
            INFO_EXECUTOR, proxy_master, session_id, range_header
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    body: Any = data
    response_headers = dict(extra_headers or {})
    response_headers.setdefault("Cache-Control", "no-cache")
    if ctype and ctype != "application/octet-stream":
        response_headers.setdefault("Content-Type", ctype)
    return Response(
        content=body,
        media_type=ctype or "application/octet-stream",
        status_code=status,
        headers=response_headers,
    )


def _parse_prefer_height_query(request: Request) -> Optional[int]:
    raw = request.query_params.get("prefer_height")
    if not raw:
        return None
    try:
        height = int(raw)
    except ValueError:
        return None
    return height if height > 0 else None


@app.get("/api/preview/hls/{session_id}/master.m3u8")
async def preview_hls_master(session_id: str, request: Request):
    return await _preview_master_response(
        session_id,
        request.headers.get("range"),
        _parse_prefer_height_query(request),
    )


@app.get("/api/preview/hls/{session_id}/stream.mp4")
async def preview_stream_mp4(session_id: str, request: Request):
    """Progressive MP4 proxy (Twitch clips) — same bytes as master, .mp4 URL for <video>."""
    return await _preview_master_response(
        session_id,
        request.headers.get("range"),
        _parse_prefer_height_query(request),
    )

@app.get("/api/preview/hls/{session_id}/resource")
async def preview_hls_resource(
    session_id: str,
    request: Request,
    id: Optional[str] = None,
):
    range_header = request.headers.get("range")
    loop = asyncio.get_running_loop()
    try:
        upstream = await loop.run_in_executor(
            None,
            lambda: resolve_upstream(session_id, id),
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    try:
        if _is_playlist_url(upstream):
            data, ctype, extra_headers, status = await loop.run_in_executor(
                None,
                lambda: proxy_playlist(session_id, upstream),
            )
            return Response(content=data, media_type=ctype, status_code=status, headers=extra_headers)

        data, ctype, extra_headers, status = await loop.run_in_executor(
            None,
            lambda: proxy_segment(session_id, upstream, range_header=range_header),
        )
        return Response(content=data, media_type=ctype, status_code=status, headers=extra_headers)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.delete("/api/preview/session/{session_id}")
async def preview_delete_session(session_id: str):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, delete_session, session_id)
    return {"ok": True}


@app.post("/api/open-folder")
async def open_folder(req: OpenFolderRequest):
    """Open Explorer/Finder on a download path and bring it to the foreground."""
    raw = (req.path or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="path is required")
    validated = _validate_open_folder_path(raw)
    try:
        # Run during the click-triggered request — BackgroundTasks runs too
        # late for Windows foreground-lock rules, so Explorer opens behind.
        _open_folder_sync(validated)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}

# --- Channel Videos ---
# How many days back the channel browser looks by default. The UI keeps the
# full result in memory and lets the user filter the visible list, so the
# window is the only knob that drives how much we fetch from upstream.
CHANNEL_DAYS_DEFAULT = 14
# Hard ceiling on results per platform. 100 is more than enough to cover
# 14 days of any reasonable streamer (1-2 VODs/day) without hammering
# the upstream endpoints.
CHANNEL_LIMIT_MAX = 100
CHANNEL_CLIP_LIMIT = 10
CHANNEL_CLIP_MAX_DURATION_SEC = 60


def _looks_like_clip_entry(entry: dict) -> bool:
    """Clips on Kick/Twitch are short (<=60s) and use clip URLs, not VOD pages."""
    url = (entry.get("url") or "").lower()
    if "/videos/" in url and "/clips/" not in url and "/clip/" not in url:
        return False
    if "/clips/" in url or "clips.twitch.tv" in url:
        pass
    elif "/clip/" in url:
        pass
    elif entry.get("content_kind") != "clip":
        return False
    duration = entry.get("duration")
    if duration is not None:
        try:
            if float(duration) > CHANNEL_CLIP_MAX_DURATION_SEC:
                return False
        except (TypeError, ValueError):
            pass
    return True


def _filter_clip_entries(entries: List[dict]) -> List[dict]:
    return [e for e in entries if _looks_like_clip_entry(e)]


def _resolve_channel_slug(raw: str) -> str:
    """Parse a channel login/slug from a bare name or Kick/Twitch URL."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Could not parse a channel name from the input.")
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    platform_hint = detect_platform(raw)
    channel: Optional[str] = None
    if platform_hint == "Twitch":
        m = re.search(r"twitch\.tv/([a-zA-Z0-9_]+)", raw)
        channel = m.group(1) if m else raw.strip().rstrip("/").split("/")[-1]
    elif platform_hint == "Kick":
        m = re.search(r"kick\.com/([a-zA-Z0-9_]+)", raw)
        channel = m.group(1) if m else raw.strip().rstrip("/").split("/")[-1]
    else:
        channel = raw.strip().rstrip("/").split("/")[-1] or raw.strip()
        if channel.startswith(("http://", "https://")):
            from urllib.parse import urlparse
            channel = urlparse(channel).path.strip("/").split("/")[0] or channel
    if not channel:
        raise ValueError("Could not parse a channel name from the input.")
    return channel


def _parse_wanted_platforms(platforms: str) -> List[str]:
    if not platforms or not platforms.strip():
        return ["Twitch", "Kick"]
    wanted = [p.strip() for p in platforms.split(",") if p.strip()]
    return wanted


def _parse_video_date(value) -> Optional[datetime]:
    """Best-effort parse of a video's `created_at` into an aware datetime.
    Returns None when the field is missing or unparseable, so the caller
    can decide whether to keep the entry (we keep it; we just can't bound
    it by date).
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Twitch `upload_date` is YYYYMMDD.
    if re.match(r"^\d{8}$", s):
        try:
            return datetime.strptime(s, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    # Kick API: 2024-05-21 12:34:56
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", s):
        s = s.replace(" ", "T") + "+00:00"
    # ISO-ish: 2024-05-21T12:34:56Z or with offset
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None
@app.get("/api/channel/videos")
async def channel_videos(
    url: str,
    limit: int = CHANNEL_LIMIT_MAX,
    days: int = CHANNEL_DAYS_DEFAULT,
    platforms: str = "Kick,Twitch",
    content: str = "vods",
    kick_slug: Optional[str] = None,
    twitch_login: Optional[str] = None,
):
    """Fetch archive VODs for a channel. Pass ``content=clips`` for clips (legacy alias)."""
    raw = unquote(url).strip()
    try:
        default_slug = _resolve_channel_slug(raw) if raw else ""
        kick_ch = (kick_slug or default_slug).strip().lower()
        twitch_ch = (twitch_login or default_slug).strip().lower()
        channel = kick_ch or twitch_ch
        wanted = _parse_wanted_platforms(platforms)
        content_norm = (content or "").strip().lower()
        limit_norm = max(1, min(int(limit), CHANNEL_CLIP_LIMIT if content_norm == "clips" else CHANNEL_LIMIT_MAX))
        days_norm = max(0, min(int(days), 365))
        cache_key = make_channel_cache_key(
            "videos",
            content_norm,
            kick_ch,
            twitch_ch,
            platforms,
            limit_norm,
            days_norm,
            ",".join(sorted(wanted)),
        )
        cached = get_cached(cache_key)
        if cached is not None:
            return cached
        if not wanted:
            is_clips = content_norm == "clips"
            payload = {
                "videos": [] if not is_clips else None,
                "clips": [] if is_clips else None,
                "channel": channel,
                "platforms": [],
                "content": "clips" if is_clips else "vods",
                "days": days,
                "per_platform_errors": {},
            }
            set_cached(cache_key, payload)
            return payload
        if content_norm == "clips":
            all_clips, per_platform_errors = await _gather_channel_clips(
                wanted=wanted,
                kick_slug=kick_ch,
                twitch_login=twitch_ch,
                limit=limit_norm,
            )
            payload = {
                "clips": all_clips,
                "videos": all_clips,
                "channel": channel,
                "platforms": wanted,
                "content": "clips",
                "per_platform_errors": per_platform_errors,
            }
            set_cached(cache_key, payload)
            return payload
        limit = limit_norm
        days = days_norm
        cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days > 0 else None
        per_platform_errors: Dict[str, str] = {}
        all_videos: List[dict] = []
        loop = asyncio.get_running_loop()

        async def _fetch_twitch() -> list:
            login = twitch_ch or channel
            if not login:
                return []
            try:
                vids = await asyncio.wait_for(
                    loop.run_in_executor(
                        CHANNEL_EXECUTOR, twitch_list_channel_videos_sync, login, limit
                    ),
                    timeout=CHANNEL_VOD_FETCH_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                per_platform_errors["Twitch"] = "VOD fetch timed out — try again"
                return []
            except Exception as e:
                per_platform_errors["Twitch"] = _format_platform_error(e)
                return []
            return [{
                "id": v["id"],
                "platform": "Twitch",
                "title": v.get("title") or "Untitled",
                "duration": v.get("duration"),
                "duration_string": v.get("duration_string"),
                "created_at": v.get("created_at"),
                "views": v.get("views"),
                "thumbnail_url": v.get("thumbnail_url"),
                "url": v.get("url") or f"https://www.twitch.tv/videos/{v['id']}",
                "channel": channel,
                "content_kind": "vod",
            } for v in vids]

        async def _fetch_kick() -> list:
            slug = kick_ch or channel
            if not slug:
                return []
            videos_url = f"https://kick.com/{slug}/videos"
            try:
                vids = await asyncio.wait_for(
                    loop.run_in_executor(
                        CHANNEL_EXECUTOR, kick_list_channel_videos_sync, videos_url, limit
                    ),
                    timeout=CHANNEL_VOD_FETCH_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                per_platform_errors["Kick"] = "VOD fetch timed out — try again"
                return []
            except Exception as e:
                per_platform_errors["Kick"] = _format_platform_error(e)
                return []
            return [{
                "id": v["id"],
                "platform": "Kick",
                "title": v.get("title") or "Untitled",
                "duration": v.get("duration"),
                "duration_string": v.get("duration_string"),
                "created_at": v.get("created_at"),
                "views": v.get("views"),
                "thumbnail_url": v.get("thumbnail"),
                "url": v.get("url") or f"https://kick.com/{channel}/videos/{v['id']}",
                "channel": channel,
                "content_kind": "vod",
            } for v in vids]

        tasks: list[asyncio.Task[list]] = []
        if "Kick" in wanted:
            tasks.append(asyncio.create_task(_fetch_kick()))
        if "Twitch" in wanted:
            tasks.append(asyncio.create_task(_fetch_twitch()))
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, list):
                    all_videos.extend(result)
                elif isinstance(result, BaseException):
                    logger.debug("Channel fetch task failed: %s", result)

        if cutoff is not None:
            filtered: List[dict] = []
            for v in all_videos:
                dt = _parse_video_date(v.get("created_at"))
                if dt is None or dt >= cutoff:
                    filtered.append(v)
            all_videos = filtered

        def _sort_key(v: dict) -> tuple:
            dt = _parse_video_date(v.get("created_at"))
            ts = -dt.timestamp() if dt else 0.0
            return (ts, v.get("platform") or "")

        all_videos.sort(key=_sort_key)
        for k, v in list(per_platform_errors.items()):
            per_platform_errors[k] = _normalize_err(v)
        payload = {
            "videos": all_videos,
            "channel": channel,
            "platforms": wanted,
            "content": "vods",
            "days": days,
            "per_platform_errors": per_platform_errors,
        }
        set_cached(cache_key, payload)
        return payload
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


CLIP_FETCH_TIMEOUT_SEC = 35
CHANNEL_VOD_FETCH_TIMEOUT_SEC = 45


async def _gather_channel_clips(
    *,
    wanted: List[str],
    kick_slug: str,
    twitch_login: str,
    limit: int,
) -> tuple[List[dict], Dict[str, str]]:
    """Fetch clips per platform using platform-specific logins."""
    per_platform_errors: Dict[str, str] = {}
    all_clips: List[dict] = []
    loop = asyncio.get_running_loop()
    kick_slug = (kick_slug or "").strip().lower()
    twitch_login = (twitch_login or "").strip().lower()

    async def _fetch_twitch() -> None:
        if not twitch_login:
            per_platform_errors["Twitch"] = "Twitch login is required"
            return
        try:
            vids = await asyncio.wait_for(
                loop.run_in_executor(
                    CHANNEL_EXECUTOR, twitch_list_channel_clips_sync, twitch_login, limit
                ),
                timeout=CLIP_FETCH_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            per_platform_errors["Twitch"] = "Clip fetch timed out — try again"
            return
        except Exception as e:
            per_platform_errors["Twitch"] = _format_platform_error(e)
            return
        for v in vids:
            all_clips.append({
                "id": v["id"],
                "platform": "Twitch",
                "title": v.get("title") or "Untitled",
                "duration": v.get("duration"),
                "duration_string": v.get("duration_string"),
                "created_at": v.get("created_at"),
                "views": v.get("views"),
                "thumbnail_url": v.get("thumbnail_url"),
                "url": v.get("url") or f"https://clips.twitch.tv/{v['id']}",
                "channel": twitch_login,
                "content_kind": "clip",
            })

    async def _fetch_kick() -> None:
        if not kick_slug:
            per_platform_errors["Kick"] = "Kick slug is required"
            return
        try:
            vids = await asyncio.wait_for(
                loop.run_in_executor(
                    CHANNEL_EXECUTOR,
                    kick_list_channel_clips_sync,
                    f"https://kick.com/{kick_slug}/clips",
                    limit,
                ),
                timeout=CLIP_FETCH_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            per_platform_errors["Kick"] = "Clip fetch timed out — try again"
            return
        except Exception as e:
            per_platform_errors["Kick"] = _format_platform_error(e)
            return
        for v in vids:
            all_clips.append({
                "id": v["id"],
                "platform": "Kick",
                "title": v.get("title") or "Untitled",
                "duration": v.get("duration"),
                "duration_string": v.get("duration_string"),
                "created_at": v.get("created_at"),
                "views": v.get("views"),
                "thumbnail_url": v.get("thumbnail"),
                "url": v.get("url") or f"https://kick.com/{kick_slug}/clips/{v['id']}",
                "channel": kick_slug,
                "content_kind": "clip",
            })

    tasks: List[asyncio.Task] = []
    if "Kick" in wanted:
        tasks.append(asyncio.create_task(_fetch_kick()))
    if "Twitch" in wanted:
        tasks.append(asyncio.create_task(_fetch_twitch()))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    all_clips = _filter_clip_entries(all_clips)
    all_clips.sort(key=lambda v: -(v.get("views") or 0))
    for k, v in list(per_platform_errors.items()):
        per_platform_errors[k] = _normalize_err(v)
    return all_clips, per_platform_errors


@app.get("/api/channel/clips")
async def channel_clips(
    url: str = "",
    platforms: str = "Kick,Twitch",
    limit: int = CHANNEL_CLIP_LIMIT,
    kick_slug: Optional[str] = None,
    twitch_login: Optional[str] = None,
):
    """Fetch recent clips for a channel (Kick ``/clips`` API, Twitch ``clips?range=7d``).

    Pass ``kick_slug`` / ``twitch_login`` when Kick and Twitch logins differ.
    Returns the last *limit* clips per platform (max 10), sorted by views desc.
  """
    try:
        default_slug = _resolve_channel_slug(unquote(url).strip()) if (url or "").strip() else ""
        kick_ch = (kick_slug or default_slug).strip().lower()
        twitch_ch = (twitch_login or default_slug).strip().lower()
        if not kick_ch and not twitch_ch:
            raise ValueError("Provide url, kick_slug, or twitch_login")

        wanted = _parse_wanted_platforms(platforms)
        limit_norm = max(1, min(int(limit), CHANNEL_CLIP_LIMIT))
        cache_key = make_channel_cache_key(
            "clips",
            kick_ch,
            twitch_ch,
            platforms,
            limit_norm,
            ",".join(sorted(wanted)),
        )
        cached = get_cached(cache_key)
        if cached is not None:
            return cached
        if not wanted:
            payload = {
                "clips": [],
                "channel": kick_ch or twitch_ch,
                "platforms": [],
                "content": "clips",
                "per_platform_errors": {},
            }
            set_cached(cache_key, payload)
            return payload
        all_clips, per_platform_errors = await _gather_channel_clips(
            wanted=wanted,
            kick_slug=kick_ch,
            twitch_login=twitch_ch,
            limit=limit_norm,
        )
        payload = {
            "clips": all_clips,
            "channel": kick_ch or twitch_ch,
            "platforms": wanted,
            "content": "clips",
            "per_platform_errors": per_platform_errors,
        }
        set_cached(cache_key, payload)
        return payload
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
def _explain_oserror(e: OSError) -> str:
    """Turn a raw OSError into something a human can act on.
    The most common offender is `[Errno 22] Invalid argument` on
    Windows, which usually means a path contains a character the OS
    rejects (e.g. `<>"|?*`, or a non-drive colon). Surface the path
    and the errno so the user can fix the offending setting.
    """
    msg = str(e) or e.__class__.__name__
    if e.filename:
        return f"{msg} (path: {e.filename!r})"
    return msg
# --- Video Info ---
@app.get("/api/info/video")
async def info_video(id: str):
    try:
        lowered = id.lower()
        if is_clip_url(id):
            return await info_clip(id)
        loop = asyncio.get_running_loop()
        if "kick.com" in lowered:
            return await loop.run_in_executor(INFO_EXECUTOR, kick_get_video_info_sync, id)
        if "twitch.tv" in lowered or re.search(r"^\d+$", id.strip()):
            return await loop.run_in_executor(INFO_EXECUTOR, twitch_get_video_info_sync, id)
        info = await get_video_info(id)
        return info
    except OSError as e:
        raise HTTPException(status_code=400, detail=_explain_oserror(e))
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/info/clip")
async def info_clip(id: str):
    try:
        loop = asyncio.get_running_loop()
        lowered = id.lower()
        if "kick.com" in lowered and is_clip_url(id):
            return await loop.run_in_executor(INFO_EXECUTOR, kick_get_clip_info_sync, id)
        if is_clip_url(id) or "clips.twitch.tv" in lowered or "/clip/" in lowered:
            return await loop.run_in_executor(INFO_EXECUTOR, twitch_get_clip_info_sync, id)
        info = await get_video_info(id)
        return info
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


# --- Downloads ---

async def _fetch_queue_meta(url: str, platform: str) -> dict:
    """Best-effort metadata fetch so the queue UI can show VOD info.
    Returns a dict with `title`, `channel`, `thumbnail`, `duration`,
    `duration_string`. Empty dict on failure (the queue will still work,
    just with less info).
    """
    try:
        loop = asyncio.get_running_loop()
        if is_clip_url(url):
            if platform == "Kick":
                info = await loop.run_in_executor(INFO_EXECUTOR, kick_get_clip_info_sync, url)
            elif platform == "Twitch":
                info = await loop.run_in_executor(INFO_EXECUTOR, twitch_get_clip_info_sync, url)
            else:
                info = await get_video_info(url)
        elif platform == "Kick":
            info = await loop.run_in_executor(INFO_EXECUTOR, kick_get_video_info_sync, url)
        elif platform == "Twitch":
            info = await loop.run_in_executor(INFO_EXECUTOR, twitch_get_video_info_sync, url)
        else:
            info = await get_video_info(url)
        if info is None:
            return {}
        # `info` may be a Pydantic model or a plain dict depending on the path.
        if hasattr(info, "model_dump"):
            info = info.model_dump()
        elif not isinstance(info, dict):
            return {}
        return {
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "duration_string": info.get("duration_string"),
        }
    except Exception:
        return {}
def _require_hls_crop(req: DownloadRequest, platform: str) -> None:
    if is_clip_url(req.url):
        return
    if platform not in ("Twitch", "Kick"):
        return
    if req.crop_start is None or req.crop_end is None:
        raise HTTPException(
            status_code=400,
            detail="crop_start and crop_end are required for Twitch/Kick downloads",
        )
    if req.crop_end <= req.crop_start:
        raise HTTPException(status_code=400, detail="crop_end must be after crop_start")


def _trim_estimated_bytes(meta: dict, crop_start: Optional[float], crop_end: Optional[float]) -> Optional[int]:
    """Scale full-VOD byte estimate to the requested trim window."""
    estimated = meta.get("estimated_bytes")
    if not estimated:
        return None
    duration = meta.get("duration")
    if crop_start is None or crop_end is None or not duration or duration <= 0:
        return int(estimated)
    clip_sec = float(crop_end) - float(crop_start)
    if clip_sec <= 0:
        return int(estimated)
    return int(int(estimated) * clip_sec / float(duration))


@app.post("/api/download/video")
async def download_video(req: DownloadRequest):
    opts = settings_mgr.get()
    platform = detect_platform(req.url)
    _require_hls_crop(req, platform)
    meta = await _fetch_queue_meta(req.url, platform)
    output = _build_output_path(req, opts, meta)
    _safe_makedirs(Path(output).parent)
    download_func = None
    if platform == "Kick":
        from services.kick_api_service import download_vod_sync as kick_download_vod
        download_func = kick_download_vod
    download_id = download_mgr.start_download(
        url=req.url,
        output_file=output,
        quality=req.quality or opts.quality,
        oauth=req.oauth or opts.oauth,
        crop_start=req.crop_start,
        crop_end=req.crop_end,
        download_func=download_func,
        settings_mgr=settings_mgr,
        title=meta.get("title"),
        channel=meta.get("channel"),
        thumbnail=meta.get("thumbnail"),
        duration=meta.get("duration"),
        duration_string=meta.get("duration_string"),
        estimated_bytes=_trim_estimated_bytes(meta, req.crop_start, req.crop_end),
    )
    return {"download_id": download_id, "status": "started"}
@app.post("/api/download/clip")
async def download_clip(req: DownloadRequest):
    opts = settings_mgr.get()
    platform = detect_platform(req.url)
    meta = await _fetch_queue_meta(req.url, platform)
    output = _build_clip_output_path(req, opts, meta)
    _safe_makedirs(Path(output).parent)
    crop_start = req.crop_start
    crop_end = req.crop_end
    if crop_start is not None and crop_end is not None and crop_end <= crop_start:
        raise HTTPException(status_code=400, detail="crop_end must be after crop_start")
    download_id = download_mgr.start_download(
        url=req.url,
        output_file=output,
        quality=req.quality or opts.quality,
        oauth=req.oauth or opts.oauth,
        crop_start=crop_start,
        crop_end=crop_end,
        download_func=None,
        download_type="clip",
        settings_mgr=settings_mgr,
        title=meta.get("title"),
        channel=meta.get("channel"),
        thumbnail=meta.get("thumbnail"),
        duration=meta.get("duration"),
        duration_string=meta.get("duration_string"),
    )
    return {"download_id": download_id, "status": "started"}

@app.get("/api/downloads")
async def list_downloads():
    return download_mgr.get_active_and_history()


@app.post("/api/download/{download_id}/retry")
async def retry_download(download_id: str):
    """Re-queue a failed, cancelled, or interrupted download (alias for resume)."""
    return await resume_download(download_id)


def _download_func_for_entry(entry: dict):
    platform = detect_platform(entry["url"])
    dtype = entry.get("type", entry.get("download_type", "video"))
    if platform == "Kick" and dtype == "video":
        from services.kick_api_service import download_vod_sync as kick_download_vod
        return kick_download_vod
    return None


@app.post("/api/download/{download_id}/resume")
async def resume_download(download_id: str):
    opts = settings_mgr.get()
    entry = download_mgr.get_resumable_entry(download_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Download not found or not resumable")
    new_id = download_mgr.resume(
        download_id,
        oauth=opts.oauth,
        download_func=_download_func_for_entry(entry),
        settings_mgr=settings_mgr,
    )
    if not new_id:
        raise HTTPException(status_code=404, detail="Download not found or not resumable")
    return {"download_id": new_id, "resumed": True}


@app.get("/api/download/{download_id}")
async def get_download(download_id: str):
    state = download_mgr.get(download_id)
    if not state:
        raise HTTPException(status_code=404, detail="Download not found")
    return state


@app.post("/api/download/{download_id}/cancel")
async def cancel_download(download_id: str):
    success = download_mgr.cancel(download_id)
    return {"cancelled": success}


@app.post("/api/download/{download_id}/pause")
async def pause_download(download_id: str):
    success = download_mgr.pause(download_id)
    if not success:
        raise HTTPException(status_code=404, detail="Download not found or not pausable")
    return {"paused": True}


def _remove_download_history(download_id: str) -> dict:
    if not download_mgr.discard_from_queue(download_id):
        raise HTTPException(
            status_code=404,
            detail="Download not found",
        )
    return {"removed": True}


@app.post("/api/download/{download_id}/remove")
async def remove_download_history(download_id: str):
    """Remove a finished download from history (POST — same pattern as cancel)."""
    return _remove_download_history(download_id)


@app.delete("/api/download/{download_id}")
async def delete_download_history(download_id: str):
    """Remove a finished download from history."""
    return _remove_download_history(download_id)


@app.get("/api/download/{download_id}/stream")
async def download_stream(download_id: str, request: Request):
    queue: asyncio.Queue = asyncio.Queue()
    download_mgr.register_sse(download_id, queue)

    async def stream():
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("complete", "error"):
                    break
            except asyncio.TimeoutError:
                yield f": keepalive\n\n"  # noqa: F541

    async def stream_wrapper():
        try:
            async for chunk in stream():
                yield chunk
        finally:
            download_mgr.unregister_sse(download_id, queue)

    return StreamingResponse(
        stream_wrapper(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# --- System ---


@app.post("/api/focus")
async def focus_app():
    """Bring the desktop window to the foreground (second-instance launch)."""
    from services.app_lifecycle import show_window

    show_window()
    return {"ok": True}


@app.post("/api/exit")
async def exit_app():
    """Shut down all processes and kill the server."""
    logger.info("Exit requested via API — shutting down")

    from services.app_lifecycle import request_app_exit

    request_app_exit()
    return {"ok": True, "message": "Shutting down"}


@app.get("/api/info")
async def server_info():
    try:
        from services._version import __version__ as app_version
    except ImportError:
        app_version = "0.0.0"
    return {
        "version": app_version,
        "name": "VOD.RIP 🪦",
        "desktop": os.environ.get("KICK_SERVE_UI", "").strip() == "1",
        "engine": "yt-dlp (Python)",
        "description": "Kick & Twitch VOD and clip downloader",
        "python_version": platform.python_version(),
    }


@app.get("/api/app/version")
async def app_version():
    try:
        from services._version import __version__
    except ImportError:
        __version__ = "0.0.0"
    return {"version": __version__}


@app.get("/api/update/check")
async def update_check(force: bool = False):
    from services.settings import _get_appdata_dir
    from services.updater import UpdateChecker

    try:
        from services._version import __version__
    except ImportError:
        __version__ = "0.0.0"

    checker = UpdateChecker(__version__, _get_appdata_dir())
    release = checker.check(force=force)
    return {"current": __version__, "update": release}


@app.post("/api/update/apply")
async def update_apply():
    from services.settings import _get_appdata_dir
    from services.updater import UpdateChecker

    try:
        from services._version import __version__
    except ImportError:
        __version__ = "0.0.0"

    checker = UpdateChecker(__version__, _get_appdata_dir())
    pending = checker.get_pending() or checker.check(force=True)
    if not pending:
        raise HTTPException(status_code=404, detail="No update available")

    result = checker.download_and_install(pending)
    if not result.ok:
        raise HTTPException(status_code=500, detail=result.message or "Update failed")
    return {"ok": True, "message": result.message or "Installing update"}


@app.get("/api/ytdlp/status")
async def ytdlp_status():
    try:
        import yt_dlp
        return {"available": True, "version": yt_dlp.version.__version__}
    except ImportError:
        return {"available": False, "version": None}


# ==================== MAIN ====================

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 7897))
    print("================================================")
    print("  Kick & Twitch Downloader v2.0 (Python)")
    print(f"  Open http://localhost:{port} in your browser")
    print("================================================")
    uvicorn.run(app, host="0.0.0.0", port=port)
