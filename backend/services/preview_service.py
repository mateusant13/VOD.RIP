"""Session-scoped HLS proxy for in-browser VOD trim preview (no ffmpeg)."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
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
# ponytail: a DELETE marks the session closed but keeps the cache_dir on disk
# for this long so late byte requests from the browser don't 404. After the
# grace window the cache_dir is wiped.
_SESSION_DELETE_GRACE_SEC = 3.0
PLAYLIST_REWRITE_TTL_SEC = 20 * 60
_MAX_PLAYLIST_FETCH_BYTES = (
    512 * 1024
)  # ponytail: non-streaming fetches only (keys/init)
_MAX_REWRITTEN_PLAYLIST_BYTES = 32 * 1024 * 1024  # long YouTube VOD media playlists
PREWARM_SEGMENT_COUNT = 5
# ponytail: window HLS mux — chunk at a time; full crop_end stays for trim UI/download
WINDOW_HLS_SEGMENT_SEC = (
    4.0  # HLS segment target duration — short for fast first segment·
)
# Hard ceiling on ffmpeg mux wall-clock time. Without this, a stalled googlevideo
# CDN connection (no EOF) blocks the background mux thread forever and the preview
# spinner never resolves.
WINDOW_HLS_MUX_TIMEOUT_SEC = 300.0  # 5 min — generous for long remux
# ponytail: smaller seek remux window = faster out-of-chunk seeks. 20 s is enough
# for a preview scrub and remuxes far quicker than the old 90/180 s windows.
WINDOW_HLS_MUX_CHUNK_SEC = 20.0  # seek remux byte-window cap (VODs under 30 min)
WINDOW_HLS_MUX_CHUNK_LONG_SEC = 40.0  # seek remux for long VODs — fewer remuxes
WINDOW_HLS_LONG_VOD_MIN_SEC = 1800.0  # 30 min — use long seek window above this
WINDOW_HLS_INITIAL_CHUNK_SEC = 2.0  # first-play chunk — smaller = faster seg0
WINDOW_HLS_SHORT_VOD_MAX_SEC = 60.0  # mux entire crop on first play when trim ≤ this
WINDOW_HLS_CACHE_MB = 500  # total disk budget for window-HLS segment cache
WINDOW_HLS_MARKER = (
    "youtube-window-hls:"  # resource_map marker for window HLS resources
)
# ponytail: alias exposed for new code paths while old imports keep working
YOUTUBE_WINDOW_HLS_MARKER = WINDOW_HLS_MARKER
# resource IDs registered in session.resource_map for window HLS access
WINDOW_HLS_PLAYLIST_RESOURCE = "window-playlist"
WINDOW_HLS_SEGMENT_RESOURCE_PREFIX = "window-seg-"
WINDOW_HLS_INIT_RESOURCE = "window-init"  # fMP4 init segment (init.mp4)
# ponytail: fMP4 / LL-HLS opt-in — gate the EXT-X-MAP + EXT-X-PART metadata behind a
# flag so legacy .ts playlists keep working if an operator sets VODRIP_PREVIEW_FMP4=0.
USE_FMP4 = os.getenv("VODRIP_PREVIEW_FMP4", "1") == "1"
# Signed googlevideo URLs expire in hours, not minutes; a 300s TTL let the warm
# storm's work evaporate before a user browsed the channel list and clicked.
# Stale 403s are already handled: the proxy re-resolves on auth errors.
_RESOLVED_STREAM_TTL_SEC = 900
_RESOLVED_STREAM_MAX = 256
_RESOLVED_STREAM_CACHE: Dict[str, Tuple[float, Tuple]] = {}
_RESOLVED_STREAM_LOCK = threading.Lock()
# Prebuilt session snapshot keyed by (vid, height) — produced by the YouTube
# warm job and consumed by create_session so the click path skips the ~5s
# extract + variant-build + custom-master work entirely on a warm hit.
# TTL matches _RESOLVED_STREAM_TTL_SEC: signed googlevideo URLs expire in
# hours, so a warm window of 15min is plenty to cover a browsing session.
_SESSION_SNAPSHOT_TTL_SEC = _RESOLVED_STREAM_TTL_SEC
_SESSION_SNAPSHOT_MAX = 256
_SESSION_SNAPSHOT: Dict[Tuple[str, int], Tuple[float, dict]] = {}
_SESSION_SNAPSHOT_LOCK = threading.Lock()
# Persistent dedup for the batch warm startup path — once a URL has been
# warmed once this process lifetime, skip re-warming it. The 60-second
# _RESOLVED_STREAM_CACHE TTL would otherwise cause redundant re-extracts
# when the frontend re-fires the batch warm on every state change.
_WARMED_URLS: set = set()
_WARMED_URLS_LOCK = threading.Lock()
# In-flight YouTube warm keyed by canonical URL — create_session awaits paste warm.
_YOUTUBE_WARM_INFLIGHT: Dict[str, threading.Event] = {}
_YOUTUBE_WARM_LOCK = threading.Lock()
# ponytail: latest URL the user is actively previewing. Warm jobs check this
# before doing heavy work and bail out cheaply if they're no longer relevant.
# Without this, parallel warm jobs from the channel list steal INFO_EXECUTOR
# workers away from the preview the user actually clicked.
# Module-level + lock so it works across asyncio threads and executor threads.
_ACTIVE_YOUTUBE_PREVIEW_KEY: Optional[str] = None
_ACTIVE_YOUTUBE_PREVIEW_LOCK = threading.Lock()
# Preflight window-HLS mux on URL paste — adopted by create_session when ready.
_PREFLIGHT_MUX_INFLIGHT: Dict[str, threading.Event] = {}
_PREFLIGHT_MUX_LOCK = threading.Lock()
MAX_SEGMENT_BYTES = 100 * 1024 * 1024
SESSION_CACHE_MAX_BYTES = 100 * 1024 * 1024
_UPSTREAM_CHUNK_BYTES = 64 * 1024
_UPSTREAM_CONNECT_TIMEOUT_SEC = 15
_PREVIEW_ROOT = (
    Path(os.environ.get("TEMP", os.environ.get("TMP", "/tmp"))) / "kd_preview"
)
# ponytail: full-VOD mux cache persisted by video_id+quality
_FULL_MUX_CACHE_DIR = _PREVIEW_ROOT / "full_mux_cache"
FULL_MUX_CACHE_TTL_SEC = 86400 * 7  # 7 days

# Progressive head cache — first bytes of a muxed MP4 tier (ftyp + moov +
# opening media). Multi-hour VOD moovs run 8+ MB; serving them from disk cuts
# ~1-2s off the browser's canplay wait after a warm.
_PROG_HEAD_DIR = _PREVIEW_ROOT / "prog_head"
_PROG_HEAD_BYTES = 12 * 1024 * 1024
_PROG_HEAD_MAX_BYTES = 256 * 1024 * 1024
_PROG_HEAD_TTL_SEC = 86400


def _prog_head_paths(video_id: str, height: int) -> Tuple[Path, Path]:
    base = _PROG_HEAD_DIR / f"{video_id}_{height}"
    return base.with_suffix(".bin"), base.with_suffix(".json")


def _prog_head_lookup(video_id: str, height: int) -> Optional[Tuple[Path, int]]:
    bin_path, meta_path = _prog_head_paths(video_id, height)
    try:
        if not bin_path.is_file() or not meta_path.is_file():
            return None
        if time.time() - bin_path.stat().st_mtime > _PROG_HEAD_TTL_SEC:
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        total = int(meta.get("total") or 0)
        if total <= 0 or bin_path.stat().st_size < 1024 * 1024:
            return None
        return bin_path, total
    except Exception:
        return None


def _prog_head_enforce_budget() -> None:
    try:
        files = [
            p
            for p in _PROG_HEAD_DIR.glob("*.bin")
            if p.is_file()
        ]
        total = sum(p.stat().st_size for p in files)
        if total <= _PROG_HEAD_MAX_BYTES:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files:
            if total <= _PROG_HEAD_MAX_BYTES:
                break
            size = p.stat().st_size
            p.unlink(missing_ok=True)
            p.with_suffix(".json").unlink(missing_ok=True)
            total -= size
    except Exception:
        pass


def kickoff_youtube_prog_head_warm(
    url: str,
    oauth: Optional[str] = None,
    prefer_height: int = 360,
) -> None:
    """Background: download the head (ftyp+moov+opening media) of the muxed
    progressive tier so the browser's canplay path reads from disk."""
    from services.youtube_innertube import extract_video_id

    vid = extract_video_id((url or "").strip())
    if not vid:
        return
    if _prog_head_lookup(vid, prefer_height):
        return
    key = f"proghead:{vid}:{prefer_height}"
    with _YOUTUBE_WARM_LOCK:
        if key in _YOUTUBE_WARM_INFLIGHT:
            return
        done = threading.Event()
        _YOUTUBE_WARM_INFLIGHT[key] = done

    def _run() -> None:
        try:
            _youtube_prog_head_warm(url, vid, oauth=oauth, prefer_height=prefer_height)
        finally:
            with _YOUTUBE_WARM_LOCK:
                ev = _YOUTUBE_WARM_INFLIGHT.pop(key, None)
            if ev is not None:
                ev.set()

    from deps import GESTURE_WARM_EXECUTOR

    GESTURE_WARM_EXECUTOR.submit(_run)


def _youtube_prog_head_warm(
    url: str, vid: str, oauth: Optional[str], prefer_height: int
) -> bool:
    try:
        _raw, headers, _platform, variant_formats, kind, yt_info = resolve_stream_info(
            url, oauth=oauth, prefer_height=prefer_height
        )
    except Exception as exc:
        logger.debug("prog head warm resolve failed %s: %s", vid, exc)
        return False
    from services.youtube_innertube import _dedupe_youtube_formats

    merged = _dedupe_youtube_formats((yt_info or {}).get("formats") or [])
    muxed = _deduped_progressive_variants({"formats": merged})
    if not muxed:
        logger.info(
            "prog head warm: %s no muxed tier (info=%s merged=%d)",
            vid, "yes" if yt_info else "no", len(merged),
        )
        return False
    prog_url = _pick_variant_by_height(
        [(int(f.get("height") or 0), f.get("url") or "") for f in muxed],
        prefer_height,
    )
    if not prog_url:
        logger.info("prog head warm: %s no prog url at h=%d", vid, prefer_height)
        return False
    mux_hdrs = _merge_youtube_session_cookies(headers or {}, url)
    bin_path, meta_path = _prog_head_paths(vid, prefer_height)
    tmp = bin_path.with_suffix(".part")
    try:
        from curl_cffi import requests as cffi_requests

        _PROG_HEAD_DIR.mkdir(parents=True, exist_ok=True)
        resp = cffi_requests.get(
            prog_url,
            headers={**mux_hdrs, "Range": f"bytes=0-{_PROG_HEAD_BYTES - 1}"},
            impersonate="chrome",
            stream=True,
            timeout=(10, 30),
        )
        try:
            if resp.status_code not in (200, 206):
                logger.info("prog head warm: %s upstream HTTP %s", vid, resp.status_code)
                return False
            total = _total_from_content_range(resp.headers.get("Content-Range", "")) or 0
            written = 0
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=262144):
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
                    if written >= _PROG_HEAD_BYTES:
                        break
        finally:
            try:
                resp.close()
            except OSError:
                pass
        if written < 1024 * 1024 or not total:
            tmp.unlink(missing_ok=True)
            return False
        os.replace(tmp, bin_path)
        meta_path.write_text(json.dumps({"total": total, "ts": time.time()}), encoding="utf-8")
        _prog_head_enforce_budget()
        logger.info("prog head warm: %s h=%d bytes=%d", vid, prefer_height, written)
        return True
    except Exception as exc:
        logger.info("prog head warm failed %s: %s", vid, exc)
        tmp.unlink(missing_ok=True)
        return False


def _prog_head_range(
    range_header: Optional[str], head_len: int, total: int
) -> Optional[Tuple[int, int]]:
    """Resolve (start, end) for a head-spliced response, or None when the
    request lies entirely beyond the cached head (plain upstream path)."""
    start = 0
    end = total - 1
    if range_header:
        m = re.match(r"bytes=(\d+)-(\d*)", range_header.strip())
        if not m:
            return None
        start = int(m.group(1))
        if m.group(2):
            end = min(end, int(m.group(2)))
    if start >= head_len or end < start:
        return None
    return start, end


# ponytail self-check: head-splice range boundaries
assert _prog_head_range(None, 100, 1000) == (0, 999)
assert _prog_head_range("bytes=0-", 100, 1000) == (0, 999)
assert _prog_head_range("bytes=50-149", 100, 1000) == (50, 149)
assert _prog_head_range("bytes=50-5000", 100, 1000) == (50, 999)  # clamp to EOF
assert _prog_head_range("bytes=100-", 100, 1000) is None  # beyond head → upstream
assert _prog_head_range("bytes=500-100", 100, 1000) is None  # empty range
assert _prog_head_range("nonsense", 100, 1000) is None


def _try_prog_head_proxy(
    session: PreviewSession,
    range_header: Optional[str],
) -> Optional[Tuple[Callable[[], object], str, dict, int, Callable[[], None]]]:
    """Serve the request's cached-head portion from disk, splicing the upstream
    for anything beyond. Returns None when no usable head cache exists."""
    from services.youtube_innertube import extract_video_id

    vid = extract_video_id(session.vod_url or "")
    if not vid:
        return None
    height = session_active_height(session) or int(session.prefer_height or 360)
    found = _prog_head_lookup(vid, height)
    if not found:
        return None
    bin_path, total = found
    head_len = bin_path.stat().st_size
    bounds = _prog_head_range(range_header, head_len, total)
    if bounds is None:
        return None  # beyond the cached head — plain upstream path
    start, end = bounds

    def _generate():
        head_end = min(end, head_len - 1)
        remaining = head_end - start + 1
        with open(bin_path, "rb") as fh:
            fh.seek(start)
            while remaining > 0:
                chunk = fh.read(min(262144, remaining))
                if not chunk:
                    break
                yield chunk
                remaining -= len(chunk)
        if end >= head_len:
            resp = _open_upstream_stream(
                session,
                session.entry_url,
                f"bytes={head_len}-{'' if end == total - 1 else end}",
            )
            try:
                for chunk in resp.iter_content(chunk_size=_UPSTREAM_CHUNK_BYTES):
                    if chunk:
                        yield chunk
            finally:
                try:
                    resp.close()
                except OSError:
                    pass

    status = 206 if (range_header or start or end < total - 1) else 200
    hdrs = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache",
        "Content-Range": f"bytes {start}-{end}/{total}",
        "Content-Length": str(end - start + 1),
    }
    if not range_header and start == 0 and end == total - 1:
        status = 200
        hdrs.pop("Content-Range", None)
    return _generate, "video/mp4", hdrs, status, lambda: None


