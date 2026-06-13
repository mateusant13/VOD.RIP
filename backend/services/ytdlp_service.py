"""yt-dlp service — wraps the yt-dlp Python library directly (no subprocess)."""

import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
import subprocess as sp
import subprocess
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Optional, Tuple
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

# Clips shorter than this are almost certainly a broken ftyp-only placeholder.
MIN_VALID_OUTPUT_BYTES = 50_000
SEGMENT_DOWNLOAD_WORKERS = 8
HLS_DOWNLOAD_AHEAD = SEGMENT_DOWNLOAD_WORKERS + 2
HLS_MUX_STALL_SECONDS = 120

# Prevents a Windows console window from popping up around bundled ffmpeg.exe.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# FFmpeg video encoders for H.264 output (Settings → Video encoder).
VIDEO_ENCODER_OPTIONS: dict[str, str] = {
    "auto": "Auto (detect GPU)",
    "copy": "Source codec (fast)",
    "libx264": "H.264 — x264 (software)",
    "h264_nvenc": "H.264 — NVIDIA NVENC",
    "h264_amf": "H.264 — AMD AMF",
    "h264_qsv": "H.264 — Intel Quick Sync",
}


def normalize_video_encoder_setting(value: Optional[str]) -> str:
    """Persisted settings value — ``auto`` is allowed."""
    key = (value or "auto").strip().lower()
    if key == "auto":
        return "auto"
    return normalize_video_encoder(key)


def normalize_video_encoder(value: Optional[str]) -> str:
    key = (value or "libx264").strip().lower()
    if key == "auto":
        return resolve_video_encoder("auto")
    if key not in VIDEO_ENCODER_OPTIONS:
        return "libx264"
    return key


def resolve_video_encoder(value: Optional[str]) -> str:
    """Turn ``auto`` into a concrete ffmpeg encoder id."""
    key = (value or "auto").strip().lower()
    if key != "auto":
        return normalize_video_encoder(key)
    try:
        from services.gpu_detect import get_encoder_detection

        detected = str(get_encoder_detection().get("detected_encoder") or "libx264")
    except Exception:
        detected = "libx264"
    enc = detected.strip().lower()
    if enc in VIDEO_ENCODER_OPTIONS and enc != "auto":
        return enc
    return "libx264"


def _codecs_from_stream_inf(line: str) -> dict:
    """Parse ``#EXT-X-STREAM-INF`` ``CODECS=`` into normalized codec names."""
    m = re.search(r'CODECS="([^"]+)"', line)
    if not m:
        return {}
    return _parse_hls_codecs(m.group(1))


def _parse_hls_codecs(codecs: str) -> dict:
    video_codec = None
    audio_codec = None
    for part in codecs.split(","):
        token = part.strip()
        if token.startswith(("avc1", "avc3")):
            video_codec = "h264"
        elif token.startswith(("hvc1", "hev1")):
            video_codec = "hevc"
        elif token.startswith("av01"):
            video_codec = "av1"
        elif token.startswith("mp4a"):
            audio_codec = "aac"
    return {
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "codecs": codecs,
    }


def can_stream_copy(
    playlist_info: Optional[dict] = None,
    probe_info: Optional[dict] = None,
) -> bool:
    """True when segments can be remuxed (H.264 + AAC + yuv420p)."""
    video = (probe_info or {}).get("video_codec")
    audio = (probe_info or {}).get("audio_codec")
    pix = (probe_info or {}).get("pix_fmt")
    if playlist_info:
        if not video:
            video = playlist_info.get("video_codec")
        if not audio:
            audio = playlist_info.get("audio_codec")
    if video in ("hevc", "av1", "vp9"):
        return False
    if video == "h264" and audio == "aac":
        return pix in (None, "yuv420p")
    return False


def resolve_concat_encoder(
    playlist_info: Optional[dict] = None,
    probe_info: Optional[dict] = None,
    user_encoder: Optional[str] = None,
) -> str:
    """Pick remux (``copy``) vs H.264 transcode from playlist CODECS and/or ffprobe."""
    if can_stream_copy(playlist_info, probe_info):
        return "copy"
    enc = resolve_video_encoder(user_encoder or "auto")
    if enc == "copy":
        enc = resolve_video_encoder("auto")
    return enc


def resolve_hls_concat_encoder(platform: str, user_encoder: Optional[str] = None) -> str:
    """Deprecated — kept for callers that have not migrated to ``resolve_concat_encoder``."""
    _ = platform
    return resolve_concat_encoder(None, None, user_encoder)


def ffmpeg_hwaccel_input_args(encoder: str) -> list[str]:
    """GPU decode flags for single-input transcodes (not concat demuxer).

    The HLS concat path in ``_concat_and_trim`` decodes on CPU and only
    uses the GPU for encode — concat + ``hwaccel_output_format=cuda`` fails
    on Kick's HEVC TS segments (pixel-format mismatch).
    """
    encoder = normalize_video_encoder(encoder)
    if encoder == "h264_nvenc":
        return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    if encoder == "h264_amf":
        return ["-hwaccel", "d3d11va"]
    if encoder == "h264_qsv":
        return ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
    if encoder == "h264_vaapi":
        return ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi"]
    return []


def ffmpeg_h264_encode_args(encoder: str) -> Optional[list[str]]:
    """Return ffmpeg output args for H.264 re-encode, or None for stream copy."""
    encoder = normalize_video_encoder(encoder)
    if encoder == "copy":
        return None
    nle = ["-profile:v", "high", "-pix_fmt", "yuv420p"]
    audio = ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    if encoder == "h264_nvenc":
        # p1 = fastest NVENC preset; long transcodes benefit from GPU speed.
        return [
            "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "hq",
            "-rc", "vbr", "-cq", "23", *nle, *audio,
        ]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "speed", *nle, *audio]
    if encoder == "h264_qsv":
        return [
            "-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "23",
            *nle, *audio,
        ]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", *nle, *audio]

import requests
import yt_dlp
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor
import yt_dlp.postprocessor as _ytdlp_pp_pkg

from models.schemas import VideoInfo


class CancelledError(Exception):
    """Raised when a download is cancelled by the user."""
    pass


class PausedError(Exception):
    """Raised when a download is paused by the user."""
    pass


class DownloadTimeoutError(Exception):
    """Raised when a download exceeds its wall-clock budget."""
    pass


def _check_pause_cancel(
    cancel_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
) -> None:
    if cancel_event and cancel_event.is_set():
        raise CancelledError("Download cancelled by user")
    if pause_event and pause_event.is_set():
        raise PausedError("Download paused by user")


def _check_cancelled(cancel_event: Optional[threading.Event]) -> None:
    _check_pause_cancel(cancel_event, None)


def _ffmpeg_cmd_with_progress(cmd: list) -> list:
    """Insert ffmpeg machine-readable progress flags before the output file."""
    if "-f" in cmd:
        idx = cmd.index("-f")
        return cmd[:idx] + ["-nostats", "-progress", "pipe:2"] + cmd[idx:]
    return cmd[:-1] + ["-nostats", "-progress", "pipe:2", cmd[-1]]


