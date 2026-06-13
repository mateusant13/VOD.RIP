"""Fast Kick.com metadata via public JSON API + curl_cffi (no browser).

Used for channel lists, VOD browse rows, preview stream resolution, and
Kick VOD downloads (curl_cffi + HLS).

Endpoints used:
  GET /api/v2/channels/{slug}/videos  — channel VOD list (~1-2s)
  GET /api/v1/video/{uuid}              — single VOD metadata + m3u8 (~0.5s)
  GET /api/v2/channels/{slug}           — channel metadata (~1s)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from services.kick_models import (
    KickChannel,
    KickVideo,
    canonical_kick_clip_url,
    extract_clip_id,
    extract_slug,
    extract_vod_id,
    format_duration,
)
from services.ytdlp_service import is_clip_url

_IMPERSONATE = "chrome"
_BASE = "https://kick.com"


def _headers(referer: str) -> Dict[str, str]:
    return {"referer": referer, "origin": _BASE}


def _get_json(path: str, referer: str, *, timeout: float = 15.0) -> Any:
    from curl_cffi import requests

    url = f"{_BASE}{path}"
    resp = requests.get(
        url,
        impersonate=_IMPERSONATE,
        headers=_headers(referer),
        timeout=timeout,
    )
    if resp.status_code == 404:
        raise ValueError(f"Kick channel not found: {referer}")
    resp.raise_for_status()
    return resp.json()


def verify_channel_exists(slug: str) -> None:
    """Raise ValueError when the Kick channel slug does not exist."""
    slug = (slug or "").strip()
    if not slug:
        raise ValueError("Kick channel slug is required")
    _get_json(f"/api/v2/channels/{slug}", f"{_BASE}/{slug}/clips")


def _thumb_url(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        return value.get("src") or value.get("url")
    return None


def _ms_to_seconds(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def _normalize_created_at(value: Any) -> Optional[str]:
    """Normalize Kick timestamps to ISO UTC for the frontend."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", s):
        return s.replace(" ", "T") + "Z"
    if s.endswith("Z") or "+" in s[10:] or s.endswith("UTC"):
        return s
    if "T" in s:
        return f"{s}Z"
    return s


def _video_from_v2_list_item(item: dict, slug: str) -> Optional[KickVideo]:
    if item.get("is_live"):
        return None
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    video_id = video.get("uuid") or ""
    if not video_id:
        return None
    raw_dur = item.get("duration")
    if raw_dur in (None, 0) and video.get("duration") is not None:
        raw_dur = video.get("duration")
    duration = _ms_to_seconds(raw_dur)
    return KickVideo(
        id=video_id,
        title=str(item.get("session_title") or "Untitled"),
        duration=duration,
        thumbnail=_thumb_url(item.get("thumbnail")),
        views=item.get("views") if isinstance(item.get("views"), (int, float)) else None,
        created_at=_normalize_created_at(
            item.get("created_at") or item.get("start_time") or video.get("created_at")
        ),
        channel=slug,
        url=f"https://kick.com/{slug}/videos/{video_id}",
        m3u8_url=item.get("source") if isinstance(item.get("source"), str) else None,
    )


def _video_from_v1(data: dict, slug_hint: Optional[str]) -> KickVideo:
    ls = data.get("livestream") if isinstance(data.get("livestream"), dict) else {}
    ch = ls.get("channel") if isinstance(ls.get("channel"), dict) else {}
    slug = slug_hint or ch.get("slug") or ch.get("user", {}).get("username")
    video_id = data.get("uuid") or ""
    duration = _ms_to_seconds(ls.get("duration") or data.get("duration"))
    cats = ls.get("categories")
    category = cats[0].get("name") if isinstance(cats, list) and cats and isinstance(cats[0], dict) else None
    return KickVideo(
        id=video_id,
        title=str(ls.get("session_title") or data.get("title") or "Untitled"),
        duration=duration,
        thumbnail=_thumb_url(ls.get("thumbnail")),
        views=data.get("views") if isinstance(data.get("views"), (int, float)) else None,
        created_at=_normalize_created_at(
            data.get("created_at") or ls.get("created_at") or ls.get("start_time")
        ),
        channel=slug,
        url=f"https://kick.com/{slug}/videos/{video_id}" if slug and video_id else None,
        category=category,
        m3u8_url=data.get("source") if isinstance(data.get("source"), str) else None,
    )


def _clip_from_api_item(item: dict, slug: str) -> Optional[KickVideo]:
    clip_id = str(item.get("id") or "").strip()
    if not clip_id:
        return None
    views = item.get("views")
    if views is None:
        views = item.get("view_count")
    dur = item.get("duration")
    if isinstance(dur, (int, float)) and dur > 0 and dur < 1000:
        duration = float(dur)
    else:
        duration = _ms_to_seconds(dur)
    return KickVideo(
        id=clip_id,
        title=str(item.get("title") or "Untitled"),
        duration=duration,
        thumbnail=item.get("thumbnail_url") if isinstance(item.get("thumbnail_url"), str) else None,
        views=int(views) if isinstance(views, (int, float)) else None,
        created_at=_normalize_created_at(item.get("created_at")),
        channel=slug,
        url=f"https://kick.com/{slug}/clips/{clip_id}",
    )


