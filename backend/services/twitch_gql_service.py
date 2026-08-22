"""Twitch metadata via the public GQL API (fast — no yt-dlp)."""

from __future__ import annotations

import json
import logging

from services.http_fingerprint import twitch_http_headers
import random
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urljoin

logger = logging.getLogger(__name__)

TWITCH_GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

_RESOLUTION_CANDIDATES = [
    ("chunked", 1080, 60, 9_000_000),
    ("1440p60", 1440, 60, 10_000_000),
    ("1080p60", 1080, 60, 6_000_000),
    ("720p60", 720, 60, 3_000_000),
    ("480p30", 480, 30, 600_000),
    ("360p30", 360, 30, 400_000),
    ("160p30", 160, 30, 200_000),
]
'''Pre-defined Twitch quality tiers from highest to lowest.  Source-quality
"chunked" is tried first; only resolutions that respond 200 are included.
Bandwidth values are sensible estimates; the variant playlist itself carries
actual BANDWIDTH when fetched.  (ponytail: no realtime m3u8 parse for
bandwidth — probes are cheap enough.)
'''
TWITCH_GQL_URL = "https://gql.twitch.tv/gql"


class TwitchVodUnavailable(RuntimeError):
    """No Twitch VOD playback path worked (sub-only / geo-restricted / removed).

    Raised instead of silently falling into a slow yt-dlp extract that would
    reproduce the same failure — the caller surfaces it immediately.
    """

CLIPS_CARDS_USER_HASH = "90c33f5e6465122fba8f9371e2a97076f9ed06c6fed3788d002ab9eba8f91d88"
CLIP_ACCESS_TOKEN_HASH = "993d9a5131f15a37bd16f32342c44ed1e0b1a9b968c6afdb662d2cddd595f6c5"
VOD_PLAYBACK_TOKEN_HASH = "ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9"

# Deep-paging ceilings for the anonymous GQL channel lists.
#  - Videos connection: cursor pages of 100 (sort: TIME). Bound the crawl at
#    1000 (10 pages) so show-more terminates; a channel with more keeps
#    has_more=true until the page request crosses the ceiling, then has_more
#    goes false instead of looping forever.
#  - Clips connection: documented to cap around ~1100 nodes; the crawler's
#    max_pages already bounds it (5 pages recent, 10 era, 3 ALL_TIME).
# ponytail: the anonymous GQL path is the fast no-auth route; if a channel
# needs deeper lists than these ceilings, the upgrade path is the official
# Helix API (cursor + 100/page, no documented node cap).
TWITCH_VIDEOS_CEILING = 1000
TWITCH_CLIPS_CEILING = 1000

