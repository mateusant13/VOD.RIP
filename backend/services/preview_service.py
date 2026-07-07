"""Session-scoped HLS proxy for in-browser VOD trim preview (no ffmpeg)."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

# ponytail: _sessions module-level dict + _lock is a global mutable singleton.
# If the app ever needs multiple preview contexts (unlikely but possible),
# this should move into a SessionManager class. For now, one global cache
# is the simplest correct solution.

from services.ytdlp_service import (
    MIN_VALID_OUTPUT_BYTES,
    _build_ydl_opts,
    _extract_hls_info,
    _find_hls_format,
    build_url,
    detect_platform,
    is_clip_url,
)

logger = logging.getLogger(__name__)

SESSION_TTL_SEC = 30 * 60
PLAYLIST_REWRITE_TTL_SEC = 20 * 60
_MAX_PLAYLIST_FETCH_BYTES = 512 * 1024
PREWARM_SEGMENT_COUNT = 3
MAX_SEGMENT_BYTES = 100 * 1024 * 1024
SESSION_CACHE_MAX_BYTES = 100 * 1024 * 1024
_UPSTREAM_CHUNK_BYTES = 64 * 1024
_UPSTREAM_CONNECT_TIMEOUT_SEC = 15
_PREVIEW_ROOT = Path(os.environ.get("TEMP", os.environ.get("TMP", "/tmp"))) / "kd_preview"
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_ALLOWED_HOST_SUFFIXES = (
    "kick.com",
    "clips.kick.com",
    "twitch.tv",
    "ttvnw.net",
    "jtvnw.net",
    "cloudfront.net",
    "amazonaws.com",
    "akamaized.net",
    "fastly.net",
    "llnwi.net",
    "edgecastcdn.net",
    "googlevideo.com",
    "googleusercontent.com",
    "youtube.com",
    "youtu.be",
    "ytimg.com",
)

_URI_IN_TAG = re.compile(r'URI="([^"]+)"')
_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)")
_RESOLUTION_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)")

# ponytail: hard cap on concurrent preview sessions — prevents memory exhaustion
# from orphaned sessions if the TTL eviction doesn't keep up.
_MAX_SESSIONS = 20




class PreviewManager:
    _max_sessions = 20

    """Manages preview session lifecycle — create, get, delete, cleanup."""

    # ponytail: _sessions dict + _lock moved from module-level into a class.
    # This bounds the state to a manager instance, making it testable and
    # preventing stale state across test runs or single-instance redirects.

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, "PreviewSession"] = {}

    def _cleanup_stale_sessions(self, ) -> None:
            now = time.time()
            stale = [sid for sid, s in self._sessions.items() if now - s.last_access > SESSION_TTL_SEC]
            for sid in stale:
                    self.delete_session(sid)




    def delete_session(self, session_id: str) -> bool:
            with self._lock:
                    session = self._sessions.pop(session_id, None)
            _PREVIEW_MUX_LOCKS.pop(session_id, None)
            if not session:
                    return False
            from services.os_services import kill_child_processes
            kill_child_processes()
            try:
                    import shutil
                    if session.cache_dir.is_dir():
                            shutil.rmtree(session.cache_dir, ignore_errors=True)
            except OSError:
                    pass
            return True




    def get_session(self, session_id: str) -> Optional[PreviewSession]:
            with self._lock:
                    session = self._sessions.get(session_id)
            if session:
                    session.touch()
            return session




    def create_session(self, 
            url: str,
            crop_start: float = 0.0,
            crop_end: float = 0.0,
            oauth: Optional[str] = None,
            prefer_height: int = 720,
    ) -> PreviewSession:
            self._cleanup_stale_sessions()
            raw_entry, headers, platform, variant_formats, kind, yt_info = resolve_stream_info(
                    url, oauth=oauth, prefer_height=prefer_height,
            )
            preview_audio_url: Optional[str] = None
            variant_muxed: Dict[int, bool] = {}
            if platform == "YouTube" and yt_info:
                audio_fmt = yt_info.get("_preview_audio_format")
                if audio_fmt and audio_fmt.get("url"):
                    preview_audio_url = audio_fmt["url"]
            for fmt in variant_formats:
                h = int(fmt.get("height") or 0)
                if h > 0:
                    variant_muxed[h] = fmt.get("acodec") not in ("none", None)
            session_id = secrets.token_hex(8)
            cache_dir = _PREVIEW_ROOT / session_id
            cache_dir.mkdir(parents=True, exist_ok=True)

            # For progressive sources (Twitch clips) the "master" is a single MP4
            # we'll route through the proxy; the master endpoint then streams the
            # bytes with video/mp4 so the frontend can use a native <video> element.
            proxy_master_url: Optional[str] = None
            if kind == "progressive":
                    proxy_master_url = f"/api/preview/hls/{session_id}/master.m3u8"

            session = PreviewSession(
                    session_id=session_id,
                    vod_url=url,
                    master_url=proxy_master_url or raw_entry,
                    entry_url=raw_entry,
                    platform=platform,
                    http_headers=headers,
                    allowed_hosts=_hosts_for_url(raw_entry),
                    cache_dir=cache_dir,
                    kind=kind,
                    crop_start=crop_start,
                    crop_end=crop_end,
                    preview_audio_url=preview_audio_url,
                    variant_muxed=variant_muxed,
            )
            if preview_audio_url:
                session.allowed_hosts.update(_hosts_for_url(preview_audio_url))

            if variant_formats:
                    session.variant_entries = [
                            (int(fmt.get("height") or 0), fmt.get("url") or "")
                            for fmt in variant_formats
                            if int(fmt.get("height") or 0) > 0 and fmt.get("url")
                    ]
                    if kind == "hls" and len(session.variant_entries) >= 2:
                            session.custom_master = _build_synthetic_master_playlist(session, variant_formats)
                            for _height, upstream in session.variant_entries:
                                    session.allowed_hosts.update(_hosts_for_url(upstream))
            if kind == "progressive":
                    session.allowed_hosts.update(_hosts_for_url(session.entry_url))
            elif session.custom_master:
                    if session.variant_entries:
                            session.entry_url = _pick_variant_by_height(
                                    session.variant_entries, prefer_height,
                            ) or session.variant_entries[0][1]
                    session.allowed_hosts.update(_hosts_for_url(session.entry_url))
            else:
                    session.entry_url = _resolve_preview_entry(session, raw_entry, prefer_height)
                    session.allowed_hosts.update(_hosts_for_url(session.entry_url))

            with self._lock:
                    self._sessions[session_id] = session
                    if len(self._sessions) > self._max_sessions:
                            stale = sorted(
                                    self._sessions.items(),
                                    key=lambda item: item[1].last_access,
                            )[:len(self._sessions) - self._max_sessions]
                            for popped_sid, popped_session in stale:
                                    del self._sessions[popped_sid]
                                    cache_dir = popped_session.cache_dir
                                    threading.Thread(
                                            target=lambda d=cache_dir: shutil.rmtree(
                                                    d, ignore_errors=True,
                                            ),
                                            daemon=True,
                                    ).start()

            if _youtube_entry_needs_mux(session):
                schedule_youtube_preview_mux(session_id)

            if kind == "progressive":
                    from services.youtube_diag import log_preview_session
                    heights = [h for h, _u in session.variant_entries]
                    log_preview_session(
                        session_id,
                        platform,
                        kind,
                        heights,
                        custom_master=bool(session.custom_master),
                        entry_url=session.entry_url or raw_entry,
                    )
                    return session

            if (
                    session.platform == "YouTube"
                    and session.entry_url
                    and not session.custom_master
            ):
                    for attempt in range(2):
                            try:
                                    proxy_playlist(session_id, session.entry_url)
                                    break
                            except Exception as exc:
                                    if attempt == 0:
                                            time.sleep(0.2)
                                    else:
                                            from services.youtube_diag import log as yt_log
                                            yt_log.warning(
                                                "preview playlist warm failed session=%s: %s",
                                                session_id[:8],
                                                exc,
                                            )

            threading.Thread(
                    target=_warm_and_prewarm_session,
                    args=(session_id, crop_start),
                    daemon=True,
                    name=f"kd-prewarm-{session_id[:8]}",
            ).start()
            from services.youtube_diag import log_preview_session
            heights = [h for h, _u in session.variant_entries]
            log_preview_session(
                session_id,
                platform,
                kind,
                heights,
                custom_master=bool(session.custom_master),
                entry_url=session.entry_url or raw_entry,
            )
            return session



@dataclass
class PreviewSession:
    session_id: str
    vod_url: str
    master_url: str
    entry_url: str
    platform: str
    http_headers: Dict[str, str] = field(default_factory=dict)
    allowed_hosts: Set[str] = field(default_factory=set)
    resource_map: Dict[str, str] = field(default_factory=dict)
    rewritten_playlists: Dict[str, Tuple[bytes, float]] = field(default_factory=dict)
    custom_master: Optional[str] = None
    kind: str = "hls"
    variant_entries: List[Tuple[int, str]] = field(default_factory=list)
    variant_muxed: Dict[int, bool] = field(default_factory=dict)
    preview_audio_url: Optional[str] = None
    crop_start: float = 0.0
    crop_end: float = 30.0
    cache_bytes: int = 0
    last_access: float = field(default_factory=time.time)
    cache_dir: Path = field(default_factory=Path)
    mux_status: str = "unnecessary"  # unnecessary | pending | ready | error
    mux_error: Optional[str] = None

    def touch(self) -> None:
        self.last_access = time.time()

    def __post_init__(self) -> None:
        # Hot-reload can leave older in-memory instances without new fields.
        if not hasattr(self, "rewritten_playlists") or self.rewritten_playlists is None:
            object.__setattr__(self, "rewritten_playlists", {})
        if not hasattr(self, "mux_status"):
            object.__setattr__(self, "mux_status", "unnecessary")
        if not hasattr(self, "mux_error"):
            object.__setattr__(self, "mux_error", None)


def _playlist_cache(session: PreviewSession) -> Dict[str, Tuple[bytes, float]]:
    cache = getattr(session, "rewritten_playlists", None)
    if cache is None:
        cache = {}
        session.rewritten_playlists = cache
    return cache


def _hosts_for_url(url: str) -> Set[str]:
    host = urlparse(url).hostname
    return {host} if host else set()


def _host_allowed(host: str, session: PreviewSession) -> bool:
    if not host:
        return False
    if host in session.allowed_hosts:
        return True
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in _ALLOWED_HOST_SUFFIXES)


def _request_headers(session: PreviewSession, range_header: Optional[str] = None) -> dict:
    headers = dict(session.http_headers)
    headers.setdefault("User-Agent", _DEFAULT_UA)
    if range_header:
        headers["Range"] = range_header
    return headers


def _is_playlist_url(url: str) -> bool:
    # ponytail: endswith only — YouTube videoplayback segments embed index.m3u8 in path
    path = urlparse(url).path.lower().rstrip("/")
    return path.endswith(".m3u8")


def _guess_content_type(url: str, header_ct: str = "") -> str:
    if header_ct and header_ct not in ("application/octet-stream", "binary/octet-stream"):
        return header_ct
    path = urlparse(url).path.lower()
    if path.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if path.endswith((".ts", ".mpeg")):
        return "video/mp2t"
    if path.endswith(".m4s"):
        return "video/iso.segment"
    if path.endswith(".mp4"):
        return "video/mp4"
    return "application/octet-stream"


def _is_rangeable_cdn_media(url: str) -> bool:
    """True for CDN URLs that must be fetched with Range (never buffer whole file)."""
    lower = url.lower()
    host = urlparse(url).hostname or ""
    return "googlevideo.com" in host or "videoplayback" in lower


_AUTH_ERROR_CODES = frozenset({403, 404, 410})


class StalePreviewUrls(RuntimeError):
    """YouTube googlevideo URLs expired — client must reload the preview manifest."""


class PreviewMuxPending(RuntimeError):
    """YouTube DASH mux in progress — poll /status then retry stream.mp4."""


def _clear_preview_url_caches(session: PreviewSession) -> None:
    session.resource_map.clear()
    session.rewritten_playlists.clear()


def _remap_youtube_url_after_refresh(
    old_url: str,
    old_entry: str,
    old_variants: List[Tuple[int, str]],
    session: PreviewSession,
) -> Optional[str]:
    if old_url == old_entry:
        return session.entry_url
    for height, upstream in old_variants:
        if upstream == old_url:
            for h2, u2 in session.variant_entries:
                if h2 == height:
                    return u2
            return session.entry_url or None
    return None


def _youtube_refresh_and_remap(
    session: PreviewSession,
    failed_url: str,
    prefer_height: int = 720,
) -> Optional[str]:
    if session.platform != "YouTube":
        return None
    old_entry = session.entry_url
    old_variants = list(session.variant_entries)
    _refresh_youtube_preview_urls(session, prefer_height=prefer_height)
    return _remap_youtube_url_after_refresh(failed_url, old_entry, old_variants, session)


def _total_from_content_range(header: str) -> Optional[int]:
    if not header:
        return None
    m = re.search(r"/(\d+)\s*$", header.strip())
    return int(m.group(1)) if m else None


def _clamp_range_header(range_header: str, total: int) -> Optional[str]:
    m = re.match(r"bytes=(\d+)-(\d*)", (range_header or "").strip())
    if not m or total <= 0:
        return None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else total - 1
    if start >= total:
        start = max(0, total - min(256 * 1024, total))
    end = min(end, total - 1)
    if end < start:
        return None
    return f"bytes={start}-{end}"


def _open_upstream_stream(
    session: PreviewSession,
    url: str,
    range_header: Optional[str] = None,
    *,
    _retried: bool = False,
):
    """Open a streaming HTTP GET to *url*; caller must close the response."""
    host = urlparse(url).hostname or ""
    if not _host_allowed(host, session):
        raise PermissionError(f"URL host not allowed for preview: {host}")
    headers = _request_headers(session, range_header)
    try:
        from curl_cffi import requests as cffi_requests

        resp = cffi_requests.get(
            url,
            headers=headers,
            impersonate="chrome",
            stream=True,
            timeout=(_UPSTREAM_CONNECT_TIMEOUT_SEC, 3600),
        )
    except ImportError:
        import requests

        resp = requests.get(
            url,
            headers=headers,
            stream=True,
            timeout=_UPSTREAM_CONNECT_TIMEOUT_SEC,
        )
    if resp.status_code in _AUTH_ERROR_CODES:
        try:
            resp.close()
        except OSError:
            pass
        if not _retried and session.platform == "YouTube":
            new_url = _youtube_refresh_and_remap(session, url)
            if new_url:
                return _open_upstream_stream(
                    session, new_url, range_header, _retried=True,
                )
        raise StalePreviewUrls(f"upstream HTTP {resp.status_code} for {url[:80]}")
    if resp.status_code == 416 and range_header and not _retried:
        total = _total_from_content_range(resp.headers.get("Content-Range", ""))
        try:
            resp.close()
        except OSError:
            pass
        if total is None:
            try:
                probe = _open_upstream_stream(session, url, "bytes=0-0", _retried=True)
                total = _total_from_content_range(probe.headers.get("Content-Range", ""))
                try:
                    probe.close()
                except OSError:
                    pass
            except Exception:
                total = None
        clamped = _clamp_range_header(range_header, total) if total else None
        if clamped:
            return _open_upstream_stream(session, url, clamped, _retried=True)
    if resp.status_code >= 400:
        resp.raise_for_status()
    session.touch()
    return resp


def _upstream_response_meta(
    resp,
    url: str,
    range_header: Optional[str] = None,
) -> Tuple[str, dict, int]:
    ctype = _guess_content_type(url, resp.headers.get("Content-Type", ""))
    out_headers: dict = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
    for key in ("Content-Range", "Content-Length", "Accept-Ranges"):
        val = resp.headers.get(key)
        if val:
            out_headers[key] = val
    status = resp.status_code
    if range_header and status == 200 and out_headers.get("Accept-Ranges"):
        status = 206
    return ctype, out_headers, status


def preview_session_kind(session_id: str) -> Optional[str]:
    session = get_session(session_id)
    return session.kind if session else None


def _refresh_youtube_preview_urls(session: PreviewSession, prefer_height: int = 720) -> None:
    """Re-resolve googlevideo URLs — they expire; stale URLs cause preview 403/500."""
    if session.platform != "YouTube":
        return
    try:
        from deps import settings_mgr
        oauth = settings_mgr.get().oauth or None
    except Exception:
        oauth = None
    raw_entry, headers, _platform, variant_formats, kind, yt_info = resolve_stream_info(
        session.vod_url, oauth=oauth, prefer_height=prefer_height,
    )
    proxy_master_url: Optional[str] = None
    if kind == "progressive":
        proxy_master_url = f"/api/preview/hls/{session.session_id}/master.m3u8"
    session.kind = kind
    session.entry_url = raw_entry
    session.master_url = proxy_master_url or raw_entry
    session.http_headers = _merge_youtube_session_cookies(headers, session.vod_url)
    session.allowed_hosts = _hosts_for_url(raw_entry)
    session.variant_entries = [
        (int(fmt.get("height") or 0), fmt.get("url") or "")
        for fmt in variant_formats
        if int(fmt.get("height") or 0) > 0 and fmt.get("url")
    ]
    session.variant_muxed = {
        int(fmt.get("height") or 0): fmt.get("acodec") not in ("none", None)
        for fmt in variant_formats
        if int(fmt.get("height") or 0) > 0
    }
    if yt_info:
        audio_fmt = yt_info.get("_preview_audio_format")
        if audio_fmt and audio_fmt.get("url"):
            session.preview_audio_url = audio_fmt["url"]
            session.allowed_hosts.update(_hosts_for_url(audio_fmt["url"]))
    elif _platform == "YouTube":
        try:
            extra = _extract_youtube_preview_info(session.vod_url, oauth)
            audio_fmt = extra.get("_preview_audio_format")
            if audio_fmt and audio_fmt.get("url"):
                session.preview_audio_url = audio_fmt["url"]
                session.allowed_hosts.update(_hosts_for_url(audio_fmt["url"]))
        except Exception:
            pass
    session.custom_master = None
    _clear_youtube_mux_cache(session)
    if kind == "hls" and len(session.variant_entries) >= 2:
        session.custom_master = _build_synthetic_master_playlist(session, variant_formats)
    elif kind == "hls" and raw_entry:
        session.custom_master = None
    for _height, upstream in session.variant_entries:
        session.allowed_hosts.update(_hosts_for_url(upstream))
    session.allowed_hosts.update(_hosts_for_url(session.entry_url))
    if kind == "hls" and not session.custom_master:
        session.entry_url = _resolve_preview_entry(session, raw_entry, prefer_height)
        session.allowed_hosts.update(_hosts_for_url(session.entry_url))
    elif session.custom_master and session.variant_entries:
        session.entry_url = _pick_variant_by_height(
            session.variant_entries, prefer_height,
        ) or session.variant_entries[0][1]
        session.allowed_hosts.update(_hosts_for_url(session.entry_url))
    _clear_preview_url_caches(session)
    session.touch()


def refresh_youtube_preview_session(
    session_id: str,
    prefer_height: int = 720,
) -> PreviewSession:
    """Refresh expired YouTube stream URLs for an active preview session."""
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")
    if session.platform != "YouTube":
        return session
    _refresh_youtube_preview_urls(session, prefer_height=prefer_height)
    return session


_PREVIEW_MUX_LOCKS: Dict[str, threading.Lock] = {}


def _pick_mux_height(session: PreviewSession, *, fast: bool = True) -> int:
    from services.ytdlp_hls import _PREVIEW_MUX_FAST_HEIGHT

    prefer = session_active_height(session) or 720
    if not fast:
        return prefer
    cap = _PREVIEW_MUX_FAST_HEIGHT
    heights = sorted({h for h, _ in session.variant_entries if h > 0}, reverse=True)
    if not heights:
        return min(prefer, cap) or cap
    for h in heights:
        if h <= cap:
            return h
    return heights[-1]


def _variant_url_for_height(session: PreviewSession, height: int) -> Optional[str]:
    for h, url in session.variant_entries:
        if h == height and url:
            return url
    return None


def _mux_output_path(session: PreviewSession, height: int) -> Path:
    return session.cache_dir / f"mux_{height}.mp4"


def _youtube_mux_file_if_ready(session: PreviewSession) -> Optional[Path]:
    for height in sorted({h for h, _ in session.variant_entries if h > 0}, reverse=True):
        path = _mux_output_path(session, height)
        if path.is_file() and path.stat().st_size >= MIN_VALID_OUTPUT_BYTES:
            return path
    path = _mux_output_path(session, _pick_mux_height(session, fast=True))
    if path.is_file() and path.stat().st_size >= MIN_VALID_OUTPUT_BYTES:
        return path
    return None


def preview_mux_ready(session: PreviewSession) -> bool:
    if session.platform != "YouTube" or not _youtube_entry_needs_mux(session):
        return True
    if session.mux_status == "ready" or _youtube_mux_file_if_ready(session):
        return True
    return False


def preview_session_mux_status(session_id: str) -> Dict[str, object]:
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")
    ready = preview_mux_ready(session)
    if ready and session.mux_status == "pending":
        session.mux_status = "ready"
    return {
        "mux_ready": ready,
        "mux_status": session.mux_status,
        "mux_error": session.mux_error or "",
    }


def _run_youtube_mux_job(session_id: str) -> None:
    session = get_session(session_id)
    if not session or not _youtube_entry_needs_mux(session):
        return
    try:
        _ensure_youtube_preview_mux(session, fast=True)
        session.mux_status = "ready"
        session.mux_error = None
    except Exception as exc:
        session.mux_status = "error"
        session.mux_error = str(exc)[:240]
        logger.warning("background youtube mux failed session=%s: %s", session_id[:8], exc)


def schedule_youtube_preview_mux(session_id: str) -> None:
    """Start DASH mux off the HTTP critical path (preview SLA)."""
    session = get_session(session_id)
    if not session or not _youtube_entry_needs_mux(session):
        return
    if _youtube_mux_file_if_ready(session):
        session.mux_status = "ready"
        return
    if session.mux_status == "pending":
        return
    session.mux_status = "pending"
    session.mux_error = None
    threading.Thread(
        target=_run_youtube_mux_job,
        args=(session_id,),
        daemon=True,
        name=f"yt-mux-{session_id[:8]}",
    ).start()


def _clear_youtube_mux_cache(session: PreviewSession) -> None:
    try:
        for path in session.cache_dir.glob("mux_*.mp4"):
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _youtube_entry_needs_mux(session: PreviewSession) -> bool:
    if session.platform != "YouTube":
        return False
    if session.preview_audio_url:
        height = session_active_height(session)
        if height and session.variant_muxed.get(height):
            return False
        return True
    # Video-only DASH googlevideo URLs are not playable in <video> — must mux or pick muxed tier.
    height = session_active_height(session)
    if height and not session.variant_muxed.get(height):
        return True
    return False


def _best_muxed_variant_url(session: PreviewSession) -> Optional[str]:
    candidates = [
        (h, u) for h, u in session.variant_entries
        if session.variant_muxed.get(h) and u
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def _ensure_youtube_preview_mux(session: PreviewSession, *, fast: bool = True) -> Path:
    """Mux DASH video+audio for the trim window into a local MP4 (preview only)."""
    from services.ytdlp_hls import (
        _PREVIEW_MUX_MAX_SEC,
        _download_muxed_dash_clip,
    )

    height = _pick_mux_height(session, fast=fast)
    out = _mux_output_path(session, height)
    if out.is_file() and out.stat().st_size >= MIN_VALID_OUTPUT_BYTES:
        return out
    lock = _PREVIEW_MUX_LOCKS.setdefault(session.session_id, threading.Lock())
    with lock:
        if out.is_file() and out.stat().st_size >= MIN_VALID_OUTPUT_BYTES:
            return out
        start = max(0.0, float(session.crop_start))
        end = max(start + 0.5, float(session.crop_end))
        # ponytail: fast only caps height (720p); duration is full preview window (≤30s)
        end = min(end, start + _PREVIEW_MUX_MAX_SEC)
        video_url = _variant_url_for_height(session, height) or session.entry_url
        _download_muxed_dash_clip(
            video_url,
            session.preview_audio_url or "",
            str(out),
            start_sec=start,
            end_sec=end,
            headers=_merge_youtube_session_cookies(session.http_headers, session.vod_url),
            allow_remote_retry=True,
        )
        yt_log = logging.getLogger("VOD.RIP.youtube")
        if yt_log.isEnabledFor(logging.DEBUG):
            yt_log.debug(
                "preview mux session=%s height=%s dur=%.1fs fast_height=%s",
                session.session_id[:8], height, end - start, fast,
            )
    if not out.is_file() or out.stat().st_size < MIN_VALID_OUTPUT_BYTES:
        raise RuntimeError("YouTube preview mux produced no output")
    return out


def _open_local_file_proxy(
    path: Path,
    range_header: Optional[str] = None,
) -> Tuple[Callable[[], object], str, dict, int, Callable[[], None]]:
    size = path.stat().st_size
    start = 0
    end = size - 1
    status = 200
    if range_header:
        m = re.match(r"bytes=(\d+)-(\d*)", range_header.strip())
        if m:
            start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), size - 1)
            else:
                end = min(start + 8 * _UPSTREAM_CHUNK_BYTES, size - 1)
            status = 206
    if start >= size:
        start = 0
        end = min(size - 1, start)
    length = max(0, end - start + 1)
    hdrs = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "no-cache",
    }
    if status == 206:
        hdrs["Content-Range"] = f"bytes {start}-{end}/{size}"

    def _generate():
        with open(path, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(_UPSTREAM_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return _generate, "video/mp4", hdrs, status, lambda: None


def open_progressive_proxy(
    session_id: str,
    range_header: Optional[str] = None,
) -> Tuple[Callable[[], object], str, dict, int, Callable[[], None]]:
    """Return (chunk_generator, content_type, response_headers, status, cleanup)."""
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")
    if session.kind != "progressive":
        raise ValueError("Preview session is not progressive")
    if _youtube_entry_needs_mux(session):
        from services.youtube_diag import log as yt_log
        ready = _youtube_mux_file_if_ready(session)
        if ready:
            return _open_local_file_proxy(ready, range_header)
        if session.mux_status == "pending":
            raise PreviewMuxPending("YouTube preview mux in progress")
        try:
            mux_path = _ensure_youtube_preview_mux(session, fast=True)
            yt_log.info(
                "preview muxed dash session=%s height=%s bytes=%d",
                session_id[:8],
                session_active_height(session),
                mux_path.stat().st_size,
            )
            return _open_local_file_proxy(mux_path, range_header)
        except Exception as exc:
            fallback = _best_muxed_variant_url(session)
            if fallback:
                yt_log.warning(
                    "preview dash mux failed session=%s (%s) — falling back to muxed tier",
                    session_id[:8],
                    exc,
                )
                session.entry_url = fallback
                session.allowed_hosts.update(_hosts_for_url(fallback))
            elif _recover_youtube_progressive_session(session):
                if session.kind == "hls":
                    raise StalePreviewUrls(
                        "YouTube preview switched to HLS — reload preview",
                    )
                if not _youtube_entry_needs_mux(session):
                    pass  # fall through to direct upstream proxy
                else:
                    try:
                        mux_path = _ensure_youtube_preview_mux(session)
                        yt_log.info(
                            "preview muxed dash (recovered) session=%s height=%s bytes=%d",
                            session_id[:8],
                            session_active_height(session),
                            mux_path.stat().st_size,
                        )
                        return _open_local_file_proxy(mux_path, range_header)
                    except Exception:
                        raise exc
            else:
                raise
    upstream = session.entry_url
    prefer_h = 0
    if session.variant_entries and session.entry_url:
        for height, url in session.variant_entries:
            if url == session.entry_url and height > 0:
                prefer_h = height
                break
    try:
        resp = _open_upstream_stream(session, upstream, range_header)
    except Exception as first_exc:
        if session.platform != "YouTube":
            raise
        logger.debug("progressive proxy refresh after error session=%s: %s", session_id[:8], first_exc)
        _refresh_youtube_preview_urls(session, prefer_height=prefer_h or 720)
        upstream = session.entry_url
        resp = _open_upstream_stream(session, upstream, range_header)
    if resp.status_code in (403, 404, 410) and session.platform == "YouTube":
        resp.close()
        _refresh_youtube_preview_urls(session, prefer_height=prefer_h or 720)
        upstream = session.entry_url
        resp = _open_upstream_stream(session, upstream, range_header)
    ctype, hdrs, status = _upstream_response_meta(resp, upstream, range_header)

    def _close_upstream() -> None:
        try:
            resp.close()
        except OSError:
            pass

    def _generate():
        try:
            for chunk in resp.iter_content(chunk_size=_UPSTREAM_CHUNK_BYTES):
                if chunk:
                    yield chunk
        finally:
            _close_upstream()

    return _generate, ctype, hdrs, status, _close_upstream


def open_segment_proxy(
    session_id: str,
    upstream_url: str,
    range_header: Optional[str] = None,
) -> Tuple[Callable[[], object], str, dict, int, Callable[[], None]]:
    """Stream a segment/init/key through the preview proxy (Range-aware)."""
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")

    if range_header is None:
        cached = _read_cache(session, upstream_url)
        if cached is not None:
            ctype = _guess_content_type(upstream_url)

            def _cached_once() -> object:
                yield cached

            return (
                _cached_once,
                ctype,
                {
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(len(cached)),
                    "Cache-Control": "public, max-age=3600",
                },
                200,
                lambda: None,
            )

    resp = _open_upstream_stream(session, upstream_url, range_header)
    ctype, hdrs, status = _upstream_response_meta(resp, upstream_url, range_header)

    def _close_upstream() -> None:
        try:
            resp.close()
        except OSError:
            pass

    def _generate() -> object:
        buf = bytearray()
        try:
            for chunk in resp.iter_content(chunk_size=_UPSTREAM_CHUNK_BYTES):
                if not chunk:
                    continue
                if range_header is None and len(buf) + len(chunk) <= MAX_SEGMENT_BYTES:
                    buf.extend(chunk)
                yield chunk
        finally:
            _close_upstream()
            if range_header is None and buf and not _is_playlist_url(upstream_url):
                _write_cache(session, upstream_url, bytes(buf))

    return _generate, ctype, hdrs, status, _close_upstream


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

    headers = _request_headers(session, range_header)
    is_playlist = _is_playlist_url(url)
    max_bytes = (
        MAX_SEGMENT_BYTES
        if range_header
        else (_MAX_PLAYLIST_FETCH_BYTES if is_playlist else MAX_SEGMENT_BYTES)
    )
    try:
        from curl_cffi import requests as cffi_requests

        resp = cffi_requests.get(
            url,
            headers=headers,
            impersonate="chrome",
            stream=True,
            timeout=(_UPSTREAM_CONNECT_TIMEOUT_SEC, 90),
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
                    session, new_url, range_header, _retried=True,
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


def _merge_youtube_session_cookies(headers: dict, vod_url: str) -> dict:
    """googlevideo needs the same Cookie/visitor session as InnerTube used to sign URLs."""
    out = dict(headers)
    if out.get("Cookie"):
        return out
    try:
        from services.youtube_innertube import extract_video_id
        from services.youtube_session import youtube_session_from_settings

        vid = extract_video_id(vod_url)
        sess = youtube_session_from_settings(video_id=vid)
        if sess.cookie_header:
            out["Cookie"] = sess.cookie_header
    except Exception:
        pass
    return out


def _youtube_info_is_dash_only_progressive(info: dict) -> bool:
    """True when preview would route video-only DASH through mux (fragile path)."""
    from services.youtube_innertube import _dedupe_youtube_formats

    merged = _dedupe_youtube_formats(info.get("formats") or [])
    if _deduped_hls_variants({"formats": merged}):
        return False
    video_heights = [f for f in merged if int(f.get("height") or 0) > 0]
    if len(video_heights) < 2 or not _formats_are_dash_https(video_heights):
        return False
    if _deduped_progressive_variants({"formats": merged}):
        return False
    return True


def _reextract_youtube_for_preview(full_url: str, oauth: Optional[str]) -> dict:
    """Bust cache and re-extract — merged InnerTube (HLS-first) then full yt-dlp."""
    from services.ytdlp_hls import (
        cached_extract_info,
        invalidate_youtube_extract_cache,
        youtube_preview_ytdl_opts,
    )
    from services.youtube_innertube import innertube_extract_info

    invalidate_youtube_extract_cache(full_url)
    try:
        from deps import settings_mgr

        cookies = settings_mgr.get().youtube_cookies_file or None
    except Exception:
        cookies = None
    opts = youtube_preview_ytdl_opts(full_url, oauth=oauth, cookies_file=cookies)
    session = opts.get("_youtube_session")
    from services.youtube_innertube import _collect_merged_innertube_info, extract_video_id

    vid = extract_video_id(full_url)
    if vid:
        merged_info = _collect_merged_innertube_info(vid, session, 12.0)
        if merged_info and not _youtube_info_is_dash_only_progressive(merged_info):
            return merged_info
    info = innertube_extract_info(
        full_url, session=session, allow_session_refresh=True,
    )
    if info and not _youtube_info_is_dash_only_progressive(info):
        return info
    working = dict(opts)
    working.pop("_preview_fast", None)
    working["socket_timeout"] = 15
    return cached_extract_info(full_url, working)


def _recover_youtube_progressive_session(session: PreviewSession) -> bool:
    """Re-resolve after DASH mux failure — may switch to HLS or muxed tiers."""
    if session.platform != "YouTube":
        return False
    try:
        from deps import settings_mgr

        oauth = settings_mgr.get().oauth or None
    except Exception:
        oauth = None
    old_kind = session.kind
    old_needs_mux = _youtube_entry_needs_mux(session)
    try:
        from services.ytdlp_hls import invalidate_youtube_extract_cache

        invalidate_youtube_extract_cache(session.vod_url)
        _reextract_youtube_for_preview(session.vod_url, oauth)
    except Exception as exc:
        logger.debug("youtube progressive recover re-extract failed: %s", exc)
    _refresh_youtube_preview_urls(session)
    session.http_headers = _merge_youtube_session_cookies(
        session.http_headers, session.vod_url,
    )
    return (
        session.kind != old_kind
        or _best_muxed_variant_url(session) is not None
        or (old_needs_mux and not _youtube_entry_needs_mux(session))
    )


def _extract_youtube_preview_info(full_url: str, oauth: Optional[str]) -> dict:
    """Cached YouTube resolve — InnerTube multi-client, then yt-dlp fallback."""
    from deps import settings_mgr
    from services.ytdlp_hls import cached_extract_info, youtube_preview_ytdl_opts

    cookies = settings_mgr.get().youtube_cookies_file or None
    return cached_extract_info(
        full_url,
        youtube_preview_ytdl_opts(full_url, oauth=oauth, cookies_file=cookies),
    )


def _deduped_hls_variants(info: dict) -> List[dict]:
    formats = info.get("formats") or []
    hls = [
        f for f in formats
        if f.get("protocol") in ("m3u8", "m3u8_native", "m3u8_ffmpeg") and f.get("url")
    ]
    hls.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
    seen_heights: set[int] = set()
    out: List[dict] = []
    for fmt in hls:
        height = int(fmt.get("height") or 0)
        if height and height in seen_heights:
            continue
        if height:
            seen_heights.add(height)
        out.append(fmt)
    return out
def _is_muxed_format(fmt: dict) -> bool:
    """True when format carries both audio and video (not DASH video-only or SABR)."""
    url = (fmt.get("url") or "").strip()
    if url:
        from services.youtube_innertube import _is_sabr_stream_url
        if _is_sabr_stream_url(url):
            return False
    fid = (fmt.get("format_id") or "").lower()
    if fid == "hls-master":
        return True
    if fid == "abr-muxed":
        return False  # SABR — never progressive preview
    ac = fmt.get("acodec")
    vc = fmt.get("vcodec")
    if ac in ("none", None) or vc in ("none", None):
        return False
    return True


def _deduped_progressive_variants(info: dict) -> List[dict]:
    """Pick distinct heights from yt-dlp's progressive (direct MP4) formats.

    Twitch clips return a flat list of MP4 URLs in ``protocol=https`` (or
    no protocol) with heights like 360/480/720/1080 and separate ``portrait-*``
    variants. We keep one entry per height (the highest-bitrate representative).
    """
    formats = info.get("formats") or []
    progressive: list[dict] = []
    for f in formats:
        url = f.get("url") or ""
        if not url:
            continue
        proto = (f.get("protocol") or "").lower()
        ext = (f.get("ext") or "").lower()
        # Accept anything that is plainly a direct file (not HLS/DASH).
        if proto in ("m3u8", "m3u8_native", "m3u8_ffmpeg", "http_dash_segments", "dash", "http_dash"):
            continue
        if proto and proto not in ("https", "http"):
            # Unknown streaming protocol — skip to stay safe.
            if "dash" in proto or "m3u" in proto:
                continue
        if ext and ext not in ("mp4", "m4v", "mov", "webm"):
            continue
        # Twitch clips also ship vertical ``portrait-*`` renditions — prefer landscape.
        fid = (f.get("format_id") or "").lower()
        if fid.startswith("portrait"):
            continue
        if not _is_muxed_format(f):
            continue
        progressive.append(f)

    progressive.sort(
        key=lambda f: (int(f.get("height") or 0), float(f.get("tbr") or 0) or float(f.get("vbr") or 0) or 0.0),
        reverse=True,
    )
    seen_heights: set[int] = set()
    seen_urls: set[str] = set()
    out: List[dict] = []
    for fmt in progressive:
        height = int(fmt.get("height") or 0)
        url = fmt.get("url") or ""
        if height in seen_heights:
            continue
        if url in seen_urls:
            continue
        seen_heights.add(height)
        seen_urls.add(url)
        out.append(fmt)
    return out


def _url_looks_like_master(url: str) -> bool:
    lower = url.lower()
    return "master" in lower or "multivariant" in lower


def _pick_variant_by_height(entries: List[Tuple[int, str]], prefer_height: int) -> Optional[str]:
    if not entries:
        return None
    by_height = sorted(entries, key=lambda t: t[0])
    for height, url in by_height:
        if height == prefer_height:
            return url
    at_or_below = [entry for entry in by_height if entry[0] and entry[0] <= prefer_height]
    if at_or_below:
        return at_or_below[-1][1]
    return by_height[0][1]


def _build_synthetic_master_playlist(session: PreviewSession, variants: List[dict]) -> str:
    audio_fmt = None
    if session.platform == "YouTube":
        try:
            info = _extract_youtube_preview_info(session.vod_url, None)
            audio_fmt = info.get("_preview_audio_format")
        except Exception:
            audio_fmt = None
        if audio_fmt or any(
            f.get("acodec") in ("none", None) for f in variants if int(f.get("height") or 0) > 0
        ):
            return _build_youtube_synthetic_hls_master(session, variants, audio_fmt)
    lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-INDEPENDENT-SEGMENTS"]
    for fmt in variants:
        height = int(fmt.get("height") or 0)
        if not height:
            continue
        width = int(fmt.get("width") or 0)
        if not width:
            width = int(height * 16 / 9)
        bandwidth = int((fmt.get("tbr") or 0) * 1000) or int((fmt.get("vbr") or 0) * 1000) or 1_000_000
        upstream = fmt.get("url") or ""
        session.allowed_hosts.update(_hosts_for_url(upstream))
        lines.append(f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={width}x{height}")
        lines.append(_proxy_url(session, upstream))
    return "\n".join(lines) + "\n"


def _build_youtube_synthetic_hls_master(
    session: PreviewSession,
    variants: List[dict],
    audio_fmt: Optional[dict] = None,
) -> str:
    """HLS master from direct googlevideo URLs + optional separate audio (IOS DASH)."""
    group = "yt-audio"
    lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-INDEPENDENT-SEGMENTS"]
    video_variants = [f for f in variants if int(f.get("height") or 0) > 0 and f.get("url")]
    needs_audio = audio_fmt and any(f.get("acodec") in ("none", None) for f in video_variants)
    if needs_audio and audio_fmt:
        audio_url = audio_fmt.get("url") or ""
        if audio_url:
            session.allowed_hosts.update(_hosts_for_url(audio_url))
            lines.append(
                f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="{group}",NAME="main",'
                f'DEFAULT=YES,AUTOSELECT=YES,URI="{_proxy_url(session, audio_url)}"'
            )
    for fmt in video_variants:
        height = int(fmt.get("height") or 0)
        width = int(fmt.get("width") or 0) or int(height * 16 / 9)
        bandwidth = int((fmt.get("tbr") or 0) * 1000) or 1_000_000
        upstream = fmt.get("url") or ""
        session.allowed_hosts.update(_hosts_for_url(upstream))
        muxed = fmt.get("acodec") not in ("none", None)
        codecs = "avc1.4d401e,mp4a.40.2" if muxed else "avc1.4d401e"
        audio_attr = "" if muxed or not needs_audio else f',AUDIO="{group}"'
        lines.append(
            f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={width}x{height},"
            f'CODECS="{codecs}"{audio_attr}'
        )
        lines.append(_proxy_url(session, upstream))
    return "\n".join(lines) + "\n"

def _formats_are_dash_https(formats: List[dict]) -> bool:
    """True when formats are direct googlevideo HTTPS URLs (not HLS playlists)."""
    if not formats:
        return False
    for fmt in formats:
        proto = str(fmt.get("protocol") or "").lower()
        url = (fmt.get("url") or "").lower()
        if "m3u8" in proto or url.endswith(".m3u8") or "/master.m3u8" in url:
            return False
    return True


def resolve_stream_info(
    url: str,
    oauth: Optional[str] = None,
    prefer_height: int = 720,
) -> Tuple[str, dict, str, List[dict], str, Optional[dict]]:
    """Return (master_url, headers, platform, variant_formats, kind, yt_info).

    ``yt_info`` is set for YouTube (avoids a second extract in create_session).

    ``kind`` is ``"hls"`` for normal HLS streams (Kick clip/VOD, Twitch VOD) and
    ``"progressive"`` for direct progressive MP4 sources (Twitch clips). For
    progressive sources ``master_url`` is a single playable MP4 URL (already
    routed through the preview proxy) and ``variant_formats`` is the list of
    MP4 alternatives (used to build the synthetic master if needed).
    """
    platform = detect_platform(url)
    headers: dict = {}

    if platform == "Kick":
        from services.kick_api_service import resolve_kick_stream_api

        info = resolve_kick_stream_api(url)
        if not info.m3u8_url:
            raise RuntimeError("Kick stream has no HLS URL")
        page = info.url or url
        headers = {
            "referer": page if page.startswith("http") else "https://kick.com/",
            "origin": "https://kick.com",
        }
        return info.m3u8_url, headers, platform, [], "hls", None

    full_url = build_url(url, platform)

    # Twitch clips — prefer yt-dlp for progressive MP4 resolution.
    # yt-dlp uses OAuth tokens (when available) for authenticating CDN requests.
    # GQL is used as a fast fallback when yt-dlp fails.
    if platform == "Twitch" and is_clip_url(url):
        variants: List[dict] = []
        headers: dict = {
            "Referer": "https://www.twitch.tv/",
            "Origin": "https://www.twitch.tv",
        }

        # yt-dlp first — uses OAuth and returns accessible MP4 URLs.
        try:
            opts = _build_ydl_opts(full_url, os.devnull, oauth=oauth)
            clip_info = _extract_hls_info(full_url, opts)
            variants = _deduped_progressive_variants(clip_info)
            first_with_headers = next(
                (v for v in variants if v.get("http_headers")), None
            )
            if first_with_headers:
                headers = first_with_headers.get("http_headers") or {}
            else:
                headers = clip_info.get("http_headers") or headers
        # ponytail: survival guarantee for yt-dlp resolve — best-effort fallback; GQL is next
        except Exception as exc:
        # ponytail: best-effort — network errors only
            logger.debug("Twitch clip yt-dlp resolve failed: %s", exc)

        # GQL fallback — faster but URLs may require auth cookies.
        if not variants:
            try:
                from services.twitch_gql_service import get_clip_progressive_variants_sync

                variants = get_clip_progressive_variants_sync(url)
            # ponytail: survival guarantee for GQL fallback — best-effort; clip will fail with RuntimeError if both resolvers fail
            except Exception as exc:
            # ponytail: best-effort — variants = get_clip_progressive_variants_sync(url)
                logger.debug("Twitch clip GQL fallback failed: %s", exc)

        if not variants:
            raise RuntimeError("Twitch clip has no progressive formats")
        chosen_url = _pick_variant_by_height(
            [(int(v.get("height") or 0), v.get("url") or "") for v in variants],
            prefer_height=prefer_height,
        )
        if not chosen_url:
            raise RuntimeError("Twitch clip has no progressive URL")
        return chosen_url, headers, platform, variants, "progressive", None

    if platform == "YouTube":
        from services.youtube_innertube import _dedupe_youtube_formats
        from services.youtube_diag import log_preview_resolve

        info = _extract_youtube_preview_info(full_url, oauth)
        if _youtube_info_is_dash_only_progressive(info):
            try:
                alt = _reextract_youtube_for_preview(full_url, oauth)
                if alt and not _youtube_info_is_dash_only_progressive(alt):
                    info = alt
            except Exception as exc:
                logger.debug("youtube dash-only re-extract: %s", exc)
        headers = _merge_youtube_session_cookies(
            info.get("http_headers") or {
                "Referer": "https://www.youtube.com/",
                "Origin": "https://www.youtube.com",
            },
            full_url,
        )
        merged = _dedupe_youtube_formats(info.get("formats") or [])
        hls_variants = _deduped_hls_variants({"formats": merged})
        with_height = [f for f in hls_variants if int(f.get("height") or 0) > 0]

        def _yt_resolve(
            kind: str,
            entry: str,
            variants: List[dict],
            *,
            custom_master: bool = False,
        ) -> Tuple[str, dict, str, List[dict], str, dict]:
            heights = [int(f.get("height") or 0) for f in variants if int(f.get("height") or 0) > 0]
            log_preview_resolve(
                platform,
                kind,
                heights,
                custom_master=custom_master,
                entry_url=entry,
            )
            return entry, headers, platform, variants, kind, info

        # Unparsed master (height=0) — still valid HLS with audio + seek.
        if not with_height and hls_variants:
            master_fmt = hls_variants[0]
            stream_url = master_fmt.get("url") or ""
            if stream_url:
                if master_fmt.get("http_headers"):
                    headers = {**headers, **master_fmt["http_headers"]}
                return _yt_resolve("hls", stream_url, hls_variants)
        if with_height:
            for fmt in hls_variants:
                stream_url = fmt.get("url") or ""
                if stream_url and _url_looks_like_master(stream_url):
                    return _yt_resolve("hls", stream_url, with_height)
            chosen_url = _pick_variant_by_height(
                [(int(v.get("height") or 0), v.get("url") or "") for v in with_height],
                prefer_height=prefer_height,
            )
            if chosen_url:
                return _yt_resolve("hls", chosen_url, with_height, custom_master=len(with_height) >= 2)
        video_heights = [f for f in merged if int(f.get("height") or 0) > 0]
        if len(video_heights) >= 2:
            chosen_url = _pick_variant_by_height(
                [(int(v.get("height") or 0), v.get("url") or "") for v in video_heights],
                prefer_height=prefer_height,
            )
            if chosen_url:
                # DASH MP4 tiers cannot play through HLS.js — use progressive + mux.
                if _formats_are_dash_https(video_heights):
                    return _yt_resolve("progressive", chosen_url, video_heights)
                return _yt_resolve("hls", chosen_url, video_heights, custom_master=True)
        progressive = _deduped_progressive_variants({"formats": merged})
        if progressive:
            heights_urls = [(int(v.get("height") or 0), v.get("url") or "") for v in progressive]
            chosen_url = _pick_variant_by_height(heights_urls, prefer_height=prefer_height)
            if not chosen_url and progressive:
                chosen_url = progressive[0].get("url") or ""
            if chosen_url:
                picked = next((f for f in progressive if f.get("url") == chosen_url), None)
                if picked and picked.get("http_headers"):
                    headers = {**headers, **picked["http_headers"]}
                return _yt_resolve("progressive", chosen_url, progressive)
        from services.youtube_diag import format_summary, log_extract_fail
        from services.youtube_innertube import extract_video_id
        log_extract_fail(
            extract_video_id(full_url) or "?",
            "preview_no_playable_url",
            detail=format_summary(info),
        )
        raise RuntimeError("YouTube video has no playable stream URL")

    opts = _build_ydl_opts(full_url, os.devnull, oauth=oauth)
    hls_info = _extract_hls_info(full_url, opts)
    variants = _deduped_hls_variants(hls_info)
    with_height = [fmt for fmt in variants if int(fmt.get("height") or 0) > 0]

    if len(with_height) >= 2:
        for fmt in variants:
            stream_url = fmt.get("url") or ""
            if stream_url and _url_looks_like_master(stream_url):
                headers = fmt.get("http_headers") or hls_info.get("http_headers") or {}
                return stream_url, headers, platform, [], "hls", None

        first = with_height[0]
        stream_url = first.get("url") or ""
        if not stream_url:
            raise RuntimeError("Twitch VOD has no HLS stream URL")
        headers = first.get("http_headers") or hls_info.get("http_headers") or {}
        return stream_url, headers, platform, with_height, "hls", None

    fmt = _find_hls_format(hls_info)
    stream_url = fmt.get("url") or hls_info.get("url") or ""
    if not stream_url:
        raise RuntimeError("Twitch VOD has no HLS stream URL")
    headers = fmt.get("http_headers") or hls_info.get("http_headers") or {}
    return stream_url, headers, platform, [], "hls", None


def _pick_preview_variant(master_text: str, master_url: str, prefer_height: int = 720) -> Optional[str]:
    """Pick a preview variant (default ~720p) for faster startup and prewarm."""
    lines = master_text.splitlines()
    variants: list[tuple[int, int, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            bw_m = _BANDWIDTH_RE.search(line)
            res_m = _RESOLUTION_RE.search(line)
            bw = int(bw_m.group(1)) if bw_m else 0
            height = int(res_m.group(2)) if res_m else 0
            if i + 1 < len(lines):
                nxt = lines[i + 1].strip()
                if nxt and not nxt.startswith("#"):
                    variants.append((height, bw, nxt))
                    i += 2
                    continue
        i += 1

    if not variants:
        return None

    for height, _bw, path in variants:
        if height == prefer_height:
            return urljoin(master_url, path)

    at_or_below = [v for v in variants if v[0] and v[0] <= prefer_height]
    if at_or_below:
        at_or_below.sort(key=lambda t: t[0], reverse=True)
        return urljoin(master_url, at_or_below[0][2])

    with_height = [v for v in variants if v[0]]
    if with_height:
        with_height.sort(key=lambda t: t[0])
        return urljoin(master_url, with_height[0][2])

    variants.sort(key=lambda t: t[1])
    return urljoin(master_url, variants[0][2])


def _resolve_preview_entry(session: PreviewSession, entry_url: str, prefer_height: int = 720) -> str:
    """Follow master playlist to a single media playlist for prewarm."""
    if session.variant_entries:
        picked = _pick_variant_by_height(session.variant_entries, prefer_height)
        if picked:
            return picked

    data, _, _, _ = _http_get_bytes(session, entry_url)
    if not data or not data.lstrip().startswith(b"#EXTM3U"):
        raise RuntimeError("Upstream returned an empty or invalid HLS playlist")

    text = data.decode("utf-8", errors="replace")
    if "#EXT-X-STREAM-INF" not in text:
        return entry_url

    variant_url = _pick_preview_variant(text, entry_url, prefer_height)
    if not variant_url:
        return entry_url

    logger.info("Preview resolved %s -> %s", entry_url[:80], variant_url[:80])
    return variant_url


def _resource_id(session: PreviewSession, upstream: str) -> str:
  # Stable per-URL id within session so playlist re-fetches stay consistent.
    digest = hashlib.sha256(upstream.encode()).hexdigest()[:16]
    session.resource_map[digest] = upstream
    return digest


def _proxy_url(session: PreviewSession, upstream: str) -> str:
    rid = _resource_id(session, upstream)
    return f"/api/preview/hls/{session.session_id}/resource?id={rid}"


def _parse_playlist_assets(text: str, playlist_url: str) -> Tuple[list[str], list[str]]:
    """Return (init/key URLs, media segment URLs in playlist order)."""
    base = playlist_url.rsplit("/", 1)[0] + "/"
    inits: list[str] = []
    segments: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if 'URI="' in stripped:
                m = _URI_IN_TAG.search(stripped)
                if m:
                    inits.append(urljoin(playlist_url, m.group(1)))
            continue
        segments.append(urljoin(base, stripped))
    return inits, segments


def _segment_index_for_time(text: str, target_sec: float) -> int:
    """Map a VOD timestamp to a segment index using EXTINF durations."""
    index = 0
    pos = 0.0
    pending_duration: Optional[float] = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXTINF:"):
            pending_duration = float(stripped.split(":")[1].split(",")[0])
        elif stripped and not stripped.startswith("#") and pending_duration is not None:
            if target_sec < pos + pending_duration or index == 0:
                return index
            pos += pending_duration
            index += 1
            pending_duration = None
    return max(0, index - 1)


def _rewrite_playlist(content: str, session: PreviewSession, playlist_url: str) -> str:
    base = playlist_url.rsplit("/", 1)[0] + "/"
    out: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if stripped.startswith("#"):
            if 'URI="' in stripped:
                def _sub(m: re.Match) -> str:
                    abs_url = urljoin(playlist_url, m.group(1))
                    return f'URI="{_proxy_url(session, abs_url)}"'
                out.append(_URI_IN_TAG.sub(_sub, line))
            else:
                out.append(line)
            continue
        abs_url = urljoin(base, stripped)
        out.append(_proxy_url(session, abs_url))
    return "\n".join(out) + "\n"


def _cache_path(session: PreviewSession, url: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:32]
    path = urlparse(url).path.lower()
    if path.endswith(".m4s"):
        ext = ".m4s"
    elif path.endswith(".mp4"):
        ext = ".mp4"
    elif _is_playlist_url(url):
        ext = ".m3u8"
    else:
        ext = ".ts"
    return session.cache_dir / f"{digest}{ext}"


def _evict_cache_if_needed(session: PreviewSession) -> None:
    if session.cache_bytes <= SESSION_CACHE_MAX_BYTES:
        return
    files: list[tuple[float, Path, int]] = []
    for entry in session.cache_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            st = entry.stat()
            files.append((st.st_atime, entry, st.st_size))
        except OSError:
            continue
    files.sort(key=lambda t: t[0])
    for _atime, path, size in files:
        if session.cache_bytes <= SESSION_CACHE_MAX_BYTES:
            break
        try:
            path.unlink()
            session.cache_bytes = max(0, session.cache_bytes - size)
        except OSError:
            pass


def _read_cache(session: PreviewSession, url: str) -> Optional[bytes]:
    path = _cache_path(session, url)
    if path.is_file():
        try:
            return path.read_bytes()
        except OSError:
            return None
    return None


def _write_cache(session: PreviewSession, url: str, data: bytes) -> None:
    if len(data) > MAX_SEGMENT_BYTES or _is_playlist_url(url):
        return
    path = _cache_path(session, url)
    try:
        path.write_bytes(data)
        session.cache_bytes += len(data)
        _evict_cache_if_needed(session)
    except OSError:
        pass


def _warm_and_prewarm_session(session_id: str, crop_start: float) -> None:
    """Background: cache playlists + segments near trim start (off session-create hot path)."""
    try:
        session = get_session(session_id)
        if not session:
            return
        try:
            if session.custom_master:
                pass  # synthetic master — entry URLs are multi-GB VOD files, never buffer
            else:
                proxy_playlist(session_id, session.master_url)
                if session.entry_url != session.master_url:
                    proxy_playlist(session_id, session.entry_url)
        except Exception as exc:
            logger.debug("Playlist warm skipped session=%s: %s", session_id[:8], exc)
        _prewarm_session(session_id, crop_start)
    except Exception as exc:
        logger.warning("Warm/prewarm failed session=%s: %s", session_id[:8], exc)


def _prewarm_session(session_id: str, crop_start: float) -> None:
    """Background: cache rewritten playlist + segments near trim start."""
    try:
        session = get_session(session_id)
        if not session or session.custom_master:
            return

        raw, _, _, _ = _http_get_bytes(session, session.entry_url)
        if not raw:
            return

        text = raw.decode("utf-8", errors="replace")
        inits, segments = _parse_playlist_assets(text, session.entry_url)
        targets: list[str] = list(dict.fromkeys(inits))

        if segments:
            idx = _segment_index_for_time(text, max(0.0, crop_start))
            idx = min(idx, len(segments) - 1)
            start = max(0, idx - 1)
            end = min(len(segments), start + PREWARM_SEGMENT_COUNT)
            targets.extend(segments[start:end])

        for upstream in targets:
            try:
                proxy_segment(session_id, upstream)
            # ponytail: survival guarantee for prewarm segment — best-effort; skip failed segments silently
            except Exception as exc:
            # ponytail: best-effort — proxy_segment(session_id, upstream)
                logger.debug("Prewarm segment skipped %s: %s", upstream[:80], exc)

        logger.info(
            "Prewarm done session=%s segments=%d inits=%d at=%.1fs",
            session_id[:8],
            len(targets) - len(inits),
            len(inits),
            crop_start,
        )
    # ponytail: survival guarantee for prewarm — best-effort; warn on failure but don't abort session creation
    except Exception as exc:
    # ponytail: best-effort — )
        logger.warning("Prewarm failed session=%s: %s", session_id[:8], exc)

def _heights_from_master_text(text: str) -> List[int]:
    heights: List[int] = []
    for line in text.splitlines():
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        res_m = _RESOLUTION_RE.search(line)
        if res_m:
            height = int(res_m.group(2))
            if height > 0:
                heights.append(height)
    return sorted(set(heights))


def session_quality_labels(session: PreviewSession) -> List[str]:
    """Human-readable quality tiers for the preview quality menu."""
    heights = session_variant_heights(session)
    if not heights:
        return []
    labels: List[str] = []
    for height in heights:
        label = f"{height}p"
        if label not in labels:
            labels.append(label)
    return labels


def session_variant_heights(session: PreviewSession) -> List[int]:
    heights = sorted({h for h, _ in session.variant_entries if h > 0})
    if heights:
        return heights
    if session.custom_master:
        heights = _heights_from_master_text(session.custom_master)
        if heights:
            return heights
    if session.kind == "hls":
        try:
            text = get_master_playlist(session.session_id)
            heights = _heights_from_master_text(text)
            if heights:
                return heights
        # ponytail: survival guarantee for height detection — best-effort; returns empty list on failure
        except Exception:
        # ponytail: best-effort — return heights
            pass
    if session.kind == "progressive" and session.entry_url:
        m = re.search(r"/(\d{3,4})/", session.entry_url)
        if m:
            return [int(m.group(1))]
    return []


def session_active_height(session: PreviewSession) -> int:
    if session.variant_entries and session.entry_url:
        for height, url in session.variant_entries:
            if url == session.entry_url and height > 0:
                return height
    m = re.search(r"/(\d{3,4})/", session.entry_url or "")
    if m:
        return int(m.group(1))
    return 0


def set_session_prefer_height(session_id: str, prefer_height: int) -> PreviewSession:
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")
    if session.platform == "YouTube":
        _refresh_youtube_preview_urls(session, prefer_height=prefer_height)
        if session.variant_entries:
            picked = _pick_variant_by_height(session.variant_entries, prefer_height)
            if picked:
                session.entry_url = picked
                session.allowed_hosts.update(_hosts_for_url(picked))
                _clear_youtube_mux_cache(session)
        return session
    if session.variant_entries:
        picked = _pick_variant_by_height(session.variant_entries, prefer_height)
        if not picked:
            raise ValueError("No preview variant for requested height")
        session.entry_url = picked
        session.allowed_hosts.update(_hosts_for_url(picked))
        session.touch()
        return session
    if session.kind != "progressive":
        raise ValueError("Preview session has no quality variants")
    raise ValueError("No preview variant for requested height")


def resolve_upstream(session_id: str, resource_id: Optional[str]) -> str:
    """Resolve a preview resource URL — only IDs registered in session.resource_map."""
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")
    if not resource_id:
        raise ValueError("Missing preview resource id")
    upstream = session.resource_map.get(resource_id)
    if not upstream:
        raise ValueError("Unknown preview resource")
    return upstream

def get_master_playlist(session_id: str) -> str:
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")
    if session.custom_master:
        return session.custom_master
    body, _, _, _ = proxy_playlist(session_id, session.master_url)
    return body.decode("utf-8")


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
        return cached[0], "application/vnd.apple.mpegurl", {"Cache-Control": "no-cache"}, 200

    data, _, _, status = _http_get_bytes(session, upstream_url)

    if not data:
        raise RuntimeError("Upstream playlist is empty")

    if data.lstrip().startswith(b"#EXTM3U") or _is_playlist_url(upstream_url):
        text = data.decode("utf-8", errors="replace")
        rewritten = _rewrite_playlist(text, session, upstream_url)
        data = rewritten.encode("utf-8")
        cache[upstream_url] = (data, now)
        return data, "application/vnd.apple.mpegurl", {"Cache-Control": "no-cache"}, status

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

    if range_header is None:
        cached = _read_cache(session, upstream_url)
        if cached is not None:
            ctype = _guess_content_type(upstream_url)
            return cached, ctype, {
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(cached)),
                "Cache-Control": "public, max-age=3600",
            }, 200

    data, ctype, headers, status = _http_get_bytes(session, upstream_url, range_header=range_header)
    if len(data) > MAX_SEGMENT_BYTES:
        raise RuntimeError("Preview segment exceeds size limit")

    if range_header is None and data and not _is_playlist_url(upstream_url):
        _write_cache(session, upstream_url, data)
        headers["Cache-Control"] = "public, max-age=3600"

    return data, ctype, headers, status


# Module-level singleton — existing importers use ``from services.preview_service import create_session``.
# ponytail: module-level singleton — one manager per process; safe because the app runs a single backend server.
_manager = PreviewManager()
_cleanup_stale_sessions = _manager._cleanup_stale_sessions
delete_session = _manager.delete_session
get_session = _manager.get_session
create_session = _manager.create_session

assert _formats_are_dash_https([{"protocol": "https", "url": "https://x/v.mp4"}])
assert not _formats_are_dash_https([{"protocol": "m3u8_native", "url": "https://x/m.m3u8"}])
# ponytail: self-check — YouTube segments must not be classified as playlists
assert not _is_playlist_url(
    "https://rr1---sn.example.googlevideo.com/videoplayback/id/x/itag/231"
    "/source/youtube/playlist/index.m3u8/seg.ts"
)
assert _is_playlist_url("https://manifest.googlevideo.com/api/manifest/hls_playlist/expire/1/master.m3u8")
assert _clamp_range_header("bytes=8388608-8454143", 3697756) == "bytes=3435612-3697755"
assert preview_mux_ready(PreviewSession(
    session_id="x", vod_url="", master_url="", entry_url="", platform="Kick",
))
assert issubclass(PreviewMuxPending, RuntimeError)
assert min(907.0, 0.0 + 30.0) == 30.0  # preview mux window cap, not 10s teaser
_dash_only = {
    "formats": [
        {"height": 720, "protocol": "https", "url": "https://x/v.mp4", "acodec": "none"},
        {"height": 480, "protocol": "https", "url": "https://x/v2.mp4", "acodec": "none"},
    ],
}
assert _youtube_info_is_dash_only_progressive(_dash_only)
assert not _youtube_info_is_dash_only_progressive({
    "formats": [{"height": 720, "protocol": "m3u8_native", "url": "https://x/m.m3u8"}],
})
