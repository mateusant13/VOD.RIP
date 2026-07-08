"""
Main download entry point — yt-dlp wrapper, InstrumentedFFmpegPP, URL helpers, and video info.
Depends on `ytdlp_ffmpeg`, `ytdlp_hls`, and `ytdlp_cache`.
"""
import itertools
import json
import logging
import os
import re
import threading
import contextvars
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Tuple

import subprocess
import time

import requests
from services import ytdlp_env  # noqa: F401 — YTDLP_NO_PLUGINS before yt-dlp import
from services.ytdlp_guard import guarded_youtube_dl
import yt_dlp
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

from models.schemas import VideoInfo

from services.ytdlp_ffmpeg import (
    CancelledError, PausedError, DownloadTimeoutError,
    MIN_VALID_OUTPUT_BYTES,
    _check_pause_cancel, _check_cancelled,
    resolve_video_encoder, resolve_concat_encoder,
    normalize_video_encoder, normalize_video_encoder_setting,
    _resolve_ffmpeg_exe,
    _parse_speed_multiplier,
    _phase_id,
    ffmpeg_h264_encode_args,
    _require_crop_range,
    _verify_output_file,
    _normalize_crop_range,
    _format_ts,
    _find_ffmpeg,
    VIDEO_ENCODER_OPTIONS,
    _track_ffmpeg_proc, _untrack_ffmpeg_proc,
)
from services.ytdlp_hls import (
    _download_hls_clip,
    _parse_prefer_height,
    prefer_height_from_quality,
    HLS_DOWNLOAD_PROGRESS_CAP,
)
from services.ytdlp_cache import _get_cache_dir, _prune_cache_dir
from services.os_services import _NO_WINDOW

logger = logging.getLogger(__name__)

# The module-level PP install is performed at the end of this file.

