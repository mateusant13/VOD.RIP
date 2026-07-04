"""
Settings routes — GET/POST /api/settings, /api/pick-folder, /api/open-folder.
"""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from models.schemas import AppSettings, OpenFolderRequest, SettingsUpdate

from deps import settings_mgr, download_mgr, OS_EXECUTOR
from utils import (
    download_dir,
    open_folder_sync,
    pick_folder_sync,
    safe_makedirs,
    validate_open_folder_path,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["settings"])


@router.get("/api/settings", response_model=AppSettings)
async def get_settings():
    return settings_mgr.get()


@router.get("/api/system/gpu-encoder")
async def system_gpu_encoder():
    from services.gpu_detect import get_encoder_detection
    from deps import INFO_EXECUTOR
    from services.ytdlp_service import _resolve_ffmpeg_exe
    ffmpeg_bin = _resolve_ffmpeg_exe(settings_mgr.get().ffmpeg_path or None)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        INFO_EXECUTOR, lambda: get_encoder_detection(ffmpeg_bin, fresh=True)
    )


@router.post("/api/settings", response_model=AppSettings)
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


@router.post("/api/pick-folder")
async def pick_folder():
    path, err = await asyncio.get_running_loop().run_in_executor(
        OS_EXECUTOR, pick_folder_sync
    )
    if path:
        current = settings_mgr.get()
        current.download_folder = path
        current.download_folder_confirmed = True
        settings_mgr.save(current)
    return {"path": path, "error": err}


@router.post("/api/open-folder")
async def open_folder(req: OpenFolderRequest):
    raw = (req.path or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="path is required")
    validated = validate_open_folder_path(raw, settings_mgr)
    try:
        open_folder_sync(validated)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}
