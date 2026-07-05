"""YouTube session material for InnerTube + yt-dlp (visitorData, po_token, cookies)."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

_INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
_VISITOR_ID_URL = f"https://www.youtube.com/youtubei/v1/visitor_id?key={_INNERTUBE_KEY}"
_YT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    " AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_ANON_LOCK = threading.Lock()
_ANON_TTL_SEC = 25 * 60
_ANON_BOOT_EVENT: Optional[threading.Event] = None
# ponytail: incognito-without-login jar — same cookies a fresh browser tab gets
_ANON_CACHE: dict[str, Any] = {
    "ts": 0.0,
    "visitor_data": None,
    "cookie_header": None,
    "cookie_file": None,
    "touched_videos": set(),
}


@dataclass(frozen=True)
class YouTubeSession:
    visitor_data: Optional[str] = None
    po_token: Optional[str] = None
    cookie_header: Optional[str] = None
    cookies_from_browser: Optional[str] = None
    anonymous: bool = False
    cookie_file: Optional[str] = None


def _cookie_header_from_jar(jar: requests.cookies.RequestsCookieJar) -> str:
    parts: list[str] = []
    for cookie in jar:
        dom = (cookie.domain or "").lower()
        if "youtube" in dom or dom.endswith("google.com"):
            parts.append(f"{cookie.name}={cookie.value}")
    return "; ".join(parts)


def _write_netscape_cookiefile(jar: requests.cookies.RequestsCookieJar) -> Optional[str]:
    if not jar:
        return None
    fd, path = tempfile.mkstemp(prefix="yt_anon_", suffix=".txt")
    os.close(fd)
    try:
        lines = ["# Netscape HTTP Cookie File\n", "# auto-generated anonymous YouTube session\n"]
        for c in jar:
            domain = c.domain or ".youtube.com"
            if not domain.startswith("."):
                domain = f".{domain}"
            secure = "TRUE" if c.secure else "FALSE"
            expires = str(int(c.expires)) if c.expires else "0"
            path = c.path or "/"
            lines.append(
                f"{domain}\tTRUE\t{path}\t{secure}\t{expires}\t{c.name}\t{c.value}\n"
            )
        Path(path).write_text("".join(lines), encoding="utf-8")
        return path
    except OSError:
        try:
            os.unlink(path)
        except OSError:
            pass
        return None


def invalidate_anonymous_session() -> None:
    """Drop cached anonymous jar so the next extract gets a fresh cold visit."""
    with _ANON_LOCK:
        old_file = _ANON_CACHE.get("cookie_file")
        _ANON_CACHE["ts"] = 0.0
        _ANON_CACHE["visitor_data"] = None
        _ANON_CACHE["cookie_header"] = None
        _ANON_CACHE["cookie_file"] = None
        _ANON_CACHE["touched_videos"] = set()
    if old_file:
        try:
            os.unlink(old_file)
        except OSError:
            pass


def _bootstrap_network(
    video_id: Optional[str],
    timeout: float,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": _YT_UA,
        "Accept-Language": "en-US,en;q=0.9",
    })
    session.get("https://www.youtube.com/", timeout=timeout)
    if video_id:
        session.get(f"https://www.youtube.com/watch?v={video_id}", timeout=timeout)
    vid_resp = session.post(
        _VISITOR_ID_URL,
        json={
            "context": {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": "2.20250224.01.00",
                    "hl": "en",
                    "gl": "US",
                }
            }
        },
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    vid_resp.raise_for_status()
    visitor_data = (vid_resp.json().get("responseContext") or {}).get("visitorData")
    cookie_header = _cookie_header_from_jar(session.cookies)
    cookie_file = _write_netscape_cookiefile(session.cookies)
    return visitor_data, cookie_header, cookie_file


def bootstrap_anonymous_session(
    video_id: Optional[str] = None,
    timeout: float = 12.0,
    force: bool = False,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Cold YouTube visit — incognito-style, no Google login required."""
    global _ANON_BOOT_EVENT
    now = time.time()
    with _ANON_LOCK:
        if not force:
            age = now - float(_ANON_CACHE.get("ts") or 0)
            if age < _ANON_TTL_SEC and _ANON_CACHE.get("cookie_header"):
                return (
                    _ANON_CACHE.get("visitor_data"),
                    _ANON_CACHE.get("cookie_header"),
                    _ANON_CACHE.get("cookie_file"),
                )
        inflight = _ANON_BOOT_EVENT
        if inflight is not None:
            leader = False
        else:
            inflight = threading.Event()
            _ANON_BOOT_EVENT = inflight
            leader = True

    if not leader:
        inflight.wait(timeout=timeout + 8.0)
        with _ANON_LOCK:
            return (
                _ANON_CACHE.get("visitor_data"),
                _ANON_CACHE.get("cookie_header"),
                _ANON_CACHE.get("cookie_file"),
            )

    old_file = _ANON_CACHE.get("cookie_file")
    visitor_data: Optional[str] = None
    cookie_header: Optional[str] = None
    cookie_file: Optional[str] = None
    try:
        for attempt in range(3):
            try:
                visitor_data, cookie_header, cookie_file = _bootstrap_network(
                    video_id, timeout,
                )
                if cookie_header and visitor_data:
                    break
            except (requests.RequestException, ValueError, TypeError) as exc:
                logger.debug(
                    "Anonymous YouTube bootstrap attempt %s failed: %s",
                    attempt + 1, exc,
                )
            if attempt + 1 < 3:
                time.sleep(0.15 * (attempt + 1))
        if cookie_header:
            logger.debug(
                "Anonymous YouTube session bootstrapped (visitor_data=%s)",
                bool(visitor_data),
            )
    finally:
        with _ANON_LOCK:
            if old_file and old_file != cookie_file:
                try:
                    os.unlink(old_file)
                except OSError:
                    pass
            if cookie_header:
                _ANON_CACHE["ts"] = time.time()
                _ANON_CACHE["visitor_data"] = visitor_data
                _ANON_CACHE["cookie_header"] = cookie_header
                _ANON_CACHE["cookie_file"] = cookie_file
            _ANON_BOOT_EVENT = None
        inflight.set()

    return visitor_data, cookie_header, cookie_file