def _hostname_from_url(url_str: str) -> str:
    """Extract hostname from a URL string."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url_str)
        return parsed.hostname or ""
    # ponytail: urlparse errors only — ValueError, AttributeError
    except (ValueError, AttributeError):
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
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com") or host == "youtu.be":
        return "YouTube"
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
    if detected == "YouTube" and id_or_url.startswith("http"):
        return id_or_url
    return id_or_url

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
    platform = detect_platform(full_url)

    cache_dir = _get_cache_dir()
    max_cache_mb = 200
    oauth = None
    cookies_file = None
    if settings_mgr is not None:
        max_cache_mb = settings_mgr.get().max_cache_mb
        oauth = settings_mgr.get().oauth or None
        cookies_file = settings_mgr.get().youtube_cookies_file or None
    max_bytes = max_cache_mb * 1024 * 1024
    cache_dir.mkdir(parents=True, exist_ok=True)
    _prune_cache_dir(cache_dir, max_bytes)

    def _extract():
        if platform == "YouTube":
            from services.ytdlp_hls import cached_extract_info, youtube_preview_ytdl_opts

            opts = youtube_preview_ytdl_opts(
                full_url, oauth=oauth, cachedir=cache_dir,
                cookies_file=cookies_file,
            )
            return cached_extract_info(full_url, opts)
        from services.ytdlp_ffmpeg import _ytdlp_engine_opts

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "cachedir": str(cache_dir),
            **_ytdlp_engine_opts(),
        }
        with guarded_youtube_dl(ydl_opts) as ydl:
            return ydl.extract_info(full_url, download=False)

    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, _extract)
    if info is None:
        raise ValueError("Could not extract video info")

    if platform == "YouTube":
        try:
            dur = float(info.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0.0
        if 0 < dur < 90:
            from services.youtube_innertube import extract_video_id, innertube_video_row_metadata

            vid = extract_video_id(full_url)
            if vid:
                meta = innertube_video_row_metadata(vid, read_timeout=5.0)
                if meta:
                    try:
                        fallback = float(meta.get("duration") or 0)
                    except (TypeError, ValueError):
                        fallback = 0.0
                    if fallback > dur:
                        info["duration"] = int(fallback)

    formats = info.get("formats", [])
    qualities = _qualities_from_formats(formats, is_clip_url(full_url))

    created_at = info.get("created_at") or info.get("upload_date")
    if not created_at and info.get("timestamp"):
        try:
            from datetime import datetime, timezone
            created_at = datetime.fromtimestamp(
                float(info["timestamp"]), tz=timezone.utc
            ).isoformat()
        except (TypeError, ValueError, OSError):
            created_at = None
    if created_at and re.match(r"^\d{8}$", str(created_at)):
        try:
            from datetime import datetime, timezone
            created_at = datetime.strptime(
                str(created_at), "%Y%m%d",
            ).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass

    from services.size_estimate import enrich_info_dict

    payload = {
        "id": info.get("id", ""),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "duration_string": info.get("duration_string"),
        "uploader": info.get("uploader"),
        "channel": info.get("channel") or info.get("channel_id") or info.get("uploader"),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url"),
        "extractor": info.get("extractor"),
        "is_live": info.get("is_live"),
        "qualities": qualities,
        "platform": detect_platform(full_url),
        "created_at": created_at,
        "views": info.get("view_count"),
    }
    enrich_info_dict(
        payload,
        formats=formats,
        is_clip=is_clip_url(full_url),
    )
    return VideoInfo(**payload)

_PP_PROGRESS_STATE_ATTR = "_vodrip_progress_state"
_pp_state_ctx: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "vodrip_pp_state", default=None,
)

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

def kill_pp_state_procs(pp_state: Optional[dict]) -> None:
    """Kill every ffmpeg Popen tracked for one download (cancel/remove)."""
    if not pp_state:
        return
    for proc in list(pp_state.get("active_procs") or []):
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    pp_state["active_procs"] = []

_ORIGINAL_REAL_RUN_FFMPEG = FFmpegPostProcessor.real_run_ffmpeg

def _ytdlp_stock_ffmpeg_cmd(
    pp: FFmpegPostProcessor,
    input_path_opts: Sequence[Tuple[str, Sequence]],
    output_path_opts: Sequence[Tuple[str, Sequence]],
) -> list:
    """Mirror yt-dlp's ``real_run_ffmpeg`` argv (merge-safe, no progress flags)."""
    from yt_dlp.utils import encodeArgument

    cmd = [pp.executable, encodeArgument("-y")]
    if pp.basename == "ffmpeg":
        cmd += [encodeArgument("-loglevel"), encodeArgument("repeat+info")]

    def make_args(file, args, name, number):
        keys = [f"_{name}{number}", f"_{name}"]
        arg_list = list(args)
        if name == "o":
            arg_list += ["-movflags", "+faststart"]
            if number == 1:
                keys.append("")
        arg_list += pp._configuration_args(pp.basename, keys)
        if name == "i":
            arg_list.append("-i")
        return (
            [encodeArgument(arg) for arg in arg_list]
            + [pp._ffmpeg_filename_argument(file)]
        )

    for arg_type, path_opts in (("i", input_path_opts), ("o", output_path_opts)):
        cmd += itertools.chain.from_iterable(
            make_args(path, list(opts), arg_type, i + 1)
            for i, (path, opts) in enumerate(path_opts) if path
        )
    return cmd

def _ytdlp_stock_ffmpeg_cmd_with_progress(
    pp: FFmpegPostProcessor,
    input_path_opts,
    output_path_opts,
) -> list:
    """Stock yt-dlp argv + ``-progress pipe:1`` (stdout) for live mux percent."""
    from yt_dlp.utils import encodeArgument

    cmd = _ytdlp_stock_ffmpeg_cmd(pp, input_path_opts, output_path_opts)
    for i, arg in enumerate(cmd):
        if str(arg) == "-i":
            cmd[i:i] = [
                encodeArgument("-nostats"),
                encodeArgument("-progress"),
                encodeArgument("pipe:1"),
            ]
            break
    return cmd

