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

# ── Preview manager ────────────────────────────────────────────────────
from services.preview_service import _manager as preview_manager

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
# Live preview sessions (POST /api/preview/live + rotate) run on their own
# small pool: the live POST must never queue behind slow/stuck VOD
# create_session extracts on PREVIEW_EXECUTOR — the popup's stall budget is
# 8s, and live creates are pure CDN fetches (no yt-dlp), so 4 workers keep
# the badge click playable even while VOD previews are saturated.
LIVE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="live")
# Background YouTube warm jobs (startup wave, channel-list, hover/batch).
# Dedicated pool: bulk warms must never saturate INFO_EXECUTOR (208+ queued
# startup jobs starved user clicks) and never compete with the user-facing
# preview path (PREVIEW_EXECUTOR is isolated). 8 workers: authenticated
# (cookies+POT) resolves land in ~1s, so the startup wave drains in seconds.
WARM_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="warm")
# User-intent YouTube warms (hover/scroll/paste) + their spawned head/preflight
# downloads. Separate pool so a 50+ job startup storm can never make a
# gesture warm wait minutes (observed: 12 min queue behind the storm, then the
# extract hit YouTube's bot-gate — the worst of both worlds).
GESTURE_WARM_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="warm-gesture")
# Native OS actions (Explorer, folder picker) — keep off the default pool so
# downloads/metadata work cannot queue "show in folder" behind long tasks.
OS_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="os")

# ── Warm-work global cap ────────────────────────────────────────────────
# Boot warm storm spans WARM(3)+GESTURE(2)+FULL(1)+ANON(2)+sync-wave(2) and
# every warm job runs a yt-dlp extract and/or an ffmpeg mux — a 50+ URL
# startup wave could otherwise run ~60 concurrent yt-dlp/ffmpeg processes on
# one user PC. Every warm _run body (warm.py, session.py prog-head warm,
# app.py sync wave) holds this semaphore around its heavy section, capping
# total warm extract/mux work at 8 regardless of which executor it lands on.
# User-click paths (create_session / PREVIEW_EXECUTOR / live paths) never
# acquire it — clicks stay responsive while the warm queue drains.
WARM_WORK_SEMAPHORE = threading.BoundedSemaphore(8)

# ── Per-thread COM (Windows shell) ─────────────────────────────────────
_shell_com_local = threading.local() if os.name == "nt" else None

# ── Channel-browsing constants ─────────────────────────────────────────
# How many days back the channel browser looks by default.
CHANNEL_DAYS_DEFAULT = 14
# Hard ceiling on results per platform.
CHANNEL_LIMIT_MAX = 250
CHANNEL_CLIP_LIMIT = 25
CHANNEL_CLIP_MAX_DURATION_SEC = 60
CLIP_FETCH_TIMEOUT_SEC = 20
CHANNEL_VOD_FETCH_TIMEOUT_SEC = 20
YOUTUBE_CHANNEL_FETCH_TIMEOUT_SEC = 30  # ponytail: yt-dlp cold bootstrap + enrich; was90s but health watchdog triggers at 45s
# Delta refresh: after the disk index is warm, refreshes fetch only the
# newest N items per platform and merge — never the full list again.
CHANNEL_DELTA_LIMIT = 25
# Snapshot freshness windows per platform. YouTube's yt-dlp extract is the
# expensive one (4-20s, globally serialized) — it runs at most this often
# per channel in the background.
KICK_CHANNEL_FRESH_SEC = 600
TWITCH_CHANNEL_FRESH_SEC = 600
YOUTUBE_CHANNEL_FRESH_SEC = 1800
