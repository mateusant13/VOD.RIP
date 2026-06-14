"""Twitch metadata via the public GQL API (fast — no yt-dlp)."""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TWITCH_GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TWITCH_GQL_URL = "https://gql.twitch.tv/gql"

CLIPS_CARDS_USER_HASH = "90c33f5e6465122fba8f9371e2a97076f9ed06c6fed3788d002ab9eba8f91d88"

CHANNEL_VIDEOS_QUERY = """
query ChannelVideos($login: String!, $first: Int!, $after: Cursor) {
  user(login: $login) {
    videos(first: $first, after: $after, type: ARCHIVE, sort: TIME) {
      pageInfo { hasNextPage endCursor }
      edges {
        node {
          id
          title
          createdAt
          lengthSeconds
          viewCount
          previewThumbnailURL(width: 320, height: 180)
        }
      }
    }
  }
}
"""

CLIP_INFO_QUERY = """
query ClipMetadata($slug: ID!) {
  clip(slug: $slug) {
    id
    slug
    title
    durationSeconds
    viewCount
    createdAt
    thumbnailURL
    videoQualities {
      quality
      sourceURL
      frameRate
    }
    broadcaster {
      login
      displayName
    }
  }
}
"""

VIDEO_INFO_QUERY = """
query VideoMetadata($id: ID!) {
  video(id: $id) {
    id
    title
    createdAt
    lengthSeconds
    viewCount
    previewThumbnailURL(width: 320, height: 180)
    game {
      displayName
    }
    owner {
      displayName
      login
    }
  }
}
"""


