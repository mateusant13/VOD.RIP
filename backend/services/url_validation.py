"""Kick/Twitch/YouTube VOD URL sanity checks for queue, history, and API validation."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from services.kick_models import extract_vod_id

_KICK_VOD_ID_MIN = 100_000
_TWITCH_VOD_ID_MIN = 1_000_000
_BAD_KICK_SLUGS = frozenset({"test", "a"})


def _bad_kick_slug(slug: str | None) -> bool:
    if not slug:
        return True
    if len(slug) <= 1:
        return True
    return slug.lower() in _BAD_KICK_SLUGS


def normalize_vod_url(url: str) -> str:
    """Canonicalize youtu.be/shorts/live + clips.twitch.tv aliases; strip noisy query. No network."""
    if not url:
        return url
    raw = url.strip()
    if raw.startswith("https//"):
        raw = "https://" + raw[len("https//"):]
    try:
        u = urlparse(raw)
    except Exception:
        return url
    host = (u.hostname or "").lower()
    path = u.path or ""
    qs = parse_qs(u.query or "")

    if host == "youtu.be" and path.strip("/"):
        vid = path.strip("/").split("/")[0]
        if vid:
            return f"https://www.youtube.com/watch?v={vid}"
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com") and path.startswith(("/live/", "/shorts/")):
        parts = path.split("/")
        if len(parts) >= 3 and parts[2]:
            return f"https://www.youtube.com/watch?v={parts[2]}"
    if host == "clips.twitch.tv" and path.strip("/"):
        slug = path.strip("/").split("/")[0]
        if slug:
            return f"https://twitch.tv/_/clip/{slug}"

    allow = None
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"):
        allow = "v"
    elif "twitch.tv" in host:
        allow = None
    new_q = ""
    if allow is not None and allow in qs:
        new_q = urlencode({allow: qs[allow]}, doseq=True)
    new_path = path.rstrip("/") if path else ""
    return urlunparse((u.scheme or "https", u.netloc, new_path, "", new_q, ""))


def is_sensible_vod_url(url: str) -> bool:
    """Return False for bogus Kick/Twitch/YouTube VOD URLs (test slugs, junk ids)."""
    if not url or not isinstance(url, str):
        return False
    url = normalize_vod_url(url)
    lower = url.lower().strip()
    if "kick.com" in lower:
        parsed = urlparse(lower)
        path = (parsed.path or "").strip("/")
        parts = path.split("/") if path else []
        if len(parts) >= 2 and parts[1] == "clips":
            return True
        if len(parts) >= 2 and parts[1] == "videos":
            if _bad_kick_slug(parts[0]):
                return False
            if extract_vod_id(url):
                return True
            m = re.search(r"/videos/(\d+)", lower)
            if m:
                return int(m.group(1)) >= _KICK_VOD_ID_MIN
            return False
        return False
    if "twitch.tv" in lower:
        if "clips.twitch.tv" in lower or "/clip/" in lower:
            return True
        m = re.search(r"/videos/(\d+)", lower)
        if m:
            return int(m.group(1)) >= _TWITCH_VOD_ID_MIN
        return False
    if "youtube.com" in lower or "youtu.be" in lower:
        if "watch?v=" in lower or "/shorts/" in lower:
            return True
        if "youtu.be/" in lower:
            return len(lower.split("youtu.be/")[-1].split("?")[0].strip("/")) >= 6
        return False
    return True


assert not is_sensible_vod_url("https://kick.com/test/videos/abc-123")
assert normalize_vod_url("https://youtu.be/abc123DEF45//foo") == "https://www.youtube.com/watch?v=abc123DEF45", normalize_vod_url("https://youtu.be/abc123DEF45//foo")
assert normalize_vod_url("https://www.youtube.com/live/abc123DEF45?feature=share") == "https://www.youtube.com/watch?v=abc123DEF45", normalize_vod_url("https://www.youtube.com/live/abc123DEF45?feature=share")
assert normalize_vod_url("https://clips.twitch.tv/Abc123") == "https://twitch.tv/_/clip/Abc123", normalize_vod_url("https://clips.twitch.tv/Abc123")
assert is_sensible_vod_url("https://www.youtube.com/watch?v=abc123DEF45") is True
assert is_sensible_vod_url("https://www.youtube.com/live/abc123DEF45") is True
print("cobalt-canon: ok")