CHANNEL_VIDEOS_QUERY = """
query ChannelVideos($login: String!, $first: Int!, $after: Cursor) {
  user(login: $login) {
    videos(first: $first, after: $after, sort: TIME) {
      pageInfo { hasNextPage endCursor }
      edges {
        node {
          id
          title
          createdAt
          lengthSeconds
          viewCount
          previewThumbnailURL(width: 320, height: 180)
          language
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
    videoOffsetSeconds
    viewCount
    createdAt
    thumbnailURL
    videoQualities {
      quality
      sourceURL
      frameRate
    }
    video {
      id
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

VOD_META_QUERY = """
query SubOnlyVideoMeta($id: ID!) {
  video(id: $id) {
    broadcastType
    createdAt
    seekPreviewsURL
    owner {
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


def _iso_ts(value: Any) -> Optional[float]:
    """Parse an ISO-8601 timestamp to epoch seconds (None on failure)."""
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


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
        "vod_id": ((node.get("video") or {}).get("id") if isinstance(node.get("video"), dict) else None),
        "offset_sec": node.get("videoOffsetSeconds"),
    }
    # Probe signed CloudFront URLs for accurate size estimates. Unsigned sourceURL
    # values redirect/403 from CloudFront, so the HEAD probe must use sig/token.
    try:
        signed_variants = get_clip_signed_variants_sync(slug or url_or_slug)
    except Exception:
        signed_variants = []
    enrich_info_dict(
        payload,
        progressive_variants=signed_variants or (node.get("videoQualities") or []),
        is_clip=True,
    )
    return payload


def get_clip_progressive_variants_sync(url_or_slug: str) -> List[Dict[str, Any]]:
    """Progressive MP4 variants for Twitch clip preview (~0.3-1s)."""
    node = _fetch_clip_node(url_or_slug)
    return _clip_progressive_formats(node.get("videoQualities") or [])


def _signed_clip_progressive_formats(
    video_qualities: List[dict],
    sig: str,
    token: str,
) -> List[Dict[str, Any]]:
    """Same as _clip_progressive_formats but appends playback-access query params."""
    out: List[Dict[str, Any]] = []
    for q in video_qualities or []:
        try:
            height = int(q.get("quality") or 0)
        except (TypeError, ValueError):
            continue
        url = (q.get("sourceURL") or "").strip()
        if not height or not url:
            continue
        signed_url = f"{url}?{urlencode({'sig': sig, 'token': token})}"
        out.append({
            "height": height,
            "url": signed_url,
            "ext": "mp4",
            "protocol": "https",
            "tbr": float(height),
        })
    out.sort(key=lambda f: int(f.get("height") or 0), reverse=True)
    return out


def get_clip_signed_variants_sync(url_or_slug: str) -> List[Dict[str, Any]]:
    """Fast, signed progressive MP4 variants for a Twitch clip.

    Uses the public ``VideoAccessToken_Clip`` persisted query so the returned
    URLs include the ``sig``/``token`` query params required by CloudFront.
    """
    slug = _extract_clip_slug(url_or_slug)
    if not slug:
        raise ValueError(f"Not a Twitch clip URL or slug: {url_or_slug}")
    data = _gql_persisted(
        "VideoAccessToken_Clip",
        CLIP_ACCESS_TOKEN_HASH,
        {"slug": slug, "platform": "web"},
    )
    clip = data.get("clip")
    if not clip:
        raise RuntimeError(f"Twitch clip not found: {slug}")
    token_data = clip.get("playbackAccessToken") or {}
    sig = token_data.get("signature")
    token = token_data.get("value")
    if not sig or not token:
        raise RuntimeError(f"Twitch clip access token missing: {slug}")
    return _signed_clip_progressive_formats(
        clip.get("videoQualities") or [], sig, token
    )


_HLS_STREAM_INF_RE = re.compile(r"#EXT-X-STREAM-INF[\s\S]*?\n([^#\n].*)", re.IGNORECASE)
_HLS_RESOLUTION_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)", re.IGNORECASE)
_HLS_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)", re.IGNORECASE)
_HLS_FRAMERATE_RE = re.compile(r"FRAME-RATE=([\d.]+)", re.IGNORECASE)


def _parse_hls_master_variants(master_url: str, master_text: str) -> List[Dict[str, Any]]:
    """Parse variant URLs and heights from a Twitch HLS master playlist."""
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for block in master_text.split("#EXT-X-STREAM-INF")[1:]:
        lines = block.splitlines()
        if not lines:
            continue
        info_line = lines[0]
        url_line = ""
        for line in lines[1:]:
            stripped = line.strip()
            if stripped:
                url_line = stripped
                break
        if not url_line:
            continue
        variant_url = url_line if url_line.startswith("http") else urljoin(master_url, url_line)
        if variant_url in seen:
            continue
        seen.add(variant_url)
        m = _HLS_RESOLUTION_RE.search(info_line)
        height = int(m.group(2)) if m else 0
        bw = _HLS_BANDWIDTH_RE.search(info_line)
        fr = _HLS_FRAMERATE_RE.search(info_line)
        nm = re.search(r'NAME="([^"]+)"', info_line)
        out.append({
            "height": height,
            "url": variant_url,
            "ext": "mp4",
            "protocol": "m3u8_native",
            # codecs/tbr/fps let size_estimate treat these like yt-dlp formats
            "vcodec": "h264",
            "acodec": "mp4a.40.2",
            "tbr": (int(bw.group(1)) / 1000.0) if bw else None,
            "fps": float(fr.group(1)) if fr else None,
            # 'audio_only_64' etc. — lets the transcribe worker pick the
            # audio-only variant for at-transcribe-time downloads.
            "name": nm.group(1) if nm else "",
        })
    out.sort(key=lambda f: int(f.get("height") or 0), reverse=True)
    return out


