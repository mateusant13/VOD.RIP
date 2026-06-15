"""
System routes — focus, exit, info, version, update, ytdlp status.
"""

import asyncio
import logging
import os
import platform

from fastapi import APIRouter, HTTPException

from deps import settings_mgr

logger = logging.getLogger(__name__)
router = APIRouter(tags=["system"])


@router.post("/api/focus")
async def focus_app():
    """Bring the desktop window to the foreground (second-instance launch)."""
    from services.app_lifecycle import show_window
    show_window()
    return {"ok": True}


@router.post("/api/exit")
async def exit_app():
    """Shut down all processes and kill the server."""
    logger.info("Exit requested via API — shutting down")
    from services.app_lifecycle import request_app_exit
    request_app_exit()
    return {"ok": True, "message": "Shutting down"}


@router.get("/api/info")
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
