"""Fast YouTube preview/info via InnerTube multi-client fallback (~400ms vs ~3s yt-dlp)."""
from __future__ import annotations

import contextlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from services.youtube_fingerprint import youtube_http_headers

logger = logging.getLogger(__name__)

_INNERTUBE_PLAYER_URL = (
    "https://www.youtube.com/youtubei/v1/player"
    "?key=AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
)
_STREAM_HEADERS = youtube_http_headers(
    extra={"Referer": "https://www.youtube.com/", "Origin": "https://www.youtube.com"},
)
_RESOLUTION_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)")
_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)")
_VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")
_CONNECT_TIMEOUT_SEC = 0.9
_READ_TIMEOUT_PLAYER_SEC = 2.0
_READ_TIMEOUT_MANIFEST_SEC = 8.0
_TV_TIMEOUT_SEC = 1.1
_RACE_TIMEOUT_SEC = 2.5

if TYPE_CHECKING:
    from services.youtube_session import YouTubeSession

FailureKind = Literal["ok", "retry", "fatal"]


@dataclass(frozen=True)
class _ClientProfile:
    name: str
    context: dict[str, Any]
    headers: dict[str, str]


_CLIENT_PROFILES: tuple[_ClientProfile, ...] = (
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
        "ANDROID_VR",
        {
            "clientName": "ANDROID_VR",
            "clientVersion": "1.60.19",
            "deviceMake": "Oculus",
            "deviceModel": "Quest 3",
            "osName": "Android",
            "osVersion": "12L",
            "androidSdkVersion": 32,
            "hl": "en",
            "gl": "US",
        },
        {
            "Content-Type": "application/json",
            "User-Agent": (
                "com.google.android.apps.youtube.vr.oculus/1.60.19"
                " (Linux; U; Android 12L) gzip"
            ),
        },
    ),
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

_PROFILE_BY_NAME = {p.name: p for p in _CLIENT_PROFILES}


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


def _is_sabr_stream_url(url: str) -> bool:
    """YouTube SABR (serverAbrStreamingUrl) returns vnd.yt-ump protobuf — not MP4/HLS."""
    return bool(url) and "sabr" in url.lower()


def _formats_from_streaming_progressive(streaming: dict[str, Any]) -> list[dict[str, Any]]:
    """Muxed progressive URLs from streamingData.formats (e.g. ANDROID_VR itag 18)."""
    formats: list[dict[str, Any]] = []
    for fmt in streaming.get("formats") or []:
        url = (fmt.get("url") or "").strip()
        if not url or _is_sabr_stream_url(url):
            continue
        mime = (fmt.get("mimeType") or "").lower()
        if "video" not in mime:
            continue
        if not any(c in mime for c in ("mp4a", "opus", "audio")):
            continue
        itag = fmt.get("itag")
        height = int(fmt.get("height") or 0)
        width = int(fmt.get("width") or 0)
        bitrate = fmt.get("bitrate") or fmt.get("averageBitrate") or 0
        formats.append({
            "format_id": f"progressive-{itag or len(formats)}",
            "url": url,
            "protocol": "https",
            "ext": "mp4",
            "height": height,
            "width": width,
            "tbr": (int(bitrate) / 1000.0) if bitrate else None,
            "vcodec": "avc1",
            "acodec": "mp4a",
            "http_headers": dict(_STREAM_HEADERS),
        })
    formats.sort(key=lambda f: int(f.get("height") or 0), reverse=True)
    return formats


def _codecs_from_mime(mime: str) -> tuple[str, str]:
    """Infer vcodec/acodec from YouTube mimeType string."""
    m = (mime or "").lower()
    vcodec = "none"
    acodec = "none"
    if "video" in m:
        vcodec = "avc1"
    if "mp4a" in m or "opus" in m:
        acodec = "mp4a" if "mp4a" in m else "opus"
    elif "audio" in m and "video" not in m:
        acodec = "mp4a"
    return vcodec, acodec