def _get_vod_meta_sync(vod_id: str) -> dict:
    """Fetch video metadata needed for cloudfront CDN bypass.

    Twitch answers this inline (non-persisted) query even for sub-only VODs.
    Returns the ``video`` dict (or raises).
    """
    data = _gql_request(
        VOD_META_QUERY,
        {"id": vod_id},
    )
    video = (data or {}).get("video") or {}
    if not video.get("seekPreviewsURL"):
        raise RuntimeError(f"VOD {vod_id} has no seekPreviewsURL")
    return video


def _resolve_cloudfront_variants(vod_id: str, video_data: dict) -> List[Dict[str, Any]]:
    """Probe cloudfront CDN for working VOD variant playlists.

    Mirror of the TwitchNoSub userscript approach — no access token needed.
    Uses ``seekPreviewsURL`` metadata to reconstruct CDN paths, then probes
    each quality tier.  Only responsive URLs are returned.

    Returns the same format as ``_parse_hls_master_variants``:
    ``[{url, height, width, tbr, fps, protocol, vcodec, acodec, ext}, ...]``.
    """
    seek_url = video_data.get("seekPreviewsURL", "")
    m = re.match(r"(https?://[^/]+)/([^/]+)/storyboards/", seek_url)
    if not m:
        raise RuntimeError(f"Cannot parse seekPreviewsURL: {seek_url}")
    domain = m.group(1)
    vod_special_id = m.group(2)
    broadcast_type = (video_data.get("broadcastType") or "").upper()
    owner = video_data.get("owner") or {}
    channel_login = (owner.get("login") or "").lower()
    created_at = video_data.get("createdAt") or ""

    is_highlight = broadcast_type == "HIGHLIGHT"

    def _variant_url(res_key: str) -> str:
        if is_highlight:
            return f"{domain}/{vod_special_id}/{res_key}/highlight-{vod_id}.m3u8"
        return f"{domain}/{vod_special_id}/{res_key}/index-dvr.m3u8"

    # Candidate URLs: the standard path first, then the upload-non-partner
    # path (needs channel_login + vod_id) — probed in ONE parallel pass.
    candidates: List[Tuple[str, int, int, int, str]] = [
        (res_key, height, fps, tbr, _variant_url(res_key))
        for res_key, height, fps, tbr in _RESOLUTION_CANDIDATES
    ]
    if channel_login and not is_highlight:
        candidates.extend(
            (
                res_key,
                height,
                fps,
                tbr,
                f"{domain}/{channel_login}/{vod_id}/{vod_special_id}/{res_key}/index-dvr.m3u8",
            )
            for res_key, height, fps, tbr in _RESOLUTION_CANDIDATES
        )

    def _responsive(url: str) -> bool:
        try:
            req = urllib.request.Request(url, headers=twitch_http_headers())
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status == 200 and bool(r.read(65536))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            return False

    # ponytail: the old loop probed up to 14 URLs SEQUENTIALLY with 10s
    # timeouts — a sub-only VOD drained 70-140s behind the play button.
    # Parallel probe caps the drain at ~10s while every responsive tier is
    # still returned (quality selection preserved).
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
        ok_flags = list(pool.map(_responsive, [c[4] for c in candidates]))

    variants: List[Dict[str, Any]] = []
    seen_heights: set = set()
    for (res_key, height, fps, tbr, url), ok in zip(candidates, ok_flags):
        if not ok or height in seen_heights:
            continue
        seen_heights.add(height)
        variants.append({
            "url": url,
            "height": height,
            "width": int(height * 16 / 9) if height else 0,
            "tbr": tbr,
            "fps": fps,
            "protocol": "m3u8_native",
            "vcodec": "h264",
            "acodec": "mp4a.40.2",
            "ext": "mp4",
        })

    if not variants:
        raise TwitchVodUnavailable(
            f"Twitch VOD {vod_id} has no playable stream — it is sub-only, "
            "geo-restricted, or removed (log in with Twitch cookies and retry)"
        )

    return variants


