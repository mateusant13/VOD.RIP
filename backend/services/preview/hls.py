from __future__ import annotations
import hashlib
import json
import logging
import math
import os
import random
import re
import socket
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple
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
def _validate_proxy_url(url: str) -> bool:
    """Return True if the URL is safe to proxy (public host, not internal)."""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host or host in ("localhost", "127.0.0.1", "::1"):
        return False
    # Check if private IP (fast fail)
    try:
        ip = socket.getaddrinfo(host, 80, socket.AF_INET)[0][4][0]
        # RFC 1918, RFC 6598, RFC 6890
        if ip.startswith(
            (
                "10.",
                "172.16.",
                "172.17.",
                "172.18.",
                "172.19.",
                "172.20.",
                "172.21.",
                "172.22.",
                "172.23.",
                "172.24.",
                "172.25.",
                "172.26.",
                "172.27.",
                "172.28.",
                "172.29.",
                "172.30.",
                "172.31.",
                "192.168.",
                "127.",
                "169.254.",
                "0.",
            )
        ):
            return False
    except (socket.gaierror, IndexError):
        return False


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
            timeout=(_UPSTREAM_CONNECT_TIMEOUT_SEC, 90),
            http_version=http_version,
        )
    except ImportError:
        import requests

        resp = requests.get(url, headers=headers, stream=True, timeout=60)

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
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=_UPSTREAM_CHUNK_BYTES):
        if not chunk:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise RuntimeError(
                f"Upstream response exceeds {max_bytes} byte cap for preview fetch"
            )
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
    if cached and now - cached[1] < PLAYLIST_REWRITE_TTL_SEC:
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
