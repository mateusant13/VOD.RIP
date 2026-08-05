"""
Settings routes — GET/POST /api/settings, /api/pick-folder, /api/open-folder.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from models.schemas import AppSettings, OpenFolderRequest, SettingsUpdate

from deps import settings_mgr, download_mgr, OS_EXECUTOR
from utils import (
    open_folder_sync,
    pick_folder_sync,
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


@router.get("/api/settings/youtube-auth")
async def youtube_auth_status():
    """Which browser / PO providers auto-auth will use (diagnostics for Settings)."""
    from deps import INFO_EXECUTOR
    from services.youtube_auth import auth_status

    s = settings_mgr.get()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        INFO_EXECUTOR,
        lambda: auth_status(
            getattr(s, "youtube_auto_auth", True),
            s.youtube_cookies_browser or "",
        ),
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
    if update.cache_dir is not None:
        # '' = auto (biggest fixed drive); any explicit path wins.
        current.cache_dir = update.cache_dir.strip()
    if update.data_dir is not None:
        # '' = auto (%APPDATA%/VOD.RIP); any explicit path wins.
        current.data_dir = update.data_dir.strip()
    if update.oauth is not None:
        current.oauth = update.oauth
    if update.youtube_cookies_file is not None:
        current.youtube_cookies_file = update.youtube_cookies_file.strip()
    if update.youtube_cookies_browser is not None:
        current.youtube_cookies_browser = update.youtube_cookies_browser.strip()
    if update.youtube_visitor_data is not None:
        current.youtube_visitor_data = update.youtube_visitor_data.strip()
    if update.youtube_po_token is not None:
        current.youtube_po_token = update.youtube_po_token.strip()
    if update.youtube_tokens_file is not None:
        current.youtube_tokens_file = update.youtube_tokens_file.strip()
    if update.youtube_auto_auth is not None:
        current.youtube_auto_auth = update.youtube_auto_auth
    if update.youtube_pot_headless is not None:
        current.youtube_pot_headless = update.youtube_pot_headless
    if update.youtube_wpc_pot is not None:
        if update.youtube_wpc_pot:
            raise HTTPException(
                status_code=400,
                detail="WPC PO tokens are disabled — they spawn headless Chrome",
            )
        current.youtube_wpc_pot = False
    if update.quality is not None:
        current.quality = update.quality
    if update.panel_layout is not None:
        current.panel_layout = update.panel_layout
    if update.window_geometry is not None:
        current.window_geometry = update.window_geometry
    if update.saved_channels is not None:
        current.saved_channels = update.saved_channels
        # A channel was added/edited/removed — wake the archive scheduler
        # so indexing for the new channel starts immediately instead of
        # waiting for the next periodic pass.
        try:
            from services.archive_scheduler import kick_scheduler_pass

            kick_scheduler_pass()
        except Exception:
            logger.debug("archive scheduler kick skipped", exc_info=True)
    if update.channel_kick_enabled is not None:
        current.channel_kick_enabled = bool(update.channel_kick_enabled)
    if update.channel_twitch_enabled is not None:
        current.channel_twitch_enabled = bool(update.channel_twitch_enabled)
    if update.channel_youtube_enabled is not None:
        current.channel_youtube_enabled = bool(update.channel_youtube_enabled)
    if update.channel_content_filter is not None:
        filt = (update.channel_content_filter or "vods").strip().lower()
        current.channel_content_filter = filt if filt in ("clips", "vods", "streams") else "vods"
    if update.mp4_faststart is not None:
        current.mp4_faststart = bool(update.mp4_faststart)
    if update.skip_youtube_startup_warm is not None:
        current.skip_youtube_startup_warm = bool(update.skip_youtube_startup_warm)
    if update.cookie_bridge_enabled is not None:
        current.cookie_bridge_enabled = bool(update.cookie_bridge_enabled)
    if update.entity_watch_enabled is not None:
        current.entity_watch_enabled = bool(update.entity_watch_enabled)
    if update.archive_vod_keep_count is not None:
        current.archive_vod_keep_count = max(1, min(50, update.archive_vod_keep_count))
    if update.whisper_model is not None:
        # Non-empty model id; empty/whitespace falls back to the default.
        val = update.whisper_model.strip()
        current.whisper_model = val or "large-v3-turbo"
    if update.whisper_model_cache is not None:
        # Any non-empty path, or None to clear back to the default cache.
        val = update.whisper_model_cache.strip()
        current.whisper_model_cache = val or None
    if update.yt_subtitles_first is not None:
        current.yt_subtitles_first = bool(update.yt_subtitles_first)
    if update.archive_smart_enrich is not None:
        current.archive_smart_enrich = bool(update.archive_smart_enrich)
    if update.asr_language is not None:
        # 'auto' or a family code ('pt'/'en'/'es'); anything else is kept
        # verbatim (whisper accepts raw codes) but never left blank.
        val = (update.asr_language or "").strip().lower()
        current.asr_language = val or "auto"
    if update.channel_asr_languages is not None:
        current.channel_asr_languages = {
            str(k).strip(): str(v).strip().lower()
            for k, v in (update.channel_asr_languages or {}).items()
            if str(k).strip()
        } or None
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
    from utils import allow_foreground

    allow_foreground()
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
