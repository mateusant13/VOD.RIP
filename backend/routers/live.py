"""Live-stream info and DVR endpoints."""

import asyncio
import json
import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from deps import settings_mgr
from services.live_capture import (
    download_live_stream,
    kick_live_info,
    twitch_live_info,
    youtube_live_info,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["live"])
_PLATFORM_LABEL = {"kick": "Kick", "twitch": "Twitch", "youtube": "YouTube"}


class LiveStatus(BaseModel):
    is_live: bool
    platform: str
    title: str = ""
    viewers: int = 0
    url: str = ""
    headers: dict = {}
    type: str = "hls"
    reason: Optional[str] = None


class LiveDownloadRequest(BaseModel):
    url: str
    platform: str
    title: str
    channel: str = ""
    headers: dict = {}


# ---------------------------------------------------------------------------
# Live captions (real-time ASR captions for the livestream popup)
# ---------------------------------------------------------------------------
#
# The popup's CC overlay subscribes here. One refcounted LiveCaptioner per
# (platform, channel) polls the live audio-only HLS rendition, buffers ~2s
# windows and transcribes them with the parakeet engine OFF the asyncio loop
# (the captioner runs in its own worker thread; caption blocks arrive through
# the same per-connection queue pattern as the chat SSE). The parakeet gate
# (sherpa-onnx importable AND model files present) 503s the stream endpoint
# and the /available probe reports it so the frontend hides the CC toggle.
_CAPTION_PLATFORMS = ("twitch", "kick", "youtube")


_CAPTION_LANGS = ("pt", "en", "es")  # in-player selector (pt-BR / English / Español)


