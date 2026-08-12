from __future__ import annotations
import hashlib
import json
import logging
import math
import os
import queue
import random
import re
import socket
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse
from services.ytdlp_service import (
    MIN_VALID_OUTPUT_BYTES,
    _build_ydl_opts,
    _extract_hls_info,
    _find_hls_format,
    build_url,
    detect_platform,
    is_clip_url,
)
from services.ytdlp_hls import _youtube_soft_neg_error

from services.preview.session import (
    MAX_SEGMENT_BYTES,
    PLAYLIST_REWRITE_TTL_SEC,
    PreviewSession,
    StalePreviewUrls,
    _AUTH_ERROR_CODES,
    _MAX_REWRITTEN_PLAYLIST_BYTES,
    _UPSTREAM_CHUNK_BYTES,
    _UPSTREAM_CONNECT_TIMEOUT_SEC,
    _bytes_response_for_range,
    _fetch_and_rewrite_playlist_streaming,
    _guess_content_type,
    _host_allowed,
    _is_playlist_url,
    _playlist_cache,
    _read_cache,
    _request_headers,
    _write_cache,
    _youtube_refresh_and_remap,
    get_session,
)
from services.preview._state import _validate_proxy_url

# Upstream stall bounds. curl_cffi's tuple timeout is NOT a per-read timeout:
# with stream=True it maps to CURLOPT_LOW_SPEED_LIMIT=1 + LOW_SPEED_TIME=ceil
# (connect+read) — a sub-1B/s transfer aborts only after the whole window, and
# a blocked iter_content() (bare queue.get()) holds the caller past it. The
# per-read value keeps that libcurl window tight; the wall-clock budgets below
# are enforced by _read_upstream_body's watchdog thread, so a 0 B/s CDN stall
# can never hang session creation or playback past the budget.
_UPSTREAM_READ_TIMEOUT_SEC = 10
_UPSTREAM_PLAYLIST_DEADLINE_SEC = 20.0  # master/media playlists, LL-HLS probes, archive
_UPSTREAM_SEGMENT_DEADLINE_SEC = 15.0  # segment/key/init fetches (proxy_segment)


def _read_upstream_body(
    resp: object,
    max_bytes: int,
    deadline_sec: float,
    url_label: str,
) -> list[bytes]:
    """Drain ``resp.iter_content`` under a hard wall-clock budget.

    The iteration runs in a daemon thread because a single chunk read can
    block far past any Python-side deadline: curl_cffi's streaming
    iter_content() is a bare queue.get() (bounded only by libcurl's low-speed
    abort, ceil(connect+read) seconds), and requests' read timeout is
    per-socket-recv. On expiry we raise RuntimeError and close the response
    from a cleanup thread so the connection is released without waiting on
    the abort.
    ponytail: one short-lived thread per fetch; move to a shared executor if
    profiling ever shows the spawn cost.
    """
    deadline = time.monotonic() + deadline_sec
    result: list[bytes] = []
    error: Optional[Exception] = None

    def _drain() -> None:
        nonlocal result, error
        try:
            total = 0
            for chunk in resp.iter_content(chunk_size=_UPSTREAM_CHUNK_BYTES):
                if not chunk:
                    continue
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"Upstream read stalled/timed out after {deadline_sec:.0f}s for {url_label}"
                    )
                result.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError(
                        f"Upstream response exceeds {max_bytes} byte cap for preview fetch"
                    )
        except Exception as exc:
            error = exc

    worker = threading.Thread(target=_drain, name="upstream-read", daemon=True)
    worker.start()
    worker.join(timeout=deadline_sec)
    if worker.is_alive():
        threading.Thread(
            target=_abort_upstream_read,
            args=(resp, worker),
            name="upstream-abort",
            daemon=True,
        ).start()
        raise RuntimeError(
            f"Upstream read stalled/timed out after {deadline_sec:.0f}s for {url_label}"
        )
    if error is not None:
        raise error
    return result


def _abort_upstream_read(resp: object, worker: threading.Thread) -> None:
    """Close a stalled upstream response and reap its drain thread.

    Runs in a daemon thread so the caller never waits on the abort: closing a
    curl_cffi stream waits for libcurl's low-speed abort, a requests stream
    for the blocked recv to error out.
    """
    try:
        resp.close()
    except Exception:
        pass
    worker.join(timeout=_UPSTREAM_READ_TIMEOUT_SEC * 2)


