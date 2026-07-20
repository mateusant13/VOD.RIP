"""
Shared application state — managers, executors, and constants.

This module is the single place where top-level singletons are created so
that every router / service can import them without circular dependency
issues.  It mirrors what used to live at the top of ``main.py``.
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

from services.download_manager import DownloadManager
from services.settings import SettingsManager

# ── Application-level singletons ────────────────────────────────────────

settings_mgr = SettingsManager()
download_mgr = DownloadManager(max_workers=4)
download_mgr.apply_settings(settings_mgr)

# Import side-effect: register the download manager so shutdown_util
# can cancel downloads without a circular import.
from services._app_state import set_download_manager
set_download_manager(download_mgr)

# ── Thread-pool executors ───────────────────────────────────────────────
# Metadata fetches use their own pool so hung yt-dlp downloads
# cannot starve /api/info/* and /api/channel/videos.
INFO_EXECUTOR = ThreadPoolExecutor(max_workers=24, thread_name_prefix="info")
CHANNEL_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="channel")
# Preview operations (session create/seek/quality/stream) run on their own
# pool so the user's click is never queued behind batch warm tasks.
PREVIEW_EXECUTOR = ThreadPoolExecutor(max_workers=12, thread_name_prefix="preview")
# Native OS actions (Explorer, folder picker) — keep off the default pool so
# downloads/metadata work cannot queue "show in folder" behind long tasks.
OS_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="os")

# ── Per-thread COM (Windows shell) ─────────────────────────────────────
_shell_com_local = threading.local() if os.name == "nt" else None

# ── Channel-browsing constants ─────────────────────────────────────────
# How many days back the channel browser looks by default.
CHANNEL_DAYS_DEFAULT = 14
# Hard ceiling on results per platform.
CHANNEL_LIMIT_MAX = 100
CHANNEL_CLIP_LIMIT = 10
CHANNEL_CLIP_MAX_DURATION_SEC = 60
CLIP_FETCH_TIMEOUT_SEC = 35
CHANNEL_VOD_FETCH_TIMEOUT_SEC = 45
YOUTUBE_CHANNEL_FETCH_TIMEOUT_SEC = 90  # ponytail: yt-dlp cold bootstrap + enrich can exceed 45s