@router.get("/live/captions")
async def live_captions_stream(
    request: Request,
    platform: str = Query(..., description="twitch | kick | youtube"),
    channel: str = Query(..., description="channel slug / login"),
    lang: Optional[str] = Query(None, description="caption translate-target family override: pt | en | es (default: app language)"),
):
    """SSE stream of live caption blocks for one channel.

    Emits ``event: caption`` frames (``{text, start, end}``) plus keepalive
    comments; a confirmed ``event: offline`` ends the stream (the frontend
    retries with backoff — a fresh connection restarts the captioner, so a
    recovered ASR/translate pipeline resumes captions without user action).
    503 when the parakeet engine is unavailable — the frontend probes
    /available first and never opens the stream then. The captioner's
    refcount drops when the connection closes (generator finally).

    ``lang`` overrides the translate target per session (the captioner is
    shared per (platform, channel), so the LAST explicit selection wins for
    all current subscribers; None follows the app language at flush time).
    """
    plat = (platform or "").lower()
    chan = (channel or "").strip().lower()
    if plat not in _CAPTION_PLATFORMS or not chan:
        raise HTTPException(status_code=400, detail="platform must be one of twitch/kick/youtube and channel is required")
    if lang is not None:
        lang = lang.strip().lower()
        if lang not in _CAPTION_LANGS:
            raise HTTPException(status_code=400, detail="lang must be one of pt/en/es")
    try:
        from services.feature_registry import is_enabled as _feat_enabled2
        if not _feat_enabled2("live-captions"):
            raise HTTPException(status_code=503, detail="live-captions feature is disabled")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="live-captions feature is disabled")
    from services.live_captions import captions_available, get_captioner
    ok, reason = await asyncio.to_thread(captions_available, plat)
    if not ok:
        raise HTTPException(status_code=503, detail=reason)
    captioner = get_captioner(plat, chan, asyncio.get_running_loop())
    if captioner is None:
        raise HTTPException(status_code=429, detail="too many active caption streams — try again later")
    queue = None
    captioner.acquire(lang)
    try:
        queue = captioner.subscribe()
        return StreamingResponse(
            _captions_sse_gen(request, captioner, queue),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    except Exception:
        if queue is not None:
            captioner.unsubscribe(queue)
        captioner.release()
        raise


@router.get("/live/captions/available")
async def live_captions_available(
    platform: str = Query(..., description="twitch | kick | youtube"),
    channel: str = Query(..., description="channel slug / login"),
) -> dict:
    """Parakeet-gate probe: ``{available, reason}`` — the popup renders the
    CC toggle only when available is true (and never opens the stream 503)."""
    plat = (platform or "").lower()
    chan = (channel or "").strip().lower()
    if plat not in _CAPTION_PLATFORMS or not chan:
        raise HTTPException(status_code=400, detail="platform must be one of twitch/kick/youtube and channel is required")
    # Feature gate: live-captions OFF => unavailable without burning GPU
    try:
        from services.feature_registry import is_enabled as _feat_enabled
        if not _feat_enabled("live-captions"):
            return {"available": False, "reason": "live-captions feature is disabled", "low_latency": False}
    except Exception:
        return {"available": False, "reason": "live-captions feature is disabled", "low_latency": False}
    import os
    from services.live_captions import captions_available, LOW_LATENCY_ENV

    ok, reason = await asyncio.to_thread(captions_available, plat)
    # Read from settings first, fall back to env var.
    try:
        from deps import settings_mgr
        low_latency = bool(settings_mgr.get().caption_low_latency)
    except Exception:
        low_latency = False
    if not low_latency:
        low_latency = (os.environ.get(LOW_LATENCY_ENV, "0") or "0").strip() == "1"
    return {"available": ok, "reason": reason or None, "low_latency": low_latency}


@router.get("/live/captions/errors")
def live_captions_errors(limit: int = Query(20, ge=1, le=50)) -> dict:
    """Recent live-caption errors (HLS/ASR/translate)."""
    from services.live_captions import get_error_ring
    # ponytail: unauthenticated but bounded (50 entries, no secrets); gate behind auth when app auth lands.
    return {"errors": get_error_ring(limit)}

async def _captions_sse_gen(request: Request, captioner, queue: asyncio.Queue) -> "AsyncGenerator[str, None]":
    """SSE body generator: forward caption blocks, keepalive comments, and
    release the captioner refcount when the connection closes. Extracted so
    tests can drive it directly (ASGITransport buffers the whole body).

    ``queue`` is this connection's own subscriber queue (from
    captioner.subscribe()) — the worker fan-outs every event to all live
    subscriber queues, so one viewer's disconnect never steals blocks from
    another. On teardown we unsubscribe and release the refcount."""
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                event, data = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event == "offline":
                yield "event: offline\ndata: {}\n\n"
                break
            yield f"event: caption\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    finally:
        captioner.unsubscribe(queue)
        captioner.release()
# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/live/{platform}")
def check_live_status(
    platform: str,
    slug: Optional[str] = Query(None),
    login: Optional[str] = Query(None),
    handle: Optional[str] = Query(None),
) -> LiveStatus:
    """Check if a channel is live on a given platform.

    Query parameters are platform-specific:
    - Kick: ``slug``
    - Twitch: ``login``
    - YouTube: ``handle``
    """
    plat = platform.lower()
    label = _PLATFORM_LABEL.get(plat, platform.capitalize())

    if plat == "kick":
        if not slug:
            raise HTTPException(422, "slug query parameter required for Kick")
        info = kick_live_info(slug)
    elif plat == "twitch":
        if not login:
            raise HTTPException(422, "login query parameter required for Twitch")
        info = twitch_live_info(login)
    elif plat == "youtube":
        if not handle:
            raise HTTPException(422, "handle query parameter required for YouTube")
        info = youtube_live_info(handle)
    else:
        raise HTTPException(400, f"Unsupported platform: {platform}")

    if info is None:
        return LiveStatus(is_live=False, platform=label)

    reason = info.pop("reason", None) if isinstance(info, dict) else None
    if not info.get("url"):
        return LiveStatus(
            is_live=False,
            platform=label,
            reason=reason or "Stream offline or unavailable",
        )

    return LiveStatus(
        is_live=True,
        platform=label,
        title=info.get("title", ""),
        viewers=info.get("viewers", 0),
        url=info.get("url", ""),
        headers=info.get("headers", {}),
        type="hls",
    )


# ---------------------------------------------------------------------------
# Channel-scoped live status (with in-process cache + startup warm)
# ---------------------------------------------------------------------------
#
# Live-status reads must return in <100ms so the Channels tab can paint the
# "LIVE" badge immediately on app open — but the underlying yt-dlp extract
# (YouTube/Twitch) takes 3-5s. The cache stores the *response payload* (list
# of LiveStatus dicts) keyed by channel_id with a 60s TTL; fresh reads return
# the cached payload instantly. On a TTL-trip the read kicks a refresh and
# WAITS (bounded, `_LIVE_REFRESH_WAIT_SEC`) for it, returning the FRESH
# payload — so the poll that crosses the TTL is the one that updates the
# badge. Serving the stale payload on the trip meant the frontend saw the
# refreshed state only on its next poll (60s later), a full cycle of badge
# lag. Concurrent trips share one in-flight refresh (deduped by channel). On
# server startup we pre-warm the cache for every saved channel so the very
# first user request hits the warm cache.
#
# Concurrency: refreshes run on a dedicated 4-worker thread pool. We bound
# concurrency so a 20-channel saved list does not slam YouTube at boot.

_LIVE_STATUS_CACHE: dict[str, tuple[float, dict]] = {}
_LIVE_STATUS_TTL_SEC = 60.0
# Serving a "LIVE" badge older than this is a lie: when refreshes keep
# failing (platform outage, parse breakage) the stale-serve path must stop
# returning ancient payloads and report unknown (empty) instead. Kept equal
# to the TTL so a failed refresh at most one cycle behind (see endpoint).
_LIVE_STATUS_MAX_STALE_SEC = 60.0
# How long a TTL-trip read waits for its kicked refresh before falling back
# to the stale serve. Platform extracts run 3-15s (parallel per channel), so
# a 20s bound covers one refresh plus warm-pool queueing headroom.
_LIVE_REFRESH_WAIT_SEC = 20.0
# channel_id -> in-flight refresh Future. TTL-trip reads WAIT on the shared
# future and return the FRESH payload — without this, the poll that crosses
# the TTL serves the stale one and the frontend only sees the refresh on its
# NEXT poll (60s later), which made LIVE badges lag a full cycle behind the
# streamer going live and blink out on every trip. The map dedupes concurrent
# trips (frontend poll + archive watchdog) so the rate-limited platform APIs
# are never double-fetched.
_LIVE_REFRESH_INFLIGHT: dict[str, "Future"] = {}
_LIVE_STATUS_LOCK = threading.Lock()
# channel_id -> monotonic timestamp when the channel was last added via
# POST /api/settings.  trigger_live_detection() stamps here; the cache-read
# fast-path (req 3) skips the TTL check so a freshly-added channel always
# gets a live fetch instead of waiting up to 60s for the next poll cycle.
_LIVE_RECENTLY_ADDED: dict[str, float] = {}
_LIVE_RECENTLY_ADDED_WINDOW_SEC = 60.0
_LIVE_WARM_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="live-warm")
# Platform fetches inside one channel run concurrently across this pool (the
# warm pool + this pool bound total concurrency: 4 channels x 3 platforms
# queue onto 6 workers). Kick/Twitch/YouTube are independent CDNs/APIs, so
# serializing them per channel only multiplied wall time for no reason.
_PLATFORM_FETCH_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="live-platform")