def _run_tracked_stock_ffmpeg(
    pp: FFmpegPostProcessor,
    input_path_opts,
    output_path_opts,
    *,
    state: dict,
    expected_retcodes=(0,),
) -> str:
    """Stock yt-dlp ffmpeg argv with a tracked, killable ``Popen`` (merge path)."""
    from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessorError
    from yt_dlp.utils import shell_quote

    pp.check_version()
    oldest_mtime = min(
        os.stat(path).st_mtime for path, _ in input_path_opts if path
    )
    cmd = _ytdlp_stock_ffmpeg_cmd_with_progress(pp, input_path_opts, output_path_opts)
    pp.write_debug(f"ffmpeg command line: {shell_quote(cmd)}")

    cancel_event = state.get("cancel_event")
    pause_event = state.get("pause_event")
    register_abort = state.get("register_abort")
    state_lock = state.get("lock") or state.get("pp_lock")
    duration_us = int(state.get("duration_us") or 0)
    _check_pause_cancel(cancel_event, pause_event)

    def _emit_progress_us(out_us: int) -> None:
        if not state_lock or duration_us <= 0 or out_us <= 0:
            return
        pct = min(0.99, out_us / duration_us)
        with state_lock:
            state["last_percent"] = pct
            state["last_emit_wall"] = time.monotonic()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        return _ORIGINAL_REAL_RUN_FFMPEG(
            pp, input_path_opts, output_path_opts,
            expected_retcodes=expected_retcodes,
        )

    def _kill_proc() -> None:
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    _track_ffmpeg_proc(proc)
    state.setdefault("active_procs", []).append(proc)
    if callable(register_abort):
        register_abort(_kill_proc)

    stderr_lines: list[str] = []
    last_progress_us = 0

    def _drain_stdout() -> None:
        nonlocal last_progress_us
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_ms="):
                try:
                    last_progress_us = int(line.split("=", 1)[1])
                    _emit_progress_us(last_progress_us)
                except ValueError:
                    pass
            elif line.startswith("progress=") and line.endswith("end"):
                if duration_us > 0:
                    _emit_progress_us(duration_us)

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)

    t_out = threading.Thread(target=_drain_stdout, daemon=True)
    t_err = threading.Thread(target=_drain_stderr, daemon=True)
    t_out.start()
    t_err.start()
    retcode = 0
    try:
        while proc.poll() is None:
            try:
                _check_pause_cancel(cancel_event, pause_event)
            except PausedError:
                _kill_proc()
                raise
            except CancelledError:
                _kill_proc()
                raise
            time.sleep(0.15)
        retcode = proc.returncode or 0
    except KeyboardInterrupt:
        _kill_proc()
        raise
    finally:
        if proc.poll() is None:
            _kill_proc()
        _untrack_ffmpeg_proc(proc)
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    stderr = "".join(stderr_lines)
    if retcode not in tuple(expected_retcodes):
        pp.write_debug(stderr)
        last = stderr.strip().splitlines()
        raise FFmpegPostProcessorError(last[-1] if last else f"ffmpeg failed (rc={retcode})")
    for out_path, _ in output_path_opts:
        if out_path:
            pp.try_utime(out_path, oldest_mtime, oldest_mtime)
    return stderr

def _selfcheck_ytdlp_stock_cmd() -> None:
    class _StubPP:
        executable = "ffmpeg"
        basename = "ffmpeg"

        def _configuration_args(self, *_a, **_k):
            return []

        def _ffmpeg_filename_argument(self, path):
            return path

    cmd = _ytdlp_stock_ffmpeg_cmd(
        _StubPP(),
        [("a.mp4", [])],
        [("out.mp4", ["-c", "copy"])],
    )
    assert any("movflags" in str(x) for x in cmd)

_selfcheck_ytdlp_stock_cmd()

def _build_ffmpeg_progress_cmd(
    executable: str,
    input_path_opts,
    output_path_opts,
) -> list:
    """ffmpeg argv with ``-progress pipe:2`` for live mux/encode percent."""
    cmd = [executable, "-y", "-loglevel", "info", "-nostats", "-progress", "pipe:2"]
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