assert _prog_head_lookup("__none__", 360) is None

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_DEFAULT_TWITCH_HEADERS: dict = {
    "Referer": "https://www.twitch.tv/",
    "Origin": "https://www.twitch.tv",
    "User-Agent": _DEFAULT_UA,
}

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

    def _cleanup_stale_sessions(
        self,
    ) -> None:
        now = time.time()
        # ponytail: closed sessions get a grace window before cache wipe; open
        # sessions follow the normal SESSION_TTL_SEC.
        to_wipe: List[str] = []
        stale_open: List[str] = []
        for sid, s in self._sessions.items():
            if s.closed:
                if now - s.closed_at >= _SESSION_DELETE_GRACE_SEC:
                    to_wipe.append(sid)
            elif now - s.last_access > SESSION_TTL_SEC:
                stale_open.append(sid)
        for sid in to_wipe:
            self._finalize_delete(sid)
        for sid in stale_open:
            self._finalize_delete(sid)

    def _finalize_delete(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        _PREVIEW_MUX_LOCKS.pop(session_id, None)
        if not session:
            return
        from services.os_services import kill_child_processes

        kill_child_processes()
        try:
            import shutil

            if session.cache_dir.is_dir():
                shutil.rmtree(session.cache_dir, ignore_errors=True)
        except OSError:
            pass

    def delete_session(self, session_id: str) -> bool:
        # ponytail: mark closed + schedule the actual wipe after the grace
        # window so in-flight byte requests from the browser still hit the
        # proxy and don't 404.
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return False
            session.closed = True
            session.closed_at = time.time()
        _PREVIEW_MUX_LOCKS.pop(session_id, None)
        from services.os_services import kill_child_processes

        timer = threading.Timer(
            _SESSION_DELETE_GRACE_SEC,
            self._finalize_delete,
            args=(session_id,),
        )
        timer.daemon = True
        timer.start()
        return True

    def get_session(self, session_id: str) -> Optional[PreviewSession]:
        with self._lock:
            session = self._sessions.get(session_id)
        if session:
            # ponytail: skip touch() on closed sessions — they're in the grace
            # window after DELETE and we don't want a late GET extending their
            # life beyond the scheduled wipe.
            if not session.closed:
                session.touch()
        return session

    def _reuse_youtube_snapshot(
        self,
        url: str,
        crop_start: float,
        crop_end: float,
        prefer_height: int,
        snapshot: dict,
    ) -> PreviewSession:
        """ponytail: rebuild a PreviewSession from the warm's prebuilt snapshot.

        All post-resolve work (variant build, custom-master, audio URL, host
        set, window-HLS bounds, format stash, duration clamp) is done once
        during the warm. The click path just creates the live session object
        and registers it — no extract, no network, no variant-build.

        The session_id and cache_dir come from the snapshot so the window-HLS
        master (which bakes session_id into the resource URL) matches the
        live session. The cache_dir was already created by the warm.
        """
        from services.youtube_innertube import extract_video_id

        session_id = snapshot["session_id"]
        cache_dir = Path(snapshot["cache_dir"])
        cache_dir.mkdir(parents=True, exist_ok=True)

        session = PreviewSession(
            session_id=session_id,
            vod_url=url,
            master_url=snapshot["master_url"],
            entry_url=snapshot["entry_url"],
            platform=snapshot["platform"],
            http_headers=dict(snapshot.get("http_headers") or {}),
            allowed_hosts=set(snapshot.get("allowed_hosts") or set()),
            cache_dir=cache_dir,
            kind=snapshot["kind"],
            crop_start=crop_start,
            crop_end=crop_end,
            preview_audio_url=snapshot.get("preview_audio_url"),
            variant_muxed=dict(snapshot.get("variant_muxed") or {}),
            variant_entries=list(snapshot.get("variant_entries") or []),
            custom_master=snapshot.get("custom_master"),
            dash_window_hls=bool(snapshot.get("dash_window_hls")),
            preview_audio_fmt=snapshot.get("preview_audio_fmt"),
            preview_video_fmt=snapshot.get("preview_video_fmt"),
            vod_duration=float(snapshot.get("vod_duration") or 0.0),
            cached_progressive_path=snapshot.get("cached_progressive_path"),
            mux_status=snapshot.get("mux_status") or "pending",
            prefer_height=prefer_height,
        )
        # explore_yt_info is a dynamic attribute set by _stash_youtube_preview_formats
        # on the live session; restore it here so post-register code can use it.
        explore_info = snapshot.get("explore_yt_info")
        if explore_info is not None:
            session.explore_yt_info = explore_info
        # Re-clamp crop_end to the VOD's actual duration (snapshot stored the
        # last-known value but the click may have a different crop window).
        if getattr(session, "explore_yt_info", None):
            _clamp_session_crop_to_vod_duration(session, session.explore_yt_info)
        with self._lock:
            self._sessions[session_id] = session
            session.timing_created_mono = time.monotonic()
        return session

    def create_session(
        self,
        url: str,
        crop_start: float = 0.0,
        crop_end: float = 0.0,
        oauth: Optional[str] = None,
        prefer_height: int = 720,
    ) -> PreviewSession:
        self._cleanup_stale_sessions()
        if detect_platform(url) == "YouTube":
            # Wait briefly for a genuinely in-flight hover/paste warm so
            # create_session reuses the resolved-stream cache (avoids a second
            # ~3s extract). ponytail: capped low — during a startup/channel warm
            # storm the warm for this URL may be *queued* (not running); waiting
            # 8s for it burns the whole preview SLA. A running warm is still
            # joined via the extract dedup inside cached_extract_info.
            await_youtube_warm_if_pending(url, timeout_sec=1.5)
            # ponytail: the warm prebuilt a full session shell — reuse it to
            # skip the ~5s extract + variant-build + custom-master work.
            from services.youtube_innertube import extract_video_id

            vid = extract_video_id(url or "")
            snap = _get_session_snapshot(vid, prefer_height) if vid else None
            if snap:
                session = self._reuse_youtube_snapshot(
                    url, crop_start, crop_end, prefer_height, snap
                )
                logger.info(
                    "preview session reused from snapshot sid=%s vid=%s h=%d",
                    session.session_id[:8], vid[:11], prefer_height,
                )
                return _finalize_youtube_session(session, crop_start)
        raw_entry, headers, platform, variant_formats, kind, yt_info = (
            resolve_stream_info(
                url,
                oauth=oauth,
                prefer_height=prefer_height,
            )
        )
        preview_audio_url: Optional[str] = None
        variant_muxed: Dict[int, bool] = {}
        if platform == "YouTube" and yt_info:
            preview_audio_url = _resolve_youtube_preview_audio(yt_info)
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
            prefer_height=prefer_height,
        )
        _clamp_session_crop_to_vod_duration(session, yt_info)
        if preview_audio_url:
            session.allowed_hosts.update(_hosts_for_url(preview_audio_url))

        if variant_formats:
            session.variant_entries = [
                (int(fmt.get("height") or 0), fmt.get("url") or "")
                for fmt in variant_formats
                if int(fmt.get("height") or 0) > 0 and fmt.get("url")
            ]
            if kind == "hls":
                from services.ytdlp_hls import preview_fast_only_mode

                # YouTube-specific DASH/window-HLS master logic must not be
                # applied to Twitch/Kick HLS — it produces an invalid custom
                # master and the player never reaches canplay.
                if platform == "YouTube":
                    _apply_youtube_custom_master(session, variant_formats, yt_info)
                elif len(session.variant_entries) >= 2:
                    session.custom_master = _build_synthetic_master_playlist(
                        session, variant_formats
                    )
                for _height, upstream in session.variant_entries:
                    session.allowed_hosts.update(_hosts_for_url(upstream))
                if session.dash_window_hls and not preview_fast_only_mode():
                    muxed = _youtube_muxed_progressive_for_long_explore(
                        session.vod_url,
                        oauth,
                        prefer_height,
                        yt_info=yt_info,
                    )
                    if muxed:
                        prog_url, prog_formats, prog_info = muxed
                        _apply_muxed_progressive_session(
                            session,
                            prog_url,
                            prog_formats,
                            prog_info,
                            prefer_height,
                        )
                        kind = "progressive"
                        variant_formats = prog_formats
                        yt_info = prog_info
                        proxy_master_url = session.master_url
        if kind == "progressive":
            session.allowed_hosts.update(_hosts_for_url(session.entry_url))
        elif session.custom_master:
            if session.variant_entries:
                session.entry_url = (
                    _pick_variant_by_height(
                        session.variant_entries,
                        prefer_height,
                    )
                    or session.variant_entries[0][1]
                )
            session.allowed_hosts.update(_hosts_for_url(session.entry_url))
        else:
            session.entry_url = _resolve_preview_entry(
                session, raw_entry, prefer_height
            )
            session.allowed_hosts.update(_hosts_for_url(session.entry_url))

        if platform == "YouTube" and variant_formats:
            _stash_youtube_preview_formats(
                session,
                variant_formats,
                yt_info,
                prefer_height,
                session.entry_url,
            )

        with self._lock:
            self._sessions[session_id] = session
            session.timing_created_mono = time.monotonic()
            if len(self._sessions) > self._max_sessions:
                stale = sorted(
                    self._sessions.items(),
                    key=lambda item: item[1].last_access,
                )[: len(self._sessions) - self._max_sessions]
                for popped_sid, popped_session in stale:
                    del self._sessions[popped_sid]
                    cache_dir = popped_session.cache_dir
                    threading.Thread(
                        target=lambda d=cache_dir: shutil.rmtree(
                            d,
                            ignore_errors=True,
                        ),
                        daemon=True,
                    ).start()

        return _finalize_youtube_session(session, crop_start)


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
    # ponytail: when set, the proxy serves this local MP4 directly instead of
    # the upstream stream. Original session state (variant_entries, audio_url,
    # etc.) is preserved for diagnostics so we don't have to mutate multiple
    # fields to flip a session to progressive.
    cached_progressive_path: Optional[str] = None
    variant_entries: List[Tuple[int, str]] = field(default_factory=list)
    variant_muxed: Dict[int, bool] = field(default_factory=dict)
    preview_audio_url: Optional[str] = None
    preview_audio_fmt: Optional[dict] = None
    preview_video_fmt: Optional[dict] = None
    crop_start: float = 0.0
    crop_end: float = 0.0
    vod_duration: float = 0.0  # seconds from extract — clamps crop_end
    cache_bytes: int = 0
    last_access: float = field(default_factory=time.time)
    cache_dir: Path = field(default_factory=Path)
    # ponytail: when True, DELETE has been called but the session is kept in
    # the dict for _SESSION_DELETE_GRACE_SEC so in-flight byte requests from
    # the browser don't 404. Late GETs still hit the proxy, just won't be
    # touched/extended. cleanup_stale_sessions sweeps expired sessions later.
    closed: bool = False
    closed_at: float = 0.0
    mux_status: str = "unnecessary"  # unnecessary | pending | ready | error
    mux_error: Optional[str] = None
    dash_window_hls: bool = False  # YouTube DASH: crop window muxed to local HLS
    window_hls_mux_start: float = 0.0  # active mux byte window (subset of crop)
    window_hls_mux_end: float = 0.0
    prefer_height: int = 0  # last applied preview tier (0 = unset)
    timing_created_mono: float = 0.0
    timing_seg0_mono: float = 0.0
    timing_last_seek_mono: float = 0.0
    timing_last_seek_pos: float = 0.0

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
        if not hasattr(self, "dash_window_hls"):
            object.__setattr__(self, "dash_window_hls", False)
        if not hasattr(self, "prefer_height"):
            object.__setattr__(self, "prefer_height", 0)
        if not hasattr(self, "vod_duration"):
            object.__setattr__(self, "vod_duration", 0.0)
        if not hasattr(self, "window_hls_mux_start"):
            object.__setattr__(self, "window_hls_mux_start", 0.0)
        if not hasattr(self, "window_hls_mux_end"):
            object.__setattr__(self, "window_hls_mux_end", 0.0)
        if not hasattr(self, "preview_audio_fmt"):
            object.__setattr__(self, "preview_audio_fmt", None)
        if not hasattr(self, "preview_video_fmt"):
            object.__setattr__(self, "preview_video_fmt", None)
        if not hasattr(self, "cached_progressive_path"):
            object.__setattr__(self, "cached_progressive_path", None)
        if not hasattr(self, "timing_created_mono"):
            object.__setattr__(self, "timing_created_mono", 0.0)
        if not hasattr(self, "timing_seg0_mono"):
            object.__setattr__(self, "timing_seg0_mono", 0.0)
        if not hasattr(self, "timing_last_seek_mono"):
            object.__setattr__(self, "timing_last_seek_mono", 0.0)
        if not hasattr(self, "timing_last_seek_pos"):
            object.__setattr__(self, "timing_last_seek_pos", 0.0)


def _vod_duration_from_info(info: Optional[dict]) -> float:
    if not info:
        return 0.0
    raw = info.get("duration")
    if raw is None:
        return 0.0
    try:
        dur = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return dur if dur > 0 else 0.0


def _resolve_youtube_preview_audio(yt_info: Optional[dict]) -> Optional[str]:
    """Attach best HTTPS audio stream for DASH segment mux."""
    if not yt_info:
        return None
    from services.ytdlp_hls import _resolve_youtube_audio_format

    audio = _resolve_youtube_audio_format(yt_info)
    if audio and audio.get("url"):
        yt_info["_preview_audio_format"] = audio
        return audio["url"]
    tagged = yt_info.get("_preview_audio_format")
    if tagged and tagged.get("url"):
        return tagged["url"]
    return None


def _boost_youtube_duration_if_underreported(
    session: PreviewSession,
    info: Optional[dict],
    client_end: float,
    dur: float,
) -> float:
    """Fast InnerTube preview extract can return ~20s for full VODs — re-check lengthSeconds."""
    if session.platform != "YouTube" or dur <= 0:
        return dur
    if client_end < 3600:
        return dur
    from services.youtube_innertube import (
        extract_video_id,
        innertube_video_row_metadata,
    )

    vid = extract_video_id(session.vod_url)
    if not vid:
        return dur
    meta = innertube_video_row_metadata(vid, read_timeout=5.0)
    if not meta:
        return dur
    try:
        fallback = float(meta.get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0
    if fallback > dur:
        if info is not None:
            info["duration"] = int(fallback)
        return fallback
    return dur


def _clamp_session_crop_to_vod_duration(
    session: PreviewSession,
    info: Optional[dict],
) -> None:
    """Never let placeholder crop_end (3600/7200) exceed real VOD length."""
    client_end = float(session.crop_end or 0)
    dur = _vod_duration_from_info(info)
    if dur <= 0:
        if 0 < client_end < 3600:
            session.vod_duration = client_end
        return
    # Channel/info often has the true length when fast extract under-reports (e.g. ~19s vs 50s).
    if 0 < client_end < 3600 and client_end > dur:
        session.vod_duration = client_end
        return
    dur = _boost_youtube_duration_if_underreported(session, info, client_end, dur)
    session.vod_duration = dur
    if session.crop_end > dur:
        session.crop_end = dur
    if session.crop_start >= session.crop_end:
        session.crop_start = max(0.0, session.crop_end - 0.5)


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
    return any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in _ALLOWED_HOST_SUFFIXES
    )


def _request_headers(
    session: PreviewSession, range_header: Optional[str] = None
) -> dict:
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
    if header_ct and header_ct not in (
        "application/octet-stream",
        "binary/octet-stream",
    ):
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
    session.rewritten_playlists.clear()
    # ponytail: custom_master playlist lines reference resource_map IDs — never wipe alone.
    if not session.custom_master:
        session.resource_map.clear()