def _format_duration(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _extract_clip_slug(url_or_slug: str) -> Optional[str]:
    raw = (url_or_slug or "").strip()
    if not raw:
        return None
    m = re.search(r"clips\.twitch\.tv/([^/?#]+)", raw, re.I)
    if m:
        return m.group(1)
    m = re.search(r"twitch\.tv/[^/]+/clip/([^/?#]+)", raw, re.I)
    if m:
        return m.group(1)
    if "/" not in raw and "?" not in raw and "#" not in raw:
        return raw
    return None


def _qualities_from_gql(video_qualities: List[dict]) -> List[str]:
    labels: List[str] = []
    for q in video_qualities or []:
        try:
            height = int(q.get("quality") or 0)
        except (TypeError, ValueError):
            continue
        if height <= 0:
            continue
        fps = q.get("frameRate")
        try:
            fps_suffix = "60" if fps and float(fps) > 30 else ""
        except (TypeError, ValueError):
            fps_suffix = ""
        label = f"{height}p{fps_suffix}"
        if label not in labels:
            labels.append(label)
    labels.sort(key=lambda s: int(re.search(r"\d+", s).group()), reverse=True)
    return labels


def _clip_progressive_formats(video_qualities: List[dict]) -> List[Dict[str, Any]]:
    """Format dicts compatible with preview_service progressive variant picking."""
    out: List[Dict[str, Any]] = []
    for q in video_qualities or []:
        try:
            height = int(q.get("quality") or 0)
        except (TypeError, ValueError):
            continue
        url = (q.get("sourceURL") or "").strip()
        if not height or not url:
            continue
        out.append({
            "height": height,
            "url": url,
            "ext": "mp4",
            "protocol": "https",
            "tbr": float(height),
        })
    out.sort(key=lambda f: int(f.get("height") or 0), reverse=True)
    return out


def _fetch_clip_node(url_or_slug: str) -> Dict[str, Any]:
    slug = _extract_clip_slug(url_or_slug)
    if not slug:
        raise ValueError(f"Not a Twitch clip URL or slug: {url_or_slug}")
    data = _gql_request(CLIP_INFO_QUERY, {"slug": slug})
    node = data.get("clip")
    if not node:
        raise RuntimeError(f"Twitch clip not found: {slug}")
    return node


def get_clip_info_sync(url_or_slug: str) -> Dict[str, Any]:
    """Return Twitch clip metadata via GQL (~0.3-1s, no yt-dlp)."""
    node = _fetch_clip_node(url_or_slug)
    slug = str(node.get("slug") or _extract_clip_slug(url_or_slug) or "")
    broadcaster = node.get("broadcaster") or {}
    login = broadcaster.get("login") or broadcaster.get("displayName")
    duration = node.get("durationSeconds")
    qualities = _qualities_from_gql(node.get("videoQualities") or [])
    clip_url = f"https://clips.twitch.tv/{slug}" if slug else url_or_slug
    from services.size_estimate import enrich_info_dict

    payload = {
        "id": str(node.get("id") or slug),
        "title": node.get("title") or "Untitled",
        "uploader": broadcaster.get("displayName") or login,
        "channel": login,
        "duration": duration,
        "duration_string": _format_duration(duration),
        "thumbnail": node.get("thumbnailURL"),
        "views": node.get("viewCount"),
        "webpage_url": clip_url,
        "qualities": qualities,
        "platform": "Twitch",
        "created_at": node.get("createdAt"),
        "content_kind": "clip",
    }
    enrich_info_dict(
        payload,
        progressive_variants=node.get("videoQualities") or [],
        is_clip=True,
    )
    return payload


def get_clip_progressive_variants_sync(url_or_slug: str) -> List[Dict[str, Any]]:
    """Progressive MP4 variants for Twitch clip preview (~0.3-1s)."""
    node = _fetch_clip_node(url_or_slug)
    return _clip_progressive_formats(node.get("videoQualities") or [])


def _extract_video_id(url_or_id: str) -> Optional[str]:
    raw = (url_or_id or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d+", raw):
        return raw
    m = re.search(r"twitch\.tv/videos/(\d+)", raw, re.I)
    if m:
        return m.group(1)
    return None


def _gql_request(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        TWITCH_GQL_URL,
        data=payload,
        headers={
            "Client-Id": TWITCH_GQL_CLIENT_ID,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"Twitch GQL HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Twitch GQL request failed: {e}") from e

    if body.get("errors"):
        msg = body["errors"][0].get("message", "Unknown GQL error")
        raise RuntimeError(msg)
    return body.get("data") or {}


def _gql_persisted(operation_name: str, sha256_hash: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    payload = json.dumps({
        "operationName": operation_name,
        "variables": variables,
        "extensions": {
            "persistedQuery": {"version": 1, "sha256Hash": sha256_hash},
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        TWITCH_GQL_URL,
        data=payload,
        headers={
            "Client-Id": TWITCH_GQL_CLIENT_ID,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"Twitch GQL HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Twitch GQL request failed: {e}") from e

    if isinstance(body, list):
        body = body[0] if body else {}
    if body.get("errors"):
        msg = body["errors"][0].get("message", "Unknown GQL error")
        raise RuntimeError(msg)
    return body.get("data") or {}


def list_channel_videos_sync(login: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Return recent archive VODs for a Twitch channel login."""
    login = (login or "").strip().lower()
    if not login:
        return []

    limit = max(1, min(int(limit), 100))
    out: List[Dict[str, Any]] = []
    cursor: Optional[str] = None

    while len(out) < limit:
        batch = min(100, limit - len(out))
        data = _gql_request(CHANNEL_VIDEOS_QUERY, {"login": login, "first": batch, "after": cursor})
        user = data.get("user")
        if not user:
            break

        block = user.get("videos") or {}
        edges = block.get("edges") or []
        for edge in edges:
            node = edge.get("node") or {}
            vid = str(node.get("id") or "").strip()
            if not vid:
                continue
            duration = node.get("lengthSeconds")
            out.append({
                "id": vid,
                "platform": "Twitch",
                "title": node.get("title") or "Untitled",
                "duration": duration,
                "duration_string": _format_duration(duration),
                "created_at": node.get("createdAt") or None,
                "views": node.get("viewCount"),
                "thumbnail_url": node.get("previewThumbnailURL"),
                "url": f"https://www.twitch.tv/videos/{vid}",
                "content_kind": "vod",
            })
            if len(out) >= limit:
                break

        page = block.get("pageInfo") or {}
        if not page.get("hasNextPage") or len(out) >= limit:
            break
        cursor = page.get("endCursor")

    return out


CLIP_MAX_DURATION_SEC = 60
# Twitch clips page uses ?range=7d — maps to LAST_WEEK in GQL.
TWITCH_CLIPS_RANGE_FILTER = "LAST_WEEK"


def list_channel_clips_sync(login: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Return the *limit* most recent clips (<=60s), ranked by view count (desc).

    Equivalent to https://www.twitch.tv/{login}/clips?range=7d
    """
    login = (login or "").strip().lower()
    if not login:
        return []

    limit = max(1, min(int(limit), 10))
    fetch_n = max(limit, 20)
    data = _gql_persisted(
        "ClipsCards__User",
        CLIPS_CARDS_USER_HASH,
        {
            "login": login,
            "limit": fetch_n,
            "criteria": {"filter": TWITCH_CLIPS_RANGE_FILTER},
        },
    )
    user = data.get("user")
    if not user:
        raise ValueError(f"Twitch channel not found: {login}")

    parsed: List[Dict[str, Any]] = []
    for edge in (user.get("clips") or {}).get("edges") or []:
        node = edge.get("node") or {}
        slug = str(node.get("slug") or "").strip()
        if not slug:
            continue
        duration = node.get("durationSeconds")
        if duration is not None:
            try:
                if float(duration) > CLIP_MAX_DURATION_SEC:
                    continue
            except (TypeError, ValueError):
                pass
        clip_url = node.get("url") or f"https://clips.twitch.tv/{slug}"
        parsed.append({
            "id": str(node.get("id") or slug),
            "platform": "Twitch",
            "title": node.get("title") or "Untitled",
            "duration": duration,
            "duration_string": _format_duration(duration),
            "created_at": node.get("createdAt") or None,
            "views": node.get("viewCount"),
            "thumbnail_url": node.get("thumbnailURL"),
            "url": clip_url,
            "channel": login,
            "content_kind": "clip",
        })

    parsed.sort(key=lambda v: v.get("created_at") or "", reverse=True)
    recent = parsed[:limit]
    recent.sort(key=lambda v: v.get("views") or 0, reverse=True)
    return recent


def _twitch_vod_playback_for_estimate(video_id: str) -> tuple[Optional[str], dict, list]:
    """HLS URL, authenticated headers, and formats from one yt-dlp probe."""
    empty_headers: dict = {
        "Referer": "https://www.twitch.tv/",
        "Origin": "https://www.twitch.tv",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
    }
    try:
        import yt_dlp

        url = f"https://www.twitch.tv/videos/{video_id}"
        with yt_dlp.YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
        }) as ydl:
            info = ydl.extract_info(url, download=False)
        formats = list(info.get("formats") or [])
        best_url: Optional[str] = None
        best_height = -1
        best_headers: dict = dict(empty_headers)
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            if fmt.get("vcodec") in (None, "none"):
                continue
            proto = (fmt.get("protocol") or "").lower()
            ext = (fmt.get("ext") or "").lower()
            if "m3u8" not in proto and ext not in ("m3u8", "mp4"):
                continue
            fu = (fmt.get("url") or "").strip()
            if not fu:
                continue
            try:
                height = int(fmt.get("height") or 0)
            except (TypeError, ValueError):
                height = 0
            if height >= best_height:
                best_height = height
                best_url = fu
                fmt_headers = fmt.get("http_headers")
                if isinstance(fmt_headers, dict) and fmt_headers:
                    best_headers = {**empty_headers, **fmt_headers}
        return best_url, best_headers, formats
    except Exception as exc:
        logger.debug("Twitch VOD playback probe failed %s: %s", video_id, exc)
        return None, empty_headers, []


def get_video_info_sync(url_or_id: str) -> Dict[str, Any]:
    """Return metadata for a single Twitch VOD via GQL (~0.5-2s)."""
    vid = _extract_video_id(url_or_id)
    if not vid:
        raise ValueError(f"Not a Twitch VOD URL or id: {url_or_id}")

    data = _gql_request(VIDEO_INFO_QUERY, {"id": vid})
    node = data.get("video")
    if not node:
        raise RuntimeError(f"Twitch video not found: {vid}")

    owner = node.get("owner") or {}
    game = node.get("game") or {}
    login = owner.get("login") or owner.get("displayName")
    duration = node.get("lengthSeconds")

    from services.size_estimate import enrich_info_dict

    payload = {
        "id": str(node.get("id") or vid),
        "title": node.get("title") or "Untitled",
        "uploader": owner.get("displayName") or login,
        "channel": login,
        "duration": duration,
        "duration_string": _format_duration(duration),
        "thumbnail": node.get("previewThumbnailURL"),
        "views": node.get("viewCount"),
        "category": game.get("displayName"),
        "webpage_url": f"https://www.twitch.tv/videos/{vid}",
        "qualities": [],
        "platform": "Twitch",
        "created_at": node.get("createdAt"),
    }
    m3u8_url, m3u8_headers, formats = _twitch_vod_playback_for_estimate(vid)
    enrich_info_dict(
        payload,
        formats=formats,
        m3u8_url=m3u8_url,
        m3u8_headers=m3u8_headers,
        is_clip=False,
    )
    return payload
