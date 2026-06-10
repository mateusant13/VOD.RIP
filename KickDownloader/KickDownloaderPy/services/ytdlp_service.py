"""yt-dlp service — wraps the yt-dlp Python library directly (no subprocess)."""

import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import subprocess as sp
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional, Tuple
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

# Clips shorter than this are almost certainly a broken ftyp-only placeholder.
MIN_VALID_OUTPUT_BYTES = 50_000
SEGMENT_DOWNLOAD_WORKERS = 8

import requests
import yt_dlp

from models.schemas import VideoInfo


class CancelledError(Exception):
    """Raised when a download is cancelled by the user."""
    pass


def _check_cancelled(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event and cancel_event.is_set():
        raise CancelledError("Download cancelled by user")


def _run_ffmpeg(
    cmd: list,
    cancel_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
) -> None:
    """Run ffmpeg, polling *cancel_event* so cancel can kill the process quickly."""
    proc = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE)
    if register_abort:
        register_abort(lambda: proc.poll() is None and proc.kill())
    try:
        while proc.poll() is None:
            _check_cancelled(cancel_event)
            time.sleep(0.05)
        stderr = proc.stderr.read() if proc.stderr else b""
        if proc.returncode != 0:
            raise RuntimeError(
                f"FFmpeg failed (exit {proc.returncode}): {stderr.decode(errors='ignore')[:500]}"
            )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def _normalize_crop_range(
    crop_start: Optional[float],
    crop_end: Optional[float],
) -> Optional[Tuple[float, float]]:
    """Return (start, end) when a valid trim range is present (start may be 0)."""
    if crop_end is None:
        return None
    start = 0.0 if crop_start is None else float(crop_start)
    end = float(crop_end)
    if end <= start:
        return None
    return start, end


def _require_crop_range(
    crop_start: Optional[float],
    crop_end: Optional[float],
) -> Tuple[float, float]:
    """Require an explicit trim window; never fall back to a full-VOD download."""
    if (crop_start is None) != (crop_end is None):
        raise ValueError(
            "Both crop_start and crop_end are required for Twitch/Kick downloads"
        )
    crop = _normalize_crop_range(crop_start, crop_end)
    if crop is None:
        raise ValueError(
            "crop_start and crop_end are required (crop_end must be after crop_start)"
        )
    return crop


def _resolve_ffmpeg_exe(ffmpeg_dir: Optional[str] = None) -> str:
    if ffmpeg_dir:
        exe = Path(ffmpeg_dir) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if exe.is_file():
            return str(exe)
    found = _find_ffmpeg()
    if found:
        exe = Path(found) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if exe.is_file():
            return str(exe)
    return "ffmpeg"


def is_clip_url(url: str) -> bool:
    """True for Twitch/Kick clip pages (not full VODs)."""
    u = (url or "").lower()
    if "clips.twitch.tv" in u:
        return True
    if "twitch.tv" in u and "/clip/" in u:
        return True
    if "kick.com" in u and "/clips/" in u:
        return True
    return False


def detect_platform(url: str) -> str:
    url_lower = url.lower()
    if "clips.twitch.tv" in url_lower or "twitch.tv" in url_lower:
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

def _qualities_from_formats(formats: list, is_clip: bool = False) -> list[str]:
    """Build sorted quality labels (e.g. 1080p) from yt-dlp format entries."""
    qualities: list[str] = []
    for f in formats:
        h = f.get("height")
        if not h or h <= 0:
            continue
        fid = (f.get("format_id") or "")
        fid_lower = fid.lower()
        if fid_lower.startswith("portrait"):
            continue
        if "Audio" in fid:
            continue
        ext = (f.get("ext") or "").lower()
        vcodec = f.get("vcodec") or "none"
        if is_clip:
            if ext not in ("mp4", "m4v", "mov", "webm"):
                continue
        elif vcodec == "none":
            continue
        fps = f.get("fps")
        fps_suffix = "60" if fps and fps > 30 else ""
        label = f"{int(h)}p{fps_suffix}"
        if label not in qualities:
            qualities.append(label)
    qualities.sort(key=lambda q: int(re.search(r"\d+", q).group()), reverse=True)
    return qualities


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
    qualities = _qualities_from_formats(formats, is_clip_url(full_url))

    created_at = info.get("upload_date")
    if not created_at and info.get("timestamp"):
        try:
            from datetime import datetime, timezone
            created_at = datetime.fromtimestamp(
                float(info["timestamp"]), tz=timezone.utc
            ).isoformat()
        except (TypeError, ValueError, OSError):
            created_at = None

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
        created_at=created_at,
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

    # UI default is "source" — not a valid yt-dlp format id; omit so HLS clip
    # extraction picks the best m3u8 variant (same as test_downloads.py).
    if quality and quality.lower() != "source":
        m = re.search(r"(\d+)p?", quality)
        if m:
            height = m.group(1)
            opts["format"] = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
        elif quality not in ("best", "worst"):
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

