"""Live stream detection and DVR capture for Kick, Twitch, YouTube."""

import json
import logging
import os
import re
import subprocess as sp
import threading
from pathlib import Path
from typing import Any, Callable, Optional

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


def twitch_live_info(login: str) -> Optional[dict]:
    """Return live-stream metadata dict or None if offline.

    Uses yt-dlp to scrape the channel page, picks the best m3u8 format
    with height ≤ 720.
    """
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

    live_url = f"https://www.youtube.com/@{handle}/live"
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

    # Attempt yt-dlp extraction
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
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