def _parse_speed_multiplier(raw: Optional[str]) -> Optional[float]:
    """Parse ffmpeg's ``speed=1.4x`` string into a float multiplier.

    Returns ``None`` when the value is missing, unparseable, or "N/A" so
    callers can fall back to the elapsed-based estimate.
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().rstrip("x").rstrip("X").strip()
    if not s or s.upper() == "N/A":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _phase_id(label: str) -> str:
    """Map a free-text phase label to a stable, lowercase id for UI logic.

    Keeping the id in lockstep with the label means a typo in either one
    is a single edit point, and the UI can ``switch (phase_id)`` instead
    of doing fragile regex matching on the displayed string.
    """
    s = (label or "").lower().rstrip("…").rstrip(".").strip()
    if "remux" in s:
        return "remuxing"
    if "finalis" in s:
        return "finalising"
    if "mux" in s:
        return "muxing"
    if "merg" in s:
        return "merging"
    return "encoding"


def _run_ffmpeg(
    cmd: list,
    cancel_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
    progress_hook: Optional[Callable] = None,
    encode_duration: Optional[float] = None,
    progress_from: float = 85.0,
    progress_to: float = 99.0,
    phase: str = "Encoding",
) -> None:
    """Run ffmpeg, polling *cancel_event* / *pause_event* so stop can kill the process.

    Emits ``progress_hook`` events with a percent in the ``progress_from → progress_to``
    range (default 85 → 99 — leaves 1% for the post-ffmpeg "Finalising…" window
    so the bar never appears stuck at 100). The hook also receives ``speed``
    (a string like ``"1.4x"``) and ``eta`` (seconds remaining) when both can
    be derived from ffmpeg's ``-progress pipe:2`` output, plus a watchdog
    event every ~3s while ffmpeg is still doing work but hasn't emitted a
    progress line (e.g. demuxer probing, encoder warm-up).
    """
    if not cmd:
        raise RuntimeError("FFmpeg command is empty")
    ffmpeg_bin = str(cmd[0])
    if not Path(ffmpeg_bin).is_file() and shutil.which(ffmpeg_bin) is None:
        raise RuntimeError(
            "FFmpeg was not found. Install FFmpeg (and add it to PATH) or set "
            "the FFmpeg folder in Settings → FFmpeg path."
        )

    track_encode = (
        progress_hook is not None
        and encode_duration is not None
        and encode_duration > 0
    )
    run_cmd = _ffmpeg_cmd_with_progress(cmd) if track_encode else cmd

    try:
        proc = sp.Popen(
            run_cmd,
            stdout=sp.PIPE,
            stderr=sp.PIPE,
            stdin=sp.DEVNULL,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "FFmpeg was not found. Install FFmpeg (and add it to PATH) or set "
            "the FFmpeg folder in Settings → FFmpeg path."
        ) from exc
    if register_abort:
        register_abort(lambda: proc.poll() is None and proc.kill())

    stderr_chunks: deque[bytes] = deque(maxlen=32)
    _stop_stderr = threading.Event()
    last_reported_pct = progress_from
    start_ts = time.monotonic()
    last_emit_ts = start_ts
    last_known_speed: Optional[str] = None

    def _emit_encode_progress(out_time_ms: int) -> None:
        nonlocal last_reported_pct, last_emit_ts, last_progress_emit_wall
        if not track_encode or not progress_hook:
            return
        encoded_sec = max(0.0, out_time_ms / 1_000_000.0)
        ratio = min(1.0, encoded_sec / encode_duration)
        pct = progress_from + ratio * (progress_to - progress_from)
        # Always emit on a 0.5% step OR every ~1s, whichever comes first.
        # The bar is now wide enough (14% by default) that this gives a
        # smooth, sub-second update rate even for short encodes, and
        # always shows motion while ffmpeg is alive.
        now = time.monotonic()
        if pct < last_reported_pct + 0.5 and (now - last_emit_ts) < 1.0:
            return
        last_reported_pct = pct
        last_emit_ts = now
        # Reset the watchdog's "last seen" wall clock. Without this the
        # watchdog fires every 3s even when ffmpeg is actively producing
        # progress, which is a real bug (caught by consultgpt review).
        last_progress_emit_wall = now
        elapsed = max(0.001, now - start_ts)
        rate = encoded_sec / elapsed if encoded_sec > 0 else 0.0
        remaining = max(0.0, encode_duration - encoded_sec)
        # Prefer ffmpeg's own speed= (media-seconds-per-wall-second,
        # already corrected for stalls and startup) over the elapsed-
        # based estimate, which can be wildly off in the first few
        # seconds. Fall back to elapsed-derived rate when speed isn't
        # yet available.
        parsed_speed = _parse_speed_multiplier(last_known_speed)
        if parsed_speed is not None and parsed_speed > 0.05:
            eta = remaining / parsed_speed
        elif rate > 0.05:
            eta = remaining / rate
        else:
            eta = None
        progress_hook({
            "status": "postprocessing",
            "percent": pct,
            "phase": phase,
            "phase_id": _phase_id(phase),
            "speed": last_known_speed,
            "eta_seconds": eta,
        })

    def _drain_stderr():
        assert proc.stderr is not None
        nonlocal last_known_speed
        line_buf = ""
        for chunk in iter(lambda: proc.stderr.read(4096), b""):
            if _stop_stderr.is_set():
                break
            stderr_chunks.append(chunk)
            if not track_encode:
                continue
            line_buf += chunk.decode(errors="ignore")
            while "\n" in line_buf:
                line, line_buf = line_buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                if line.startswith("out_time_ms="):
                    try:
                        _emit_encode_progress(int(line.split("=", 1)[1]))
                    except ValueError:
                        pass
                elif line.startswith("speed="):
                    raw = line.split("=", 1)[1].strip()
                    if raw and raw != "N/A":
                        last_known_speed = raw

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    # Watchdog: while ffmpeg is still running, emit a synthetic
    # "Encoding (probing)… N frames" event every 3s if we haven't heard
    # from ffmpeg's progress pipe. The user sees the bar move and the
    # status text update, which kills the "frozen at 99%" feeling during
    # long demuxer probes (common on HLS with 1000+ segments).
    #
    # Disabled during Remuxing because ``-c copy`` finishes in well under
    # 3s for typical clips, and a stream of "Remuxing… 93%" duplicates is
    # just noise.
    last_progress_emit_wall = start_ts
    watchdog_enabled = track_encode and _phase_id(phase) == "encoding"
    try:
        while proc.poll() is None:
            try:
                _check_pause_cancel(cancel_event, pause_event)
            except PausedError:
                proc.kill()
                raise
            time.sleep(0.2)
            if watchdog_enabled and progress_hook:
                now = time.monotonic()
                # The progress pipe emits out_time every ~500ms; if we
                # haven't seen one in 3s, ffmpeg is likely still demuxing
                # or warming up the encoder — nudge the UI so the user
                # knows the process is alive. ``last_progress_emit_wall``
                # is updated by ``_emit_encode_progress`` on every real
                # progress line so this only fires when ffmpeg is silent.
                if now - last_progress_emit_wall >= 3.0:
                    last_progress_emit_wall = now
                    encoded_sec = max(0.0, (last_reported_pct - progress_from)
                                       / max(0.01, (progress_to - progress_from))
                                       * encode_duration)
                    elapsed = max(0.001, now - start_ts)
                    rate = encoded_sec / elapsed if encoded_sec > 0 else 0.0
                    remaining = max(0.0, encode_duration - encoded_sec)
                    parsed_speed = _parse_speed_multiplier(last_known_speed)
                    if parsed_speed is not None and parsed_speed > 0.05:
                        eta = remaining / parsed_speed
                    elif rate > 0.05:
                        eta = remaining / rate
                    else:
                        eta = None
                    progress_hook({
                        "status": "postprocessing",
                        "percent": last_reported_pct,
                        "phase": f"{phase}…",
                        "phase_id": _phase_id(f"{phase}…"),
                        "speed": last_known_speed,
                        "eta_seconds": eta,
                    })
        _stop_stderr.set()
        stderr_thread.join(timeout=2)
        stderr = b"".join(stderr_chunks)[-2000:]
        if proc.returncode != 0:
            raise RuntimeError(
                f"FFmpeg failed (exit {proc.returncode}): {stderr.decode(errors='ignore')[:500]}"
            )
    finally:
        _stop_stderr.set()
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


def _ffmpeg_exe_name() -> str:
    return "ffmpeg.exe" if os.name == "nt" else "ffmpeg"


def _ffmpeg_bin_from_dir(directory: Path) -> Optional[str]:
    exe = directory / _ffmpeg_exe_name()
    return str(exe) if exe.is_file() else None


def _resolve_ffmpeg_exe(ffmpeg_dir: Optional[str] = None) -> str:
    if ffmpeg_dir:
        custom = Path(ffmpeg_dir)
        if custom.is_file():
            return str(custom)
        exe = _ffmpeg_bin_from_dir(custom)
        if exe:
            return exe
    found = _find_ffmpeg()
    if found:
        exe = _ffmpeg_bin_from_dir(Path(found))
        if exe:
            return exe
    raise RuntimeError(
        "FFmpeg was not found. Install FFmpeg (and add it to PATH) or set "
        "the FFmpeg folder in Settings → FFmpeg path."
    )


def _resolve_ffprobe_exe(ffmpeg_exe: Optional[str] = None) -> Optional[str]:
    if ffmpeg_exe:
        parent = Path(ffmpeg_exe).parent
        name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
        candidate = parent / name
        if candidate.is_file():
            return str(candidate)
    return shutil.which("ffprobe")


def probe_segment_codec(
    segment_path: str,
    ffmpeg_exe: Optional[str] = None,
) -> dict:
    """Inspect the first downloaded HLS segment via ffprobe."""
    ffprobe = _resolve_ffprobe_exe(ffmpeg_exe)
    if not ffprobe or not os.path.isfile(segment_path):
        return {}
    try:
        out = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries", "stream=codec_name,codec_type,pix_fmt",
                "-of", "json",
                segment_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_NO_WINDOW,
        )
    except Exception:
        return {}
    if out.returncode != 0:
        return {}
    try:
        data = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    result: dict = {}
    for stream in data.get("streams") or []:
        ctype = stream.get("codec_type")
        name = (stream.get("codec_name") or "").lower()
        if ctype == "video" and "video_codec" not in result:
            result["video_codec"] = name
            if stream.get("pix_fmt"):
                result["pix_fmt"] = stream["pix_fmt"]
        elif ctype == "audio" and "audio_codec" not in result:
            result["audio_codec"] = name
    return result


def _hostname_from_url(url_str: str) -> str:
    """Extract hostname from a URL string."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url_str)
        return parsed.hostname or ""
    except Exception:
        return ""


