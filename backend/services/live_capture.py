"""Live stream detection and DVR capture for Kick, Twitch, YouTube."""

import json
import logging
import os
import random
import re
import subprocess as sp
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlencode

import requests

from services.kick_api_service import get_channel_api as _get_kick_channel
from services.os_services import _NO_WINDOW, _kill_pid, register_child_pid, unregister_child_pid
from services.ytdlp_ffmpeg import _resolve_ffmpeg_exe

logger = logging.getLogger(__name__)

_YT_LIVE_PAGE_RE = re.compile(
    rb'"videoId":\s*"([a-zA-Z0-9_-]{11})"',
)

# ---------------------------------------------------------------------------
# Kick
# ---------------------------------------------------------------------------


def kick_live_info(slug: str) -> Optional[dict]:
    """Return live-stream metadata dict or None if channel is offline.

    The returned dict uses keys: url, headers, title, viewers, platform.
    """
    try:
        ch = _get_kick_channel(f"https://kick.com/{slug}")
    except Exception as exc:
        logger.debug("kick_live_info(%r) failed: %s", slug, exc)
        return None

    if not ch.is_live or not ch.playback_url:
        return None

    return {
        "url": ch.playback_url,
        "headers": {
            "Referer": "https://kick.com/",
            "Origin": "https://kick.com/",
        },
        "title": ch.live_title or slug,
        "viewers": ch.viewers or 0,
        "platform": "Kick",
    }


# ---------------------------------------------------------------------------
# Twitch
# ---------------------------------------------------------------------------
#
# vaft-style stream rotation (pixeltris/TwitchAdSolutions): Twitch decides per
# ``player_type`` whether to stitch ads into the media playlist, so rotating
# the usher master across player types — each with a FRESH PlaybackAccessToken
# — yields an ad-free stream. ``probe_twitch_live_master`` fetches each type's
# media playlist and returns the first one without 'stitched' segments; when
# every type carries ads it falls back to the embed master (existing
# behavior) and the frontend pLoader strips the segments locally. No proxies,
# no paid services — direct GQL + usher + CDN requests only.

# vaft BackupPlayerTypes order: embed=Source, popout=Source, autoplay=360p.
_TWITCH_PLAYER_TYPES: tuple = ("embed", "popout", "autoplay")
# vaft FallbackPlayerType — used when no player type is ad-free.
_TWITCH_FALLBACK_PLAYER_TYPE = "embed"
# Per-channel probe cache so the frontend's 3s live-polling loop stays O(1)
# (a cold probe costs 1 GQL + 1 usher + 1 media fetch per player type).
_TWITCH_MASTER_TTL_SEC = 60.0
_TWITCH_MASTER_CACHE: dict = {}
_TWITCH_MASTER_LOCK = threading.Lock()
_TWITCH_HEADERS = {"Referer": "https://www.twitch.tv/", "Origin": "https://www.twitch.tv/"}


def _twitch_gql_live_token(login: str, player_type: str) -> Optional[tuple]:
    """Fresh GQL PlaybackAccessToken (sig, token) for one player type."""
    from services.twitch_gql_service import VOD_PLAYBACK_TOKEN_HASH, _gql_persisted

    gql_data = _gql_persisted(
        "PlaybackAccessToken",
        VOD_PLAYBACK_TOKEN_HASH,
        {
            "isLive": True,
            "login": login.lower(),
            "isVod": False,
            "vodID": "",
            "playerType": player_type,
            "platform": "site",
        },
    )
    token_node = gql_data.get("streamPlaybackAccessToken") or gql_data.get("playbackAccessToken") or {}
    sig = token_node.get("signature")
    token = token_node.get("value")
    if not sig or not token:
        return None
    return sig, token


