"""
System routes — focus, exit, info, version, update, ytdlp status, local media.
"""

import asyncio
import logging
import os
import platform

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from deps import OS_EXECUTOR, settings_mgr
from utils import media_type_for_path, validate_local_media_path

logger = logging.getLogger(__name__)
router = APIRouter(tags=["system"])


@router.get("/api/local/media")
async def local_media(path: str):
    """Stream a completed download from disk (Range-aware)."""
    try:
        loop = asyncio.get_running_loop()
        file_path = await loop.run_in_executor(
            OS_EXECUTOR,
            lambda: validate_local_media_path(path, settings_mgr),
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return FileResponse(
        str(file_path),
        media_type=media_type_for_path(file_path),
        filename=file_path.name,
    )


@router.post("/api/focus")
async def focus_app():
    """Bring the desktop window to the foreground (second-instance launch)."""
    from services.app_lifecycle import show_window
    show_window()
    return {"ok": True}


@router.post("/api/exit")
async def exit_app():
    """Shut down all processes and kill the server."""
    logger.warning("Exit requested via API — shutting down (caller traceback follows)")
    import traceback as _tb
    logger.warning("".join(_tb.format_stack(limit=8)))
    from services.app_lifecycle import request_app_exit
    request_app_exit()
    return {"ok": True, "message": "Shutting down"}


@router.get("/api/info")
async def server_info():
    # include features so /api/info reflects opt-in state
    try:
        from services.feature_registry import get_enabled_map
        _feats = get_enabled_map()
    except Exception:
        _feats = {}
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
        "features": _feats,
    }


@router.get("/api/asr/runtime")
async def asr_runtime_status() -> dict:
    """Report whether the optional speech runtime is installed."""
    from services.asr_runtime import runtime_status

    return runtime_status()


@router.post("/api/asr/runtime")
async def install_asr_runtime() -> dict:
    """Download and install the optional speech runtime on explicit request."""
    from services.asr_runtime import ensure_runtime, runtime_status

    try:
        await asyncio.to_thread(ensure_runtime)
    except Exception as exc:
        logger.warning("ASR runtime installation failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return runtime_status()


@router.get("/api/errors/latest")
async def latest_errors(limit: int = Query(20, ge=1, le=500)) -> dict:
    """Latest server/application errors (bounded ring, no secrets).

    Mirrors the live-captions error ring: unauthenticated but bounded (max
    500 entries, sanitized — cookies/tokens are stripped on ingest).
    """
    from services.error_log import get_error_ring

    # ponytail: unauthenticated but bounded (500 entries, sanitized); gate
    # behind auth when app auth lands (same caveat as live_captions_errors).
    return {"errors": get_error_ring(limit)}


@router.get("/api/health")
async def health():
    """Aggregate liveness for external supervisors (dev-all, launcher watchdog).

    Answering 200 is itself the liveness proof; the fields let a supervisor
    tell the app's real state: queue backlog, detached worker/background
    daemons, and the age of the app's own 30s heartbeat (stale heartbeat
    with a live process = hung app). Best-effort — a DB hiccup degrades
    fields to None/False, never raises."""
    from services import archive_db

    try:
        pending = archive_db.has_pending_jobs()
    except Exception:
        pending = None
    try:
        worker = archive_db.worker_live(age_s=45, tag="transcribe")
    except Exception:
        worker = False
    try:
        background = archive_db.worker_live(age_s=90, tag="background")
    except Exception:
        background = False
    try:
        activity_age = archive_db.worker_heartbeat_age("app-activity")
    except Exception:
        activity_age = None
    return {
        "ok": True,
        "name": "VOD.RIP",
        "queue_pending": pending,
        "worker_alive": worker,
        "background_alive": background,
        "app_activity_age_s": activity_age,
    }


@router.get("/api/app/version")
async def app_version():
    try:
        from services._version import __version__
    except ImportError:
        __version__ = "0.0.0"
    return {"version": __version__}


@router.get("/api/update/check")
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


@router.post("/api/update/apply")
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


@router.get("/api/ytdlp/status")
async def ytdlp_status():
    try:
        import yt_dlp
        return {"available": True, "version": yt_dlp.version.__version__}
    except ImportError:
        return {"available": False, "version": None}


@router.get("/api/system/window-state")
async def window_state():
    """Current VOD.RIP window state for runtime policy decisions."""
    from services.app_lifecycle import is_window_active, is_window_minimized, get_window_policy
    return {
        "active": is_window_active(),
        "minimized": is_window_minimized(),
        "policy": get_window_policy(),
    }


@router.post("/api/presence")
async def presence(body: dict):
    """Presence heartbeat: POST {foreground: bool} toggles governor ceiling 40%<->80%."""
    fg = bool(body.get("foreground", True)) if isinstance(body, dict) else True
    try:
        from services.resource_governor import get_governor
        target = get_governor().set_foreground(fg)
    except Exception:
        target = 0.80 if fg else 0.40
    return {"foreground": fg, "effective_target": target}


@router.get("/api/features")
async def list_features():
    """Canonical feature list — manifest + current enabled map."""
    from services.feature_registry import get_manifest, get_enabled_map
    return {"manifest": get_manifest(), "features": get_enabled_map()}


@router.get("/api/info/features")
async def info_features():
    """Deprecated alias for /api/features — kept for backward compat."""
    from services.feature_registry import get_manifest, get_enabled_map
    return {"manifest": get_manifest(), "features": get_enabled_map()}
