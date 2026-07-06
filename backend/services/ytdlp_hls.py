"""
HLS playlist parsing, segment downloading, and clip assembly — the segment-level
HLS downloader that avoids yt-dlp for Twitch/Kick VODs.
"""
import contextlib
import errno
import io
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
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
    _track_ffmpeg_proc, _untrack_ffmpeg_proc, _terminate_ffmpeg_proc,
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
    "cachedir", "quiet", "no_warnings", "cookiefile",
})


class _YtdlpQuietLogger:
    """Capture yt-dlp chatter; surface only at DEBUG."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def debug(self, msg: object) -> None:
        pass

    def info(self, msg: object) -> None:
        pass

    def warning(self, msg: object) -> None:
        self.lines.append(str(msg))

    def error(self, msg: object) -> None:
        self.lines.append(str(msg))


@contextlib.contextmanager
def _silence_stderr():
    """Redirect fd 2 — yt-dlp writes ERROR lines past logging hooks."""
    buf = io.StringIO()
    saved_fd = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 2)
        with contextlib.redirect_stderr(buf):
            old_sys = sys.stderr
            sys.stderr = buf
            try:
                yield buf
            finally:
                sys.stderr = old_sys
    finally:
        os.dup2(saved_fd, 2)
        os.close(saved_fd)
        os.close(devnull)


SEGMENT_DOWNLOAD_WORKERS = 8

HLS_DOWNLOAD_AHEAD = SEGMENT_DOWNLOAD_WORKERS + 2

HLS_MUX_STALL_SECONDS = 120

HLS_DOWNLOAD_PROGRESS_CAP = 90.0

_EXTRACT_INFO_CACHE: dict[str, tuple[float, dict]] = {}
_EXTRACT_INFLIGHT: dict[str, tuple[threading.Event, dict]] = {}
_EXTRACT_CACHE_LOCK = threading.Lock()
_EXTRACT_CACHE_TTL_SEC = 10 * 60
_EXTRACT_CACHE_MAX = 32
_EXTRACT_WAIT_SEC = 120
_YOUTUBE_EXTRACT_PARALLEL_SEC = 6.0


def _youtube_manual_auth_configured() -> bool:
    """True when user set cookies file, browser, po_token, or tokens file in Settings."""
    try:
        from deps import settings_mgr
        s = settings_mgr.get()
        if (getattr(s, "youtube_cookies_file", "") or "").strip():
            return True
        if (getattr(s, "youtube_cookies_browser", "") or "").strip():
            return True
        if (getattr(s, "youtube_po_token", "") or "").strip():
            return True
        if (getattr(s, "youtube_tokens_file", "") or "").strip():
            return True
    except Exception:
        pass
    return False


def _extract_cache_key(url: str, opts: dict) -> str:
    clients = (
        (opts.get("extractor_args") or {})
        .get("youtube", {})
        .get("player_client")
    )
    oauth = opts.get("password") or opts.get("username") or ""
    cookie = opts.get("cookiefile") or ""
    browser = opts.get("cookiesfrombrowser") or ""
    session = opts.get("_youtube_session")
    sess_key = ""
    if session is not None:
        sess_key = f"{bool(session.visitor_data)}|{bool(session.po_token)}|{bool(session.cookie_header)}"
    return f"{url}|{clients}|{bool(oauth)}|{cookie}|{browser}|{sess_key}"


def _youtube_url_from_opts(url: str, opts: dict) -> bool:
    from services.youtube_innertube import extract_video_id

    return extract_video_id(url) is not None


def _youtube_cookie_path(opts: dict) -> Optional[str]:
    path = (opts.get("cookiefile") or "").strip()
    if path and Path(path).is_file():
        return path
    return None


def _try_innertube_info(url: str, session=None) -> Optional[dict]:
    from services.youtube_innertube import innertube_extract_info

    return innertube_extract_info(url, session=session)


def youtube_preview_ytdl_opts(
    full_url: str,
    oauth: Optional[str] = None,
    cachedir: Optional[Path] = None,
    cookies_file: Optional[str] = None,
    session=None,
) -> dict:
    """Fast YouTube extract profile for preview (HLS ladder, disk cache)."""
    from services.ytdlp_cache import _get_cache_dir
    from services.ytdlp_ffmpeg import _find_ffmpeg
    from services.youtube_innertube import extract_video_id
    from services.youtube_session import (
        resolve_ytdlp_cookiefile,
        youtube_session_from_settings,
        youtube_session_from_values,
        ytdlp_extractor_args,
    )

    vid = extract_video_id(full_url)
    auto_auth = True
    if session is None:
        try:
            from deps import settings_mgr
            s = settings_mgr.get()
            auto_auth = getattr(s, "youtube_auto_auth", True)
            session = youtube_session_from_values(
                visitor_data=getattr(s, "youtube_visitor_data", "") or None,
                po_token=getattr(s, "youtube_po_token", "") or None,
                cookies_file=cookies_file or getattr(s, "youtube_cookies_file", "") or None,
                tokens_file=getattr(s, "youtube_tokens_file", "") or None,
                cookies_from_browser=getattr(s, "youtube_cookies_browser", "") or None,
                video_id=vid,
                auto_auth=auto_auth,
            )
        except Exception:
            session = youtube_session_from_settings(video_id=vid)
            auto_auth = True

    opts: dict = {
        "extractor_args": ytdlp_extractor_args(session, auto_auth=auto_auth),
        "cachedir": str(cachedir or _get_cache_dir()),
        "_youtube_session": session,
        "socket_timeout": 10,
    }
    if oauth:
        opts["username"] = "oauth_token"
        opts["password"] = oauth
    from services.youtube_session import apply_ytdlp_cookie_opts

    apply_ytdlp_cookie_opts(opts, session, auto_auth=auto_auth, cookies_file=cookies_file)
    found = _find_ffmpeg()
    if found:
        opts["ffmpeg_location"] = found
    return opts


def _youtube_info_has_hls(info: dict) -> bool:
    """Preview needs m3u8 ladder — progressive-only yt-dlp hits must not be cached."""
    for fmt in info.get("formats") or []:
        if not fmt.get("url"):
            continue
        fid = (fmt.get("format_id") or "").lower()
        if fid == "hls-master":
            return True
        if fmt.get("protocol") in ("m3u8", "m3u8_native", "m3u8_ffmpeg"):
            return True
    return False


def _youtube_info_use_clip_path(info: dict) -> bool:
    """Route YouTube through clip downloader (HLS or InnerTube direct googlevideo URLs)."""
    if _youtube_info_has_hls(info):
        return True
    return any(
        int(f.get("height") or 0) > 0
        and (f.get("protocol") or "").lower() in ("https", "http")
        for f in info.get("formats") or []
        if f.get("url")
    )


def _is_webm_video_format(fmt: dict) -> bool:
    url = (fmt.get("url") or "").lower()
    if "mime=video%2fwebm" in url or "mime=video/webm" in url:
        return True
    vc = (fmt.get("vcodec") or "").lower()
    return vc.startswith("vp9") or vc.startswith("av01")


def _pick_format_by_height(formats: list, prefer_height: int) -> dict:
    with_h = [f for f in formats if int(f.get("height") or 0) > 0]
    if not with_h:
        raise RuntimeError("No video formats with height")
    exact = [f for f in with_h if int(f["height"]) == prefer_height]
    if exact:
        return max(exact, key=lambda f: f.get("tbr") or 0)
    at_or_below = [f for f in with_h if int(f["height"]) <= prefer_height]
    if at_or_below:
        return max(at_or_below, key=lambda f: int(f["height"]))
    return min(with_h, key=lambda f: int(f["height"]))


def _pick_youtube_clip_video_format(formats: list, prefer_height: int) -> dict:
    """Pick video for trimmed download — prefer muxed MP4, then h264 DASH at height."""
    candidates = [
        f for f in formats
        if f.get("url") and int(f.get("height") or 0) > 0
    ]
    if not candidates:
        raise RuntimeError("No video formats with height")
    at_height = [f for f in candidates if int(f["height"]) == prefer_height]
    if at_height:
        muxed = [f for f in at_height if _is_muxed_progressive(f)]
        if muxed:
            return max(muxed, key=lambda f: f.get("tbr") or 0)
        mp4 = [f for f in at_height if not _is_webm_video_format(f)]
        if mp4:
            return max(mp4, key=lambda f: f.get("tbr") or 0)
        return max(at_height, key=lambda f: f.get("tbr") or 0)
    return _pick_format_by_height(candidates, prefer_height)


def _youtube_format_playable(fmt: dict) -> bool:
    if not fmt.get("url"):
        return False
    fid = (fmt.get("format_id") or "").lower()
    if fid == "hls-master":
        return True
    if fid == "abr-muxed":
        return False
    proto = (fmt.get("protocol") or "").lower()
    if proto in ("m3u8", "m3u8_native", "m3u8_ffmpeg"):
        return True
    if proto not in ("https", "http"):
        return False
    if int(fmt.get("height") or 0) > 0:
        return True
    return fmt.get("acodec") not in (None, "none") and fmt.get("vcodec") not in (None, "none")


def _youtube_info_playable(info: dict) -> bool:
    """HLS, muxed ABR, or direct progressive URL."""
    if _youtube_info_has_hls(info):
        return True
    return any(_youtube_format_playable(f) for f in info.get("formats") or [])


def _youtube_cache_ok(url: str, opts: dict, info: dict) -> bool:
    if not _youtube_url_from_opts(url, opts):
        return True
    return _youtube_info_playable(info)


def _try_innertube_info_retry(url: str, attempts: int = 1, session=None) -> Optional[dict]:
    """InnerTube multi-client chain — one pass; outer extract loop handles retries."""
    for i in range(attempts):
        info = _try_innertube_info(url, session=session)
        if info and _youtube_info_playable(info):
            return info
        if i + 1 < attempts:
            time.sleep(0.12 * (i + 1))
    return None


def _merge_fresh_youtube_session(opts: dict, url: str) -> dict:
    """Soft anonymous refresh — bootstrap new visitor cookies, no browser auth path."""
    from services.youtube_innertube import extract_video_id
    from services.youtube_session import (
        bootstrap_anonymous_session,
        youtube_session_from_settings,
        ytdlp_extractor_args,
    )

    vid = extract_video_id(url)
    bootstrap_anonymous_session(video_id=vid, force=True)
    fresh = youtube_session_from_settings(video_id=vid)
    merged = dict(opts)
    merged["_youtube_session"] = fresh
    merged["extractor_args"] = ytdlp_extractor_args(fresh, auto_auth=False)
    merged.pop("cookiefile", None)
    merged.pop("cookiesfrombrowser", None)
    return merged


def _youtube_extract_parallel(
    url: str,
    opts: dict,
    yt_session,
    vid: str,
) -> Optional[dict]:
    """Race InnerTube vs yt-dlp anonymous — first playable result wins."""
    from services.youtube_diag import log_extract_ok

    bare_opts = {
        k: v for k, v in opts.items()
        if k not in ("cookiefile", "cookiesfrombrowser")
    }
    bare_opts["socket_timeout"] = min(int(bare_opts.get("socket_timeout") or 10), 6)

    def _inn() -> Optional[dict]:
        return _try_innertube_info_retry(url, session=yt_session)

    def _ydl() -> Optional[dict]:
        return _extract_hls_info_quiet(url, bare_opts)

    winner: Optional[dict] = None
    source = ""
    # ponytail: shutdown(wait=False) — `with ThreadPoolExecutor` waits for slow yt-dlp (~5s)
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="yt-extract")
    try:
        futures = {
            pool.submit(_inn): "innertube_pass",
            pool.submit(_ydl): "ytdlp_bare",
        }
        deadline = time.monotonic() + _YOUTUBE_EXTRACT_PARALLEL_SEC
        pending = set(futures.keys())
        while pending and time.monotonic() < deadline:
            done, pending = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
            for fut in done:
                try:
                    info = fut.result()
                except Exception as exc:
                    logger.debug("parallel extract task failed %s: %s", vid, exc)
                    continue
                if info and _youtube_info_playable(info):
                    winner = info
                    source = futures[fut]
                    break
            if winner:
                break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if winner:
        log_extract_ok(vid, source, winner, yt_session)
    return winner


def _youtube_extract_pass(url: str, opts: dict) -> Optional[dict]:
    from services.youtube_innertube import extract_video_id
    from services.youtube_diag import log_extract_ok

    yt_session = opts.get("_youtube_session")
    vid = extract_video_id(url) or url[:32]
    has_auth = (
        _youtube_cookie_path(opts)
        or opts.get("cookiesfrombrowser")
        or _youtube_manual_auth_configured()
    )
    if has_auth:
        if _youtube_cookie_path(opts) or opts.get("cookiesfrombrowser"):
            info = _extract_hls_info_quiet(url, opts)
            if info:
                log_extract_ok(vid, "ytdlp_cookies", info, yt_session)
                return info
        info = _try_innertube_info_retry(url, session=yt_session)
        if info:
            log_extract_ok(vid, "innertube_pass", info, yt_session)
            return info
        info = _extract_hls_info_quiet(url, {k: v for k, v in opts.items() if k != "cookiefile"})
        if info:
            log_extract_ok(vid, "ytdlp_bare", info, yt_session)
        return info

    info = _youtube_extract_parallel(url, opts, yt_session, vid)
    if info:
        return info
    info = _try_innertube_info_retry(url, session=yt_session)
    if info:
        log_extract_ok(vid, "innertube_pass", info, yt_session)
        return info
    info = _extract_hls_info_quiet(url, {k: v for k, v in opts.items() if k != "cookiefile"})
    if info:
        log_extract_ok(vid, "ytdlp_bare", info, yt_session)
    return info


def _youtube_extract_with_retries(url: str, opts: dict, attempts: int = 3) -> dict:
    working = dict(opts)
    last_err: Optional[BaseException] = None
    for i in range(attempts):
        try:
            info = _youtube_extract_pass(url, working)
            if info and _youtube_info_playable(info):
                return info
        except Exception as exc:
            last_err = exc
        if i + 1 < attempts:
            if i % 2 == 1:
                working = _merge_fresh_youtube_session(working, url)
            time.sleep(0.12 * (i + 1))
    if last_err is not None:
        raise last_err
    raise RuntimeError("YouTube preview unavailable for this video")


assert _youtube_format_playable({"format_id": "hls-master", "url": "https://x/master.m3u8", "protocol": "m3u8_native"}) is True


def _cache_extract_result(key: str, info: dict) -> None:
    now = time.time()
    with _EXTRACT_CACHE_LOCK:
        if len(_EXTRACT_INFO_CACHE) >= _EXTRACT_CACHE_MAX:
            oldest = min(_EXTRACT_INFO_CACHE.items(), key=lambda item: item[1][0])
            _EXTRACT_INFO_CACHE.pop(oldest[0], None)
        _EXTRACT_INFO_CACHE[key] = (now, info)


def cached_extract_info(url: str, opts: dict) -> dict:
    """yt-dlp extract_info with in-memory TTL cache (preview + /api/info share hits)."""
    key = _extract_cache_key(url, opts)
    now = time.time()
    with _EXTRACT_CACHE_LOCK:
        hit = _EXTRACT_INFO_CACHE.get(key)
        if hit and (now - hit[0]) < _EXTRACT_CACHE_TTL_SEC and _youtube_cache_ok(url, opts, hit[1]):
            return hit[1]
        if hit and not _youtube_cache_ok(url, opts, hit[1]):
            _EXTRACT_INFO_CACHE.pop(key, None)
        inflight = _EXTRACT_INFLIGHT.get(key)
        if inflight is not None:
            leader = False
        else:
            box: dict = {"result": None, "error": None}
            inflight = (threading.Event(), box)
            _EXTRACT_INFLIGHT[key] = inflight
            leader = True

    if not leader:
        event, box = inflight
        if not event.wait(timeout=_EXTRACT_WAIT_SEC):
            raise TimeoutError("YouTube metadata extract timed out")
        with _EXTRACT_CACHE_LOCK:
            hit = _EXTRACT_INFO_CACHE.get(key)
            if hit and _youtube_cache_ok(url, opts, hit[1]):
                return hit[1]
        err = box.get("error")
        if err is not None:
            raise err
        result = box.get("result")
        if result is not None:
            return result
        return cached_extract_info(url, opts)

    event, box = inflight
    try:
        info = None
        if _youtube_url_from_opts(url, opts):
            info = _youtube_extract_with_retries(url, opts)
        else:
            info = _extract_hls_info(url, opts)
        if info is None:
            from services.youtube_innertube import extract_video_id
            from services.youtube_diag import log_extract_fail
            log_extract_fail(
                extract_video_id(url) or "?",
                "extract_returned_none",
                opts.get("_youtube_session"),
            )
            raise RuntimeError(
                "YouTube blocked this video — add cookies, browser cookies, or po_token in Settings"
            )
        if _youtube_url_from_opts(url, opts) and not _youtube_info_playable(info):
            from services.youtube_innertube import extract_video_id
            from services.youtube_diag import format_summary, log_extract_fail
            log_extract_fail(
                extract_video_id(url) or "?",
                "not_playable",
                opts.get("_youtube_session"),
                detail=format_summary(info),
            )
            raise RuntimeError("YouTube extract returned no playable formats")
        box["result"] = info
        if _youtube_cache_ok(url, opts, info):
            _cache_extract_result(key, info)
        return info
    except BaseException as exc:
        if _youtube_url_from_opts(url, opts):
            from services.youtube_innertube import extract_video_id
            from services.youtube_diag import log_extract_fail
            log_extract_fail(
                extract_video_id(url) or "?",
                "extract_exception",
                opts.get("_youtube_session"),
                exc=exc if isinstance(exc, Exception) else None,
                detail=str(exc)[:200],
            )
        box["error"] = exc
        raise
    finally:
        with _EXTRACT_CACHE_LOCK:
            _EXTRACT_INFLIGHT.pop(key, None)
        event.set()


assert cached_extract_info.__name__ == "cached_extract_info"
assert _youtube_info_has_hls({"formats": [{"url": "https://x/a.m3u8", "protocol": "m3u8_native"}]})
assert _youtube_info_use_clip_path({"formats": [{"url": "https://x/v.mp4", "protocol": "https", "height": 720}]})
assert _youtube_info_playable({"formats": [{"url": "https://x/v.mp4", "protocol": "https", "height": 720}]})


def warm_youtube_extract(url: str, oauth: Optional[str] = None, cookies_file: Optional[str] = None) -> bool:
    """Populate shared extract cache (InnerTube first). Cheap — safe to call from hover/prefetch."""
    from services.youtube_innertube import extract_video_id

    if not extract_video_id(url):
        return False
    try:
        opts = youtube_preview_ytdl_opts(url, oauth=oauth, cookies_file=cookies_file)
        cached_extract_info(url, opts)
        return True
    except Exception as exc:
        from services.youtube_diag import log_extract_fail
        from services.youtube_innertube import extract_video_id
        log_extract_fail(extract_video_id(url) or "?", "warm_skipped", exc=exc)
        logger.debug("YouTube warm extract skipped for %s: %s", url[:80], exc)
        return False


def _extract_hls_info_quiet(url: str, opts: dict) -> Optional[dict]:
    """yt-dlp extract without raising; stderr suppressed unless DEBUG."""
    try:
        return _extract_hls_info(url, opts)
    except Exception as exc:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("yt-dlp fallback failed for %s: %s", url, exc)
        return None


def _extract_hls_info(url: str, opts: dict) -> dict:
    """Use yt-dlp to get HLS info without downloading, passing auth etc."""
    from services.ytdlp_ffmpeg import _ytdlp_engine_opts

    ydl_log = _YtdlpQuietLogger()
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "logger": ydl_log,
        "socket_timeout": 10,
        **_ytdlp_engine_opts(),
    }
    ffmpeg_loc = opts.get("ffmpeg_location")
    if not ffmpeg_loc:
        from services.ytdlp_ffmpeg import _find_ffmpeg

        ffmpeg_loc = _find_ffmpeg()
    if ffmpeg_loc:
        ydl_opts["ffmpeg_location"] = ffmpeg_loc
    for key in _HLS_FORWARD_KEYS:
        if key in opts:
            ydl_opts[key] = opts[key]
    for key in ("js_runtimes", "extractor_args"):
        if key in opts:
            ydl_opts[key] = opts[key]
    ytdlp_logger = logging.getLogger("yt_dlp")
    prev_level = ytdlp_logger.level
    if not logger.isEnabledFor(logging.DEBUG):
        ytdlp_logger.setLevel(logging.CRITICAL)
    try:
        quiet = not logger.isEnabledFor(logging.DEBUG)
        ctx = _silence_stderr() if quiet else contextlib.nullcontext()
        with ctx:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
    finally:
        if not logger.isEnabledFor(logging.DEBUG):
            ytdlp_logger.setLevel(prev_level)
        if ydl_log.lines and logger.isEnabledFor(logging.DEBUG):
            logger.debug("yt-dlp: %s", "\n".join(ydl_log.lines))

def _is_muxed_progressive(fmt: dict) -> bool:
    fid = (fmt.get("format_id") or "").lower()
    if fid == "hls-master":
        return True
    if fid == "abr-muxed":
        return False
    ac = fmt.get("acodec")
    vc = fmt.get("vcodec")
    if ac in ("none", None) or vc in ("none", None):
        return False
    return True


def _find_progressive_format(info: dict) -> dict:
    formats = info.get("formats") or []
    prog = [
        f for f in formats
        if f.get("url")
        and (f.get("protocol") or "").lower() in ("https", "http")
        and int(f.get("height") or 0) > 0
        and _is_muxed_progressive(f)
    ]
    if not prog:
        raise RuntimeError("No progressive format found in video info")
    prog.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
    return prog[0]


def _find_media_format(info: dict) -> dict:
    """HLS first; YouTube InnerTube often returns progressive-only."""
    try:
        return _find_hls_format(info)
    except RuntimeError:
        return _find_progressive_format(info)


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
        or (f.get("format_id") or "").lower() == "hls-master"
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


def _is_broken_pipe_error(exc: BaseException) -> bool:
    if isinstance(exc, BrokenPipeError):
        return True
    if isinstance(exc, OSError):
        if exc.errno in (errno.EINVAL, 22):
            return True
        if getattr(exc, "winerror", None) == 233:
            return True
    return False


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
            except OSError as exc:
                if _is_broken_pipe_error(exc):
                    break
                raise
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
        register_abort(lambda: _terminate_ffmpeg_proc(proc))

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
            pipe_eof_exc: Optional[Exception] = None
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
                    if proc.poll() is not None:
                        break
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
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                if _is_broken_pipe_error(exc):
                    pipe_eof_exc = exc
                else:
                    with err_lock:
                        errors.append(exc)
                    raise
            finally:
                for fut in futures.values():
                    fut.result()

            stderr_done.wait(timeout=5)
            stderr = proc.stderr.read() if proc.stderr else b""
            rc = proc.wait()
            if pipe_eof_exc is not None:
                if not (
                    os.path.isfile(tmp_mp4)
                    and os.path.getsize(tmp_mp4) >= MIN_VALID_OUTPUT_BYTES
                ):
                    raise pipe_eof_exc
            else:
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
        _terminate_ffmpeg_proc(proc)
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

def _extract_hls_audio(video_path: str, output_path: str, ffmpeg_exe: Optional[str] = None) -> None:
    """Strip video track from an HLS clip mux to mp3."""
    resolved = ffmpeg_exe or _resolve_ffmpeg_exe()
    _run_ffmpeg(
        [
            resolved, "-y", "-i", video_path,
            "-vn", "-acodec", "libmp3lame", "-b:a", "192k",
            output_path,
        ],
        cancel_event=None,
        pause_event=None,
        register_abort=None,
    )
    _verify_output_file(output_path)


def _ffmpeg_input_headers(headers: dict) -> list[str]:
    if not headers:
        return []
    return ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]


def _download_progressive_clip(
    media_url: str,
    output_path: str,
    start_sec: float,
    end_sec: float,
    headers: Optional[dict] = None,
    ffmpeg_exe: Optional[str] = None,
    progress_hook: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
    prefer_height: int = 720,
    video_encoder: Optional[str] = None,
    mp4_faststart: bool = False,
    audio_only: bool = False,
) -> None:
    """Trim a direct MP4 URL (YouTube adaptiveFormats) via ffmpeg."""
    del prefer_height
    duration = max(0.1, end_sec - start_sec)
    resolved = ffmpeg_exe or _resolve_ffmpeg_exe()
    media_rng = _googlevideo_byte_range(media_url, start_sec, end_sec)
    tmpdir: Optional[str] = None
    media_input = media_url
    ss = start_sec
    if media_rng:
        tmpdir = tempfile.mkdtemp(prefix="prog_clip_")
        media_input = os.path.join(tmpdir, "in.mp4")
        _fetch_googlevideo_range(media_url, media_rng, headers, media_input)
        ss = max(0.0, start_sec - _range_lead_sec(media_url, media_rng[0]))
    cmd = [resolved, "-y", "-hide_banner", "-loglevel", "error"]
    if not media_rng:
        cmd += _ffmpeg_input_headers(headers or {})
    cmd += ["-ss", str(ss), "-i", media_input, "-t", str(duration)]
    if audio_only:
        cmd += ["-vn", "-acodec", "libmp3lame", "-b:a", "192k", output_path]
    else:
        enc_args = ffmpeg_h264_encode_args(video_encoder or "copy")
        if enc_args:
            cmd += enc_args
        else:
            cmd += ["-c", "copy"]
        if mp4_faststart:
            cmd += ["-movflags", "+faststart"]
        cmd.append(output_path)
    if progress_hook:
        progress_hook({
            "status": "downloading",
            "percent": 5,
            "phase": "Downloading",
            "phase_id": _phase_id("Downloading"),
        })
    try:
        _run_ffmpeg(
            cmd,
            cancel_event=cancel_event,
            pause_event=pause_event,
            register_abort=register_abort,
            progress_hook=progress_hook,
            encode_duration=duration,
            progress_from=10.0,
            progress_to=92.0,
            phase="Remuxing" if (video_encoder or "copy") == "copy" else "Encoding",
        )
        _verify_output_file(output_path)
        if progress_hook:
            progress_hook({
                "status": "postprocessing",
                "percent": 99,
                "phase": "Finalising",
                "phase_id": _phase_id("Finalising"),
            })
            progress_hook({"status": "finished"})
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _dur_sec_from_googlevideo_url(url: str) -> Optional[float]:
    m = re.search(r"[?&]dur=([\d.]+)", url or "", re.I)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    return v if v > 0 else None


def _googlevideo_byte_range(
    url: str, start_sec: float, end_sec: float,
) -> Optional[tuple[int, int]]:
    """Byte range covering [start_sec, end_sec] with keyframe lead-in."""
    from services.size_estimate import _clen_bytes_from_url

    clen = _clen_bytes_from_url(url)
    dur = _dur_sec_from_googlevideo_url(url)
    if not clen or not dur:
        return None
    clip = max(0.1, end_sec - start_sec)
    lead = min(start_sec, 3.0)
    t0 = max(0.0, start_sec - lead)
    t1 = min(dur, end_sec + clip * 0.08)
    b0 = int(clen * (t0 / dur))
    b1 = min(clen - 1, int(clen * (t1 / dur)))
    if b1 <= b0:
        return None
    return b0, b1


def _range_lead_sec(url: str, byte_start: int) -> float:
    from services.size_estimate import _clen_bytes_from_url

    clen = _clen_bytes_from_url(url)
    dur = _dur_sec_from_googlevideo_url(url)
    if not clen or not dur:
        return 0.0
    return max(0.0, byte_start / clen * dur)


def _fetch_googlevideo_range(
    url: str,
    byte_range: tuple[int, int],
    headers: Optional[dict],
    dest: str,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """HTTP Range fetch of a googlevideo slice to a local file."""
    b0, b1 = byte_range
    hdrs = dict(headers or {})
    hdrs["Range"] = f"bytes={b0}-{b1}"
    try:
        from curl_cffi import requests as cffi_requests

        resp = cffi_requests.get(
            url, headers=hdrs, impersonate="chrome", stream=True,
            timeout=(10, 180),
        )
    except ImportError:
        resp = requests.get(url, headers=hdrs, stream=True, timeout=180)
    resp.raise_for_status()
    with open(dest, "wb") as out:
        for chunk in resp.iter_content(256 * 1024):
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("Download cancelled by user")
            if chunk:
                out.write(chunk)


def _dash_video_needs_transcode(video_url: str, video_fmt: Optional[dict] = None) -> bool:
    """VP9/AV1/webm cannot be copied into MP4."""
    if video_fmt:
        ext = (video_fmt.get("ext") or "").lower()
        vc = (video_fmt.get("vcodec") or "").lower()
        if ext == "webm" or vc.startswith(("vp9", "av01")):
            return True
    u = (video_url or "").lower()
    if "mime=video%2fwebm" in u or "mime=video/webm" in u:
        return True
    if "itag/313" in u or "itag/315" in u or "itag/401" in u:
        return True
    return False


def _ffmpeg_reconnect_args() -> list[str]:
    return ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]


def _local_dash_slice_valid(path: str, *, audio: bool = False) -> bool:
    """Range-fetched googlevideo slice must look like a readable media file."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < 256:
            return False
        with open(path, "rb") as handle:
            head = handle.read(32)
        if b"ftyp" in head:
            return True
        # YouTube DASH audio is often webm-wrapped (EBML) even when muxed to m4a.
        if audio and head[:4] == b"\x1aE\xdf\xa3":
            return True
        return False
    except OSError:
        return False