def _fetch_channel_live_payload(
    channel: dict,
    platform_pool: Optional[ThreadPoolExecutor] = None,
) -> dict:
    """Build the response payload for a single channel's live status.

    Kicks off the channel's three platform fetchers (Kick/Twitch/YouTube)
    CONCURRENTLY on the platform pool — serializing them cost
    sum(3-15s) per channel instead of max(3-15s). ``platform_pool`` lets the
    boot warm burst temporarily widen the 6-slot steady-state pool; the
    steady-state polls keep the small pool so periodic refreshes never slam
    the rate-limited platform APIs. The returned dict matches the live
    router response: ``{"live": [...], "channel_id": ...}``.
    """
    pool = platform_pool or _PLATFORM_FETCH_POOL

    ks = (channel.get("kickSlug") or "").strip()
    ts = (channel.get("twitchSlug") or "").strip()
    ys = (channel.get("youtubeSlug") or "").strip()

    jobs: list[tuple[str, "object"]] = []
    if ks:
        jobs.append(("kick", pool.submit(kick_live_info, ks)))
    if ts:
        jobs.append(("twitch", pool.submit(twitch_live_info, ts)))
    if ys:
        jobs.append(("youtube", pool.submit(youtube_live_info, ys)))

    # Settle all futures; re-raise the first platform error in slug order
    # (kick before twitch before youtube), matching the old sequential path's
    # failure semantics so a dead platform still fails the whole refresh.
    infos: dict[str, dict] = {}
    first_exc: Optional[BaseException] = None
    for plat, fut in jobs:
        try:
            info = fut.result()
        except Exception as exc:
            if first_exc is None:
                first_exc = exc
            continue
        if info and isinstance(info, dict):
            infos[plat] = info
    if first_exc is not None:
        raise first_exc

    live: list[dict] = []
    kick_info = infos.get("kick")
    if kick_info and kick_info.get("url"):
        live.append({
            "is_live": True,
            "platform": "Kick",
            "title": kick_info.get("title", ""),
            "viewers": kick_info.get("viewers", 0),
            "url": kick_info.get("url", ""),
            "headers": kick_info.get("headers", {}),
            "type": "hls",
        })
    tw_info = infos.get("twitch")
    if tw_info and tw_info.get("url"):
        live.append({
            "is_live": True,
            "platform": "Twitch",
            "title": tw_info.get("title", ""),
            "viewers": tw_info.get("viewers", 0),
            "url": tw_info.get("url", ""),
            "headers": tw_info.get("headers", {}),
            "type": "hls",
            # vaft rotation extras — frontend ignores unknown keys.
            "player_type": tw_info.get("player_type", "embed"),
            "ad_free": bool(tw_info.get("ad_free")),
            # ISO/epoch stream start — archive chat watchdog anchors
            # message offsets to it. Frontend ignores unknown keys.
            "started_at": tw_info.get("started_at"),
        })
    yt_info = infos.get("youtube")
    if yt_info and yt_info.get("url"):
        live.append({
            "is_live": True,
            "platform": "YouTube",
            "title": yt_info.get("title", ""),
            "viewers": yt_info.get("viewers", 0),
            "url": yt_info.get("url", ""),
            "headers": yt_info.get("headers", {}),
            "type": "hls",
            # Real videoId — archive watchdog anchors chat-capture video
            # rows to it. Frontend ignores unknown keys.
            "videoId": yt_info.get("videoId"),
        })
    return {"live": live, "channel_id": str(channel.get("id") or "")}


def _fetch_or_cached_channel_live_payload(
    channel: dict, max_age_sec: float = _LIVE_STATUS_TTL_SEC
) -> dict:
    """Cached payload when fresher than ``max_age_sec``, else a blocking
    refresh (which also populates the shared cache).

    Used by the archive chat watchdog: it must see current live state to
    start/stop captures, but must NOT duplicate the warm pool / frontend
    poll's work — a cache entry the poll just refreshed is reused as-is.
    """
    cid = str(channel.get("id") or "")
    now = time.monotonic()
    # Fast-path (req 3): newly-added channels (<60s) skip the cache so the
    # first poll always fetches fresh live state instead of returning empty.
    recently_added = False
    with _LIVE_STATUS_LOCK:
        added_ts = _LIVE_RECENTLY_ADDED.get(cid)
        if added_ts is not None:
            recently_added = (now - added_ts) < _LIVE_RECENTLY_ADDED_WINDOW_SEC
    if not recently_added:
        with _LIVE_STATUS_LOCK:
            cached = _LIVE_STATUS_CACHE.get(cid)
            if cached and (now - cached[0]) < max_age_sec:
                return cached[1]
    fut = _submit_refresh(cid, channel)
    if fut is not None:
        try:
            return fut.result(timeout=_LIVE_REFRESH_WAIT_SEC)
        except TimeoutError:
            logger.debug("live status refresh wait timed out for %s", cid)
        except Exception as exc:
            logger.debug("live status refresh wait failed for %s: %s", cid, exc)
        # Refresh still running in the pool — reuse the cache (stale-bound)
        # rather than launching a duplicate fetch.
        with _LIVE_STATUS_LOCK:
            cached = _LIVE_STATUS_CACHE.get(cid)
        if cached and (time.monotonic() - cached[0]) < _LIVE_STATUS_MAX_STALE_SEC:
            return cached[1]
        return {"live": [], "channel_id": cid}
    # Pool unavailable (shutdown/stub): refresh synchronously — the watchdog
    # needs current state to start/stop captures and has no other path.
    return _refresh_channel_live_cache(cid, channel)


