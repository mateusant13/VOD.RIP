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


def _redact_ai_key(settings: AppSettings) -> AppSettings:
    """Never serialize the write-only AI key: GET/POST settings responses
    report ai_api_key_set (bool) instead of the key itself."""
    return settings.model_copy(update={
        "ai_api_key": "",
        "ai_api_key_set": bool(settings.ai_api_key),
    })


def _prioritize_new_channels(old: list, new: list) -> None:
    """Top-priority the archive scheduler for channels that were just added.

    The FE saves the whole saved_channels list on every change (debounced),
    so a fresh channel is one whose id was not in the previous list. Marking
    happens before the swap so its first ingest passes skip the older
    backlog; edit/removal of existing ids leaves their priority untouched.
    """
    old_ids = {str(c.get("id") or "") for c in (old or [])}
    fresh = [
        c for c in (new or [])
        if str(c.get("id") or "") and str(c.get("id")) not in old_ids
    ]
    if not fresh:
        return
    try:
        from services.archive_db import mark_channel_priority

        for ch in fresh:
            for platform, key in (
                ("twitch", ch.get("twitchSlug")),
                ("kick", ch.get("kickSlug")),
                ("youtube", ch.get("youtubeSlug")),
            ):
                if (key or "").strip():
                    mark_channel_priority(platform, key)
    except Exception:  # noqa: BLE001 — priority marking must never fail the save
        logger.debug("channel priority mark skipped", exc_info=True)


@router.get("/api/settings", response_model=AppSettings)
async def get_settings():
    return _redact_ai_key(settings_mgr.get())


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
        # '' = auto (fastest usable drive); any explicit path wins.
        current.data_dir = update.data_dir.strip()
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
        _prioritize_new_channels(current.saved_channels, update.saved_channels)
        # Instant previews are keyed by channel id — drop preview files of
        # channels removed from the saved list so stale media never lingers.
        try:
            from services.instant_preview import remove_channel_previews

            old_ids = {str(c.get("id") or "") for c in (current.saved_channels or [])}
            for ch in (update.saved_channels or []):
                old_ids.discard(str(ch.get("id") or ""))
            for cid in old_ids:
                remove_channel_previews(cid)
        except Exception:  # noqa: BLE001 — cleanup must never fail the save
            logger.debug("instant preview cleanup skipped", exc_info=True)
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
    if update.start_with_windows is not None:
        current.start_with_windows = bool(update.start_with_windows)
        try:
            from services.autostart import set_windows_autostart

            ok = set_windows_autostart(current.start_with_windows)
            if not ok:
                logger.warning("autostart registry update failed — setting kept, launch stays manual")
        except Exception:
            logger.debug("autostart update skipped", exc_info=True)
    if update.cookie_bridge_enabled is not None:
        current.cookie_bridge_enabled = bool(update.cookie_bridge_enabled)
    if update.auto_install_extension is not None:
        current.auto_install_extension = bool(update.auto_install_extension)
    if update.entity_watch_enabled is not None:
        current.entity_watch_enabled = bool(update.entity_watch_enabled)
    if update.twitch_monitor_enabled is not None:
        current.twitch_monitor_enabled = bool(update.twitch_monitor_enabled)
    if update.archive_vod_keep_count is not None:
        current.archive_vod_keep_count = max(1, min(50, update.archive_vod_keep_count))
    if update.whisper_model is not None:
        # Non-empty model id; empty/whitespace falls back to the default.
        val = update.whisper_model.strip()
        current.whisper_model = val or "large-v3-turbo"
    if update.asr_engine is not None:
        # 'parakeet' (default) or 'whisper' — anything else falls back to
        # the default; the job router still auto-falls back to whisper for
        # ja/ko/zh/ar and parakeet-engine failures.
        val = (update.asr_engine or "").strip().lower()
        current.asr_engine = val if val in ("parakeet", "whisper") else "parakeet"
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
    if update.ui_language is not None:
        # 'en' | 'pt-BR' | 'es' — anything else is rejected ('' clears it
        # so the frontend re-seeds from the system language).
        val = (update.ui_language or "").strip()
        current.ui_language = val if val in ("en", "pt-BR", "es") else ""

    if update.download_layout is not None:
        layout = (update.download_layout or 'typed').strip().lower()
        current.download_layout = layout if layout in ('flat', 'typed') else 'typed'
    if update.download_transcript_sidecar is not None:
        current.download_transcript_sidecar = bool(update.download_transcript_sidecar)
    # Write-only AI key: handled BEFORE the toggle so a single save that sets
    # both key and toggle-on validates against the fresh key.
    if update.ai_api_key is not None:
        current.ai_api_key = (update.ai_api_key or "").strip()
        current.ai_api_key_set = bool(current.ai_api_key)
    if update.experimental_ai_enabled is not None:
        if update.experimental_ai_enabled and not current.ai_api_key:
            raise HTTPException(
                status_code=400,
                detail="Cannot enable experimental AI without an API key — add your OpenAI-compatible key first.",
            )
        current.experimental_ai_enabled = bool(update.experimental_ai_enabled)
    settings_mgr.save(current)
    download_mgr.apply_settings(settings_mgr)
    return _redact_ai_key(current)


@router.get("/api/settings/recommended")
async def recommended_resources():
    """Resource defaults suggested for THIS machine: download threads + max
    cache MB, computed from CPU threads, RAM and the cache drive's free
    space (services.settings.recommended_resource_defaults). The Settings
    UI exposes this as a one-click "Recommended" fill next to the fields."""
    from services.settings import recommended_resource_defaults

    return recommended_resource_defaults()


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