def _format_from_raw_adaptive(raw: dict, *, audio_only: bool = False) -> dict[str, Any]:
    url = (raw.get("url") or "").strip()
    mime = raw.get("mimeType") or ""
    vcodec, acodec = _codecs_from_mime(mime)
    if audio_only:
        vcodec = "none"
    itag = raw.get("itag")
    height = int(raw.get("height") or 0)
    width = int(raw.get("width") or 0)
    bitrate = raw.get("bitrate") or raw.get("averageBitrate") or 0
    fid = f"audio-{itag}" if audio_only else f"adaptive-{itag or len(url)}"
    return {
        "format_id": fid,
        "url": url,
        "protocol": "https",
        "ext": "m4a" if audio_only else "mp4",
        "height": height,
        "width": width,
        "tbr": (int(bitrate) / 1000.0) if bitrate else None,
        "vcodec": vcodec,
        "acodec": acodec,
        "http_headers": dict(_STREAM_HEADERS),
    }


def _is_auto_dubbed_audio(raw: dict) -> bool:
    """YouTube serves English auto-dub tracks when client locale is en — skip them."""
    if raw.get("isAutoDubbed") is True:
        return True
    track = raw.get("audioTrack")
    if isinstance(track, dict):
        if track.get("isAutoDubbed") is True:
            return True
        name = (track.get("displayName") or track.get("id") or "").lower()
        if "auto" in name and "dub" in name:
            return True
    return False


def _pick_best_audio_format(streaming: dict[str, Any]) -> Optional[dict[str, Any]]:
    best_orig: Optional[dict[str, Any]] = None
    best_orig_br = -1
    best_any: Optional[dict[str, Any]] = None
    best_any_br = -1
    for raw in streaming.get("adaptiveFormats") or []:
        mime = (raw.get("mimeType") or "").lower()
        if "audio" not in mime or "video" in mime:
            continue
        url = (raw.get("url") or "").strip()
        if not url or _is_sabr_stream_url(url):
            continue
        br = int(raw.get("bitrate") or raw.get("averageBitrate") or 0)
        fmt = _format_from_raw_adaptive(raw, audio_only=True)
        if _is_auto_dubbed_audio(raw):
            if br > best_any_br:
                best_any_br = br
                best_any = fmt
        elif br > best_orig_br:
            best_orig_br = br
            best_orig = fmt
    return best_orig or best_any


def _formats_from_player_streaming(data: dict) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]], Optional[str]]:
    """Extract all usable formats + best audio + hls master URL from player response."""
    streaming = data.get("streamingData") or {}
    out: list[dict[str, Any]] = []
    out.extend(_formats_from_streaming_progressive(streaming))
    out.extend(_streaming_url_formats(streaming))
    adaptive = _formats_from_adaptive(streaming)
    out.extend(adaptive)
    audio = _pick_best_audio_format(streaming)
    hls = (streaming.get("hlsManifestUrl") or "").strip() or None
    return out, audio, hls


