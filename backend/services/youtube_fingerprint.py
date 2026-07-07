"""Single coherent YouTube HTTP fingerprint (UA + locale) for all extract paths."""
from __future__ import annotations

YT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    " AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
YT_ACCEPT_LANGUAGE = "*"  # ponytail: no forced en — avoids translated titles / auto-dub audio


def youtube_http_headers(*, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": YT_USER_AGENT,
        "Accept-Language": YT_ACCEPT_LANGUAGE,
    }
    if extra:
        headers.update(extra)
    return headers


assert youtube_http_headers()["User-Agent"] == YT_USER_AGENT
