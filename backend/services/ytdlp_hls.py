"""
HLS playlist parsing, segment downloading, and clip assembly — the segment-level
HLS downloader that avoids yt-dlp for Twitch/Kick VODs.
"""
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin

import requests
import shutil
import subprocess as sp
import tempfile

import yt_dlp

from services.ytdlp_ffmpeg import (
    CancelledError, PausedError,
    MIN_VALID_OUTPUT_BYTES,
    _track_ffmpeg_proc, _untrack_ffmpeg_proc,
    resolve_video_encoder, resolve_concat_encoder,
    _check_pause_cancel,
    ffmpeg_h264_encode_args,
    probe_segment_codec,
    _apply_mp4_faststart,
    _atomic_replace,
    _chunked_copy,
    _format_ts,
    _phase_id,
    _resolve_ffmpeg_exe,
    _run_ffmpeg,
    _verify_output_file,
    _codecs_from_stream_inf,
)
from services.os_services import _NO_WINDOW, register_child_pid, unregister_child_pid

logger = logging.getLogger(__name__)

# These constants are also used by ytdlp_ffmpeg and will be kept there
# while re-exported via the shim.

_SEGMENT_CONNECT_TIMEOUT = 15
_SEGMENT_READ_TIMEOUT = 60
_SEGMENT_STALL_SECONDS = 90

_HLS_FORWARD_KEYS = frozenset({
    "format", "username", "password",
    "cachedir", "quiet", "no_warnings",
})


SEGMENT_DOWNLOAD_WORKERS = 8

HLS_DOWNLOAD_AHEAD = SEGMENT_DOWNLOAD_WORKERS + 2

HLS_MUX_STALL_SECONDS = 120

HLS_DOWNLOAD_PROGRESS_CAP = 90.0

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

    if progress_hook:
        progress_hook({
            "status": "downloading",
            "percent": 0,
            "phase": "Downloading",
            "phase_id": _phase_id("Downloading"),
        })

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
    _track_ffmpeg_proc(proc)
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
        # ponytail: survival guarantee for per-segment download — segment I/O may fail in many ways; skip to next segment
        except Exception as exc:
        # ponytail: subprocess/codec errors only — best-effort encoder detection
            with err_lock:
                errors.append(exc)
        finally:
            ready[i].set()

    stderr_thread = threading.Thread(target=_drain_stderr, name="hls-mux-stderr", daemon=True)
    stderr_thread.start()

    try:
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
                            "percent": (i + 1) / total * HLS_DOWNLOAD_PROGRESS_CAP,
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
            # ponytail: segment cleanup I/O errors only
            except (OSError, AttributeError) as exc:
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
    finally:
        _untrack_ffmpeg_proc(proc)

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
    segments, stream_info = _parse_m3u8(media_url, headers, prefer_height)
    # Compute actual total duration from parsed segments so we never
    # rely on the caller's end_sec (which may be a sentinel like 999999).
    total_duration = sum(s["duration"] for s in segments)
    # Cap end_sec at the actual media length — prevents sentinel values
    # from inflating the duration used for ffmpeg -t and progress math.
    if end_sec > total_duration + 1.0:
        end_sec = total_duration
    duration = end_sec - start_sec
    if duration <= 0:
        raise RuntimeError(
            f"HLS trim range {start_sec}-{end_sec} results in non-positive duration"
        )
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
        # ponytail: survival guarantee for register_pp_state callback — arbitrary callback
        except Exception:
        # ponytail: best-effort — register_temp_dir(tmpdir)
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
