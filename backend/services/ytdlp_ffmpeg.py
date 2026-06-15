"""
FFmpeg execution and codec utilities — runs ffmpeg, probes segments, resolves encoders,
and defines the base exception classes used across the ytdlp family of modules.
"""
import json
import logging
import os
import re
import shutil
import subprocess as sp
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from services.os_services import _NO_WINDOW, register_child_pid, unregister_child_pid

logger = logging.getLogger(__name__)


def _check_pause_cancel(
    cancel_event: Optional[threading.Event],
    pause_event: Optional[threading.Event] = None,
) -> None:
    """Raise ``CancelledError`` or ``PausedError`` when the respective event is set."""
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("Download cancelled by user")
    if pause_event is not None and pause_event.is_set():
        raise PausedError("Download paused by user")


def _check_cancelled(cancel_event: Optional[threading.Event]) -> None:
    """Raise ``CancelledError`` when *cancel_event* is set."""
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("Download cancelled by user")

MIN_VALID_OUTPUT_BYTES = 50_000

def _track_ffmpeg_proc(proc: sp.Popen) -> None:
    if proc.pid:
        register_child_pid(proc.pid)

def _untrack_ffmpeg_proc(proc: sp.Popen) -> None:
    if proc.pid:
        unregister_child_pid(proc.pid)

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
    # ponytail: survival guarantee for encoder detection — yt-dlp probe may raise from various internal states
    except Exception:
    # ponytail: best-effort — detected = str(get_encoder_detection().get("detect
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

class CancelledError(Exception):
    """Raised when a download is cancelled by the user."""
    pass

class PausedError(Exception):
    """Raised when a download is paused by the user."""
    pass

class DownloadTimeoutError(Exception):
    """Raised when a download exceeds its wall-clock budget."""
    pass

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
    _track_ffmpeg_proc(proc)
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
        _untrack_ffmpeg_proc(proc)

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
        out = sp.run(
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
    # ponytail: subprocess errors only — OSError, timeout, CalledProcessError
    except (OSError, sp.TimeoutExpired, sp.CalledProcessError):
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
            "percent": 91,
            "phase": "Optimising",
            "phase_id": _phase_id("Optimising"),
        })
    _run_ffmpeg(
        cmd,
        cancel_event=cancel_event,
        pause_event=pause_event,
        register_abort=register_abort,
        progress_hook=progress_hook,
        progress_from=91.0,
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

def _format_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
