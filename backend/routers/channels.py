"""
Channel browsing routes — VODs and clips for saved Kick/Twitch channels.
"""

import asyncio
import functools
import logging
import threading
from typing import Dict, List, Optional
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException

from deps import (
    CHANNEL_CLIP_LIMIT,
    CHANNEL_CLIP_MAX_DURATION_SEC,
    CHANNEL_DAYS_DEFAULT,
    CHANNEL_DELTA_LIMIT,
    CHANNEL_EXECUTOR,
    CHANNEL_LIMIT_MAX,
    CHANNEL_VOD_FETCH_TIMEOUT_SEC,
    CLIP_FETCH_TIMEOUT_SEC,
    KICK_CHANNEL_FRESH_SEC,
    TWITCH_CHANNEL_FRESH_SEC,
    YOUTUBE_CHANNEL_FETCH_TIMEOUT_SEC,
    YOUTUBE_CHANNEL_FRESH_SEC,
)
from services.archive_db import (
    channel_snapshot_age_sec,
    list_videos,
    touch_channel_snapshot,
    upsert_channel_video,
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
from services.youtube_service import list_channel_videos_sync as youtube_list_channel_videos_sync
from utils import (
    filter_clip_entries,
    filter_clips_by_age_window,
    filter_videos_recent_or_all_by_platform,
    format_platform_error,
    looks_like_clip_entry,
    normalize_err,
    user_visible_platform_error,
    parse_video_date,
    parse_wanted_platforms,
    resolve_channel_slug,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["channels"])

# Dedupe guard for background delta refreshes: one in-flight refresh per
# (channel, platforms) key, so concurrent opens never double-fire the
# expensive YouTube extract.
_bg_refresh_lock = threading.Lock()
_bg_refresh_inflight: set[str] = set()


def _cache_channel_payload(key: str, payload: dict) -> dict:
    """Skip caching empty error-only responses so retries aren't blocked 90s."""
    errs = payload.get("per_platform_errors") or {}
    rows = payload.get("videos") or payload.get("clips") or []
    if errs and not rows:
        return payload
    set_cached(key, payload)
    return payload


async def _gather_channel_clips(
    *,
    wanted: List[str],
    kick_slug: str,
    twitch_login: str,
    youtube_slug: str = "",
    limit: int,
    days: int = CHANNEL_DAYS_DEFAULT,
    min_days: int = 0,
    sort: str = "date",
) -> tuple[List[dict], Dict[str, str], int]:
    """Fetch clips per platform using platform-specific logins."""
    per_platform_errors: Dict[str, str] = {}
    all_clips: List[dict] = []
    loop = asyncio.get_running_loop()
    kick_slug = (kick_slug or "").strip().lower()
    twitch_login = (twitch_login or "").strip().lower()
    youtube_ref = (youtube_slug or "").strip()

    async def _fetch_youtube_shorts() -> None:
        if not youtube_ref:
            return
        try:
            from functools import partial
            vids = await asyncio.wait_for(
                loop.run_in_executor(
                    CHANNEL_EXECUTOR,
                    partial(
                        youtube_list_channel_videos_sync,
                        youtube_ref,
                        limit,
                        playlist="shorts",
                        enrich=True,
                    ),
                ),
                timeout=YOUTUBE_CHANNEL_FETCH_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            per_platform_errors["YouTube"] = "Shorts fetch timed out — try again"
            return
        except Exception as e:
            per_platform_errors["YouTube"] = format_platform_error(e)
            return
        all_clips.extend(vids)

    async def _fetch_twitch() -> None:
        if not twitch_login:
            per_platform_errors["Twitch"] = "Twitch login is required"
            return
        # Map days (window's OLDER edge) → smallest Twitch GQL window that
        # covers it. 0 (All) and >30d must use ALL_TIME — Twitch GQL has no
        # wider window.
        if days <= 0 or days > 30:
            gte = "ALL_TIME"
        elif days <= 1:
            gte = "LAST_DAY"
        elif days <= 7:
            gte = "LAST_WEEK"
        else:
            gte = "LAST_MONTH"
        try:
            vids = await asyncio.wait_for(
                loop.run_in_executor(
                    CHANNEL_EXECUTOR,
                    functools.partial(
                        twitch_list_channel_clips_sync,
                        twitch_login,
                        limit,
                        range_label=gte,
                        sort=sort,
                        older_than_days=days,
                        newer_than_days=min_days,
                    ),
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
                    functools.partial(
                        kick_list_channel_clips_sync,
                        f"https://kick.com/{kick_slug}/clips",
                        limit,
                        sort=sort,
                    ),
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
    if "YouTube" in wanted:
        tasks.append(asyncio.create_task(_fetch_youtube_shorts()))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    all_clips = filter_clip_entries(all_clips)
    # Era window (e.g. 1mo = 14–30 days old) — no "show all when empty"
    # fallback: an explicit era that comes back empty is the honest answer.
    all_clips = filter_clips_by_age_window(all_clips, min_days, days)
    effective_days = days
    # Final merge sort: date-desc for Newest, views-desc for Most Views —
    # the per-platform views sort was previously flattened to date order here.
    if sort == "views":
        all_clips.sort(key=lambda v: int(v.get("views") or 0), reverse=True)
    else:
        all_clips.sort(key=lambda v: v.get("created_at") or "", reverse=True)
    # Era windows can return hundreds of clips (deep Twitch paging) — cap the
    # payload; the UI shows 10 and pages client-side.
    all_clips = all_clips[:200]
    for k, v in list(per_platform_errors.items()):
        per_platform_errors[k] = user_visible_platform_error(normalize_err(v))
        if not per_platform_errors[k]:
            del per_platform_errors[k]
    return all_clips, per_platform_errors, effective_days


@router.get("/api/channel/videos")
async def channel_videos(
    url: str,
    limit: int = CHANNEL_LIMIT_MAX,
    days: int = CHANNEL_DAYS_DEFAULT,
    min_days: int = 0,
    sort: str = "date",
    platforms: str = "Kick,Twitch,YouTube",
    content: str = "vods",
    kick_slug: Optional[str] = None,
    twitch_login: Optional[str] = None,
    youtube_slug: Optional[str] = None,
    force: str = "0",
):
    """Fetch archive VODs for a channel."""
    raw = unquote(url).strip()
    try:
        default_slug = resolve_channel_slug(raw) if raw else ""
        kick_ch = (kick_slug or default_slug).strip().lower()
        twitch_ch = (twitch_login or default_slug).strip().lower()
        youtube_ch = (youtube_slug or default_slug).strip()
        channel = kick_ch or twitch_ch or youtube_ch
        wanted = parse_wanted_platforms(platforms)
        content_norm = (content or "").strip().lower()
        force_refresh = force == "1"
        limit_norm = max(1, min(int(limit), CHANNEL_CLIP_LIMIT if content_norm == "clips" else CHANNEL_LIMIT_MAX))
        days_norm = max(0, min(int(days), 365))
        min_days_norm = max(0, min(int(min_days), days_norm)) if days_norm > 0 else 0
        cache_key = make_channel_cache_key(
            "videos", content_norm, kick_ch, twitch_ch, platforms, limit_norm, days_norm, sort,
            ",".join(sorted(wanted)), youtube_ch, min_days_norm,
        )
        if not force_refresh:
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
            _cache_channel_payload(cache_key, payload)
            return payload
        if content_norm == "clips":
            all_clips, per_platform_errors, days_eff = await _gather_channel_clips(
                wanted=wanted,
                kick_slug=kick_ch,
                twitch_login=twitch_ch,
                youtube_slug=youtube_ch,
                limit=limit_norm,
                days=days_norm,
                min_days=min_days_norm,
                sort=sort,
            )
            payload = {
                "clips": all_clips,
                "videos": all_clips,
                "channel": channel,
                "platforms": wanted,
                "content": "clips",
                "days": days_eff,
                "per_platform_errors": per_platform_errors,
            }
            _cache_channel_payload(cache_key, payload)
            return payload
        if content_norm == "streams":
            per_platform_errors: Dict[str, str] = {}
            all_videos: List[dict] = []
            loop = asyncio.get_running_loop()
            if "YouTube" in wanted and youtube_ch:
                try:
                    from functools import partial
                    vids = await asyncio.wait_for(
                        loop.run_in_executor(
                            CHANNEL_EXECUTOR,
                            partial(
                                youtube_list_channel_videos_sync,
                                youtube_ch,
                                limit_norm,
                                playlist="streams",
                                enrich=True,
                            ),
                        ),
                        timeout=YOUTUBE_CHANNEL_FETCH_TIMEOUT_SEC,
                    )
                    all_videos.extend(vids)
                except asyncio.TimeoutError:
                    per_platform_errors["YouTube"] = "Stream VOD fetch timed out — try again"
                except Exception as e:
                    per_platform_errors["YouTube"] = format_platform_error(e)
            elif "YouTube" in wanted:
                per_platform_errors["YouTube"] = "YouTube channel is required"
            for k, v in list(per_platform_errors.items()):
                per_platform_errors[k] = user_visible_platform_error(normalize_err(v))
                if not per_platform_errors[k]:
                    del per_platform_errors[k]
            all_videos, days_eff = filter_videos_recent_or_all_by_platform(all_videos, days_norm)
            payload = {
                "videos": all_videos,
                "channel": channel,
                "platforms": wanted,
                "content": "streams",
                "days": days_eff,
                "per_platform_errors": per_platform_errors,
            }
            _cache_channel_payload(cache_key, payload)
            asyncio.create_task(_warm_youtube_previews(all_videos))
            return payload
        limit = limit_norm
        per_platform_errors: Dict[str, str] = {}
        loop = asyncio.get_running_loop()

        # Platform identity: (archive platform, slug ref, freshness window).
        # The disk index stores rows under the route's resolved `channel`
        # slug; snapshots are keyed per platform's own ref so an
        # '@titiltei' YouTube handle tracks separately from a bare Kick slug.
        platform_specs = {
            "Kick": ("kick", kick_ch or channel, KICK_CHANNEL_FRESH_SEC),
            "Twitch": ("twitch", twitch_ch or channel, TWITCH_CHANNEL_FRESH_SEC),
            "YouTube": ("youtube", youtube_ch or channel, YOUTUBE_CHANNEL_FRESH_SEC),
        }
        _LABEL_TO_ARCHIVE = {label: spec[0] for label, spec in platform_specs.items()}
        _ARCHIVE_TO_LABEL = {p: label for label, (p, _, _) in platform_specs.items()}

        async def _fetch_one(label: str, fetch_limit: int, errors: dict) -> list:
            """Fetch one platform's VOD list (route-payload shape)."""
            if label == "Kick":
                slug = kick_ch or channel
                if not slug:
                    return []
                try:
                    vids = await asyncio.wait_for(
                        loop.run_in_executor(
                            CHANNEL_EXECUTOR,
                            kick_list_channel_videos_sync,
                            f"https://kick.com/{slug}/videos",
                            fetch_limit,
                        ),
                        timeout=CHANNEL_VOD_FETCH_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    errors["Kick"] = "VOD fetch timed out — try again"
                    return []
                except Exception as e:
                # ponytail: best-effort — return []
                    errors["Kick"] = format_platform_error(e)
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
            if label == "Twitch":
                login = twitch_ch or channel
                if not login:
                    return []
                try:
                    vids = await asyncio.wait_for(
                        loop.run_in_executor(
                            CHANNEL_EXECUTOR,
                            twitch_list_channel_videos_sync,
                            login,
                            fetch_limit,
                        ),
                        timeout=CHANNEL_VOD_FETCH_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    errors["Twitch"] = "VOD fetch timed out — try again"
                    return []
                except Exception as e:
                # ponytail: best-effort — return []
                    errors["Twitch"] = format_platform_error(e)
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
            ref = youtube_ch or channel
            if not ref:
                return []
            try:
                vids = await asyncio.wait_for(
                    loop.run_in_executor(
                        CHANNEL_EXECUTOR,
                        functools.partial(
                            youtube_list_channel_videos_sync,
                            ref,
                            fetch_limit,
                            playlist="videos",
                            enrich=True,
                        ),
                    ),
                    timeout=YOUTUBE_CHANNEL_FETCH_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                errors["YouTube"] = "VOD fetch timed out — try again"
                return []
            except Exception as e:
                errors["YouTube"] = format_platform_error(e)
                return []
            return vids

        def _persist_platform(label: str, items: list) -> None:
            """Accumulate fetched items into the permanent channel index.

            Upsert-only, metadata-only: archive fields (archive_path, status,
            canonical_key) are never touched, so a download in flight is
            never clobbered by a list refresh."""
            archive_platform, snap_key, _ = platform_specs[label]
            for v in items:
                upsert_channel_video({
                    "platform": archive_platform,
                    "video_id": str(v.get("id") or ""),
                    "channel": channel,
                    "title": v.get("title") or "Untitled",
                    "kind": v.get("content_kind") or "vod",
                    "started_at": v.get("created_at"),
                    "duration_sec": v.get("duration"),
                    "duration_string": v.get("duration_string"),
                    "views": v.get("views"),
                    "thumbnail_url": v.get("thumbnail_url") or v.get("thumbnail"),
                })
            if items and snap_key:
                touch_channel_snapshot(archive_platform, snap_key)

        async def _fetch_platforms(labels: List[str], fetch_limit: int, errors: dict) -> list:
            """Parallel fetch of a subset of platforms."""
            tasks = [
                asyncio.create_task(_fetch_one(label, fetch_limit, errors))
                for label in labels
            ]
            if not tasks:
                return []
            results = await asyncio.gather(*tasks, return_exceptions=True)
            out: list[dict] = []
            for result in results:
                if isinstance(result, list):
                    out.extend(result)
                elif isinstance(result, BaseException):
                    logger.debug("Channel fetch task failed: %s", result)
            return out

        def _index_rows_by_platform() -> dict:
            """All accumulated index rows for the wanted platforms."""
            grouped: dict[str, list] = {}
            for label in wanted:
                archive_platform, _, _ = platform_specs[label]
                for r in list_videos(platform=archive_platform, channel=channel):
                    grouped.setdefault(archive_platform, []).append(r)
            return grouped

        def _row_to_payload_item(r: dict) -> dict:
            vid = str(r.get("video_id") or "")
            archive_platform = r.get("platform") or ""
            label = _ARCHIVE_TO_LABEL.get(archive_platform, archive_platform.capitalize())
            if archive_platform == "kick":
                url = f"https://kick.com/{channel}/videos/{vid}"
            elif archive_platform == "twitch":
                url = f"https://www.twitch.tv/videos/{vid}"
            else:
                url = f"https://www.youtube.com/watch?v={vid}"
            return {
                "id": vid,
                "platform": label,
                "title": r.get("title") or "Untitled",
                "duration": r.get("duration_sec"),
                "duration_string": r.get("duration_string"),
                "created_at": r.get("started_at"),
                "views": r.get("views"),
                "thumbnail_url": r.get("thumbnail_url"),
                "url": url,
                "channel": r.get("channel") or channel,
                "content_kind": r.get("kind") or "vod",
            }

        # --- index state -------------------------------------------------
        index_by_platform = _index_rows_by_platform()

        def _idx(label: str) -> list:
            return index_by_platform.get(_LABEL_TO_ARCHIVE[label], [])

        fresh = {}
        for label in wanted:
            archive_platform, snap_key, window = platform_specs[label]
            age = (
                channel_snapshot_age_sec(archive_platform, snap_key)
                if snap_key else None
            )
            fresh[label] = age is not None and age < window

        # Platforms we must fetch NOW (blocking): forced refresh, or the
        # index has never seen this channel. Platforms with a stale snapshot
        # but an existing index are refreshed in the BACKGROUND with a small
        # delta — the response is served instantly from the accumulated
        # index, and the merge lands on the next request.
        block_set = [label for label in wanted if force_refresh or not _idx(label)]
        bg_set = [label for label in wanted if label not in block_set and not fresh[label]]

        fetched: list[dict] = []
        for label in block_set:
            fetch_limit = CHANNEL_DELTA_LIMIT if _idx(label) else limit
            items = await _fetch_platforms([label], fetch_limit, per_platform_errors)
            fetched.extend(items)
            _persist_platform(label, items)

        # Merge fetched (wins) over accumulated index rows, keyed per
        # (platform label, video id).
        merged: dict[tuple, dict] = {
            (str(v.get("platform") or ""), str(v.get("id") or "")): v for v in fetched
        }
        for label in wanted:
            for r in _idx(label):
                merged.setdefault(
                    (label, str(r.get("video_id") or "")),
                    _row_to_payload_item(r),
                )
        all_videos = list(merged.values())

        all_videos, days_eff = filter_videos_recent_or_all_by_platform(all_videos, days_norm)

        def _sort_key(v: dict) -> tuple:
            dt = parse_video_date(v.get("created_at"))
            ts = -dt.timestamp() if dt else 0.0
            return (ts, v.get("platform") or "")

        all_videos.sort(key=_sort_key)
        # The index accumulates forever — cap the response at the requested
        # limit (newest first) so payload size stays bounded.
        all_videos = all_videos[:limit]
        for k, v in list(per_platform_errors.items()):
            per_platform_errors[k] = user_visible_platform_error(normalize_err(v))
            if not per_platform_errors[k]:
                del per_platform_errors[k]
        refreshing = bool(bg_set) and not force_refresh
        payload = {
            "videos": all_videos,
            "channel": channel,
            "platforms": wanted,
            "content": "vods",
            "days": days_eff,
            "per_platform_errors": per_platform_errors,
            "refreshing": refreshing,
        }
        if fetched:
            asyncio.create_task(_warm_youtube_previews(fetched))
        # Never cache the refreshing flag: L1 hits within 300s must not keep
        # scheduling follow-up refreshes.
        _cache_channel_payload(cache_key, {**payload, "refreshing": False})

        # --- background delta refresh ------------------------------------
        if bg_set:
            bg_key = f"{channel}|{','.join(sorted(bg_set))}|vods"
            with _bg_refresh_lock:
                if bg_key in _bg_refresh_inflight:
                    pass
                else:
                    _bg_refresh_inflight.add(bg_key)

                    async def _bg_refresh() -> None:
                        try:
                            bg_errors: Dict[str, str] = {}
                            items = await _fetch_platforms(
                                bg_set, CHANNEL_DELTA_LIMIT, bg_errors
                            )
                            for label in bg_set:
                                _persist_platform(
                                    label,
                                    [v for v in items if v.get("platform") == label],
                                )
                            asyncio.create_task(_warm_youtube_previews(
                                [v for v in items if v.get("platform") == "YouTube"]
                            ))
                        except Exception:
                            logger.debug(
                                "background channel delta refresh failed", exc_info=True
                            )
                        finally:
                            with _bg_refresh_lock:
                                _bg_refresh_inflight.discard(bg_key)

                    asyncio.create_task(_bg_refresh())

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
    days: int = CHANNEL_DAYS_DEFAULT,
    min_days: int = 0,
    sort: str = "date",
    kick_slug: Optional[str] = None,
    twitch_login: Optional[str] = None,
    youtube_slug: Optional[str] = None,
):
    """Fetch recent clips for a channel."""
    try:
        default_slug = resolve_channel_slug(unquote(url).strip()) if (url or "").strip() else ""
        kick_ch = (kick_slug or default_slug).strip().lower()
        twitch_ch = (twitch_login or default_slug).strip().lower()
        youtube_ch = (youtube_slug or default_slug).strip()
        if not kick_ch and not twitch_ch and not youtube_ch:
            raise ValueError("Provide url, kick_slug, twitch_login, or youtube_slug")
        channel = kick_ch or twitch_ch or youtube_ch
        wanted = parse_wanted_platforms(platforms)
        limit_norm = max(1, min(int(limit), CHANNEL_CLIP_LIMIT))
        days_norm = max(0, min(int(days), 365))
        min_days_norm = max(0, min(int(min_days), days_norm)) if days_norm > 0 else 0
        cache_key = make_channel_cache_key(
            "clips", kick_ch, twitch_ch, platforms, limit_norm, days_norm, sort,
            ",".join(sorted(wanted)), youtube_ch, min_days_norm,
        )
        cached = get_cached(cache_key)
        if cached is not None:
            return cached
        if not wanted:
            payload = {
                "clips": [],
                "channel": channel,
                "platforms": [],
                "content": "clips",
                "per_platform_errors": {},
            }
            _cache_channel_payload(cache_key, payload)
            return payload
        all_clips, per_platform_errors, days_eff = await _gather_channel_clips(
            wanted=wanted,
            kick_slug=kick_ch,
            twitch_login=twitch_ch,
            youtube_slug=youtube_ch,
            limit=limit_norm,
            days=days_norm,
            min_days=min_days_norm,
            sort=sort,
        )
        payload = {
            "clips": all_clips,
            "channel": channel,
            "platforms": wanted,
            "content": "clips",
            "days": days_eff,
            "per_platform_errors": per_platform_errors,
        }
        _cache_channel_payload(cache_key, payload)
        return payload
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
    # ponytail: best-effort — network errors only
        raise HTTPException(status_code=400, detail=str(e))

async def _warm_youtube_previews(videos: list[dict]) -> None:
    """Fire-and-forget: warm YouTube preview caches for all YouTube videos in the list.

    kickoff_youtube_warm is a non-blocking submit onto the dedicated WARM_EXECUTOR
    (deduped per canonical URL there), so no executor hop or semaphore is needed.
    """
    from services.preview_service import kickoff_youtube_batch_warm

    seen: set[str] = set()
    for v in videos:
        url = v.get("url") or ""
        if v.get("platform") != "YouTube" or not url or url in seen:
            continue
        seen.add(url)
        try:
            # Resolve-only bulk warm — the frontend's scroll/hover warm adds the
            # heavier preflight/head downloads for rows the user can actually see.
            kickoff_youtube_batch_warm(url, prefer_height=360)
        except Exception as exc:
            logger.debug("YouTube preview warm failed for %s: %s", url[:50], exc)
