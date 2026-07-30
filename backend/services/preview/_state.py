"""Shared mutable state for the preview package — extracted from session.py and warm.py.

These were all in the same module scope in the original monolithic preview_service.py.
After the split, they must live in a module both session.py and warm.py can import
without circular dependencies. Neither session.py nor warm.py imports from this module;
they import the names directly from the preview package.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple


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
