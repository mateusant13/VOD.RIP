"""Kick & Twitch Downloader — FastAPI server with yt-dlp backend."""

import asyncio
import json
import logging
import os
import platform
import queue
import re
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from models.schemas import (
    AppSettings,
    DownloadRequest,
    DownloadState,
    OpenFolderRequest,
    PreviewSessionCreateRequest,
    PreviewSessionResponse,
    SettingsUpdate,
    VideoInfo,
)
from services.preview_service import (
    create_session,
    delete_session,
    get_master_playlist,
    proxy_master,
    proxy_playlist,
    proxy_segment,
    resolve_upstream,
    _is_playlist_url,
)
from services.download_manager import DownloadManager
from services.settings import SettingsManager
import yt_dlp

logger = logging.getLogger(__name__)
from services.ytdlp_service import detect_platform, get_video_info, is_clip_url
from services.twitch_gql_service import (
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
# Characters that Windows rejects in file paths. Anything we use as part
# of an output path or cache dir must be stripped of these or we get
# `[Errno 22] Invalid argument` on filesystem syscalls.
_WINDOWS_FORBIDDEN_PATH_CHARS = set('<>:"/\\|?*')
# Bare reserved device names that Windows treats specially. Suffixing
# with a dot/space or appending an extension is the only way out.
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
def _sanitize_path_component(value: str, fallback: str = "download") -> str:
    """Strip characters Windows rejects from a single path component.
    Returns `fallback` when the cleaned component is empty or matches a
    reserved device name. Use this whenever user input flows into a path
    segment that yt-dlp or ffmpeg will touch — `[Errno 22] Invalid
    argument` is what you get otherwise on Windows.
    """
    if value is None:
        return fallback
    cleaned = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", str(value)).strip(" .")
    if not cleaned or cleaned.upper() in _WINDOWS_RESERVED_NAMES:
        return fallback
    return cleaned
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
app = FastAPI(title="Kick & Twitch Downloader", version="2.0.0")
settings_mgr = SettingsManager()
download_mgr = DownloadManager(max_workers=4)
download_mgr.apply_settings(settings_mgr)
# Metadata fetches use their own pool so hung yt-dlp/playwright downloads
# cannot starve /api/info/* and /api/channel/videos.
INFO_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="info")
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
<title>VOD.RIP</title></head>
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


@app.post("/api/settings", response_model=AppSettings)
async def update_settings(update: SettingsUpdate):
    current = settings_mgr.get()
    if update.download_threads is not None:
        current.download_threads = max(1, min(16, update.download_threads))
    if update.max_cache_mb is not None:
        current.max_cache_mb = max(50, min(2000, update.max_cache_mb))
    if update.throttle_kib is not None:
        current.throttle_kib = update.throttle_kib
    if update.ffmpeg_path is not None:
        current.ffmpeg_path = update.ffmpeg_path
    if update.download_folder is not None:
        current.download_folder = update.download_folder.strip()
    if update.temp_folder is not None:
        current.temp_folder = update.temp_folder
    if update.oauth is not None:
        current.oauth = update.oauth
    if update.quality is not None:
        current.quality = update.quality
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
    stem = _sanitize_path_component(Path(raw).stem, fallback="clip")
    return str(base / f"{stem}.mp4")


def _clip_id_from_url(url: str) -> str:
    lowered = (url or "").lower()
    m = re.search(r"clips\.twitch\.tv/([^/?#]+)", lowered)
    if m:
        return m.group(1)[:24]
    m = re.search(r"twitch\.tv/[^/]+/clip/([^/?#]+)", lowered)
    if m:
        return m.group(1)[:24]
    m = re.search(r"kick\.com/[^/]+/clips/([^/?#]+)", lowered, re.I)
    if m:
        return m.group(1)[:24]
    return ""


def _clip_duration_tag(seconds: Optional[float]) -> str:
    """Filesystem-safe clip length tag, e.g. clip_1m10s or clip_70s."""
    if seconds is None or seconds <= 0:
        return "clip"
    sec = max(1, int(round(seconds)))
    minutes, secs = divmod(sec, 60)
    if minutes > 0:
        return f"clip_{minutes}m{secs}s"
    return f"clip_{secs}s"


def _build_output_path(req: DownloadRequest, opts: AppSettings, meta: dict) -> str:
    if req.output_file:
        return req.output_file
    base = _download_dir(opts)
    title = meta.get("title") or detect_platform(req.url).lower()
    stem = _sanitize_path_component(str(title), fallback="video")
    platform = detect_platform(req.url).lower()
    vod_id = _vod_id_from_url(req.url)
    suffix = f"_{vod_id}" if vod_id else ""
    return str(base / f"{stem}_{platform}{suffix}.mp4")


