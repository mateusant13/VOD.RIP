"""Live-stream info and DVR endpoints."""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

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
# of LiveStatus dicts) keyed by channel_id with a 60s TTL; reads return the
# cached payload instantly and trigger a background refresh if stale. On
# server startup we pre-warm the cache for every saved channel so the very
# first user request hits the warm cache.
#
# Concurrency: refreshes run on a dedicated 4-worker thread pool. We bound
# concurrency so a 20-channel saved list does not slam YouTube at boot.

_LIVE_STATUS_CACHE: dict[str, tuple[float, dict]] = {}
_LIVE_STATUS_TTL_SEC = 60.0
# Serving a "LIVE" badge older than this is a lie: when refreshes keep
# failing (platform outage, parse breakage) the stale-serve path must stop
# returning ancient payloads and report unknown (empty) instead.
_LIVE_STATUS_MAX_STALE_SEC = 600.0
_LIVE_STATUS_LOCK = threading.Lock()
_LIVE_WARM_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="live-warm")


def _fetch_channel_live_payload(channel: dict) -> dict:
    """Build the response payload for a single channel's live status.

    Reads all three platform fetchers (Kick/Twitch/YouTube) sequentially —
    YouTube is the slowest so it dominates wall time, but the per-channel
    fetch is parallel across the pool. The returned dict matches the live
    router response: ``{"live": [...], "channel_id": ...}``.
    """
    live: list[dict] = []
    ks = (channel.get("kickSlug") or "").strip()
    if ks:
        info = kick_live_info(ks)
        if info and info.get("url"):
            live.append({
                "is_live": True,
                "platform": "Kick",
                "title": info.get("title", ""),
                "viewers": info.get("viewers", 0),
                "url": info.get("url", ""),
                "headers": info.get("headers", {}),
                "type": "hls",
            })
    ts = (channel.get("twitchSlug") or "").strip()
    if ts:
        info = twitch_live_info(ts)
        if info and info.get("url"):
            live.append({
                "is_live": True,
                "platform": "Twitch",
                "title": info.get("title", ""),
                "viewers": info.get("viewers", 0),
                "url": info.get("url", ""),
                "headers": info.get("headers", {}),
                "type": "hls",
                # vaft rotation extras — frontend ignores unknown keys.
                "player_type": info.get("player_type", "embed"),
                "ad_free": bool(info.get("ad_free")),
                # ISO/epoch stream start — archive chat watchdog anchors
                # message offsets to it. Frontend ignores unknown keys.
                "started_at": info.get("started_at"),
            })
    ys = (channel.get("youtubeSlug") or "").strip()
    if ys:
        info = youtube_live_info(ys)
        if info and isinstance(info, dict) and info.get("url"):
            live.append({
                "is_live": True,
                "platform": "YouTube",
                "title": info.get("title", ""),
                "viewers": info.get("viewers", 0),
                "url": info.get("url", ""),
                "headers": info.get("headers", {}),
                "type": "hls",
                # Real videoId — archive watchdog anchors chat-capture video
                # rows to it. Frontend ignores unknown keys.
                "videoId": info.get("videoId"),
            })
    return {"live": live, "channel_id": str(channel.get("id") or "")}


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
    try:
        _LIVE_WARM_POOL.submit(_refresh_channel_live_cache, str(channel_id), channel)
    except Exception:
        logger.debug("live warm submit failed for %s", channel_id, exc_info=True)


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
        try:
            _LIVE_WARM_POOL.submit(_refresh_channel_live_cache, cid, ch)
            count += 1
        except Exception:
            logger.debug("live warm submit failed for %s", cid, exc_info=True)
    if count:
        logger.info("live warm: %d channel(s) queued", count)


@router.get("/channels/{channel_id}/live")
def channel_live_status(channel_id: str) -> dict:
    """Aggregate live status for a saved channel across all platforms.

    Returns the cached payload (warm or last-refreshed) immediately and
    kicks a background refresh if the entry is older than the TTL. The
    response shape is unchanged from the prior synchronous version so the
    existing frontend contract holds: ``{"live": [...], "channel_id": ...}``.
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
    stale = True
    with _LIVE_STATUS_LOCK:
        cached = _LIVE_STATUS_CACHE.get(cid)
    if cached:
        ts, payload = cached
        stale = (now - ts) >= _LIVE_STATUS_TTL_SEC
        if not stale:
            return payload

    if stale:
        # Cache miss or stale. If we have *some* cached payload (stale) serve
        # it now and refresh in the background — best UX: the user always sees
        # last-known state within milliseconds. Beyond the max-stale bound the
        # cached state is too old to trust — block on a synchronous refresh
        # like a true cache miss.
        if cached and (now - cached[0]) < _LIVE_STATUS_MAX_STALE_SEC:
            try:
                _LIVE_WARM_POOL.submit(_refresh_channel_live_cache, cid, channel)
            except Exception:
                logger.debug("live status refresh submit failed for %s", cid, exc_info=True)
            return cached[1]
        payload = _refresh_channel_live_cache(cid, channel)
        return payload


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
