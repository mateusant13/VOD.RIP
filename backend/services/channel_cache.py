"""Short-lived in-memory cache for channel list API responses."""

from __future__ import annotations

import time
from threading import Lock
from typing import Any, Optional

_CACHE: dict[str, tuple[float, Any]] = {}
_LOCK = Lock()
_DEFAULT_TTL_SEC = 90.0


def _purge_expired(now: float, ttl: float) -> None:
    expired = [k for k, (ts, _) in _CACHE.items() if now - ts > ttl]
    for key in expired:
        _CACHE.pop(key, None)


def get_cached(key: str, *, ttl: float = _DEFAULT_TTL_SEC) -> Optional[Any]:
    now = time.monotonic()
    with _LOCK:
        _purge_expired(now, ttl)
        entry = _CACHE.get(key)
        if not entry:
            return None
        ts, data = entry
        if now - ts > ttl:
            _CACHE.pop(key, None)
            return None
        return data


def set_cached(key: str, data: Any) -> None:
    with _LOCK:
        _CACHE[key] = (time.monotonic(), data)


def make_channel_cache_key(*parts: object) -> str:
    return "|".join(str(p) for p in parts)
