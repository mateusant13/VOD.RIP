"""yt-dlp service — wraps the yt-dlp Python library directly (no subprocess)."""

import os
import re
import shutil
import tempfile
import threading
import subprocess as sp
from pathlib import Path
from typing import Optional, Callable
from urllib.parse import urljoin

import requests
import yt_dlp

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


# ---------------------------------------------------------------------------
# ffmpeg auto-detection (Windows, macOS, Linux)
# ---------------------------------------------------------------------------

def _find_ffmpeg() -> Optional[str]:
    """Locate ffmpeg — returns the BIN DIRECTORY (not the exe).

    yt-dlp's ``ffmpeg_location`` and our clip pipeline both need the
    *directory* that contains *both* ``ffmpeg.exe`` and ``ffprobe.exe``.
    """
    exe = shutil.which("ffmpeg")
    if exe:
        return str(Path(exe).parent)

    def _check_bin(parent: Path) -> Optional[str]:
        if not parent.is_dir():
            return None
        for f in parent.iterdir():
            if f.name.lower() in ("ffmpeg.exe", "ffmpeg"):
                return str(parent)
        return None

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

    winget_root = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if winget_root.is_dir():
        for sub in winget_root.rglob("ffmpeg.exe"):
            return str(sub.parent)
        for pkg_dir in winget_root.iterdir():
            if pkg_dir.is_dir() and "ffmpeg" in pkg_dir.name.lower():
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
        opts["ratelimit"] = throttle_kib * 1024

    if temp_folder:
        opts["paths"] = {"home": temp_folder}

    resolved_ffmpeg = ffmpeg_path or _find_ffmpeg()
    if resolved_ffmpeg:
        opts["ffmpeg_location"] = (
            resolved_ffmpeg
            if os.path.isdir(resolved_ffmpeg)
            else os.path.dirname(resolved_ffmpeg)
        )

    return opts


# ---------------------------------------------------------------------------
# Segment-level HLS download (TwitchDownloader-style)
# ---------------------------------------------------------------------------

