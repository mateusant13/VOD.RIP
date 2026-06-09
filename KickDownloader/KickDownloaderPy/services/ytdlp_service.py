"""yt-dlp service — wraps the yt-dlp Python library directly (no subprocess)."""

import os
import re
import shutil
import yt_dlp
from pathlib import Path
from typing import Optional, Callable

from models.schemas import VideoInfo


class CancelledError(Exception):
    """Raised when a download is cancelled by the user."""
    pass


# Default section duration when no crop params given (1 hour = 3600 seconds)
DEFAULT_CLIP_SECONDS = 3600


def detect_platform(url: str) -> str:
    url_lower = url.lower()
    if "twitch.tv" in url_lower:
        return "Twitch"
    if "kick.com" in url_lower:
        return "Kick"
    if re.match(r"^\d+$", url.strip()):
        return "Twitch"
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", url.strip()):
        return "Kick"
    return "Unknown"


def build_url(id_or_url: str, platform: Optional[str] = None) -> str:
    detected = platform or detect_platform(id_or_url)
    if id_or_url.startswith("http"):
        return id_or_url
    if detected == "Twitch":
        if re.match(r"^\d+$", id_or_url):
            return f"https://www.twitch.tv/videos/{id_or_url}"
        return f"https://www.twitch.tv/{id_or_url}/videos"
    if detected == "Kick":
        if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", id_or_url):
            return f"https://kick.com/video/{id_or_url}"
        return f"https://kick.com/{id_or_url}/videos"
    return id_or_url


# ---------------------------------------------------------------------------
# Cache directory and pruning
# ---------------------------------------------------------------------------

def _get_cache_dir() -> Path:
    """Return the dedicated yt-dlp cache directory for this application."""
    base = Path.home() / ".cache" / "KickDownloader"
    return base / "yt-dlp-cache"


def _prune_cache_dir(cache_dir: Path, max_bytes: int) -> None:
    """Remove oldest entries from *cache_dir* until its size is <= *max_bytes*.

    Every file and directory directly inside *cache_dir* is a candidate.
    Directories are sized recursively.  Removal order is by last-access time
    (``os.path.getatime``), oldest first.
    """
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

    # Sort ascending so oldest-access comes first.
    entries.sort(key=lambda t: t[0])

    # Track size decrementally instead of re-walking the tree on each iteration.
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
            pass  # best-effort: skip files we cannot remove


def _dir_size(path: Path) -> int:
    """Return total size of all files under *path* (recursive)."""
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


# ---------------------------------------------------------------------------
# Core service
# ---------------------------------------------------------------------------

async def get_video_info(url: str, settings_mgr=None) -> VideoInfo:
    """Extract video metadata without downloading."""
    import asyncio
    full_url = build_url(url)

    cache_dir = _get_cache_dir()
    max_cache_mb = 200
    if settings_mgr is not None:
        max_cache_mb = settings_mgr.get().max_cache_mb
    max_bytes = max_cache_mb * 1024 * 1024
    cache_dir.mkdir(parents=True, exist_ok=True)
    _prune_cache_dir(cache_dir, max_bytes)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "cachedir": str(cache_dir),
    }

    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(full_url, download=False)

    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, _extract)
    if info is None:
        raise ValueError("Could not extract video info")

    formats = info.get("formats", [])
    qualities = []
    for f in formats:
        h = f.get("height")
        fps = f.get("fps")
        vcodec = f.get("vcodec", "none")
        fid = f.get("format_id", "")
        if h and h > 0 and vcodec != "none" and "Audio" not in fid:
            fps_suffix = "60" if fps and fps > 30 else ""
            label = f"{h}p{fps_suffix}"
            if label not in qualities:
                qualities.append(label)
    qualities.sort(key=lambda q: int(re.search(r"\d+", q).group()), reverse=True)

    return VideoInfo(
        id=info.get("id", ""),
        title=info.get("title"),
        duration=info.get("duration"),
        duration_string=info.get("duration_string"),
        uploader=info.get("uploader"),
        thumbnail=info.get("thumbnail"),
        webpage_url=info.get("webpage_url"),
        extractor=info.get("extractor"),
        is_live=info.get("is_live"),
        qualities=qualities,
        platform=detect_platform(full_url),
    )


def _build_ydl_opts(
    url: str,
    output_path: str,
    quality: Optional[str] = None,
    oauth: Optional[str] = None,
    progress_hook: Optional[Callable] = None,
    cachedir: Optional[str] = None,
    throttle_kib: Optional[int] = None,
    temp_folder: Optional[str] = None,
    ffmpeg_path: Optional[str] = None,
) -> dict:
    opts = {
        "outtmpl": output_path,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "no_warnings": True,
        "quiet": True,
    }

    if cachedir:
        opts["cachedir"] = cachedir

    if quality:
        m = re.search(r"(\d+)p?", quality)
        if m:
            height = m.group(1)
            opts["format"] = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
        else:
            opts["format"] = quality

    if oauth:
        opts["username"] = "oauth_token"
        opts["password"] = oauth

    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    if throttle_kib is not None and throttle_kib > 0:
        opts["ratelimit"] = throttle_kib * 1024  # KiB/s to bytes/s

    if temp_folder:
        opts["paths"] = {"home": temp_folder}

    if ffmpeg_path:
        opts["ffmpeg_location"] = ffmpeg_path

    return opts


def download_video_sync(
    url: str,
    output_path: str,
    quality: Optional[str] = None,
    oauth: Optional[str] = None,
    crop_start: Optional[float] = None,
    crop_end: Optional[float] = None,
    progress_hook: Optional[Callable] = None,
    settings_mgr=None,
) -> str:
    """Download a video or clip. Called from the download manager's worker thread."""
    full_url = build_url(url)

    cache_dir = _get_cache_dir()
    max_cache_mb = 200
    if settings_mgr is not None:
        max_cache_mb = settings_mgr.get().max_cache_mb
    max_bytes = max_cache_mb * 1024 * 1024
    cache_dir.mkdir(parents=True, exist_ok=True)
    _prune_cache_dir(cache_dir, max_bytes)

    opts = _build_ydl_opts(
        full_url, output_path, quality, oauth, progress_hook, cachedir=str(cache_dir),
        throttle_kib=settings_mgr.get().throttle_kib if settings_mgr is not None else None,
        temp_folder=settings_mgr.get().temp_folder if settings_mgr is not None else None,
        ffmpeg_path=settings_mgr.get().ffmpeg_path if settings_mgr is not None else None,
    )

    # Download section: defaults to 1 hour (3600s) from start.
    # crop_start/crop_end override this to a custom range.
    if crop_start and crop_start > 0 and crop_end and crop_end > crop_start:
        start = _format_ts(crop_start)
        end = _format_ts(crop_end)
        opts["download_sections"] = [f"*{start}-{end}"]
    elif crop_start and crop_start > 0:
        start = _format_ts(crop_start)
        opts["download_sections"] = [f"*{start}-+{DEFAULT_CLIP_SECONDS}"]
    elif crop_end and crop_end > 0:
        end = _format_ts(crop_end)
        opts["download_sections"] = [f"*-{end}"]
    else:
        opts["download_sections"] = [f"*0-+{DEFAULT_CLIP_SECONDS}"]

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([full_url])

    if not os.path.isfile(output_path):
        raise RuntimeError(f"Download failed: output file not found at {output_path}")

    return output_path


def _format_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
