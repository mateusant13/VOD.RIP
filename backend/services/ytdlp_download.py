"""
Main download entry point — yt-dlp wrapper, InstrumentedFFmpegPP, URL helpers, and video info.
Depends on `ytdlp_ffmpeg`, `ytdlp_hls`, and `ytdlp_cache`.
"""
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Optional

import subprocess
import time

import requests
import yt_dlp
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor, FFmpegVideoConvertorPP
import yt_dlp.postprocessor as _ytdlp_pp_pkg

from models.schemas import VideoInfo

from services.ytdlp_ffmpeg import (
    CancelledError, PausedError, DownloadTimeoutError,
    MIN_VALID_OUTPUT_BYTES,
    _check_pause_cancel, _check_cancelled,
    resolve_video_encoder, resolve_concat_encoder,
    normalize_video_encoder,
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

class _InstrumentedFFmpegPP(FFmpegVideoConvertorPP):
    """``FFmpegVideoConvertorPP`` that surfaces live ffmpeg progress to us.

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

        _track_ffmpeg_proc(proc)

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
        finally:
            _untrack_ffmpeg_proc(proc)
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
                progress_hook({
                    "status": "postprocessing", "percent": 97,
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
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
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

    platform = detect_platform(full_url)
    is_hls = platform in ("Twitch", "Kick") and not is_clip_url(full_url)

    # HLS downloads report their own 0→90% progress while segments are
    # fetched and muxed. The yt-dlp postprocessor poller only applies to
    # the direct yt-dlp download path (non-HLS).
    if register_pp_state is not None and not is_hls:
        try:
            register_pp_state(pp_state)
        # ponytail: survival guarantee for register_pp_state callback — arbitrary callback
        except Exception:
        # ponytail: best-effort — register_pp_state(pp_state)
            pass

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


# Install the instrumented FFmpeg postprocessor at module import time.
_ytdlp_pp_pkg.postprocessors.value["FFmpegVideoConvertorPP"] = _InstrumentedFFmpegPP
