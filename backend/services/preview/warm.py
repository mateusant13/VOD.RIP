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
logger = logging.getLogger(__name__)

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

from services.preview._state import (
    _ACTIVE_YOUTUBE_PREVIEW_KEY,
    _ACTIVE_YOUTUBE_PREVIEW_LOCK,
    _CHANNEL_WARM_SLOTS,
    _CHANNEL_WARM_SLOTS_LOCK,
    _full_warm_queued,
    _MAX_WARM_FAILURES,
    _PREFLIGHT_MUX_INFLIGHT,
    _PREFLIGHT_MUX_LOCK,
    _PRINTED_COOLDOWN,
    preview_root,
    _RESOLVED_STREAM_CACHE,
    _RESOLVED_STREAM_LOCK,
    _RESOLVED_STREAM_MAX,
    _RESOLVED_STREAM_TTL_SEC,
    _SESSION_SNAPSHOT,
    _SESSION_SNAPSHOT_LOCK,
    _SESSION_SNAPSHOT_MAX,
    _SESSION_SNAPSHOT_TTL_SEC,
    _WARMED_URLS,
    _WARMED_URLS_LOCK,
    _WARM_COOLDOWN_SEC,
    _YOUTUBE_WARM_CACHE,
    _YOUTUBE_WARM_CACHE_LOCK,
    _YOUTUBE_WARM_CONSECUTIVE_FAILURES,
    _YOUTUBE_WARM_COOLDOWN_UNTIL,
    _YOUTUBE_WARM_INFLIGHT,
    _YOUTUBE_WARM_LOCK,
    _YOUTUBE_WARM_RATE_LIMIT_LOCK,
)


def _warm_rate_limit_check() -> bool:
    """Return True if YouTube warm requests should be skipped (in cooldown)."""
    return time.monotonic() < _YOUTUBE_WARM_COOLDOWN_UNTIL


# Track whether POT warm completed (resets circuit breaker)
_pot_ready: bool = False
# Once-per-process POT readiness wait for the first cold preview click.
_pot_wait_attempted: bool = False


def _await_pot_readiness_once(timeout_sec: float = 2.0) -> None:
    """Cold-start: wait briefly (once per process) for the bgutil POT server.

    The POT server warms async at startup; the first preview click often
    lands before it is ready, so WEB_SAFARI/WEB probes fail with
    gated-looking responses. A short one-time wait gives the first click
    POT-backed WEB probes; later clicks never pay it.
    ponytail: 2s cap — a slow node cold start just falls through to the
    mobile-client ladder (which needs no POT), so the wait never blocks a
    preview past its SLA.
    """
    global _pot_wait_attempted
    if _pot_wait_attempted:
        return
    _pot_wait_attempted = True
    try:
        from services.youtube_auth import pot_minting_enabled

        if not pot_minting_enabled():
            return
        from services.youtube_pot_service import pot_service_is_ready, pot_service_ping

        if pot_service_is_ready() or pot_service_ping():
            return
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            time.sleep(0.1)
            if pot_service_ping():
                return
    except Exception:
        pass


def _maybe_reset_circuit_breaker() -> None:
    """Once POT is ready, clear the circuit breaker so warm tries again."""
    from services.youtube_auth import pot_minting_enabled as _pot_minting_enabled

    global _YOUTUBE_WARM_CONSECUTIVE_FAILURES, _YOUTUBE_WARM_COOLDOWN_UNTIL, _PRINTED_COOLDOWN, _pot_ready
    if _pot_ready or not _pot_minting_enabled():
        return
    try:
        from services.youtube_pot_service import pot_service_ping
        if pot_service_ping():
            _pot_ready = True
            with _YOUTUBE_WARM_RATE_LIMIT_LOCK:
                if _YOUTUBE_WARM_COOLDOWN_UNTIL > 0:
                    _YOUTUBE_WARM_COOLDOWN_UNTIL = 0.0
                    _YOUTUBE_WARM_CONSECUTIVE_FAILURES = 0
                    logger.info("YouTube warm circuit breaker reset -- POT ready")
    except Exception:
        pass


