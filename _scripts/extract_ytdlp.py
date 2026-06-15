"""
Extract backend/services/ytdlp_service.py (2,343 lines) into four focused modules:

  - ytdlp_ffmpeg.py   — FFmpeg execution, codec/encoder helpers, probe utilities
  - ytdlp_hls.py      — HLS playlist parsing, segment download, clip assembly
  - ytdlp_cache.py    — Cache directory management and pruning
  - ytdlp_download.py — Main download entry point, yt-dlp wrapper, InstrumentedFFmpegPP
  - ytdlp_service.py  — Backward-compat shim re-exporting from the above modules

Usage: python _scripts/extract_ytdlp.py
"""

import re
from pathlib import Path

SERVICES = Path(__file__).resolve().parent.parent / "backend" / "services"


def read_source() -> str:
    path = SERVICES / "ytdlp_service.py"
    return path.read_text(encoding="utf-8")


def write_module(name: str, content: str, *, is_new: bool = True) -> None:
    path = SERVICES / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    tag = "NEW" if is_new else "UPDATED"
    print(f"  [{tag}] {name}")


# ===========================================================================
# 1. ytdlp_ffmpeg.py — ffmpeg execution, codecs, encoder helpers, probe
# ===========================================================================

FFMPEG_MODULE = '''"""
FFmpeg execution and codec utilities — runs ffmpeg, probes segments, resolves encoders.
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

# Clips shorter than this are almost certainly a broken ftyp-only placeholder.
MIN_VALID_OUTPUT_BYTES = 50_000


def _track_ffmpeg_proc(proc: sp.Popen) -> None:
    if proc.pid:
        register_child_pid(proc.pid)


def _untrack_ffmpeg_proc(proc: sp.Popen) -> None:
    if proc.pid:
        unregister_child_pid(proc.pid)


# FFmpeg video encoders for H.264 output (Settings -> Video encoder).
VIDEO_ENCODER_OPTIONS: dict[str, str] = {
    "auto": "Auto (detect GPU)",
    "copy": "Source codec (fast)",
    "libx264": "H.264 -- x264 (software)",
    "h264_nvenc": "H.264 -- NVIDIA NVENC",
    "h264_amf": "H.264 -- AMD AMF",
    "h264_qsv": "H.264 -- Intel Quick Sync",
}


def normalize_video_encoder_setting(value: Optional[str]) -> str:
    """Persisted settings value -- ``auto`` is allowed."""
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
    # ponytail: subprocess/codec errors only -- best-effort encoder detection
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
    """Deprecated -- kept for callers that have not migrated to ``resolve_concat_encoder``."""
    _ = platform
    return resolve_concat_encoder(None, None, user_encoder)


def ffmpeg_hwaccel_input_args(encoder: str) -> list[str]:
    """GPU decode flags for single-input transcodes (not concat demuxer)."""
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


def _ffmpeg_cmd_with_progress(cmd: list) -> list:
    """Insert ffmpeg machine-readable progress flags before the output file."""
    if "-f" in cmd:
        idx = cmd.index("-f")
        return cmd[:idx] + ["-nostats", "-progress", "pipe:2"] + cmd[idx:]
    return cmd[:-1] + ["-nostats", "-progress", "pipe:2", cmd[-1]]


def _parse_speed_multiplier(raw: Optional[str]) -> Optional[float]:
    """Parse ffmpeg's ``speed=1.4x`` string into a float multiplier."""
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
    """Map a free-text phase label to a stable, lowercase id for UI logic."""
    s = (label or "").lower().rstrip("...").rstrip(".").strip()
    if "remux" in s:
        return "remuxing"
    if "finalis" in s:
        return "finalising"
    if "mux" in s:
        return "muxing"
    if "merg" in s:
        return "merging"
    return "encoding"


def _normalize_crop_range(
    crop_start: Optional[float],
    crop_end: Optional[float],
) -> Optional[Tuple[float, float]]:
    """Return (start, end) when a valid trim range is present."""
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
        raise ValueError("Both crop_start and crop_end are required for Twitch/Kick downloads")
    crop = _normalize_crop_range(crop_start, crop_end)
    if crop is None:
        raise ValueError("crop_start and crop_end are required (crop_end must be after crop_start)")
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
        "the FFmpeg folder in Settings -> FFmpeg path."
    )


def _resolve_ffprobe_exe(ffmpeg_exe: Optional[str] = None) -> Optional[str]:
    if ffmpeg_exe:
        parent = Path(ffmpeg_exe).parent
        name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
        candidate = parent / name
        if candidate.is_file():
            return str(candidate)
    return shutil.which("ffprobe")


def probe_segment_codec(segment_path: str, ffmpeg_exe: Optional[str] = None) -> dict:
    """Inspect the first downloaded HLS segment via ffprobe."""
    ffprobe = _resolve_ffprobe_exe(ffmpeg_exe)
    if not ffprobe or not os.path.isfile(segment_path):
        return {}
    try:
        out = sp.run(
            [ffprobe, "-v", "error", "-show_entries", "stream=codec_name,codec_type,pix_fmt",
             "-of", "json", segment_path],
            capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW,
        )
    # ponytail: subprocess errors only -- OSError, timeout, CalledProcessError
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
    """Locate ffmpeg -- returns the BIN DIRECTORY (not the exe)."""
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


def _format_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


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
    """Run ffmpeg, polling *cancel_event* / *pause_event* so stop can kill the process."""
    if not cmd:
        raise RuntimeError("FFmpeg command is empty")
    ffmpeg_bin = str(cmd[0])
    if not Path(ffmpeg_bin).is_file() and shutil.which(ffmpeg_bin) is None:
        raise RuntimeError(
            "FFmpeg was not found. Install FFmpeg (and add it to PATH) or set "
            "the FFmpeg folder in Settings -> FFmpeg path."
        )
    track_encode = (
        progress_hook is not None
        and encode_duration is not None
        and encode_duration > 0
    )
    run_cmd = _ffmpeg_cmd_with_progress(cmd) if track_encode else cmd
    try:
        proc = sp.Popen(
            run_cmd, stdout=sp.PIPE, stderr=sp.PIPE, stdin=sp.DEVNULL,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "FFmpeg was not found. Install FFmpeg (and add it to PATH) or set "
            "the FFmpeg folder in Settings -> FFmpeg path."
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
        now = time.monotonic()
        if pct < last_reported_pct + 0.5 and (now - last_emit_ts) < 1.0:
            return
        last_reported_pct = pct
        last_emit_ts = now
        last_progress_emit_wall = now
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
            while "\\n" in line_buf:
                line, line_buf = line_buf.split("\\n", 1)
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
    last_progress_emit_wall = start_ts
    watchdog_enabled = track_encode and _phase_id(phase) == "encoding"
    try:
        while proc.poll() is None:
            _from ytdlp_download import CancelledError, PausedError  # lazy import avoids circular
            try:
                _check_pause_cancel(cancel_event, pause_event)
            except PausedError:
                proc.kill()
                raise
            time.sleep(0.2)
            # ... watchdog omitted for brevity in extraction, will be present in final file
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


def _verify_output_file(path: str) -> None:
    """Check that a file exists and is larger than MIN_VALID_OUTPUT_BYTES."""
    if not os.path.isfile(path):
        raise RuntimeError(f"Output file missing: {path}")
    size = os.path.getsize(path)
    if size < MIN_VALID_OUTPUT_BYTES:
        try:
            os.remove(path)
        except OSError:
            pass
        raise RuntimeError(f"Output file too small ({size} bytes); download incomplete")


def _atomic_replace(
    src: str, dst: str,
    cancel_event: Optional[threading.Event] = None,
    progress_hook: Optional[Callable] = None,
) -> None:
    """Move *src* to *dst*, falling back to a chunked copy on cross-drive."""
    try:
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
        _chunked_copy(src, dst, cancel_event=cancel_event, progress_hook=progress_hook, phase="Finalising")
        os.remove(src)


def _chunked_copy(
    src: str, dst: str,
    cancel_event: Optional[threading.Event] = None,
    progress_hook: Optional[Callable] = None,
    phase: str = "Finalising",
) -> None:
    """Stream *src* to *dst* in 8 MB chunks, reporting throughput + ETA."""
    from ytdlp_download import CancelledError  # lazy import avoids circular
    total = os.path.getsize(src)
    copied = 0
    start = time.monotonic()
    last_emit = start
    chunk = 8 * 1024 * 1024
    def _emit(ratio: float, copied: int, total: int) -> None:
        if progress_hook is None:
            return
        elapsed = max(0.001, time.monotonic() - start)
        rate_bps = copied / elapsed
        speed_mbps = rate_bps / (1024 * 1024)
        remaining = max(0, total - copied)
        eta = (remaining / rate_bps) if rate_bps > 0 else None
        pct = 99.0 + ratio * 0.9
        progress_hook({
            "status": "postprocessing", "percent": pct, "phase": phase,
            "phase_id": _phase_id(phase),
            "speed": f"{speed_mbps:.1f} MB/s",
            "eta_seconds": eta,
            "copied_bytes": copied, "total_bytes": total,
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
    if total > 0:
        _emit(1.0, total, total)


def _apply_mp4_faststart(
    src: str, dst: str, ffmpeg_exe: str,
    progress_hook: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
) -> None:
    """Optional second pass -- moves moov atom for faster Premiere scrubbing."""
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
            "status": "postprocessing", "percent": 91,
            "phase": "Optimising", "phase_id": _phase_id("Optimising"),
        })
    _run_ffmpeg(
        cmd, cancel_event=cancel_event, pause_event=pause_event,
        register_abort=register_abort, progress_hook=progress_hook,
        progress_from=91.0, progress_to=97.0, phase="Optimising",
    )
    _verify_output_file(tmp_out)
    try:
        os.remove(src)
    except OSError:
        pass
    _atomic_replace(tmp_out, dst, cancel_event=cancel_event, progress_hook=progress_hook)
'''

# Hmm, this approach is getting unwieldy due to circular import issues. Let me take a different approach.
# Instead of inline extraction, let me write each module separately as direct file content.

print("This script is a placeholder -- the actual extraction will be done by writing each module directly.")
print("See _scripts/extract_ytdlp_v2.py for the complete implementation.")
