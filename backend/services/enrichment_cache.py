"""Persistent per-video enrichment cache for YouTube metadata.

Stores enrichment results (created_at, views, duration, availability) as a
JSON file under the app data directory. Reduces network calls on repeat channel
fetches — only new or stale video_ids hit InnerTube.

Thread-safety: simple threading.Lock; concurrent writers last-write-wins is
intentional (ponytail: avoids fsync overhead and atomic-rename complexity for a
cache where occasional data loss is harmless).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

# TTLs in seconds
TTL_META = 7 * 86400       # 7 days — created_at, duration (immutable facts)
TTL_VIEWS = 6 * 3600       # 6 hours — view count (changes slowly)
TTL_AVAIL = 6 * 3600       # 6 hours — member-only status (rarely flips)

_CACHE_FILE = "enrichment_cache.json"

_cache: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
_loaded = False
_cache_path: Optional[str] = None


def _get_appdata_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "VOD.RIP"


def _get_cache_path() -> str:
    global _cache_path
    if _cache_path is None:
        _cache_path = str(_get_appdata_dir() / _CACHE_FILE)
    return _cache_path


def _ensure_loaded() -> None:
    global _cache, _loaded
    if _loaded:
        return
    path = _get_cache_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _cache = data
    except (json.JSONDecodeError, OSError):
        pass  # ponytail: corrupt file = start fresh
    _loaded = True


def _save() -> None:
    path = _get_cache_path()
    dirpath = os.path.dirname(path)
    try:
        os.makedirs(dirpath, exist_ok=True)
        # ponytail: simple overwrite — a corrupt cache on crash is acceptable
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False)
    except OSError:
        pass  # ponytail: best-effort cache write, never block the caller


def get(video_id: str) -> Optional[dict[str, Any]]:
    """Return the cached entry for *video_id*, or None."""
    if not video_id:
        return None
    with _lock:
        _ensure_loaded()
        return _cache.get(video_id)


def set(video_id: str, data: dict[str, Any]) -> None:
    """Store metadata for *video_id* and persist."""
    if not video_id:
        return
    with _lock:
        _ensure_loaded()
        old = _cache.get(video_id, {})
        merged = dict(old)
        merged.update(data)
        merged["cached_at"] = time.time()
        _cache[video_id] = merged
        _save()


def set_availability(video_id: str, availability: Optional[str]) -> None:
    """Store only the availability field (from the lighter check pass)."""
    if not video_id:
        return
    with _lock:
        _ensure_loaded()
        entry = _cache.get(video_id, {})
        entry["availability"] = availability
        entry["avail_cached_at"] = time.time()
        entry["cached_at"] = time.time()
        _cache[video_id] = entry
        _save()


def apply_to_row(row: dict[str, Any]) -> bool:
    """Apply cached fresh enrichment fields to *row*.

    Returns True if any field was applied (row may still need more enrichment).
    Returns False if no cached data exists.
    """
    vid = row.get("id", "")
    if not vid:
        return False
    entry = get(vid)
    if not entry:
        return False

    now = time.time()
    applied = False
    cached_at = entry.get("cached_at", 0)

    # created_at — immutable, 7d TTL
    ca = entry.get("created_at")
    if ca and not row.get("created_at") and now - cached_at < TTL_META:
        row["created_at"] = ca
        applied = True

    # duration — immutable, 7d TTL
    dur = entry.get("duration")
    if dur is not None and not row.get("duration") and now - cached_at < TTL_META:
        row["duration"] = dur
        applied = True

    # views — changes slowly, 6h TTL
    views = entry.get("views")
    if views is not None and row.get("views") is None and now - cached_at < TTL_VIEWS:
        row["views"] = views
        applied = True

    # availability — 6h TTL from separate timestamp
    avail = entry.get("availability")
    avail_ts = entry.get("avail_cached_at", 0)
    if avail is not None and not row.get("_availability_checked") and now - avail_ts < TTL_AVAIL:
        row["_availability_checked"] = True
        row["availability"] = avail
        applied = True

    # If metadata fields (created_at, views, duration) were cached — even as None —
    # signal that enrichment was attempted. Avoids re-fetching for channels where
    # the InnerTube API simply doesn't return these fields.
    has_meta = any(k in entry for k in ("created_at", "views", "duration"))
    if has_meta and now - cached_at < TTL_META:
        row["_enriched"] = True
        applied = True

    return applied
