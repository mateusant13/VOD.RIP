"""Central HTTP fingerprints — app identity vs browser-mimic User-Agents.

Use USER_AGENT for honest app-identified requests (GitHub updates,
thumbnails, WebSocket handshakes). Use youtube_http_headers() /
twitch_http_headers() / BROWSER_USER_AGENT for platform APIs and
CDNs that expect a desktop Chrome fingerprint (curl_cffi impersonation is
unchanged — these apply to requests.* / urllib paths).
"""
from __future__ import annotations

from services._version import USER_AGENT
from services.youtube_fingerprint import (
    YT_ACCEPT_LANGUAGE,
    YT_USER_AGENT,
    youtube_http_headers,
)

# Desktop Chrome fingerprint shared by YouTube + Twitch CDN/GQL/HLS fetches.
BROWSER_USER_AGENT = YT_USER_AGENT
TWITCH_USER_AGENT = YT_USER_AGENT


def twitch_http_headers(*, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Standard Twitch usher/GQL/CDN headers with a consistent Chrome UA."""
    headers = {
        "Referer": "https://www.twitch.tv/",
        "Origin": "https://www.twitch.tv/",
        "User-Agent": TWITCH_USER_AGENT,
    }
    if extra:
        headers.update(extra)
    return headers
