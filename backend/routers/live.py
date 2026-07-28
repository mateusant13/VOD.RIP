"""Live-stream info and DVR endpoints."""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from services.live_capture import (  
    download_live_stream,
    kick_live_info,
    twitch_live_info,
    youtube_live_info,
)
from deps import settings_mgr

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


@router.get("/channels/{channel_id}/live")
def channel_live_status(channel_id: str) -> dict:
    """Aggregate live status for a saved channel across all platforms.

    Inspects the channel's ``kickSlug``, ``twitchSlug``, and ``youtubeSlug``
    and returns a list of currently-live streams.
    """
    settings = settings_mgr.get()
    channel: Optional[dict] = None
    for ch in (settings.saved_channels or []):
        if str(ch.get("id")) == str(channel_id):
            channel = ch
            break

    if channel is None:
        raise HTTPException(404, "Channel not found")

    live: list[LiveStatus] = []

    # Kick
    ks = (channel.get("kickSlug") or "").strip()
    if ks:
        info = kick_live_info(ks)
        if info and info.get("url"):
            live.append(LiveStatus(
                is_live=True,
                platform="Kick",
                title=info.get("title", ""),
                viewers=info.get("viewers", 0),
                url=info.get("url", ""),
                headers=info.get("headers", {}),
                type="hls",
            ))

    # Twitch
    ts = (channel.get("twitchSlug") or "").strip()
    if ts:
        info = twitch_live_info(ts)
        if info and info.get("url"):
            live.append(LiveStatus(
                is_live=True,
                platform="Twitch",
                title=info.get("title", ""),
                viewers=info.get("viewers", 0),
                url=info.get("url", ""),
                headers=info.get("headers", {}),
                type="hls",
            ))

    # YouTube
    ys = (channel.get("youtubeSlug") or "").strip()
    if ys:
        info = youtube_live_info(ys)
        if info and isinstance(info, dict) and info.get("url"):
            live.append(LiveStatus(
                is_live=True,
                platform="YouTube",
                title=info.get("title", ""),
                viewers=info.get("viewers", 0),
                url=info.get("url", ""),
                headers=info.get("headers", {}),
                type="hls",
            ))

    return {"live": [s.model_dump() for s in live], "channel_id": channel_id}


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