def _twitch_usher_master_url(login: str, sig: str, token: str) -> str:
    query = urlencode({
        "allow_source": "true",
        "allow_audio_only": "true",
        "playlist_include_framerate": "true",
        "supported_codecs": "h264",
        "platform": "web",
        # LL-HLS: short segments + PART tags — hls.js 1.6 lowLatencyMode. The
        # preview session probes the media playlist for #EXT-X-PART-INF at
        # session creation and falls back to a non-LL master if absent.
        "low_latency": "true",
        "p": str(random.randint(1_000_000, 9_999_999)),
        "nauth": token,
        "nauthsig": sig,
    })
    return f"https://usher.ttvnw.net/api/channel/hls/{login.lower()}.m3u8?{query}"


def _twitch_master_for_player_type(login: str, player_type: str) -> Optional[dict]:
    """Build a fresh usher master URL for one player type (no cache)."""
    token = _twitch_gql_live_token(login, player_type)
    if not token:
        return None
    sig, tok = token
    return {"url": _twitch_usher_master_url(login, sig, tok), "player_type": player_type}


def _twitch_pick_media_variant(master_url: str) -> Optional[str]:
    """Fetch an usher master and return its first (highest-quality) variant URL."""
    try:
        resp = requests.get(master_url, headers=_TWITCH_HEADERS, timeout=8.0)
        resp.raise_for_status()
    except (requests.RequestException, OSError, ValueError):
        logger.debug("usher master fetch failed: %s", master_url)
        return None
    from services.twitch_gql_service import _parse_hls_master_variants

    variants = _parse_hls_master_variants(master_url, resp.text)
    if not variants:
        return None
    return variants[0]["url"]


def _twitch_media_has_ads(media_url: str) -> Optional[bool]:
    """True when a media playlist contains 'stitched' ad segments.

    None means the fetch failed — the caller should try the next player type.
    """
    try:
        resp = requests.get(media_url, headers=_TWITCH_HEADERS, timeout=8.0)
        resp.raise_for_status()
        return "stitched" in resp.text
    except (requests.RequestException, OSError, ValueError):
        logger.debug("media playlist fetch failed: %s", media_url)
        return None


def probe_twitch_live_master(
    login: str,
    player_types: Optional[Sequence[str]] = None,
    skip_cache: bool = False,
) -> Optional[dict]:
    """Rotate across player types (vaft order) and return the first ad-free master.

    Returns ``{"url", "headers", "player_type", "ad_free"}`` or None when the
    channel is unreachable (every player type failed to fetch). When no player
    type's media playlist is ad-free, falls back to the embed master with
    ``ad_free=False`` — the frontend pLoader strips stitched segments locally.
    """
    login = (login or "").strip().lower()
    if not login:
        return None
    order = tuple(player_types) if player_types else _TWITCH_PLAYER_TYPES
    if not order:
        return None
    if not skip_cache:
        with _TWITCH_MASTER_LOCK:
            cached = _TWITCH_MASTER_CACHE.get(login)
        if cached and time.time() - cached[0] < _TWITCH_MASTER_TTL_SEC:
            return cached[1]

    # vaft flow: build + probe one player type at a time, stop at the first
    # clean one (fresh GQL token per type, fetched lazily like vaft).
    first_built: Optional[dict] = None
    fallback: Optional[dict] = None  # embed master — vaft FallbackPlayerType
    result: Optional[dict] = None
    for pt in order:
        built = _twitch_master_for_player_type(login, pt)
        if not built:
            continue
        if first_built is None:
            first_built = built
        media_url = _twitch_pick_media_variant(built["url"])
        if media_url is None:
            continue
        if _twitch_media_has_ads(media_url) is False:
            result = {
                "url": built["url"],
                "headers": dict(_TWITCH_HEADERS),
                "player_type": built["player_type"],
                "ad_free": True,
            }
            break
        if fallback is None and built["player_type"] == _TWITCH_FALLBACK_PLAYER_TYPE:
            fallback = built

    if result is None and fallback is None and first_built is not None:
        fallback = first_built
    if result is None and fallback is not None:
        # vaft fallback: no player type is ad-free — serve the embed master
        # anyway and let the frontend strip the stitched segments locally.
        result = {
            "url": fallback["url"],
            "headers": dict(_TWITCH_HEADERS),
            "player_type": fallback["player_type"],
            "ad_free": False,
        }

    if result is not None and not skip_cache:
        with _TWITCH_MASTER_LOCK:
            _TWITCH_MASTER_CACHE[login] = (time.time(), result)
    return result