def _remap_youtube_url_after_refresh(
    old_url: str,
    old_entry: str,
    old_variants: List[Tuple[int, str]],
    session: PreviewSession,
    *,
    old_audio_url: str = "",
) -> Optional[str]:
    if old_url == old_entry:
        return session.entry_url
    if old_audio_url and old_url == old_audio_url and session.preview_audio_url:
        return session.preview_audio_url
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
    old_audio = session.preview_audio_url or ""
    if session.dash_window_hls:
        _refresh_youtube_window_hls_urls(session, prefer_height=prefer_height)
    else:
        _refresh_youtube_preview_urls(session, prefer_height=prefer_height)
    return _remap_youtube_url_after_refresh(
        failed_url,
        old_entry,
        old_variants,
        session,
        old_audio_url=old_audio,
    )


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

        # QUIC/HTTP3 for googlevideo CDN (faster segment fetch, 0-RTT resumption)
        http_version = None
        if "googlevideo.com" in url:
            http_version = "v3"  # HTTP/3

        resp = cffi_requests.get(
            url,
            headers=headers,
            impersonate="chrome",
            stream=True,
            timeout=(_UPSTREAM_CONNECT_TIMEOUT_SEC, 3600),
            http_version=http_version,
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
                    session,
                    new_url,
                    range_header,
                    _retried=True,
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
                total = _total_from_content_range(
                    probe.headers.get("Content-Range", "")
                )
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
    if _is_rangeable_cdn_media(url) and "Accept-Ranges" not in out_headers:
        out_headers["Accept-Ranges"] = "bytes"
    status = resp.status_code
    if range_header and status == 200 and out_headers.get("Accept-Ranges"):
        status = 206
    return ctype, out_headers, status


def preview_session_kind(session_id: str) -> Optional[str]:
    session = get_session(session_id)
    return session.kind if session else None


def _refresh_youtube_preview_urls(
    session: PreviewSession, prefer_height: int = 720
) -> None:
    """Re-resolve googlevideo URLs — they expire; stale URLs cause preview 403/500."""
    if session.platform != "YouTube":
        return
    try:
        from deps import settings_mgr

        oauth = settings_mgr.get().oauth or None
    except Exception:
        oauth = None
    was_window = session.dash_window_hls
    was_progressive = session.kind == "progressive"
    _invalidate_youtube_resolve_caches(session.vod_url, prefer_height)
    raw_entry, headers, _platform, variant_formats, kind, yt_info = resolve_stream_info(
        session.vod_url,
        oauth=oauth,
        prefer_height=prefer_height,
        force_fresh=True,
    )
    # ponytail: a progressive session that is being refreshed (e.g. far seek in
    # explore popup) must stay progressive so byte-range seek stays instant.
    # DASH-only resolves now return window-HLS; force a muxed progressive fallback
    # instead of making the user wait for a fresh seg0 mux on every seek.
    if was_progressive and kind == "hls":
        muxed = _youtube_muxed_progressive_for_long_explore(
            session.vod_url,
            oauth,
            prefer_height,
            yt_info=yt_info,
        )
        if muxed:
            prog_url, prog_formats, prog_info = muxed
            _apply_muxed_progressive_session(
                session,
                prog_url,
                prog_formats,
                prog_info,
                prefer_height,
            )
            return
        # ponytail: never make a progressive session switch to window-HLS on a
        # far seek. If YouTube returns only DASH formats, keep the existing
        # progressive URL and fresh cookies rather than forcing a multi-second
        # seg0 remux. The URL is usually still valid for the duration of a
        # preview session; if it expired, the player surfaces a network error
        # and the user can reopen the preview.
        session.http_headers = _merge_youtube_session_cookies(headers, session.vod_url)
        session.touch()
        _clear_preview_url_caches(session)
        return
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
    if was_window:
        _clear_youtube_window_hls_cache(session)
    else:
        session.dash_window_hls = False
        _clear_youtube_window_hls_cache(session)
    if kind == "hls":
        _apply_youtube_custom_master(session, variant_formats, yt_info)
    elif was_window:
        session.dash_window_hls = False
    for _height, upstream in session.variant_entries:
        session.allowed_hosts.update(_hosts_for_url(upstream))
    session.allowed_hosts.update(_hosts_for_url(session.entry_url))
    if kind == "hls" and not session.custom_master:
        session.entry_url = _resolve_preview_entry(session, raw_entry, prefer_height)
        session.allowed_hosts.update(_hosts_for_url(session.entry_url))
    elif session.custom_master and session.variant_entries:
        session.entry_url = (
            _pick_variant_by_height(
                session.variant_entries,
                prefer_height,
            )
            or session.variant_entries[0][1]
        )
        session.allowed_hosts.update(_hosts_for_url(session.entry_url))
    _stash_youtube_preview_formats(
        session,
        variant_formats,
        yt_info,
        prefer_height,
        session.entry_url,
    )
    _clear_preview_url_caches(session)
    session.touch()


def _pick_format_for_url(
    formats: List[dict], url: str, prefer_height: int
) -> Optional[dict]:
    for fmt in formats:
        if fmt.get("url") == url:
            return fmt
    for fmt in formats:
        if int(fmt.get("height") or 0) == prefer_height:
            return fmt
    for fmt in formats:
        if int(fmt.get("height") or 0) > 0:
            return fmt
    return None


def _stash_youtube_preview_formats(
    session: PreviewSession,
    variant_formats: List[dict],
    yt_info: Optional[dict],
    prefer_height: int,
    entry_url: str,
) -> None:
    """Keep format metadata for byte-range mux when googlevideo URLs omit clen/dur."""
    if yt_info:
        audio = yt_info.get("_preview_audio_format")
        session.preview_audio_fmt = audio if isinstance(audio, dict) else None
        session.explore_yt_info = yt_info
    session.preview_video_fmt = _pick_format_for_url(
        variant_formats,
        entry_url,
        prefer_height,
    )


def _refresh_youtube_window_hls_urls(
    session: PreviewSession, prefer_height: int = 720
) -> None:
    """Refresh googlevideo URLs for the window HLS mux — swap URL set, keep window cache."""
    if session.platform != "YouTube":
        return
    try:
        from deps import settings_mgr

        oauth = settings_mgr.get().oauth or None
    except Exception:
        oauth = None
    _invalidate_youtube_resolve_caches(session.vod_url, prefer_height)
    _raw, headers, _platform, variant_formats, _kind, yt_info = resolve_stream_info(
        session.vod_url,
        oauth=oauth,
        prefer_height=prefer_height,
        force_fresh=True,
    )
    session.http_headers = _merge_youtube_session_cookies(headers, session.vod_url)
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
    if session.variant_entries:
        picked = _pick_variant_by_height(session.variant_entries, prefer_height)
        if picked:
            session.entry_url = picked
            session.allowed_hosts.update(_hosts_for_url(picked))
    _stash_youtube_preview_formats(
        session,
        variant_formats,
        yt_info,
        prefer_height,
        session.entry_url,
    )
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
# ponytail: window HLS mux is one job per session — gate on session_id to prevent dup threads
_WINDOW_HLS_RUNNING: Set[str] = set()
_WINDOW_HLS_LOCK = threading.Lock()


def _pick_mux_height(session: PreviewSession, *, fast: bool = True) -> int:
    from services.ytdlp_hls import _PREVIEW_MUX_FAST_HEIGHT

    prefer = session_active_height(session) or 720
    if not fast:
        return prefer
    cap = _PREVIEW_MUX_FAST_HEIGHT
    heights = sorted({h for h, _ in session.variant_entries if h > 0})
    if not heights:
        return min(prefer, cap) or cap
    # Fast mux should respect the requested preview height. If the user asked
    # for 360p, don't silently upgrade to 480p just because it's under the cap.
    target = min(prefer, cap)
    at_or_below = [h for h in heights if h <= target]
    if at_or_below:
        return at_or_below[-1]
    at_or_below_cap = [h for h in heights if h <= cap]
    if at_or_below_cap:
        return at_or_below_cap[-1]
    return heights[0]


def _variant_url_for_height(session: PreviewSession, height: int) -> Optional[str]:
    for h, url in session.variant_entries:
        if h == height and url:
            return url
    return None


def _mux_output_path(session: PreviewSession, height: int) -> Path:
    return session.cache_dir / f"mux_{height}.mp4"


def _youtube_mux_file_if_ready(session: PreviewSession) -> Optional[Path]:
    for height in sorted(
        {h for h, _ in session.variant_entries if h > 0}, reverse=True
    ):
        path = _mux_output_path(session, height)
        if path.is_file() and path.stat().st_size >= MIN_VALID_OUTPUT_BYTES:
            return path
    path = _mux_output_path(session, _pick_mux_height(session, fast=True))
    if path.is_file() and path.stat().st_size >= MIN_VALID_OUTPUT_BYTES:
        return path
    return None


def preview_playlist_ready(session: PreviewSession) -> bool:
    """True when the player can attach (full VOD playlist or direct URL exists)."""
    if session.dash_window_hls:
        return _window_hls_seg0_matches_bounds(session)
    if session.platform == "YouTube" and _youtube_entry_needs_mux(session):
        if session.mux_status == "ready" or _youtube_mux_file_if_ready(session):
            return True
        return False
    return True


def preview_segment_buffer_ready(session: PreviewSession) -> bool:
    """True when the first playback segment is on disk (window HLS seg0 ready)."""
    if session.dash_window_hls:
        return _window_hls_seg0_matches_bounds(session)
    return preview_playlist_ready(session)


def preview_mux_ready(session: PreviewSession) -> bool:
    """True when mux is complete enough for playback.

    For window HLS, ready when ``window.m3u8`` is complete (contains
    ``#EXT-X-ENDLIST``) OR every contiguous segment the playlist declares is
    on disk.
    """
    if session.dash_window_hls:
        if _window_hls_playlist_complete(session):
            return True
        # fallback: every segment indexed by EXTINF must exist on disk
        playlist = _window_hls_playlist_path(session)
        if not playlist.is_file():
            return False
        try:
            text = playlist.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        declared = sum(
            1 for line in text.splitlines() if line.strip().startswith("#EXTINF:")
        )
        if declared <= 0:
            return _window_hls_seg0_ready(session)
        return _window_hls_all_segments_done(
            session, expected=declared
        ) or _window_hls_seg0_ready(session)
    if session.platform != "YouTube" or not _youtube_entry_needs_mux(session):
        return True
    if session.mux_status == "ready" or _youtube_mux_file_if_ready(session):
        return True
    return False


def preview_session_mux_status(session_id: str) -> Dict[str, object]:
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")
    if session.dash_window_hls and not preview_mux_ready(session):
        schedule_youtube_window_hls_mux(session_id)
    ready = preview_mux_ready(session)
    if ready and session.mux_status == "pending":
        session.mux_status = "ready"
    return {
        "mux_ready": ready,
        "playlist_ready": preview_playlist_ready(session),
        "segment_buffer_ready": preview_segment_buffer_ready(session),
        "mux_status": session.mux_status,
        "mux_error": session.mux_error or "",
        "window_hls_mux_start": float(getattr(session, "window_hls_mux_start", 0) or 0),
        "window_hls_mux_end": float(getattr(session, "window_hls_mux_end", 0) or 0),
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
        logger.warning(
            "background youtube mux failed session=%s: %s", session_id[:8], exc
        )


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


def _full_mux_cache_path(
    video_id: str, height: int, crop_start: float, crop_end: float
) -> Path:
    """Path to cached full-VOD mux — keyed by video_id and quality only.

    The cache file covers the entire VOD so any subsequent trim window is
    served via byte-range seeks against the same file. Older cache entries
    with crop-range keys are no longer written; existing ones are tolerated
    until TTL evicts them.
    """
    cache_key = f"{video_id}_{height}_full"
    return _FULL_MUX_CACHE_DIR / f"{cache_key}.mp4"


def _try_use_full_mux_cache(session: PreviewSession) -> Optional[Path]:
    """Return cached full mux path if it exists and is within TTL."""
    from services.youtube_innertube import extract_video_id

    vid = extract_video_id(session.vod_url or "")
    if not vid:
        return None
    height = session_active_height(session) or 720
    cached = _full_mux_cache_path(vid, height, session.crop_start, session.crop_end)
    if cached.is_file() and cached.stat().st_size >= MIN_VALID_OUTPUT_BYTES:
        age = time.time() - cached.stat().st_mtime
        if age < FULL_MUX_CACHE_TTL_SEC:
            return cached
        try:
            cached.unlink()
        except OSError:
            pass
    return None


def _enforce_full_mux_cache_budget(max_mb: int = 2000) -> None:
    """LRU eviction for full-mux persistent cache — removes oldest files when budget exceeded."""
    cache_dir = _FULL_MUX_CACHE_DIR
    if not cache_dir.is_dir():
        return
    files = list(cache_dir.glob("*.mp4"))
    if not files:
        return
    total = sum(f.stat().st_size for f in files if f.is_file())
    max_bytes = max_mb * 1024 * 1024
    if total <= max_bytes:
        return
    files.sort(key=lambda f: f.stat().st_mtime)
    for f in files:
        if total <= max_bytes:
            break
        try:
            sz = f.stat().st_size
            f.unlink()
            total -= sz
            logger.debug(
                "full-mux cache evicted %s (%d MB), total now %d MB",
                f.name,
                sz // (1024 * 1024),
                total // (1024 * 1024),
            )
        except OSError:
            continue


def _schedule_background_full_mux(session_id: str) -> None:
    """Kick off background job to mux full VOD at current quality to cache."""
    session = get_session(session_id)
    if not session or session.platform != "YouTube":
        return
    # CDN HLS already serves a muxed stream at the chosen tier — no mux needed.
    # Window-HLS, on the other hand, serves tiny chunks and benefits from a
    # background full-file mux so subsequent opens are instant from cache.
    if session.kind == "hls" and not session.dash_window_hls:
        return
    if session.custom_master:
        return
    if not _youtube_entry_needs_mux(session):
        return
    from services.youtube_innertube import extract_video_id

    vid = extract_video_id(session.vod_url or "")
    if not vid:
        return
    height = session_active_height(session) or 720
    cached = _full_mux_cache_path(vid, height, session.crop_start, session.crop_end)
    if cached.is_file() and cached.stat().st_size >= MIN_VALID_OUTPUT_BYTES:
        return
    threading.Thread(
        target=_run_background_full_mux,
        args=(session_id,),
        daemon=True,
        name=f"yt-full-mux-{session_id[:8]}",
    ).start()


# ponytail: MuxJob — single abstraction over the byte-range + ffmpeg pipeline.
# Both clip (window-HLS) and full (persistent cache) modes share authentication,
# retry, and validation logic through this struct. The actual download/mux
# implementation lives in ytdlp_hls._download_muxed_dash_clip — only the window
# and output path differ between modes.
@dataclass
class MuxJob:
    video_url: str
    audio_url: Optional[str]
    output_path: Path
    start_sec: float
    end_sec: float
    headers: Dict[str, str]
    prefer_height: int
    vod_url: str
    job_kind: str  # "clip" | "full" — for logging + telemetry only
    vod_duration: float = 0.0

    def run(self) -> bool:
        from services.ytdlp_hls import _download_muxed_dash_clip

        try:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            _download_muxed_dash_clip(
                self.video_url,
                self.audio_url or "",
                str(self.output_path),
                start_sec=self.start_sec,
                end_sec=self.end_sec,
                headers=self.headers,
                allow_remote_retry=True,
            )
            ok = (
                self.output_path.is_file()
                and self.output_path.stat().st_size >= MIN_VALID_OUTPUT_BYTES
            )
            if ok:
                logger.info(
                    "mux job done kind=%s height=%s bytes=%d start=%.1f end=%.1f",
                    self.job_kind,
                    self.prefer_height,
                    self.output_path.stat().st_size,
                    self.start_sec,
                    self.end_sec,
                )
            return ok
        except Exception as exc:
            logger.debug("mux job failed kind=%s: %s", self.job_kind, exc)
            return False


def _mux_job_from_session(session: PreviewSession) -> Optional[MuxJob]:
    """Build a clip-window MuxJob from an active preview session."""
    from services.youtube_innertube import extract_video_id

    vid = extract_video_id(session.vod_url or "")
    if not vid:
        return None
    height = _pick_mux_height(session, fast=True)
    out = _full_mux_cache_path(vid, height, session.crop_start, session.crop_end)
    video_url = _variant_url_for_height(session, height) or session.entry_url
    return MuxJob(
        video_url=video_url,
        audio_url=session.preview_audio_url,
        output_path=out,
        start_sec=max(0.0, float(session.crop_start)),
        end_sec=max(float(session.crop_start) + 0.5, float(session.crop_end)),
        headers=_merge_youtube_session_cookies(session.http_headers, session.vod_url),
        prefer_height=height,
        vod_url=session.vod_url,
        job_kind="clip",
    )


def _run_background_full_mux(session_id: str) -> None:
    """Background: mux the session's crop window to MP4 and persist to cache."""
    session = get_session(session_id)
    if not session:
        return
    job = _mux_job_from_session(session)
    if job is None:
        return
    if (
        job.output_path.is_file()
        and job.output_path.stat().st_size >= MIN_VALID_OUTPUT_BYTES
    ):
        return
    job.run()


def _youtube_entry_needs_mux(session: PreviewSession) -> bool:
    if session.platform != "YouTube":
        return False
    # Window-HLS already serves playable chunks via its own mux. We still want
    # to kick off a background full-file mux at the user's chosen quality so
    # re-opens of the same VOD are instant from cache (window-HLS cold-start
    # is ~1-3s; cached full mux is <100ms).
    if session.dash_window_hls:
        return bool(session.preview_audio_url) and bool(session.variant_entries)
    # Synthetic / CDN HLS — hls.js proxies googlevideo; no full-file ffmpeg mux.
    if session.kind == "hls" or session.custom_master:
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


def _finalize_youtube_session(session: PreviewSession, crop_start: float) -> PreviewSession:
    """ponytail: post-register work that both the inline and snapshot-reuse
    create_session paths share. Runs the mux cache check, schedules the
    background full mux / window-HLS mux, prewarms the first segment,
    and emits the diagnostic log line."""
    session_id = session.session_id
    if _youtube_entry_needs_mux(session):
        cached_full = _try_use_full_mux_cache(session)
        if cached_full:
            # Cached full-VOD mux is available. Mark the session as
            # progressive and remember the cached file path — the
            # proxy checks `cached_progressive_path` and serves the
            # local MP4 directly with HTTP Range support. Original
            # state (variant_entries, audio_url, etc.) is left
            # intact for diagnostics.
            session.kind = "progressive"
            session.dash_window_hls = False
            session.cached_progressive_path = str(cached_full)
            session.mux_status = "ready"
        else:
            schedule_youtube_preview_mux(session_id)

    if session.kind == "progressive":
        from services.youtube_diag import log_preview_session

        heights = [h for h, _u in session.variant_entries]
        log_preview_session(
            session_id,
            session.platform,
            session.kind,
            heights,
            custom_master=bool(session.custom_master),
            entry_url=session.entry_url,
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

    if session.dash_window_hls:
        _init_window_hls_mux_bounds(session)
        schedule_youtube_window_hls_mux(session_id)

    _enforce_full_mux_cache_budget()
    _schedule_background_full_mux(session_id)
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
        session.platform,
        session.kind,
        heights,
        custom_master=bool(session.custom_master),
        entry_url=session.entry_url,
    )
    return session


def _best_muxed_variant_url(session: PreviewSession) -> Optional[str]:
    candidates = [
        (h, u) for h, u in session.variant_entries if session.variant_muxed.get(h) and u
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def _ensure_youtube_preview_mux(session: PreviewSession, *, fast: bool = True) -> Path:
    """Mux DASH video+audio for the trim window into a local MP4 (preview only)."""
    from services.ytdlp_hls import _download_muxed_dash_clip

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
        video_url = _variant_url_for_height(session, height) or session.entry_url
        _download_muxed_dash_clip(
            video_url,
            session.preview_audio_url or "",
            str(out),
            start_sec=start,
            end_sec=end,
            headers=_merge_youtube_session_cookies(
                session.http_headers, session.vod_url
            ),
            allow_remote_retry=True,
        )
        yt_log = logging.getLogger("VOD.RIP.youtube")
        if yt_log.isEnabledFor(logging.DEBUG):
            yt_log.debug(
                "preview mux session=%s height=%s dur=%.1fs fast_height=%s",
                session.session_id[:8],
                height,
                end - start,
                fast,
            )
    if not out.is_file() or out.stat().st_size < MIN_VALID_OUTPUT_BYTES:
        raise RuntimeError("YouTube preview mux produced no output")
    return out


def _local_file_content_type(path: Path) -> str:
    suf = path.suffix.lower()
    if suf == ".ts":
        return "video/mp2t"
    if suf == ".mp4":
        return "video/mp4"
    return _guess_content_type(str(path))


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

    return _generate, _local_file_content_type(path), hdrs, status, lambda: None


def _open_memory_bytes_proxy(
    data: bytes,
    content_type: str,
    range_header: Optional[str] = None,
) -> Tuple[Callable[[], object], str, dict, int, Callable[[], None]]:
    """Range-aware proxy for in-memory cached segments (206 + Accept-Ranges)."""
    size = len(data)
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
        "Cache-Control": "public, max-age=3600",
    }
    if status == 206:
        hdrs["Content-Range"] = f"bytes {start}-{end}/{size}"
    slice_data = data[start : start + length]

    def _once() -> object:
        yield slice_data

    return _once, content_type, hdrs, status, lambda: None


def _bytes_response_for_range(
    data: bytes,
    range_header: Optional[str],
) -> Tuple[bytes, dict, int]:
    """Slice buffered bytes for Range requests (206 + Content-Range)."""
    size = len(data)
    if not range_header:
        return (
            data,
            {
                "Accept-Ranges": "bytes",
                "Content-Length": str(size),
                "Cache-Control": "public, max-age=3600",
            },
            200,
        )
    m = re.match(r"bytes=(\d+)-(\d*)", range_header.strip())
    if not m:
        return data, {"Accept-Ranges": "bytes", "Content-Length": str(size)}, 200
    start = int(m.group(1))
    end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
    if start >= size:
        start = max(0, size - min(256 * 1024, size))
        end = size - 1
    if end < start:
        end = start
    body = data[start : end + 1]
    return (
        body,
        {
            "Accept-Ranges": "bytes",
            "Content-Length": str(len(body)),
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Cache-Control": "public, max-age=3600",
        },
        206,
    )


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
    # Warmed head cache: serve ftyp+moov+opening media from disk (moov on
    # multi-hour VODs is 8+ MB — the dominant canplay cost after a warm).
    if session.platform == "YouTube":
        spliced = _try_prog_head_proxy(session, range_header)
        if spliced is not None:
            return spliced
    # ponytail: cached full-VOD mux shortcut. The session was originally HLS
    # (window-HLS) but a full mux finished in the background; flipping kind
    # to progressive + setting cached_progressive_path is the bridge. Serve
    # the local MP4 directly so the browser can do native byte-range seeks.
    cached_path = getattr(session, "cached_progressive_path", None)
    if cached_path:
        p = Path(cached_path)
        if p.is_file() and p.stat().st_size >= MIN_VALID_OUTPUT_BYTES:
            return _open_local_file_proxy(p, range_header)
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
        logger.debug(
            "progressive proxy refresh after error session=%s: %s",
            session_id[:8],
            first_exc,
        )
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

    cached = _read_cache(session, upstream_url)
    if cached is not None:
        return _open_memory_bytes_proxy(
            cached,
            _guess_content_type(upstream_url),
            range_header,
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
    from services.youtube_innertube import (
        _collect_merged_innertube_info,
        extract_video_id,
    )

    vid = extract_video_id(full_url)
    if vid:
        merged_info = _collect_merged_innertube_info(vid, session, 12.0)
        if merged_info and not _youtube_info_is_dash_only_progressive(merged_info):
            return merged_info
    info = innertube_extract_info(
        full_url,
        session=session,
        allow_session_refresh=True,
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
        session.http_headers,
        session.vod_url,
    )
    return (
        session.kind != old_kind
        or _best_muxed_variant_url(session) is not None
        or (old_needs_mux and not _youtube_entry_needs_mux(session))
    )


def _extract_youtube_preview_info(
    full_url: str, oauth: Optional[str], *, warm_light: bool = False
) -> dict:
    """Cached YouTube resolve — InnerTube multi-client, then yt-dlp fallback.

    ``warm_light=True`` (bulk warm) runs the InnerTube fast pass only — the
    yt-dlp lock and retry chain are reserved for real user clicks.
    """
    from deps import settings_mgr
    from services.ytdlp_hls import cached_extract_info, youtube_preview_ytdl_opts

    cookies = settings_mgr.get().youtube_cookies_file or None
    opts = youtube_preview_ytdl_opts(full_url, oauth=oauth, cookies_file=cookies)
    if warm_light:
        opts["_warm_light"] = True
    return cached_extract_info(full_url, opts)


def _deduped_hls_variants(info: dict) -> List[dict]:
    formats = info.get("formats") or []
    hls = [
        f
        for f in formats
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


def _looks_like_direct_mp4_file(fmt: dict) -> bool:
    """True for plain https/http MP4/MOV files even when codec metadata is missing.

    Twitch clip formats from yt-dlp come through without vcodec/acodec, but the
    URLs are direct progressive MP4s. This keeps them in the progressive pool
    while staying conservative enough to avoid treating YouTube DASH video-only
    MP4s as muxed previews.
    """
    proto = (fmt.get("protocol") or "").lower()
    ext = (fmt.get("ext") or "").lower()
    if proto and proto not in ("https", "http"):
        return False
    if ext not in ("mp4", "m4v", "mov"):
        return False
    url = (fmt.get("url") or "").lower()
    if ".m3u8" in url or "/manifest" in url or ".mpd" in url:
        return False
    # Only trust codec-less MP4s for Twitch clip CDNs.
    if "twitch.tv" not in url and "cloudfront.net" not in url:
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
        if proto in (
            "m3u8",
            "m3u8_native",
            "m3u8_ffmpeg",
            "http_dash_segments",
            "dash",
            "http_dash",
        ):
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
        if not _is_muxed_format(f) and not _looks_like_direct_mp4_file(f):
            continue
        progressive.append(f)

    progressive.sort(
        key=lambda f: (
            int(f.get("height") or 0),
            float(f.get("tbr") or 0) or float(f.get("vbr") or 0) or 0.0,
        ),
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


def _youtube_muxed_progressive_for_long_explore(
    vod_url: str,
    oauth: Optional[str],
    prefer_height: int,
    *,
    yt_info: Optional[dict] = None,
) -> Optional[Tuple[str, List[dict], dict]]:
    """Fresh muxed URL for long-VOD explore — accepts sole low tier (360p) for byte-range seek."""
    from services.youtube_innertube import _dedupe_youtube_formats

    def _pick_from_info(info: dict) -> Optional[Tuple[str, List[dict], dict]]:
        _resolve_youtube_preview_audio(info)
        merged = _dedupe_youtube_formats(info.get("formats") or [])
        muxed = _deduped_progressive_variants({"formats": merged})
        if not muxed:
            return None
        prog_urls = [(int(v.get("height") or 0), v.get("url") or "") for v in muxed]
        prog_url = _pick_variant_by_height(prog_urls, prefer_height) or (
            muxed[0].get("url") or ""
        )
        if not prog_url:
            return None
        out_info = dict(info)
        picked = next((f for f in muxed if f.get("url") == prog_url), muxed[0])
        if picked.get("http_headers"):
            out_info["http_headers"] = {
                **(out_info.get("http_headers") or {}),
                **picked["http_headers"],
            }
        return prog_url, muxed, out_info

    if yt_info:
        picked = _pick_from_info(yt_info)
        if picked:
            return picked
    try:
        info = _reextract_youtube_for_preview(vod_url, oauth)
    except Exception as exc:
        logger.debug("long explore muxed re-extract: %s", exc)
        return None
    return _pick_from_info(info)


def _apply_muxed_progressive_session(
    session: PreviewSession,
    prog_url: str,
    prog_formats: List[dict],
    prog_info: dict,
    prefer_height: int,
) -> None:
    """Switch session from window-HLS/DASH to native progressive playback."""
    session.kind = "progressive"
    session.dash_window_hls = False
    session.custom_master = None
    session.master_url = f"/api/preview/hls/{session.session_id}/master.m3u8"
    session.entry_url = prog_url
    session.variant_entries = [
        (int(fmt.get("height") or 0), fmt.get("url") or "")
        for fmt in prog_formats
        if int(fmt.get("height") or 0) > 0 and fmt.get("url")
    ]
    session.variant_muxed = {
        int(fmt.get("height") or 0): True
        for fmt in prog_formats
        if int(fmt.get("height") or 0) > 0
    }
    session.preview_audio_url = _resolve_youtube_preview_audio(prog_info)
    if prog_info.get("http_headers"):
        session.http_headers = _merge_youtube_session_cookies(
            prog_info.get("http_headers") or session.http_headers,
            session.vod_url,
        )
    for _height, upstream in session.variant_entries:
        session.allowed_hosts.update(_hosts_for_url(upstream))
    session.allowed_hosts.update(_hosts_for_url(session.entry_url))
    _clear_youtube_window_hls_cache(session)
    _clear_youtube_mux_cache(session)
    _stash_youtube_preview_formats(
        session,
        prog_formats,
        prog_info,
        prefer_height,
        session.entry_url,
    )
    _clear_preview_url_caches(session)
    session.mux_status = "ready"
    session.mux_error = None


def _url_looks_like_master(url: str) -> bool:
    lower = url.lower()
    return "master" in lower or "multivariant" in lower


def _pick_variant_by_height(
    entries: List[Tuple[int, str]], prefer_height: int
) -> Optional[str]:
    if not entries:
        return None
    by_height = sorted(entries, key=lambda t: t[0])
    for height, url in by_height:
        if height == prefer_height:
            return url
    at_or_below = [
        entry for entry in by_height if entry[0] and entry[0] <= prefer_height
    ]
    if at_or_below:
        return at_or_below[-1][1]
    return by_height[0][1]


def _build_synthetic_master_playlist(
    session: PreviewSession, variants: List[dict]
) -> str:
    audio_fmt = None
    if session.platform == "YouTube":
        try:
            info = _extract_youtube_preview_info(session.vod_url, None)
            audio_fmt = info.get("_preview_audio_format")
        except Exception:
            audio_fmt = None
        if audio_fmt or any(
            f.get("acodec") in ("none", None)
            for f in variants
            if int(f.get("height") or 0) > 0
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
        bandwidth = (
            int((fmt.get("tbr") or 0) * 1000)
            or int((fmt.get("vbr") or 0) * 1000)
            or 1_000_000
        )
        upstream = fmt.get("url") or ""
        session.allowed_hosts.update(_hosts_for_url(upstream))
        lines.append(
            f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={width}x{height}"
        )
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
    video_variants = [
        f for f in variants if int(f.get("height") or 0) > 0 and f.get("url")
    ]
    needs_audio = audio_fmt and any(
        f.get("acodec") in ("none", None) for f in video_variants
    )
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


def _session_crop_span_sec(session: PreviewSession) -> float:
    return max(0.0, float(session.crop_end) - float(session.crop_start))


def session_trim_timeline(session: PreviewSession) -> bool:
    """Clip-relative 0-based timeline — only short trim window-HLS, not full-VOD explore."""
    return bool(
        getattr(session, "dash_window_hls", False)
        and _session_crop_span_sec(session) <= WINDOW_HLS_SHORT_VOD_MAX_SEC
    )


def _youtube_is_dash_separate_audio(
    formats: List[dict],
    yt_info: Optional[dict],
) -> bool:
    """True when YouTube only offers separate video+audio googlevideo HTTPS (no muxed tier)."""
    if not yt_info:
        return False
    _resolve_youtube_preview_audio(yt_info)
    audio_fmt = yt_info.get("_preview_audio_format")
    if not audio_fmt or not audio_fmt.get("url"):
        return False
    video_fmts = [f for f in formats if int(f.get("height") or 0) > 0]
    if not video_fmts or not _formats_are_dash_https(video_fmts):
        return False
    return any(f.get("acodec") in ("none", None) for f in video_fmts)


def _youtube_needs_dash_window_hls(
    formats: List[dict],
    yt_info: Optional[dict],
    *,
    crop_span_sec: float = WINDOW_HLS_SHORT_VOD_MAX_SEC,
) -> bool:
    """DASH-only — local ffmpeg HLS mux (short trim or long explore when synthetic MP4 fails).

    ponytail: crop_span gate removed for long VOD — synthetic DASH master is not hls.js-playable.
    Window HLS initial chunk is 8s; frontend must not block session open on seg0 poll.
    """
    if not _youtube_is_dash_separate_audio(formats, yt_info):
        return False
    return True


def _apply_youtube_custom_master(
    session: PreviewSession,
    variant_formats: List[dict],
    yt_info: Optional[dict],
) -> None:
    if _youtube_needs_dash_window_hls(
        variant_formats,
        yt_info,
        crop_span_sec=_session_crop_span_sec(session),
    ):
        session.dash_window_hls = True
        session.resource_map = {
            k: v
            for k, v in session.resource_map.items()
            if not k.startswith(WINDOW_HLS_MARKER)
        }
        session.custom_master = _build_youtube_window_hls_master(session)
        return
    session.dash_window_hls = False
    # ponytail: synthetic master points hls.js at raw googlevideo MP4 — not playable.
    if len(session.variant_entries) >= 2 and not _youtube_is_dash_separate_audio(
        variant_formats,
        yt_info,
    ):
        session.custom_master = _build_synthetic_master_playlist(
            session, variant_formats
        )
    else:
        session.custom_master = None


def _build_youtube_window_hls_master(session: PreviewSession) -> str:
    """Single-variant master — HLS.js loads the window-playlist resource via /resource."""
    height = _pick_mux_height(session, fast=True) or 720
    width = int(height * 16 / 9)
    bandwidth = 1_500_000
    playlist_url = f"/api/preview/hls/{session.session_id}/resource?id={WINDOW_HLS_PLAYLIST_RESOURCE}"
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={width}x{height}",
        playlist_url,
    ]
    return "\n".join(lines) + "\n"


def _window_hls_dir(session: PreviewSession) -> Path:
    return session.cache_dir / "window_hls"


def _window_hls_playlist_path(session: PreviewSession) -> Path:
    """Path to the locally muxed window HLS playlist (window.m3u8)."""
    return _window_hls_dir(session) / "window.m3u8"


def _window_hls_segment_path(session: PreviewSession, index: int) -> Path:
    """Path to a single muxed segment index (seg_NNN.ts or seg_NNN.m4s when fMP4)."""
    out_dir = _window_hls_dir(session)
    if USE_FMP4 and (out_dir / f"seg_{index:03d}.m4s").is_file():
        return out_dir / f"seg_{index:03d}.m4s"
    return out_dir / f"seg_{index:03d}.ts"


def _window_hls_existing_segments(session: PreviewSession) -> List[Path]:
    """Return sorted seg_NNN.ts (or .m4s when fMP4) files on disk (empty until mux starts)."""
    out_dir = _window_hls_dir(session)
    if not out_dir.is_dir():
        return []
    if USE_FMP4:
        m4s = sorted(out_dir.glob("seg_[0-9][0-9][0-9].m4s"))
        if m4s:
            return m4s
    return sorted(out_dir.glob("seg_[0-9][0-9][0-9].ts"))


def _window_hls_seg0_ready(session: PreviewSession) -> bool:
    """True when at least one muxed segment is on disk — first-playback ready."""
    seg0 = _window_hls_segment_path(session, 0)
    if seg0.is_file() and seg0.stat().st_size >= MIN_VALID_OUTPUT_BYTES:
        return True
    return False


def _window_hls_mux_start_marker(session: PreviewSession) -> Path:
    return _window_hls_dir(session) / ".mux_start"


def _window_hls_seg0_matches_bounds(session: PreviewSession) -> bool:
    """Seg0 on disk belongs to the session's current window_hls_mux_start (not a stale seek)."""
    if not _window_hls_seg0_ready(session):
        return False
    marker = _window_hls_mux_start_marker(session)
    if not marker.is_file():
        return float(session.window_hls_mux_start or 0) < 0.01
    try:
        stamped = float(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return abs(stamped - float(session.window_hls_mux_start or 0)) < 0.01


def _stamp_window_hls_mux_start(session: PreviewSession, start_sec: float) -> None:
    try:
        _window_hls_mux_start_marker(session).write_text(
            f"{float(start_sec):.3f}",
            encoding="utf-8",
        )
    except OSError:
        pass


def _window_hls_playlist_complete(session: PreviewSession) -> bool:
    """True when window.m3u8 contains the EXT-X-ENDLIST tag (mux finished)."""
    playlist = _window_hls_playlist_path(session)
    if not playlist.is_file():
        return False
    try:
        text = playlist.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "#EXT-X-ENDLIST" in text


def _window_hls_all_segments_done(
    session: PreviewSession,
    expected: Optional[int] = None,
) -> bool:
    """True when the muxed window has produced every contiguous segment up to *expected*.

    If *expected* is None we accept "all on-disk segments are non-empty" as done
    (handles the case where crop_end clamps the segment count below the playlist's
    declared target duration).
    """
    segs = _window_hls_existing_segments(session)
    if not segs:
        return False
    if expected is not None:
        if len(segs) < expected:
            return False
        # ensure final gap from playlist to disk is full and contiguous
        try:
            last_index = int(segs[-1].stem.split("_", 1)[1])
        except (IndexError, ValueError):
            return False
        if last_index + 1 < expected:
            return False
    return all(seg.stat().st_size >= MIN_VALID_OUTPUT_BYTES for seg in segs)


def _clear_youtube_window_hls_cache(session: PreviewSession) -> None:
    try:
        out_dir = _window_hls_dir(session)
        if out_dir.is_dir():
            for child in out_dir.iterdir():
                try:
                    if child.is_file():
                        child.unlink()
                    elif child.is_dir():
                        import shutil

                        shutil.rmtree(child, ignore_errors=True)
                except OSError:
                    continue
    except OSError:
        pass


def _enforce_window_hls_cache_budget(
    session: PreviewSession,
) -> None:
    """Evict LRU window-HLS segments when total cache exceeds budget (default 500 MB)."""
    out_dir = _window_hls_dir(session)
    if not out_dir.is_dir():
        return
    segs = list(out_dir.glob("seg_[0-9][0-9][0-9].ts"))
    if not segs:
        return
    total = sum(s.stat().st_size for s in segs if s.is_file())
    max_bytes = WINDOW_HLS_CACHE_MB * 1024 * 1024
    if total <= max_bytes:
        return
    segs.sort(key=lambda s: s.stat().st_mtime)
    for s in segs:
        if total <= max_bytes:
            break
        try:
            sz = s.stat().st_size
            s.unlink()
            total -= sz
            logger.debug(
                "window-HLS evicted %s (%d bytes), cache now %d MB",
                s.name,
                sz,
                total // (1024 * 1024),
            )
        except OSError:
            continue


def _window_hls_seek_chunk_sec(session: PreviewSession) -> float:
    """Seek remux window — wider on long VODs to cut remux frequency."""
    dur = float(session.vod_duration or 0)
    if dur >= WINDOW_HLS_LONG_VOD_MIN_SEC:
        return WINDOW_HLS_MUX_CHUNK_LONG_SEC
    return WINDOW_HLS_MUX_CHUNK_SEC


def _window_hls_mux_bounds(
    session: PreviewSession,
    *,
    around_sec: Optional[float] = None,
) -> Tuple[float, float]:
    """Return [start, end) seconds to mux — capped chunk inside crop trim."""
    crop_lo = max(0.0, float(session.crop_start))
    crop_hi = max(crop_lo + 0.5, float(session.crop_end))
    chunk = (
        _window_hls_seek_chunk_sec(session)
        if around_sec is not None
        else WINDOW_HLS_INITIAL_CHUNK_SEC
    )
    if around_sec is not None:
        pos = max(crop_lo, min(float(around_sec), crop_hi - 0.1))
        # ponytail: put the target near the start of the chunk (one segment in)
        # instead of in the middle. This makes the first buffered segment start
        # close to the seek target, so the explicit currentTime seek lands on a
        # keyframe and the user does not see pre-target content while the decoder
        # catches up. The rest of the chunk still provides enough runway for
        # playback.
        start = max(crop_lo, pos - WINDOW_HLS_SEGMENT_SEC)
        end = min(crop_hi, start + chunk)
        if end - start < min(chunk, crop_hi - crop_lo):
            start = max(crop_lo, end - chunk)
        return start, end
    span = crop_hi - crop_lo
    if span <= WINDOW_HLS_SHORT_VOD_MAX_SEC:
        return crop_lo, crop_hi
    end = min(crop_hi, crop_lo + chunk)
    return crop_lo, end


def _init_window_hls_mux_bounds(session: PreviewSession) -> None:
    start, end = _window_hls_mux_bounds(session)
    session.window_hls_mux_start = start
    session.window_hls_mux_end = end


def _position_in_window_hls_mux(session: PreviewSession, position_sec: float) -> bool:
    if session.window_hls_mux_end <= session.window_hls_mux_start:
        return False
    span = session.window_hls_mux_end - session.window_hls_mux_start
    # ponytail: keep a one-segment safety margin, but never eat the whole short
    # initial chunk (e.g. 3 s) — cap margin at 25 % of the current window.
    margin = min(WINDOW_HLS_SEGMENT_SEC, max(0.5, span * 0.25))
    return (
        session.window_hls_mux_start - margin
        <= float(position_sec)
        < session.window_hls_mux_end - margin
    )


def _window_hls_bytes_cached(session: PreviewSession, position_sec: float) -> int:
    """Bytes already on disk for a given position — used for seek detection.

    Returns the total bytes cached around *position_sec* within the active mux window.
    A non-zero return means the browser can range-seek without a new remux.
    """
    if not _window_hls_seg0_ready(session):
        return 0
    if not _position_in_window_hls_mux(session, position_sec):
        return 0
    total = 0
    for seg in _window_hls_existing_segments(session):
        try:
            total += seg.stat().st_size
        except OSError:
            continue
    return total


def youtube_window_hls_seek_remux(session_id: str, position_sec: float) -> bool:
    """Remux a fresh chunk around *position_sec* when outside the active window."""
    session = get_session(session_id)
    if not session or not session.dash_window_hls:
        return False
    if _position_in_window_hls_mux(
        session, position_sec
    ) and _window_hls_seg0_matches_bounds(session):
        cached = _window_hls_bytes_cached(session, position_sec)
        if cached > 0:
            return False  # bytes already on disk — native range seek is instant
    start, end = _window_hls_mux_bounds(session, around_sec=position_sec)
    session.window_hls_mux_start = start
    session.window_hls_mux_end = end
    session.mux_status = "pending"
    session.mux_error = None
    height = _pick_mux_height(session, fast=True)
    _refresh_youtube_window_hls_urls(session, prefer_height=height)
    _clear_youtube_window_hls_cache(session)
    with _WINDOW_HLS_LOCK:
        _WINDOW_HLS_RUNNING.discard(session_id)
    schedule_youtube_window_hls_mux(session_id)
    return True


def _preflight_mux_dir(video_id: str, prefer_height: int) -> Path:
    return _PREVIEW_ROOT / "preflight" / f"{video_id}_{prefer_height}"


def _preflight_seg0_ready(out_dir: Path) -> bool:
    seg0 = out_dir / "seg_000.ts"
    return seg0.is_file() and seg0.stat().st_size >= MIN_VALID_OUTPUT_BYTES


def _try_adopt_preflight_mux(session: PreviewSession) -> bool:
    """Reuse paste-warm mux when crop starts at 0 and tier matches."""
    from services.youtube_innertube import extract_video_id

    if float(session.window_hls_mux_start) > 0.01:
        return False
    vid = extract_video_id(session.vod_url or "")
    if not vid:
        return False
    height = int(session.prefer_height or 720)
    src = _preflight_mux_dir(vid, height)
    if not _preflight_seg0_ready(src):
        return False
    dst = _window_hls_dir(session)
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("seg_000.ts", "window.m3u8", "_v.mp4", "_a.m4a"):
        src_file = src / name
        if src_file.is_file():
            shutil.copy2(src_file, dst / name)
    for seg in sorted(src.glob("seg_[0-9][0-9][0-9].ts")):
        if seg.name == "seg_000.ts":
            continue
        shutil.copy2(seg, dst / seg.name)
    return _window_hls_seg0_ready(session)


def kickoff_youtube_preflight_mux(
    url: str,
    oauth: Optional[str] = None,
    prefer_height: int = 720,
) -> None:
    """Background mux of the initial window chunk — adopted on create_session."""
    from services.youtube_innertube import extract_video_id

    vid = extract_video_id((url or "").strip())
    if not vid:
        return
    key = f"{vid}:{prefer_height}"
    with _PREFLIGHT_MUX_LOCK:
        if key in _PREFLIGHT_MUX_INFLIGHT:
            return
        out_dir = _preflight_mux_dir(vid, prefer_height)
        if _preflight_seg0_ready(out_dir):
            return
        done = threading.Event()
        _PREFLIGHT_MUX_INFLIGHT[key] = done

    def _run() -> None:
        try:
            _youtube_preflight_mux(url, oauth=oauth, prefer_height=prefer_height)
        finally:
            with _PREFLIGHT_MUX_LOCK:
                _PREFLIGHT_MUX_INFLIGHT.pop(key, None)
            done.set()

    from deps import GESTURE_WARM_EXECUTOR

    GESTURE_WARM_EXECUTOR.submit(_run)


def kickoff_youtube_batch_warm(
    url: str,
    oauth: Optional[str] = None,
    cookies_file: Optional[str] = None,
    prefer_height: int = 720,
) -> None:
    """Lightweight warm for batch (startup) use.

    - Deduped via the same _YOUTUBE_WARM_INFLIGHT set as kickoff_youtube_warm.
    - Skips the "active preview" bail so the user's first click doesn't
      cancel preloading.
    - Populates only the resolve cache (no preflight mux) so concurrent
      batch warm jobs don't fight over _PREFLIGHT_MUX_INFLIGHT.
    """
    from services.ytdlp_hls import preview_fast_only_mode

    if preview_fast_only_mode():
        return
    key = _youtube_warm_inflight_key(url)
    if not key:
        return

    def _run() -> None:
        # See kickoff_youtube_warm — in-flight registration happens at run start
        # so create_session never waits on a queued (not running) warm.
        with _YOUTUBE_WARM_LOCK:
            if key in _YOUTUBE_WARM_INFLIGHT:
                return
            done = threading.Event()
            _YOUTUBE_WARM_INFLIGHT[key] = done
        try:
            # ponytail: warm_youtube_resolve_only itself builds + stashes the
            # session snapshot. Calling it twice would re-extract + re-build.
            warm_youtube_resolve_only(
                url, oauth=oauth, prefer_height=prefer_height,
            )
        finally:
            with _YOUTUBE_WARM_LOCK:
                ev = _YOUTUBE_WARM_INFLIGHT.pop(key, None)
            if ev is not None:
                ev.set()

    from deps import WARM_EXECUTOR

    WARM_EXECUTOR.submit(_run)


def _youtube_preflight_mux(
    url: str,
    oauth: Optional[str] = None,
    prefer_height: int = 720,
) -> bool:
    """Mux [0, INITIAL_CHUNK) to preflight cache — no session required."""
    from services.youtube_innertube import extract_video_id
    from services.ytdlp_hls import _mux_dash_window_to_hls

    vid = extract_video_id((url or "").strip())
    if not vid:
        return False
    out_dir = _preflight_mux_dir(vid, prefer_height)
    if _preflight_seg0_ready(out_dir):
        return True
    try:
        _raw, headers, platform, variant_formats, _kind, yt_info = resolve_stream_info(
            url,
            oauth=oauth,
            prefer_height=prefer_height,
        )
    except Exception as exc:
        logger.debug("preflight mux resolve skipped %s: %s", vid, exc)
        return False
    if platform != "YouTube" or not _youtube_needs_dash_window_hls(
        variant_formats, yt_info
    ):
        return False
    _resolve_youtube_preview_audio(yt_info)
    audio_fmt = yt_info.get("_preview_audio_format") if yt_info else None
    audio_url = (audio_fmt or {}).get("url") or ""
    video_url = (
        _pick_variant_by_height(
            [(int(f.get("height") or 0), f.get("url") or "") for f in variant_formats],
            prefer_height=prefer_height,
        )
        or ""
    )
    if not video_url or not audio_url:
        return False
    video_fmt = next((f for f in variant_formats if f.get("url") == video_url), None)
    mux_hdrs = _merge_youtube_session_cookies(headers, url)
    end = WINDOW_HLS_INITIAL_CHUNK_SEC
    vod_dur = float((yt_info or {}).get("duration") or 0)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        _mux_dash_window_to_hls(
            video_url,
            audio_url,
            str(out_dir),
            start_sec=0.0,
            end_sec=end,
            headers=mux_hdrs,
            video_fmt=video_fmt,
            audio_fmt=audio_fmt if isinstance(audio_fmt, dict) else None,
            vod_duration=vod_dur,
        )
    except Exception as exc:
        logger.debug("preflight mux failed %s: %s", vid, exc)
        return False
    return _preflight_seg0_ready(out_dir)


def _try_fallback_from_window_hls(
    session: PreviewSession, prefer_height: int = 720
) -> bool:
    """On window-HLS mux failure, switch to muxed HLS when InnerTube offers it."""
    if not session.dash_window_hls or session.platform != "YouTube":
        return False
    try:
        from deps import settings_mgr

        oauth = settings_mgr.get().oauth or None
    except Exception:
        oauth = None
    if _session_crop_span_sec(session) > WINDOW_HLS_SHORT_VOD_MAX_SEC:
        muxed = _youtube_muxed_progressive_for_long_explore(
            session.vod_url,
            oauth,
            prefer_height,
            yt_info=getattr(session, "explore_yt_info", None),
        )
        if muxed:
            prog_url, prog_formats, prog_info = muxed
            _apply_muxed_progressive_session(
                session,
                prog_url,
                prog_formats,
                prog_info,
                prefer_height,
            )
            logger.info(
                "window hls fallback to progressive session=%s",
                session.session_id[:8],
            )
            return True
    _invalidate_youtube_resolve_caches(session.vod_url, prefer_height)
    try:
        raw_entry, headers, _platform, variant_formats, kind, yt_info = (
            resolve_stream_info(
                session.vod_url,
                oauth=oauth,
                prefer_height=prefer_height,
                force_fresh=True,
            )
        )
    except Exception as exc:
        logger.debug(
            "window hls fallback resolve failed session=%s: %s",
            session.session_id[:8],
            exc,
        )
        return False
    if _youtube_needs_dash_window_hls(
        variant_formats,
        yt_info,
        crop_span_sec=_session_crop_span_sec(session),
    ):
        return False
    session.kind = kind
    session.entry_url = raw_entry
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
    session.preview_audio_url = _resolve_youtube_preview_audio(yt_info)
    session.dash_window_hls = False
    session.custom_master = None
    _clear_youtube_window_hls_cache(session)
    _clear_youtube_mux_cache(session)
    proxy_master_url: Optional[str] = None
    if kind == "progressive":
        proxy_master_url = f"/api/preview/hls/{session.session_id}/master.m3u8"
    session.master_url = proxy_master_url or raw_entry
    if kind == "hls":
        _apply_youtube_custom_master(session, variant_formats, yt_info)
        for _height, upstream in session.variant_entries:
            session.allowed_hosts.update(_hosts_for_url(upstream))
    if kind == "progressive":
        session.allowed_hosts.update(_hosts_for_url(session.entry_url))
    elif session.custom_master and session.variant_entries:
        session.entry_url = (
            _pick_variant_by_height(
                session.variant_entries,
                prefer_height,
            )
            or session.variant_entries[0][1]
        )
        session.allowed_hosts.update(_hosts_for_url(session.entry_url))
    elif kind == "hls" and not session.custom_master:
        session.entry_url = _resolve_preview_entry(session, raw_entry, prefer_height)
        session.allowed_hosts.update(_hosts_for_url(session.entry_url))
    _stash_youtube_preview_formats(
        session,
        variant_formats,
        yt_info,
        prefer_height,
        session.entry_url,
    )
    _clear_preview_url_caches(session)
    session.mux_status = "ready"
    session.mux_error = None
    logger.info(
        "window hls fallback to %s session=%s",
        kind,
        session.session_id[:8],
    )
    return True


def _ensure_youtube_window_hls_mux(session: PreviewSession) -> bool:
    """Background job — mux a capped chunk of the crop window to local HLS.

    Full ``crop_end`` stays on the session for trim UI; only
    ``window_hls_mux_start/end`` (capped chunk) are fetched/muxed per pass.
    Seek remux recenters the chunk via :func:`youtube_window_hls_seek_remux`.
    """
    from services.ytdlp_hls import StaleGooglevideoUrl, _mux_dash_window_to_hls

    if not session.dash_window_hls:
        return False
    out_dir = _window_hls_dir(session)
    if _window_hls_seg0_matches_bounds(session):
        _register_youtube_window_hls_resources(session)
        return True
    if _try_adopt_preflight_mux(session):
        _stamp_window_hls_mux_start(session, float(session.window_hls_mux_start or 0))
        _register_youtube_window_hls_resources(session)
        return True
    out_dir.mkdir(parents=True, exist_ok=True)
    if session.window_hls_mux_end <= session.window_hls_mux_start:
        _init_window_hls_mux_bounds(session)
    start = max(0.0, float(session.window_hls_mux_start))
    end = max(start + 0.5, float(session.window_hls_mux_end))
    height = _pick_mux_height(session, fast=True)
    _refresh_youtube_window_hls_urls(session, prefer_height=height)
    video_url = _variant_url_for_height(session, height) or session.entry_url
    audio_url = session.preview_audio_url or ""
    if not audio_url:
        raise RuntimeError("YouTube window HLS missing audio URL")
    if not video_url:
        raise RuntimeError("YouTube window HLS missing video URL")
    if not _hosts_for_url(video_url) & session.allowed_hosts:
        session.allowed_hosts.update(_hosts_for_url(video_url))
    if not _hosts_for_url(audio_url) & session.allowed_hosts:
        session.allowed_hosts.update(_hosts_for_url(audio_url))
    mux_hdrs = _merge_youtube_session_cookies(session.http_headers, session.vod_url)
    last_exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            _mux_dash_window_to_hls(
                video_url,
                audio_url,
                str(out_dir),
                start_sec=start,
                end_sec=end,
                headers=mux_hdrs,
                video_fmt=getattr(session, "preview_video_fmt", None),
                audio_fmt=getattr(session, "preview_audio_fmt", None),
                vod_duration=float(session.vod_duration or 0),
                mux_timeout_sec=WINDOW_HLS_MUX_TIMEOUT_SEC,
            )
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            logger.debug(
                "window hls mux attempt %d session=%s: %s",
                attempt,
                session.session_id[:8],
                exc,
            )
            _clear_youtube_window_hls_cache(session)
            out_dir.mkdir(parents=True, exist_ok=True)
            if attempt == 0:
                _refresh_youtube_window_hls_urls(session, prefer_height=height)
                video_url = (
                    _variant_url_for_height(session, height) or session.entry_url
                )
                audio_url = session.preview_audio_url or audio_url
                mux_hdrs = _merge_youtube_session_cookies(
                    session.http_headers,
                    session.vod_url,
                )
                continue
            break
    if last_exc is not None:
        if isinstance(last_exc, StaleGooglevideoUrl) or "403" in str(last_exc):
            if _try_fallback_from_window_hls(session, prefer_height=height):
                return True
        raise last_exc
    _stamp_window_hls_mux_start(session, start)
    _register_youtube_window_hls_resources(session)
    return _window_hls_seg0_matches_bounds(session)


def schedule_youtube_window_hls_mux(session_id: str) -> None:
    """Idempotent — start (or join) the background window HLS mux thread."""
    with _WINDOW_HLS_LOCK:
        if session_id in _WINDOW_HLS_RUNNING:
            return
        _WINDOW_HLS_RUNNING.add(session_id)

    def _job() -> None:
        try:
            session = get_session(session_id)
            if not session or not session.dash_window_hls:
                return
            if _window_hls_seg0_matches_bounds(session):
                session.mux_status = "ready"
                return
            session.mux_status = "pending"
            session.mux_error = None
            try:
                ok = _ensure_youtube_window_hls_mux(session)
                session.mux_status = "ready" if ok else "pending"
                if not ok and not session.mux_error:
                    session.mux_error = "window HLS mux produced no seg0"
                if ok and _window_hls_seg0_matches_bounds(session):
                    now = time.monotonic()
                    if session.timing_seg0_mono <= 0:
                        session.timing_seg0_mono = now
                        from services.preview_timing import (
                            log_server_seg0_ready,
                            log_server_seek_seg0,
                        )

                        created = session.timing_created_mono or now
                        if (
                            session.timing_last_seek_mono > 0
                            and session.timing_last_seek_mono >= created
                        ):
                            log_server_seek_seg0(
                                session,
                                since_seek_ms=(now - session.timing_last_seek_mono)
                                * 1000.0,
                                position_sec=session.timing_last_seek_pos,
                            )
                        else:
                            log_server_seg0_ready(
                                session,
                                since_create_ms=(now - created) * 1000.0,
                            )
            except Exception as exc:
                if _try_fallback_from_window_hls(
                    session,
                    prefer_height=session.prefer_height or 720,
                ):
                    return
                session.mux_status = "error"
                session.mux_error = str(exc)[:240]
                logger.warning(
                    "window hls mux failed session=%s: %s",
                    session_id[:8],
                    exc,
                )
        finally:
            with _WINDOW_HLS_LOCK:
                _WINDOW_HLS_RUNNING.discard(session_id)

    threading.Thread(
        target=_job,
        daemon=True,
        name=f"yt-window-hls-{session_id[:8]}",
    ).start()


# ponytail: spec alias — _kick_window_hls_mux is the same idempotent kick as
# schedule_youtube_window_hls_mux (kept as separate name for grep-ability).
_kick_window_hls_mux = schedule_youtube_window_hls_mux


def _is_youtube_window_hls_resource(upstream: str) -> bool:
    """True when *upstream* is a window HLS marker URL (resource_map entry)."""
    return bool(upstream) and upstream.startswith(WINDOW_HLS_MARKER)


def _register_youtube_window_hls_resources(session: PreviewSession) -> None:
    """Register the window-playlist and window-seg-NNN resource IDs in session.resource_map.

    Called after the mux job finishes; safe to call repeatedly (overwrites are
    idempotent). Re-registration after a mux reset lets old segment URLs fall
    off the resource_map so stale client fetches 404.
    """
    # drop any previous window-HLS entries — prevents stale segments from
    # resolving after a mux refresh / height change.
    session.resource_map = {
        k: v
        for k, v in session.resource_map.items()
        if not k.startswith(WINDOW_HLS_MARKER)
        and k != WINDOW_HLS_PLAYLIST_RESOURCE
        and not k.startswith(WINDOW_HLS_SEGMENT_RESOURCE_PREFIX)
    }
    session.resource_map[WINDOW_HLS_PLAYLIST_RESOURCE] = f"{WINDOW_HLS_MARKER}playlist"
    if USE_FMP4:
        # fMP4 init segment — served via ?id=window-init
        session.resource_map[WINDOW_HLS_INIT_RESOURCE] = f"{WINDOW_HLS_MARKER}init.mp4"
    for seg in _window_hls_existing_segments(session):
        try:
            idx = int(seg.stem.split("_", 1)[1])
        except (IndexError, ValueError):
            continue
        rid = f"{WINDOW_HLS_SEGMENT_RESOURCE_PREFIX}{idx:03d}"
        ext = ".m4s" if USE_FMP4 else ".ts"
        session.resource_map[rid] = f"{WINDOW_HLS_MARKER}seg_{idx:03d}{ext}"


def _build_youtube_window_hls_media_playlist(session: PreviewSession) -> bytes:
    """Build the window HLS media playlist on-the-fly from disk-state.

    Scans ``seg_*.ts`` (or ``seg_*.m4s`` when fMP4) files in the window_hls dir,
    registers each segment resource ID on the session, and emits an HLS playlist
    whose segment URLs point to
    ``/api/preview/hls/{sid}/resource?id=window-seg-NNN``.

    When ``USE_FMP4`` is enabled, the playlist is emitted as an LL-HLS (v9)
    fMP4 playlist: an ``#EXT-X-MAP`` init reference, ``#EXT-X-PART-INF`` /
    ``#EXT-X-SERVER-CONTROL`` tags, and per-segment ``#EXT-X-PART`` +
    ``#EXT-X-PRELOAD-HINT`` hints pointing at ``seg_NNN_part_*.m4s`` chunks.
    """
    base = f"/api/preview/hls/{session.session_id}/resource?id="
    segs = _window_hls_existing_segments(session)
    target_duration = max(int(WINDOW_HLS_SEGMENT_SEC) + 1, 5)
    if not segs:
        # mux not started — return a minimal valid playlist pointing at
        # the playlist resource itself so HLS.js polls without 404s.
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:9" if USE_FMP4 else "#EXT-X-VERSION:6",
            f"#EXT-X-TARGETDURATION:{target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
        ]
        return "\n".join(lines).encode("utf-8") + b"\n"
    duration = max(1, int(WINDOW_HLS_SEGMENT_SEC))
    if USE_FMP4:
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:9",
            "#EXT-X-INDEPENDENT-SEGMENTS",
            f"#EXT-X-TARGETDURATION:{target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-PART-INF:PART-TARGET=0.5",
            "#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,CAN-SKIP-UNTIL=60,HOLD-BACK=2.0",
            f"#EXT-X-MAP:URI=\"{base}init.mp4\"",
        ]
    else:
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:6",
            "#EXT-X-INDEPENDENT-SEGMENTS",
            f"#EXT-X-TARGETDURATION:{target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
        ]
    for seg in segs:
        try:
            idx = int(seg.stem.split("_", 1)[1])
        except (IndexError, ValueError):
            continue
        rid = f"{WINDOW_HLS_SEGMENT_RESOURCE_PREFIX}{idx:03d}"
        if USE_FMP4:
            session.resource_map[rid] = f"{WINDOW_HLS_MARKER}seg_{idx:03d}.m4s"
            # LL-HLS partial-segment hints for the first two parts of this segment.
            lines.append(
                f'#EXT-X-PRELOAD-HINT:TYPE=PART,URI=\"seg_{idx:03d}_part_000.m4s\"'
            )
            lines.append(
                f'#EXT-X-PART:DURATION=0.5,URI=\"seg_{idx:03d}_part_001.m4s\",INDEPENDENT=YES'
            )
        else:
            session.resource_map[rid] = f"{WINDOW_HLS_MARKER}seg_{idx:03d}.ts"
        lines.append(f"#EXTINF:{duration:.3f},")
        lines.append(f"{base}{rid}")
    if _window_hls_playlist_complete(session):
        lines.append("#EXT-X-ENDLIST")
    return ("\n".join(lines) + "\n").encode("utf-8")


def open_youtube_window_hls_proxy(
    session_id: str,
    resource_id: str,
    range_header: Optional[str] = None,
) -> Tuple[Callable[[], object], str, dict, int, Callable[[], None]]:
    """Serve either the window-playlist or a window-seg-NNN resource."""
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")
    if not session.dash_window_hls:
        raise ValueError("Not a window HLS preview session")
    if resource_id == WINDOW_HLS_PLAYLIST_RESOURCE:
        # make sure mux is running even if client beats the warm thread to it
        schedule_youtube_window_hls_mux(session_id)
        _register_youtube_window_hls_resources(session)
        body = _build_youtube_window_hls_media_playlist(session)
        generator, ctype, hdrs, status, cleanup = _open_memory_bytes_proxy(
            body,
            "application/vnd.apple.mpegurl",
            range_header,
        )
        return generator, ctype, hdrs, status, cleanup
    # fMP4 init segment (init.mp4) — served either via the window-init resource
    # id or the literal "init.mp4" URI referenced by the playlist's EXT-X-MAP.
    if resource_id == WINDOW_HLS_INIT_RESOURCE or resource_id == "init.mp4":
        init_path = _window_hls_dir(session) / "init.mp4"
        if not init_path.is_file():
            schedule_youtube_window_hls_mux(session_id)
            raise PreviewMuxPending("window HLS init.mp4 not ready")
        session.resource_map[WINDOW_HLS_INIT_RESOURCE] = f"{WINDOW_HLS_MARKER}init.mp4"
        init_gen, _init_ctype, init_hdrs, init_status, init_cleanup = _open_local_file_proxy(
            init_path, range_header
        )
        # fMP4 init segment — advertise as mp4 (iso.segment also acceptable).
        return init_gen, "video/mp4", init_hdrs, init_status, init_cleanup
    # segment resource
    m = re.match(
        rf"^{re.escape(WINDOW_HLS_SEGMENT_RESOURCE_PREFIX)}(\d+)$", resource_id or ""
    )
    if not m:
        raise ValueError("Invalid window HLS segment resource id")
    idx = int(m.group(1))
    path = _window_hls_segment_path(session, idx)
    if not path.is_file():
        schedule_youtube_window_hls_mux(session_id)
        raise PreviewMuxPending(
            f"window HLS segment seg_{idx:03d}{'.m4s' if USE_FMP4 else '.ts'} not ready"
        )
    return _open_local_file_proxy(path, range_header)


# ponytail: legacy exports retained so existing imports + routers don't break.
proxy_window_hls_playlist = None  # set by shim below for backward compatibility


def _legacy_proxy_window_hls_playlist(session_id: str) -> Tuple[bytes, str, dict, int]:
    """Dispatch window.m3u8 requests to the new resource-based path."""
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")
    if not session.dash_window_hls:
        raise ValueError("Not a window HLS preview session")
    schedule_youtube_window_hls_mux(session_id)
    _register_youtube_window_hls_resources(session)
    body = _build_youtube_window_hls_media_playlist(session)
    return (
        body,
        "application/vnd.apple.mpegurl",
        {"Cache-Control": "no-cache"},
        200,
    )


proxy_window_hls_playlist = _legacy_proxy_window_hls_playlist


def open_window_hls_segment_proxy(
    session_id: str,
    segment_name: str,
    range_header: Optional[str] = None,
) -> Tuple[Callable[[], object], str, dict, int, Callable[[], None]]:
    """ponytail: legacy alias — prefer ``open_youtube_window_hls_proxy`` via /resource."""
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")
    if not session.dash_window_hls:
        raise ValueError("Not a window HLS preview session")
    m = re.match(r"^seg[_]?(\d{3,4})\.ts$", segment_name or "")
    if not m:
        raise ValueError("Invalid window HLS segment name")
    idx = int(m.group(1))
    path = _window_hls_segment_path(session, idx)
    if not path.is_file():
        raise PreviewMuxPending(f"window HLS segment {segment_name} not ready")
    return _open_local_file_proxy(path, range_header)


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

def _put_resolved_stream_cache(key: str, value: Tuple) -> None:
    with _RESOLVED_STREAM_LOCK:
        if len(_RESOLVED_STREAM_CACHE) >= _RESOLVED_STREAM_MAX:
            oldest = min(_RESOLVED_STREAM_CACHE.items(), key=lambda item: item[1][0])
            _RESOLVED_STREAM_CACHE.pop(oldest[0], None)
        _RESOLVED_STREAM_CACHE[key] = (time.time(), value)


def _build_youtube_session_snapshot(
    url: str,
    crop_start: float,
    crop_end: float,
    prefer_height: int,
    oauth: Optional[str],
    resolve_result: Tuple,
) -> Optional[Tuple[str, int, dict]]:
    """ponytail: prebuild a session snapshot during the warm.

    Runs the same YouTube session-construction work that ``create_session``
    performs after ``resolve_stream_info``, but writes the result into a
    plain dict instead of a live ``PreviewSession``. Returns ``(vid, height,
    snapshot)`` so the caller can stash it in ``_SESSION_SNAPSHOT``.

    Returns ``None`` when the resolve result is unusable (e.g. Twitch clip
    that took a different code path — caller's job to detect via platform).
    """
    raw_entry, headers, platform, variant_formats, kind, yt_info = resolve_result
    if platform != "YouTube":
        return None
    from services.youtube_innertube import extract_video_id

    vid = extract_video_id(url or "")
    if not vid:
        return None

    session_id = secrets.token_hex(8)
    cache_dir = _PREVIEW_ROOT / session_id
    cache_dir.mkdir(parents=True, exist_ok=True)

    preview_audio_url: Optional[str] = None
    variant_muxed: Dict[int, bool] = {}
    if yt_info:
        preview_audio_url = _resolve_youtube_preview_audio(yt_info)
    for fmt in variant_formats or []:
        h = int(fmt.get("height") or 0)
        if h > 0:
            variant_muxed[h] = fmt.get("acodec") not in ("none", None)

    proxy_master_url: Optional[str] = None
    if kind == "progressive":
        proxy_master_url = f"/api/preview/hls/{session_id}/master.m3u8"

    # Build a temporary PreviewSession so the existing helpers (which mutate
    # ``session.*``) can run unchanged. We pull the populated fields back out
    # into a dict at the end.
    tmp = PreviewSession(
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
        prefer_height=prefer_height,
    )
    _clamp_session_crop_to_vod_duration(tmp, yt_info)
    if preview_audio_url:
        tmp.allowed_hosts.update(_hosts_for_url(preview_audio_url))

    if variant_formats:
        tmp.variant_entries = [
            (int(fmt.get("height") or 0), fmt.get("url") or "")
            for fmt in variant_formats
            if int(fmt.get("height") or 0) > 0 and fmt.get("url")
        ]
        if kind == "hls":
            from services.ytdlp_hls import preview_fast_only_mode

            if platform == "YouTube":
                _apply_youtube_custom_master(tmp, variant_formats, yt_info)
            elif len(tmp.variant_entries) >= 2:
                tmp.custom_master = _build_synthetic_master_playlist(
                    tmp, variant_formats
                )
            for _height, upstream in tmp.variant_entries:
                tmp.allowed_hosts.update(_hosts_for_url(upstream))
            if tmp.dash_window_hls and not preview_fast_only_mode():
                muxed = _youtube_muxed_progressive_for_long_explore(
                    url, oauth, prefer_height, yt_info=yt_info
                )
                if muxed:
                    prog_url, prog_formats, prog_info = muxed
                    _apply_muxed_progressive_session(
                        tmp, prog_url, prog_formats, prog_info, prefer_height
                    )
                    kind = "progressive"
                    variant_formats = prog_formats
                    yt_info = prog_info
                    proxy_master_url = tmp.master_url
    if kind == "progressive":
        tmp.allowed_hosts.update(_hosts_for_url(tmp.entry_url))
    elif tmp.custom_master:
        if tmp.variant_entries:
            tmp.entry_url = (
                _pick_variant_by_height(tmp.variant_entries, prefer_height)
                or tmp.variant_entries[0][1]
            )
        tmp.allowed_hosts.update(_hosts_for_url(tmp.entry_url))
    else:
        tmp.entry_url = _resolve_preview_entry(tmp, raw_entry, prefer_height)
        tmp.allowed_hosts.update(_hosts_for_url(tmp.entry_url))

    if variant_formats:
        _stash_youtube_preview_formats(
            tmp, variant_formats, yt_info, prefer_height, tmp.entry_url
        )
    if tmp.dash_window_hls:
        _init_window_hls_mux_bounds(tmp)

    snapshot = {
        "session_id": session_id,
        "cache_dir": str(cache_dir),
        "master_url": tmp.master_url,
        "entry_url": tmp.entry_url,
        "platform": platform,
        "http_headers": dict(tmp.http_headers),
        "allowed_hosts": set(tmp.allowed_hosts),
        "kind": kind,
        "preview_audio_url": tmp.preview_audio_url,
        "variant_muxed": dict(tmp.variant_muxed),
        "variant_entries": list(tmp.variant_entries),
        "custom_master": tmp.custom_master,
        "dash_window_hls": tmp.dash_window_hls,
        "preview_audio_fmt": tmp.preview_audio_fmt,
        "preview_video_fmt": tmp.preview_video_fmt,
        "explore_yt_info": yt_info,
        "vod_duration": float(tmp.vod_duration or 0.0),
        "cached_progressive_path": tmp.cached_progressive_path,
        "mux_status": "pending",
    }
    return vid, int(prefer_height or 0), snapshot

def _get_resolved_stream_cached(key: str) -> Optional[Tuple]:
    now = time.time()
    with _RESOLVED_STREAM_LOCK:
        hit = _RESOLVED_STREAM_CACHE.get(key)
        if hit and (now - hit[0]) < _RESOLVED_STREAM_TTL_SEC:
            return hit[1]
        if hit:
            _RESOLVED_STREAM_CACHE.pop(key, None)
    return None


def _put_resolved_stream_cache(key: str, value: Tuple) -> None:
    with _RESOLVED_STREAM_LOCK:
        if len(_RESOLVED_STREAM_CACHE) >= _RESOLVED_STREAM_MAX:
            oldest = min(_RESOLVED_STREAM_CACHE.items(), key=lambda item: item[1][0])
            _RESOLVED_STREAM_CACHE.pop(oldest[0], None)
        _RESOLVED_STREAM_CACHE[key] = (time.time(), value)


def _get_session_snapshot(
    vid: str, height: int
) -> Optional[dict]:
    """Return prebuilt session fields for (vid, height) or None on miss/expire."""
    if not vid:
        return None
    key = (vid, int(height or 0))
    now = time.time()
    with _SESSION_SNAPSHOT_LOCK:
        hit = _SESSION_SNAPSHOT.get(key)
        if hit and (now - hit[0]) < _SESSION_SNAPSHOT_TTL_SEC:
            return hit[1]
        if hit:
            _SESSION_SNAPSHOT.pop(key, None)
    return None


def _put_session_snapshot(vid: str, height: int, snapshot: dict) -> None:
    """Stash session fields for click-time reuse."""
    if not vid:
        return
    key = (vid, int(height or 0))
    with _SESSION_SNAPSHOT_LOCK:
        if len(_SESSION_SNAPSHOT) >= _SESSION_SNAPSHOT_MAX:
            oldest = min(_SESSION_SNAPSHOT.items(), key=lambda item: item[1][0])
            _SESSION_SNAPSHOT.pop(oldest[0], None)
        _SESSION_SNAPSHOT[key] = (time.time(), snapshot)


def invalidate_session_snapshot(vid: str, height: Optional[int] = None) -> None:
    """Drop snapshot(s) for *vid* — used when refresh forces a fresh resolve."""
    with _SESSION_SNAPSHOT_LOCK:
        if height is None:
            for k in list(_SESSION_SNAPSHOT.keys()):
                if k[0] == vid:
                    _SESSION_SNAPSHOT.pop(k, None)
        else:
            _SESSION_SNAPSHOT.pop((vid, int(height)), None)


def invalidate_resolved_stream_cache(
    url: str,
    prefer_height: int = 720,
) -> None:
    """Drop cached resolve for *url* — refresh must not recycle expired googlevideo URLs."""
    from services.youtube_innertube import extract_video_id

    vid = extract_video_id((url or "").strip())
    if not vid:
        return
    key = f"{vid}:{prefer_height}:v2"
    with _RESOLVED_STREAM_LOCK:
        _RESOLVED_STREAM_CACHE.pop(key, None)


def _invalidate_youtube_resolve_caches(
    url: str,
    prefer_height: int = 720,
) -> None:
    from services.ytdlp_hls import invalidate_youtube_extract_cache

    invalidate_youtube_extract_cache(url)
    invalidate_resolved_stream_cache(url, prefer_height)


def _youtube_warm_inflight_key(url: str) -> str:
    """Dedup paste warm + create_session await on video id, not shorts vs watch URL."""
    from services.youtube_innertube import extract_video_id

    raw = (url or "").strip()
    return extract_video_id(raw) or raw


def kickoff_youtube_warm(
    url: str,
    oauth: Optional[str] = None,
    cookies_file: Optional[str] = None,
    prefer_height: int = 360,
    *,
    force: bool = False,
) -> None:
    """Fire-and-forget warm on URL paste — deduped per canonical URL.

    ``prefer_height`` is forwarded so the warmed resolved-stream cache is keyed
    by the same ``{vid}:{prefer_height}:v2`` key that ``create_session`` will
    later read. It defaults to 360 (the YouTube fast-start height that
    ``create_session`` uses for progressive previews) so a plain hover warm
    actually lands in the cache the preview open will read. The full-mux warm
    path passes its own (typically 720) height explicitly.

    ``force=True`` skips the "active preview" bail check. Use for batch
    warm on startup so preloading doesn't get cancelled the moment the
    user clicks their first video.
    """
    from services.ytdlp_hls import preview_fast_only_mode

    if preview_fast_only_mode():
        return
    key = _youtube_warm_inflight_key(url)
    if not key:
        return

    def _run() -> None:
        # ponytail: register in-flight only once the job actually starts — a
        # queued-but-not-running warm must not make create_session wait for it.
        with _YOUTUBE_WARM_LOCK:
            if key in _YOUTUBE_WARM_INFLIGHT:
                return
            done = threading.Event()
            _YOUTUBE_WARM_INFLIGHT[key] = done
        try:
            if not force:
                # Bail out cheaply if the user has moved on to a different VOD.
                # This keeps stale warm jobs from hogging INFO_EXECUTOR workers.
                with _ACTIVE_YOUTUBE_PREVIEW_LOCK:
                    active = _ACTIVE_YOUTUBE_PREVIEW_KEY
                if active is not None and active != key:
                    logger.debug("YouTube warm bailing — active preview is %s", active[:80])
                    return
            logger.info("YouTube gesture warm start: %s", key[:24])
            # ponytail: warm_youtube_extract -> warm_youtube_preview_resolve
            # now builds + stashes the session snapshot itself.
            from services.ytdlp_hls import warm_youtube_extract

            warm_youtube_extract(
                url, oauth=oauth, cookies_file=cookies_file, prefer_height=prefer_height
            )
        finally:
            # ponytail: failed warm on slow VOD must not nuke session before create_session runs
            with _YOUTUBE_WARM_LOCK:
                ev = _YOUTUBE_WARM_INFLIGHT.pop(key, None)
            if ev is not None:
                ev.set()

    from deps import GESTURE_WARM_EXECUTOR

    GESTURE_WARM_EXECUTOR.submit(_run)


def kickoff_youtube_full_mux_warm(
    url: str,
    oauth: Optional[str] = None,
    cookies_file: Optional[str] = None,
    prefer_height: int = 720,
) -> None:
    """ponytail: hover-triggered full-VOD mux. Resolves the URL's variants and
    muxes the full VOD to the persistent cache in a daemon thread. If the user
    opens the preview before the mux finishes, the session falls back to
    window-HLS and the cache lands for next time.

    Respects the active-preview marker so we don't steal workers when the user
    has already opened a different VOD.
    """
    from services.youtube_innertube import extract_video_id

    vid = extract_video_id(url or "")
    if not vid:
        return
    cache_path = _full_mux_cache_path(vid, prefer_height, 0.0, 0.0)
    if cache_path.is_file() and cache_path.stat().st_size >= MIN_VALID_OUTPUT_BYTES:
        return
    key = _youtube_warm_inflight_key(url)
    if not key:
        return

    def _run() -> None:
        logger.info("YouTube full-mux warm start: %s h=%d", vid, prefer_height)
        with _ACTIVE_YOUTUBE_PREVIEW_LOCK:
            active = _ACTIVE_YOUTUBE_PREVIEW_KEY
        # ponytail: full-mux is a background task — let it run even when the user
        # has another preview open. It must not race the click's session create for
        # the same URL (they'd both download the same upstream) so we still bail
        # when the active marker equals this key. Different URLs don't compete.
        if active is not None and active == key:
            logger.info("full-mux warm bailing — user already previewing %s", key[:24])
            return
        try:
            from services.ytdlp_hls import warm_youtube_extract

            if not warm_youtube_extract(url, oauth=oauth, cookies_file=cookies_file):
                return
            try:
                _entry, headers, _platform, variant_formats, _kind, yt_info = (
                    resolve_stream_info(url, oauth=oauth, prefer_height=prefer_height)
                )
            except Exception as exc:
                logger.warning("full-mux warm resolve failed for %s: %s", url[:80], exc, exc_info=True)
                return
            audio_url = _resolve_youtube_preview_audio(yt_info) if yt_info else None
            vod_dur = 0.0
            if yt_info:
                try:
                    vod_dur = float(yt_info.get("duration") or 0)
                except (TypeError, ValueError):
                    vod_dur = 0.0
            logger.info("full-mux warm resolved %s h=%d dur=%.1fs audio=%s", vid, prefer_height, vod_dur, bool(audio_url))
            variant_url = None
            for f in variant_formats or []:
                if int(f.get("height") or 0) == prefer_height and f.get("url"):
                    variant_url = f["url"]
                    break
            if not variant_url:
                logger.info("full-mux warm no variant for h=%d (have %s)", prefer_height, [int(f.get("height") or 0) for f in (variant_formats or [])])
                return
            logger.info("full-mux warm starting MuxJob %s -> %s", variant_url[:80], cache_path)
            MuxJob(
                video_url=variant_url,
                audio_url=audio_url,
                output_path=cache_path,
                start_sec=0.0,
                end_sec=max(0.5, vod_dur),
                headers=headers or {},
                prefer_height=prefer_height,
                vod_url=url,
                job_kind="full",
                vod_duration=vod_dur,
            ).run()
        except Exception as exc:
            logger.warning("full-mux warm failed for %s: %s", url[:80], exc, exc_info=True)

    threading.Thread(target=_run, daemon=True, name=f"yt-hover-mux-{vid}").start()


def set_active_youtube_preview(url: Optional[str]) -> None:
    """Mark which URL the user is actively previewing. Warm jobs for other URLs
    will skip their yt-dlp probe when they wake up."""
    global _ACTIVE_YOUTUBE_PREVIEW_KEY
    try:
        key = _youtube_warm_inflight_key(url) if url else None
        with _ACTIVE_YOUTUBE_PREVIEW_LOCK:
            _ACTIVE_YOUTUBE_PREVIEW_KEY = key
    except Exception:
        pass


def await_youtube_warm_if_pending(url: str, timeout_sec: float = 1.0) -> None:
    """Briefly reuse paste warm; never let warm make preview feel stuck."""
    key = _youtube_warm_inflight_key(url)
    if not key:
        return
    with _YOUTUBE_WARM_LOCK:
        ev = _YOUTUBE_WARM_INFLIGHT.get(key)
    if ev is not None and not ev.wait(timeout_sec):
        logger.debug("YouTube warm wait timed out for %s", key[:80])


def warm_youtube_preview_resolve(
    url: str,
    oauth: Optional[str] = None,
    cookies_file: Optional[str] = None,
    prefer_height: int = 720,
) -> bool:
    """Populate resolved-stream cache on hover — same path as create_session.

    Uses the full extract race (InnerTube + yt-dlp fallback): gesture warms are
    few (hover/scroll-visible rows) and must survive InnerTube bot-gating that
    kills the light-only pass. The bulk startup storm uses warm_youtube_resolve_only.

    Also prebuilds the session snapshot so the click path skips the ~5s
    extract + variant-build work on a warm hit.
    """
    try:
        resolve_result = resolve_stream_info(
            url, oauth=oauth, prefer_height=prefer_height
        )
        kickoff_youtube_preflight_mux(url, oauth=oauth, prefer_height=prefer_height)
        kickoff_youtube_prog_head_warm(url, oauth=oauth, prefer_height=prefer_height)
    except Exception as exc:
        logger.info("YouTube warm resolve skipped for %s: %s", url[:80], exc)
        return False
    try:
        snap = _build_youtube_session_snapshot(
            url, 0.0, 0.0, prefer_height, oauth, resolve_result
        )
        if snap:
            _put_session_snapshot(snap[0], snap[1], snap[2])
            logger.info(
                "YouTube session snapshot ready: %s h=%d sid=%s",
                snap[0][:11], snap[1], snap[2]["session_id"][:8],
            )
    except Exception as exc:
        logger.warning(
            "session snapshot failed for %s: %s", url[:80], exc, exc_info=True
        )
    return True


def warm_youtube_resolve_only(
    url: str,
    oauth: Optional[str] = None,
    cookies_file: Optional[str] = None,
    prefer_height: int = 720,
) -> bool:
    """Populate resolved-stream cache + preflight the head so the click can play
    immediately. Uses the light extract so concurrent warm jobs don't fight for
    yt-dlp locks during a startup storm. The full InnerTube race path runs on
    the user's first preview click.

    Also prebuilds the session snapshot so the click path skips the ~5s
    extract + variant-build work on a warm hit. Every warm path that lands
    a resolve must produce a snapshot — otherwise the user's first click
    waits the full SLA.
    """
    try:
        resolve_result = resolve_stream_info(
            url, oauth=oauth, prefer_height=prefer_height, warm_light=True
        )
    except Exception as exc:
        logger.debug("YouTube batch warm resolve skipped for %s: %s", url[:80], exc)
        return False
    # ponytail: preflight the head + mux so the first 5s of playable bytes are
    # on disk before the user clicks. 360p muxed progressive is unauthenticated
    # and survives without cookies/POT/visitor_data.
    try:
        kickoff_youtube_prog_head_warm(url, oauth=oauth, prefer_height=prefer_height)
    except Exception as exc:
        logger.debug("YouTube batch warm head skipped for %s: %s", url[:80], exc)
    # ponytail: build the session shell so the click fast-path applies. Cached
    # resolve + build once during warm = sub-50ms click instead of ~5s.
    try:
        snap = _build_youtube_session_snapshot(
            url, 0.0, 0.0, prefer_height, oauth, resolve_result
        )
        if snap:
            _put_session_snapshot(snap[0], snap[1], snap[2])
            logger.info(
                "YouTube session snapshot ready: %s h=%d sid=%s",
                snap[0][:11], snap[1], snap[2]["session_id"][:8],
            )
    except Exception as exc:
        logger.warning(
            "session snapshot failed for %s: %s", url[:80], exc, exc_info=True
        )
    return True


def resolve_stream_info(
    url: str,
    oauth: Optional[str] = None,
    prefer_height: int = 720,
    *,
    force_fresh: bool = False,
    warm_light: bool = False,
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
    if platform == "YouTube":
        from services.youtube_innertube import canonical_youtube_watch_url

        full_url = canonical_youtube_watch_url(full_url) or full_url

    # Twitch clips — fast signed GQL path with yt-dlp fallback.
    # GQL's sourceURLs need a sig/token query string; VideoAccessToken_Clip
    # returns both, so we can avoid the slow yt-dlp extract in the hot path.
    if platform == "Twitch" and is_clip_url(url):
        variants: List[dict] = []
        headers: dict = dict(_DEFAULT_TWITCH_HEADERS)

        from services.twitch_gql_service import (
            _extract_clip_slug,
            get_clip_signed_variants_sync,
        )

        slug = _extract_clip_slug(url)
        cache_key = f"twclip:{slug}:{prefer_height}" if slug else None
        if cache_key and not force_fresh:
            cached = _get_resolved_stream_cached(cache_key)
            if cached is not None:
                return cached

        try:
            variants = get_clip_signed_variants_sync(url)
        except Exception as exc:
            logger.debug("Twitch clip signed GQL resolve failed: %s", exc)

        # yt-dlp fallback — slower, may provide working URLs if GQL/auth fails.
        if not variants:
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
            except Exception as exc:
                logger.debug("Twitch clip yt-dlp fallback failed: %s", exc)

        if not variants:
            raise RuntimeError("Twitch clip has no progressive formats")
        chosen_url = _pick_variant_by_height(
            [(int(v.get("height") or 0), v.get("url") or "") for v in variants],
            prefer_height=prefer_height,
        )
        if not chosen_url:
            raise RuntimeError("Twitch clip has no progressive URL")
        result = (chosen_url, headers, platform, variants, "progressive", None)
        if cache_key and not force_fresh:
            _put_resolved_stream_cache(cache_key, result)
        return result

    if platform == "YouTube":
        from services.youtube_innertube import _dedupe_youtube_formats, extract_video_id
        from services.youtube_diag import log_preview_resolve

        cache_key: Optional[str] = None
        vid = extract_video_id(full_url)
        if vid:
            cache_key = f"{vid}:{prefer_height}:v2"
            if force_fresh:
                _invalidate_youtube_resolve_caches(full_url, prefer_height)
            else:
                cached = _get_resolved_stream_cached(cache_key)
                if cached is not None:
                    return cached

        info = (
            _reextract_youtube_for_preview(full_url, oauth)
            if force_fresh
            else _extract_youtube_preview_info(full_url, oauth, warm_light=warm_light)
        )
        _resolve_youtube_preview_audio(info)
        if _youtube_info_is_dash_only_progressive(info):
            try:
                alt = _reextract_youtube_for_preview(full_url, oauth)
                if alt and not _youtube_info_is_dash_only_progressive(alt):
                    info = alt
            except Exception as exc:
                logger.debug("youtube dash-only re-extract: %s", exc)
        headers = _merge_youtube_session_cookies(
            info.get("http_headers")
            or {
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
            heights = [
                int(f.get("height") or 0)
                for f in variants
                if int(f.get("height") or 0) > 0
            ]
            log_preview_resolve(
                platform,
                kind,
                heights,
                custom_master=custom_master,
                entry_url=entry,
            )
            result = (entry, headers, platform, variants, kind, info)
            if cache_key and not force_fresh:
                _put_resolved_stream_cache(cache_key, result)
            return result

        # MP4 (muxed progressive) → muxed HLS → DASH window HLS (ffmpeg last resort).
        muxed_hls = [f for f in hls_variants if _is_muxed_format(f)]
        muxed_with_height = [f for f in muxed_hls if int(f.get("height") or 0) > 0]
        muxed_progressive = _deduped_progressive_variants({"formats": merged})
        # ponytail: use any available muxed progressive tier for instant preview.
        # For DASH-only VODs the lone 360p muxed tier is fast but leaves the quality
        # menu with only one option; fall through to window-HLS so all adaptive
        # video heights are exposed. Higher tiers are still reachable later via
        # POST /quality on genuinely muxed sources.
        if muxed_progressive and not _youtube_is_dash_separate_audio(merged, info):
            prog_urls = [
                (int(v.get("height") or 0), v.get("url") or "")
                for v in muxed_progressive
            ]
            prog_url = _pick_variant_by_height(prog_urls, prefer_height=prefer_height)
            if not prog_url:
                prog_url = muxed_progressive[0].get("url") or ""
            if prog_url:
                picked = next(
                    (f for f in muxed_progressive if f.get("url") == prog_url),
                    None,
                )
                if picked and picked.get("http_headers"):
                    headers = {**headers, **picked["http_headers"]}
                return _yt_resolve("progressive", prog_url, muxed_progressive)

        if muxed_with_height:
            for fmt in muxed_hls:
                stream_url = fmt.get("url") or ""
                if stream_url and _url_looks_like_master(stream_url):
                    if fmt.get("http_headers"):
                        headers = {**headers, **fmt["http_headers"]}
                    return _yt_resolve("hls", stream_url, muxed_with_height)
            chosen_url = _pick_variant_by_height(
                [
                    (int(v.get("height") or 0), v.get("url") or "")
                    for v in muxed_with_height
                ],
                prefer_height=prefer_height,
            )
            if chosen_url:
                picked = next(
                    (f for f in muxed_with_height if f.get("url") == chosen_url), None
                )
                if picked and picked.get("http_headers"):
                    headers = {**headers, **picked["http_headers"]}
                return _yt_resolve(
                    "hls",
                    chosen_url,
                    muxed_with_height,
                    custom_master=len(muxed_with_height) >= 2,
                )

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
                return _yt_resolve(
                    "hls", chosen_url, with_height, custom_master=len(with_height) >= 2
                )
        video_heights = [f for f in merged if int(f.get("height") or 0) > 0]
        if video_heights:
            chosen_url = _pick_variant_by_height(
                [
                    (int(v.get("height") or 0), v.get("url") or "")
                    for v in video_heights
                ],
                prefer_height=prefer_height,
            )
            if (
                chosen_url
                and _formats_are_dash_https(video_heights)
                and _youtube_needs_dash_window_hls(
                    merged,
                    info,
                )
            ):
                return _yt_resolve("hls", chosen_url, video_heights, custom_master=True)
        if len(video_heights) >= 2:
            chosen_url = _pick_variant_by_height(
                [
                    (int(v.get("height") or 0), v.get("url") or "")
                    for v in video_heights
                ],
                prefer_height=prefer_height,
            )
            if chosen_url:
                # DASH video-only tiers: window HLS mux (entire crop window to local HLS).
                if _formats_are_dash_https(
                    video_heights
                ) and _youtube_needs_dash_window_hls(
                    merged,
                    info,
                ):
                    return _yt_resolve(
                        "hls", chosen_url, video_heights, custom_master=True
                    )
                if _formats_are_dash_https(video_heights):
                    _resolve_youtube_preview_audio(info)
                    if not info.get("_preview_audio_format"):
                        try:
                            alt = _reextract_youtube_for_preview(full_url, oauth)
                            if alt:
                                info = alt
                                merged = _dedupe_youtube_formats(
                                    info.get("formats") or []
                                )
                                video_heights = [
                                    f for f in merged if int(f.get("height") or 0) > 0
                                ]
                                _resolve_youtube_preview_audio(info)
                                if _youtube_needs_dash_window_hls(merged, info):
                                    chosen_url = (
                                        _pick_variant_by_height(
                                            [
                                                (
                                                    int(v.get("height") or 0),
                                                    v.get("url") or "",
                                                )
                                                for v in video_heights
                                            ],
                                            prefer_height=prefer_height,
                                        )
                                        or chosen_url
                                    )
                                    return _yt_resolve(
                                        "hls",
                                        chosen_url,
                                        video_heights,
                                        custom_master=True,
                                    )
                        except Exception as exc:
                            logger.debug("youtube dash audio re-extract: %s", exc)
                    # ponytail: never return DASH video-only as progressive — window HLS only path
                    if _youtube_needs_dash_window_hls(merged, info):
                        return _yt_resolve(
                            "hls", chosen_url, video_heights, custom_master=True
                        )
                    return _yt_resolve("progressive", chosen_url, video_heights)
                return _yt_resolve("hls", chosen_url, video_heights, custom_master=True)
        progressive = _deduped_progressive_variants({"formats": merged})
        if progressive:
            heights_urls = [
                (int(v.get("height") or 0), v.get("url") or "") for v in progressive
            ]
            chosen_url = _pick_variant_by_height(
                heights_urls, prefer_height=prefer_height
            )
            if not chosen_url and progressive:
                chosen_url = progressive[0].get("url") or ""
            if chosen_url:
                picked = next(
                    (f for f in progressive if f.get("url") == chosen_url), None
                )
                if picked and picked.get("http_headers"):
                    headers = {**headers, **picked["http_headers"]}
                if _formats_are_dash_https(
                    progressive
                ) and _youtube_needs_dash_window_hls(
                    merged,
                    info,
                ):
                    return _yt_resolve(
                        "hls", chosen_url, progressive, custom_master=True
                    )
                return _yt_resolve("progressive", chosen_url, progressive)
        from services.youtube_diag import format_summary, log_extract_fail
        from services.youtube_innertube import extract_video_id

        log_extract_fail(
            extract_video_id(full_url) or "?",
            "preview_no_playable_url",
            detail=format_summary(info),
        )
        raise RuntimeError("YouTube video has no playable stream URL")

    # Twitch VODs — fast GQL + Usher path; fall back to yt-dlp if auth/geo fails.
    if platform == "Twitch" and not is_clip_url(url):
        try:
            from services.twitch_gql_service import get_vod_playback_sync

            master_url, vod_headers, vod_variants = get_vod_playback_sync(url)
            with_height = [v for v in vod_variants if int(v.get("height") or 0) > 0]
            if master_url:
                return (
                    master_url,
                    vod_headers,
                    platform,
                    with_height or vod_variants,
                    "hls",
                    None,
                )
        except Exception as exc:
            logger.debug(
                "Twitch VOD GQL resolve failed, falling back to yt-dlp: %s", exc
            )

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


def _pick_preview_variant(
    master_text: str, master_url: str, prefer_height: int = 720
) -> Optional[str]:
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


def _resolve_preview_entry(
    session: PreviewSession, entry_url: str, prefer_height: int = 720
) -> str:
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


def _rebuild_synthetic_resource_map(session: PreviewSession) -> None:
    """Re-register googlevideo URLs when resource_map was cleared but master text remains."""
    if not session.custom_master:
        return
    for _height, url in session.variant_entries:
        if url:
            _resource_id(session, url)
    audio = session.preview_audio_url or ""
    if audio:
        _resource_id(session, audio)


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


def _rewrite_playlist_line(
    line: str,
    session: PreviewSession,
    playlist_url: str,
    base: str,
) -> str:
    stripped = line.strip()
    if not stripped:
        return line
    if stripped.startswith("#"):
        if 'URI="' in stripped:

            def _sub(m: re.Match) -> str:
                abs_url = urljoin(playlist_url, m.group(1))
                return f'URI="{_proxy_url(session, abs_url)}"'

            return _URI_IN_TAG.sub(_sub, line)
        return line
    abs_url = urljoin(base, stripped)
    return _proxy_url(session, abs_url)


def _rewrite_playlist(content: str, session: PreviewSession, playlist_url: str) -> str:
    base = playlist_url.rsplit("/", 1)[0] + "/"
    out = [
        _rewrite_playlist_line(line, session, playlist_url, base)
        for line in content.splitlines()
    ]
    return "\n".join(out) + "\n"


def _fetch_and_rewrite_playlist_streaming(
    session: PreviewSession,
    upstream_url: str,
) -> Tuple[bytes, int]:
    """Stream upstream m3u8 and rewrite line-by-line (long VOD playlists exceed 512KB)."""
    base = upstream_url.rsplit("/", 1)[0] + "/"
    resp = _open_upstream_stream(session, upstream_url)
    out = bytearray()
    status = resp.status_code
    pending = ""
    try:
        for chunk in resp.iter_content(chunk_size=_UPSTREAM_CHUNK_BYTES):
            if not chunk:
                continue
            pending += chunk.decode("utf-8", errors="replace")
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                rewritten = _rewrite_playlist_line(line, session, upstream_url, base)
                row = (rewritten + "\n").encode("utf-8")
                if len(out) + len(row) > _MAX_REWRITTEN_PLAYLIST_BYTES:
                    raise RuntimeError(
                        f"Rewritten playlist exceeds {_MAX_REWRITTEN_PLAYLIST_BYTES} byte cap",
                    )
                out.extend(row)
        if pending:
            rewritten = _rewrite_playlist_line(pending, session, upstream_url, base)
            row = (rewritten + "\n").encode("utf-8")
            if len(out) + len(row) > _MAX_REWRITTEN_PLAYLIST_BYTES:
                raise RuntimeError(
                    f"Rewritten playlist exceeds {_MAX_REWRITTEN_PLAYLIST_BYTES} byte cap",
                )
            out.extend(row)
    finally:
        try:
            resp.close()
        except OSError:
            pass
    if not out:
        raise RuntimeError("Upstream playlist is empty")
    session.touch()
    return bytes(out), status


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
        if session.dash_window_hls:
            schedule_youtube_window_hls_mux(session_id)
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
    if session.prefer_height == prefer_height:
        return session
    session.prefer_height = prefer_height
    if session.platform == "YouTube":
        if session.dash_window_hls:
            _refresh_youtube_window_hls_urls(session, prefer_height=prefer_height)
            _clear_youtube_mux_cache(session)
            _clear_youtube_window_hls_cache(session)
            session.resource_map = {
                k: v
                for k, v in session.resource_map.items()
                if not k.startswith(WINDOW_HLS_MARKER)
            }
            session.custom_master = _build_youtube_window_hls_master(session)
            schedule_youtube_window_hls_mux(session_id)
        else:
            _refresh_youtube_preview_urls(session, prefer_height=prefer_height)
            if session.variant_entries:
                picked = _pick_variant_by_height(session.variant_entries, prefer_height)
                if picked:
                    session.entry_url = picked
                    session.allowed_hosts.update(_hosts_for_url(picked))
        session.touch()
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
    """Resolve a preview resource URL — only IDs registered in session.resource_map.

    For window HLS resource IDs (``window-playlist``, ``window-seg-NNN``) we
    lazily re-register before lookup so hot-mux or refresh paths don't 404 on
    stale IDs.
    """
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")
    if not resource_id:
        raise ValueError("Missing preview resource id")
    if session.dash_window_hls and (
        resource_id == WINDOW_HLS_PLAYLIST_RESOURCE
        or resource_id.startswith(WINDOW_HLS_SEGMENT_RESOURCE_PREFIX)
    ):
        _register_youtube_window_hls_resources(session)
    upstream = session.resource_map.get(resource_id)
    if not upstream and session.custom_master:
        _rebuild_synthetic_resource_map(session)
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


# Module-level singleton — existing importers use ``from services.preview_service import create_session``.
# ponytail: module-level singleton — one manager per process; safe because the app runs a single backend server.
_manager = PreviewManager()
_cleanup_stale_sessions = _manager._cleanup_stale_sessions
delete_session = _manager.delete_session
get_session = _manager.get_session
create_session = _manager.create_session

assert _formats_are_dash_https([{"protocol": "https", "url": "https://x/v.mp4"}])
assert not _formats_are_dash_https(
    [{"protocol": "m3u8_native", "url": "https://x/m.m3u8"}]
)
# ponytail: self-check — YouTube segments must not be classified as playlists
assert not _is_playlist_url(
    "https://rr1---sn.example.googlevideo.com/videoplayback/id/x/itag/231"
    "/source/youtube/playlist/index.m3u8/seg.ts"
)
assert _is_playlist_url(
    "https://manifest.googlevideo.com/api/manifest/hls_playlist/expire/1/master.m3u8"
)
assert _clamp_range_header("bytes=8388608-8454143", 3697756) == "bytes=3435612-3697755"
assert preview_mux_ready(
    PreviewSession(
        session_id="x",
        vod_url="",
        master_url="",
        entry_url="",
        platform="Kick",
    )
)
_window_pl_sess = PreviewSession(
    session_id="x",
    vod_url="",
    master_url="",
    entry_url="",
    platform="YouTube",
    cache_dir=Path("/tmp"),
    crop_start=0,
    crop_end=60,
    dash_window_hls=True,
    custom_master="#EXTM3U\n",
)
assert not preview_playlist_ready(_window_pl_sess)
# mux_ready requires on-disk seg0; custom_master alone does not flip mux_ready
assert not preview_mux_ready(_window_pl_sess)
assert not preview_segment_buffer_ready(_window_pl_sess)
assert _bytes_response_for_range(b"abcdef", "bytes=1-3")[2] == 206
assert issubclass(PreviewMuxPending, RuntimeError)
_dash_only = {
    "formats": [
        {
            "height": 720,
            "protocol": "https",
            "url": "https://x/v.mp4",
            "acodec": "none",
        },
        {
            "height": 480,
            "protocol": "https",
            "url": "https://x/v2.mp4",
            "acodec": "none",
        },
    ],
}
assert _youtube_info_is_dash_only_progressive(_dash_only)
assert not _youtube_info_is_dash_only_progressive(
    {
        "formats": [
            {"height": 720, "protocol": "m3u8_native", "url": "https://x/m.m3u8"}
        ],
    }
)
assert _youtube_needs_dash_window_hls(
    _dash_only["formats"],
    {"_preview_audio_format": {"url": "https://x/a.m4a"}},
    crop_span_sec=30,
)
assert _youtube_needs_dash_window_hls(
    _dash_only["formats"],
    {"_preview_audio_format": {"url": "https://x/a.m4a"}},
    crop_span_sec=120,
)
assert not _youtube_needs_dash_window_hls(_dash_only["formats"], {})
_window_sess = PreviewSession(
    session_id="x",
    vod_url="https://www.youtube.com/watch?v=x",
    platform="YouTube",
    master_url="",
    entry_url="https://example.com/v",
    cache_dir=Path("/tmp"),
    crop_start=0,
    crop_end=25,
    dash_window_hls=True,
)
_pl = _build_youtube_window_hls_master(_window_sess)
assert _pl.startswith("#EXTM3U") and "window-playlist" in _pl
assert _pl.splitlines()[3].startswith("#EXT-X-STREAM-INF:")
assert "resource?id=window-playlist" in _pl
assert _window_pl_sess.dash_window_hls is True
assert session_trim_timeline(_window_sess) is True
_explore_sess = PreviewSession(
    session_id="y",
    vod_url="https://www.youtube.com/watch?v=y",
    platform="YouTube",
    master_url="",
    entry_url="https://example.com/v",
    cache_dir=Path("/tmp"),
    crop_start=0,
    crop_end=7200,
    dash_window_hls=True,
)
assert session_trim_timeline(_explore_sess) is False
assert _window_hls_dir(_window_sess).name == "window_hls"
assert _window_hls_playlist_path(_window_sess).name == "window.m3u8"
assert _window_hls_segment_path(_window_sess, 0).name == "seg_000.ts"
assert _window_hls_segment_path(_window_sess, 7).name == "seg_007.ts"
assert not _window_hls_seg0_ready(_window_sess)
assert not _window_hls_playlist_complete(_window_sess)
_window_hls_dir(_window_sess).mkdir(parents=True, exist_ok=True)
(_window_hls_dir(_window_sess) / "seg_000.ts").write_bytes(b"\x47" * 60000)
assert _window_hls_seg0_ready(_window_sess)
_stamp_window_hls_mux_start(_window_sess, 0)
assert _window_hls_seg0_matches_bounds(_window_sess)
_clear_youtube_window_hls_cache(_window_sess)
assert not _window_hls_seg0_matches_bounds(_window_sess)
import shutil as _sh

_sh.rmtree(_window_hls_dir(_window_sess), ignore_errors=True)
# Build a synthetic session for media-playlist builder assertions (no network).
import tempfile as _tmp

_with_dir = _tmp.mkdtemp(prefix="dash_window_hls_test_")
_mp_sess = PreviewSession(
    session_id="mp",
    vod_url="https://www.youtube.com/watch?v=x",
    platform="YouTube",
    master_url="",
    entry_url="https://example.com/v",
    cache_dir=Path(_with_dir),
    crop_start=0,
    crop_end=20,
    dash_window_hls=True,
)
(_with_dir_p := Path(_with_dir) / "window_hls").mkdir(parents=True, exist_ok=True)
(_with_dir_p / "seg_000.ts").write_bytes(b"\x47" * 60000)
(_with_dir_p / "seg_001.ts").write_bytes(b"\x47" * 50000)
(_with_dir_p / "window.m3u8").write_text(
    "#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-TARGETDURATION:5\n"
    "#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-PLAYLIST-TYPE:VOD\n"
    "#EXTINF:4.000,\nseg_000.ts\n#EXTINF:4.000,\nseg_001.ts\n#EXT-X-ENDLIST\n",
    encoding="utf-8",
)
_mp_body = _build_youtube_window_hls_media_playlist(_mp_sess)
assert _mp_body.startswith(b"#EXTM3U")
assert b"window-seg-000" in _mp_body
assert b"window-seg-001" in _mp_body
assert b"#EXT-X-ENDLIST" in _mp_body
assert preview_mux_ready(_mp_sess)
assert _window_hls_playlist_complete(_mp_sess)
_register_youtube_window_hls_resources(_mp_sess)
assert _mp_sess.resource_map[WINDOW_HLS_PLAYLIST_RESOURCE] == f"{WINDOW_HLS_MARKER}playlist"
_mp_seg_ext = ".m4s" if USE_FMP4 else ".ts"
assert _mp_sess.resource_map["window-seg-000"] == f"{WINDOW_HLS_MARKER}seg_000{_mp_seg_ext}"
assert _mp_sess.resource_map["window-seg-001"] == f"{WINDOW_HLS_MARKER}seg_001{_mp_seg_ext}"
if USE_FMP4:
    assert _mp_sess.resource_map[WINDOW_HLS_INIT_RESOURCE] == f"{WINDOW_HLS_MARKER}init.mp4"
    assert b"#EXT-X-PART-INF" in _mp_body
    assert b"#EXT-X-SERVER-CONTROL" in _mp_body
    assert b"#EXT-X-MAP:URI=\"" in _mp_body
    assert b"#EXT-X-PART:DURATION=0.5" in _mp_body
    assert b"#EXT-X-PRELOAD-HINT" in _mp_body
    assert _mp_body.splitlines()[1].startswith(b"#EXT-X-VERSION:9")
assert _is_youtube_window_hls_resource(
    _mp_sess.resource_map[WINDOW_HLS_PLAYLIST_RESOURCE]
)
assert _is_youtube_window_hls_resource("youtube-window-hls:anything")
assert not _is_youtube_window_hls_resource("https://googlevideo.com/v")
_sh.rmtree(_with_dir, ignore_errors=True)
assert _get_resolved_stream_cached("missing:key") is None
_put_resolved_stream_cache("t:720", ("u", {}, "YouTube", [], "hls", {}))
assert _get_resolved_stream_cached("t:720")[0] == "u"
assert int("window-seg-000".rsplit("-", 1)[1]) == 0
_sess = PreviewSession(
    session_id="t",
    vod_url="",
    master_url="",
    entry_url="",
    platform="YouTube",
    cache_dir=Path("/tmp"),
    crop_start=0,
    crop_end=7200,
)
_clamp_session_crop_to_vod_duration(_sess, {"duration": 212})
assert _sess.crop_end == 212
assert _sess.vod_duration == 212
_under = PreviewSession(
    session_id="u",
    vod_url="",
    master_url="",
    entry_url="",
    platform="YouTube",
    cache_dir=Path("/tmp"),
    crop_start=0,
    crop_end=50,
)
_clamp_session_crop_to_vod_duration(_under, {"duration": 19})
assert _under.crop_end == 50 and _under.vod_duration == 50
_placeholder = PreviewSession(
    session_id="p",
    vod_url="https://www.youtube.com/watch?v=x",
    master_url="",
    entry_url="",
    platform="YouTube",
    cache_dir=Path("/tmp"),
    crop_start=0,
    crop_end=7200,
)
_clamp_session_crop_to_vod_duration(_placeholder, {"duration": 19})
assert _placeholder.crop_end == 19, (
    "placeholder clamp without extract dur stays at extract dur"
)
assert (
    _youtube_warm_inflight_key("https://www.youtube.com/shorts/dQw4w9WgXcQ")
    == "dQw4w9WgXcQ"
)
assert (
    _youtube_warm_inflight_key("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    == "dQw4w9WgXcQ"
)
_long = PreviewSession(
    session_id="long",
    vod_url="",
    master_url="",
    entry_url="",
    platform="YouTube",
    cache_dir=Path("/tmp"),
    crop_start=0,
    crop_end=1158,
    dash_window_hls=True,
)
_lo, _hi = _window_hls_mux_bounds(_long)
assert _hi - _lo <= WINDOW_HLS_INITIAL_CHUNK_SEC + 0.01
assert _hi == WINDOW_HLS_INITIAL_CHUNK_SEC
_short = PreviewSession(
    session_id="short",
    vod_url="",
    master_url="",
    entry_url="",
    platform="YouTube",
    cache_dir=Path("/tmp"),
    crop_start=0,
    crop_end=30,
    dash_window_hls=True,
)
_slo, _shi = _window_hls_mux_bounds(_short)
assert _shi == 30 and _slo == 0
_seek_lo, _seek_hi = _window_hls_mux_bounds(_long, around_sec=868.5)
assert _seek_lo < 868.5 < _seek_hi
assert _seek_hi - _seek_lo <= WINDOW_HLS_MUX_CHUNK_SEC + 0.01
_long.vod_duration = WINDOW_HLS_LONG_VOD_MIN_SEC + 1
assert _window_hls_seek_chunk_sec(_long) == WINDOW_HLS_MUX_CHUNK_LONG_SEC