def _record_warm_failure() -> None:
    """Record a consecutive warm failure. If threshold reached, start cooldown.

    Failures before POT ready are NOT counted — startup wave fires before POT
    warm completes, and bot-gate errors during that window are expected.
    Circuit breaker resets once POT becomes ready.
    """
    global _YOUTUBE_WARM_CONSECUTIVE_FAILURES, _YOUTUBE_WARM_COOLDOWN_UNTIL, _PRINTED_COOLDOWN
    from services.youtube_auth import pot_minting_enabled as _pot_minting_enabled

    _maybe_reset_circuit_breaker()
    if _YOUTUBE_WARM_COOLDOWN_UNTIL > 0:
        return  # already in cooldown — don't accumulate
    if _YOUTUBE_WARM_CONSECUTIVE_FAILURES == 0 and not _pot_ready and _pot_minting_enabled():
        return  # POT not ready yet — don't count startup failures
    with _YOUTUBE_WARM_RATE_LIMIT_LOCK:
        _YOUTUBE_WARM_CONSECUTIVE_FAILURES += 1
        if _YOUTUBE_WARM_CONSECUTIVE_FAILURES >= _MAX_WARM_FAILURES:
            _YOUTUBE_WARM_COOLDOWN_UNTIL = time.monotonic() + _WARM_COOLDOWN_SEC
            if not _PRINTED_COOLDOWN:
                logger.warning(
                    "YouTube warm rate-limit circuit breaker activated: %d consecutive failures, "
                    "pausing warm for %ds",
                    _MAX_WARM_FAILURES, _WARM_COOLDOWN_SEC)
                _PRINTED_COOLDOWN = True
            _YOUTUBE_WARM_CONSECUTIVE_FAILURES = 0
_PREFLIGHT_MUX_LOCK = threading.Lock()
def _preflight_mux_dir(video_id: str, prefer_height: int) -> Path:
    return preview_root() / "preflight" / f"{video_id}_{prefer_height}"
def _preflight_seg0_ready(out_dir: Path) -> bool:
    """seg_000 exists — .m4s when USE_FMP4 (default), else .ts."""
    from services.preview.session import USE_FMP4

    exts = ("m4s", "ts") if USE_FMP4 else ("ts", "m4s")
    return any(
        (out_dir / f"seg_000.{ext}").is_file()
        and (out_dir / f"seg_000.{ext}").stat().st_size >= MIN_VALID_OUTPUT_BYTES
        for ext in exts
    )
def _try_adopt_preflight_mux(session: PreviewSession) -> bool:
    """Reuse paste-warm mux when crop starts at 0 and tier matches."""
    from services.preview.session import _window_hls_dir, _window_hls_seg0_ready
    from services.youtube_innertube import extract_video_id

    if float(getattr(session, "window_hls_mux_start", 0.0)) > 0.01:
        return False
    vid = extract_video_id(session.vod_url or "")
    if not vid:
        return False
    height = int(session.prefer_height or 360)
    src = _preflight_mux_dir(vid, height)
    if not _preflight_seg0_ready(src):
        return False
    dst = _window_hls_dir(session)
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("seg_000.m4s", "seg_000.ts", "init.mp4", "window.m3u8", "_v.mp4", "_a.m4a"):
        src_file = src / name
        if src_file.is_file():
            shutil.copy2(src_file, dst / name)
    for seg in sorted(
        [*src.glob("seg_[0-9][0-9][0-9].m4s"), *src.glob("seg_[0-9][0-9][0-9].ts")]
    ):
        if seg.name in ("seg_000.m4s", "seg_000.ts"):
            continue
        shutil.copy2(seg, dst / seg.name)
    return _window_hls_seg0_ready(session)
def kickoff_youtube_preflight_mux(
    url: str,
    prefer_height: int = 360,
) -> None:
    """Background mux of the initial window chunk — adopted on create_session."""
    from services.preview.session import _window_hls_dir, _window_hls_seg0_ready
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
        from services.preview.session import MuxJob
        try:
            _youtube_preflight_mux(url, prefer_height=prefer_height)
        finally:
            with _PREFLIGHT_MUX_LOCK:
                _PREFLIGHT_MUX_INFLIGHT.pop(key, None)
            done.set()

    from deps import GESTURE_WARM_EXECUTOR

    GESTURE_WARM_EXECUTOR.submit(_run)