def _twitch_helix_live_info(login: str) -> Optional[dict]:
    """Fast live-status check via Helix (client creds from env).

    Returns the live metadata dict, or None when the channel is offline.
    Raises on any API failure so the caller can fall back to a page scrape.
    """
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("TWITCH_CLIENT_ID/SECRET not configured")

    token_resp = requests.post(
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=10,
    )
    token_resp.raise_for_status()
    access_token = (token_resp.json() or {}).get("access_token")
    if not access_token:
        raise RuntimeError("Helix token missing")

    resp = requests.get(
        "https://api.twitch.tv/helix/streams",
        params={"user_login": login.lower()},
        headers={"Client-Id": client_id, "Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    resp.raise_for_status()
    data = (resp.json() or {}).get("data") or []
    if not data:
        return None  # offline — fast path, no page scrape

    stream = data[0]
    # Rotate player types and keep the first ad-free master (probe cached 60s).
    probed = probe_twitch_live_master(login)
    if not probed:
        raise RuntimeError("no live playback token from GQL")


    return {
        "url": probed["url"],
        "headers": probed["headers"],
        "title": stream.get("title") or login,
        "viewers": stream.get("viewer_count") or 0,
        "platform": "Twitch",
        "player_type": probed["player_type"],
        "ad_free": probed["ad_free"],
    }


def twitch_archive_info(login: str) -> Optional[dict]:
    """Resolve the channel's current (likely in-progress) VOD master URL.

    DVR fallback for the live popup when the frontend has no VOD URL to pass:
    the most recent broadcast from the GQL channel-videos list resolves to a
    usher vod master via the existing VOD playback-token flow. Returns
    {url, headers, vod_id, platform} or None (offline/never streamed).
    """
    login = (login or "").strip().lower()
    if not login:
        return None
    try:
        from services.twitch_gql_service import get_vod_playback_sync, list_channel_videos_sync

        vids = list_channel_videos_sync(login, limit=1)
        if not vids:
            return None
        vod_id = str(vids[0].get("id") or "").strip()
        if not vod_id:
            return None
        master_url, headers, _variants = get_vod_playback_sync(vod_id)
        return {"url": master_url, "headers": headers, "vod_id": vod_id, "platform": "Twitch"}
    except Exception as exc:
        logger.debug("twitch_archive_info(%r) failed: %s", login, exc)
        return None


def kick_archive_info(slug: str) -> Optional[dict]:
    """Resolve the channel's current (likely in-progress) VOD m3u8 URL.

    DVR fallback for the live popup: the most recent channel VOD from the Kick
    videos API. Returns {url, vod_id, platform} or None.
    """
    slug = (slug or "").strip()
    if not slug:
        return None
    try:
        from services.kick_api_service import list_channel_videos_api

        vids = list_channel_videos_api(slug, limit=1)
        if not vids:
            return None
        m3u8 = (vids[0].m3u8_url or "").strip()
        if not m3u8:
            return None
        return {"url": m3u8, "vod_id": vids[0].id or "", "platform": "Kick"}
    except Exception as exc:
        logger.debug("kick_archive_info(%r) failed: %s", slug, exc)
        return None


def twitch_live_info(login: str) -> Optional[dict]:
    """Return live-stream metadata dict or None if offline.

    Uses the Helix API when TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are set
    (fast offline detection, no yt-dlp), and falls back to a yt-dlp page
    scrape otherwise or on any Helix failure.
    """
    if os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET"):
        try:
            return _twitch_helix_live_info(login)
        except Exception as exc:
            logger.debug(
                "twitch_live_info(%r): Helix failed — falling back to page scrape: %s", login, exc
            )

    import yt_dlp

    url = f"https://www.twitch.tv/{login}"
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        logger.debug("twitch_live_info(%r) failed: %s", login, exc)
        return None

    if not info or not info.get("is_live"):
        return None

    formats = info.get("formats") or []
    # Pick the best m3u8 format at ≤720p
    candidates = [
        f for f in formats
        if f.get("protocol") in ("m3u8", "m3u8_native") and f.get("vcodec") != "none"
    ]
    best = None
    for f in candidates:
        h = int(f.get("height") or 0)
        if best is None or (h <= 720 and h > int(best.get("height") or 0)):
            best = f
    if not best:
        # fallback: first m3u8
        best = next((f for f in candidates), None)
    if not best:
        return None

    return {
        "url": best["url"],
        "headers": {
            "Referer": "https://www.twitch.tv/",
            "Origin": "https://www.twitch.tv/",
        },
        "title": info.get("title") or login,
        "viewers": info.get("viewer_count") or 0,
        "platform": "Twitch",
    }


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------


def youtube_live_info(handle: str) -> Optional[dict]:
    """Return live-stream metadata dict or None if offline/unavailable.

    Resolves ``https://www.youtube.com/@{handle}/live`` → videoId, then
    attempts extraction via yt-dlp (which uses innertube when available).
    Returns None with a ``reason`` key when auth is missing (bot wall).
    """
    import yt_dlp

    live_url = (
        f"https://www.youtube.com/channel/{handle}/live"
        if handle.startswith("UC")
        else f"https://www.youtube.com/@{handle.lstrip('@')}/live"
    )
    # Fetch the /live redirect page to get the actual videoId
    try:
        resp = requests.get(
            live_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=15,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        logger.debug("youtube_live_info(%r) page fetch failed: %s", handle, exc)
        return {"reason": f"Page fetch failed: {exc}"}

    vid_match = _YT_LIVE_PAGE_RE.search(resp.content or b"")
    if not vid_match:
        return {"reason": "Could not find videoId in live page"}

    vid = vid_match.group(1).decode()
    watch_url = f"https://www.youtube.com/watch?v={vid}"

    # Primary: app InnerTube — survives bot walls that kill yt-dlp (its
    # multi-client race + POT carries live HLS manifest in streamingData).
    try:
        from services.youtube_innertube import innertube_extract_info

        info = innertube_extract_info(watch_url, timeout=20.0)
    except Exception as exc:
        logger.debug("youtube_live_info(%r) innertube failed: %s", handle, exc)
        info = None
    if info:
        candidates = [
            f for f in (info.get("formats") or [])
            if f.get("protocol") in ("m3u8", "m3u8_native")
        ]
        best = None
        for f in candidates:
            h = int(f.get("height") or 0)
            if best is None or (h <= 720 and h > int(best.get("height") or 0)):
                best = f
        if not best:
            best = next((f for f in candidates), None)
        if best:
            return {
                "url": best["url"],
                "headers": dict(info.get("http_headers") or {}),
                "title": info.get("title") or handle,
                "viewers": info.get("viewer_count") or 0,
                "platform": "YouTube",
            }

    # Fallback: yt-dlp — wire the app's YouTube auth (cookies / PO
    # token / visitor data) or every live extract dies at the bot wall.
    from services.youtube_session import (
        apply_ytdlp_cookie_opts,
        ytdlp_extractor_args,
        youtube_session_from_settings,
    )

    yt_session = youtube_session_from_settings(video_id=vid)
    opts = {"quiet": True, "no_warnings": True}
    opts["extractor_args"] = ytdlp_extractor_args(yt_session)
    apply_ytdlp_cookie_opts(opts, yt_session)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(watch_url, download=False)
    except Exception as exc:
        low = str(exc).lower()
        if "sign in" in low or "cookie" in low or "bot" in low or "unavailable" in low:
            return {"reason": f"Extraction unavailable (bot wall / auth needed): {exc}"}
        logger.debug("youtube_live_info(%r) extract failed: %s", handle, exc)
        return {"reason": f"Extraction failed: {exc}"}

    if not info or not info.get("is_live"):
        return {"reason": "Stream is not live"}

    formats = info.get("formats") or []
    # Pick best m3u8 at ≤720p
    candidates = [
        f for f in formats
        if f.get("protocol") in ("m3u8", "m3u8_native")
    ]
    best = None
    for f in candidates:
        h = int(f.get("height") or 0)
        if best is None or (h <= 720 and h > int(best.get("height") or 0)):
            best = f
    if not best:
        best = next((f for f in candidates), None)
    if not best:
        return {"reason": "No suitable HLS format found"}

    return {
        "url": best["url"],
        "headers": dict(info.get("http_headers") or {}),
        "title": info.get("title") or handle,
        "viewers": info.get("viewer_count") or 0,
        "platform": "YouTube",
    }


# ---------------------------------------------------------------------------
# DVR / HLS stream recording via ffmpeg
# ---------------------------------------------------------------------------


def download_live_stream(
    url: str,
    output_path: str,
    *,
    headers: Optional[dict] = None,
    cancel_event: threading.Event,
    pause_event: Optional[threading.Event] = None,
    progress_hook: Optional[callable] = None,
    register_abort: Optional[callable] = None,
    register_temp_dir: Optional[callable] = None,
    **kwargs,
) -> str:
    """Record an HLS live stream to *output_path* until *cancel_event* is set.

    Runs ffmpeg with ``-c copy -f mp4`` and parses stderr for time= progress.
    When cancel_event fires, sends SIGTERM to ffmpeg and returns *output_path*.
    """
    # pylint: disable=unused-argument
    del pause_event, register_abort, register_temp_dir, kwargs

    ffmpeg = _resolve_ffmpeg_exe()
    cmd = [ffmpeg, "-y"]

    # Start at the live edge of HLS playlists rather than the oldest segment.
    cmd.append("-live_start_index")
    cmd.append("-1")

    # Pass custom headers via a single -headers string.
    header_lines = "".join(f"{k}: {v}\r\n" for k, v in sorted((headers or {}).items()))
    if header_lines:
        cmd.extend(["-headers", header_lines])

    cmd.extend(["-i", url, "-c", "copy", "-f", "mp4", output_path])

    proc = sp.Popen(
        cmd,
        stdout=sp.PIPE,
        stderr=sp.PIPE,
        creationflags=_NO_WINDOW,
    )
    register_child_pid(proc.pid)

    # Pipe stderr reader thread — parse time= progress
    _stderr_buf: list[bytes] = []

    def _reader() -> None:
        for line in iter(proc.stderr.readline, b""):  # type: ignore[union-attr]
            _stderr_buf.append(line)
            if progress_hook is not None:
                _emit_progress(line, progress_hook)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    try:
        while True:
            if cancel_event.wait(timeout=1.0):
                break
            if proc.poll() is not None:
                break
    finally:
        _kill_pid(proc.pid)
        proc.wait(timeout=10)
        unregister_child_pid(proc.pid)

    # Log any stderr diagnostics
    stderr_text = b"".join(_stderr_buf).decode("utf-8", "replace")
    if stderr_text.strip():
        logger.debug("ffmpeg live-capture stderr (%s):\n%s", output_path, stderr_text.strip())

    return output_path


def _emit_progress(line: bytes, hook: Optional[Callable[[dict], None]]) -> None:
    """Parse ffmpeg ``time=HH:MM:SS.MS`` line and call *hook*."""
    if hook is None:
        return
    raw = line.decode("utf-8", "replace")
    m = re.search(r"time=(\d+):(\d+):(\d+)\.(\d+)", raw)
    if not m:
        return
    h, mi, s, ms = int(m[1]), int(m[2]), int(m[3]), int(m[4])
    secs = h * 3600 + mi * 60 + s + ms / 100
    hook({
        "status": "downloading",
        "percent": 0,
        "speed": f"live {secs}s",
        "eta_seconds": None,
    })