def _submit_refresh(
    channel_id: str,
    channel: dict,
    warm_pool: Optional[ThreadPoolExecutor] = None,
    platform_pool: Optional[ThreadPoolExecutor] = None,
) -> Optional["Future"]:
    """Submit a deduped background refresh for a channel.

    Returns the shared in-flight Future — concurrent callers (frontend poll,
    archive watchdog, warm) wait on the SAME refresh instead of double-fetching
    the rate-limited platform APIs. Returns None when the pool is unavailable
    or rejected the submit; callers then fall back to the stale-serve path.

    ``warm_pool``/``platform_pool`` let the boot warm burst widen concurrency
    temporarily; the steady-state pools (4 warm / 6 platform) stay small so
    periodic TTL-trip refreshes never slam Kick/Twitch/YouTube.
    """
    pool = warm_pool or _LIVE_WARM_POOL
    with _LIVE_STATUS_LOCK:
        fut = _LIVE_REFRESH_INFLIGHT.get(channel_id)
        if fut is not None and not fut.done():
            return fut
    try:
        if platform_pool is not None:
            fut = pool.submit(
                _refresh_channel_live_cache, channel_id, channel, platform_pool
            )
        else:
            # 2-arg call keeps existing monkeypatched signatures working
            # (tests stub _refresh_channel_live_cache(cid, channel)).
            fut = pool.submit(_refresh_channel_live_cache, channel_id, channel)
    except Exception:
        logger.debug("live refresh submit failed for %s", channel_id, exc_info=True)
        return None
    if fut is None:  # stubbed/unavailable pool (tests, shutdown)
        return None
    with _LIVE_STATUS_LOCK:
        # A concurrent caller may have inserted a refresh while we were
        # submitting — reuse it instead of stacking a duplicate fetch.
        existing = _LIVE_REFRESH_INFLIGHT.get(channel_id)
        if existing is not None and not existing.done():
            return existing
        _LIVE_REFRESH_INFLIGHT[channel_id] = fut
    fut.add_done_callback(
        lambda f, cid=channel_id: _LIVE_REFRESH_INFLIGHT.pop(cid, None)
        if _LIVE_REFRESH_INFLIGHT.get(cid) is f else None
    )
    return fut


def _refresh_channel_live_cache(
    channel_id: str,
    channel: dict,
    platform_pool: Optional[ThreadPoolExecutor] = None,
) -> dict:
    """Re-fetch a channel's live status and update the cache. Returns the payload."""
    try:
        if platform_pool is not None:
            payload = _fetch_channel_live_payload(channel, platform_pool)
        else:
            # 1-arg call keeps existing monkeypatched signatures working
            # (tests stub _fetch_channel_live_payload(channel)).
            payload = _fetch_channel_live_payload(channel)
    except Exception as exc:
        logger.debug("live_status refresh failed for %s: %s", channel_id, exc)
        # Keep stale cache rather than wiping it; transient failures shouldn't
        # erase a confirmed-live state from the UI — but only within the
        # max-stale bound; older than that, report unknown (empty).
        with _LIVE_STATUS_LOCK:
            cached = _LIVE_STATUS_CACHE.get(channel_id)
        if cached and (time.monotonic() - cached[0]) < _LIVE_STATUS_MAX_STALE_SEC:
            # Only stale-serve if channel was known live, never stale "not live"
            cached_live = cached[1].get("live", [])
            if cached_live:
                return cached[1]
        return {"live": [], "channel_id": channel_id}
    with _LIVE_STATUS_LOCK:
        _LIVE_STATUS_CACHE[channel_id] = (time.monotonic(), payload)
        _LIVE_RECENTLY_ADDED.pop(channel_id, None)  # fast-path consumed
    return payload


def warm_channel_live_status(channel_id: str) -> None:
    """Kick a background refresh for one channel. Used by the polling frontend
    to refresh on every tick without waiting on a synchronous extract."""
    settings = settings_mgr.get()
    channel: Optional[dict] = None
    for ch in (settings.saved_channels or []):
        if str(ch.get("id")) == str(channel_id):
            channel = ch
            break
    if channel is None:
        return
    _submit_refresh(str(channel_id), channel)


def _assert_live_cache_populated(channel_id: str, deadline_sec: float = 10.0) -> None:
    """Background verifier: poll until channel_id appears in _LIVE_STATUS_CACHE
    or deadline elapses. Runs on a daemon thread so it never blocks the caller."""
    deadline = time.monotonic() + deadline_sec
    while time.monotonic() < deadline:
        with _LIVE_STATUS_LOCK:
            if channel_id in _LIVE_STATUS_CACHE:
                return
        time.sleep(0.2)
    # Assertion failed — log loudly so the issue is visible in tests/logs.
    with _LIVE_STATUS_LOCK:
        present = channel_id in _LIVE_STATUS_CACHE
    assert present, (
        f"trigger_live_detection: {channel_id} not in _LIVE_STATUS_CACHE "
        f"after {deadline_sec}s — live badge will be delayed"
    )