def _instrumented_real_run_ffmpeg(
    self,
    input_path_opts,
    output_path_opts,
    *,
    expected_retcodes=(0,),
):
    """Patch all ffmpeg postprocessors (merge, convert, extract) for progress."""
    import shlex

    state = getattr(self, _PP_PROGRESS_STATE_ATTR, None) or _pp_state_ctx.get()
    if state is None:
        return _ORIGINAL_REAL_RUN_FFMPEG(
            self, input_path_opts, output_path_opts,
            expected_retcodes=expected_retcodes,
        )

    self.check_version()
    oldest_mtime = min(
        os.stat(path).st_mtime for path, _ in input_path_opts if path
    )

    state_lock = state.get("lock") or state.get("pp_lock")
    cancel_event = state.get("cancel_event")
    pause_event = state.get("pause_event")
    register_abort = state.get("register_abort")

    # ponytail: yt-dlp merge — stock argv (not progress cmd); tracked Popen so delete can kill ffmpeg
    input_count = sum(1 for path, _ in input_path_opts if path)
    if input_count > 1:
        try:
            return _run_tracked_stock_ffmpeg(
                self, input_path_opts, output_path_opts,
                state=state, expected_retcodes=expected_retcodes,
            )
        finally:
            with state_lock:
                state["last_percent"] = 1.0
                state["last_speed"] = ""
                state["last_eta_seconds"] = 0
                state["last_emit_wall"] = time.monotonic()

    cmd = _build_ffmpeg_progress_cmd(self.executable, input_path_opts, output_path_opts)
    self.write_debug(f"instrumented ffmpeg command line: {shlex.join(cmd)}")

    duration_us = state.get("duration_us") or 0

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        return _run_tracked_stock_ffmpeg(
            self, input_path_opts, output_path_opts,
            state=state, expected_retcodes=expected_retcodes,
        )

    def _kill_proc() -> None:
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    _track_ffmpeg_proc(proc)
    state.setdefault("active_procs", []).append(proc)
    if callable(register_abort):
        register_abort(_kill_proc)

    def _drain_stdout():
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
                    elif line.endswith("end"):
                        pct = 0.999
                    else:
                        pct = state.get("last_percent") or 0.0
                    state["last_percent"] = pct
                    state["last_speed"] = last_speed
                    if duration_us > 0 and last_progress_us > 0 and last_speed:
                        state["last_eta_seconds"] = (
                            (duration_us - last_progress_us) / 1_000_000
                            / max(0.01, _parse_speed_multiplier(last_speed) or 1.0)
                        )
                    else:
                        state["last_eta_seconds"] = None
                    state["last_emit_wall"] = time.monotonic()

    def _drain_stderr():
        assert proc.stderr is not None
        for _line in proc.stderr:
            pass

    t_out = threading.Thread(target=_drain_stdout, daemon=True)
    t_err = threading.Thread(target=_drain_stderr, daemon=True)
    t_out.start()
    t_err.start()
    retcode = 0
    try:
        while proc.poll() is None:
            try:
                _check_pause_cancel(cancel_event, pause_event)
            except PausedError:
                _kill_proc()
                raise
            except CancelledError:
                _kill_proc()
                raise
            time.sleep(0.15)
        retcode = proc.returncode or 0
    except KeyboardInterrupt:
        _kill_proc()
        raise
    finally:
        if proc.poll() is None:
            _kill_proc()
        _untrack_ffmpeg_proc(proc)
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    if retcode not in expected_retcodes:
        raise Exception(f"ffmpeg failed (rc={retcode})")
    for out_path, _ in output_path_opts:
        if out_path:
            self.try_utime(out_path, oldest_mtime, oldest_mtime)
    with state_lock:
        state["last_percent"] = 1.0
        state["last_speed"] = ""
        state["last_eta_seconds"] = 0
        state["last_emit_wall"] = time.monotonic()
    return ""

FFmpegPostProcessor.real_run_ffmpeg = _instrumented_real_run_ffmpeg

def _youtube_remux_by_default(encoder: Optional[str]) -> bool:
    """YouTube with auto/copy skips H.264 re-encode — merge-only is seconds."""
    return (encoder or "auto").strip().lower() in ("auto", "copy", "")