def _iter_upstream_bounded(
    resp: object,
    deadline_sec: float,
    url_label: str,
) -> Iterator[bytes]:
    """Yield ``resp.iter_content`` chunks under a wall-clock IDLE watchdog.

    Streaming sibling of _read_upstream_body: each chunk resets the budget,
    so a slow-but-flowing transfer completes while a 0 B/s stall (blocked
    iter_content queue.get) aborts after deadline_sec. On expiry the response
    is closed from a daemon thread (same reasoning as _read_upstream_body) and
    RuntimeError propagates to the caller, so the proxy responds fast instead
    of pinning a PREVIEW_EXECUTOR worker up to libcurl's 3600s low-speed
    window.
    ponytail: one short-lived drain thread per fetch; move to a shared
    executor if profiling ever shows the spawn cost.
    """
    q: "queue.Queue[object]" = queue.Queue(maxsize=64)
    drained: List[Exception] = []
    sentinel = object()

    def _drain() -> None:
        try:
            for chunk in resp.iter_content(chunk_size=_UPSTREAM_CHUNK_BYTES):
                q.put(chunk)
            q.put(sentinel)
        except Exception as exc:
            drained.append(exc)
            q.put(sentinel)

    worker = threading.Thread(target=_drain, name="upstream-drain", daemon=True)
    worker.start()
    aborted = False

    def _abort() -> None:
        nonlocal aborted
        if aborted:
            return
        aborted = True
        threading.Thread(
            target=_abort_upstream_read,
            args=(resp, worker),
            name="upstream-abort",
            daemon=True,
        ).start()

    try:
        deadline = time.monotonic() + deadline_sec
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _abort()
                raise RuntimeError(
                    f"Upstream read stalled/timed out after {deadline_sec:.0f}s for {url_label}"
                )
            try:
                chunk = q.get(timeout=remaining)
            except queue.Empty:
                continue
            if chunk is sentinel:
                if drained:
                    raise drained[0]
                return
            if chunk:
                deadline = time.monotonic() + deadline_sec
            yield chunk
    finally:
        # Consumer abandoned the stream early (byte cap, caller error) — reap
        # the drain thread without waiting on a possibly-stalled recv.
        if worker.is_alive() and not drained:
            _abort()


def _http_get_bytes(
    session: PreviewSession,
    url: str,
    range_header: Optional[str] = None,
    *,
    _retried: bool = False,
) -> Tuple[bytes, str, dict, int]:
    """Fetch upstream bytes. curl_cffi must use stream=False or .content is empty."""
    host = urlparse(url).hostname or ""
    if not _host_allowed(host, session):
        raise PermissionError(f"URL host not allowed for preview: {host}")
    if not _validate_proxy_url(url):
        raise PermissionError(f"URL resolves to private/internal address: {url[:80]}")

    headers = _request_headers(session, range_header, host=host)
    is_playlist = _is_playlist_url(url)
    # ponytail: 512KB was meant for key/init fetches; YouTube master playlists
    # for long VODs can be several MB. Use the rewritten-playlist cap (32MB)
    # for playlist fetches instead of the small key-fetch cap.
    max_bytes = (
        MAX_SEGMENT_BYTES
        if range_header
        else (_MAX_REWRITTEN_PLAYLIST_BYTES if is_playlist else MAX_SEGMENT_BYTES)
    )
    timeout = (_UPSTREAM_CONNECT_TIMEOUT_SEC, _UPSTREAM_READ_TIMEOUT_SEC)
    try:
        from curl_cffi import requests as cffi_requests

        # QUIC/HTTP3 for googlevideo CDN
        http_version = None
        if "googlevideo.com" in url:
            http_version = "v3"

        resp = cffi_requests.get(
            url,
            headers=headers,
            impersonate="chrome",
            stream=True,
            timeout=timeout,
            http_version=http_version,
        )
    except ImportError:
        import requests

        resp = requests.get(url, headers=headers, stream=True, timeout=timeout)

    if resp.status_code in _AUTH_ERROR_CODES:
        if not _retried and session.platform == "YouTube":
            new_url = _youtube_refresh_and_remap(session, url)
            if new_url:
                try:
                    resp.close()
                except OSError:
                    pass
                return _http_get_bytes(
                    session,
                    new_url,
                    range_header,
                    _retried=True,
                )
        raise StalePreviewUrls(f"upstream HTTP {resp.status_code} for {url[:80]}")
    resp.raise_for_status()
    deadline_sec = (
        _UPSTREAM_SEGMENT_DEADLINE_SEC
        if not is_playlist
        else _UPSTREAM_PLAYLIST_DEADLINE_SEC
    )
    chunks = _read_upstream_body(resp, max_bytes, deadline_sec, url[:80])
    try:
        resp.close()
    except OSError:
        pass
    data = b"".join(chunks)
    ctype = _guess_content_type(url, resp.headers.get("Content-Type", ""))
    if session.platform == "YouTube":
        from services.youtube_diag import log_preview_upstream

        note = ""
        if is_playlist and data and not data.lstrip().startswith(b"#EXTM3U"):
            note = "playlist_body_not_m3u8"
        elif not is_playlist and len(data) == 0:
            note = "empty_body"
        log_preview_upstream(
            "upstream_fetch",
            session.session_id,
            resp.status_code,
            len(data),
            ctype,
            url,
            note=note,
        )
    out_headers: dict = {"Accept-Ranges": "bytes"}
    for key in ("Content-Range", "Content-Length"):
        if key in resp.headers:
            out_headers[key] = resp.headers[key]
    if not out_headers.get("Content-Length") and data:
        out_headers["Content-Length"] = str(len(data))
    session.touch()
    status = resp.status_code
    if range_header and status == 200:
        status = 206
    return data, ctype, out_headers, status