def trigger_live_detection(channel_id: str) -> None:
    """Immediately kick a background refresh for ONE newly-added channel.

    Called by the settings router when a channel is added via POST /api/settings
    so the frontend sees its LIVE badge within ~10s instead of waiting for the
    next 60s poll cycle.  Also stamps the fast-path so the first cache read
    after add skips the TTL check and always fetches fresh.
    """
    with _LIVE_STATUS_LOCK:
        _LIVE_RECENTLY_ADDED[channel_id] = time.monotonic()
    warm_channel_live_status(channel_id)
    # Verify the background refresh lands in cache within 10s (assert req 4).
    t = threading.Thread(
        target=_assert_live_cache_populated, args=(channel_id, 10.0), daemon=True
    )
    t.start()


def _reap_burst_pools(warm_pool: ThreadPoolExecutor, plat_pool: ThreadPoolExecutor) -> None:
    """Daemon reaper for the boot burst: wait for the burst to drain (each
    warm worker waits on its platform futures), then close both pools. Runs
    on a daemon thread so a slow yt-dlp retry can never hold the live-warm
    thread or the completion log hostage."""
    try:
        warm_pool.shutdown(wait=True)
    finally:
        plat_pool.shutdown(wait=False)


def warm_all_saved_channel_live_status() -> None:
    """Pre-warm the live-status cache for every saved channel at server startup.

    Runs on the daemon warm thread spawned from app.py lifespan — never blocks
    the API. The first detection after boot is BURST: a temporary pool wide
    enough to fetch every channel in ~1 wave, so the LIVE badges paint as
    soon as the user opens the Channels tab. The steady-state pools (4 warm /
    6 platform) stay small for periodic TTL-trip refreshes — those pace the
    rate-limited platform APIs. Measured on 19 channels (2026-08):
    4x6 = 18.6-24.5s, 8x12 = 10.6-14.2s, 12x18 = 12.5-13.7s, 16x24 = 29.7s
    (the widest burst thunders the herd and trips YouTube rate limits, so
    bigger is NOT better). 8x12 is the sweet spot.

    The burst is fire-and-forget: this function submits all channels then
    returns immediately (never waits on the pools — a slow yt-dlp retry
    could hold the log line hostage for minutes). The cache fills
    incrementally as each channel settles, and the /api/channels/{id}/live
    endpoint returns whatever the cache has (empty cold-miss + background
    refresh otherwise), so the frontend's first 3s polls pick each channel
    up as its entry lands.
    """
    try:
        settings = settings_mgr.get()
    except Exception:
        logger.debug("live warm: settings unavailable", exc_info=True)
        return
    channels = settings.saved_channels or []
    if not channels:
        logger.debug("live warm: no saved channels")
        return
    # Tests (and embedders) monkeypatch _LIVE_WARM_POOL with a stub pool that
    # never executes; the burst pools below would bypass it and run real
    # platform fetches. Fall back to the old queued path whenever the warm
    # pool isn't a real ThreadPoolExecutor.
    if not isinstance(_LIVE_WARM_POOL, ThreadPoolExecutor):
        count = 0
        for ch in channels:
            cid = str(ch.get("id") or "")
            if not cid:
                continue
            if _submit_refresh(cid, ch) is not None:
                count += 1
        if count:
            logger.info("live warm: %d channel(s) queued", count)
        return
    n_chan = min(len(channels), 8)
    n_plat = min(len(channels) * 3, 12)
    warm_pool = ThreadPoolExecutor(max_workers=n_chan, thread_name_prefix="live-boot-warm")
    plat_pool = ThreadPoolExecutor(max_workers=n_plat, thread_name_prefix="live-boot-plat")
    count = 0
    try:
        for ch in channels:
            cid = str(ch.get("id") or "")
            if not cid:
                continue
            if _submit_refresh(cid, ch, warm_pool=warm_pool, platform_pool=plat_pool) is not None:
                count += 1
    finally:
        # Reap in a daemon thread, do NOT shut down here: an immediate
        # shutdown(wait=False) makes the pools reject the warm workers'
        # platform submits mid-burst ("cannot schedule new futures after
        # shutdown") and silently drops channels. The reaper waits for the
        # burst to drain, then closes both pools — meanwhile the live-warm
        # thread returns at once and the cache fills incrementally as each
        # channel settles (Kick ~0.7s, Twitch ~2s, YouTube ~6s), which is
        # exactly the fast-first-detection behavior wanted; the frontend's
        # 3s polls pick each channel up as its entry lands.
        threading.Thread(
            target=_reap_burst_pools,
            args=(warm_pool, plat_pool),
            daemon=True,
            name="live-boot-reap",
        ).start()
    if count:
        logger.info("live warm: %d channel(s) burst launched (%dx%d)", count, n_chan, n_plat)


