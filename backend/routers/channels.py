"""
Channel browsing routes — VODs and clips for saved Kick/Twitch channels.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException

from deps import (
    CHANNEL_CLIP_LIMIT,
    CHANNEL_CLIP_MAX_DURATION_SEC,
    CHANNEL_DAYS_DEFAULT,
    CHANNEL_EXECUTOR,
    CHANNEL_LIMIT_MAX,
    CHANNEL_VOD_FETCH_TIMEOUT_SEC,
    CLIP_FETCH_TIMEOUT_SEC,
)
from services.channel_cache import get_cached, make_channel_cache_key, set_cached
from services.kick_api_service import (
    list_channel_clips_sync as kick_list_channel_clips_sync,
    list_channel_videos_sync as kick_list_channel_videos_sync,
)
from services.twitch_gql_service import (
    list_channel_clips_sync as twitch_list_channel_clips_sync,
    list_channel_videos_sync as twitch_list_channel_videos_sync,
)
from utils import (
    filter_clip_entries,
    format_platform_error,
    looks_like_clip_entry,
    normalize_err,
    parse_video_date,
    parse_wanted_platforms,
    resolve_channel_slug,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["channels"])


async def _gather_channel_clips(
    *,
    wanted: List[str],
    kick_slug: str,
    twitch_login: str,
    limit: int,
) -> tuple[List[dict], Dict[str, str]]:
    """Fetch clips per platform using platform-specific logins."""
    per_platform_errors: Dict[str, str] = {}
    all_clips: List[dict] = []
    loop = asyncio.get_running_loop()
    kick_slug = (kick_slug or "").strip().lower()
    twitch_login = (twitch_login or "").strip().lower()

    async def _fetch_twitch() -> None:
        if not twitch_login:
            per_platform_errors["Twitch"] = "Twitch login is required"
            return
        try:
            vids = await asyncio.wait_for(
                loop.run_in_executor(
                    CHANNEL_EXECUTOR, twitch_list_channel_clips_sync, twitch_login, limit
                ),
                timeout=CLIP_FETCH_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            per_platform_errors["Twitch"] = "Clip fetch timed out — try again"
            return
        except Exception as e:
        # ponytail: best-effort — return
            per_platform_errors["Twitch"] = format_platform_error(e)
            return
        for v in vids:
            all_clips.append({
                "id": v["id"],
                "platform": "Twitch",
                "title": v.get("title") or "Untitled",
                "duration": v.get("duration"),
                "duration_string": v.get("duration_string"),
                "created_at": v.get("created_at"),
                "views": v.get("views"),
                "thumbnail_url": v.get("thumbnail_url"),
                "url": v.get("url") or f"https://clips.twitch.tv/{v['id']}",
                "channel": twitch_login,
                "content_kind": "clip",
            })

    async def _fetch_kick() -> None:
        if not kick_slug:
            per_platform_errors["Kick"] = "Kick slug is required"
            return
        try:
            vids = await asyncio.wait_for(
                loop.run_in_executor(
                    CHANNEL_EXECUTOR,
                    kick_list_channel_clips_sync,
                    f"https://kick.com/{kick_slug}/clips",
                    limit,
                ),
                timeout=CLIP_FETCH_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            per_platform_errors["Kick"] = "Clip fetch timed out — try again"
            return
        except Exception as e:
        # ponytail: best-effort — return
            per_platform_errors["Kick"] = format_platform_error(e)
            return
        for v in vids:
            all_clips.append({
                "id": v["id"],
                "platform": "Kick",
                "title": v.get("title") or "Untitled",
                "duration": v.get("duration"),
                "duration_string": v.get("duration_string"),
                "created_at": v.get("created_at"),
                "views": v.get("views"),
                "thumbnail_url": v.get("thumbnail"),
                "url": v.get("url") or f"https://kick.com/{kick_slug}/clips/{v['id']}",
                "channel": kick_slug,
                "content_kind": "clip",
            })

    tasks: List[asyncio.Task] = []
    if "Kick" in wanted:
        tasks.append(asyncio.create_task(_fetch_kick()))
    if "Twitch" in wanted:
        tasks.append(asyncio.create_task(_fetch_twitch()))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    all_clips = filter_clip_entries(all_clips)
    all_clips.sort(key=lambda v: -(v.get("views") or 0))
    for k, v in list(per_platform_errors.items()):
        per_platform_errors[k] = normalize_err(v)
    return all_clips, per_platform_errors


@router.get("/api/channel/videos")
async def channel_videos(
    url: str,
    limit: int = CHANNEL_LIMIT_MAX,
    days: int = CHANNEL_DAYS_DEFAULT,
    platforms: str = "Kick,Twitch",
    content: str = "vods",
    kick_slug: Optional[str] = None,
    twitch_login: Optional[str] = None,
):
    """Fetch archive VODs for a channel."""
    raw = unquote(url).strip()
    try:
        default_slug = resolve_channel_slug(raw) if raw else ""
        kick_ch = (kick_slug or default_slug).strip().lower()
        twitch_ch = (twitch_login or default_slug).strip().lower()
        channel = kick_ch or twitch_ch
        wanted = parse_wanted_platforms(platforms)
        content_norm = (content or "").strip().lower()
        limit_norm = max(1, min(int(limit), CHANNEL_CLIP_LIMIT if content_norm == "clips" else CHANNEL_LIMIT_MAX))
        days_norm = max(0, min(int(days), 365))
        cache_key = make_channel_cache_key(
            "videos", content_norm, kick_ch, twitch_ch, platforms, limit_norm, days_norm,
            ",".join(sorted(wanted)),
        )
        cached = get_cached(cache_key)
        if cached is not None:
            return cached
        if not wanted:
            is_clips = content_norm == "clips"
            payload = {
                "videos": [] if not is_clips else None,
                "clips": [] if is_clips else None,
                "channel": channel,
                "platforms": [],
                "content": "clips" if is_clips else "vods",
                "days": days,
                "per_platform_errors": {},
            }
            set_cached(cache_key, payload)
            return payload
        if content_norm == "clips":
            all_clips, per_platform_errors = await _gather_channel_clips(
                wanted=wanted, kick_slug=kick_ch, twitch_login=twitch_ch, limit=limit_norm,
            )
            payload = {
                "clips": all_clips,
                "videos": all_clips,
                "channel": channel,
                "platforms": wanted,
                "content": "clips",
                "per_platform_errors": per_platform_errors,
            }
            set_cached(cache_key, payload)
            return payload
        limit = limit_norm
        days = days_norm
        cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days > 0 else None
        per_platform_errors: Dict[str, str] = {}
        all_videos: List[dict] = []
        loop = asyncio.get_running_loop()

        async def _fetch_twitch() -> list:
            login = twitch_ch or channel
            if not login:
                return []
            try:
                vids = await asyncio.wait_for(
                    loop.run_in_executor(
                        CHANNEL_EXECUTOR, twitch_list_channel_videos_sync, login, limit
                    ),
                    timeout=CHANNEL_VOD_FETCH_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                per_platform_errors["Twitch"] = "VOD fetch timed out — try again"
                return []
            except Exception as e:
            # ponytail: best-effort — return []
                per_platform_errors["Twitch"] = format_platform_error(e)
                return []
            return [{
                "id": v["id"],
                "platform": "Twitch",
                "title": v.get("title") or "Untitled",
                "duration": v.get("duration"),
                "duration_string": v.get("duration_string"),
                "created_at": v.get("created_at"),
                "views": v.get("views"),
                "thumbnail_url": v.get("thumbnail_url"),
                "url": v.get("url") or f"https://www.twitch.tv/videos/{v['id']}",
                "channel": channel,
                "content_kind": "vod",
            } for v in vids]

        async def _fetch_kick() -> list:
            slug = kick_ch or channel
            if not slug:
                return []
            videos_url = f"https://kick.com/{slug}/videos"
            try:
                vids = await asyncio.wait_for(
                    loop.run_in_executor(
                        CHANNEL_EXECUTOR, kick_list_channel_videos_sync, videos_url, limit
                    ),
                    timeout=CHANNEL_VOD_FETCH_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                per_platform_errors["Kick"] = "VOD fetch timed out — try again"
                return []
            except Exception as e:
            # ponytail: best-effort — return []
                per_platform_errors["Kick"] = format_platform_error(e)
                return []
            return [{
                "id": v["id"],
                "platform": "Kick",
                "title": v.get("title") or "Untitled",
                "duration": v.get("duration"),
                "duration_string": v.get("duration_string"),
                "created_at": v.get("created_at"),
                "views": v.get("views"),
                "thumbnail_url": v.get("thumbnail"),
                "url": v.get("url") or f"https://kick.com/{channel}/videos/{v['id']}",
                "channel": channel,
                "content_kind": "vod",
            } for v in vids]

        tasks: list[asyncio.Task[list]] = []
        if "Kick" in wanted:
            tasks.append(asyncio.create_task(_fetch_kick()))
        if "Twitch" in wanted:
            tasks.append(asyncio.create_task(_fetch_twitch()))
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, list):
                    all_videos.extend(result)
                elif isinstance(result, BaseException):
                    logger.debug("Channel fetch task failed: %s", result)

        if cutoff is not None:
            filtered: List[dict] = []
            for v in all_videos:
                dt = parse_video_date(v.get("created_at"))
                if dt is None or dt >= cutoff:
                    filtered.append(v)
            all_videos = filtered

        def _sort_key(v: dict) -> tuple:
            dt = parse_video_date(v.get("created_at"))
            ts = -dt.timestamp() if dt else 0.0
            return (ts, v.get("platform") or "")

        all_videos.sort(key=_sort_key)
        for k, v in list(per_platform_errors.items()):
            per_platform_errors[k] = normalize_err(v)
        payload = {
            "videos": all_videos,
            "channel": channel,
            "platforms": wanted,
            "content": "vods",
            "days": days,
            "per_platform_errors": per_platform_errors,
        }
        set_cached(cache_key, payload)
        return payload
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
    # ponytail: best-effort — network errors only
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/channel/clips")
async def channel_clips(
    url: str = "",
    platforms: str = "Kick,Twitch",
    limit: int = CHANNEL_CLIP_LIMIT,
    kick_slug: Optional[str] = None,
    twitch_login: Optional[str] = None,
):
    """Fetch recent clips for a channel."""
    try:
        default_slug = resolve_channel_slug(unquote(url).strip()) if (url or "").strip() else ""
        kick_ch = (kick_slug or default_slug).strip().lower()
        twitch_ch = (twitch_login or default_slug).strip().lower()
        if not kick_ch and not twitch_ch:
            raise ValueError("Provide url, kick_slug, or twitch_login")
        wanted = parse_wanted_platforms(platforms)
        limit_norm = max(1, min(int(limit), CHANNEL_CLIP_LIMIT))
        cache_key = make_channel_cache_key(
            "clips", kick_ch, twitch_ch, platforms, limit_norm,
            ",".join(sorted(wanted)),
        )
        cached = get_cached(cache_key)
        if cached is not None:
            return cached
        if not wanted:
            payload = {
                "clips": [],
                "channel": kick_ch or twitch_ch,
                "platforms": [],
                "content": "clips",
                "per_platform_errors": {},
            }
            set_cached(cache_key, payload)
            return payload
        all_clips, per_platform_errors = await _gather_channel_clips(
            wanted=wanted, kick_slug=kick_ch, twitch_login=twitch_ch, limit=limit_norm,
        )
        payload = {
            "clips": all_clips,
            "channel": kick_ch or twitch_ch,
            "platforms": wanted,
            "content": "clips",
            "per_platform_errors": per_platform_errors,
        }
        set_cached(cache_key, payload)
        return payload
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
    # ponytail: best-effort — network errors only
        raise HTTPException(status_code=400, detail=str(e))