def get_vod_playback_sync(url_or_id: str) -> Tuple[str, dict, List[Dict[str, Any]]]:
    """Fast Twitch VOD playback URL via GQL + Usher (no yt-dlp).

    Returns (master_m3u8_url, headers, variant_formats).
    """
    vid = _extract_video_id(url_or_id)
    if not vid:
        raise ValueError(f"Not a Twitch VOD URL or id: {url_or_id}")

    data = _gql_persisted(
        "PlaybackAccessToken",
        VOD_PLAYBACK_TOKEN_HASH,
        {
            "isLive": False,
            "login": "",
            "isVod": True,
            "vodID": vid,
            "playerType": "embed",
            "platform": "site",
        },
    )
    token_node = data.get("videoPlaybackAccessToken") or data.get("playbackAccessToken") or {}
    sig = token_node.get("signature")
    token = token_node.get("value")

    if sig and token:
        query = urlencode({
            "allow_source": "true",
            "allow_audio_only": "true",
            "playlist_include_framerate": "true",
            "supported_codecs": "h264",
            "platform": "web",
            "p": str(random.randint(1_000_000, 9_999_999)),
            "nauth": token,
            "nauthsig": sig,
        })
        master_url = f"https://usher.ttvnw.net/vod/v2/{vid}.m3u8?{query}"

        headers = twitch_http_headers()
        try:
            req = urllib.request.Request(master_url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=15) as resp:
                master_text = resp.read().decode("utf-8", errors="replace")
            variants = _parse_hls_master_variants(master_url, master_text)
            return master_url, headers, variants
        except (urllib.error.HTTPError, urllib.error.URLError):
            logger.info("Usher fetch failed for %s — trying cloudfront bypass", vid)

    logger.info("Trying cloudfront CDN bypass for VOD %s", vid)
    video_data = _get_vod_meta_sync(vid)
    cf_variants = _resolve_cloudfront_variants(vid, video_data)
    if not cf_variants:
        raise RuntimeError(f"No playable variants for VOD {vid}")
    cf_headers: dict = twitch_http_headers()
    # Use the highest quality variant as the "master" URL placeholder.
    # The synthetic master playlist later built by the caller uses
    # absolute variant URLs, so the base is irrelevant.
    return cf_variants[0]["url"], cf_headers, cf_variants


