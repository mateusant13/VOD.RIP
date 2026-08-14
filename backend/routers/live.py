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
_CAPTION_PLATFORMS = ("twitch", "kick")


@router.get("/live/captions")
async def live_captions_stream(
    request: Request,
    platform: str = Query(..., description="twitch | kick"),
    channel: str = Query(..., description="channel slug / login"),
):
    """SSE stream of live caption blocks for one channel.

    Emits ``event: caption`` frames (``{text, start, end}``) plus keepalive
    comments; a confirmed ``event: offline`` ends the stream (the frontend
    hides the overlay). 503 when the parakeet engine is unavailable — the
    frontend probes /available first and never opens the stream then. The
    captioner's refcount drops when the connection closes (generator finally).
    """
    plat = (platform or "").lower()
    chan = (channel or "").strip().lower()
    if plat not in _CAPTION_PLATFORMS or not chan:
        raise HTTPException(status_code=400, detail="platform must be one of twitch/kick and channel is required")
    from services.live_captions import captions_available, get_captioner

    ok, reason = await asyncio.to_thread(captions_available, plat)
    if not ok:
        raise HTTPException(status_code=503, detail=reason)

    captioner = get_captioner(plat, chan, asyncio.get_running_loop())
    captioner.acquire()
    try:
        return StreamingResponse(
            _captions_sse_gen(request, captioner),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    except Exception:
        captioner.release()
        raise


@router.get("/live/captions/available")
async def live_captions_available(
    platform: str = Query(..., description="twitch | kick"),
    channel: str = Query(..., description="channel slug / login"),
) -> dict:
    """Parakeet-gate probe: ``{available, reason}`` — the popup renders the
    CC toggle only when available is true (and never opens the stream 503)."""
    plat = (platform or "").lower()
    chan = (channel or "").strip().lower()
    if plat not in _CAPTION_PLATFORMS or not chan:
        raise HTTPException(status_code=400, detail="platform must be one of twitch/kick and channel is required")
    from services.live_captions import captions_available

    ok, reason = await asyncio.to_thread(captions_available, plat)
    return {"available": ok, "reason": reason or None}


async def _captions_sse_gen(request: Request, captioner) -> "AsyncGenerator[str, None]":
    """SSE body generator: forward caption blocks, keepalive comments, and
    release the captioner refcount when the connection closes. Extracted so
    tests can drive it directly (ASGITransport buffers the whole body)."""
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                event, data = await asyncio.wait_for(captioner.events.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event == "offline":
                yield "event: offline\ndata: {}\n\n"
                break
            yield f"event: caption\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    finally:
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


@router.get("/live/chat/stream")
async def live_chat_stream(
    request: Request,
    platform: str = Query(..., description="twitch | kick | youtube"),
    slug: str = Query(..., description="chat room slug (login / slug / @handle)"),
):
    """SSE stream of LIVE chat rows for one channel (per-viewer capture).

    Each connection owns a ChatSink; rows flush every ~1s and are forwarded
    as ``data: {…}`` frames. Same keepalive/disconnect contract as the
    download stream endpoint. The sink dies with the connection.
    """
    plat = (platform or "").lower()
    if plat not in _CHAT_PLATFORM_KWARG or not slug:
        raise HTTPException(status_code=400, detail="platform must be one of twitch/kick/youtube and slug is required")

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _push(rows) -> int:
        # ChatSink base sums the returned count into rows_flushed — returning
        # None (bare call_soon_threadsafe) makes every flush raise TypeError.
        loop.call_soon_threadsafe(queue.put_nowait, rows)
        return len(rows)

    sink = _build_viewer_chat_sink(plat, slug, _push)

    return StreamingResponse(
        _chat_sse_gen(request, queue, sink),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


async def _chat_sse_gen(request: Request, queue: asyncio.Queue, sink) -> "AsyncGenerator[str, None]":
    """SSE body generator: start the sink, forward flushed row batches, and
    keep the connection alive with comment frames. Extracted so tests can
    drive it directly (ASGITransport buffers the whole infinite body)."""
    try:
        sink.start()
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
        sink.stop()



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
    """Fast-clip capability report — HONEST, never fakes a clip.

    There is NO server-side live clip path in this build (audited):
    - Twitch: the app routes live clips to Twitch's own browser editor
      (twitch.tv/<channel> → player Clip button) driven by the VOD.RIP cookie
      extension with the session cookie — no Helix token/scopes, no server
      mutation. The frontend never calls this endpoint for Twitch anymore.
    - Kick: has no public clip-creation API.
    - YouTube: has no public live-clip API.
    So the payload always reports ``available: false`` with the exact
    requirement the UI surfaces (never a fabricated clip id).
    """
    plat = (req.platform or "").lower()
    if plat == "twitch":
        return {
            "available": False,
            "reason": "Twitch live clips are created in the browser clip editor (cookie extension flow).",
            "needed": [
                "Logged-in Twitch session cookie in the browser",
                "VOD.RIP cookie extension (clip_assist content script)",
            ],
        }
    if plat == "kick":
        return {
            "available": False,
            "reason": "Kick has no public clip-creation API.",
            "needed": ["No public Kick endpoint — a first-party account flow would be required"],
        }
    if plat == "youtube":
        return {
            "available": False,
            "reason": "YouTube has no public live-clip API.",
            "needed": ["No public YouTube endpoint for live clip creation"],
        }
    raise HTTPException(status_code=400, detail="platform must be one of twitch/kick/youtube")