def is_clip_url(url: str) -> bool:
    """True for Twitch/Kick clip pages (not full VODs)."""
    host = _hostname_from_url(url)
    path = (url or "").lower()
    if host == "clips.twitch.tv":
        return True
    if host in ("twitch.tv", "www.twitch.tv") and "/clip/" in path:
        return True
    if host in ("kick.com", "www.kick.com") and "/clips/" in path:
        return True
    return False


def detect_platform(url: str) -> str:
    host = _hostname_from_url(url)
    if host in ("clips.twitch.tv", "twitch.tv", "www.twitch.tv") or host.endswith(".twitch.tv"):
        return "Twitch"
    if host in ("kick.com", "www.kick.com") or host.endswith(".kick.com"):
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

def _bundled_ffmpeg_dirs() -> list[Path]:
    """PyInstaller COLLECT / one-file extract dirs that ship ffmpeg next to the app."""
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass))
    return dirs


def _find_ffmpeg() -> Optional[str]:
    """Locate ffmpeg — returns the BIN DIRECTORY (not the exe).

    yt-dlp's ``ffmpeg_location`` and our clip pipeline both need the
    *directory* that contains *both* ``ffmpeg.exe`` and ``ffprobe.exe``.
    """
    def _check_bin(parent: Path) -> Optional[str]:
        if not parent.is_dir():
            return None
        if _ffmpeg_bin_from_dir(parent):
            return str(parent)
        return None

    for bundled in _bundled_ffmpeg_dirs():
        result = _check_bin(bundled)
        if result:
            return result

    exe = shutil.which("ffmpeg")
    if exe:
        return str(Path(exe).parent)

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

    from services.size_estimate import enrich_info_dict

    payload = {
        "id": info.get("id", ""),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "duration_string": info.get("duration_string"),
        "uploader": info.get("uploader"),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url"),
        "extractor": info.get("extractor"),
        "is_live": info.get("is_live"),
        "qualities": qualities,
        "platform": detect_platform(full_url),
        "created_at": created_at,
    }
    enrich_info_dict(
        payload,
        formats=formats,
        is_clip=is_clip_url(full_url),
    )
    return VideoInfo(**payload)


# ---------------------------------------------------------------------------
# Custom yt-dlp postprocessor that instruments ffmpeg with -progress pipe:2
# ---------------------------------------------------------------------------
#
# Background
# ----------
# yt-dlp's stock ``FFmpegPostProcessor.real_run_ffmpeg`` (see the
# postprocessor/ffmpeg.py source in the installed wheel) shells out via
# ``subprocess.Popen.run`` and never passes ``-progress pipe:2`` to ffmpeg.
# That means the only progress feedback we get is the three placeholder
# events (``started``/``processing``/``finished``) emitted by yt-dlp's own
# postprocessor_hooks. For a 15 GB VOD the actual ffmpeg merge can take
# 30-60 seconds with **zero** UI feedback, which feels exactly like the
# download is stuck.
#
# Fix
# ---
# We override ``real_run_ffmpeg`` to:
#   1. Inject ``-progress pipe:2`` + ``-nostats`` into the command we
#      hand to ffmpeg (immediately before the output file argument).
#   2. Replace the synchronous ``Popen.run`` with our own ``Popen`` +
#      threaded stderr drain (lifted from ``_run_ffmpeg`` above) so we
#      can parse ``out_time_ms=`` lines as ffmpeg reports them.
#   3. Forward each parsed progress event to a thread-safe ``_pp_state``
#      dict that the manager can poll from its progress thread.
#
# We deliberately do NOT subclass ``FFmpegVideoConvertorPP`` (which would
# inherit convertor-specific args that we don't want) or
# ``FFmpegMergerPP`` (which would inherit merge-only assumptions).
# Generic ``FFmpegPostProcessor`` is the right abstraction: it runs any
# ffmpeg command yt-dlp asks for, we just add progress plumbing.

_PP_PROGRESS_STATE_ATTR = "_vodrip_progress_state"


def _set_pp_progress_state(pp: FFmpegPostProcessor, state: dict) -> None:
    """Attach a thread-safe progress-state dict to a postprocessor instance.

    The postprocessor lives inside the yt-dlp download thread. We use a
    plain ``dict`` + a ``threading.Lock`` for the cross-thread handoff
    because the manager's progress thread polls every 250 ms and yt-dlp
    may invoke the postprocessor from a different thread (e.g. the
    file-download thread). Lock contention is essentially zero because
    the post is a single dict-set and the reads are dict-gets.
    """
    state_lock = state.setdefault("lock", threading.Lock())
    setattr(pp, _PP_PROGRESS_STATE_ATTR, state)
    # Keep a back-reference so we can reach the lock from the manager
    # without poking the private attribute twice.
    state["pp_lock"] = state_lock