CLIP_MAX_DURATION_SEC = 60


def list_channel_clips_api(slug: str, limit: int = 10, *, verify: bool = True) -> List[KickVideo]:
    """Last *limit* clips by date, then ranked by views (desc).

    Uses Kick channel clips page/API: https://kick.com/{slug}/clips
    """
    slug = (slug or "").strip().lower()
    if verify:
        verify_channel_exists(slug)
    referer = f"{_BASE}/{slug}/clips"
    data = _get_json(f"/api/v2/channels/{slug}/clips", referer)
    raw = data.get("clips") if isinstance(data, dict) else []
    if not isinstance(raw, list):
        raise RuntimeError("Unexpected Kick clips API response")
    parsed: List[KickVideo] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        clip = _clip_from_api_item(item, slug)
        if not clip or clip.id in seen:
            continue
        if clip.duration is not None and clip.duration > CLIP_MAX_DURATION_SEC:
            continue
        seen.add(clip.id)
        parsed.append(clip)
    parsed.sort(key=lambda c: c.created_at or "", reverse=True)
    recent = parsed[: max(1, min(int(limit), 10))]
    recent.sort(key=lambda c: c.views or 0, reverse=True)
    return recent


def list_channel_clips_sync(url: str, limit: int = 10) -> list[dict]:
    slug = extract_slug(url)
    if not slug:
        raise ValueError(f"Not a Kick channel URL: {url}")
    clips = list_channel_clips_api(slug, limit, verify=False)
    return [
        {
            "id": c.id,
            "title": c.title,
            "url": c.url,
            "thumbnail": c.thumbnail,
            "duration": c.duration,
            "duration_string": format_duration(c.duration),
            "created_at": c.created_at,
            "views": c.views,
            "channel": c.channel or slug,
            "content_kind": "clip",
        }
        for c in clips
    ]


def list_channel_videos_api(slug: str, limit: int = 20) -> List[KickVideo]:
    referer = f"{_BASE}/{slug}/videos"
    data = _get_json(f"/api/v2/channels/{slug}/videos", referer)
    if not isinstance(data, list):
        raise RuntimeError("Unexpected Kick videos API response")
    out: List[KickVideo] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        v = _video_from_v2_list_item(item, slug)
        if v and v.id not in seen:
            seen.add(v.id)
            out.append(v)
        if len(out) >= limit:
            break
    return out


def resolve_kick_stream_api(url: str) -> KickVideo:
    """Resolve Kick VOD or clip metadata (+ m3u8) from any supported URL shape."""
    raw = (url or "").strip()
    if is_clip_url(raw) or extract_clip_id(raw):
        return get_clip_info_api(canonical_kick_clip_url(raw))
    return get_video_info_api(raw)


def get_clip_info_api(url: str) -> KickVideo:
    clip_id = extract_clip_id(url)
    if not clip_id:
        raise ValueError(f"Not a Kick clip URL: {url}")
    slug = extract_slug(url)
    referer = url if url.startswith("http") else f"{_BASE}/{slug}/clips/{clip_id}"
    data = _get_json(f"/api/v2/clips/{clip_id}", referer)
    clip = data.get("clip") if isinstance(data, dict) else None
    if not isinstance(clip, dict):
        raise RuntimeError("Unexpected Kick clip API response")
    channel = clip.get("channel") if isinstance(clip.get("channel"), dict) else {}
    ch_slug = channel.get("slug") or slug
    m3u8 = clip.get("clip_url") or clip.get("video_url")
    if not isinstance(m3u8, str) or not m3u8.strip():
        raise RuntimeError("Kick API returned no HLS source for this clip")
    dur = clip.get("duration")
    duration = float(dur) if isinstance(dur, (int, float)) and dur > 0 else None
    views = clip.get("views")
    if views is None:
        views = clip.get("view_count")
    return KickVideo(
        id=str(clip.get("id") or clip_id),
        title=str(clip.get("title") or "Untitled"),
        duration=duration,
        thumbnail=clip.get("thumbnail_url") if isinstance(clip.get("thumbnail_url"), str) else None,
        views=int(views) if isinstance(views, (int, float)) else None,
        created_at=clip.get("created_at") if isinstance(clip.get("created_at"), str) else None,
        channel=ch_slug,
        url=url if url.startswith("http") else f"{_BASE}/{ch_slug}/clips/{clip_id}",
        m3u8_url=m3u8.strip(),
    )


def get_video_info_api(url: str) -> KickVideo:
    video_id = extract_vod_id(url)
    if not video_id:
        raise ValueError(f"Not a Kick VOD URL: {url}")
    slug = extract_slug(url)
    referer = url if url.startswith("http") else f"{_BASE}/{slug}/videos/{video_id}"
    data = _get_json(f"/api/v1/video/{video_id}", referer)
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected Kick video API response")
    v = _video_from_v1(data, slug)
    if not v.m3u8_url:
        raise RuntimeError("Kick API returned no HLS source for this VOD")
    return v