def kickoff_youtube_batch_warm(
    url: str,
    cookies_file: Optional[str] = None,
    prefer_height: int = 360,
    channel_key: str = "",
) -> None:
    """Lightweight warm for batch (startup) use.

    - Deduped via the same _YOUTUBE_WARM_INFLIGHT set as kickoff_youtube_warm.
    - Skips the "active preview" bail so the user's first click doesn't
      cancel preloading.
    - Populates only the resolve cache (no preflight mux) so concurrent
      batch warm jobs don't fight over _PREFLIGHT_MUX_INFLIGHT.
    """
    from services.preview.session import create_session
    from services.ytdlp_hls import preview_fast_only_mode

    if preview_fast_only_mode():
        return
    key = _youtube_warm_inflight_key(url)
    if not key:
        return
    # Register in-flight at ENQUEUE time (not run start) so create_session's
    # 1.5s + 3.0s wait can join a queued-but-not-running warm (startup storm
    # warms land within the wait window). Mirror kickoff_youtube_warm.
    with _YOUTUBE_WARM_LOCK:
        if key in _YOUTUBE_WARM_INFLIGHT:
            return
        done = threading.Event()
        _YOUTUBE_WARM_INFLIGHT[key] = done

    def _run() -> None:
        try:
            if time.monotonic() < _warm_bot_gate_pause_until:
                return
            # Rate-limit circuit breaker: skip if YouTube is rate-limiting us
            if _warm_rate_limit_check():
                return
            # ponytail: warm_youtube_resolve_only itself builds + stashes the
            # session snapshot. Calling it twice would re-extract + re-build.
            warm_youtube_resolve_only(
                url, prefer_height=prefer_height,
                channel_key=channel_key,
            )
            with _YOUTUBE_WARM_RATE_LIMIT_LOCK:
                _YOUTUBE_WARM_CONSECUTIVE_FAILURES = 0
                _PRINTED_COOLDOWN = False
        except Exception as exc:
            logger.debug("Warm failed for %s: %s", url, exc)
            _record_warm_failure()
            raise
        finally:
            with _YOUTUBE_WARM_LOCK:
                ev = _YOUTUBE_WARM_INFLIGHT.pop(key, None)
            if ev is not None:
                ev.set()

    from deps import WARM_EXECUTOR

    WARM_EXECUTOR.submit(_run)
def _youtube_preflight_mux(
    url: str,
    prefer_height: int = 360,
) -> bool:
    """Mux [0, INITIAL_CHUNK) to preflight cache — no session required."""
    from services.youtube_innertube import extract_video_id
    from services.ytdlp_hls import _mux_dash_window_to_hls
    from services.preview.session import resolve_stream_info
    from services.preview.session import _resolve_youtube_preview_audio
    from services.preview.session import _youtube_needs_dash_window_hls, _pick_variant_by_height, _merge_youtube_session_cookies, WINDOW_HLS_INITIAL_CHUNK_SEC

    vid = extract_video_id((url or "").strip())
    if not vid:
        return False
    out_dir = _preflight_mux_dir(vid, prefer_height)
    if _preflight_seg0_ready(out_dir):
        return True
    try:
        _raw, headers, platform, variant_formats, _kind, yt_info = resolve_stream_info(
            url,
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
    from services.preview.session import WINDOW_HLS_INITIAL_CHUNK_SEC, _merge_youtube_session_cookies
    from services.preview.session import _apply_youtube_custom_master, _build_synthetic_master_playlist, _clamp_session_crop_to_vod_duration, _hosts_for_url, _pick_variant_by_height, create_session
    from services.preview.session import _init_window_hls_mux_bounds, _resolve_preview_entry, _stash_youtube_preview_formats
    from services.preview.session import _youtube_needs_dash_window_hls
    from services.preview.session import _resolve_youtube_preview_audio
    raw_entry, headers, platform, variant_formats, kind, yt_info = resolve_result
    if platform != "YouTube":
        return None
    from services.youtube_innertube import extract_video_id

    vid = extract_video_id(url or "")
    if not vid:
        return None

    session_id = secrets.token_hex(8)
    cache_dir = preview_root() / session_id
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

    from services.preview.session import PreviewSession

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
            if platform == "YouTube":
                _apply_youtube_custom_master(tmp, variant_formats, yt_info)
            elif len(tmp.variant_entries) >= 2:
                tmp.custom_master = _build_synthetic_master_playlist(
                    tmp, variant_formats
                )
            for _height, upstream in tmp.variant_entries:
                tmp.allowed_hosts.update(_hosts_for_url(upstream))
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
    if getattr(tmp, "dash_window_hls", False):
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
        "dash_window_hls": getattr(tmp, "dash_window_hls", False),
        "preview_audio_fmt": tmp.preview_audio_fmt,
        "preview_video_fmt": getattr(tmp, "preview_video_fmt", None),
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
    prefer_height: int = 360,
) -> None:
    """Drop cached resolve for *url* — refresh must not recycle expired googlevideo URLs."""
    from services.youtube_innertube import extract_video_id

    vid = extract_video_id((url or "").strip())
    if not vid:
        return
    key = f"{vid}:{prefer_height}:v2"
    with _RESOLVED_STREAM_LOCK:
        _RESOLVED_STREAM_CACHE.pop(key, None)
