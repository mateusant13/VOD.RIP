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
# ffmpeg auto-detection (Windows, macOS, Linux)
# ---------------------------------------------------------------------------

def _find_ffmpeg() -> Optional[str]:
    """Locate ffmpeg on the system — returns the BIN DIRECTORY, not the exe.

    yt-dlp's ``ffmpeg_location`` option is most reliable when given the
    *directory* that contains *both* ``ffmpeg.exe`` and ``ffprobe.exe``,
    rather than the executable file itself.
    """
    # 1. Check PATH
    exe = shutil.which("ffmpeg")
    if exe:
        return str(Path(exe).parent)

    def _check_bin(parent: Path) -> Optional[str]:
        """If *parent* contains ffmpeg.exe or ffmpeg, return *parent* as str."""
        if not parent.is_dir():
            return None
        for f in parent.iterdir():
            if f.name.lower() in ("ffmpeg.exe", "ffmpeg"):
                return str(parent)
        return None

    # 2. Common install locations (Windows)
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "ffmpeg" / "bin",
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "FFmpeg" / "bin",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "ffmpeg" / "bin",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "FFmpeg" / "bin",
        Path.home() / "scoop" / "apps" / "ffmpeg" / "current" / "bin",
        Path("/usr/bin"),
        Path("/usr/local/bin"),
    ]

    for c in candidates:
        result = _check_bin(c)
        if result:
            return result

    # 3. winget installs into a versioned subfolder under Packages
    winget_root = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if winget_root.is_dir():
        # Look for any subdirectory tree that contains ffmpeg.exe
        for sub in winget_root.rglob("ffmpeg.exe"):
            return str(sub.parent)
        # Fallback: check direct children that look like ffmpeg packages
        for pkg_dir in winget_root.iterdir():
            if pkg_dir.is_dir() and "ffmpeg" in pkg_dir.name.lower():
                # Search within the package directory for bin/
                for bin_dir in pkg_dir.rglob("bin"):
                    result = _check_bin(bin_dir)
                    if result:
                        return result

    return None


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

    resolved_ffmpeg = ffmpeg_path or _find_ffmpeg()
    if resolved_ffmpeg:
        # yt-dlp is most reliable when given a directory, not an executable.
        opts["ffmpeg_location"] = (
            resolved_ffmpeg
            if os.path.isdir(resolved_ffmpeg)
            else os.path.dirname(resolved_ffmpeg)
        )

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

    platform = detect_platform(full_url)
    is_hls = platform in ("Twitch", "Kick")

    # Determine crop bounds
    need_crop = (
        crop_start is not None
        and crop_end is not None
        and crop_end > crop_start
    )

    if need_crop and is_hls:
        # ── HLS optimisation ──────────────────────────────────────────────
        # download_sections on HLS (Twitch/Kick) forces yt-dlp into the
        # FFmpeg downloader path which is known to hang on large VODs.
        # Instead we download the full stream first, then clip with FFmpeg.
        #
        # 1) Download whole VOD (no sections)
        _do_full_download(full_url, output_path, opts)

        # 2) FFmpeg-clip the requested range into a temp file, then swap.
        _ffmpeg_clip(output_path, crop_start, crop_end, opts)

    elif need_crop:
        # Non-HLS platform — download_sections is fine
        start = _format_ts(crop_start)
        end = _format_ts(crop_end)
        opts["download_sections"] = [f"*{start}-{end}"]
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([full_url])
    elif crop_start is not None and crop_end is None:
        start = _format_ts(crop_start)
        opts["download_sections"] = [f"*{start}-+{DEFAULT_CLIP_SECONDS}"]
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([full_url])
    elif crop_end is not None and crop_start is None:
        end = _format_ts(crop_end)
        opts["download_sections"] = [f"*-{end}"]
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([full_url])
    else:
        # No crop — download the entire video
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([full_url])

    if not os.path.isfile(output_path):
        raise RuntimeError(f"Download failed: output file not found at {output_path}")

    return output_path


def _do_full_download(url: str, output_path: str, opts: dict) -> None:
    """Download the full VOD without any section cropping."""
    clean = {k: v for k, v in opts.items() if k != "download_sections"}
    with yt_dlp.YoutubeDL(clean) as ydl:
        ydl.download([url])


def _ffmpeg_clip(full_path: str, start: float, end: float, opts: dict) -> None:
    """Use ffmpeg to extract *start*-*end* from *full_path* in-place.

    The clipped file replaces the original.
    """
    import subprocess as sp
    import tempfile

    ffmpeg_dir = opts.get("ffmpeg_location")
    ffmpeg_exe = str(Path(ffmpeg_dir) / "ffmpeg.exe") if ffmpeg_dir else "ffmpeg"

    tmp = tempfile.mktemp(suffix=".mp4", prefix="clip_")
    duration = end - start

    cmd = [
        ffmpeg_exe,
        "-ss", str(start),
        "-i", full_path,
        "-t", str(duration),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-y",
        tmp,
    ]
    try:
        sp.run(cmd, check=True, capture_output=True, timeout=600)
    except sp.CalledProcessError as exc:
        raise RuntimeError(
            f"FFmpeg clipping failed (start={start}, end={end}):\n"
            f"  stderr: {exc.stderr.decode(errors='replace')[:500]}"
        ) from exc

    # Swap clipped file with original
    os.replace(tmp, full_path)


def _format_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
