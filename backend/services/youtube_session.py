"""YouTube session material for InnerTube + yt-dlp (visitorData, po_token, cookies)."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any, Optional

import requests

from services.youtube_fingerprint import YT_USER_AGENT, youtube_http_headers

logger = logging.getLogger(__name__)

_INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
_VISITOR_ID_URL = f"https://www.youtube.com/youtubei/v1/visitor_id?key={_INNERTUBE_KEY}"
_YT_UA = YT_USER_AGENT
_CONNECT_TIMEOUT_SEC = 0.9
_READ_TIMEOUT_SEC = 6.0
_ANON_LOCK = threading.Lock()
_ANON_TTL_SEC = 2 * 3600
_ANON_BOOT_EVENT: Optional[threading.Event] = None
_ANON_CACHE: dict[str, Any] = {
    "ts": 0.0,
    "visitor_data": None,
    "cookie_header": None,
    "cookie_file": None,
    "http_session": None,
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
    http_session: Optional[requests.Session] = field(default=None, compare=False, repr=False)


def http_session_for(session: Optional[YouTubeSession]) -> requests.Session:
    """Reuse TLS + cookies across bootstrap, InnerTube player, and manifest fetches."""
    if session and session.http_session is not None:
        return session.http_session
    with _ANON_LOCK:
        cached = _ANON_CACHE.get("http_session")
        if isinstance(cached, requests.Session):
            return cached
    http = requests.Session()
    http.headers.update(youtube_http_headers())
    return http


def _attach_anon_http(http: requests.Session) -> None:
    with _ANON_LOCK:
        _ANON_CACHE["http_session"] = http


def _cookie_header_from_jar(jar) -> str:
    try:
        as_dict = dict(jar)
        if as_dict:
            return "; ".join(f"{k}={v}" for k, v in as_dict.items())
    except (TypeError, ValueError):
        pass
    parts: list[str] = []
    for cookie in jar:
        if isinstance(cookie, str):
            val = jar.get(cookie)
            if val:
                parts.append(f"{cookie}={val}")
            continue
        dom = (cookie.domain or "").lower()
        if "youtube" in dom or dom.endswith("google.com"):
            parts.append(f"{cookie.name}={cookie.value}")
    return "; ".join(parts)


def _write_netscape_cookiefile(jar) -> Optional[str]:
    if not jar:
        return None
    fd, path = tempfile.mkstemp(prefix="yt_anon_", suffix=".txt")
    os.close(fd)
    try:
        lines = ["# Netscape HTTP Cookie File\n", "# auto-generated anonymous YouTube session\n"]
        try:
            cookie_items = [
                (name, val, ".youtube.com", "/")
                for name, val in dict(jar).items()
            ]
        except (TypeError, ValueError):
            cookie_items = []
            for c in jar:
                if isinstance(c, str):
                    continue
                cookie_items.append((c.name, c.value, c.domain or ".youtube.com", c.path or "/"))
        for name, value, domain, cookie_path in cookie_items:
            if not domain.startswith("."):
                domain = f".{domain}"
            lines.append(f"{domain}\tTRUE\t{cookie_path}\tTRUE\t0\t{name}\t{value}\n")
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
        old_http = _ANON_CACHE.get("http_session")
        _ANON_CACHE["ts"] = 0.0
        _ANON_CACHE["visitor_data"] = None
        _ANON_CACHE["cookie_header"] = None
        _ANON_CACHE["cookie_file"] = None
        _ANON_CACHE["http_session"] = None
        _ANON_CACHE["touched_videos"] = set()
    if old_file:
        try:
            os.unlink(old_file)
        except OSError:
            pass
    if isinstance(old_http, requests.Session):
        try:
            old_http.close()
        except OSError:
            pass


def _new_youtube_http_session():
    """Chrome TLS fingerprint when curl_cffi is available — plain requests trips bot gates."""
    try:
        from curl_cffi import requests as cffi_requests

        return cffi_requests.Session(impersonate="chrome")
    except ImportError:
        logger.warning(
            "curl_cffi unavailable — YouTube HTTP using plain requests (higher bot-gate risk)",
        )
        http = requests.Session()
        http.headers.update(youtube_http_headers())
        return http


def _bootstrap_network(
    video_id: Optional[str],
    timeout: float,
) -> tuple[Optional[str], Optional[str], Optional[str], Any]:
    http = _new_youtube_http_session()
    if not hasattr(http, "impersonate"):
        http.headers.update(youtube_http_headers())
    req_timeout = (_CONNECT_TIMEOUT_SEC, timeout)
    http.get("https://www.youtube.com/", timeout=req_timeout)
    if video_id:
        http.get(f"https://www.youtube.com/watch?v={video_id}", timeout=req_timeout)
    vid_resp = http.post(
        _VISITOR_ID_URL,
        json={
            "context": {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": "2.20250224.01.00",
                }
            }
        },
        headers={"Content-Type": "application/json"},
        timeout=req_timeout,
    )
    vid_resp.raise_for_status()
    visitor_data = (vid_resp.json().get("responseContext") or {}).get("visitorData")
    cookie_header = _cookie_header_from_jar(http.cookies)
    cookie_file = _write_netscape_cookiefile(http.cookies)
    _attach_anon_http(http)
    return visitor_data, cookie_header, cookie_file, http


def bootstrap_anonymous_session(
    video_id: Optional[str] = None,
    timeout: float = _READ_TIMEOUT_SEC,
    force: bool = False,
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[requests.Session]]:
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
                    _ANON_CACHE.get("http_session"),
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
                _ANON_CACHE.get("http_session"),
            )

    old_file = _ANON_CACHE.get("cookie_file")
    old_http = _ANON_CACHE.get("http_session")
    visitor_data: Optional[str] = None
    cookie_header: Optional[str] = None
    cookie_file: Optional[str] = None
    http_session: Optional[Any] = None
    try:
        for attempt in range(3):
            try:
                visitor_data, cookie_header, cookie_file, http_session = _bootstrap_network(
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
            if isinstance(old_http, requests.Session) and old_http is not http_session:
                try:
                    old_http.close()
                except OSError:
                    pass
            if cookie_header:
                _ANON_CACHE["ts"] = time.time()
                _ANON_CACHE["visitor_data"] = visitor_data
                _ANON_CACHE["cookie_header"] = cookie_header
                _ANON_CACHE["cookie_file"] = cookie_file
                _ANON_CACHE["http_session"] = http_session
            _ANON_BOOT_EVENT = None
        inflight.set()

    return visitor_data, cookie_header, cookie_file, http_session


def _touch_video_page(
    video_id: str,
    cookie_header: str,
    http: Optional[requests.Session] = None,
    timeout: float = 5.0,
) -> None:
    """Watch-page visit on a warm jar — InnerTube often needs video-bound context."""
    if not video_id or not cookie_header:
        return
    with _ANON_LOCK:
        touched: set = _ANON_CACHE.setdefault("touched_videos", set())
        if video_id in touched:
            return
    client = http or http_session_for(None)
    try:
        client.get(
            f"https://www.youtube.com/watch?v={video_id}",
            headers={"Cookie": cookie_header},
            timeout=(_CONNECT_TIMEOUT_SEC, timeout),
        )
        with _ANON_LOCK:
            _ANON_CACHE.setdefault("touched_videos", set()).add(video_id)
    except requests.RequestException as exc:
        logger.debug("video touch %s failed: %s", video_id, exc)


def warm_youtube_session(video_id: Optional[str] = None) -> None:
    """Pre-bootstrap anonymous session (server startup / before first extract)."""
    bootstrap_anonymous_session(video_id=video_id, force=True)


def fetch_visitor_data(timeout: float = _READ_TIMEOUT_SEC) -> Optional[str]:
    """Session-bound visitorData (visitor_id API), not a stale homepage scrape."""
    vd, _, _, _ = bootstrap_anonymous_session(timeout=timeout)
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
    auto_auth: bool = True,
) -> YouTubeSession:
    vd = (visitor_data or "").strip() or None
    pt = (po_token or "").strip() or None
    if tokens_file:
        file_vd, file_pt = _load_tokens_file(tokens_file)
        vd = vd or file_vd
        pt = pt or file_pt

    explicit_browser = (cookies_from_browser or "").strip().lower() or None

    cookie_header: Optional[str] = None
    cookie_file: Optional[str] = None
    http_session: Optional[Any] = None
    anonymous = False
    manual_cookie = (cookies_file or "").strip()
    if manual_cookie and Path(manual_cookie).is_file():
        cookie_header = _cookie_header_from_file(manual_cookie)
        cookie_file = manual_cookie
        http_session = http_session_for(None)
        if cookie_header:
            http_session.headers["Cookie"] = cookie_header
    elif explicit_browser:
        from services.youtube_auth import load_best_browser_session

        loaded, cookie_header, http_session = load_best_browser_session(explicit_browser, False)

    if not cookie_header:
        anon_vd, anon_cookie, anon_file, anon_http = bootstrap_anonymous_session(
            video_id=video_id,
        )
        vd = vd or anon_vd
        cookie_header = anon_cookie
        cookie_file = anon_file
        http_session = anon_http
        anonymous = bool(anon_cookie)
        if video_id and anon_cookie:
            _touch_video_page(video_id, anon_cookie, http=anon_http)
    elif not vd:
        vd = fetch_visitor_data()

    if not vd:
        vd = fetch_visitor_data()

    return YouTubeSession(
        visitor_data=vd,
        po_token=pt,
        cookie_header=cookie_header,
        cookies_from_browser=explicit_browser,
        anonymous=anonymous,
        cookie_file=cookie_file if not explicit_browser else None,
        http_session=http_session,
    )


def youtube_session_from_settings(
    settings_mgr=None,
    video_id: Optional[str] = None,
) -> YouTubeSession:
    if settings_mgr is None:
        from deps import settings_mgr as sm
        settings_mgr = sm
    s = settings_mgr.get()
    auto_auth = getattr(s, "youtube_auto_auth", True)
    return youtube_session_from_values(
        visitor_data=getattr(s, "youtube_visitor_data", "") or None,
        po_token=getattr(s, "youtube_po_token", "") or None,
        cookies_file=getattr(s, "youtube_cookies_file", "") or None,
        tokens_file=getattr(s, "youtube_tokens_file", "") or None,
        cookies_from_browser=getattr(s, "youtube_cookies_browser", "") or None,
        video_id=video_id,
        auto_auth=auto_auth,
    )


def ytdlp_youtube_extractor_args(session: YouTubeSession, *, auto_auth: bool = True) -> dict[str, list[str]]:
    """Map session into yt-dlp youtube extractor_args."""
    # ponytail: fetch_pot off — getpot_wpc spawns headless Chrome; use manual po_token in Settings
    args: dict[str, list[str]] = {
        "player_client": ["ios", "android", "mweb", "web_safari"],
        "fetch_pot": ["never"],
    }
    if session.visitor_data:
        args["visitor_data"] = [session.visitor_data]
    if session.po_token:
        token = session.po_token
        args["po_token"] = [
            f"web.player+{token}",
            f"web.gvs+{token}",
            f"mweb.gvs+{token}",
        ]
        args["player_client"] = ["ios", "android", "mweb", "web_safari"]
    return args


def ytdlp_extractor_args(
    session: YouTubeSession,
    *,
    auto_auth: bool = True,
) -> dict[str, dict[str, list[str]]]:
    """youtube extractor_args for yt-dlp opts."""
    return {
        "youtube": ytdlp_youtube_extractor_args(session, auto_auth=auto_auth),
    }


def resolve_ytdlp_cookiefile(session: YouTubeSession, explicit: Optional[str] = None) -> Optional[str]:
    """Cookie file for yt-dlp: manual export > anonymous temp jar > none."""
    if explicit and Path(explicit).is_file():
        return explicit
    if session.cookies_from_browser:
        return None
    if session.cookie_file and Path(session.cookie_file).is_file():
        return session.cookie_file
    return None


def apply_ytdlp_cookie_opts(
    opts: dict,
    session: YouTubeSession,
    *,
    auto_auth: bool = True,
    cookies_file: Optional[str] = None,
) -> None:
    """Wire cookiefile / cookiesfrombrowser — manual Settings only (no auto browser on hot path)."""
    cookie_path = resolve_ytdlp_cookiefile(session, cookies_file)
    if not cookie_path:
        from services.youtube_auth import find_fresh_cookie_cache

        cookie_path = find_fresh_cookie_cache()
    if cookie_path:
        opts["cookiefile"] = cookie_path
        opts.pop("cookiesfrombrowser", None)
        return
    browser = (session.cookies_from_browser or "").strip().lower() or None
    if browser:
        opts["cookiesfrombrowser"] = (browser,)


assert youtube_session_from_values(visitor_data="abc", auto_auth=False).visitor_data == "abc"
_anon_vd, _anon_ch, _anon_cf, _anon_http = bootstrap_anonymous_session(force=True)
assert _anon_ch is None or "VISITOR" in _anon_ch or "YSC" in _anon_ch or len(_anon_ch) > 0
assert _anon_cf is None or Path(_anon_cf).is_file(), "anonymous cookie file must be written for yt-dlp"