assert _youtube_remux_by_default("auto") and not _youtube_remux_by_default("libx264")
assert _youtube_remux_by_default("copy") and not _youtube_remux_by_default("h264_nvenc")

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
    audio_only: bool = False,
) -> dict:
    from services.ytdlp_ffmpeg import _ytdlp_engine_opts

    opts = {
        "outtmpl": output_path,
        "noplaylist": True,
        "no_warnings": True,
        "quiet": True,
        "concurrent_fragment_downloads": 8,
        **_ytdlp_engine_opts(),
    }
    if not audio_only:
        opts["merge_output_format"] = "mp4"

    if cachedir:
        opts["cachedir"] = cachedir

    # UI default is "source" — not a valid yt-dlp format id; omit so HLS clip
    # extraction picks the best m3u8 variant (same as test_downloads.py).
    if audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    elif quality and quality.lower() != "source":
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
            # yt-dlp may provide either a PostProcessor instance or a string name.
            # Only attach state to real objects that support instance attributes.
            if pp_state is not None and pp is not None and hasattr(pp, "__dict__"):
                _set_pp_progress_state(pp, pp_state)

            # yt-dlp's own postprocessor pipeline (used when we hand it
            # the file directly, e.g. download_sections for non-HLS
            # sources) doesn't expose a real progress stream. These
            # placeholders are the only feedback we get; map them into
            # the 90→99 postprocess range (download phase caps at 90).
            status = d.get("status")
            if status == "started":
                progress_hook({
                    "status": "postprocessing", "percent": 90,
                    "phase": "Postprocess", "phase_id": "encoding",
                })
            elif status == "processing":
                progress_hook({
                    "status": "postprocessing", "percent": 94,
                    "phase": "Postprocess", "phase_id": "encoding",
                })
            elif status == "finished":
                if pp_state is not None:
                    lock = pp_state.get("lock") or pp_state.get("pp_lock")
                    with lock:
                        pp_state["last_percent"] = 1.0
                        pp_state["last_emit_wall"] = time.monotonic()
                progress_hook({
                    "status": "postprocessing", "percent": 99,
                    "phase": "Finalising", "phase_id": "encoding",
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

    encoder_setting = (video_encoder or "auto").strip().lower()
    wants_transcode = not audio_only and encoder_setting not in ("auto", "copy", "")
    if not audio_only and wants_transcode:
        encode_args = ffmpeg_h264_encode_args(resolve_video_encoder(video_encoder))
        opts["postprocessors"] = [
            {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"},
        ]
        opts["postprocessor_args"] = {"ffmpeg": encode_args}
    # ponytail: YouTube auto/copy — merge_output_format mp4 only (no re-encode pass)

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

assert not _build_ydl_opts(
    "https://www.youtube.com/watch?v=x", "/tmp/out.mp4", video_encoder="auto",
).get("postprocessors")

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

def sanitize_download_error(exc: BaseException) -> str:
    """Strip yt-dlp ANSI noise; return a short user-facing message."""
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
    msg = re.sub(r"^ERROR:\s*", "", msg, flags=re.IGNORECASE).strip()
    low = msg.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return (
            "YouTube blocked this video — try again or add youtube_cookies_file in settings.json"
        )
    if msg.lower().startswith("error:"):
        msg = msg[6:].strip()
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _ydl_download(
    url: str,
    opts: dict,
    cancel_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
) -> None:
    from services.ytdlp_hls import _YtdlpQuietLogger, _silence_stderr

    quiet_opts = dict(opts)
    quiet_opts.setdefault("quiet", True)
    quiet_opts.setdefault("no_warnings", True)
    quiet_opts["logger"] = _YtdlpQuietLogger()
    with _silence_stderr():
        with guarded_youtube_dl(quiet_opts) as ydl:
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
    audio_only: bool = False,
) -> str:
    """Download a video or clip. Called from the download manager's worker thread."""
    full_url = build_url(url)
    platform = detect_platform(full_url)
    progress_hook = _wrap_progress_hook(progress_hook, cancel_event, pause_event)
    encoder_setting = normalize_video_encoder_setting(
        video_encoder or (settings_mgr.get().video_encoder if settings_mgr else None),
    )
    resolved_encoder = resolve_video_encoder(encoder_setting)

    cache_dir = _get_cache_dir()
    max_cache_mb = 200
    cookies_file = None
    if settings_mgr is not None:
        max_cache_mb = settings_mgr.get().max_cache_mb
        cookies_file = settings_mgr.get().youtube_cookies_file or None
        if not oauth:
            oauth = settings_mgr.get().oauth or None
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
        if platform == "YouTube":
            from services.ytdlp_hls import cached_extract_info, youtube_preview_ytdl_opts

            info = cached_extract_info(
                full_url,
                youtube_preview_ytdl_opts(
                    full_url, oauth=oauth, cachedir=cache_dir,
                    cookies_file=cookies_file,
                ),
            )
            if info:
                expected_duration = info.get("duration")
        else:
            from services.ytdlp_ffmpeg import _ytdlp_engine_opts
            with guarded_youtube_dl({
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "skip_download": True,
                "cachedir": str(cache_dir),
                **_ytdlp_engine_opts(),
            }) as ydl:
                info = ydl.extract_info(full_url, download=False)
            if info:
                expected_duration = info.get("duration")
    except (OSError, json.JSONDecodeError, ValueError, TypeError, RuntimeError):
        expected_duration = None
    except yt_dlp.utils.DownloadError as exc:
        logger.debug("YouTube duration probe failed: %s", exc)
        expected_duration = None

    trim = _normalize_crop_range(crop_start, crop_end)
    if trim:
        expected_duration = trim[1] - trim[0]

    opts = _build_ydl_opts(
        full_url, output_path, quality, oauth, progress_hook, cachedir=str(cache_dir),
        temp_folder=settings_mgr.get().temp_folder if settings_mgr is not None else None,
        ffmpeg_path=settings_mgr.get().ffmpeg_path if settings_mgr is not None else None,
        video_encoder=encoder_setting,
        pp_state=pp_state,
        expected_duration=expected_duration,
        audio_only=audio_only,
    )
    from services.youtube_session import (
        youtube_session_from_settings,
        ytdlp_extractor_args,
        apply_ytdlp_cookie_opts,
    )
    from services.youtube_innertube import extract_video_id

    yt_session = youtube_session_from_settings(settings_mgr, video_id=extract_video_id(full_url))
    opts["_youtube_session"] = yt_session
    try:
        auto_auth = getattr(settings_mgr.get(), "youtube_auto_auth", True)
    except Exception:
        auto_auth = True
    apply_ytdlp_cookie_opts(
        opts, yt_session, auto_auth=auto_auth, cookies_file=cookies_file,
    )
    if platform == "YouTube":
        ext_args = ytdlp_extractor_args(yt_session, auto_auth=auto_auth)
        yt_args = opts.get("extractor_args") or {}
        opts["extractor_args"] = {
            **yt_args,
            **ext_args,
            "youtube": {
                **(yt_args.get("youtube") or {}),
                **ext_args["youtube"],
            },
        }

    if register_abort is not None:
        register_abort(lambda: kill_pp_state_procs(pp_state))

    # The download manager will poll the state dict to synthesise
    # real-time progress events for the postprocess phase. We expose
    # it via the opts dict so the manager can find it without having
    # to know about yt_dlp's internal state.
    opts["_vodrip_pp_state"] = pp_state

    is_hls = (
        platform == "YouTube"
        or (
            not audio_only
            and platform in ("Twitch", "Kick")
            and not is_clip_url(full_url)
        )
    )
    if platform == "YouTube":
        from services.ytdlp_hls import _youtube_info_use_clip_path, cached_extract_info

        try:
            probe = cached_extract_info(full_url, opts)
            is_hls = _youtube_info_use_clip_path(probe)
        except Exception as exc:
            logger.debug("YouTube probe failed, preferring clip path over yt-dlp: %s", exc)
            is_hls = True

    # HLS downloads report their own 0→90% progress while segments are
    # fetched and muxed. The yt-dlp postprocessor poller only applies to
    # the direct yt-dlp download path (non-HLS).
    if register_pp_state is not None and not is_hls:
        pp_state["cancel_event"] = cancel_event
        pp_state["pause_event"] = pause_event
        pp_state["register_abort"] = register_abort
        try:
            register_pp_state(pp_state)
        # ponytail: survival guarantee for register_pp_state callback — arbitrary callback
        except Exception:
        # ponytail: best-effort — register_pp_state(pp_state)
            pass

    if is_hls:
        crop = _normalize_crop_range(crop_start, crop_end)
        if crop is not None:
            start_sec, end_sec = crop
        else:
            # Full VOD download — start at 0, end at known duration.
            # download_hls_media_clip caps end_sec at actual segment length,
            # so a large fallback is safe (ffmpeg stops at EOF anyway).
            start_sec = 0.0
            end_sec = expected_duration if expected_duration and expected_duration > 0 else 1e18
        mp4_faststart = bool(settings_mgr.get().mp4_faststart) if settings_mgr else False
        from services.youtube_diag import log as yt_log
        yt_log.info(
            "download path=hls_clip url=%s trim=%s-%s quality=%s",
            full_url[:100],
            start_sec,
            end_sec,
            quality,
        )
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
            audio_only=audio_only,
        )
    else:
        crop = _normalize_crop_range(crop_start, crop_end)
        from services.youtube_diag import log as yt_log
        yt_log.info(
            "download path=ytdlp url=%s trim=%s platform=%s",
            full_url[:100],
            crop,
            platform,
        )
        if crop_start is not None or crop_end is not None:
            if crop is None:
                raise ValueError(
                    "Both crop_start and crop_end are required for trimmed downloads"
                )
            start = _format_ts(crop[0])
            end = _format_ts(crop[1])
            opts["download_sections"] = [f"*{start}-{end}"]
        _pp_token = _pp_state_ctx.set(pp_state)
        try:
            _ydl_download(full_url, opts, cancel_event, pause_event, register_abort)
        finally:
            _pp_state_ctx.reset(_pp_token)

    _verify_output_file(output_path)
    return output_path
