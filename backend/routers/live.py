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


def _fetch_channel_live_payload(channel: dict) -> dict:
    """Build the response payload for a single channel's live status.

    Kicks off the channel's three platform fetchers (Kick/Twitch/YouTube)
    CONCURRENTLY on the shared platform pool — serializing them cost
    sum(3-15s) per channel instead of max(3-15s). The returned dict matches
    the live router response: ``{"live": [...], "channel_id": ...}``.
    """
    ks = (channel.get("kickSlug") or "").strip()
    ts = (channel.get("twitchSlug") or "").strip()
    ys = (channel.get("youtubeSlug") or "").strip()

    jobs: list[tuple[str, "object"]] = []
    if ks:
        jobs.append(("kick", _PLATFORM_FETCH_POOL.submit(kick_live_info, ks)))
    if ts:
        jobs.append(("twitch", _PLATFORM_FETCH_POOL.submit(twitch_live_info, ts)))
    if ys:
        jobs.append(("youtube", _PLATFORM_FETCH_POOL.submit(youtube_live_info, ys)))

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


def _submit_refresh(channel_id: str, channel: dict) -> Optional["Future"]:
    """Submit a deduped background refresh for a channel.

    Returns the shared in-flight Future — concurrent callers (frontend poll,
    archive watchdog, warm) wait on the SAME refresh instead of double-fetching
    the rate-limited platform APIs. Returns None when the pool is unavailable
    or rejected the submit; callers then fall back to the stale-serve path.
    """
    with _LIVE_STATUS_LOCK:
        fut = _LIVE_REFRESH_INFLIGHT.get(channel_id)
        if fut is not None and not fut.done():
            return fut
    try:
        fut = _LIVE_WARM_POOL.submit(_refresh_channel_live_cache, channel_id, channel)
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


def _refresh_channel_live_cache(channel_id: str, channel: dict) -> dict:
    """Re-fetch a channel's live status and update the cache. Returns the payload."""
    try:
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


def warm_all_saved_channel_live_status() -> None:
    """Pre-warm the live-status cache for every saved channel at server startup.

    Runs on the daemon warm thread spawned from app.py lifespan — never blocks
    the API. Each channel's extract happens concurrently across the dedicated
    pool (4 workers). The /api/channels/{id}/live endpoint becomes O(1) for
    the first user request after boot.
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
    count = 0
    for ch in channels:
        cid = str(ch.get("id") or "")
        if not cid:
            continue
        if _submit_refresh(cid, ch) is not None:
            count += 1
    if count:
        logger.info("live warm: %d channel(s) queued", count)


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
    sink = _build_viewer_chat_sink(plat, slug, lambda rows: loop.call_soon_threadsafe(queue.put_nowait, rows))

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


class FastClipRequest(BaseModel):
    platform: str = Field(..., description="twitch | kick | youtube")
    slug: str = Field(..., description="channel slug / login / @handle")
    duration_sec: int = Field(30, ge=1, le=60, description="clip duration in seconds (1..60)")


@router.post("/live/clip")
def fast_live_clip(req: FastClipRequest) -> dict:
    """Fast-clip capability report — HONEST, never fakes a clip.

    There is NO server-side live clip path in this build (audited):
    - Twitch: Helix ``POST /helix/clips`` needs an OAuth user token with the
      ``clips:edit`` scope + a Client-Id header; the old implementation was
      removed in c609e93 and no OAuth client creds remain. The session-cookie
      auth this app uses is not accepted by Helix.
    - Kick: has no public clip-creation API.
    - YouTube: has no public live-clip API.
    So the payload always reports ``available: false`` with the exact
    requirement the UI surfaces (never a fabricated clip id).
    """
    plat = (req.platform or "").lower()
    if plat == "twitch":
        return {
            "available": False,
            "reason": "Twitch live clips need a Helix OAuth user token with clips:edit scope (POST /helix/clips).",
            "needed": [
                "OAuth user token with clips:edit scope",
                "Client-Id header",
                "POST https://api.twitch.tv/helix/clips",
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