def _build_and_cache_youtube_snapshot(
    url: str,
    prefer_height: int,
    resolve_result,
) -> Optional[Tuple[str, int, dict]]:
    """Build and cache a session snapshot from an already-resolved result.

    Called by both warm (which resolves via warm_light) and create_session
    (via _resolve_and_cache_youtube_snapshot below).  Ensures the snapshot
    goes under the exact key that create_session will look up:
    (vid, int(prefer_height or 0)).

    Returns (vid, height, snapshot_dict) for immediate session reuse, or
    None if snapshot-building fails.
    """
    from services.preview.session import create_session
    snap = _build_youtube_session_snapshot(
        url, 0.0, 0.0, prefer_height, resolve_result,
    )
    if snap:
        _put_session_snapshot(snap[0], snap[1], snap[2])
    return snap
def _resolve_and_cache_youtube_snapshot(
    url: str,
    prefer_height: int = 360,
    **resolve_kwargs,
) -> Optional[Tuple[str, int, dict]]:
    """Resolve + cache — full self-contained path for create_session fallback.

    Calls resolve_stream_info, then _build_and_cache_youtube_snapshot so
    the snapshot is stored under the exact key create_session looks up.
    Returns (vid, height, snapshot_dict) or None on failure.
    """
    _await_pot_readiness_once()
    from services.preview.session import create_session
    from services.preview.session import resolve_stream_info
    t0 = time.monotonic()
    try:
        resolve_result = resolve_stream_info(
            url, prefer_height=prefer_height, **resolve_kwargs,
        )
    except Exception as exc:
        # ponytail: this failure used to vanish — a cold click then burned ~27s
        # in silence before the fallthrough re-raised. One line keeps the
        # console honest without leaking per-retry noise (those stay debug).
        logger.warning(
            "cold preview resolve failed for %s after %.1fs: %s",
            url[:80], time.monotonic() - t0, exc,
        )
        return None
    return _build_and_cache_youtube_snapshot(url, prefer_height, resolve_result)
def _invalidate_youtube_resolve_caches(
    url: str,
    prefer_height: int = 360,
) -> None:
    from services.ytdlp_hls import invalidate_youtube_extract_cache

    invalidate_youtube_extract_cache(url)
    invalidate_resolved_stream_cache(url, prefer_height)
def invalidate_youtube_preview_caches(url: str) -> None:
    """Drop EVERY in-memory preview cache layer for *url* (cold-reset for tests).

    Covers all height-keyed variants, unlike invalidate_resolved_stream_cache
    which drops a single {vid}:{height}:v2 key. Disk caches (prog head, yt-dlp
    cachedir) are intentionally kept — they don't affect session-create time.
    """
    from services.youtube_innertube import extract_video_id
    from services.ytdlp_hls import invalidate_youtube_extract_cache

    vid = extract_video_id((url or "").strip())
    if not vid:
        return
    invalidate_youtube_extract_cache(url)
    invalidate_session_snapshot(vid)
    with _RESOLVED_STREAM_LOCK:
        for k in [k for k in _RESOLVED_STREAM_CACHE if k.startswith(f"{vid}:")]:
            _RESOLVED_STREAM_CACHE.pop(k, None)
def _youtube_warm_inflight_key(url: str) -> str:
    """Dedup paste warm + create_session await on video id, not shorts vs watch URL."""
    from services.youtube_innertube import extract_video_id

    raw = (url or "").strip()
    return extract_video_id(raw) or raw
