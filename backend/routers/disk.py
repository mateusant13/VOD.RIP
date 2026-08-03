"""Disk management — per-category usage, one-click cleanups, low-disk status.

Settings > Disk surface. All paths resolve at request time from the same env
knobs the services use (VODRIP_ARCHIVE_DIR, VODRIP_WHISPER_CACHE,
VODRIP_ARCHIVE_DB, APPDATA, LOCALAPPDATA, TEMP), so tests run against scratch
dirs and never touch the real user profile. Handlers are sync ``def`` so the
blocking scandir/rmtree work runs in FastAPI's threadpool.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.disk_hygiene import (
    active_whisper_model_id,
    prune_inactive_whisper_models,
    whisper_cache_dir,
)
from services.settings import _get_appdata_dir

router = APIRouter(tags=["disk"])

FREE_THRESHOLD_BYTES = 5 * 1024**3  # low-disk warning below 5 GB free
DEFAULT_KEEP_COUNT = 5

CLEANABLE = ("archive_vods", "whisper_models", "preview_cache", "update_temps")


class CleanupRequest(BaseModel):
    category: str


# --- path resolution (mirrors each service's own env knob) -----------------

def _archive_dir() -> Path:
    override = os.environ.get("VODRIP_ARCHIVE_DIR", "").strip()
    if override:
        return Path(override)
    return _get_appdata_dir() / "archive"


def _whisper_cache_dir() -> Path:
    # Shared resolver (services.disk_hygiene): VODRIP_WHISPER_CACHE env ->
    # settings.whisper_model_cache -> %APPDATA%/VOD.RIP/whisper-models.
    return whisper_cache_dir()


def _db_path() -> Path:
    override = os.environ.get("VODRIP_ARCHIVE_DB", "").strip()
    if override:
        return Path(override)
    return _get_appdata_dir() / "archive.db"


def _bgutil_dir() -> Path:
    # ponytail: mirrors youtube_pot_service._pot_server_dir (LOCALAPPDATA on
    # Windows, TEMP elsewhere); importing that module drags in yt-dlp deps.
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "VOD.RIP" / "bgutil-pot"
    return Path(os.environ.get("TEMP") or tempfile.gettempdir()) / "VOD.RIP" / "bgutil-pot"


def _category_paths() -> dict[str, list[Path]]:
    db = _db_path()
    return {
        "archive_vods": [_archive_dir()],
        "whisper_models": [_whisper_cache_dir()],
        "db": [db, Path(str(db) + "-wal"), Path(str(db) + "-shm")],
        "logs": [_get_appdata_dir() / "logs", _bgutil_dir() / "pot-server.log"],
        "preview_cache": [Path(tempfile.gettempdir()) / "kd_preview"],
        "update_temps": [Path(tempfile.gettempdir()) / "VOD.RIP-Updates"],
    }


# --- sizing ----------------------------------------------------------------

def _dir_size(root: Path) -> int:
    """Recursive scandir sum; no symlink following, errors treated as 0.

    ponytail: O(n) single pass per request is fine at these sizes; if the
    archive ever grows past ~100k files, cache sizes with an mtime index.
    """
    total = 0
    try:
        with os.scandir(root) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        total += _dir_size(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _path_size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            return _dir_size(path)
    except OSError:
        pass
    return 0


# --- deletion helpers ------------------------------------------------------

def _delete_entry(path: Path) -> int:
    """Delete a file or dir tree; returns pre-delete size (0 if gone/failed)."""
    try:
        if path.is_dir() and not path.is_symlink():
            size = _dir_size(path)
            shutil.rmtree(path, ignore_errors=True)
            return size if not path.exists() else 0
        if path.is_file():
            size = path.stat().st_size
            path.unlink()
            return size
    except OSError:
        pass
    return 0


def _delete_contents(root: Path) -> int:
    """Delete a dir's entries but keep the root itself (recreatable caches)."""
    freed = 0
    if not root.is_dir():
        return 0
    for entry in root.iterdir():
        freed += _delete_entry(entry)
    return freed


# --- category cleanups -----------------------------------------------------

def _keep_count() -> int:
    try:
        from deps import settings_mgr

        return int(getattr(settings_mgr.get(), "archive_vod_keep_count", DEFAULT_KEEP_COUNT))
    except Exception:
        return DEFAULT_KEEP_COUNT


def _cleanup_archive_vods() -> int:
    """Evict old archive VODs via the shared retention service (DB-driven:
    files are deleted only when their rows exist and are beyond the keep
    count); returns bytes freed (dir-size before/after)."""
    from services.archive_retention import enforce_archive_vod_retention

    root = _archive_dir()
    before = _dir_size(root)
    enforce_archive_vod_retention(keep_count=_keep_count())
    return max(0, before - _dir_size(root))


# --- routes ----------------------------------------------------------------

@router.get("/api/disk/usage")
def disk_usage() -> dict[str, int]:
    """Per-category bytes under the app's data dirs."""
    usage: dict[str, int] = {}
    total = 0
    for cat, paths in _category_paths().items():
        cat_bytes = sum(_path_size(p) for p in paths)
        usage[cat] = cat_bytes
        total += cat_bytes
    usage["total"] = total
    return usage


@router.get("/api/disk/status")
def disk_status() -> dict:
    """Free space on the data drive + configured VOD retention count."""
    free = _free_bytes()
    return {
        "free_bytes": free,
        "threshold_bytes": FREE_THRESHOLD_BYTES,
        "low": free < FREE_THRESHOLD_BYTES,
        "keep_count": _keep_count(),
    }


@router.post("/api/disk/cleanup")
def disk_cleanup(req: CleanupRequest) -> dict[str, int]:
    """One-click cleanup per category. Returns freed_bytes (pre-delete size)."""
    cat = req.category
    if cat == "archive_vods":
        freed = _cleanup_archive_vods()
    elif cat == "whisper_models":
        freed = prune_inactive_whisper_models(
            _whisper_cache_dir(), active_whisper_model_id()
        )
    elif cat == "preview_cache":
        freed = _delete_contents(Path(tempfile.gettempdir()) / "kd_preview")
    elif cat == "update_temps":
        freed = _delete_contents(Path(tempfile.gettempdir()) / "VOD.RIP-Updates")
    else:
        raise HTTPException(status_code=400, detail=f"unsupported category: {cat}")
    return {"freed_bytes": freed}


def _free_bytes() -> int:
    """Free bytes on the drive holding the app data dir (climb to an
    existing ancestor so scratch APPDATA paths that don't exist yet work)."""
    path = _get_appdata_dir()
    while not path.exists():
        parent = path.parent
        if parent == path:
            break
        path = parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0