from concurrent.futures import ThreadPoolExecutor as _TPE
from concurrent.futures import TimeoutError as FuturesTimeoutError
def proxy_master(
    session_id: str,
    range_header: Optional[str] = None,
) -> Tuple[bytes, str, dict, int]:
    """Serve the master resource: HLS playlist text or a single progressive MP4.

    For ``kind == "hls"`` this returns the rewritten master playlist text.
    For ``kind == "progressive"`` it streams the underlying MP4 through the
    preview proxy so the frontend can use a native ``<video>`` element.
    """
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")
    if session.kind == "progressive":
        raise ValueError("Use open_progressive_proxy for progressive streams")
    if (
        getattr(session, "dash_window_hls", False)
        and not session.custom_master
        and session.variant_entries
    ):
        # ponytail: warm-snapshot reuse can lose custom_master — rebuild the
        # window-HLS master from memory (variant entries + local resource URL)
        # instead of fetching upstream, which stalls the manifest request past
        # hls.js's manifestLoadingTimeOut and aborts it.
        from services.preview.session import _build_youtube_window_hls_master

        session.custom_master = _build_youtube_window_hls_master(session)
    if session.custom_master:
        data = session.custom_master.encode("utf-8")
        from services.youtube_diag import log_preview_upstream

        log_preview_upstream(
            "master_synthetic",
            session_id,
            200,
            len(data),
            "application/vnd.apple.mpegurl",
            session.entry_url or "",
        )
        return data, "application/vnd.apple.mpegurl", {"Cache-Control": "no-cache"}, 200
    body, ctype, headers, status = proxy_playlist(session_id, session.master_url)
    return body, ctype, headers, status
def proxy_playlist(session_id: str, upstream_url: str) -> Tuple[bytes, str, dict, int]:
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")

    now = time.time()
    cache = _playlist_cache(session)
    cached = cache.get(upstream_url)
    # Live sessions: media playlists refresh at 0.5s (lower latency); the
    # master keeps the module default so it is not re-fetched on every loop.
    ttl = PLAYLIST_REWRITE_TTL_SEC
    if session.playlist_ttl_sec > 0 and upstream_url != session.master_url:
        ttl = session.playlist_ttl_sec
    if cached and now - cached[1] < ttl:
        return (
            cached[0],
            "application/vnd.apple.mpegurl",
            {"Cache-Control": "no-cache"},
            200,
        )

    if _is_playlist_url(upstream_url):
        data, status = _fetch_and_rewrite_playlist_streaming(session, upstream_url)
        if not data.lstrip().startswith(b"#EXTM3U"):
            raise RuntimeError("Upstream playlist body is not HLS m3u8")
        cache[upstream_url] = (data, now)
        return (
            data,
            "application/vnd.apple.mpegurl",
            {"Cache-Control": "no-cache"},
            status,
        )

    data, _, _, status = _http_get_bytes(session, upstream_url)
    if not data:
        raise RuntimeError("Upstream playlist is empty")
    ctype = _guess_content_type(upstream_url)
    return data, ctype, {"Cache-Control": "no-cache"}, status
def proxy_segment(
    session_id: str,
    upstream_url: str,
    range_header: Optional[str] = None,
) -> Tuple[bytes, str, dict, int]:
    """Fetch a segment/key/init file (buffered — typical HLS segments are a few MB)."""
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")

    cached = _read_cache(session, upstream_url)
    if cached is not None:
        body, hdrs, status = _bytes_response_for_range(cached, range_header)
        return body, _guess_content_type(upstream_url), hdrs, status

    data, ctype, headers, status = _http_get_bytes(
        session, upstream_url, range_header=range_header
    )
    if len(data) > MAX_SEGMENT_BYTES:
        raise RuntimeError("Preview segment exceeds size limit")

    if range_header is None and data and not _is_playlist_url(upstream_url):
        _write_cache(session, upstream_url, data)
        headers["Cache-Control"] = "public, max-age=3600"

    return data, ctype, headers, status