@router.get("/channels/{channel_id}/live")
def channel_live_status(channel_id: str) -> dict:
    """Aggregate live status for a saved channel across all platforms.

    Returns the cached payload (warm or last-refreshed) immediately while it
    is younger than the TTL. On a TTL-trip it kicks a refresh and WAITS for
    it (bounded by ``_LIVE_REFRESH_WAIT_SEC``) so this poll returns the
    FRESH payload — the poll that crosses the TTL is the one that updates the
    badge. The old fire-and-forget path served the stale payload here and the
    frontend only saw the refresh on its next poll (60s later), which made
    LIVE badges lag a full poll cycle behind the streamer going live. A true
    cold miss returns an empty payload instantly and lets the warm pool fill
    the cache. The response shape is unchanged from the prior synchronous
    version: ``{"live": [...], "channel_id": ...}``.
    """
    settings = settings_mgr.get()
    channel: Optional[dict] = None
    for ch in (settings.saved_channels or []):
        if str(ch.get("id")) == str(channel_id):
            channel = ch
            break

    if channel is None:
        raise HTTPException(404, "Channel not found")

    cid = str(channel_id)
    now = time.monotonic()
    with _LIVE_STATUS_LOCK:
        cached = _LIVE_STATUS_CACHE.get(cid)

    if cached:
        ts, payload = cached
        if (now - ts) < _LIVE_STATUS_TTL_SEC:
            return payload
        # TTL-trip: WAIT (bounded) on the kicked refresh and return the FRESH
        # payload. The old fire-and-forget path returned the stale payload
        # here, so the frontend only saw the refreshed state on its NEXT poll
        # (60s later) — LIVE badges lagged a full poll cycle behind the
        # streamer going live, and since MAX_STALE == TTL the stale entry was
        # served as empty, blinking the badge out every cycle. Waiting makes
        # the poll that crosses the TTL the one that updates the badge.
        fut = _submit_refresh(cid, channel)
        if fut is not None:
            try:
                return fut.result(timeout=_LIVE_REFRESH_WAIT_SEC)
            except TimeoutError:
                logger.debug("live status refresh wait timed out for %s", cid)
            except Exception as exc:
                logger.debug("live status refresh wait failed for %s: %s", cid, exc)
        # Refresh unavailable/timed out: serve last-known only within the
        # max-stale bound (never a days-old "LIVE" lie); older → unknown.
        if (time.monotonic() - ts) >= _LIVE_STATUS_MAX_STALE_SEC:
            payload = {"live": [], "channel_id": cid}
        return payload

    # True cold miss (boot before the warm finishes): return empty instantly
    # and schedule the background refresh.
    _submit_refresh(cid, channel)
    return {"live": [], "channel_id": cid}


# ---------------------------------------------------------------------------
# Live download endpoint — registered in downloads.py router
# ---------------------------------------------------------------------------


def _build_live_output_path(title: str, platform: str, channel: str) -> str:
    """Build output filename for a live recording.

    ponytail: reuses UserSettings output dir. Upgrade to a dedicated output dir
    for DVR recordings when the settings schema supports it.
    """
    from pathlib import Path
    import re

    settings = settings_mgr.get()
    out_dir = Path(settings.output_folder or "downloads")
    safe_ch = re.sub(r"[^\w.-]", "_", (channel or "live").strip())
    safe_title = re.sub(r"[^\w.-]", "_", (title or "stream").strip())
    out_dir.mkdir(parents=True, exist_ok=True)
    # Sequential number dedup
    candidate = out_dir / f"{safe_ch}_{safe_title}_{platform}.mp4"
    counter = 1
    while candidate.exists():
        candidate = out_dir / f"{safe_ch}_{safe_title}_{platform}_{counter}.mp4"
        counter += 1
    return str(candidate)


# ---------------------------------------------------------------------------
# Live chat stream (per-viewer SSE) + fast-clip capability
# ---------------------------------------------------------------------------
#
# The livestream popup docks a LIVE chat panel right of the video. There is no
# archived chat for a viewer session, so each SSE connection builds a fresh
# per-viewer sink (reusing the SAME capture classes as the archive watchdog —
# Twitch anon IRC / Kick Pusher / yt-dlp live_chat) with a flush callback that
# pushes rows into the connection's asyncio queue instead of writing to the
# archive. `video_id` is viewer-scoped (never a saved video id), so a viewer
# stream can never collide with or pollute archive rows. Disconnect stops the
# sink (interrupting the IRC socket / pusher ws / yt-dlp process).

_CHAT_PLATFORM_KWARG = {"twitch": "login", "kick": "slug", "youtube": "handle"}
_CHAT_FLUSH_INTERVAL_SEC = 1.0  # live panel needs near-real-time, not the 5s archive cadence
_CHAT_FLUSH_MAX_ROWS = 20


def _build_viewer_chat_sink(platform: str, slug: str, push) -> "object":
    """Create a per-viewer chat sink whose flushes forward rows to ``push``.

    Mirrors the archive watchdog's sink factory (login/slug/handle per
    platform) but with a viewer-scoped video_id and a flush callback instead
    of the archive writer. Lazy-imported so this router never pulls chat-sink
    deps (websockets/yt-dlp) into the app boot path unless a stream is open.
    """
    from services.chat_sinks import SINKS

    cls = SINKS[platform]
    kwargs = {
        "video_id": f"viewer-{platform}-{slug}-{uuid.uuid4().hex[:8]}",
        "channel": slug,
        "title": "",
        "stream_start_ts": None,
        "flush_interval": _CHAT_FLUSH_INTERVAL_SEC,
        "flush_max": _CHAT_FLUSH_MAX_ROWS,
        "flush_cb": push,
    }
    kwargs[_CHAT_PLATFORM_KWARG[platform]] = slug
    return cls(**kwargs)


