"""Shared mutable state for the preview package — extracted from session.py and warm.py.

These were all in the same module scope in the original monolithic preview_service.py.
After the split, they must live in a module both session.py and warm.py can import
without circular dependencies. Neither session.py nor warm.py imports from this module;
they import the names directly from the preview package.
"""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


def preview_root() -> Path:
    """Root of the kd_preview temp/media cache.

    Routed through the data root (fastest usable drive) so live preview media
    is read/written on quick storage; the data root itself defaults to the
    fastest drive and falls back to the app-data drive when none exists. Lazy
    (not a module constant) so a data_dir change is picked up at
    session-create time and tests can pin it via VODRIP_DATA_DIR / settings.
    """
    from services.disk_hygiene import data_dir

    return data_dir() / "kd_preview"


# --- Resolved stream cache ---
_RESOLVED_STREAM_TTL_SEC: int = 3600
_RESOLVED_STREAM_MAX: int = 256
_RESOLVED_STREAM_CACHE: Dict[str, Tuple[float, Tuple]] = {}
_RESOLVED_STREAM_LOCK = threading.Lock()

# --- Session snapshot cache ---
_SESSION_SNAPSHOT_TTL_SEC: int = _RESOLVED_STREAM_TTL_SEC  # type: ignore[misc]
_SESSION_SNAPSHOT_MAX: int = 256
_SESSION_SNAPSHOT: Dict[Tuple[str, int], Tuple[float, dict]] = {}
_SESSION_SNAPSHOT_LOCK = threading.Lock()

# --- YouTube warm inflight dedup ---
_YOUTUBE_WARM_INFLIGHT: Dict[str, threading.Event] = {}

# --- YouTube warm cache ---
_YOUTUBE_WARM_CACHE: dict[str, Any] = {}

# --- Channel warm slots ---
_CHANNEL_WARM_SLOTS: dict[str, list[str]] = {}

# --- YouTube warm cooldown ---
_YOUTUBE_WARM_COOLDOWN_UNTIL: float = 0  # time.monotonic() threshold

# --- Active YouTube preview ---
_ACTIVE_YOUTUBE_PREVIEW_KEY: Optional[str] = None
_ACTIVE_YOUTUBE_PREVIEW_LOCK = threading.Lock()

# --- Preflight mux ---
_PREFLIGHT_MUX_INFLIGHT: Dict[str, threading.Event] = {}
_PREFLIGHT_MUX_LOCK = threading.Lock()

# --- Cold create dedup (Gap 1) ---
# Leader/follower registry for concurrent cold create_session calls on the
# same YouTube video: the first click resolves, later clicks wait on the
# event and reuse the snapshot instead of stampeding the extract chain.
_CREATE_INFLIGHT: Dict[str, threading.Event] = {}
_CREATE_INFLIGHT_LOCK = threading.Lock()

# --- Warm rate limiter ---
_MAX_WARM_FAILURES: int = 3
_WARM_COOLDOWN_SEC: int = 120  # 2 minutes
_YOUTUBE_WARM_RATE_LIMIT_LOCK = threading.Lock()

# --- Warmed URLs dedup ---
_WARMED_URLS: set = set()
_WARMED_URLS_LOCK = threading.Lock()

# --- YouTube warm locks ---
_YOUTUBE_WARM_LOCK = threading.Lock()
_YOUTUBE_WARM_CACHE_LOCK = threading.Lock()
_CHANNEL_WARM_SLOTS_LOCK = threading.Lock()

# --- Warm consecutive failure state ---
_YOUTUBE_WARM_CONSECUTIVE_FAILURES: int = 0
_PRINTED_COOLDOWN: bool = False

# --- Per-video warm dead-skip ---
# A video whose FULL warm extract chain failed as "preview unavailable"
# (members-only / age-gated / region-blocked / deleted-but-listed) is skipped
# by WARM passes for this long. One dead row in a channel's recent list must
# never re-grind ~20s per warm attempt — and must never arm the global
# bot-gate pause that kills warm for EVERY video (observed: one dead VOD
# re-armed the 2h pause 14 times in a 12h window; warm was dead the whole
# session and every preview open went cold).
_WARM_DEAD_VID_TTL_SEC: int = 6 * 3600
_WARM_DEAD_VIDS: dict[str, float] = {}  # video_id -> time.monotonic() until
_WARM_DEAD_VIDS_LOCK = threading.Lock()

# --- Full warm dedup set ---
_full_warm_queued: set = set()


def _validate_proxy_url(url: str) -> bool:
    """Return True if the URL is safe to proxy (public host, not internal)."""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host or host in ("localhost", "127.0.0.1", "::1"):
        return False
    # Check if private IP (fast fail)
    try:
        ip = socket.getaddrinfo(host, 80, socket.AF_INET)[0][4][0]
        # RFC 1918, RFC 6598, RFC 6890
        if ip.startswith(
            (
                "10.",
                "172.16.",
                "172.17.",
                "172.18.",
                "172.19.",
                "172.20.",
                "172.21.",
                "172.22.",
                "172.23.",
                "172.24.",
                "172.25.",
                "172.26.",
                "172.27.",
                "172.28.",
                "172.29.",
                "172.30.",
                "172.31.",
                "192.168.",
                "127.",
                "169.254.",
                "0.",
            )
        ):
            return False
    except (socket.gaierror, IndexError):
        return False
    return True
