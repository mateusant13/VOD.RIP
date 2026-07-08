"""
HLS playlist parsing, segment downloading, and clip assembly ÔÇö the segment-level
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

from services import ytdlp_env  # noqa: F401 ÔÇö YTDLP_NO_PLUGINS before yt-dlp import
from services.ytdlp_guard import assert_ytdlp_safe, guarded_youtube_dl, YTDLP_EXTRACT_LOCK as _YTDLP_EXTRACT_LOCK
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
    """Redirect fd 2 ÔÇö yt-dlp writes ERROR lines past logging hooks."""
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
# yt-dlp getpot_wpc registers globally ÔÇö guarded_youtube_dl serializes all YoutubeDL().
_EXTRACT_CACHE_TTL_SEC = 6 * 3600
_EXTRACT_CACHE_MAX = 32
_EXTRACT_WAIT_SEC = 120
_YOUTUBE_EXTRACT_PARALLEL_SEC = 4.5
_YOUTUBE_PREVIEW_SOCKET_SEC = 5
_PREVIEW_EXTRACT_WAIT_SEC = 8.0
_PREVIEW_EXTRACT_MAX_WALL_SEC = 8.0
_PREVIEW_EXTRACT_FALLBACK_SEC = 22.0  # total wall incl. full yt-dlp retries on cache miss
_PREVIEW_MUX_FAST_SEC = 10.0  # ponytail: unused teaser cap ÔÇö mux uses session trim window
_PREVIEW_MUX_FAST_HEIGHT = 480


def _youtube_manual_auth_configured() -> bool:
    """True when user set cookies file, browser, po_token, or tokens file in Settings."""
    try:
        from deps import settings_mgr
        s = settings_mgr.get()
        path = (getattr(s, "youtube_cookies_file", "") or "").strip()
        if path and Path(path).is_file() and not Path(path).name.startswith("yt_anon_"):
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


def _youtube_has_user_auth(opts: dict) -> bool:
    """True only for user-configured auth — anonymous bootstrap cookie jar is not manual auth."""
    session = opts.get("_youtube_session")
    if session and getattr(session, "anonymous", False):
        return False
    if opts.get("cookiesfrombrowser"):
        return True
    if _youtube_manual_auth_configured():
        return True
    cookie_path = _youtube_cookie_path(opts)
    if cookie_path and not Path(cookie_path).name.startswith("yt_anon_"):
        return True
    return False


def invalidate_youtube_extract_cache(url: str) -> None:
    """Drop cached extract for *url* so the next resolve can try other clients."""
    from services.youtube_innertube import canonical_youtube_watch_url, extract_video_id

    keys = {(canonical_youtube_watch_url(url) or url)}
    vid = extract_video_id(url)
    if vid:
        keys.add(f"https://www.youtube.com/watch?v={vid}")
    prefixes = tuple(f"{k}|" for k in keys)
    with _EXTRACT_CACHE_LOCK:
        for key in list(_EXTRACT_INFO_CACHE):
            if key.startswith(prefixes):
                _EXTRACT_INFO_CACHE.pop(key, None)


assert invalidate_youtube_extract_cache.__name__ == "invalidate_youtube_extract_cache"


def _extract_cache_key(url: str, opts: dict) -> str:
    from services.youtube_innertube import canonical_youtube_watch_url

    cache_url = canonical_youtube_watch_url(url) or url
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
    return f"{cache_url}|{clients}|{bool(oauth)}|{cookie}|{browser}|{sess_key}"


def _youtube_url_from_opts(url: str, opts: dict) -> bool:
    from services.youtube_innertube import extract_video_id

    return extract_video_id(url) is not None


def _youtube_cookie_path(opts: dict) -> Optional[str]:
    path = (opts.get("cookiefile") or "").strip()
    if path and Path(path).is_file():
        return path
    return None


def _try_innertube_info(url: str, session=None, *, allow_session_refresh: bool = True) -> Optional[dict]:
    from services.youtube_innertube import innertube_extract_info

    try:
        return innertube_extract_info(
            url, session=session, allow_session_refresh=allow_session_refresh,
        )
    except Exception as exc:
        logger.debug("InnerTube extract error for %s: %s", url, exc)
        return None


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
        "socket_timeout": _YOUTUBE_PREVIEW_SOCKET_SEC,
        "_preview_fast": True,
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
    """Preview needs m3u8 ladder ÔÇö progressive-only yt-dlp hits must not be cached."""
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
    """Pick video for trimmed download ÔÇö prefer muxed MP4, then h264 DASH at height."""
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


def _try_innertube_info_retry(
    url: str, attempts: int = 1, session=None, *, allow_session_refresh: bool = True,
) -> Optional[dict]:
    """InnerTube multi-client chain ÔÇö one pass; outer extract loop handles retries."""
    for i in range(attempts):
        info = _try_innertube_info(
            url, session=session, allow_session_refresh=allow_session_refresh,
        )
        if info and _youtube_info_playable(info):
            return info
        if i + 1 < attempts:
            time.sleep(0.12 * (i + 1))
    return None


def _bare_ytdlp_opts(opts: dict) -> dict:
    """Strip all cookie/session auth ÔÇö last-resort yt-dlp pass."""
    return {
        k: v for k, v in opts.items()
        if k not in ("cookiefile", "cookiesfrombrowser", "_youtube_session")
    }


def _merge_fresh_youtube_session(opts: dict, url: str) -> dict:
    """Soft anonymous refresh — bootstrap new visitor cookies, no settings cookies_file."""
    from services.youtube_innertube import extract_video_id
    from services.youtube_session import (
        apply_ytdlp_cookie_opts,
        youtube_session_bootstrap_only,
        ytdlp_extractor_args,
    )

    vid = extract_video_id(url)
    fresh = youtube_session_bootstrap_only(video_id=vid, force=True)
    merged = dict(opts)
    merged["_youtube_session"] = fresh
    merged["extractor_args"] = ytdlp_extractor_args(fresh, auto_auth=False)
    merged.pop("cookiefile", None)
    merged.pop("cookiesfrombrowser", None)
    apply_ytdlp_cookie_opts(merged, fresh, auto_auth=False, cookies_file=None)
    return merged


def _youtube_extract_parallel_fast(
    url: str,
    opts: dict,
    yt_session,
    vid: str,
) -> Optional[dict]:
    """Race InnerTube vs yt-dlp+cookies ÔÇö first playable within ~4.5s wins (preview SLA)."""
    from services.youtube_diag import log_extract_ok

    cookie_opts = dict(opts)
    cookie_opts["socket_timeout"] = min(
        int(cookie_opts.get("socket_timeout") or 10), _YOUTUBE_PREVIEW_SOCKET_SEC,
    )

    def _inn() -> Optional[dict]:
        return _try_innertube_info_retry(
            url, session=yt_session, allow_session_refresh=False,
        )

    def _ydl() -> Optional[dict]:
        return _extract_hls_info_quiet(url, cookie_opts)

    winner: Optional[dict] = None
    source = ""
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="yt-extract")
    try:
        futures = {
            pool.submit(_inn): "innertube_pass",
            pool.submit(_ydl): "ytdlp_cookies",
        }
        deadline = time.monotonic() + _YOUTUBE_EXTRACT_PARALLEL_SEC
        pending = set(futures.keys())
        while pending and time.monotonic() < deadline:
            done, pending = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
            for fut in done:
                try:
                    info = fut.result()
                except Exception as exc:
                    logger.debug("parallel extract %s failed %s: %s", vid, futures[fut], exc)
                    continue
                if info and _youtube_info_playable(info):
                    winner = info
                    source = futures[fut]
                    break
            if winner:
                break
    finally:
        with contextlib.suppress(Exception):
            pool.shutdown(wait=False, cancel_futures=True)
    if winner:
        log_extract_ok(vid, source, winner, yt_session)
    return winner


def _youtube_extract_pass(url: str, opts: dict) -> Optional[dict]:
    from services.youtube_innertube import extract_video_id
    from services.youtube_diag import log_extract_ok

    yt_session = opts.get("_youtube_session")
    vid = extract_video_id(url) or url[:32]
    has_auth = _youtube_has_user_auth(opts)
    bare_opts = _bare_ytdlp_opts(opts)

    # Anonymous: race InnerTube vs yt-dlp+cookies (~4.5s), then bare last resort.
    if not has_auth:
        info = _youtube_extract_parallel_fast(url, opts, yt_session, vid)
        if info:
            return info
        if opts.get("_preview_fast"):
            return None
        info = _extract_hls_info_quiet(url, bare_opts)
        if info and _youtube_info_playable(info):
            log_extract_ok(vid, "ytdlp_bare", info, yt_session)
        return info

    if _youtube_cookie_path(opts) or opts.get("cookiesfrombrowser"):
        info = _extract_hls_info_quiet(url, opts)
        if info and _youtube_info_playable(info):
            log_extract_ok(vid, "ytdlp_cookies", info, yt_session)
            return info
    info = _try_innertube_info_retry(url, session=yt_session)
    if info and _youtube_info_playable(info):
        log_extract_ok(vid, "innertube_pass", info, yt_session)
        return info
    # ponytail: stale youtube_cookies_file blocks all clients — retry anon fast path once
    if has_auth and _youtube_manual_auth_configured():
        from services.youtube_diag import log_extract_fail

        log_extract_fail(vid, "auth_fallback_anon", yt_session, detail="manual cookies exhausted")
        anon_opts = _merge_fresh_youtube_session(_bare_ytdlp_opts(opts), url)
        anon_session = anon_opts.get("_youtube_session")
        info = _youtube_extract_parallel_fast(url, anon_opts, anon_session, vid)
        if info:
            return info
    info = _extract_hls_info_quiet(url, bare_opts)
    if info and _youtube_info_playable(info):
        log_extract_ok(vid, "ytdlp_bare", info, yt_session)
    return info


def _youtube_extract_with_retries(url: str, opts: dict, attempts: int = 3) -> dict:
    working = dict(opts)
    has_auth = _youtube_has_user_auth(working)
    max_attempts = attempts if has_auth else 2
    last_err: Optional[BaseException] = None
    refreshed = False
    for i in range(max_attempts):
        try:
            info = _youtube_extract_pass(url, working)
            if info and _youtube_info_playable(info):
                return info
        except Exception as exc:
            last_err = exc
        if i + 1 < max_attempts:
            if not has_auth and not refreshed:
                from services.youtube_session import invalidate_anonymous_session

                invalidate_anonymous_session()
                working = _merge_fresh_youtube_session(working, url)
                refreshed = True
            else:
                time.sleep(0.2 * (i + 1))
    if last_err is not None:
        raise last_err
    from services.youtube_innertube import extract_video_id
    from services.youtube_diag import log_extract_fail

    log_extract_fail(
        extract_video_id(url) or "?",
        "all_fallbacks_exhausted",
        working.get("_youtube_session"),
        final=True,
    )
    raise RuntimeError("YouTube preview unavailable for this video")


def _youtube_extract_preview_with_retries(url: str, opts: dict) -> dict:
    """Preview SLA: fast InnerTube race, then full retries before giving up."""
    t0 = time.monotonic()
    info = _youtube_extract_pass(url, opts)
    if info and _youtube_info_playable(info):
        return info
    if _youtube_has_user_auth(opts) and _youtube_manual_auth_configured():
        anon_opts = _merge_fresh_youtube_session(_bare_ytdlp_opts(opts), url)
        info = _youtube_extract_pass(url, anon_opts)
        if info and _youtube_info_playable(info):
            return info
    if not _youtube_has_user_auth(opts):
        from services.youtube_session import invalidate_anonymous_session

        invalidate_anonymous_session()
        working = _merge_fresh_youtube_session(dict(opts), url)
        info = _youtube_extract_pass(url, working)
        if info and _youtube_info_playable(info):
            return info
    # ponytail: channel VODs often miss the 8s fast path — allow full extract before 500
    if time.monotonic() - t0 < _PREVIEW_EXTRACT_FALLBACK_SEC:
        try:
            retry_opts = opts
            if _youtube_manual_auth_configured():
                retry_opts = _merge_fresh_youtube_session(_bare_ytdlp_opts(opts), url)
            return _youtube_extract_with_retries(url, retry_opts, attempts=3)
        except Exception as exc:
            logger.debug("preview extract fallback failed %s: %s", url[:60], exc)
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
        wait_sec = _PREVIEW_EXTRACT_WAIT_SEC if opts.get("_preview_fast") else _EXTRACT_WAIT_SEC
        if not event.wait(timeout=wait_sec):
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
            if opts.get("_preview_fast"):
                info = _youtube_extract_preview_with_retries(url, opts)
            else:
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
                "YouTube blocked this video — try again or add youtube_cookies_file in settings.json"
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
assert _youtube_has_user_auth({"_youtube_session": type("S", (), {"anonymous": True})(), "cookiefile": "/x"}) is False
assert isinstance(_YTDLP_EXTRACT_LOCK, type(threading.Lock()))
assert _youtube_info_has_hls({"formats": [{"url": "https://x/a.m3u8", "protocol": "m3u8_native"}]})
assert _youtube_info_use_clip_path({"formats": [{"url": "https://x/v.mp4", "protocol": "https", "height": 720}]})
assert _youtube_info_playable({"formats": [{"url": "https://x/v.mp4", "protocol": "https", "height": 720}]})


def warm_youtube_extract(url: str, oauth: Optional[str] = None, cookies_file: Optional[str] = None) -> bool:
    """Prefetch resolved-stream cache on hover — same path as create_session."""
    from services.youtube_innertube import extract_video_id

    if not extract_video_id(url):
        return False
    try:
        from services.preview_service import warm_youtube_preview_resolve

        return warm_youtube_preview_resolve(url, oauth=oauth, prefer_height=720)
    except Exception as exc:
        from services.youtube_diag import log_extract_fail
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
            with guarded_youtube_dl(ydl_opts) as ydl:
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
            "the FFmpeg folder in Settings ÔåÆ FFmpeg path."
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
        # ponytail: survival guarantee for per-segment download ÔÇö segment I/O may fail in many ways; skip to next segment
        except Exception as exc:
        # ponytail: subprocess/codec errors only ÔÇö best-effort encoder detection
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
        # Reserve 99 ÔåÆ 100 for the "FinalisingÔÇª" window (atomic replace
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
    # Cap end_sec at the actual media length ÔÇö prevents sentinel values
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
            "HLS clip selected %.1fs of media for %.1fs trim ÔÇö check segment selection",
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
        # ponytail: survival guarantee for register_pp_state callback ÔÇö arbitrary callback
        except Exception:
        # ponytail: best-effort ÔÇö register_temp_dir(tmpdir)
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


_INIT_PREFIX_BYTES = 5_000_000
_INIT_BOX_BYTES = 2_097_152
# ponytail: googlevideo CDN rejects some large mid-file Range spans — split fetches
_GOOGLEVIDEO_RANGE_CHUNK_BYTES = 1 * 1024 * 1024
# ponytail: ~16MB cap on contiguous Range from byte 0 — use init+media concat beyond this
_GOOGLEVIDEO_MAX_FROM_ZERO_BYTES = 16 * 1024 * 1024


def _range_lead_sec(url: str, byte_start: int) -> float:
    from services.size_estimate import _clen_bytes_from_url

    clen = _clen_bytes_from_url(url)
    dur = _dur_sec_from_googlevideo_url(url)
    if not clen or not dur:
        return 0.0
    return max(0.0, byte_start / clen * dur)


def _fetch_googlevideo_range_once(
    url: str,
    byte_range: tuple[int, int],
    headers: Optional[dict],
    dest: str,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """Single HTTP Range request to a local file."""
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


def _fetch_googlevideo_span_resilient(
    url: str,
    byte_range: tuple[int, int],
    headers: Optional[dict],
    dest: str,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """Fetch a byte span; bisect on 403 and tolerate missing tail bytes."""
    b0, b1 = byte_range
    try:
        _fetch_googlevideo_range_once(url, byte_range, headers, dest, cancel_event)
        return
    except Exception as exc:
        if b1 - b0 < 256 * 1024:
            if b0 > 0 and os.path.isfile(dest) and os.path.getsize(dest) >= 65536:
                logger.debug("googlevideo span tail dropped %d-%d: %s", b0, b1, exc)
                return
            raise
        mid = (b0 + b1) // 2
        part = f"{dest}.hi"
        try:
            _fetch_googlevideo_span_resilient(
                url, (b0, mid), headers, dest, cancel_event,
            )
            _fetch_googlevideo_span_resilient(
                url, (mid + 1, b1), headers, part, cancel_event,
            )
            with open(dest, "ab") as out, open(part, "rb") as src:
                shutil.copyfileobj(src, out)
        finally:
            if os.path.isfile(part):
                os.remove(part)


def _fetch_googlevideo_range(
    url: str,
    byte_range: tuple[int, int],
    headers: Optional[dict],
    dest: str,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """HTTP Range fetch of a googlevideo slice to a local file."""
    b0, b1 = byte_range
    if b1 - b0 <= _GOOGLEVIDEO_RANGE_CHUNK_BYTES:
        _fetch_googlevideo_span_resilient(url, byte_range, headers, dest, cancel_event)
        return
    with open(dest, "wb") as out:
        pos = b0
        while pos <= b1:
            end = min(pos + _GOOGLEVIDEO_RANGE_CHUNK_BYTES, b1)
            part = f"{dest}.part"
            try:
                _fetch_googlevideo_span_resilient(
                    url, (pos, end), headers, part, cancel_event,
                )
                with open(part, "rb") as src:
                    shutil.copyfileobj(src, out)
            except Exception as exc:
                if out.tell() >= 65536 and end >= b1 - _GOOGLEVIDEO_RANGE_CHUNK_BYTES:
                    logger.debug("googlevideo range tail skipped %d-%d: %s", pos, end, exc)
                    break
                raise
            finally:
                if os.path.isfile(part):
                    os.remove(part)
            pos = end + 1


def _grow_googlevideo_prefix_cache(
    url: str,
    headers: Optional[dict],
    cache_path: str,
    need_byte_end: int,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """Grow a byte-indexed googlevideo prefix file through need_byte_end."""
    cache = Path(cache_path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    have_end = cache.stat().st_size - 1 if cache.is_file() else -1
    if have_end >= need_byte_end:
        return
    start_b = max(0, have_end + 1)
    part = f"{cache_path}.grow"
    try:
        _fetch_googlevideo_range(url, (start_b, need_byte_end), headers, part, cancel_event)
        mode = "ab" if have_end >= 0 else "wb"
        with open(cache_path, mode) as out, open(part, "rb") as src:
            shutil.copyfileobj(src, out)
    finally:
        if os.path.isfile(part):
            os.remove(part)
    if not cache.is_file() or cache.stat().st_size < 65536:
        raise RuntimeError("googlevideo prefix cache grow failed")


def _concat_googlevideo_init_media(
    url: str,
    media: tuple[int, int],
    headers: Optional[dict],
    dest: str,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """Deep seek: init box + media byte window (concat) — avoids 16MB-from-zero CDN cap."""
    mb0, mb1 = media
    tmp = tempfile.mkdtemp(prefix="gv_deep_")
    try:
        init_p = os.path.join(tmp, "init.mp4")
        media_p = os.path.join(tmp, "media.mp4")
        init_end = min(_INIT_BOX_BYTES, max(mb0 - 1, 0))
        if init_end > 0:
            _fetch_googlevideo_range(url, (0, init_end), headers, init_p, cancel_event)
        _fetch_googlevideo_range(url, (mb0, mb1), headers, media_p, cancel_event)
        if init_end > 0 and os.path.isfile(init_p) and os.path.getsize(init_p) > 256:
            clist = os.path.join(tmp, "c.txt")
            with open(clist, "w", encoding="utf-8") as f:
                for p in (init_p, media_p):
                    f.write(f"file '{p.replace(chr(92), '/')}'\n")
            ff = _resolve_ffmpeg_exe()
            _run_ffmpeg([
                ff, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", clist, "-c", "copy", dest,
            ])
        elif os.path.isfile(media_p):
            shutil.copyfile(media_p, dest)
        else:
            raise RuntimeError("googlevideo deep window fetch produced no data")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fetch_googlevideo_window_local(
    url: str,
    start_sec: float,
    end_sec: float,
    headers: Optional[dict],
    dest: str,
    cancel_event: Optional[threading.Event] = None,
    prefix_cache: Optional[str] = None,
) -> float:
    """Fetch a seekable local googlevideo window; return ffmpeg -ss for the clip start."""
    media = _googlevideo_byte_range(url, start_sec, end_sec)
    if not media:
        raise RuntimeError("no googlevideo byte range for clip window")
    _mb0, mb1 = media
    if _mb0 > _GOOGLEVIDEO_MAX_FROM_ZERO_BYTES:
        _concat_googlevideo_init_media(url, media, headers, dest, cancel_event)
        return start_sec
    if prefix_cache:
        _grow_googlevideo_prefix_cache(url, headers, prefix_cache, mb1, cancel_event)
        shutil.copyfile(prefix_cache, dest)
        return start_sec
    _fetch_googlevideo_range(url, (0, mb1), headers, dest, cancel_event)
    return start_sec


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
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            head = handle.read(32)
        if b"ftyp" in head:
            return True
        # YouTube DASH audio is often webm-wrapped (EBML) even when muxed to m4a.
        if audio and head[:4] == b"\x1aE\xdf\xa3":
            return True
        # Mid-file DASH fragments lack ftyp — ffmpeg reads them with -ss after range fetch.
        return size >= 65536
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
    use_byte_range: bool = True,
    video_prefix_cache: Optional[str] = None,
    audio_prefix_cache: Optional[str] = None,
) -> None:
    """Trim separate video+audio googlevideo URLs and mux to MP4 or MPEG-TS."""
    duration = max(0.1, end_sec - start_sec)
    resolved = ffmpeg_exe or _resolve_ffmpeg_exe()
    hdr = _ffmpeg_input_headers(headers or {})
    reconnect = _ffmpeg_reconnect_args()
    probe = ["-probesize", "8M", "-analyzeduration", "2M"]
    is_ts = output_path.lower().endswith(".ts")

    v_range = (
        _googlevideo_byte_range(video_url, start_sec, end_sec) if use_byte_range else None
    )
    a_range = (
        _googlevideo_byte_range(audio_url, start_sec, end_sec) if use_byte_range else None
    )
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
        try:
            v_ss = _fetch_googlevideo_window_local(
                video_url, start_sec, end_sec, headers, v_local, cancel_event,
                prefix_cache=video_prefix_cache,
            )
            a_ss = _fetch_googlevideo_window_local(
                audio_url, start_sec, end_sec, headers, a_local, cancel_event,
                prefix_cache=audio_prefix_cache,
            )
            if _local_dash_slice_valid(v_local) and _local_dash_slice_valid(a_local, audio=True):
                used_local = True
                v_input, a_input = v_local, a_local
            else:
                shutil.rmtree(tmpdir, ignore_errors=True)
                tmpdir = None
        except Exception:
            shutil.rmtree(tmpdir, ignore_errors=True)
            tmpdir = None
            v_range = None

    cmd = [resolved, "-y", "-hide_banner", "-loglevel", "error"]
    remote = not used_local
    if remote and use_byte_range:
        raise RuntimeError("googlevideo dash clip requires local range fetch")
    inp_hdr = (hdr + reconnect) if remote else []
    cmd += probe + inp_hdr + ["-ss", str(v_ss), "-i", v_input]
    cmd += probe + inp_hdr + ["-ss", str(a_ss), "-i", a_input]
    cmd += ["-t", str(duration), "-map", "0:v:0", "-map", "1:a:0"]
    enc_args = ffmpeg_h264_encode_args(video_encoder or "copy")
    if enc_args:
        cmd += enc_args
    elif _dash_video_needs_transcode(video_url, video_fmt) and not is_ts:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "copy"]
    elif is_ts:
        # ponytail: m4a→mpegts needs AAC ADTS for browser MSE — plain -c copy is silent
        # initial_discontinuity: each on-demand segment resets PTS; pairs with #EXT-X-DISCONTINUITY
        cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-mpegts_flags", "+initial_discontinuity"]
    else:
        cmd += ["-c", "copy"]
    if is_ts:
        cmd += ["-f", "mpegts", output_path]
    elif mp4_faststart:
        cmd += ["-movflags", "+faststart", output_path]
    else:
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
        if used_local and allow_remote_retry and not use_byte_range:
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


def _mux_dash_window_to_hls(
    video_url: str,
    audio_url: str,
    output_dir: str,
    start_sec: float,
    end_sec: float,
    headers: Optional[dict] = None,
    video_prefix_cache: Optional[str] = None,
    audio_prefix_cache: Optional[str] = None,
) -> Path:
    """Mux a DASH crop window into a local HLS playlist (window.m3u8 + seg_NNN.ts).

    Reuses the ``_fetch_googlevideo_window_local`` Range-fetch path so we never
    pull the full adaptive-format MP4 — just the byte window the trim needs.
    Output is ``{output_dir}/window.m3u8`` (VOD playlist, independent segments,
    4-second target duration) so the frontend MSE player can attach the instant
    ``seg_000.ts`` lands and HLS.js finalises when ``#EXT-X-ENDLIST`` appears.
    """
    from services.ytdlp_ffmpeg import _resolve_ffmpeg_exe

    os.makedirs(output_dir, exist_ok=True)
    duration = max(0.1, end_sec - start_sec)
    ffmpeg_exe = _resolve_ffmpeg_exe()
    v_local = os.path.join(output_dir, "_v.mp4")
    a_local = os.path.join(output_dir, "_a.m4a")
    v_ss = _fetch_googlevideo_window_local(
        video_url, start_sec, end_sec, headers, v_local,
        prefix_cache=video_prefix_cache,
    )
    a_ss = _fetch_googlevideo_window_local(
        audio_url, start_sec, end_sec, headers, a_local,
        prefix_cache=audio_prefix_cache,
    )
    playlist = os.path.join(output_dir, "window.m3u8")
    seg_pattern = os.path.join(output_dir, "seg_%03d.ts")
    cmd = [
        ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
        "-probesize", "8M", "-analyzeduration", "2M",
        "-ss", str(v_ss), "-i", v_local,
        "-probesize", "8M", "-analyzeduration", "2M",
        "-ss", str(a_ss), "-i", a_local,
        "-map", "0:v:0", "-map", "1:a:0",
        "-t", str(duration),
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k",
        "-f", "hls",
        "-hls_time", "4",
        "-hls_playlist_type", "vod",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename", seg_pattern,
        playlist,
    ]
    _run_ffmpeg(
        cmd,
        encode_duration=duration,
        progress_from=10.0,
        progress_to=92.0,
        phase="Muxing",
    )
    playlist_path = Path(playlist)
    if not playlist_path.is_file() or playlist_path.stat().st_size < 32:
        raise RuntimeError("window HLS mux produced no playlist")
    return playlist_path


def _resolve_youtube_audio_format(info: dict) -> Optional[dict]:
    """Best HTTPS audio-only stream from InnerTube info."""
    tagged = info.get("_preview_audio_format")
    if tagged and tagged.get("url"):
        return tagged
    candidates = [
        f for f in info.get("formats") or []
        if f.get("url")
        and (f.get("protocol") or "").lower() in ("https", "http")
        and f.get("acodec") not in (None, "none")
        and f.get("vcodec") in (None, "none")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.get("abr") or f.get("tbr") or 0)


def _ytdlp_audio_section_download(
    url: str,
    output_path: str,
    start_sec: float,
    end_sec: float,
    opts: dict,
    progress_hook: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    register_abort: Optional[Callable[[Callable[[], None]], None]] = None,
) -> None:
    """ponytail: yt-dlp section + extract when DASH audio URL unavailable."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import download_range_func

    tmpdir = tempfile.mkdtemp(prefix="ytdlp_aud_")
    try:
        out_tmpl = os.path.join(tmpdir, "clip.%(ext)s")
        ydl_opts = dict(opts)
        ydl_opts.update({
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "bestaudio/best",
            "download_ranges": download_range_func(None, [(start_sec, end_sec)]),
            "force_keyframes_at_cuts": True,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
        if progress_hook:
            ydl_opts["progress_hooks"] = [progress_hook]
        with guarded_youtube_dl(ydl_opts) as ydl:
            if register_abort:
                register_abort(lambda: getattr(ydl, "cancel_download", lambda: None)())
            _check_pause_cancel(cancel_event, pause_event)
            ydl.download([url])
        mp3s = sorted(Path(tmpdir).glob("clip.*"))
        if not mp3s:
            raise RuntimeError("yt-dlp audio section produced no output")
        src = str(mp3s[0])
        if src != output_path:
            if os.path.isfile(output_path):
                os.unlink(output_path)
            shutil.move(src, output_path)
        _verify_output_file(output_path)
        if progress_hook:
            progress_hook({"status": "finished"})
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
    audio_only: bool = False,
) -> None:
    """Download only the HLS segments covering *start_sec*ÔÇô*end_sec*."""
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
        if audio_only:
            audio_fmt = _resolve_youtube_audio_format(info)
            if audio_fmt and audio_fmt.get("url"):
                aheaders = audio_fmt.get("http_headers") or headers
                _download_progressive_clip(
                    audio_fmt["url"], output_path, start_sec, end_sec, headers=aheaders,
                    ffmpeg_exe=ffmpeg_exe,
                    progress_hook=progress_hook,
                    cancel_event=cancel_event,
                    pause_event=pause_event,
                    register_abort=register_abort,
                    prefer_height=prefer_height,
                    video_encoder=video_encoder,
                    mp4_faststart=mp4_faststart,
                    audio_only=True,
                )
                return
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
        if audio_only:
            _ytdlp_audio_section_download(
                url, output_path, start_sec, end_sec, extract_opts,
                progress_hook=progress_hook,
                cancel_event=cancel_event,
                pause_event=pause_event,
                register_abort=register_abort,
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


def ytdlp_section_mux_to_ts(
    url: str,
    output_path: str,
    start_sec: float,
    end_sec: float,
    oauth: Optional[str] = None,
    cookies_file: Optional[str] = None,
) -> None:
    """ponytail: yt-dlp section download when googlevideo Range seek fails (deep scrub)."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import download_range_func

    tmpdir = tempfile.mkdtemp(prefix="ytdlp_seg_")
    try:
        base = os.path.join(tmpdir, "clip")
        opts = youtube_preview_ytdl_opts(url, oauth=oauth, cookies_file=cookies_file)
        opts.update({
            "outtmpl": base + ".%(ext)s",
            "download_ranges": download_range_func(None, [(start_sec, end_sec)]),
            "force_keyframes_at_cuts": True,
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
        })
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
        mp4s = sorted(Path(tmpdir).glob("clip.*"))
        if not mp4s:
            raise RuntimeError("yt-dlp section download produced no output")
        src = str(mp4s[0])
        if output_path.lower().endswith(".ts"):
            ff = _resolve_ffmpeg_exe()
            _run_ffmpeg([
                ff, "-y", "-hide_banner", "-loglevel", "error",
                "-i", src, "-c", "copy", "-f", "mpegts", output_path,
            ])
        else:
            shutil.copyfile(src, output_path)
        _verify_output_file(output_path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


assert _resolve_youtube_audio_format({
    "formats": [
        {"url": "https://x/v.mp4", "protocol": "https", "height": 720, "vcodec": "avc1", "acodec": "mp4a"},
        {"url": "https://x/a.m4a", "protocol": "https", "vcodec": "none", "acodec": "mp4a", "abr": 128},
    ],
})["url"] == "https://x/a.m4a"
assert _find_media_format({
    "formats": [{
        "url": "https://x/v.mp4",
        "protocol": "https",
        "height": 720,
        "vcodec": "avc1",
        "acodec": "mp4a",
    }],
}).get("height") == 720
assert _local_dash_slice_valid("nonexistent_dash_slice_abc123") is False
assert _local_dash_slice_valid("nonexistent_dash_slice_abc123", audio=True) is False
assert _GOOGLEVIDEO_RANGE_CHUNK_BYTES == 1 * 1024 * 1024
assert _GOOGLEVIDEO_MAX_FROM_ZERO_BYTES == 16 * 1024 * 1024
assert _mux_dash_window_to_hls.__name__ == "_mux_dash_window_to_hls"
if __name__ == "__main__":
    assert _is_broken_pipe_error(BrokenPipeError())
    assert _is_broken_pipe_error(OSError(errno.EINVAL, "Invalid argument"))
    if os.name == "nt":
        assert _is_broken_pipe_error(OSError(22, "Invalid argument"))
        assert _is_broken_pipe_error(OSError(0, "pipe ended", None, 233))