def _download_muxed_dash_clip(
    video_url: str,
    audio_url: str,
    output_path: str,
    start_sec: float,
    end_sec: float,
    headers: Optional[dict] = None,
    ffmpeg_exe: Optional[str] = None,
    progress_hook: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
    video_encoder: Optional[str] = None,
    mp4_faststart: bool = False,
    video_fmt: Optional[dict] = None,
    allow_remote_retry: bool = True,
) -> None:
    """Trim separate video+audio googlevideo URLs and mux to MP4."""
    duration = max(0.1, end_sec - start_sec)
    resolved = ffmpeg_exe or _resolve_ffmpeg_exe()
    hdr = _ffmpeg_input_headers(headers or {})
    reconnect = _ffmpeg_reconnect_args()
    probe = ["-probesize", "8M", "-analyzeduration", "2M"]

    v_range = _googlevideo_byte_range(video_url, start_sec, end_sec)
    a_range = _googlevideo_byte_range(audio_url, start_sec, end_sec)
    tmpdir: Optional[str] = None
    v_input, a_input = video_url, audio_url
    v_ss, a_ss = start_sec, start_sec
    used_local = False

    if v_range and a_range:
        tmpdir = tempfile.mkdtemp(prefix="dash_clip_")
        v_local = os.path.join(tmpdir, "v.mp4")
        a_local = os.path.join(tmpdir, "a.m4a")
        if progress_hook:
            progress_hook({
                "status": "downloading",
                "percent": 8,
                "phase": "Downloading",
                "phase_id": _phase_id("Downloading"),
            })
        _fetch_googlevideo_range(video_url, v_range, headers, v_local, cancel_event)
        _fetch_googlevideo_range(audio_url, a_range, headers, a_local, cancel_event)
        if _local_dash_slice_valid(v_local) and _local_dash_slice_valid(a_local, audio=True):
            used_local = True
            v_input, a_input = v_local, a_local
            v_ss = max(0.0, start_sec - _range_lead_sec(video_url, v_range[0]))
            a_ss = max(0.0, start_sec - _range_lead_sec(audio_url, a_range[0]))
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)
            tmpdir = None

    cmd = [resolved, "-y", "-hide_banner", "-loglevel", "error"]
    remote = not used_local
    inp_hdr = (hdr + reconnect) if remote else []
    cmd += probe + inp_hdr + ["-ss", str(v_ss), "-i", v_input]
    cmd += probe + inp_hdr + ["-ss", str(a_ss), "-i", a_input]
    cmd += ["-t", str(duration), "-map", "0:v:0", "-map", "1:a:0"]
    enc_args = ffmpeg_h264_encode_args(video_encoder or "copy")
    if enc_args:
        cmd += enc_args
    elif _dash_video_needs_transcode(video_url, video_fmt) and output_path.lower().endswith(".mp4"):
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "copy"]
    else:
        cmd += ["-c", "copy"]
    if mp4_faststart:
        cmd += ["-movflags", "+faststart"]
    cmd.append(output_path)
    try:
        if progress_hook and remote:
            progress_hook({
                "status": "downloading",
                "percent": 5,
                "phase": "Downloading",
                "phase_id": _phase_id("Downloading"),
            })
        _run_ffmpeg(
            cmd,
            cancel_event=cancel_event,
            pause_event=pause_event,
            register_abort=register_abort,
            progress_hook=progress_hook,
            encode_duration=duration,
            progress_from=10.0,
            progress_to=92.0,
            phase="Muxing",
        )
        _verify_output_file(output_path)
        if progress_hook:
            progress_hook({
                "status": "postprocessing",
                "percent": 99,
                "phase": "Finalising",
                "phase_id": _phase_id("Finalising"),
            })
            progress_hook({"status": "finished"})
    except RuntimeError:
        if used_local and allow_remote_retry:
            shutil.rmtree(tmpdir, ignore_errors=True)
            tmpdir = None
            used_local = False
            v_input, a_input = video_url, audio_url
            v_ss, a_ss = start_sec, start_sec
            remote = True
            cmd = [resolved, "-y", "-hide_banner", "-loglevel", "error"]
            inp_hdr = hdr + reconnect
            cmd += probe + inp_hdr + ["-ss", str(v_ss), "-i", v_input]
            cmd += probe + inp_hdr + ["-ss", str(a_ss), "-i", a_input]
            cmd += ["-t", str(duration), "-map", "0:v:0", "-map", "1:a:0"]
            enc_args = ffmpeg_h264_encode_args(video_encoder or "copy")
            if enc_args:
                cmd += enc_args
            elif _dash_video_needs_transcode(video_url, video_fmt) and output_path.lower().endswith(".mp4"):
                cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "copy"]
            else:
                cmd += ["-c", "copy"]
            if mp4_faststart:
                cmd += ["-movflags", "+faststart"]
            cmd.append(output_path)
            _run_ffmpeg(
                cmd,
                cancel_event=cancel_event,
                pause_event=pause_event,
                register_abort=register_abort,
                progress_hook=progress_hook,
                encode_duration=duration,
                progress_from=10.0,
                progress_to=92.0,
                phase="Muxing",
            )
            _verify_output_file(output_path)
            if progress_hook:
                progress_hook({"status": "finished"})
        else:
            raise
    finally:
        if tmpdir:
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
    audio_only: bool = False,
) -> None:
    """Download only the HLS segments covering *start_sec*–*end_sec*."""
    from services.youtube_innertube import extract_video_id

    extract_opts = dict(opts)
    if extract_video_id(url):
        if not extract_opts.get("_youtube_session"):
            extract_opts = youtube_preview_ytdl_opts(
                url,
                oauth=opts.get("password"),
                cachedir=opts.get("cachedir"),
                cookies_file=opts.get("cookiefile"),
            )
        info = cached_extract_info(url, extract_opts)
    else:
        info = _extract_hls_info(url, opts)
    headers = info.get("http_headers") or {}
    ffmpeg_exe = _resolve_ffmpeg_exe(opts.get("ffmpeg_location"))

    if extract_video_id(url) and not _youtube_info_has_hls(info):
        https_formats = [
            f for f in info.get("formats") or []
            if f.get("url")
            and int(f.get("height") or 0) > 0
            and (f.get("protocol") or "").lower() in ("https", "http")
        ]
        if https_formats:
            video_fmt = _pick_youtube_clip_video_format(https_formats, prefer_height)
            vheaders = video_fmt.get("http_headers") or headers
            if _is_muxed_progressive(video_fmt):
                _download_progressive_clip(
                    video_fmt["url"], output_path, start_sec, end_sec, headers=vheaders,
                    ffmpeg_exe=ffmpeg_exe,
                    progress_hook=progress_hook,
                    cancel_event=cancel_event,
                    pause_event=pause_event,
                    register_abort=register_abort,
                    prefer_height=prefer_height,
                    video_encoder=video_encoder,
                    mp4_faststart=mp4_faststart,
                    audio_only=audio_only,
                )
                return
            audio_fmt = info.get("_preview_audio_format")
            if audio_fmt and audio_fmt.get("url") and not audio_only:
                _download_muxed_dash_clip(
                    video_fmt["url"], audio_fmt["url"], output_path,
                    start_sec, end_sec, headers=vheaders,
                    ffmpeg_exe=ffmpeg_exe,
                    progress_hook=progress_hook,
                    cancel_event=cancel_event,
                    pause_event=pause_event,
                    register_abort=register_abort,
                    video_encoder=video_encoder,
                    mp4_faststart=mp4_faststart,
                    video_fmt=video_fmt,
                )
                return

    fmt = _find_media_format(info)
    media_url = fmt["url"]
    headers = fmt.get("http_headers") or headers
    is_progressive = (fmt.get("protocol") or "").lower() in ("https", "http")

    if is_progressive and extract_video_id(url):
        _download_progressive_clip(
            media_url, output_path, start_sec, end_sec, headers=headers,
            ffmpeg_exe=ffmpeg_exe,
            progress_hook=progress_hook,
            cancel_event=cancel_event,
            pause_event=pause_event,
            register_abort=register_abort,
            prefer_height=prefer_height,
            video_encoder=video_encoder,
            mp4_faststart=mp4_faststart,
            audio_only=audio_only,
        )
        return

    clip_target = output_path
    temp_video: Optional[str] = None
    if audio_only:
        temp_video = tempfile.mktemp(suffix=".mp4", prefix="hls_audio_")
        clip_target = temp_video

    try:
        download_hls_media_clip(
            media_url, start_sec, end_sec, clip_target, headers=headers,
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
        if audio_only and temp_video:
            _extract_hls_audio(temp_video, output_path, ffmpeg_exe)
    finally:
        if temp_video and os.path.isfile(temp_video):
            os.unlink(temp_video)


assert _find_media_format({
    "formats": [{
        "url": "https://x/v.mp4",
        "protocol": "https",
        "height": 720,
        "vcodec": "avc1",
        "acodec": "mp4a",
    }],
}).get("height") == 720
assert _local_dash_slice_valid(__file__) is False
assert _local_dash_slice_valid(__file__, audio=True) is False
if __name__ == "__main__":
    assert _is_broken_pipe_error(BrokenPipeError())
    assert _is_broken_pipe_error(OSError(errno.EINVAL, "Invalid argument"))
    if os.name == "nt":
        assert _is_broken_pipe_error(OSError(22, "Invalid argument"))
        assert _is_broken_pipe_error(OSError(0, "pipe ended", None, 233))
