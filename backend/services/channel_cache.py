"""Short-lived in-memory cache for channel list API responses."""

from __future__ import annotations

import time
from threading import Lock
from typing import Any, Optional


class ChannelCache:
    """Thread-safe, bounded TTL cache for channel API responses.

    Maintains a single in-memory dict keyed by channel identifiers,
    auto-expiring entries after *ttl* seconds and capping total
    entries at_max_entries.
    """

    def __init__(self, default_ttl_sec: float = 90.0, max_entries: int = 50) -> None:
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = Lock()
        self._default_ttl_sec = default_ttl_sec
        self._max_entries = max_entries

    # ponytail: bounded cache — prevents unbounded growth under abuse
    # self._max_entries caps the cache at construction time

    def _purge_expired(self, now: float, ttl: float) -> None:
        expired = [k for k, (ts, _) in self._cache.items() if now - ts > ttl]
        for key in expired:
            self._cache.pop(key, None)

    def get_cached(self, key: str, *, ttl: Optional[float] = None) -> Optional[Any]:
        ttl = ttl if ttl is not None else self._default_ttl_sec
        now = time.monotonic()
        with self._lock:
            self._purge_expired(now, ttl)
            entry = self._cache.get(key)
            if not entry:
                return None
            ts, data = entry
            if now - ts > ttl:
                self._cache.pop(key, None)
                return None
            return data

    def set_cached(self, key: str, data: Any) -> None:
        with self._lock:
            self._cache[key] = (time.monotonic(), data)
            if len(self._cache) > self._max_entries:
                self._purge_expired(time.monotonic(), self._default_ttl_sec)
            if len(self._cache) > self._max_entries:
                oldest = min(self._cache.items(), key=lambda item: item[1][0])
                self._cache.pop(oldest[0], None)


# Module-level singleton so existing importers keep working.
# ponytail: module-level singleton — one cache per process; safe because the app runs a single backend server.
_cache = ChannelCache()
get_cached = _cache.get_cached
set_cached = _cache.set_cached


def make_channel_cache_key(*parts: object) -> str:
    """Build a deterministic cache key from ordered parts."""
    return "|".join(str(p) for p in parts)