def get_channel_api(url: str) -> KickChannel:
    slug = extract_slug(url)
    if not slug:
        raise ValueError(f"Not a Kick channel URL: {url}")
    data = _get_json(f"/api/v2/channels/{slug}", f"{_BASE}/{slug}")
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected Kick channel API response")
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    ls = data.get("livestream") if isinstance(data.get("livestream"), dict) else None
    return KickChannel(
        slug=slug,
        username=user.get("username") or slug,
        channel_id=data.get("id"),
        followers=data.get("followers_count") or data.get("followersCount"),
        is_live=bool(ls),
        live_title=(ls.get("session_title") if ls else None),
    )


# Sync helpers for FastAPI routes — curl_cffi only, never Playwright.
def get_clip_info_sync(url: str) -> dict:
    from services.size_estimate import enrich_info_dict

    v = get_clip_info_api(url)
    payload = {
        "id": v.id,
        "title": v.title,
        "uploader": v.channel,
        "channel": v.channel,
        "duration": v.duration,
        "duration_string": format_duration(v.duration),
        "thumbnail": v.thumbnail,
        "views": v.views,
        "webpage_url": v.url,
        "qualities": [],
        "platform": "Kick",
        "created_at": v.created_at,
        "content_kind": "clip",
    }
    headers = {"referer": v.url or url, "origin": _BASE}
    enrich_info_dict(
        payload,
        m3u8_url=v.m3u8_url,
        m3u8_headers=headers,
        is_clip=True,
    )
    return payload


def get_video_info_sync(url: str) -> dict:
    from services.size_estimate import enrich_info_dict

    v = get_video_info_api(url)
    payload = {
        "id": v.id,
        "title": v.title,
        "uploader": v.channel,
        "channel": v.channel,
        "duration": v.duration,
        "duration_string": format_duration(v.duration),
        "thumbnail": v.thumbnail,
        "views": v.views,
        "category": v.category,
        "webpage_url": v.url,
        "qualities": [],
        "platform": "Kick",
        "created_at": v.created_at,
    }
    headers = {"referer": v.url or url, "origin": _BASE}
    enrich_info_dict(
        payload,
        m3u8_url=v.m3u8_url,
        m3u8_headers=headers,
        is_clip=False,
    )
    return payload


def list_channel_videos_sync(url: str, limit: int = 20) -> list[dict]:
    slug = extract_slug(url)
    if not slug:
        raise ValueError(f"Not a Kick channel URL: {url}")
    vids = list_channel_videos_api(slug, limit)
    return [
        {
            "id": v.id,
            "title": v.title,
            "url": v.url,
            "thumbnail": v.thumbnail,
            "duration": v.duration,
            "duration_string": format_duration(v.duration),
            "created_at": v.created_at,
            "views": v.views,
            "channel": v.channel or slug,
            "content_kind": "vod",
        }
        for v in vids
    ]


def get_channel_info_sync(url: str) -> dict:
    ch = get_channel_api(url)
    return {
        "slug": ch.slug,
        "username": ch.username,
        "channel_id": ch.channel_id,
        "followers": ch.followers,
        "is_live": ch.is_live,
        "live_title": ch.live_title,
    }


def download_vod_sync(
    url: str,
    output_path: str,
    quality: Optional[str] = None,
    crop_start: Optional[float] = None,
    crop_end: Optional[float] = None,
    progress_hook=None,
    cancel_event=None,
    pause_event=None,
    register_abort=None,
    video_encoder=None,
    settings_mgr=None,
    **_,
) -> str:
    """Download a Kick VOD clip via the fast JSON API + HLS segments (no browser)."""
    from services.ytdlp_service import (
        _parse_prefer_height,
        _require_crop_range,
        _resolve_ffmpeg_exe,
        _verify_output_file,
        download_hls_media_clip,
    )

    if video_encoder is None and settings_mgr is not None:
        video_encoder = settings_mgr.get().video_encoder
    mp4_faststart = bool(settings_mgr.get().mp4_faststart) if settings_mgr else False

    info = get_video_info_api(url)
    if not info.m3u8_url:
        raise RuntimeError("Kick API returned no HLS source for this VOD")
    start_sec, end_sec = _require_crop_range(crop_start, crop_end)
    page_url = info.url or url
    headers = {"referer": page_url, "origin": _BASE}
    download_hls_media_clip(
        info.m3u8_url,
        start_sec,
        end_sec,
        output_path,
        headers=headers,
        ffmpeg_exe=_resolve_ffmpeg_exe(),
        progress_hook=progress_hook,
        cancel_event=cancel_event,
        pause_event=pause_event,
        register_abort=register_abort,
        prefer_height=_parse_prefer_height(quality),
        video_encoder=video_encoder,
        mp4_faststart=mp4_faststart,
    )
    _verify_output_file(output_path)
    return output_path