def _dedupe_youtube_formats(formats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per height — prefer muxed/HLS over video-only DASH."""
    by_height: dict[int, dict[str, Any]] = {}
    extras: list[dict[str, Any]] = []

    def _score(fmt: dict) -> tuple[int, int, int]:
        muxed = fmt.get("acodec") not in ("none", None)
        proto = (fmt.get("protocol") or "").lower()
        hls = proto in ("m3u8", "m3u8_native", "m3u8_ffmpeg") or fmt.get("format_id") == "hls-master"
        url = (fmt.get("url") or "").lower()
        webm = "mime=video%2fwebm" in url or "mime=video/webm" in url
        return (
            2 if hls else 1 if muxed else 0,
            0 if webm else 1,
            int(fmt.get("tbr") or 0),
        )

    for fmt in formats:
        url = (fmt.get("url") or "").strip()
        if not url or _is_sabr_stream_url(url):
            continue
        height = int(fmt.get("height") or 0)
        if height <= 0:
            if (fmt.get("format_id") or "") == "hls-master":
                extras.append(fmt)
            continue
        prev = by_height.get(height)
        if prev is None or _score(fmt) > _score(prev):
            by_height[height] = fmt
    merged = sorted(by_height.values(), key=lambda f: int(f.get("height") or 0), reverse=True)
    return extras + merged


def _formats_from_adaptive(streaming: dict[str, Any]) -> list[dict[str, Any]]:
    """Build yt-dlp-shaped formats from streamingData.adaptiveFormats (DASH/mp4)."""
    formats: list[dict[str, Any]] = []
    for fmt in streaming.get("adaptiveFormats") or []:
        url = fmt.get("url") or ""
        if not url:
            continue
        mime = (fmt.get("mimeType") or "").lower()
        if "video" not in mime and "mpegurl" not in mime:
            continue
        height = int(fmt.get("height") or 0)
        width = int(fmt.get("width") or 0)
        vcodec, acodec = _codecs_from_mime(fmt.get("mimeType") or "")
        if "mpegurl" in mime or ".m3u8" in url:
            protocol = "m3u8_native"
            ext = "mp4"
        else:
            protocol = "https"
            ext = "mp4"
        bitrate = fmt.get("bitrate") or fmt.get("averageBitrate") or 0
        formats.append({
            "format_id": f"adaptive-{fmt.get('itag') or len(formats)}",
            "url": url,
            "protocol": protocol,
            "ext": ext,
            "height": height,
            "width": width,
            "tbr": (int(bitrate) / 1000.0) if bitrate else None,
            "vcodec": vcodec,
            "acodec": acodec,
            "http_headers": dict(_STREAM_HEADERS),
        })
    formats.sort(key=lambda f: int(f.get("height") or 0), reverse=True)
    return formats


def _format_from_streaming_url(url: str, format_id: str, *, height: int = 0) -> dict[str, Any]:
    """Single HLS master or muxed ABR URL from streamingData."""
    lower = url.lower()
    if ".m3u8" in lower or "/api/manifest/hls" in lower or "mpegurl" in lower:
        protocol = "m3u8_native"
    else:
        protocol = "https"
    return {
        "format_id": format_id,
        "url": url,
        "protocol": protocol,
        "ext": "mp4",
        "height": height,
        "width": 0,
        "vcodec": "avc1",
        "acodec": "mp4a",
        "http_headers": dict(_STREAM_HEADERS),
    }


def _streaming_url_formats(streaming: dict[str, Any]) -> list[dict[str, Any]]:
    """HLS master only — serverAbrStreamingUrl is SABR (not proxyable as MP4)."""
    out: list[dict[str, Any]] = []
    hls = (streaming.get("hlsManifestUrl") or "").strip()
    if hls:
        out.append(_format_from_streaming_url(hls, "hls-master"))
    return out


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
    if status == "LOGIN_REQUIRED" and ("age" in reason_l or "confirm your age" in reason_l):
        return "fatal"
    if status in ("ERROR", "UNPLAYABLE", "LOGIN_REQUIRED", "CONTENT_CHECK_REQUIRED"):
        return "retry"
    if status == "LIVE_STREAM_OFFLINE":
        return "fatal"
    if "private" in reason_l or "removed" in reason_l or "deleted" in reason_l:
        return "fatal"
    if status == "UNPLAYABLE" and any(
        k in reason_l for k in ("token", "visitor", "integrity", "bot")
    ):
        return "retry"
    return "retry"


def _merge_headers(profile: _ClientProfile, session: Optional["YouTubeSession"]) -> dict[str, str]:
    headers = dict(_STREAM_HEADERS)
    headers.update(profile.headers)
    headers["X-Youtube-Client-Name"] = str(profile.context.get("clientName", ""))
    headers["X-Youtube-Client-Version"] = str(profile.context.get("clientVersion", ""))
    if session and session.cookie_header:
        headers["Cookie"] = session.cookie_header
    return headers


def _enrich_client_context(client: dict[str, Any], profile_name: str) -> dict[str, Any]:
    out = dict(client)
    # ponytail: omit hl/gl — forced en/US triggers translated titles and auto-dubbed audio
    out.pop("hl", None)
    out.pop("gl", None)
    out["clientScreen"] = "WATCH"
    out["utcOffsetMinutes"] = -180
    return out


def _profiles_for_session(session: Optional["YouTubeSession"]) -> tuple[_ClientProfile, ...]:
    """TV → MWEB → ANDROID → IOS → WEB; po_token unlocks full ladder."""
    order = ("TVHTML5", "MWEB", "ANDROID", "IOS", "WEB")
    return tuple(_PROFILE_BY_NAME[n] for n in order if n in _PROFILE_BY_NAME)


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
        "playbackContext": {
            "contentPlaybackContext": {
                "html5Preference": "HTML5_PREF_WANTS",
                "signatureTimestamp": 0,
            },
        },
    }
    if session and session.po_token:
        body["serviceIntegrityDimensions"] = {"poToken": session.po_token}
    return body


def _http_for(session: Optional["YouTubeSession"]) -> requests.Session:
    from services.youtube_session import http_session_for

    return http_session_for(session)


def _player_request(
    video_id: str,
    profile: _ClientProfile,
    read_timeout: float,
    session: Optional["YouTubeSession"] = None,
    http: Optional[requests.Session] = None,
) -> tuple[Optional[dict], int, FailureKind]:
    body = _player_body(video_id, profile, session)
    headers = _merge_headers(profile, session)
    client = http or _http_for(session)
    timeout = (_CONNECT_TIMEOUT_SEC, read_timeout)
    try:
        resp = client.post(
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
    if (
        not streaming.get("hlsManifestUrl")
        and not streaming.get("adaptiveFormats")
        and not streaming.get("formats")
    ):
        logger.debug("InnerTube %s missing streamingData for %s", profile.name, video_id)
        return data, resp.status_code, "retry"

    return data, resp.status_code, "ok"


def _created_at_from_player_data(data: dict) -> Optional[str]:
    """Publish/upload date from player microformat (not in videoDetails)."""
    from datetime import datetime, timezone

    micro = (data.get("microformat") or {}).get("playerMicroformatRenderer") or {}
    for key in ("publishDate", "uploadDate"):
        raw = micro.get(key)
        if not raw:
            continue
        s = str(raw).strip()
        if not s:
            continue
        if re.match(r"^\d{8}$", s):
            try:
                return datetime.strptime(s, "%Y%m%d").replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                continue
        if re.match(r"^\d{4}-\d{2}-\d{2}", s):
            if "T" in s:
                try:
                    norm = s.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(norm)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.astimezone(timezone.utc).isoformat()
                except ValueError:
                    pass
            return f"{s}T00:00:00+00:00"
    return None


def _info_from_player_data(
    data: dict,
    video_id: str,
    master_text: Optional[str],
    master_url: Optional[str],
    adaptive_formats: Optional[list[dict[str, Any]]] = None,
) -> Optional[dict]:
    formats: list[dict[str, Any]] = []
    if master_text and master_url:
        formats = _parse_hls_variants(master_text, master_url)
    elif master_url and not master_text:
        formats = [_format_from_streaming_url(master_url, "hls-master")]
    if not formats and adaptive_formats:
        formats = _dedupe_youtube_formats(adaptive_formats)
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

    created_at = _created_at_from_player_data(data)
    upload_date = None
    if created_at and len(created_at) >= 10 and created_at[4] == "-":
        upload_date = created_at[:10].replace("-", "")

    return {
        "id": video_id,
        "title": details.get("title"),
        "duration": duration,
        "uploader": details.get("author"),
        "channel": details.get("author"),
        "view_count": view_count,
        "created_at": created_at,
        "upload_date": upload_date,
        "thumbnail": thumb,
        "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
        "extractor": "youtube",
        "formats": formats,
        "http_headers": dict(_STREAM_HEADERS),
    }


def _resolve_profile(
    video_id: str,
    profile: _ClientProfile,
    session: Optional["YouTubeSession"],
    read_timeout: float,
) -> Optional[dict[str, Any]]:
    http = _http_for(session)
    data, _status, kind = _player_request(
        video_id, profile, read_timeout, session=session, http=http,
    )
    if kind == "fatal" or kind != "ok" or not data:
        return None

    streaming = data.get("streamingData") or {}
    master_url = streaming.get("hlsManifestUrl")
    progressive = _formats_from_streaming_progressive(streaming)
    adaptive = _formats_from_adaptive(streaming)
    url_formats = _streaming_url_formats(streaming)

    if master_url:
        manifest_headers = _merge_headers(profile, session)
        try:
            manifest = http.get(
                master_url,
                headers=manifest_headers,
                timeout=(_CONNECT_TIMEOUT_SEC, _READ_TIMEOUT_MANIFEST_SEC),
            )
            if manifest.status_code in (403, 429):
                logger.debug(
                    "InnerTube %s manifest HTTP %s for %s",
                    profile.name, manifest.status_code, video_id,
                )
            else:
                manifest.raise_for_status()
                info = _info_from_player_data(
                    data, video_id, manifest.text, master_url, adaptive_formats=adaptive,
                )
                if info:
                    logger.debug("InnerTube %s HLS succeeded for %s", profile.name, video_id)
                    return info
        except requests.RequestException as exc:
            logger.debug(
                "InnerTube %s manifest fetch failed %s: %s", profile.name, video_id, exc,
            )
        # Manifest text fetch failed — still expose master URL for HLS preview (muxed + seekable).
        info = _info_from_player_data(
            data,
            video_id,
            None,
            master_url,
            adaptive_formats=url_formats or adaptive,
        )
        if info:
            logger.debug("InnerTube %s HLS master URL (unparsed) for %s", profile.name, video_id)
            return info

    if progressive:
        info = _info_from_player_data(
            data, video_id, None, None, adaptive_formats=progressive,
        )
        if info:
            logger.debug("InnerTube %s progressive formats for %s", profile.name, video_id)
            return info

    if url_formats:
        info = _info_from_player_data(
            data, video_id, None, None, adaptive_formats=url_formats + adaptive,
        )
        if info:
            logger.debug("InnerTube %s streaming URL formats for %s", profile.name, video_id)
            return info

    if adaptive:
        info = _info_from_player_data(data, video_id, None, None, adaptive_formats=adaptive)
        if info:
            logger.debug("InnerTube %s adaptiveFormats succeeded for %s", profile.name, video_id)
            return info

    return None


def _race_profiles(
    profiles: tuple[_ClientProfile, ...],
    video_id: str,
    session: Optional["YouTubeSession"],
    read_timeout: float,
    wall_timeout: float,
) -> Optional[dict[str, Any]]:
    if not profiles:
        return None
    if len(profiles) == 1:
        return _resolve_profile(video_id, profiles[0], session, read_timeout)

    winner: Optional[dict[str, Any]] = None
    pool = ThreadPoolExecutor(max_workers=len(profiles), thread_name_prefix="innertube")
    try:
        futures = {
            pool.submit(_resolve_profile, video_id, profile, session, read_timeout): profile
            for profile in profiles
        }
        try:
            for fut in as_completed(futures, timeout=wall_timeout):
                try:
                    result = fut.result()
                except Exception as exc:
                    profile = futures[fut]
                    logger.debug("InnerTube race %s error %s: %s", profile.name, video_id, exc)
                    continue
                if result:
                    winner = result
                    break
        except TimeoutError:
            logger.debug("InnerTube race timeout %s after %.1fs", video_id, wall_timeout)
    finally:
        with contextlib.suppress(Exception):
            pool.shutdown(wait=False, cancel_futures=True)
    return winner


def _is_bot_gate(data: Optional[dict]) -> bool:
    """YouTube LOGIN_REQUIRED bot check — stop hammering all clients."""
    if not data:
        return False
    play = data.get("playabilityStatus") or {}
    if play.get("status") != "LOGIN_REQUIRED":
        return False
    reason = (play.get("reason") or "").lower()
    return "bot" in reason or "confirm you're not" in reason or "confirm you" in reason


def _collect_merged_innertube_info(
    video_id: str,
    session: Optional["YouTubeSession"],
    read_timeout: float,
) -> Optional[dict[str, Any]]:
    """Merge formats from all InnerTube clients — ANDROID_VR muxed + IOS heights."""
    http = _http_for(session)
    profile_order = ("IOS", "ANDROID", "ANDROID_VR", "TVHTML5", "MWEB", "WEB")
    all_formats: list[dict[str, Any]] = []
    audio_fmt: Optional[dict[str, Any]] = None
    hls_url: Optional[str] = None
    meta_data: Optional[dict] = None

    bot_hits = 0
    for name in profile_order:
        profile = _PROFILE_BY_NAME.get(name)
        if not profile:
            continue
        timeout = min(read_timeout, _TV_TIMEOUT_SEC) if name == "TVHTML5" else read_timeout
        data, _status, kind = _player_request(
            video_id, profile, timeout, session=session, http=http,
        )
        if kind != "ok" or not data:
            if _is_bot_gate(data):
                bot_hits += 1
                if bot_hits >= 2:
                    break
            continue
        if meta_data is None:
            meta_data = data
        partial, audio, hls = _formats_from_player_streaming(data)
        all_formats.extend(partial)
        if audio and audio_fmt is None:
            audio_fmt = audio
        if hls and hls_url is None:
            hls_url = hls

    if not meta_data:
        from services.youtube_diag import log_extract_fail
        log_extract_fail(video_id, "innertube_no_playable_client", session)
        return None

    merged = _dedupe_youtube_formats(all_formats)
    video_heights = [f for f in merged if int(f.get("height") or 0) > 0]

    # Prefer HLS when any client returned a manifest — avoids DASH mux 403s in preview.
    if hls_url:
        headers = dict(_STREAM_HEADERS)
        if session and session.cookie_header:
            headers["Cookie"] = session.cookie_header
        try:
            manifest = http.get(
                hls_url,
                headers=headers,
                timeout=(_CONNECT_TIMEOUT_SEC, _READ_TIMEOUT_MANIFEST_SEC),
            )
            if manifest.status_code == 200:
                info = _info_from_player_data(
                    meta_data, video_id, manifest.text, hls_url,
                    adaptive_formats=merged,
                )
                if info:
                    if audio_fmt:
                        info["_preview_audio_format"] = audio_fmt
                    return info
        except requests.RequestException as exc:
            logger.debug("merged HLS manifest fetch failed %s: %s", video_id, exc)
        info = _info_from_player_data(
            meta_data, video_id, None, hls_url, adaptive_formats=merged,
        )
        if info:
            if audio_fmt:
                info["_preview_audio_format"] = audio_fmt
            return info

    # Multiple DASH heights — progressive proxy + mux (no HLS manifest worked).
    if len(video_heights) >= 2:
        info = _info_from_player_data(
            meta_data, video_id, None, None, adaptive_formats=merged,
        )
        if info:
            if audio_fmt:
                info["_preview_audio_format"] = audio_fmt
            return info

    if not merged:
        return None
    info = _info_from_player_data(meta_data, video_id, None, None, adaptive_formats=merged)
    if info and audio_fmt:
        info["_preview_audio_format"] = audio_fmt
    return info


def innertube_video_row_metadata(
    video_id: str,
    session: Optional["YouTubeSession"] = None,
    read_timeout: float = 2.0,
) -> Optional[dict[str, Any]]:
    """Lightweight player call for channel list rows (date/views/duration only)."""
    if session is None:
        from services.youtube_session import youtube_session_from_settings
        session = youtube_session_from_settings(video_id=video_id)
    out: dict[str, Any] = {}
    for name in ("WEB", "IOS", "ANDROID"):
        profile = _PROFILE_BY_NAME.get(name)
        if not profile:
            continue
        data, _status, kind = _player_request(
            video_id, profile, read_timeout, session=session,
        )
        if kind == "fatal" or not data:
            continue
        details = data.get("videoDetails") or {}
        if not out.get("created_at"):
            created = _created_at_from_player_data(data)
            if created:
                out["created_at"] = created
        if out.get("views") is None:
            views = details.get("viewCount")
            try:
                if views is not None:
                    out["views"] = int(views)
            except (TypeError, ValueError):
                pass
        if not out.get("duration"):
            length = details.get("lengthSeconds")
            try:
                if length is not None:
                    out["duration"] = int(length)
            except (TypeError, ValueError):
                pass
        if out.get("created_at") and out.get("views") is not None and out.get("duration"):
            break
    if not out:
        return None
    return out


def _ensure_info_created_at(
    info: Optional[dict[str, Any]],
    video_id: str,
    session: Optional["YouTubeSession"],
) -> None:
    """IOS race winners often lack publishDate — backfill from WEB microformat."""
    if not info or info.get("created_at") or info.get("upload_date"):
        return
    meta = innertube_video_row_metadata(video_id, session=session, read_timeout=2.5)
    if not meta or not meta.get("created_at"):
        return
    info["created_at"] = meta["created_at"]
    ca = str(meta["created_at"])
    if len(ca) >= 10 and ca[4] == "-":
        info["upload_date"] = ca[:10].replace("-", "")


def innertube_extract_info(
    url: str,
    timeout: Optional[float] = None,
    session: Optional["YouTubeSession"] = None,
    *,
    allow_session_refresh: bool = True,
) -> Optional[dict[str, Any]]:
    """Resolve YouTube metadata + streams via merged InnerTube clients."""
    video_id = extract_video_id(url)
    if session is None:
        from services.youtube_session import youtube_session_from_settings
        session = youtube_session_from_settings(video_id=video_id)

    if not video_id:
        return None

    read_timeout = timeout if timeout is not None else _READ_TIMEOUT_PLAYER_SEC
    profiles = _profiles_for_session(session)
    info = _race_profiles(profiles, video_id, session, read_timeout, _RACE_TIMEOUT_SEC)
    if info:
        _ensure_info_created_at(info, video_id, session)
        from services.youtube_diag import log_extract_ok
        log_extract_ok(video_id, "innertube_race", info, session)
        return info

    info = _collect_merged_innertube_info(video_id, session, read_timeout)
    if info:
        _ensure_info_created_at(info, video_id, session)
        from services.youtube_diag import log_extract_ok
        log_extract_ok(video_id, "innertube", info, session)
        return info

    # ponytail: outer cached_extract_info retry already re-bootstraps — avoid double hammer
    if allow_session_refresh and session and getattr(session, "anonymous", True):
        from services.youtube_session import invalidate_anonymous_session, youtube_session_from_settings

        invalidate_anonymous_session()
        fresh = youtube_session_from_settings(video_id=video_id)
        info = _race_profiles(
            _profiles_for_session(fresh), video_id, fresh, read_timeout, _RACE_TIMEOUT_SEC,
        )
        if info:
            _ensure_info_created_at(info, video_id, fresh)
            from services.youtube_diag import log_extract_ok
            log_extract_ok(video_id, "innertube_race_fresh", info, fresh)
            return info
        info = _collect_merged_innertube_info(video_id, fresh, read_timeout)
        if info:
            _ensure_info_created_at(info, video_id, fresh)
            from services.youtube_diag import log_extract_ok
            log_extract_ok(video_id, "innertube_fresh", info, fresh)
            return info

    try:
        from deps import settings_mgr
        auto_auth = getattr(settings_mgr.get(), "youtube_auto_auth", True)
    except Exception:
        auto_auth = True
    if auto_auth and session and not getattr(session, "anonymous", True):
        from services.youtube_auth import strengthen_youtube_session

        strong = strengthen_youtube_session(
            session, video_id, auto_auth=auto_auth, fetch_pot=False,
        )
        if strong is not session:
            info = _collect_merged_innertube_info(video_id, strong, read_timeout)
            if info:
                from services.youtube_diag import log_extract_ok
                log_extract_ok(video_id, "innertube_strengthened", info, strong)
                return info

    from services.youtube_diag import log_extract_fail
    log_extract_fail(video_id, "innertube_exhausted", session)
    logger.debug("InnerTube all clients exhausted for %s", video_id)
    return None


assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
assert extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
assert "hl" not in _enrich_client_context({"hl": "en", "gl": "US"}, "WEB")
assert _is_auto_dubbed_audio({"audioTrack": {"isAutoDubbed": True}}) is True
assert not _is_auto_dubbed_audio({"audioTrack": {"displayName": "Portuguese"}})
_sample_master = (
    "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720\n"
    "https://example.com/720.m3u8\n"
)
_parsed = _parse_hls_variants(_sample_master, "https://example.com/master.m3u8")
assert len(_parsed) == 1 and _parsed[0]["height"] == 720
_prog = _formats_from_streaming_progressive({
    "formats": [{
        "url": "https://cdn.example/v.mp4",
        "mimeType": 'video/mp4; codecs="avc1.42001E, mp4a.40.2"',
        "itag": 18,
        "height": 360,
    }],
})
assert len(_prog) == 1 and _prog[0]["format_id"] == "progressive-18"
assert _is_sabr_stream_url("https://x.googlevideo.com/videoplayback?sabr=1")
assert _classify_playability("LOGIN_REQUIRED", None) == "retry"
assert _classify_playability("LOGIN_REQUIRED", "Confirm your age") == "fatal"
assert _classify_playability("LIVE_STREAM_OFFLINE", None) == "fatal"
assert _profiles_for_session(None)[0].name == "TVHTML5"
