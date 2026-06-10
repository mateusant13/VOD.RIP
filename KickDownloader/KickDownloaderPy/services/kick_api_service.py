"""Fast Kick.com access via public JSON API + curl_cffi (no browser).

Endpoints used:
  GET /api/v2/channels/{slug}/videos  — channel VOD list (~1-2s)
  GET /api/v1/video/{uuid}              — single VOD + m3u8 source (~0.5s)
  GET /api/v2/channels/{slug}           — channel metadata (~1s)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from services.kick_playwright_service import KickChannel, KickVideo, _extract_slug, _extract_vod_id

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
    resp.raise_for_status()
    return resp.json()


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
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    video_id = video.get("uuid") or ""
    if not video_id:
        return None
    duration = _ms_to_seconds(item.get("duration"))
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


def get_video_info_api(url: str) -> KickVideo:
    video_id = _extract_vod_id(url)
    if not video_id:
        raise ValueError(f"Not a Kick VOD URL: {url}")
    slug = _extract_slug(url)
    referer = url if url.startswith("http") else f"{_BASE}/{slug}/videos/{video_id}"
    data = _get_json(f"/api/v1/video/{video_id}", referer)
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected Kick video API response")
    v = _video_from_v1(data, slug)
    if not v.m3u8_url:
        raise RuntimeError("Kick API returned no HLS source for this VOD")
    return v


def get_channel_api(url: str) -> KickChannel:
    slug = _extract_slug(url)
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