def kickoff_youtube_warm(
    url: str,
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
    from services.preview.session import create_session
    from services.ytdlp_hls import preview_fast_only_mode

    if preview_fast_only_mode():
        return
    key = _youtube_warm_inflight_key(url)
    if not key:
        return
    # Register in-flight at ENQUEUE time (not run start) so create_session's
    # 1.5s + 3.0s wait can join a warm that is queued but not yet running
    # (hover warm fired within the wait window). Mirror kickoff_youtube_preflight_mux.
    with _YOUTUBE_WARM_LOCK:
        if key in _YOUTUBE_WARM_INFLIGHT:
            return
        done = threading.Event()
        _YOUTUBE_WARM_INFLIGHT[key] = done

    def _run() -> None:
        try:
            if time.monotonic() < _warm_bot_gate_pause_until:
                return
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
                url, cookies_file=cookies_file, prefer_height=prefer_height
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
    from services.preview.session import _full_mux_cache_path
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
        from services.preview.session import _resolve_youtube_preview_audio
        global _warm_bot_gate_pause_until
        if time.monotonic() < _warm_bot_gate_pause_until:
            return
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

            if not warm_youtube_extract(url, cookies_file=cookies_file):
                return
            try:
                _entry, headers, _platform, variant_formats, _kind, yt_info = (
                    resolve_stream_info(url, prefer_height=prefer_height)
                )
            except Exception as exc:
                from services.ytdlp_hls import _youtube_soft_neg_error

                if _youtube_soft_neg_error(exc):
                    if (
                        _warm_note_soft_neg()
                        and time.monotonic() >= _warm_bot_gate_pause_until
                    ):
                        _warm_bot_gate_pause_until = time.monotonic() + _FULL_WARM_BACKOFF_SEC
                        logger.warning(f"YouTube bot-gate detected (2 consecutive soft-negatives); warm paused {int(_FULL_WARM_BACKOFF_SEC // 3600)}h")
                logger.warning("full-mux warm resolve failed for %s: %s", url[:80], exc, exc_info=True)
                return
            _warm_note_success()
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
    try:
        key = _youtube_warm_inflight_key(url) if url else None
        from services.preview import _state as _pv_state

        with _pv_state._ACTIVE_YOUTUBE_PREVIEW_LOCK:
            _pv_state._ACTIVE_YOUTUBE_PREVIEW_KEY = key
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
def _await_youtube_warm_catchup(url: str, timeout_sec: float = 3.0) -> None:
    """If the warm is still running after the initial brief wait, give it
    more time before falling through to a fresh resolve."""
    key = _youtube_warm_inflight_key(url)
    if not key:
        return
    with _YOUTUBE_WARM_LOCK:
        ev = _YOUTUBE_WARM_INFLIGHT.get(key)
    if ev is not None and not ev.wait(timeout_sec):
        logger.debug("YouTube warm catchup wait timed out for %s", key[:80])
from concurrent.futures import ThreadPoolExecutor as _TPE
from concurrent.futures import TimeoutError as FuturesTimeoutError
_FULL_WARM_EXECUTOR = _TPE(max_workers=1, thread_name_prefix="yt-full-warm")
_ANON_PROBE_EXECUTOR = _TPE(max_workers=2, thread_name_prefix="yt-anon")
_ANON_PROBE_HEAD_START_SEC = 2.0
_full_warm_backoff_until = 0.0
# Research (2026-08): YouTube's "sign in to confirm you're not a bot" check has
# no official duration; community reports say it clears in hours (IP-level 403
# blocks: 12-24h). 10min was too short — the warm kept re-hitting the gate and
# re-triggering. 2h covers the mild gate while a false positive recovers
# within a session. Only prefetch warm paths consult this pause; interactive
# previews (create_session -> resolve_stream_info) resolve directly.
_FULL_WARM_BACKOFF_SEC = 7200.0
_warm_bot_gate_pause_until = 0.0   # monotonic clock, checked by all warm _run to fast-skip on YouTube bot-gate
# Bot-gate pause requires 2+ CONSECUTIVE soft-negative failures. A single
# soft-neg (e.g. the innertube race exhausting under degraded network) is a
# transient error, not a bot-gate — pausing every warm for 10 min on one of
# them is what made startup warm crawl. Any successful resolve resets the
# streak (it proves the IP is not gated).
_warm_soft_neg_streak = 0
# 3+ consecutive soft-negs = likely real bot-gate. 2 was still tripping on
# transient innertube races during cold-network startup (observed on the
# user's machine: extracts succeeded right after the pause fired).
_SOFT_NEG_PAUSE_THRESHOLD = 3


def _warm_note_soft_neg() -> bool:
    """Count a soft-negative warm failure; True only on the 2nd+ consecutive."""
    global _warm_soft_neg_streak
    _warm_soft_neg_streak += 1
    return _warm_soft_neg_streak >= _SOFT_NEG_PAUSE_THRESHOLD


def _warm_note_success() -> None:
    """A successful warm resolve proves we are not bot-gated — reset the streak."""
    global _warm_soft_neg_streak
    _warm_soft_neg_streak = 0


def _enqueue_full_warm(
    url: str, cookies_file: Optional[str], prefer_height: int
) -> None:
    global _full_warm_backoff_until
    if time.monotonic() < _full_warm_backoff_until:
        return
    if url in _full_warm_queued:
        return
    _full_warm_queued.add(url)

    def _run() -> None:
        global _full_warm_backoff_until
        global _warm_bot_gate_pause_until
        if time.monotonic() < _warm_bot_gate_pause_until:
            return
        try:
            warm_youtube_preview_resolve(
                url, cookies_file=cookies_file, prefer_height=prefer_height,
                reraise=True,
            )
        except Exception as exc:
            from services.ytdlp_hls import _youtube_soft_neg_error

            if _youtube_soft_neg_error(exc):
                if _warm_note_soft_neg():
                    _full_warm_backoff_until = time.monotonic() + _FULL_WARM_BACKOFF_SEC
                    if time.monotonic() >= _warm_bot_gate_pause_until:
                        _warm_bot_gate_pause_until = time.monotonic() + _FULL_WARM_BACKOFF_SEC
                        logger.warning(f"YouTube bot-gate detected (2 consecutive soft-negatives); warm paused {int(_FULL_WARM_BACKOFF_SEC // 3600)}h")
        finally:
            _full_warm_queued.discard(url)

    _FULL_WARM_EXECUTOR.submit(_run)
def warm_youtube_preview_resolve(
    url: str,
    cookies_file: Optional[str] = None,
    prefer_height: int = 360,
    *,
    reraise: bool = False,
) -> bool:
    """Populate resolved-stream cache on hover — same path as create_session.

    Uses the full extract race (InnerTube + yt-dlp fallback): gesture warms are
    few (hover/scroll-visible rows) and must survive InnerTube bot-gating that
    kills the light-only pass. The bulk startup storm uses warm_youtube_resolve_only.

    Also prebuilds the session snapshot so the click path skips the ~5s
    extract + variant-build work on a warm hit.
    """
    from services.preview.session import create_session
    from services.preview.session import kickoff_youtube_prog_head_warm
    from services.preview.session import resolve_stream_info
    try:
        resolve_result = resolve_stream_info(
            url, prefer_height=prefer_height
        )
        kickoff_youtube_preflight_mux(url, prefer_height=prefer_height)
        kickoff_youtube_prog_head_warm(url, prefer_height=prefer_height)
    except Exception as exc:
        from services.ytdlp_hls import _youtube_soft_neg_error

        if _youtube_soft_neg_error(exc):
            global _warm_bot_gate_pause_until
            if (
                _warm_note_soft_neg()
                and time.monotonic() >= _warm_bot_gate_pause_until
            ):
                _warm_bot_gate_pause_until = time.monotonic() + _FULL_WARM_BACKOFF_SEC
                logger.warning(f"YouTube bot-gate detected (2 consecutive soft-negatives); warm paused {int(_FULL_WARM_BACKOFF_SEC // 3600)}h")
        logger.info("YouTube warm resolve skipped for %s: %s", url[:80], exc)
        if reraise:
            raise
        return False
    try:
        snap = _build_youtube_session_snapshot(
            url, 0.0, 0.0, prefer_height, resolve_result
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
    _warm_note_success()
    return True
def warm_youtube_resolve_only(
    url: str,
    cookies_file: Optional[str] = None,
    prefer_height: int = 360,
    channel_key: str = "",
) -> bool:
    """Populate resolved-stream cache + preflight the head so the click can play
    immediately. Uses the light extract so concurrent warm jobs don't fight for
    yt-dlp locks during a startup storm. The full InnerTube race path runs on
    the user's first preview click.

    Also prebuilds the session snapshot so the click path skips the ~5s
    extract + variant-build work on a warm hit. Every warm path that lands
    a resolve must produce a snapshot — otherwise the user's first click
    waits the full SLA.

    When ``channel_key`` is set, the warmed URL is tracked in a per-channel
    slot list. The oldest entry is evicted when the channel exceeds the max
    of 5 warm slots, so the frontend's YOUTUBE_WARM_VOD_LIMIT is respected
    on the backend side too.
    """
    from services.preview.session import _prog_head_paths
    from services.preview.session import resolve_stream_info
    from services.preview.session import kickoff_youtube_prog_head_warm
    try:
        resolve_result = resolve_stream_info(
            url, prefer_height=prefer_height, warm_light=True
        )
    except Exception as exc:
        # ponytail: light pass failed — usually anon-bot-gated videos the full
        # chain resolves in ~3s. Falling back inline here saturated
        # WARM_EXECUTOR + the yt-dlp lock and starved real clicks (measured:
        # 90s+ create timeouts during a warm storm), so the full-chain retry
        # runs on a dedicated single worker with a bot-gate backoff instead.
        # Fatal failures (members-only/unavailable) are never re-tried.
        from services.ytdlp_hls import _youtube_soft_neg_error

        if _youtube_soft_neg_error(exc):
            global _warm_bot_gate_pause_until
            if (
                _warm_note_soft_neg()
                and time.monotonic() >= _warm_bot_gate_pause_until
            ):
                _warm_bot_gate_pause_until = time.monotonic() + _FULL_WARM_BACKOFF_SEC
                logger.warning(f"YouTube bot-gate detected (2 consecutive soft-negatives); warm paused {int(_FULL_WARM_BACKOFF_SEC // 3600)}h")
        from services.ytdlp_hls import _youtube_fatal_extract_error

        if not _youtube_fatal_extract_error(exc):
            _enqueue_full_warm(url, cookies_file, prefer_height)
        logger.debug("YouTube batch warm resolve skipped for %s: %s", url[:80], exc)
        return False
    # ponytail: preflight the head + mux so the first 5s of playable bytes are
    # on disk before the user clicks. 360p muxed progressive is unauthenticated
    # and survives without cookies/POT/visitor_data.
    try:
        kickoff_youtube_prog_head_warm(url, prefer_height=prefer_height)
    except Exception as exc:
        logger.debug("YouTube batch warm head skipped for %s: %s", url[:80], exc)
    # ponytail: build the session shell through the shared _build_and_cache
    # helper so warm and create_session produce identical keys + payloads.
    snap = _build_and_cache_youtube_snapshot(
        url, prefer_height, resolve_result,
    )
    if snap:
        logger.info(
            "YouTube session snapshot ready: %s h=%d sid=%s",
            snap[0][:11], snap[1], snap[2]["session_id"][:8],
        )
        # Track in warm cache + per-channel slots for eviction
        key = _youtube_warm_inflight_key(url)
        with _YOUTUBE_WARM_CACHE_LOCK:
            _YOUTUBE_WARM_CACHE[key] = (time.time(), snap)
        if channel_key:
            with _CHANNEL_WARM_SLOTS_LOCK:
                slots = _CHANNEL_WARM_SLOTS.setdefault(channel_key, [])
                slots.append(key)
                while len(slots) > 10:
                    evict_key = slots.pop(0)
                    with _YOUTUBE_WARM_CACHE_LOCK:
                        _YOUTUBE_WARM_CACHE.pop(evict_key, None)
                    import shutil
                    # Delete on-disk prog head segments for the evicted video
                    for h in (144, 240, 360, 480, 720, 1080):
                        bin_p, meta_p = _prog_head_paths(evict_key, h)
                        if bin_p.is_file():
                            bin_p.unlink(missing_ok=True)
                        if meta_p.is_file():
                            meta_p.unlink(missing_ok=True)
    else:
        logger.debug("session snapshot skipped for %s (unsupported type)", url[:80])
    # Track in warm cache + per-channel slot eviction so the backend doesn't
    # hold more warmed entries than the frontend YOUTUBE_WARM_VOD_LIMIT of 5.
    if snap:
        from services.youtube_innertube import youtube_watch_url_to_key

        inflight_key = youtube_watch_url_to_key(url)
        if inflight_key:
            with _YOUTUBE_WARM_CACHE_LOCK:
                _YOUTUBE_WARM_CACHE[inflight_key] = snap
        if channel_key:
            with _CHANNEL_WARM_SLOTS_LOCK:
                slots = _CHANNEL_WARM_SLOTS.setdefault(channel_key, [])
                slots.append(url[:200])
                while len(slots) > 5:
                    evicted = slots.pop(0)
                    evicted_key = youtube_watch_url_to_key(evicted)
                    if evicted_key:
                        _YOUTUBE_WARM_CACHE.pop(evicted_key, None)
    _warm_note_success()
    return True