def _build_clip_output_path(req: DownloadRequest, opts: AppSettings, meta: dict) -> str:
    base = _download_dir(opts)
    clipper = (
        meta.get("channel")
        or meta.get("uploader")
        or _channel_slug_from_url(req.url)
        or "channel"
    )
    title = meta.get("title") or "clip"
    default_stem = _sanitize_path_component(f"{clipper} - {title} (clip)", fallback="clip")
    default_path = str(base / f"{default_stem}.mp4")
    return _resolve_output_file_override(req, opts, default_path)


def _tk_pick_folder() -> Optional[str]:
    """Native folder dialog via tkinter (STA thread on Windows)."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update_idletasks()
    try:
        path = filedialog.askdirectory(title="Choose download folder", parent=root)
        return path or None
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def _pick_folder_sync() -> tuple[Optional[str], Optional[str]]:
    """Show the native folder picker. Returns (path, error_message)."""
    err_msg: Optional[str] = None

    result_q: queue.Queue = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            result_q.put(("ok", _tk_pick_folder()))
        except Exception as exc:
            result_q.put(("err", str(exc)))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=125)
    if not result_q.empty():
        kind, value = result_q.get()
        if kind == "ok" and value:
            return value, None
        if kind == "err":
            err_msg = str(value)

    if os.name == "nt":
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$d.Description = 'Choose download folder'; "
            "$d.ShowNewFolderButton = $true; "
            "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { "
            "  Write-Output $d.SelectedPath "
            "}"
        )
        for exe in ("powershell", "pwsh"):
            try:
                out = subprocess.run(
                    [exe, "-NoProfile", "-Sta", "-Command", ps],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                path = (out.stdout or "").strip()
                if path:
                    return path, None
                if out.returncode != 0 and out.stderr:
                    err_msg = out.stderr.strip()
            except FileNotFoundError:
                continue
            except Exception as exc:
                err_msg = str(exc)
                logger.warning("Folder picker %s failed: %s", exe, exc)

    if err_msg:
        return None, err_msg
    return None, "Folder picker cancelled or unavailable."


def _open_folder_sync(path: str) -> None:
    """Reveal a file in Explorer, or open its parent folder (e.g. in-progress downloads)."""
    p = Path(path).expanduser()
    if p.exists():
        target = str(p.resolve())
        if os.name == "nt":
            if p.is_file():
                # /select,<path> must be one argument — separate args often fail or hang.
                subprocess.Popen(
                    ["explorer", f"/select,{target}"],
                    close_fds=True,
                )
            else:
                os.startfile(target)
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["open", "-R", target] if p.is_file() else ["open", target]
            )
        else:
            subprocess.Popen(["xdg-open", target if p.is_dir() else str(p.parent)])
        return

    parent = p.parent.resolve()
    if not parent.is_dir():
        raise FileNotFoundError(f"Folder does not exist: {parent}")
    folder = str(parent)
    if os.name == "nt":
        os.startfile(folder)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", folder])
    else:
        subprocess.Popen(["xdg-open", folder])


def _validate_open_folder_path(path: str) -> str:
    """Return normalized path if the file or its parent folder exists."""
    raw = (path or "").strip()
    if not raw:
        raise ValueError("path is required")
    p = Path(raw).expanduser()
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
        settings_mgr.save(current)
    return {"path": path, "error": err}


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
        session = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: create_session(
                preview_url,
                req.crop_start,
                req.crop_end,
                oauth=opts.oauth or None,
                prefer_height=req.prefer_height,
            ),
        )
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
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _preview_master_response(session_id: str, range_header: Optional[str]) -> Response:
    try:
        data, ctype, extra_headers, status = await asyncio.get_event_loop().run_in_executor(
            None, proxy_master, session_id, range_header
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


@app.get("/api/preview/hls/{session_id}/master.m3u8")
async def preview_hls_master(session_id: str, request: Request):
    return await _preview_master_response(session_id, request.headers.get("range"))


@app.get("/api/preview/hls/{session_id}/stream.mp4")
async def preview_stream_mp4(session_id: str, request: Request):
    """Progressive MP4 proxy (Twitch clips) — same bytes as master, .mp4 URL for <video>."""
    return await _preview_master_response(session_id, request.headers.get("range"))

@app.get("/api/preview/hls/{session_id}/resource")
async def preview_hls_resource(
    session_id: str,
    request: Request,
    id: Optional[str] = None,
    u: Optional[str] = None,
):
    range_header = request.headers.get("range")
    try:
        upstream = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: resolve_upstream(session_id, id, unquote(u) if u else None),
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    try:
        if _is_playlist_url(upstream):
            data, ctype, extra_headers, status = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: proxy_playlist(session_id, upstream),
            )
            return Response(content=data, media_type=ctype, status_code=status, headers=extra_headers)

        data, ctype, extra_headers, status = await asyncio.get_event_loop().run_in_executor(
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
    await asyncio.get_event_loop().run_in_executor(None, delete_session, session_id)
    return {"ok": True}


@app.post("/api/open-folder")
async def open_folder(req: OpenFolderRequest, background_tasks: BackgroundTasks):
    """Open Explorer/Finder on a download path. Returns immediately (non-blocking)."""
    try:
        normalized = _validate_open_folder_path(req.path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    background_tasks.add_task(_open_folder_sync, normalized)
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
        if not wanted:
            is_clips = (content or "").strip().lower() == "clips"
            return {
                "videos": [] if not is_clips else None,
                "clips": [] if is_clips else None,
                "channel": channel,
                "platforms": [],
                "content": "clips" if is_clips else "vods",
                "days": days,
                "per_platform_errors": {},
            }
        if (content or "").strip().lower() == "clips":
            clip_limit = max(1, min(int(limit), CHANNEL_CLIP_LIMIT))
            all_clips, per_platform_errors = await _gather_channel_clips(
                wanted=wanted,
                kick_slug=kick_ch,
                twitch_login=twitch_ch,
                limit=clip_limit,
            )
            return {
                "clips": all_clips,
                "videos": all_clips,
                "channel": channel,
                "platforms": wanted,
                "content": "clips",
                "per_platform_errors": per_platform_errors,
            }
        limit = max(1, min(int(limit), CHANNEL_LIMIT_MAX))
        days = max(0, min(int(days), 365))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days > 0 else None
        per_platform_errors: Dict[str, str] = {}
        all_videos: List[dict] = []
        loop = asyncio.get_running_loop()

        async def _fetch_twitch() -> None:
            try:
                vids = await loop.run_in_executor(
                    INFO_EXECUTOR, twitch_list_channel_videos_sync, channel, limit
                )
            except Exception as e:
                per_platform_errors["Twitch"] = _format_platform_error(e)
                return
            for v in vids:
                all_videos.append({
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
                })

        async def _fetch_kick() -> None:
            videos_url = f"https://kick.com/{channel}/videos"
            try:
                vids = await loop.run_in_executor(
                    INFO_EXECUTOR, kick_list_channel_videos_sync, videos_url, limit
                )
            except Exception as e:
                per_platform_errors["Kick"] = _format_platform_error(e)
                return
            for v in vids:
                all_videos.append({
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
                })

        tasks: List[asyncio.Task] = []
        if "Kick" in wanted:
            tasks.append(asyncio.create_task(_fetch_kick()))
        if "Twitch" in wanted:
            tasks.append(asyncio.create_task(_fetch_twitch()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
        return {
            "videos": all_videos,
            "channel": channel,
            "platforms": wanted,
            "content": "vods",
            "days": days,
            "per_platform_errors": per_platform_errors,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


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
            vids = await loop.run_in_executor(
                INFO_EXECUTOR, twitch_list_channel_clips_sync, twitch_login, limit
            )
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
            vids = await loop.run_in_executor(
                INFO_EXECUTOR,
                kick_list_channel_clips_sync,
                f"https://kick.com/{kick_slug}/clips",
                limit,
            )
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
        if not wanted:
            return {
                "clips": [],
                "channel": kick_ch or twitch_ch,
                "platforms": [],
                "content": "clips",
                "per_platform_errors": {},
            }
        limit = max(1, min(int(limit), CHANNEL_CLIP_LIMIT))
        all_clips, per_platform_errors = await _gather_channel_clips(
            wanted=wanted,
            kick_slug=kick_ch,
            twitch_login=twitch_ch,
            limit=limit,
        )
        return {
            "clips": all_clips,
            "channel": kick_ch or twitch_ch,
            "platforms": wanted,
            "content": "clips",
            "per_platform_errors": per_platform_errors,
        }
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
    )
    return {"download_id": download_id, "status": "started"}
@app.post("/api/download/clip")
async def download_clip(req: DownloadRequest):
    opts = settings_mgr.get()
    platform = detect_platform(req.url)
    meta = await _fetch_queue_meta(req.url, platform)
    output = _build_clip_output_path(req, opts, meta)
    _safe_makedirs(Path(output).parent)
    download_id = download_mgr.start_download(
        url=req.url,
        output_file=output,
        quality=req.quality or opts.quality,
        oauth=req.oauth or opts.oauth,
        crop_start=None,
        crop_end=None,
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


def _remove_download_history(download_id: str) -> dict:
    if not download_mgr.remove_history(download_id):
        raise HTTPException(
            status_code=404,
            detail="Download not found or still active",
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
                yield f": keepalive\n\n"

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

@app.get("/api/info")
async def server_info():
    return {
        "version": "2.0.0",
        "name": "Kick & Twitch Downloader",
        "engine": "yt-dlp (Python)",
        "description": "Lightweight web interface for downloading VODs and clips from Kick and Twitch",
        "python_version": platform.python_version(),
    }


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
    print(f"================================================")
    print(f"  Kick & Twitch Downloader v2.0 (Python)")
    print(f"  Open http://localhost:{port} in your browser")
    print(f"================================================")
    uvicorn.run(app, host="0.0.0.0", port=port)