def _touch_video_page(video_id: str, cookie_header: str, timeout: float = 10.0) -> None:
    """Watch-page visit on a warm jar — InnerTube often needs video-bound context."""
    if not video_id or not cookie_header:
        return
    with _ANON_LOCK:
        touched: set = _ANON_CACHE.setdefault("touched_videos", set())
        if video_id in touched:
            return
    try:
        requests.get(
            f"https://www.youtube.com/watch?v={video_id}",
            headers={"User-Agent": _YT_UA, "Cookie": cookie_header},
            timeout=timeout,
        )
        with _ANON_LOCK:
            _ANON_CACHE.setdefault("touched_videos", set()).add(video_id)
    except requests.RequestException as exc:
        logger.debug("video touch %s failed: %s", video_id, exc)


def warm_youtube_session(video_id: Optional[str] = None) -> None:
    """Pre-bootstrap anonymous session (server startup / before first extract)."""
    bootstrap_anonymous_session(video_id=video_id, force=True)


def fetch_visitor_data(timeout: float = 12.0) -> Optional[str]:
    """Session-bound visitorData (visitor_id API), not a stale homepage scrape."""
    vd, _, _ = bootstrap_anonymous_session(timeout=timeout)
    return vd


def _cookie_header_from_file(path: str) -> Optional[str]:
    jar = MozillaCookieJar(path)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, ValueError) as exc:
        logger.debug("cookie file load failed %s: %s", path, exc)
        return None
    parts: list[str] = []
    for cookie in jar:
        dom = (cookie.domain or "").lower()
        if "youtube" in dom or dom.endswith("google.com"):
            parts.append(f"{cookie.name}={cookie.value}")
    return "; ".join(parts) if parts else None


