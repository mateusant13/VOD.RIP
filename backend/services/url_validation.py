"""Kick/Twitch/YouTube VOD URL sanity checks for queue, history, and API validation."""

from __future__ import annotations

import re
from urllib.parse import urlparse

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


def is_sensible_vod_url(url: str) -> bool:
    """Return False for bogus Kick/Twitch/YouTube VOD URLs (test slugs, junk ids)."""
    if not url or not isinstance(url, str):
        return False
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