# Keys from _build_ydl_opts that should be forwarded to _extract_hls_info
_HLS_FORWARD_KEYS = frozenset({
    "format", "username", "password",
    "cachedir", "quiet", "no_warnings",
})


def _extract_hls_info(url: str, opts: dict) -> dict:
    """Use yt-dlp to get HLS info without downloading, passing auth etc."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    for key in _HLS_FORWARD_KEYS:
        if key in opts:
            ydl_opts[key] = opts[key]
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


def _find_hls_format(info: dict) -> dict:
    """Pick the best HLS (m3u8) format matching the user's quality preference.

    The *info* dict is expected to come from ``_extract_hls_info`` which
    was called with a ``format`` opt that may contain a height constraint
    such as ``[height<=720]``.  This function honours that constraint.
    """
    formats = info.get("formats") or []
    hls_formats = [
        f for f in formats
        if f.get("protocol") in ("m3u8", "m3u8_native", "m3u8_ffmpeg")
    ]
    if not hls_formats:
        raise RuntimeError("No HLS format found in video info")

    # If the caller already passed a format string with height constraint,
    # yt-dlp's extract_info will have already filtered the results.
    # We just pick the tallest / highest-bitrate HLS entry from what's left.
    hls_formats.sort(
        key=lambda f: (f.get("height") or 0, f.get("tbr") or 0),
        reverse=True,
    )
    return hls_formats[0]


def _parse_prefer_height(quality: Optional[str], default: int = 720) -> int:
    if not quality or quality.lower() == "source":
        return default
    m = re.search(r"(\d+)", quality)
    return int(m.group(1)) if m else default


def _resolve_media_playlist(
    media_url: str,
    headers: dict,
    prefer_height: int = 720,
) -> str:
    """Follow HLS master playlists (Kick IVS, Twitch variants) to a media playlist."""
    r = requests.get(media_url, headers=headers, timeout=30)
    r.raise_for_status()
    text = r.text
    if "#EXTINF:" in text:
        return media_url

    variants: list[tuple[int, int, str]] = []
    lines = text.splitlines()
    pending_bandwidth = 0
    pending_height = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#EXT-X-STREAM-INF"):
            bw_m = re.search(r"BANDWIDTH=(\d+)", stripped)
            res_m = re.search(r"RESOLUTION=(\d+)x(\d+)", stripped)
            pending_bandwidth = int(bw_m.group(1)) if bw_m else 0
            pending_height = int(res_m.group(2)) if res_m else 0
            continue
        if stripped and not stripped.startswith("#"):
            variants.append((pending_height, pending_bandwidth, urljoin(media_url, stripped)))
            pending_bandwidth = 0
            pending_height = 0

    if not variants:
        raise RuntimeError(f"No HLS media playlist found at {media_url}")

    with_height = [v for v in variants if v[0] > 0]
    if with_height:
        exact = [v for v in with_height if v[0] == prefer_height]
        if exact:
            chosen = max(exact, key=lambda item: item[1])
        else:
            at_or_below = [v for v in with_height if v[0] <= prefer_height]
            if at_or_below:
                chosen = max(at_or_below, key=lambda item: item[0])
            else:
                chosen = min(with_height, key=lambda item: item[0])
    else:
        chosen = max(variants, key=lambda item: item[1])

    return _resolve_media_playlist(chosen[2], headers, prefer_height)


def _parse_m3u8(
    media_url: str,
    headers: dict,
    prefer_height: int = 720,
) -> list[dict]:
    """Download and parse an HLS media playlist, return list of segment dicts."""
    media_url = _resolve_media_playlist(media_url, headers, prefer_height)
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


def _download_one_segment(
    index: int,
    seg: dict,
    headers: dict,
    temp_dir: str,
    cancel_event: Optional[threading.Event],
) -> str:
    if cancel_event and cancel_event.is_set():
        raise CancelledError("Download cancelled by user")

    path = os.path.join(temp_dir, f"{index:05d}.ts")
    r = requests.get(seg["url"], headers=headers, stream=True, timeout=60)
    r.raise_for_status()
    with open(path, "wb") as f:
        for chunk in r.iter_content(256 * 1024):
            _check_cancelled(cancel_event)
            if chunk:
                f.write(chunk)
    size = os.path.getsize(path)
    if size < 1024:
        raise RuntimeError(f"HLS segment {index} is too small ({size} bytes)")
    return path


def _download_segments(
    segments: list[dict],
    headers: dict,
    temp_dir: str,
    progress_hook: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
) -> list[str]:
    """Download HLS segment files into *temp_dir* (parallel)."""
    total = len(segments)
    if total == 0:
        raise RuntimeError("No HLS segments to download")

    files: list[Optional[str]] = [None] * total
    completed = 0
    workers = min(SEGMENT_DOWNLOAD_WORKERS, total)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _download_one_segment, i, seg, headers, temp_dir, cancel_event,
            ): i
            for i, seg in enumerate(segments)
        }
        for fut in as_completed(futures):
            index = futures[fut]
            files[index] = fut.result()
            completed += 1
            if progress_hook:
                progress_hook({
                    "status": "downloading",
                    "percent": completed / total * 100.0,
                })

    return [path for path in files if path]


def _atomic_replace(src: str, dst: str) -> None:
    """Move *src* to *dst*, falling back to copy on cross-drive Windows paths."""
    try:
        os.replace(src, dst)
    except OSError as exc:
        winerr = getattr(exc, "winerror", None)
        if winerr not in (17, 18) and exc.errno not in (18,):
            raise
        shutil.copy2(src, dst)
        os.remove(src)


def _verify_output_file(path: str) -> None:
    if not os.path.isfile(path):
        raise RuntimeError(f"Output file missing: {path}")
    size = os.path.getsize(path)
    if size < MIN_VALID_OUTPUT_BYTES:
        try:
            os.remove(path)
        except OSError:
            pass
        raise RuntimeError(
            f"Output file too small ({size} bytes); download incomplete"
        )


def _concat_and_trim(
    files: list[str],
    output_path: str,
    offset: float,
    duration: float,
    ffmpeg_exe: str,
    progress_hook: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
) -> None:
    """Concatenate HLS segments and trim in one lossless ffmpeg pass."""
    tmp_dir = os.path.dirname(files[0])
    concat_txt = os.path.join(tmp_dir, "concat.txt")
    tmp_out = os.path.join(tmp_dir, f"clip_{uuid.uuid4().hex}.mp4")

    with open(concat_txt, "w", encoding="utf8") as f:
        for seg_path in files:
            posix = seg_path.replace("\\", "/")
            f.write(f"file '{posix}'\n")

    if progress_hook:
        progress_hook({"status": "downloading", "percent": 92})

    cmd = [
        ffmpeg_exe, "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", concat_txt,
    ]
    if offset > 0.001:
        cmd += ["-ss", str(offset)]
    cmd += [
        "-t", str(duration),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-f", "mp4",
        tmp_out,
    ]
    _run_ffmpeg(
        cmd,
        cancel_event=cancel_event,
        register_abort=register_abort,
    )
    _verify_output_file(tmp_out)

    if progress_hook:
        progress_hook({"status": "downloading", "percent": 98})

    # Atomic replace when possible; copy fallback for cross-drive temp (Windows).
    _atomic_replace(tmp_out, output_path)
    _verify_output_file(output_path)

    if progress_hook:
        progress_hook({"status": "downloading", "percent": 100})
        progress_hook({"status": "finished"})


def download_hls_media_clip(
    media_url: str,
    start_sec: float,
    end_sec: float,
    output_path: str,
    headers: Optional[dict] = None,
    ffmpeg_exe: Optional[str] = None,
    progress_hook: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
    prefer_height: int = 720,
) -> None:
    """Download an HLS media playlist clip by segment (Kick m3u8 URL or Twitch variant)."""
    headers = headers or {}
    duration = end_sec - start_sec
    segments = _parse_m3u8(media_url, headers, prefer_height)
    selected, first_offset = _select_segments(segments, start_sec, end_sec)
    selected_duration = sum(s["duration"] for s in selected)
    logger.info(
        "HLS clip %.2f-%.2fs: %d segments, %.1fs media (offset %.2fs)",
        start_sec, end_sec, len(selected), selected_duration, first_offset,
    )
    if selected_duration > duration * 2 + 120:
        logger.warning(
            "HLS clip selected %.1fs of media for %.1fs trim — check segment selection",
            selected_duration, duration,
        )

    resolved_ffmpeg = ffmpeg_exe or _resolve_ffmpeg_exe()

    out_parent = os.path.dirname(os.path.abspath(output_path))
    if out_parent:
        os.makedirs(out_parent, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hls_clip_", dir=out_parent or None) as tmpdir:
        files = _download_segments(
            selected, headers, tmpdir, progress_hook, cancel_event,
        )
        _concat_and_trim(
            files, output_path, first_offset, duration, resolved_ffmpeg,
            progress_hook,
            cancel_event=cancel_event,
            register_abort=register_abort,
        )


def _download_hls_clip(
    url: str,
    output_path: str,
    start_sec: float,
    end_sec: float,
    opts: dict,
    progress_hook: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
    prefer_height: int = 720,
) -> None:
    """Download only the HLS segments covering *start_sec*–*end_sec*."""
    info = _extract_hls_info(url, opts)
    fmt = _find_hls_format(info)
    media_url = fmt["url"]
    headers = fmt.get("http_headers") or info.get("http_headers") or {}
    ffmpeg_exe = _resolve_ffmpeg_exe(opts.get("ffmpeg_location"))

    download_hls_media_clip(
        media_url, start_sec, end_sec, output_path, headers=headers,
        ffmpeg_exe=ffmpeg_exe,
        progress_hook=progress_hook,
        cancel_event=cancel_event,
        register_abort=register_abort,
        prefer_height=prefer_height,
    )


# ---------------------------------------------------------------------------
# Main download entry point
# ---------------------------------------------------------------------------

def _wrap_progress_hook(
    progress_hook: Optional[Callable],
    cancel_event: Optional[threading.Event],
) -> Optional[Callable]:
    if not progress_hook and not cancel_event:
        return progress_hook

    def hook(d):
        _check_cancelled(cancel_event)
        if progress_hook:
            progress_hook(d)

    return hook


def _ydl_download(
    url: str,
    opts: dict,
    cancel_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
) -> None:
    ydl = yt_dlp.YoutubeDL(opts)
    if register_abort:
        register_abort(lambda: getattr(ydl, "cancel_download", lambda: None)())
    try:
        ydl.download([url])
    finally:
        _check_cancelled(cancel_event)


def download_video_sync(
    url: str,
    output_path: str,
    quality: Optional[str] = None,
    oauth: Optional[str] = None,
    crop_start: Optional[float] = None,
    crop_end: Optional[float] = None,
    progress_hook: Optional[Callable] = None,
    settings_mgr=None,
    cancel_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
) -> str:
    """Download a video or clip. Called from the download manager's worker thread."""
    full_url = build_url(url)
    progress_hook = _wrap_progress_hook(progress_hook, cancel_event)

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
    is_hls = platform in ("Twitch", "Kick") and not is_clip_url(full_url)

    if is_hls:
        start_sec, end_sec = _require_crop_range(crop_start, crop_end)
        _download_hls_clip(
            full_url, output_path, start_sec, end_sec, opts,
            progress_hook=progress_hook,
            cancel_event=cancel_event,
            register_abort=register_abort,
            prefer_height=_parse_prefer_height(quality),
        )
    else:
        crop = _normalize_crop_range(crop_start, crop_end)
        if crop_start is not None or crop_end is not None:
            if crop is None:
                raise ValueError(
                    "Both crop_start and crop_end are required for trimmed downloads"
                )
            start = _format_ts(crop[0])
            end = _format_ts(crop[1])
            opts["download_sections"] = [f"*{start}-{end}"]
        _ydl_download(full_url, opts, cancel_event, register_abort)

    _verify_output_file(output_path)
    return output_path


def _format_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