class _InstrumentedFFmpegPP(FFmpegPostProcessor):
    """``FFmpegPostProcessor`` that surfaces live ffmpeg progress to us.

    All other behaviour (including the postprocessor arg parsing,
    ``-movflags +faststart`` injection, and the per-postprocessor
    ``_configuration_args`` lookup) is inherited unchanged. We only
    override ``real_run_ffmpeg``.
    """

    # ``real_run_ffmpeg`` is what every ffmpeg-bound postprocessor in
    # yt-dlp ultimately calls, so overriding it gives us visibility into
    # the merge, convert, and audio-extract steps with one change.
    def real_run_ffmpeg(self, input_path_opts, output_path_opts, *, expected_retcodes=(0,)):
        import shlex

        state = getattr(self, _PP_PROGRESS_STATE_ATTR, None)
        # No state attached = manager doesn't want our progress events
        # (e.g. during format-probing or an early test path). Fall back
        # to the stock behaviour so we never accidentally break callers
        # that don't opt in.
        if state is None:
            return super().real_run_ffmpeg(
                input_path_opts, output_path_opts,
                expected_retcodes=expected_retcodes,
            )

        self.check_version()

        oldest_mtime = min(
            os.stat(path).st_mtime for path, _ in input_path_opts if path
        )

        cmd = self._build_progress_cmd(input_path_opts, output_path_opts)
        self.write_debug(f"instrumented ffmpeg command line: {shlex.join(cmd)}")

        # Total expected duration in microseconds, if we know it. We
        # compute it once from the input files' probe info (best-effort
        # via ffprobe below) so the percent math has a real denominator.
        state_lock = state.get("lock") or state.get("pp_lock")
        duration_us = state.get("duration_us") or 0

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                text=True,
                bufsize=1,
                creationflags=_NO_WINDOW,
            )
        except FileNotFoundError:
            return super().real_run_ffmpeg(
                input_path_opts, output_path_opts,
                expected_retcodes=expected_retcodes,
            )

        def _drain_stdout():
            # Reads ffmpeg's -progress key=value stream, accumulates
            # the latest ``out_time_ms`` and ``speed`` values, and on
            # every ``progress=continue`` snapshot updates the
            # shared state dict for the manager's poller.
            last_speed = ""
            last_progress_us = 0
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_ms="):
                    try:
                        last_progress_us = int(line.split("=", 1)[1])
                    except ValueError:
                        pass
                elif line.startswith("speed="):
                    raw = line.split("=", 1)[1].strip()
                    if raw and raw != "N/A":
                        last_speed = raw
                elif line.startswith("progress="):
                    with state_lock:
                        if duration_us > 0 and last_progress_us > 0:
                            pct = min(0.99, last_progress_us / duration_us)
                            state["last_percent"] = pct
                            state["last_speed"] = last_speed
                            state["last_eta_seconds"] = (
                                (duration_us - last_progress_us) / 1_000_000
                                / max(0.01, _parse_speed_multiplier(last_speed) or 1.0)
                                if last_speed else None
                            )
                            state["last_emit_wall"] = time.monotonic()

        def _drain_stderr():
            # Swallow ffmpeg's normal log output (we still want it in
            # the yt-dlp debug log via the original ``Popen.run``-style
            # write_debug path, but here we just drain to keep the pipe
            # from blocking).
            assert proc.stderr is not None
            for _line in proc.stderr:
                pass

        t_out = threading.Thread(target=_drain_stdout, daemon=True)
        t_err = threading.Thread(target=_drain_stderr, daemon=True)
        t_out.start()
        t_err.start()

        # Heartbeat: keep the progress-state dict fresh even when
        # ffmpeg is silent (e.g. probing large HLS segments). The
        # manager polls ``last_emit_wall`` to decide whether to show
        # the user "Muxing 98% (working...)" or freeze.
        try:
            retcode = proc.wait()
        except KeyboardInterrupt:
            proc.kill()
            raise
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        if retcode not in expected_retcodes:
            raise Exception(f"ffmpeg failed (rc={retcode})")
        for out_path, _ in output_path_opts:
            if out_path:
                self.try_utime(out_path, oldest_mtime, oldest_mtime)
        with state_lock:
            state["last_percent"] = 0.999
            state["last_speed"] = ""
            state["last_eta_seconds"] = 0
            state["last_emit_wall"] = time.monotonic()
        return ""

    def _build_progress_cmd(
        self, input_path_opts, output_path_opts,
    ) -> list:
        """Build the ffmpeg command with -progress pipe:2 injected.

        We rebuild the command from scratch (instead of mutating the
        stock ``real_run_ffmpeg`` output) so we can guarantee the
        ``-progress`` flag sits in a stable position, just after
        ``-loglevel info`` and ``-nostats``. Input / output options
        are mirrored from the stock path.
        """
        cmd = [self.executable, "-y", "-loglevel", "info",
               "-nostats", "-progress", "pipe:2"]
        for path, opts in input_path_opts:
            if not path:
                continue
            cmd += ["-i", f"file:{path}"]
            for opt in (opts or []):
                cmd += [opt] if isinstance(opt, str) else list(opt)
        for path, opts in output_path_opts:
            if not path:
                continue
            for opt in (opts or []):
                cmd += [opt] if isinstance(opt, str) else list(opt)
            cmd += ["-movflags", "+faststart", path]
        return cmd


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
    video_encoder: Optional[str] = None,
    pp_state: Optional[dict] = None,
    expected_duration: Optional[float] = None,
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

        def _postprocessor_hook(d: dict) -> None:
            # Attach the progress-state dict to the postprocessor
            # instance the first time we see it. The instrumented PP
            # (``_InstrumentedFFmpegPP``) writes its real out_time_ms
            # into this state, which the manager polls from its
            # progress thread. We do the attach here (not in the PP
            # constructor) so the state lives only for this download.
            pp = d.get("postprocessor")
            if pp_state is not None and pp is not None:
                _set_pp_progress_state(pp, pp_state)

            # yt-dlp's own postprocessor pipeline (used when we hand it
            # the file directly, e.g. download_sections for non-HLS
            # sources) doesn't expose a real progress stream. These
            # placeholders are the only feedback we get; the
            # _hook_progress_percent path in the manager applies a
            # monotonic clamp so they don't regress the wider 85->99
            # ffmpeg range that the HLS path uses.
            status = d.get("status")
            if status == "started":
                progress_hook({
                    "status": "postprocessing", "percent": 92,
                    "phase": "Postprocess", "phase_id": "encoding",
                })
            elif status == "processing":
                progress_hook({
                    "status": "postprocessing", "percent": 95,
                    "phase": "Postprocess", "phase_id": "encoding",
                })
            elif status == "finished":
                progress_hook({
                    "status": "postprocessing", "percent": 98,
                    "phase": "Postprocess", "phase_id": "encoding",
                })

        opts["postprocessor_hooks"] = [_postprocessor_hook]

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

    encode_args = ffmpeg_h264_encode_args(resolve_video_encoder(video_encoder))
    # The postprocessor swap (FFmpegVideoConvertor -> _InstrumentedFFmpegPP)
    # is performed once at module import time (see the bottom of this
    # file). This process is single-tenant for yt-dlp, so a process-wide
    # swap is safe and avoids the overhead of mutating a global dict on
    # every download.
    if encode_args:
        opts["postprocessors"] = [
            {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"},
        ]
        opts["postprocessor_args"] = {"ffmpeg": encode_args}
    else:
        # No re-encode requested: still want real progress through the
        # internal merge (bestvideo + bestaudio mux).
        opts["postprocessors"] = [
            {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"},
        ]

    if pp_state is not None:
        # Populate the state dict the manager will poll. The total
        # duration is the *only* denominator we need to turn the raw
        # out_time_ms into a real 0..1 percent.
        pp_state.setdefault("lock", threading.Lock())
        if expected_duration:
            pp_state["duration_us"] = int(expected_duration * 1_000_000)
        else:
            pp_state["duration_us"] = 0

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


def _parse_prefer_height(quality: Optional[str], default: int = 1080) -> int:
    if not quality or quality.lower() == "source":
        return 10_000
    m = re.search(r"(\d+)", quality)
    return int(m.group(1)) if m else default


def prefer_height_from_quality(quality: Optional[str]) -> int:
    """Height used for HLS variant selection and size estimates (public helper)."""
    h = _parse_prefer_height(quality)
    return 10_000 if h >= 10_000 else h


def _resolve_media_playlist(
    media_url: str,
    headers: dict,
    prefer_height: int = 720,
    _depth: int = 0,
    _stream_info: Optional[dict] = None,
) -> tuple[str, dict]:
    """Follow HLS master playlists (Kick IVS, Twitch variants) to a media playlist."""
    if _depth > 10:
        raise RuntimeError(f"HLS playlist resolution exceeded max depth (10) at {media_url}")
    r = requests.get(media_url, headers=headers, timeout=30)
    r.raise_for_status()
    text = r.text
    if len(text) > 5 * 1024 * 1024:
        raise RuntimeError(f"HLS playlist too large ({len(text)} bytes) at {media_url}")
    if "#EXTINF:" in text:
        return media_url, _stream_info or {}

    variants: list[tuple[int, int, str, dict]] = []
    lines = text.splitlines()
    pending_bandwidth = 0
    pending_height = 0
    pending_codecs: dict = {}
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#EXT-X-STREAM-INF"):
            bw_m = re.search(r"BANDWIDTH=(\d+)", stripped)
            res_m = re.search(r"RESOLUTION=(\d+)x(\d+)", stripped)
            pending_bandwidth = int(bw_m.group(1)) if bw_m else 0
            pending_height = int(res_m.group(2)) if res_m else 0
            pending_codecs = _codecs_from_stream_inf(stripped)
            continue
        if stripped and not stripped.startswith("#"):
            variants.append((
                pending_height,
                pending_bandwidth,
                urljoin(media_url, stripped),
                pending_codecs,
            ))
            pending_bandwidth = 0
            pending_height = 0
            pending_codecs = {}

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

    return _resolve_media_playlist(
        chosen[2], headers, prefer_height, _depth=_depth + 1, _stream_info=chosen[3],
    )


def _parse_m3u8(
    media_url: str,
    headers: dict,
    prefer_height: int = 720,
) -> tuple[list[dict], dict]:
    """Download and parse an HLS media playlist, return segments and stream info."""
    media_url, stream_info = _resolve_media_playlist(media_url, headers, prefer_height)
    r = requests.get(media_url, headers=headers, timeout=30)
    r.raise_for_status()
    lines = r.text.splitlines()
    MAX_PLAYLIST_LINES = 100_000
    if len(lines) > MAX_PLAYLIST_LINES:
        raise RuntimeError(f"HLS media playlist too large ({len(lines)} lines) at {media_url}")

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
    return segments, stream_info


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


_SEGMENT_CONNECT_TIMEOUT = 15
_SEGMENT_READ_TIMEOUT = 60
_SEGMENT_STALL_SECONDS = 90


def _iter_response_chunks(response, chunk_size: int, stall_seconds: float):
    """Yield response chunks; abort when no bytes arrive for *stall_seconds*."""
    deadline = time.monotonic() + stall_seconds
    for chunk in response.iter_content(chunk_size):
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"HLS segment download stalled (>{stall_seconds:.0f}s without data)"
            )
        if chunk:
            deadline = time.monotonic() + stall_seconds
        yield chunk


def _download_one_segment(
    index: int,
    seg: dict,
    headers: dict,
    temp_dir: str,
    cancel_event: Optional[threading.Event],
    pause_event: Optional[threading.Event] = None,
) -> str:
    _check_pause_cancel(cancel_event, pause_event)

    path = os.path.join(temp_dir, f"{index:05d}.ts")
    r = requests.get(
        seg["url"],
        headers=headers,
        stream=True,
        timeout=(_SEGMENT_CONNECT_TIMEOUT, _SEGMENT_READ_TIMEOUT),
    )
    r.raise_for_status()
    with open(path, "wb") as f:
        for chunk in _iter_response_chunks(r, 256 * 1024, _SEGMENT_STALL_SECONDS):
            _check_pause_cancel(cancel_event, pause_event)
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
    pause_event: Optional[threading.Event] = None,
    index_offset: int = 0,
) -> list[str]:
    """Download HLS segment files into *temp_dir* (parallel)."""
    total = len(segments)
    if total == 0:
        return []

    files: list[Optional[str]] = [None] * total
    completed = 0
    workers = min(SEGMENT_DOWNLOAD_WORKERS, total)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _download_one_segment,
                index_offset + i,
                seg,
                headers,
                temp_dir,
                cancel_event,
                pause_event,
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

    if any(path is None for path in files):
        raise RuntimeError("HLS segment download incomplete")
    return list(files)