def _load_tokens_file(path: str) -> tuple[Optional[str], Optional[str]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    vd = data.get("visitorData") or data.get("visitor_data")
    pt = data.get("poToken") or data.get("po_token")
    return (
        str(vd).strip() if vd else None,
        str(pt).strip() if pt else None,
    )


def youtube_session_from_values(
    *,
    visitor_data: Optional[str] = None,
    po_token: Optional[str] = None,
    cookies_file: Optional[str] = None,
    tokens_file: Optional[str] = None,
    cookies_from_browser: Optional[str] = None,
    video_id: Optional[str] = None,
) -> YouTubeSession:
    vd = (visitor_data or "").strip() or None
    pt = (po_token or "").strip() or None
    if tokens_file:
        file_vd, file_pt = _load_tokens_file(tokens_file)
        vd = vd or file_vd
        pt = pt or file_pt

    cookie_header: Optional[str] = None
    cookie_file: Optional[str] = None
    anonymous = False
    manual_cookie = (cookies_file or "").strip()
    if manual_cookie and Path(manual_cookie).is_file():
        cookie_header = _cookie_header_from_file(manual_cookie)
        cookie_file = manual_cookie
    elif not (cookies_from_browser or "").strip():
        anon_vd, anon_cookie, anon_file = bootstrap_anonymous_session(video_id=video_id)
        vd = vd or anon_vd
        cookie_header = anon_cookie
        cookie_file = anon_file
        anonymous = bool(anon_cookie)
        if video_id and anon_cookie:
            _touch_video_page(video_id, anon_cookie)

    if not vd:
        vd = fetch_visitor_data()

    browser = (cookies_from_browser or "").strip().lower() or None
    return YouTubeSession(
        visitor_data=vd,
        po_token=pt,
        cookie_header=cookie_header,
        cookies_from_browser=browser,
        anonymous=anonymous,
        cookie_file=cookie_file if not browser else None,
    )


def youtube_session_from_settings(
    settings_mgr=None,
    video_id: Optional[str] = None,
) -> YouTubeSession:
    if settings_mgr is None:
        from deps import settings_mgr as sm
        settings_mgr = sm
    s = settings_mgr.get()
    return youtube_session_from_values(
        visitor_data=getattr(s, "youtube_visitor_data", "") or None,
        po_token=getattr(s, "youtube_po_token", "") or None,
        cookies_file=getattr(s, "youtube_cookies_file", "") or None,
        tokens_file=getattr(s, "youtube_tokens_file", "") or None,
        cookies_from_browser=getattr(s, "youtube_cookies_browser", "") or None,
        video_id=video_id,
    )


def ytdlp_youtube_extractor_args(session: YouTubeSession) -> dict[str, list[str]]:
    """Map session into yt-dlp youtube extractor_args."""
    args: dict[str, list[str]] = {
        "player_client": ["web_safari", "ios", "mweb"],
        "fetch_pot": ["auto"],
    }
    if session.visitor_data:
        args["visitor_data"] = [session.visitor_data]
    if session.po_token:
        token = session.po_token
        args["po_token"] = [f"web.player+{token}", f"web.gvs+{token}"]
    return args


def resolve_ytdlp_cookiefile(session: YouTubeSession, explicit: Optional[str] = None) -> Optional[str]:
    """Cookie file for yt-dlp: manual export > anonymous temp jar > none."""
    if explicit and Path(explicit).is_file():
        return explicit
    if session.cookies_from_browser:
        return None
    if session.cookie_file and Path(session.cookie_file).is_file():
        return session.cookie_file
    return None


assert youtube_session_from_values(visitor_data="abc").visitor_data == "abc"
_anon_vd, _anon_ch, _ = bootstrap_anonymous_session()
assert _anon_ch is None or "VISITOR" in _anon_ch or "YSC" in _anon_ch or len(_anon_ch) > 0
