"""Fast YouTube preview/info via InnerTube multi-client fallback (~400ms vs ~3s yt-dlp)."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

_INNERTUBE_PLAYER_URL = (
    "https://www.youtube.com/youtubei/v1/player"
    "?key=AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
)
_STREAM_HEADERS = {
    "Referer": "https://www.youtube.com/",
    "Origin": "https://www.youtube.com",
}
_RESOLUTION_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)")
_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)")
_VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")
_PLAYER_TIMEOUT_SEC = 12.0
_MANIFEST_TIMEOUT_SEC = 20.0

if TYPE_CHECKING:
    from services.youtube_session import YouTubeSession

FailureKind = Literal["ok", "retry", "fatal"]


@dataclass(frozen=True)
class _ClientProfile:
    name: str
    context: dict[str, Any]
    headers: dict[str, str]


# IOS → ANDROID → MWEB → TVHTML5 → WEB — each with matching UA + client context.
_CLIENT_PROFILES: tuple[_ClientProfile, ...] = (
    _ClientProfile(
        "IOS",
        {
            "clientName": "IOS",
            "clientVersion": "20.03.02",
            "deviceMake": "Apple",
            "deviceModel": "iPhone16,2",
            "osName": "iOS",
            "osVersion": "17.5.1.21F90",
            "hl": "en",
            "gl": "US",
        },
        {
            "Content-Type": "application/json",
            "User-Agent": (
                "com.google.ios.youtube/20.03.02"
                " (iPhone16,2; U; CPU iOS 17_5_1 like Mac OS X)"
            ),
        },
    ),
    _ClientProfile(
        "ANDROID",
        {
            "clientName": "ANDROID",
            "clientVersion": "19.44.38",
            "androidSdkVersion": 30,
            "hl": "en",
            "gl": "US",
        },
        {
            "Content-Type": "application/json",
            "User-Agent": (
                "com.google.android.youtube/19.44.38"
                " (Linux; U; Android 11) gzip"
            ),
        },
    ),
    _ClientProfile(
        "MWEB",
        {
            "clientName": "MWEB",
            "clientVersion": "2.20250224.01.00",
            "hl": "en",
            "gl": "US",
        },
        {
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X)"
                " AppleWebKit/605.1.15 (KHTML, like Gecko)"
                " Version/17.4 Mobile/15E148 Safari/604.1"
            ),
        },
    ),
    _ClientProfile(
        "TVHTML5",
        {
            "clientName": "TVHTML5",
            "clientVersion": "7.20250312.16.00",
            "hl": "en",
            "gl": "US",
        },
        {
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (ChromiumStylePlatform) Cobalt/Version"
            ),
        },
    ),
    _ClientProfile(
        "WEB",
        {
            "clientName": "WEB",
            "clientVersion": "2.20250224.01.00",
            "hl": "en",
            "gl": "US",
        },
        {
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                " AppleWebKit/537.36 (KHTML, like Gecko)"
                " Chrome/131.0.0.0 Safari/537.36"
            ),
        },
    ),
)


def extract_video_id(url: str) -> Optional[str]:
    """Pull an 11-char YouTube video id from a URL or bare id."""
    raw = (url or "").strip()
    if _VIDEO_ID_RE.fullmatch(raw):
        return raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower().replace("www.", "")
    if host == "youtu.be":
        vid = parsed.path.strip("/").split("/")[0]
        return vid if _VIDEO_ID_RE.fullmatch(vid) else None
    if host in ("youtube.com", "m.youtube.com", "music.youtube.com"):
        qs = parse_qs(parsed.query)
        if qs.get("v"):
            vid = qs["v"][0]
            return vid if _VIDEO_ID_RE.fullmatch(vid) else None
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in ("shorts", "live", "embed"):
            vid = parts[1]
            return vid if _VIDEO_ID_RE.fullmatch(vid) else None
    return None


def _parse_hls_variants(master_text: str, master_url: str) -> list[dict[str, Any]]:
    """Turn an HLS master playlist into yt-dlp-shaped format entries."""
    formats: list[dict[str, Any]] = []
    lines = master_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            res_m = _RESOLUTION_RE.search(line)
            bw_m = _BANDWIDTH_RE.search(line)
            height = int(res_m.group(2)) if res_m else 0
            width = int(res_m.group(1)) if res_m else 0
            bandwidth = int(bw_m.group(1)) if bw_m else 0
            if i + 1 < len(lines):
                uri = lines[i + 1].strip()
                if uri and not uri.startswith("#"):
                    formats.append({
                        "format_id": f"hls-{height or len(formats)}",
                        "url": urljoin(master_url, uri),
                        "protocol": "m3u8_native",
                        "ext": "mp4",
                        "height": height,
                        "width": width,
                        "tbr": (bandwidth / 1000.0) if bandwidth else None,
                        "vcodec": "avc1",
                        "acodec": "mp4a",
                        "http_headers": dict(_STREAM_HEADERS),
                    })
                    i += 2
                    continue
        i += 1
    formats.sort(key=lambda f: int(f.get("height") or 0), reverse=True)
    return formats


def _classify_http(status_code: int) -> FailureKind:
    if status_code in (403, 429, 500, 502, 503, 504):
        return "retry"
    if status_code == 404:
        return "fatal"
    return "ok"


def _classify_playability(status: Optional[str], reason: Optional[str]) -> FailureKind:
    if not status or status == "OK":
        return "ok"
    reason_l = (reason or "").lower()
    if status in ("ERROR", "UNPLAYABLE", "LOGIN_REQUIRED", "CONTENT_CHECK_REQUIRED"):
        return "retry"
    if status == "LIVE_STREAM_OFFLINE":
        return "fatal"
    if "private" in reason_l or "removed" in reason_l or "deleted" in reason_l:
        return "fatal"
    return "retry"


def _merge_headers(profile: _ClientProfile, session: Optional["YouTubeSession"]) -> dict[str, str]:
    headers = dict(profile.headers)
    if session and session.cookie_header:
        headers["Cookie"] = session.cookie_header
    return headers


def _enrich_client_context(client: dict[str, Any], profile_name: str) -> dict[str, Any]:
    out = dict(client)
    out.setdefault("hl", "en")
    out.setdefault("gl", "US")
    out["clientScreen"] = "WATCH"
    # ponytail: fixed BRT offset — upgrade path: read from settings or tzlocal
    out["utcOffsetMinutes"] = -180
    return out


def _profiles_for_session(session: Optional["YouTubeSession"]) -> tuple[_ClientProfile, ...]:
    """Without po_token, WEB/MWEB are slow misses — try TV before mobile web."""
    if session and session.po_token:
        return _CLIENT_PROFILES
    order = ("IOS", "ANDROID", "TVHTML5", "MWEB", "WEB")
    return tuple(sorted(_CLIENT_PROFILES, key=lambda p: order.index(p.name)))


def _player_body(
    video_id: str,
    profile: _ClientProfile,
    session: Optional["YouTubeSession"],
) -> dict[str, Any]:
    client = _enrich_client_context(dict(profile.context), profile.name)
    if session and session.visitor_data:
        client["visitorData"] = session.visitor_data
    body: dict[str, Any] = {
        "context": {"client": client},
        "videoId": video_id,
        "contentCheckOk": True,
        "racyCheckOk": True,
    }
    if session and session.po_token:
        body["serviceIntegrityDimensions"] = {"poToken": session.po_token}
    return body


def _player_request(
    video_id: str,
    profile: _ClientProfile,
    timeout: float,
    session: Optional["YouTubeSession"] = None,
) -> tuple[Optional[dict], int, FailureKind]:
    body = _player_body(video_id, profile, session)
    headers = _merge_headers(profile, session)
    try:
        resp = requests.post(
            _INNERTUBE_PLAYER_URL,
            json=body,
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.debug("InnerTube %s network error %s: %s", profile.name, video_id, exc)
        return None, 0, "retry"

    http_kind = _classify_http(resp.status_code)
    if http_kind != "ok":
        logger.debug(
            "InnerTube %s HTTP %s for %s", profile.name, resp.status_code, video_id,
        )
        return None, resp.status_code, http_kind

    try:
        data = resp.json()
    except ValueError as exc:
        logger.debug("InnerTube %s bad JSON %s: %s", profile.name, video_id, exc)
        return None, resp.status_code, "retry"

    play = data.get("playabilityStatus") or {}
    pb_kind = _classify_playability(play.get("status"), play.get("reason"))
    if pb_kind != "ok":
        logger.debug(
            "InnerTube %s playability %s (%s) for %s",
            profile.name, play.get("status"), play.get("reason"), video_id,
        )
        return data, resp.status_code, pb_kind

    streaming = data.get("streamingData") or {}
    if not streaming.get("hlsManifestUrl") and not streaming.get("adaptiveFormats"):
        logger.debug("InnerTube %s missing streamingData for %s", profile.name, video_id)
        return data, resp.status_code, "retry"

    return data, resp.status_code, "ok"


def _info_from_player_data(data: dict, video_id: str, master_text: str, master_url: str) -> Optional[dict]:
    formats = _parse_hls_variants(master_text, master_url)
    if not formats:
        return None

    details = data.get("videoDetails") or {}
    thumbs = details.get("thumbnail", {}).get("thumbnails") or []
    thumb = thumbs[-1].get("url") if thumbs else None
    length = details.get("lengthSeconds")
    try:
        duration = int(length) if length is not None else None
    except (TypeError, ValueError):
        duration = None

    views = details.get("viewCount")
    try:
        view_count = int(views) if views is not None else None
    except (TypeError, ValueError):
        view_count = None

    return {
        "id": video_id,
        "title": details.get("title"),
        "duration": duration,
        "uploader": details.get("author"),
        "channel": details.get("author"),
        "view_count": view_count,
        "thumbnail": thumb,
        "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
        "extractor": "youtube",
        "formats": formats,
        "http_headers": dict(_STREAM_HEADERS),
    }


def innertube_extract_info(
    url: str,
    timeout: Optional[float] = None,
    session: Optional["YouTubeSession"] = None,
) -> Optional[dict[str, Any]]:
    """Resolve YouTube metadata + HLS via InnerTube, trying clients in order."""
    video_id = extract_video_id(url)
    if session is None:
        from services.youtube_session import youtube_session_from_settings
        session = youtube_session_from_settings(video_id=video_id)

    player_timeout = timeout if timeout is not None else _PLAYER_TIMEOUT_SEC
    manifest_timeout = timeout if timeout is not None else _MANIFEST_TIMEOUT_SEC
    if not video_id:
        return None

    manifest_headers = dict(_STREAM_HEADERS)
    if session.cookie_header:
        manifest_headers["Cookie"] = session.cookie_header

    saw_fatal = False
    for profile in _profiles_for_session(session):
        data, _status, kind = _player_request(
            video_id, profile, player_timeout, session=session,
        )
        if kind == "fatal":
            saw_fatal = True
            continue
        if kind != "ok" or not data:
            time.sleep(0.15)
            continue

        streaming = data.get("streamingData") or {}
        master_url = streaming.get("hlsManifestUrl")
        if not master_url:
            logger.debug("InnerTube %s no hlsManifestUrl for %s", profile.name, video_id)
            continue

        try:
            manifest = requests.get(
                master_url,
                headers=manifest_headers,
                timeout=manifest_timeout,
            )
            if manifest.status_code in (403, 429):
                logger.debug(
                    "InnerTube %s manifest HTTP %s for %s",
                    profile.name, manifest.status_code, video_id,
                )
                continue
            manifest.raise_for_status()
            master_text = manifest.text
        except requests.RequestException as exc:
            logger.debug(
                "InnerTube %s manifest fetch failed %s: %s", profile.name, video_id, exc,
            )
            continue

        info = _info_from_player_data(data, video_id, master_text, master_url)
        if info:
            logger.debug("InnerTube %s succeeded for %s", profile.name, video_id)
            return info
        logger.debug("InnerTube %s empty HLS ladder for %s", profile.name, video_id)

    if saw_fatal:
        logger.debug("InnerTube all clients failed (fatal) for %s", video_id)
    else:
        logger.debug("InnerTube all clients exhausted for %s", video_id)
    return None


assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
assert extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
_sample_master = (
    "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720\n"
    "https://example.com/720.m3u8\n"
)
_parsed = _parse_hls_variants(_sample_master, "https://example.com/master.m3u8")
assert len(_parsed) == 1 and _parsed[0]["height"] == 720
assert _classify_playability("LOGIN_REQUIRED", None) == "retry"
assert _classify_playability("LIVE_STREAM_OFFLINE", None) == "fatal"