def _feed_path_to_stdin(
    path: str,
    stdin,
    cancel_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    on_chunk: Optional[Callable[[], None]] = None,
) -> None:
    with open(path, "rb") as fin:
        while True:
            _check_pause_cancel(cancel_event, pause_event)
            buf = fin.read(1024 * 1024)
            if not buf:
                break
            try:
                stdin.write(buf)
            except BrokenPipeError:
                break
            if on_chunk:
                on_chunk()
    try:
        os.remove(path)
    except OSError:
        pass


def _apply_mp4_faststart(
    src: str,
    dst: str,
    ffmpeg_exe: str,
    progress_hook: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
) -> None:
    """Optional second pass — moves moov atom for faster Premiere scrubbing."""
    tmp_out = f"{dst}.faststart_{uuid.uuid4().hex}.mp4"
    cmd = [
        ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
        "-i", src,
        "-c", "copy",
        "-movflags", "+faststart",
        "-f", "mp4", tmp_out,
    ]
    if progress_hook:
        progress_hook({
            "status": "postprocessing",
            "percent": 93,
            "phase": "Optimising",
            "phase_id": _phase_id("Optimising"),
        })
    _run_ffmpeg(
        cmd,
        cancel_event=cancel_event,
        pause_event=pause_event,
        register_abort=register_abort,
        progress_hook=progress_hook,
        progress_from=93.0,
        progress_to=97.0,
        phase="Optimising",
    )
    _verify_output_file(tmp_out)
    try:
        os.remove(src)
    except OSError:
        pass
    _atomic_replace(
        tmp_out, dst,
        cancel_event=cancel_event,
        progress_hook=progress_hook,
    )


