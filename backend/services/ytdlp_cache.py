"""
Cache directory management for yt-dlp — manages the `.cache/KickDownloader/yt-dlp-cache` directory.
"""
import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def _get_cache_dir() -> Path:
    """Return the dedicated yt-dlp cache directory for this application.
    If the user's `Path.home()` produces a path that can't be created
    (e.g. the home dir is a UNC share the OS won't create child
    directories on, or has illegal characters in it), fall back to the
    system temp dir. This is the most common cause of `[Errno 22]
    Invalid argument` users see when fetching a VOD.
    """
    base = Path.home() / ".cache" / "KickDownloader"
    try:
        base.mkdir(parents=True, exist_ok=True)
        return base / "yt-dlp-cache"
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "KickDownloader" / "yt-dlp-cache"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback

def _prune_cache_dir(cache_dir: Path, max_bytes: int) -> None:
    """Remove oldest entries from *cache_dir* until its size is <= *max_bytes*."""
    if not cache_dir.is_dir():
        return

    entries = []
    for entry in cache_dir.iterdir():
        try:
            atime = entry.stat().st_atime
        except OSError:
            continue
        if entry.is_file():
            size = entry.stat().st_size
        elif entry.is_dir():
            size = _dir_size(entry)
        else:
            continue
        entries.append((atime, entry, size))

    entries.sort(key=lambda t: t[0])

    current_size = sum(size for _, _, size in entries)
    while entries and current_size > max_bytes:
        atime, oldest, oldest_size = entries.pop(0)
        try:
            if oldest.is_dir():
                shutil.rmtree(oldest)
            else:
                oldest.unlink()
            current_size -= oldest_size
        except OSError:
            pass

def _dir_size(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for fname in files:
                fpath = Path(root) / fname
                try:
                    total += fpath.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total
