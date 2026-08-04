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

    Routed through the cache root (cache_dir setting -> biggest fixed drive)
    when one exists; otherwise the historical TEMP location. Lazy (not a
    module constant) so a cache_dir change is picked up at session-create
    time and tests can pin it via VODRIP_CACHE_DIR / settings.
    """
    from services.settings import cache_root

    root = cache_root()
    if root is not None:
        return root / "kd_preview"
    return Path(os.environ.get("TEMP", os.environ.get("TMP", "/tmp"))) / "kd_preview"


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