def _progressive_hls_copy_to_mp4(
    segments: list[dict],
    headers: dict,
    temp_dir: str,
    output_path: str,
    offset: float,
    duration: float,
    ffmpeg_exe: str,
    progress_hook: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
    first_segment_path: Optional[str] = None,
    mp4_faststart: bool = False,
) -> None:
    """Parallel download with backpressure, piped into a Premiere-ready MP4."""
    total = len(segments)
    if total == 0:
        raise RuntimeError("No HLS segments to mux")

    tmp_mp4 = os.path.join(temp_dir, f"stream_{uuid.uuid4().hex}.mp4")
    mux_cmd = [
        ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts+igndts",
        "-f", "mpegts", "-i", "pipe:0",
    ]
    if offset > 0.001:
        mux_cmd += ["-ss", str(offset)]
    mux_cmd += [
        "-t", str(duration),
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-avoid_negative_ts", "make_zero",
        "-max_muxing_queue_size", "9999",
        "-f", "mp4", tmp_mp4,
    ]
    try:
        proc = sp.Popen(
            mux_cmd,
            stdin=sp.PIPE,
            stderr=sp.PIPE,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "FFmpeg was not found. Install FFmpeg (and add it to PATH) or set "
            "the FFmpeg folder in Settings → FFmpeg path."
        ) from exc
    if register_abort:
        register_abort(lambda: proc.poll() is None and proc.kill())

    seg_paths: list[Optional[str]] = [None] * total
    ready = [threading.Event() for _ in range(total)]
    errors: list[Exception] = []
    err_lock = threading.Lock()
    last_ffmpeg_activity = [time.monotonic()]
    stderr_done = threading.Event()

    if first_segment_path:
        seg_paths[0] = first_segment_path
        ready[0].set()

    def _touch_ffmpeg_activity() -> None:
        last_ffmpeg_activity[0] = time.monotonic()

    def _check_ffmpeg_stall() -> None:
        if proc.poll() is not None:
            return
        if time.monotonic() - last_ffmpeg_activity[0] > HLS_MUX_STALL_SECONDS:
            proc.kill()
            raise RuntimeError(
                f"FFmpeg mux stalled (>{HLS_MUX_STALL_SECONDS}s without progress)"
            )

    def _drain_stderr() -> None:
        try:
            assert proc.stderr is not None
            for raw in iter(proc.stderr.readline, b""):
                if raw:
                    _touch_ffmpeg_activity()
        finally:
            stderr_done.set()

    def _download_idx(i: int) -> None:
        if first_segment_path and i == 0:
            return
        try:
            seg_paths[i] = _download_one_segment(
                i, segments[i], headers, temp_dir, cancel_event, pause_event,
            )
        except Exception as exc:
            with err_lock:
                errors.append(exc)
        finally:
            ready[i].set()

    stderr_thread = threading.Thread(target=_drain_stderr, name="hls-mux-stderr", daemon=True)
    stderr_thread.start()

    futures: dict[int, Any] = {}
    with ThreadPoolExecutor(max_workers=min(SEGMENT_DOWNLOAD_WORKERS, total)) as pool:

        def _schedule_through(cap: int) -> None:
            for idx in range(total):
                if idx > cap:
                    break
                if idx in futures or (first_segment_path and idx == 0):
                    continue
                futures[idx] = pool.submit(_download_idx, idx)

        assert proc.stdin is not None
        try:
            for i in range(total):
                _schedule_through(i + HLS_DOWNLOAD_AHEAD)
                ready[i].wait()
                _check_pause_cancel(cancel_event, pause_event)
                _check_ffmpeg_stall()
                with err_lock:
                    if errors:
                        raise errors[0]
                path = seg_paths[i]
                if not path:
                    raise RuntimeError(f"HLS segment {i} missing after download")
                _feed_path_to_stdin(
                    path, proc.stdin, cancel_event, pause_event,
                    on_chunk=_touch_ffmpeg_activity,
                )
                seg_paths[i] = None
                if progress_hook:
                    progress_hook({
                        "status": "downloading",
                        "percent": (i + 1) / total * 92.0,
                        "phase": "Downloading",
                        "concat_encoder": "copy",
                    })
                if proc.poll() is not None:
                    break
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        except BrokenPipeError:
            try:
                proc.stdin.close()
            except OSError:
                pass
        except Exception as exc:
            with err_lock:
                errors.append(exc)
            try:
                proc.stdin.close()
            except OSError:
                pass
            raise
        finally:
            for fut in futures.values():
                fut.result()

        stderr_done.wait(timeout=5)
        stderr = proc.stderr.read() if proc.stderr else b""
        rc = proc.wait()
        with err_lock:
            if errors:
                raise errors[0]
        if rc != 0:
            err_tail = stderr.decode(errors="ignore")[-800:]
            raise RuntimeError(f"FFmpeg stream mux failed (exit {rc}): {err_tail}")
        if not os.path.isfile(tmp_mp4) or os.path.getsize(tmp_mp4) < MIN_VALID_OUTPUT_BYTES:
            err_tail = stderr.decode(errors="ignore")[-800:]
            raise RuntimeError(f"FFmpeg produced no output: {err_tail}")

    _verify_output_file(tmp_mp4)
    if mp4_faststart:
        fast_tmp = os.path.join(temp_dir, f"faststart_{uuid.uuid4().hex}.mp4")
        _apply_mp4_faststart(
            tmp_mp4, fast_tmp, ffmpeg_exe,
            progress_hook=progress_hook,
            cancel_event=cancel_event,
            pause_event=pause_event,
            register_abort=register_abort,
        )
        try:
            os.remove(tmp_mp4)
        except OSError:
            pass
        if progress_hook:
            progress_hook({
                "status": "postprocessing",
                "percent": 97,
                "phase": "Finalising",
                "phase_id": _phase_id("Finalising"),
                "concat_encoder": "copy",
            })
        _atomic_replace(
            fast_tmp, output_path,
            cancel_event=cancel_event,
            progress_hook=progress_hook,
        )
    else:
        if progress_hook:
            progress_hook({
                "status": "postprocessing",
                "percent": 97,
                "phase": "Finalising",
                "phase_id": _phase_id("Finalising"),
                "concat_encoder": "copy",
            })
        _atomic_replace(
            tmp_mp4, output_path,
            cancel_event=cancel_event,
            progress_hook=progress_hook,
        )
    _verify_output_file(output_path)


