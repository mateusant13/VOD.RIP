"""Twitch channel VOD listings via the public GQL API (includes createdAt)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TWITCH_GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TWITCH_GQL_URL = "https://gql.twitch.tv/gql"

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


def _gql_request(variables: Dict[str, Any]) -> Dict[str, Any]:
    payload = json.dumps({"query": CHANNEL_VIDEOS_QUERY, "variables": variables}).encode("utf-8")
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
        data = _gql_request({"login": login, "first": batch, "after": cursor})
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
            out.append({
                "id": vid,
                "platform": "Twitch",
                "title": node.get("title") or "Untitled",
                "duration": node.get("lengthSeconds"),
                "created_at": node.get("createdAt"),
                "views": node.get("viewCount"),
                "thumbnail_url": node.get("previewThumbnailURL"),
                "url": f"https://www.twitch.tv/videos/{vid}",
            })
            if len(out) >= limit:
                break

        page = block.get("pageInfo") or {}
        if not page.get("hasNextPage") or len(out) >= limit:
            break
        cursor = page.get("endCursor")

    return out
