"""Kick datatypes and URL helpers (no Playwright dependency)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse


@dataclass
class KickVideo:
    id: str
    title: str
    duration: Optional[float] = None
    thumbnail: Optional[str] = None
    views: Optional[int] = None
    created_at: Optional[str] = None
    channel: Optional[str] = None
    url: Optional[str] = None
    category: Optional[str] = None
    m3u8_url: Optional[str] = None


@dataclass
class KickChannel:
    slug: str
    username: Optional[str] = None
    channel_id: Optional[int] = None
    followers: Optional[int] = None
    is_live: bool = False
    live_title: Optional[str] = None


def format_duration(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def extract_slug(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if "kick.com" not in (parsed.netloc or "").lower():
        return None
    path = (parsed.path or "").strip("/")
    if not path:
        return None
    return path.split("/")[0] or None


def extract_vod_id(url: str) -> Optional[str]:
    m = re.search(r"/videos/([\da-f]{8}-(?:[\da-f]{4}-){3}[\da-f]{12})", url, re.I)
    return m.group(1) if m else None