def _extract_video_id(url_or_id: str) -> Optional[str]:
    raw = (url_or_id or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d+", raw):
        return raw
    # Accept both twitch.tv/videos/{id} and the channel-prefixed form
    # twitch.tv/{channel}/videos/{id} (the latter is what browser share
    # buttons and the acceptance timing URL use).
    m = re.search(r"twitch\.tv/(?:[^/]+/)?videos/(\d+)", raw, re.I)
    if m:
        return m.group(1)
    return None


def _gql_headers() -> Dict[str, str]:
    """Base GQL headers plus bridge cookies when present (additive).

    Existing headers are never replaced — the Cookie header is added only
    when the bridge holds Twitch cookies (auth-token/sp), so requests stay
    identical when the bridge is disabled or empty (regression bar).
    """
    headers = {
        "Client-Id": TWITCH_GQL_CLIENT_ID,
        "Content-Type": "application/json",
    }
    try:
        from services.cookie_bridge import cookie_header

        cookie = cookie_header("twitch")
        if cookie:
            headers["Cookie"] = cookie
    except Exception:
        pass
    return headers


def _gql_request(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        TWITCH_GQL_URL,
        data=payload,
        headers=_gql_headers(),
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
        headers=_gql_headers(),
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


def list_channel_videos_sync(
    login: str, limit: int = 100, *, return_has_more: bool = False
) -> List[Dict[str, Any]]:
    """Return recent VODs/highlights/uploads for a Twitch channel login.

    return_has_more: when True, return (items, has_more) — has_more is the
    GQL pageInfo.hasNextPage at the stop point, i.e. whether a deeper page
    exists (False when the connection is exhausted OR the request crossed
    TWITCH_VIDEOS_CEILING)."""
    login = (login or "").strip().lower()
    if not login:
        return ([], False) if return_has_more else []

    limit = max(1, min(int(limit), TWITCH_VIDEOS_CEILING))

    out: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    has_more = False

    while len(out) < limit:
        batch = min(100, limit - len(out))
        data = _gql_request(CHANNEL_VIDEOS_QUERY, {"login": login, "first": batch, "after": cursor})
        user = data.get("user")
        if not user:
            has_more = False
            break

        block = user.get("videos") or {}
        page = block.get("pageInfo") or {}
        has_more = bool(page.get("hasNextPage"))
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
                # VOD language = the broadcaster language at stream time
                # (the closest anonymous GQL analogue of the official API's
                # broadcaster_language — 'pt', 'en', ... or None).
                "language": node.get("language") or None,
            })
            if len(out) >= limit:
                break

        if not has_more or len(out) >= limit:
            break
        cursor = page.get("endCursor")

    if return_has_more:
        # Boundary guard: once the request depth is clamped at the ceiling the
        # connection may still report hasNextPage=true (a >1000-VOD channel) —
        # has_more must go False there or show-more loops forever on empty
        # pages at the bound.
        return out, has_more and limit < TWITCH_VIDEOS_CEILING
    return out


CLIP_MAX_DURATION_SEC = 60
# Primary: clips from the last week (small window so even low-view clips surface).
# Fallback to ALL_TIME only if LAST_WEEK returned too few.
TWITCH_CLIPS_RANGE_FILTER = "ALL_TIME"  # fallback