def _extract_hls_info(url: str, opts: dict) -> dict:
    """Use yt-dlp to get HLS info without downloading."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    if "format" in opts:
        ydl_opts["format"] = opts["format"]
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


def _find_hls_format(info: dict) -> dict:
    """Pick the best HLS (m3u8) format from extracted info."""
    formats = info.get("formats") or []
    hls_formats = [
        f for f in formats
        if f.get("protocol") in ("m3u8", "m3u8_native", "m3u8_ffmpeg")
    ]
    if not hls_formats:
        raise RuntimeError("No HLS format found in video info")
    hls_formats.sort(
        key=lambda f: (f.get("height") or 0, f.get("tbr") or 0),
        reverse=True,
    )
    return hls_formats[0]


def _parse_m3u8(media_url: str, headers: dict) -> list[dict]:
    """Download and parse an HLS media playlist, return list of segment dicts."""
    r = requests.get(media_url, headers=headers, timeout=30)
    r.raise_for_status()
    lines = r.text.splitlines()

    segments = []
    current_duration = None
    for line in lines:
        line = line.strip()
        if line.startswith("#EXTINF:"):
            current_duration = float(line.split(":")[1].split(",")[0])
        elif line and not line.startswith("#") and current_duration is not None:
            segments.append({
                "duration": current_duration,
                "url": urljoin(media_url, line),
            })
            current_duration = None
    return segments


def _select_segments(
    segments: list[dict],
    start_sec: float,
    end_sec: float,
) -> tuple[list[dict], float]:
    """Select HLS segments overlapping [start_sec, end_sec).

    Returns (selected_segments, offset_into_first_segment).
    """
    selected = []
    pos = 0.0
    first_offset = 0.0
    for seg in segments:
        seg_start = pos
        seg_end = pos + seg["duration"]
        if seg_end > start_sec and seg_start < end_sec:
            if not selected:
                first_offset = max(0.0, start_sec - seg_start)
            selected.append(seg)
        pos = seg_end
        if seg_start >= end_sec:
            break
    if not selected:
        raise RuntimeError(f"No HLS segments found in range {start_sec}-{end_sec}")
    return selected, first_offset


def _download_segments(
    segments: list[dict],
    headers: dict,
    temp_dir: str,
    progress_hook: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
) -> list[str]:
    """Download HLS segment files into *temp_dir*.

    Supports cancellation (via *cancel_event*) and progress reporting
    (via *progress_hook*, called with yt-dlp-style progress dicts).
    """
    files = []
    total = len(segments)
    for i, seg in enumerate(segments):
        # Check cancellation before each segment
        if cancel_event and cancel_event.is_set():
            raise CancelledError("Download cancelled by user")

        path = os.path.join(temp_dir, f"{i:05d}.ts")
        r = requests.get(seg["url"], headers=headers, stream=True, timeout=60)
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if cancel_event and cancel_event.is_set():
                    raise CancelledError("Download cancelled by user")
                if chunk:
                    f.write(chunk)
        files.append(path)

        # Report progress
        if progress_hook:
            pct = int((i + 1) / total * 100)
            progress_hook({
                "status": "downloading",
                "total_bytes": total,
                "downloaded_bytes": i + 1,
                "_percent": pct,
                "_speed_str": "",
                "_eta_str": "",
            })
    return files


def _concat_and_trim(
    files: list[str],
    output_path: str,
    offset: float,
    duration: float,
    ffmpeg_exe: str,
    progress_hook: Optional[Callable] = None,
) -> None:
    """Losslessly concatenate segment files, then trim to exact duration."""
    tmp_dir = os.path.dirname(files[0])
    joined = os.path.join(tmp_dir, "joined.ts")
    concat_txt = os.path.join(tmp_dir, "concat.txt")

    with open(concat_txt, "w", encoding="utf8") as f:
        for seg_path in files:
            # Use forward slashes on Windows (ffmpeg concat demuxer handles them)
            posix = seg_path.replace("\\", "/")
            f.write(f"file '{posix}'\n")

    sp.run(
        [ffmpeg_exe, "-f", "concat", "-safe", "0", "-i", concat_txt,
         "-c", "copy", "-y", joined],
        check=True, capture_output=True,
    )

    sp.run(
        [ffmpeg_exe, "-ss", str(offset), "-i", joined,
         "-t", str(duration), "-c", "copy",
         "-avoid_negative_ts", "make_zero", "-y", output_path],
        check=True, capture_output=True,
    )

    # Notify completion
    if progress_hook:
        progress_hook({"status": "finished"})


def _download_hls_clip(
    url: str,
    output_path: str,
    start_sec: float,
    end_sec: float,
    opts: dict,
    progress_hook: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """Download only the HLS segments covering *start_sec*–*end_sec*.

    Uses the same approach as TwitchDownloader: parse the m3u8 playlist,
    download only the needed TS fragments, then concat + trim with ffmpeg.
    No full VOD download — only the segments in the time range.

    Supports cancellation via *cancel_event* and progress via *progress_hook*.
    """
    duration = end_sec - start_sec

    info = _extract_hls_info(url, opts)
    fmt = _find_hls_format(info)
    media_url = fmt["url"]
    headers = fmt.get("http_headers") or info.get("http_headers") or {}

    segments = _parse_m3u8(media_url, headers)
    selected, first_offset = _select_segments(segments, start_sec, end_sec)

    ffmpeg_dir = opts.get("ffmpeg_location")
    ffmpeg_exe = str(Path(ffmpeg_dir) / "ffmpeg.exe") if ffmpeg_dir else "ffmpeg"

    with tempfile.TemporaryDirectory(prefix="hls_clip_") as tmpdir:
        files = _download_segments(selected, headers, tmpdir, progress_hook, cancel_event)
        _concat_and_trim(files, output_path, first_offset, duration, ffmpeg_exe, progress_hook)


# ---------------------------------------------------------------------------
# Main download entry point
# ---------------------------------------------------------------------------

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

    has_crop = (
        crop_start is not None
        and crop_end is not None
        and crop_end > crop_start
    )

    if has_crop and is_hls:
        # ── HLS segment-level clip (TwitchDownloader-style) ──────────────
        # Download ONLY the TS segments covering the time range, concat + trim.
        _download_hls_clip(full_url, output_path, crop_start, crop_end, opts)

    elif has_crop:
        # Non-HLS platform — yt-dlp download_sections works fine
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
        # No crop — let yt-dlp download the full video normally
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
