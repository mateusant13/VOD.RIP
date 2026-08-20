"""Fast Kick.com metadata via public JSON API + curl_cffi (no browser).

Used for channel lists, VOD browse rows, preview stream resolution, and
Kick VOD downloads (curl_cffi + HLS).

Endpoints used:
  GET /api/v2/channels/{slug}/videos  — channel VOD list (~1-2s)
  GET /api/v1/video/{uuid}              — single VOD metadata + m3u8 (~0.5s)
  GET /api/v2/channels/{slug}           — channel metadata (~1s)
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from services import kick_gate
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

logger = logging.getLogger(__name__)

# Depth ceilings for the channel-list endpoints. Both v2 list endpoints are
# single-shot (one response, no cursor): the service truncates the parsed
# list at the REQUESTED depth, so show-more grows the index by re-asking with
# a deeper depth. 500 is the safety bound — a channel whose full list exceeds
# it is served up to the bound, then has_more goes false instead of looping
# forever. ponytail: the v2 list endpoints have no documented node cap; if a
# channel genuinely exceeds 500 rows the upgrade path is cursor paging on the
# channel's own API.
KICK_VIDEOS_CEILING = 500
KICK_CLIPS_CEILING = 500

_IMPERSONATE = "chrome"
_BASE = "https://kick.com"


def _headers(referer: str) -> Dict[str, str]:
    return {"referer": referer, "origin": _BASE}


def _bridge_cookie_jar() -> Optional[Dict[str, str]]:
    """Bridge auth cookies (auth_token/g_session), or None — additive only.

    None means "send no cookies", so requests stay byte-identical when the
    bridge is disabled or the store has no Kick cookies (regression bar).
    Merges with whatever curl_cffi sends for the impersonated browser —
    never clobbers existing headers.
    """
    try:
        from services.cookie_bridge import cookie_dict

        return cookie_dict("kick")
    except Exception:
        return None


# Exponential backoff on transient failures: start 1s, double, cap at 30s
# (same constants as archive_twitch's GQL 429 backoff).
_BACKOFF_START_SEC = 1.0
_BACKOFF_MAX_SEC = 30.0
_BACKOFF_MAX_ATTEMPTS = 8


class KickGateError(RuntimeError):
    """Kick request rejected while the Cloudflare/rate-limit gate is active."""


class KickRateLimitError(RuntimeError):
    """429 retries exhausted — Kick rate-limited this IP/session."""


def _get_json(path: str, referer: str, *, timeout: float = 15.0) -> Any:
    from curl_cffi import requests

    url = f"{_BASE}{path}"
    backoff = _BACKOFF_START_SEC
    for attempt in range(1, _BACKOFF_MAX_ATTEMPTS + 1):
        if kick_gate.kick_gate_active():
            # Fail fast while frozen — no point hammering Cloudflare. The
            # archive download path requeues (never fails) gated jobs.
            raise KickGateError(
                f"Kick requests frozen for {kick_gate.gate_remaining_sec():.0f}s "
                "(Cloudflare/rate-limit cooldown)"
            )
        try:
            resp = requests.get(
                url,
                impersonate=_IMPERSONATE,
                headers=_headers(referer),
                cookies=_bridge_cookie_jar(),
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 — curl_cffi transport errors (timeout/DNS/conn) are transient
            if attempt >= _BACKOFF_MAX_ATTEMPTS or not kick_gate.classify_transient_kick_error(exc):
                raise
            time.sleep(backoff)
            backoff = min(backoff * 2.0, _BACKOFF_MAX_SEC)
            continue
        if resp.status_code == 404:
            raise ValueError(f"Kick channel not found: {referer}")
        if resp.status_code == 403:
            # Cloudflare classification (bot block): record the event and
            # arm the gate cooldown; consecutive events escalate to a long
            # freeze (kick_gate).
            kick_gate.note_kick_gate_event(f"403 on {path}")
            raise KickGateError(f"Kick request blocked (Cloudflare/403): {path}")
        if resp.status_code == 429:
            if attempt >= _BACKOFF_MAX_ATTEMPTS:
                kick_gate.note_kick_gate_event(f"429 rate-limited on {path}")
                raise KickRateLimitError(
                    f"Kick rate-limited (429) after {_BACKOFF_MAX_ATTEMPTS} attempts: {path}"
                )
            time.sleep(backoff)
            backoff = min(backoff * 2.0, _BACKOFF_MAX_SEC)
            continue
        if resp.status_code >= 500:
            if attempt >= _BACKOFF_MAX_ATTEMPTS:
                resp.raise_for_status()  # terminal — curl_cffi HTTPError
            time.sleep(backoff)
            backoff = min(backoff * 2.0, _BACKOFF_MAX_SEC)
            continue
        # Any other 4xx is terminal (raise_for_status keeps prior semantics).
        if resp.status_code >= 400:
            resp.raise_for_status()
        kick_gate.note_kick_success()
        return resp.json()
    raise RuntimeError("unreachable")  # pragma: no cover — loop always returns or raises


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
    # Depth-truncate at the requested depth (show-more pages ask deeper), not
    # the old fixed 10 — the UI "shows 10 and pages client-side" cap that
    # made kick clips unpaginatable.
    return parsed[: max(1, min(int(limit), KICK_CLIPS_CEILING))]


def list_channel_clips_sync(url: str, limit: int = 10, *, sort: str = "date") -> list[dict]:
    slug = extract_slug(url)
    if not slug:
        raise ValueError(f"Not a Kick channel URL: {url}")
    clips = list_channel_clips_api(slug, limit, verify=False)
    if sort == "views":
        clips = sorted(clips, key=lambda c: int(getattr(c, "views", 0) or 0), reverse=True)
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
    limit = max(1, min(int(limit), KICK_VIDEOS_CEILING))
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
        is_live=bool(ls and (ls.get("id") or ls.get("channel_id"))),
        live_title=(ls.get("session_title") if ls else None),
        viewers=(ls.get("viewer_count") if ls else None),
        playback_url=data.get("playback_url"),
        # Kick's channel payload carries the broadcaster language in the
        # user block (top-level fallback kept for older payload shapes).
        language=user.get("language") or data.get("language") or None,
    )


def get_channel_language_sync(slug: str) -> Optional[str]:
    """Language clue for a Kick channel slug (cached ~1h, best-effort).

    Part of the WS-3 platform-clue path: called at channel-list refresh
    time, never raises — a blocked/failed payload yields None (no clue)."""
    slug = (slug or "").strip().lower()
    if not slug:
        return None
    from services.channel_cache import get_cached, make_channel_cache_key, set_cached

    key = make_channel_cache_key("kick-lang", slug)
    cached = get_cached(key, ttl=3600.0)
    if cached is not None:
        return cached or None
    try:
        data = _get_json(f"/api/v2/channels/{slug}", f"{_BASE}/{slug}")
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        lang = user.get("language") or data.get("language") or None
    except Exception:
        logger.debug("Kick channel language fetch failed for %s", slug, exc_info=True)
        lang = None
    set_cached(key, lang or "")
    return lang or None


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
        "language": ch.language,
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
    audio_only: bool = False,
    **_,
) -> str:
    """Download a Kick VOD clip via the fast JSON API + HLS segments (no browser)."""
    from services.ytdlp_service import (
        _parse_prefer_height,
        _resolve_ffmpeg_exe,
        _verify_output_file,
        download_hls_media_clip,
    )

    if video_encoder is None and settings_mgr is not None:
        video_encoder = settings_mgr.get().video_encoder
    mp4_faststart = bool(settings_mgr.get().mp4_faststart) if settings_mgr else False

    info = resolve_kick_stream_api(url)
    if not info.m3u8_url:
        raise RuntimeError("Kick API returned no HLS source for this Kick content")
    from services.ytdlp_ffmpeg import _normalize_crop_range
    crop = _normalize_crop_range(crop_start, crop_end)
    if crop is not None:
        start_sec, end_sec = crop
    else:
        # Full VOD download — use known duration or large fallback.
        # download_hls_media_clip caps end_sec at actual segment length,
        # so a large fallback is safe (ffmpeg stops at EOF anyway).
        start_sec = 0.0
        end_sec = info.duration if info.duration and info.duration > 0 else 1e18
    page_url = info.url or url
    headers = {"referer": page_url, "origin": _BASE}
    clip_target = output_path
    temp_video: Optional[str] = None
    if audio_only:
        import tempfile

        temp_video = tempfile.mktemp(suffix=".mp4", prefix="kick_audio_")
        clip_target = temp_video
    try:
        download_hls_media_clip(
            info.m3u8_url,
            start_sec,
            end_sec,
            clip_target,
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
        if audio_only and temp_video:
            from services.ytdlp_hls import _extract_hls_audio

            _extract_hls_audio(temp_video, output_path)
        _verify_output_file(output_path)
    finally:
        if temp_video and os.path.isfile(temp_video):
            try:
                os.remove(temp_video)
            except OSError:
                pass
    return output_path