def list_channel_clips_sync(
    login: str, limit: int = 10,
    *,
    range_label: str = "LAST_WEEK",
    sort: str = "date",
    older_than_days: int = 0,
    newer_than_days: int = 0,
) -> List[Dict[str, Any]]:
    """Return the *limit* most recent clips (<=60s).

    range_label: Twitch GQL ClipsFilter — LAST_DAY/LAST_WEEK/LAST_MONTH/ALL_TIME.
    For ranges >1mo the GQL window must be ALL_TIME; the caller client-filters
    the desired window after this call.

    older_than_days/newer_than_days: era-window paging. When older_than_days
    >0, keep paginating until we have enough clips inside
    [newer_than_days, older_than_days] or pass its older edge — era callers
    (e.g. 6mo) need depth, not just the newest page.

    sort: 'date' (newest first) or 'views' (most viewed first).
    """
    login = (login or "").strip().lower()
    if not login:
        return []

    limit = max(1, min(int(limit), TWITCH_CLIPS_CEILING))
    fetch_n = max(limit, 100)
    older_cutoff: Optional[float] = None
    newer_cutoff: Optional[float] = None
    if older_than_days > 0:
        import time as _time

        older_cutoff = _time.time() - older_than_days * 86400
        newer_cutoff = _time.time() - max(0, newer_than_days) * 86400

    # Enough in-window clips for the UI (10 shown + show-more headroom).
    # Deep page requests scale the target: a show-more page at offset N asks
    # for N+limit clips, so the crawl must not stop short of the page end
    # (probe +1 keeps has_more true while more in-window clips exist).
    _IN_WINDOW_TARGET = 100
    _in_window_target = max(_IN_WINDOW_TARGET, fetch_n + 1)

    def _fetch(filter_label: str = "LAST_WEEK") -> List[Dict[str, Any]]:
        """Fetch clips from Twitch GQL with the given filter, paginating."""
        out: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        # ponytail: page ceilings for deep era windows. ALL_TIME is NOT
        # date-ordered (popularity order) so early date-stop is unreliable
        # there — cap it lower and rely on the in-window count instead.
        # Upgrade path: raise caps or binary-search the cursor by date.
        if older_cutoff is None:
            max_pages = 5
        elif filter_label == "ALL_TIME":
            max_pages = 3
        else:
            max_pages = 10
        pages = 0
        in_window = 0
        while True:
            variables: Dict[str, Any] = {
                "login": login,
                "limit": 100,
                "criteria": {"filter": filter_label},
            }
            if cursor:
                variables["cursor"] = cursor
            data = _gql_persisted(
                "ClipsCards__User",
                CLIPS_CARDS_USER_HASH,
                variables,
            )
            user = data.get("user")
            if not user:
                raise ValueError(f"Twitch channel not found: {login}")
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
                out.append({
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
                if len(out) >= fetch_n and older_cutoff is None:
                    break
                if older_cutoff is not None:
                    ts = _iso_ts(node.get("createdAt"))
                    if ts is not None and ts <= (newer_cutoff or ts) and ts >= older_cutoff:
                        in_window += 1
            pages += 1
            pi = (user.get("clips") or {}).get("pageInfo") or {}
            if not pi.get("hasNextPage") or pages >= max_pages:
                break
            if older_cutoff is not None:
                if in_window >= _in_window_target:
                    break
                if filter_label != "ALL_TIME" and sort != "views":
                    # Date-ordered bucket: past the older edge = full window.
                    last_ts = _iso_ts(out[-1].get("created_at")) if out else None
                    if last_ts is not None and last_ts <= older_cutoff:
                        break
            elif len(out) >= fetch_n:
                break
            cursor = pi.get("endCursor")
        return out

    # Map UI range days to the smallest Twitch GQL window that covers it.
    # Today/7d/14d/1mo map directly; 6mo/1y/All must use ALL_TIME (no wider GQL
    # option exists). The caller is responsible for client-side filtering the
    # exact day count via the `days` parameter in the API layer.
    if range_label not in {"LAST_DAY", "LAST_WEEK", "LAST_MONTH", "ALL_TIME"}:
        range_label = "LAST_WEEK"
    parsed = _fetch(range_label)
    # If we asked LAST_DAY/WEEK/MONTH and got nothing back (channel with very
    # few clips), widen to ALL_TIME and merge so the user sees the channel's
    # most-recent content regardless of bucket.
    if not parsed and range_label != "ALL_TIME":
        parsed = _fetch("ALL_TIME")

    if sort == "views":
        parsed.sort(key=lambda v: int(v.get("views") or 0), reverse=True)
    else:
        parsed.sort(key=lambda v: v.get("created_at") or "", reverse=True)
    if older_cutoff is not None:
        # Era window: the API layer filters the exact age window and the UI
        # pages client-side — return the whole deep fetch, not the newest 10.
        return parsed
    return parsed[:limit]


def _twitch_vod_playback_for_estimate(video_id: str) -> tuple[Optional[str], dict, list]:
    """HLS URL, authenticated headers, and formats from one yt-dlp probe."""
    empty_headers: dict = twitch_http_headers()
    try:
        import yt_dlp

        from services.ytdlp_guard import guarded_youtube_dl
        url = f"https://www.twitch.tv/videos/{video_id}"
        with guarded_youtube_dl({
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
    # ponytail: best-effort — return best_url, best_headers, formats
        logger.debug("Twitch VOD playback probe failed %s: %s", video_id, exc)
        return None, empty_headers, []


def _attach_playback_estimate(payload: Dict[str, Any], vid: str) -> None:
    """Fast path: GQL playback token + usher master (~0.6s) instead of the
    yt-dlp probe (~4.7s). Variants carry tbr/fps so enrich_info_dict needs
    no further network. Fall back to the yt-dlp probe on any failure."""
    try:
        m3u8_url, m3u8_headers, formats = get_vod_playback_sync(vid)
        master_parsed = bool(formats)
    except Exception as exc:
        logger.debug("fast VOD playback failed for %s, using yt-dlp probe: %s", vid, exc)
        m3u8_url, m3u8_headers, formats = _twitch_vod_playback_for_estimate(vid)
        master_parsed = False
    from services.size_estimate import enrich_info_dict

    enrich_info_dict(
        payload,
        formats=formats,
        # Fast path already fetched+parsed the master — don't fetch it again.
        m3u8_url=None if master_parsed else m3u8_url,
        m3u8_headers=m3u8_headers,
        is_clip=False,
    )


def get_video_info_sync(url_or_id: str) -> Dict[str, Any]:
    """Return metadata for a single Twitch VOD."""
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
    _attach_playback_estimate(payload, vid)
    return payload


TWITCH_STREAM_STATUS_QUERY = """
query ChannelStream($login: String!) {
  user(login: $login) {
    stream {
      id
      title
      viewersCount
      startedAt
    }
  }
}
"""

# Slack for clock skew when deciding whether a VOD predates the ongoing
# stream: a live broadcast's own VOD is only published after it ends, so a
# live stream + a VOD created more than this far before the stream start
# means the VOD is a PREVIOUS broadcast (never the current one).
VOD_STREAM_START_TOLERANCE_SEC = 300.0


def get_channel_stream_status_sync(login: str) -> Optional[dict]:
    """Live status for a Twitch channel: ``{live, started_at}`` or None.

    One cheap GQL query (channel stream node). ``started_at`` is the ISO-8601
    stream start. None means the query failed transiently — callers MUST
    preserve their pre-status behavior in that case.
    """
    login = (login or "").strip().lower()
    if not login:
        return None
    try:
        data = _gql_request(TWITCH_STREAM_STATUS_QUERY, {"login": login})
        stream = ((data or {}).get("user") or {}).get("stream")
    except Exception:
        return None
    if not stream:
        return {"live": False, "started_at": None}
    return {
        "live": True,
        "started_at": stream.get("startedAt") or None,
        "title": stream.get("title") or None,
        "viewers": int(stream.get("viewersCount") or 0),
    }


def twitch_video_created_at(vod_id_or_url: str) -> Optional[str]:
    """ISO-8601 ``createdAt`` for a single Twitch VOD (one GQL query).

    Accepts a video id or a twitch.tv/videos/{id} URL (same shapes as
    ``_extract_video_id``). None on parse failure, missing video, or a
    transient query failure.
    """
    vid = _extract_video_id(vod_id_or_url)
    if not vid:
        return None
    try:
        data = _gql_request(VIDEO_INFO_QUERY, {"id": vid})
        video = (data or {}).get("video") or {}
        return video.get("createdAt") or None
    except Exception:
        return None


def is_vod_previous_broadcast(
    vod_created_at: Optional[str],
    stream_status: Optional[dict],
    *,
    tolerance_sec: float = VOD_STREAM_START_TOLERANCE_SEC,
) -> bool:
    """True when a VOD clearly predates the channel's ONGOING live stream.

    A live broadcast's own VOD is published only after the stream ends, so
    while a stream is live the newest listed VOD is always a PREVIOUS
    broadcast — unless its ``createdAt`` sits within ``tolerance_sec`` of the
    stream start (clock skew; the just-ended broadcast case is covered by
    ``live=False``). Stream offline, missing timestamps, or a failed status
    query (None) all return False — never break replay on transient failure.
    """
    if not stream_status or not stream_status.get("live"):
        return False
    stream_start = _iso_ts(stream_status.get("started_at"))
    vod_ts = _iso_ts(vod_created_at)
    if stream_start is None or vod_ts is None:
        return False
    return abs(stream_start - vod_ts) > tolerance_sec


assert "type: ARCHIVE" not in CHANNEL_VIDEOS_QUERY
assert TWITCH_CLIPS_RANGE_FILTER == "ALL_TIME"