# One shared upstream ChatSink per (platform, slug), ref-counted by every
# live SSE viewer of that channel. Without fanout each viewer spawned its
# own upstream socket + two daemon threads; with it, one sink + one socket
# serves all viewers, torn down when the last one disconnects. Rows flush
# on the SINK's flush cadence and are broadcast to every subscriber queue.
_CHAT_FANOUT_LOCK = threading.Lock()
_CHAT_FANOUT: "dict[tuple[str, str], _ChatFanout]" = {}


class _ChatFanout:
    """Ref-counted fan-out of one shared ChatSink to many viewer queues.

    First subscriber builds the sink (via _build_viewer_chat_sink) and
    starts it; each flush broadcasts the row batch to every subscribed
    queue. Last unsubscribe stops the shared sink and the fanout drops out
    of the registry, so no upstream socket or daemon thread outlives the
    viewers."""

    def __init__(self, platform: str, slug: str, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self._refcount = 0
        self._subscribers: "set[asyncio.Queue]" = set()
        self._sink = None  # built lazily on first subscriber
        self._sink_lock = threading.Lock()

        def _broadcast(rows) -> int:
            # Runs on the sink's flush thread — hop to the loop, then fan
            # the batch out to every subscriber queue. Returning len(rows)
            # keeps the ChatSink base flush accounting correct.
            with self._sink_lock:
                targets = tuple(self._subscribers)
            if targets:
                self.loop.call_soon_threadsafe(
                    lambda: [q.put_nowait(rows) for q in targets]
                )
            return len(rows)

        self._broadcast = _broadcast
        self._platform = platform
        self._slug = slug

    def start(self) -> None:
        """Bump the refcount; start the shared sink on the first viewer."""
        if self._refcount == 0:
            self._sink = _build_viewer_chat_sink(self._platform, self._slug, self._broadcast)
            self._sink.start()
        self._refcount += 1

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        with self._sink_lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Drop a viewer; stop + unregister the shared sink on the last one."""
        with self._sink_lock:
            self._subscribers.discard(q)
            self._refcount -= 1
            last = self._refcount <= 0
            sink = self._sink
            self._sink = None
        if last and sink is not None:
            sink.stop()
            with _CHAT_FANOUT_LOCK:
                _CHAT_FANOUT.pop((self._platform, self._slug), None)


@router.get("/live/chat/stream")
async def live_chat_stream(
    request: Request,
    platform: str = Query(..., description="twitch | kick | youtube"),
    slug: str = Query(..., description="chat room slug (login / slug / @handle)"),
):
    """SSE stream of LIVE chat rows for one channel (shared fanout).

    All viewers of a channel subscribe to ONE shared _ChatFanout (one
    upstream ChatSink + socket + two daemon threads), flushed every ~1s;
    rows are broadcast to every viewer queue and forwarded as ``data: {…}``
    frames. Same keepalive/disconnect contract as the download stream
    endpoint. The shared sink dies when the last viewer disconnects.
    """
    plat = (platform or "").lower()
    if plat not in _CHAT_PLATFORM_KWARG or not slug:
        raise HTTPException(status_code=400, detail="platform must be one of twitch/kick/youtube and slug is required")

    key = (plat, slug)
    with _CHAT_FANOUT_LOCK:
        fanout = _CHAT_FANOUT.get(key)
        if fanout is None:
            fanout = _ChatFanout(plat, slug, asyncio.get_running_loop())
            _CHAT_FANOUT[key] = fanout

    queue = fanout.subscribe()
    return StreamingResponse(
        _chat_sse_gen(request, queue, fanout),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


async def _chat_sse_gen(request: Request, queue: asyncio.Queue, fanout) -> "AsyncGenerator[str, None]":
    """SSE body generator: start the shared fanout, forward flushed row
    batches, and keep the connection alive with comment frames. Extracted
    so tests can drive it directly (ASGITransport buffers the whole
    infinite body). On teardown we unsubscribe this viewer's queue; the
    fanout stops the shared sink when the LAST viewer leaves."""
    try:
        fanout.start()
        while True:
            if await request.is_disconnected():
                break
            try:
                rows = await asyncio.wait_for(queue.get(), timeout=15)
                for row in rows:
                    yield f"data: {json.dumps(row, ensure_ascii=False)}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    finally:
        fanout.unsubscribe(queue)



@router.get("/chat/emotes")
def chat_emotes(
    platform: Optional[str] = Query(None, description="twitch | kick | youtube"),
    slug: Optional[str] = Query(None, description="channel login"),
) -> dict:
    """Custom + official emotes for one channel (render-only; Twitch + Kick).

    Twitch returns BTTV/FFZ/7TV customs plus official Twitch globals, merged
    in Chatterino priority order (FFZ channel > BTTV channel > 7TV channel >
    FFZ global > BTTV global > 7TV global > Twitch global); name collisions
    keep the first (highest-priority) provider, so channel/global customs
    shadow official Twitch emotes like LUL. Kick returns 7TV channel emotes
    plus BTTV/7TV custom globals (no FFZ, no official Twitch emotes).
    Unknown platform or network failure returns ``{"emotes": []}`` — chat
    rendering must never break because emotes fail. Emotes are display-only:
    stored message text and search indexes are never touched. The service is
    lazy-imported so this router never pulls chat-emote deps into the app
    boot path.
    """
    if not platform or not slug:
        raise HTTPException(status_code=400, detail="platform and slug are required")
    from services.chat_emotes import fetch_emotes

    return {"emotes": fetch_emotes(platform, slug)}


@router.get("/chat/history")
def chat_history(
    platform: str = Query(..., description="twitch | kick | youtube"),
    slug: str = Query(..., description="channel login"),
    limit: int = Query(50, ge=1, le=500, description="max backlog rows"),
) -> dict:
    """Archived chat captured BEFORE the current session for one channel —
    the Chatterino-style backlog the live panel pre-fills on open (e.g. a
    channel added now still shows earlier captured chat). Spans every
    archived video of the channel (watchdog live captures included, matched
    case-insensitively on videos.channel), ordered oldest→newest. Best-
    effort: empty DB / no captures / unknown channel → ``{"messages": []}``,
    never 500 — the panel treats the backlog as optional. The service is
    lazy-imported so this router never pulls archive deps into boot.
    """
    plat = (platform or "").lower()
    if plat not in _CHAT_PLATFORM_KWARG or not slug:
        raise HTTPException(status_code=400, detail="platform must be one of twitch/kick/youtube and slug is required")
    from services.archive_db import chat_history_for_channel

    return {"messages": chat_history_for_channel(plat, slug, limit)}


class FastClipRequest(BaseModel):
    platform: str = Field(..., description="twitch | kick | youtube")
    slug: str = Field(..., description="channel slug / login / @handle")
    duration_sec: int = Field(30, ge=1, le=60, description="clip duration in seconds (1..60)")


@router.post("/live/clip")
def fast_live_clip(req: FastClipRequest) -> dict:
    """Fast live-clip: Kick/YouTube cut via ffmpeg, Twitch via browser editor.

    Kick/YouTube resolve the live HLS master, ffmpeg -c copy to a temp file
    and return available:true only if the file exists and is >1 KiB. Twitch
    still routes to the browser clip editor (cookie flow) and always returns
    available:false with the needed cookie hint.
    Offloaded via to_thread-capable helper: callers should still rate-limit.
    Temp files older than 24h are reaped on each request.
    """
    plat = (req.platform or "").lower()
    slug = (req.slug or "").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="channel is required")
    # whitelist slug charset — prevents path-injection via slug "a/b"
    import re as _re
    if plat in ("twitch", "kick"):
        if not _re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", slug):
            raise HTTPException(status_code=400, detail="invalid channel slug")
    elif plat == "youtube":
        if not _re.fullmatch(r"@?[A-Za-z0-9_\.\-]{1,64}", slug):
            raise HTTPException(status_code=400, detail="invalid channel slug")
    if plat not in ("twitch", "kick", "youtube"):
        raise HTTPException(status_code=400, detail="platform must be one of twitch/kick/youtube")
    if plat == "twitch":
        return {
            "available": False,
            "reason": "Twitch live clips are created in the browser clip editor (cookie extension flow).",
            "needed": [
                "Logged-in Twitch session cookie in the browser",
                "VOD.RIP cookie extension (clip_assist content script)",
            ],
        }
    try:
        from services.live_capture import kick_live_info, youtube_live_info
        from services.ytdlp_ffmpeg import _resolve_ffmpeg_exe
        import subprocess, tempfile, time as _tm
        from pathlib import Path as _P
        # reap stale clips (>24h) — best-effort, keeps temp bounded
        try:
            _clip_dir = _P(tempfile.gettempdir()) / "VOD.RIP-live-clips"
            if _clip_dir.exists():
                _now = _tm.time()
                for _f in _clip_dir.iterdir():
                    try:
                        if _now - _f.stat().st_mtime > 86400:
                            _f.unlink(missing_ok=True)
                    except Exception:
                        pass
        except Exception:
            pass
        info = (kick_live_info if plat == "kick" else youtube_live_info)(slug)
        if not info or not info.get("url"):
            reason = (info or {}).get("reason") or f"{plat} channel offline or no live master"
            return {"available": False, "reason": reason, "needed": [reason]}
        ffmpeg = _resolve_ffmpeg_exe()
        out_dir = _P(tempfile.gettempdir()) / "VOD.RIP-live-clips"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in slug)[:40]
        out = out_dir / f"{plat}_{safe}_{req.duration_sec}s.mp4"
        cmd = [ffmpeg, "-y", "-i", info["url"], "-t", str(req.duration_sec), "-c", "copy", str(out)]
        hdrs = info.get("headers") or {}
        if hdrs:
            hdr_str = "\r\n".join(f"{k}: {v}" for k, v in hdrs.items()) + "\r\n"
            idx = cmd.index("-i")
            cmd[idx:idx] = ["-headers", hdr_str]
        # ponytail: runs synchronously on the worker thread; upgrade to BackgroundTasks or capped threadpool when clipping rate grows.
        proc = subprocess.run(cmd, capture_output=True, timeout=30 + req.duration_sec)
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 1024:
            return {"available": True, "path": str(out), "reason": "local HLS segment"}
        err = (proc.stderr or b"").decode("utf-8", "replace")[-400:]
        return {"available": False, "reason": f"ffmpeg clip failed: {err or proc.returncode}", "needed": ["ffmpeg available and a live stream"]}
    except Exception as exc:
        return {"available": False, "reason": str(exc)[:400], "needed": [str(exc)[:80]]}