def _mkv_to_premiere_mp4(
    mkv_path: str,
    output_path: str,
    offset: float,
    duration: float,
    ffmpeg_exe: str,
    progress_hook: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
) -> None:
    """Fallback: single-file MKV → MP4 stream copy (used only outside the progressive path)."""
    tmp_mp4 = f"{output_path}.tmp_{uuid.uuid4().hex}.mp4"
    cmd = [ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error", "-i", mkv_path]
    if offset > 0.001:
        cmd += ["-ss", str(offset)]
    cmd += [
        "-t", str(duration),
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        "-max_muxing_queue_size", "9999",
        "-f", "mp4", tmp_mp4,
    ]
    if progress_hook:
        progress_hook({
            "status": "postprocessing",
            "percent": 84,
            "phase": "Packaging",
            "phase_id": _phase_id("Packaging"),
            "concat_encoder": "copy",
        })
    _check_pause_cancel(cancel_event, pause_event)
    _run_ffmpeg(
        cmd,
        cancel_event=cancel_event,
        pause_event=pause_event,
        register_abort=register_abort,
        progress_hook=progress_hook,
        encode_duration=duration,
        progress_from=84.0,
        progress_to=97.0,
        phase="Packaging",
    )
    _verify_output_file(tmp_mp4)
    if progress_hook:
        progress_hook({
            "status": "postprocessing",
            "percent": 98,
            "phase": "Finalising",
            "phase_id": _phase_id("Finalising"),
        })
    _atomic_replace(
        tmp_mp4, output_path,
        cancel_event=cancel_event,
        progress_hook=progress_hook,
    )
    _verify_output_file(output_path)


def _chunked_copy(
    src: str,
    dst: str,
    cancel_event: Optional[threading.Event] = None,
    progress_hook: Optional[Callable] = None,
    phase: str = "Finalising",
) -> None:
    """Stream *src* to *dst* in 8 MB chunks, reporting throughput + ETA.

    Replaces ``shutil.copy2`` for the cross-drive fallback in
    ``_atomic_replace`` so the user gets real-time feedback during what
    would otherwise be 30-120 s of silence on a 15 GB file. Honours
    *cancel_event* (raises ``CancelledError`` if set) so the disk write
    is interruptible, unlike the synchronous ``os.replace`` fast path.
    """
    total = os.path.getsize(src)
    copied = 0
    start = time.monotonic()
    last_emit = start
    chunk = 8 * 1024 * 1024  # 8 MB

    def _emit(ratio: float, copied: int, total: int) -> None:
        if progress_hook is None:
            return
        elapsed = max(0.001, time.monotonic() - start)
        rate_bps = copied / elapsed
        speed_mbps = rate_bps / (1024 * 1024)
        remaining = max(0, total - copied)
        eta = (remaining / rate_bps) if rate_bps > 0 else None
        # 99 -> 99.9 range so the bar still moves while the copy
        # runs, but the user is never shown 100% before the worker
        # actually transitions to Completed.
        pct = 99.0 + ratio * 0.9
        progress_hook({
            "status": "postprocessing",
            "percent": pct,
            "phase": phase,
            "phase_id": _phase_id(phase),
            "speed": f"{speed_mbps:.1f} MB/s",
            "eta_seconds": eta,
            "copied_bytes": copied,
            "total_bytes": total,
        })

    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("Download cancelled during final write")
            buf = fin.read(chunk)
            if not buf:
                break
            fout.write(buf)
            copied += len(buf)
            now = time.monotonic()
            if (now - last_emit) >= 0.5:
                _emit(copied / total if total > 0 else 0.0, copied, total)
                last_emit = now
    # Always emit one final event so even a 1-second copy of a small
    # file gets a progress row, and the bar reaches 99.9% just before
    # the worker transitions to 100/Completed.
    if total > 0:
        _emit(1.0, total, total)


def _atomic_replace(
    src: str,
    dst: str,
    cancel_event: Optional[threading.Event] = None,
    progress_hook: Optional[Callable] = None,
) -> None:
    """Move *src* to *dst*, falling back to a chunked copy on cross-drive.

    The fast path (``os.replace``) is synchronous and un-instrumentable,
    so for very small files we just do it and trust the user won't
    notice the sub-second blip. For the chunked copy fallback we
    stream in 8 MB blocks and surface real-time throughput to the
    progress hook so the user can see the disk write happening instead
    of staring at a frozen bar.
    """
    try:
        # ``os.rename`` (and its alias ``os.replace``) is atomic on the
        # same filesystem and is essentially free even for multi-GB
        # files. If it works, no need to stream.
        os.replace(src, dst)
        if progress_hook is not None:
            progress_hook({
                "status": "postprocessing",
                "percent": 99.9,
                "phase": "Finalising",
                "phase_id": _phase_id("Finalising"),
                "speed": None,
                "eta_seconds": 0,
            })
        return
    except OSError as exc:
        winerr = getattr(exc, "winerror", None)
        if winerr not in (17, 18) and exc.errno not in (18,):
            raise
        # Cross-drive fallback: stream with progress. We use copyfileobj
        # semantics (no metadata copy) so the operation is fast on
        # cross-drive paths; preserving mtime via shutil.copystat is
        # unnecessary for our use case.
        _chunked_copy(
            src, dst,
            cancel_event=cancel_event,
            progress_hook=progress_hook,
            phase="Finalising",
        )
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
    pause_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
    video_encoder: Optional[str] = None,
) -> None:
    """Concatenate HLS segments and trim (stream copy or H.264 re-encode)."""
    tmp_dir = os.path.dirname(files[0])
    concat_txt = os.path.join(tmp_dir, "concat.txt")
    tmp_out = os.path.join(tmp_dir, f"clip_{uuid.uuid4().hex}.mp4")

    with open(concat_txt, "w", encoding="utf8") as f:
        for seg_path in files:
            posix = seg_path.replace("\\", "/")
            f.write(f"file '{posix}'\n")

    encoder = resolve_video_encoder(video_encoder)
    phase = "Remuxing" if encoder == "copy" else "Encoding"
    if progress_hook:
        progress_hook({
            "status": "postprocessing",
            "percent": 85,
            "phase": phase,
            "phase_id": _phase_id(phase),
            "speed": None,
            "eta_seconds": None,
        })

    encode_args = ffmpeg_h264_encode_args(encoder)
    cmd = [ffmpeg_exe, "-y", "-loglevel", "error"]
    cmd += ["-fflags", "+genpts+igndts"]
    # CPU decode for concat demuxer; NVENC/AMF/QSV still handles encode.
    cmd += ["-f", "concat", "-safe", "0", "-i", concat_txt]
    if offset > 0.001:
        cmd += ["-ss", str(offset)]
    cmd += ["-t", str(duration)]
    if encode_args:
        cmd += encode_args
    else:
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart"]
    cmd += ["-f", "mp4", tmp_out]
    _check_pause_cancel(cancel_event, pause_event)
    _run_ffmpeg(
        cmd,
        cancel_event=cancel_event,
        pause_event=pause_event,
        register_abort=register_abort,
        progress_hook=progress_hook,
        encode_duration=duration,
        progress_from=85.0,
        progress_to=99.0,
        phase=phase,
    )
    _check_pause_cancel(cancel_event, pause_event)
    _verify_output_file(tmp_out)

    if progress_hook:
        # Reserve 99 → 100 for the "Finalising…" window (atomic replace
        # of multi-GB files can take 10+ seconds on a slow disk; we want
        # the user to see explicit feedback that the bar is moving
        # through the disk-write phase, not frozen at 99).
        progress_hook({
            "status": "postprocessing",
            "percent": 99,
            "phase": "Finalising",
            "phase_id": _phase_id("Finalising"),
            "speed": None,
            "eta_seconds": None,
        })

    # Atomic replace when possible; copy fallback for cross-drive temp (Windows).
    # Thread the cancel/pause events and progress hook through so the
    # user gets real-time throughput feedback during a multi-GB disk
    # write and can interrupt it cleanly (the synchronous os.replace
    # fast path is uninterruptible, which is acceptable because it's
    # microseconds on the same drive; only the chunked copy needs the
    # cancel plumbing).
    _atomic_replace(
        tmp_out, output_path,
        cancel_event=cancel_event,
        progress_hook=progress_hook,
    )
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
    pause_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
    register_temp_dir: Optional[Callable[[str], None]] = None,
    prefer_height: int = 720,
    video_encoder: Optional[str] = None,
    mp4_faststart: bool = False,
) -> None:
    """Download an HLS media playlist clip by segment (Kick m3u8 URL or Twitch variant)."""
    headers = headers or {}
    duration = end_sec - start_sec
    segments, stream_info = _parse_m3u8(media_url, headers, prefer_height)
    selected, first_offset = _select_segments(segments, start_sec, end_sec)
    playlist_encoder = resolve_concat_encoder(stream_info, None, video_encoder)
    if progress_hook and playlist_encoder != "copy":
        progress_hook({
            "status": "downloading",
            "concat_encoder": playlist_encoder,
            "phase": "Encoding",
        })
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
    # We don't use ``tempfile.TemporaryDirectory`` because we want the path
    # to be observable to the download manager (so it can be wiped on
    # cancel even if ffmpeg is killed mid-encode) and we want to register
    # ownership so concurrent downloads in the same output folder never
    # accidentally wipe each other's temp dirs.
    tmpdir = tempfile.mkdtemp(prefix="hls_clip_", dir=out_parent or None)
    if register_temp_dir:
        try:
            register_temp_dir(tmpdir)
        except Exception:
            pass
    try:
        first_path: Optional[str] = None
        if selected:
            first_path = _download_one_segment(
                0, selected[0], headers, tmpdir, cancel_event, pause_event,
            )
            probe_info = probe_segment_codec(first_path, resolved_ffmpeg)
        else:
            probe_info = {}
        hls_encoder = resolve_concat_encoder(stream_info, probe_info, video_encoder)
        logger.info(
            "HLS concat: %s (playlist=%s, probe=%s)",
            hls_encoder, stream_info or {}, probe_info or {},
        )
        if progress_hook:
            progress_hook({
                "status": "downloading",
                "concat_encoder": hls_encoder,
                "phase": "Remuxing" if hls_encoder == "copy" else "Encoding",
            })
        if hls_encoder == "copy":
            _progressive_hls_copy_to_mp4(
                selected, headers, tmpdir, output_path,
                first_offset, duration, resolved_ffmpeg,
                progress_hook=progress_hook,
                cancel_event=cancel_event,
                pause_event=pause_event,
                register_abort=register_abort,
                first_segment_path=first_path,
                mp4_faststart=mp4_faststart,
            )
            if progress_hook:
                progress_hook({"status": "downloading", "percent": 100})
                progress_hook({"status": "finished"})
            return

        files = _download_segments(
            selected[1:], headers, tmpdir, progress_hook, cancel_event, pause_event,
            index_offset=1,
        ) if len(selected) > 1 else []
        if first_path:
            files = [first_path, *files]
        _concat_and_trim(
            files, output_path, first_offset, duration, resolved_ffmpeg,
            progress_hook,
            cancel_event=cancel_event,
            pause_event=pause_event,
            register_abort=register_abort,
            video_encoder=hls_encoder,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _download_hls_clip(
    url: str,
    output_path: str,
    start_sec: float,
    end_sec: float,
    opts: dict,
    progress_hook: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
    register_temp_dir: Optional[Callable[[str], None]] = None,
    prefer_height: int = 720,
    video_encoder: Optional[str] = None,
    mp4_faststart: bool = False,
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
        pause_event=pause_event,
        register_abort=register_abort,
        register_temp_dir=register_temp_dir,
        prefer_height=prefer_height,
        video_encoder=video_encoder,
        mp4_faststart=mp4_faststart,
    )


# ---------------------------------------------------------------------------
# Main download entry point
# ---------------------------------------------------------------------------

def _wrap_progress_hook(
    progress_hook: Optional[Callable],
    cancel_event: Optional[threading.Event],
    pause_event: Optional[threading.Event] = None,
) -> Optional[Callable]:
    if not progress_hook and not cancel_event and not pause_event:
        return progress_hook

    def hook(d):
        _check_pause_cancel(cancel_event, pause_event)
        if progress_hook:
            progress_hook(d)

    return hook


def _ydl_download(
    url: str,
    opts: dict,
    cancel_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
) -> None:
    ydl = yt_dlp.YoutubeDL(opts)
    if register_abort:
        register_abort(lambda: getattr(ydl, "cancel_download", lambda: None)())
    try:
        ydl.download([url])
    finally:
        _check_pause_cancel(cancel_event, pause_event)


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
    pause_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
    register_temp_dir: Optional[Callable[[str], None]] = None,
    video_encoder: Optional[str] = None,
    register_pp_state: Optional[Callable[[dict], None]] = None,
) -> str:
    """Download a video or clip. Called from the download manager's worker thread."""
    full_url = build_url(url)
    progress_hook = _wrap_progress_hook(progress_hook, cancel_event, pause_event)
    resolved_encoder = resolve_video_encoder(
        video_encoder or (settings_mgr.get().video_encoder if settings_mgr else None)
    )

    cache_dir = _get_cache_dir()
    max_cache_mb = 200
    if settings_mgr is not None:
        max_cache_mb = settings_mgr.get().max_cache_mb
    max_bytes = max_cache_mb * 1024 * 1024
    cache_dir.mkdir(parents=True, exist_ok=True)
    _prune_cache_dir(cache_dir, max_bytes)

    # Allocate the progress-state dict that the instrumented PP will
    # write to. The manager's progress thread polls it every 250 ms and
    # synthesises a progress event whenever ffmpeg's out_time_ms is
    # newer than the last reported one. We pass the expected duration
    # (in seconds, from the extract step) so the PP can convert
    # out_time_ms to a real 0..1 percent.
    pp_state: dict = {
        "lock": threading.Lock(),
        "duration_us": 0,
        "last_percent": 0.0,
        "last_speed": "",
        "last_eta_seconds": None,
        "last_emit_wall": 0.0,
    }
    # The expected duration is the full VOD/clip length. We re-use the
    # extract step's network call result (yt-dlp would do this anyway
    # before the download, so it's a free call from our perspective).
    expected_duration: Optional[float] = None
    try:
        with yt_dlp.YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "cachedir": str(cache_dir),
        }) as ydl:
            info = ydl.extract_info(full_url, download=False)
        if info:
            expected_duration = info.get("duration")
    except Exception:
        expected_duration = None

    opts = _build_ydl_opts(
        full_url, output_path, quality, oauth, progress_hook, cachedir=str(cache_dir),
        throttle_kib=settings_mgr.get().throttle_kib if settings_mgr is not None else None,
        temp_folder=settings_mgr.get().temp_folder if settings_mgr is not None else None,
        ffmpeg_path=settings_mgr.get().ffmpeg_path if settings_mgr is not None else None,
        video_encoder=resolved_encoder,
        pp_state=pp_state,
        expected_duration=expected_duration,
    )

    # The download manager will poll the state dict to synthesise
    # real-time progress events for the postprocess phase. We expose
    # it via the opts dict so the manager can find it without having
    # to know about yt_dlp's internal state.
    opts["_vodrip_pp_state"] = pp_state

    # If the caller passed a holder (a one-element dict), populate it
    # with the state ref so they can start a polling thread.
    if register_pp_state is not None:
        try:
            register_pp_state(pp_state)
        except Exception:
            pass

    platform = detect_platform(full_url)
    is_hls = platform in ("Twitch", "Kick") and not is_clip_url(full_url)

    if is_hls:
        start_sec, end_sec = _require_crop_range(crop_start, crop_end)
        mp4_faststart = bool(settings_mgr.get().mp4_faststart) if settings_mgr else False
        _download_hls_clip(
            full_url, output_path, start_sec, end_sec, opts,
            progress_hook=progress_hook,
            cancel_event=cancel_event,
            pause_event=pause_event,
            register_abort=register_abort,
            prefer_height=_parse_prefer_height(quality),
            video_encoder=resolved_encoder,
            register_temp_dir=register_temp_dir,
            mp4_faststart=mp4_faststart,
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
        _ydl_download(full_url, opts, cancel_event, pause_event, register_abort)

    _verify_output_file(output_path)
    return output_path


def _format_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Install the instrumented FFmpeg postprocessor at module import time.
# ---------------------------------------------------------------------------
# This is a one-line, process-wide swap in yt-dlp's postprocessor
# registry: from now on, ``FFmpegVideoConvertor`` requests resolve to
# our ``_InstrumentedFFmpegPP`` instead of the stock class. yt-dlp's
# internal lookup goes through the ``postprocessors.value`` dict, which
# is a regular dict (wrapped in an ``Indirect``), so this is a safe
# in-place mutation.
#
# We do it at import (not per-call) because:
#  * The process is single-tenant for yt-dlp — there is no other
#    consumer in this process that needs the stock class.
#  * Per-call swap+restore would still race with yt-dlp's import-time
#    building of its own PP list (which is the race the v1.0.32
#    follow-up was about).
_ytdlp_pp_pkg.postprocessors.value["FFmpegVideoConvertorPP"] = _InstrumentedFFmpegPP
