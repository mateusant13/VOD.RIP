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


def list_channel_clips_sync(login: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Return the *limit* most recent clips, ranked by view count (desc)."""
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
            "criteria": {"filter": "LAST_WEEK"},
        },
    )
    user = data.get("user")
    if not user:
        return []

    parsed: List[Dict[str, Any]] = []
    for edge in (user.get("clips") or {}).get("edges") or []:
        node = edge.get("node") or {}
        slug = str(node.get("slug") or "").strip()
        if not slug:
            continue
        duration = node.get("durationSeconds")
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

    return {
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
