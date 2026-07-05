"""YouTube auth — browser cookie jar reads only (no headless Chrome / WPC)."""
from __future__ import annotations

import dataclasses
import logging
import shutil
import sys
import threading
import time
from typing import TYPE_CHECKING, Optional

import requests

if TYPE_CHECKING:
    from services.youtube_session import YouTubeSession

logger = logging.getLogger(__name__)

_BROWSER_CACHE: dict[str, tuple[float, str, requests.Session]] = {}
_BROWSER_CACHE_LOCK = threading.Lock()
_BROWSER_CACHE_TTL_SEC = 10 * 60
_BROWSER_FAIL_UNTIL: dict[str, float] = {}
_BROWSER_FAIL_COOLDOWN_SEC = 5 * 60


def pot_minting_enabled() -> bool:
    """WPC/Chrome PO mint removed — InnerTube + cookie file is enough."""
    return False


def infer_default_browser() -> Optional[str]:
    """Best-effort default HTTPS browser (Windows UserChoice, then common installs)."""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
            ) as key:
                prog_id, _ = winreg.QueryValueEx(key, "ProgId")
            pl = str(prog_id).lower()
            if "firefox" in pl:
                return "firefox"
            if "brave" in pl:
                return "brave"
            if "chrome" in pl and "edge" not in pl:
                return "chrome"
            if "edge" in pl or "msedge" in pl:
                return "edge"
        except OSError:
            pass
    for name in ("chrome", "edge", "firefox", "brave", "chromium"):
        if _browser_likely_installed(name):
            return name
    return None


def _browser_likely_installed(name: str) -> bool:
    if name == "chrome":
        return bool(shutil.which("chrome")) or _win_path_exists(
            r"Program Files\Google\Chrome\Application\chrome.exe",
            r"Program Files (x86)\Google\Chrome\Application\chrome.exe",
        )
    if name == "edge":
        return _win_path_exists(
            r"Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"Program Files\Microsoft\Edge\Application\msedge.exe",
        ) or bool(shutil.which("msedge"))
    if name == "firefox":
        return bool(shutil.which("firefox"))
    if name in ("brave", "chromium"):
        return bool(shutil.which(name))
    return False


def _win_path_exists(*parts: str) -> bool:
    if sys.platform != "win32":
        return False
    from pathlib import Path

    for part in parts:
        for root in (Path(os_environ_programfiles()), Path(os_environ_programfiles(x86=True))):
            if (root / part).is_file():
                return True
    return False


def os_environ_programfiles(x86: bool = False) -> str:
    import os

    key = "ProgramFiles(x86)" if x86 else "ProgramFiles"
    return os.environ.get(key) or (r"C:\Program Files (x86)" if x86 else r"C:\Program Files")


def resolve_youtube_browser(
    cookies_from_browser: Optional[str],
    auto_auth: bool,
) -> Optional[str]:
    manual = (cookies_from_browser or "").strip().lower() or None
    if manual:
        return manual
    if auto_auth:
        return infer_default_browser()
    return None


def _cookie_header_from_jar(jar: requests.cookies.RequestsCookieJar) -> Optional[str]:
    parts: list[str] = []
    for cookie in jar:
        dom = (cookie.domain or "").lower()
        if "youtube" in dom or dom.endswith("google.com"):
            parts.append(f"{cookie.name}={cookie.value}")
    return "; ".join(parts) if parts else None


def load_best_browser_session(
    cookies_from_browser: Optional[str],
    auto_auth: bool,
) -> tuple[Optional[str], Optional[str], Optional[requests.Session]]:
    """Try browsers in order; skip names that failed recently (Chrome DB lock, etc.)."""
    candidates: list[str] = []
    manual = (cookies_from_browser or "").strip().lower()
    if manual:
        candidates.append(manual)
    elif auto_auth:
        detected = infer_default_browser()
        if detected:
            candidates.append(detected)
        for name in ("edge", "firefox", "brave", "chrome"):
            if name not in candidates:
                candidates.append(name)

    now = time.time()
    for browser in candidates:
        if now < _BROWSER_FAIL_UNTIL.get(browser, 0):
            continue
        try:
            header, http = browser_cookie_session(browser)
            if header:
                return browser, header, http
        except Exception as exc:
            _BROWSER_FAIL_UNTIL[browser] = now + _BROWSER_FAIL_COOLDOWN_SEC
            logger.debug("browser cookies unavailable (%s): %s", browser, exc)
    return None, None, None


class _QuietCookieLogger:
    """Suppress yt-dlp [Cookies] progress + ERROR spam when DB is locked."""

    def debug(self, msg: object) -> None:
        pass

    def info(self, msg: object) -> None:
        pass

    def warning(self, msg: object, *, once: bool = False) -> None:
        pass

    def error(self, msg: object, cause=None) -> None:
        logger.debug("browser cookie extract: %s", msg)

    def progress_bar(self):
        return None


def browser_cookie_session(browser: str) -> tuple[Optional[str], requests.Session]:
    """Load YouTube/Google cookies from a local browser into a requests.Session."""
    from yt_dlp.cookies import extract_cookies_from_browser

    name = browser.strip().lower()
    now = time.time()
    with _BROWSER_CACHE_LOCK:
        hit = _BROWSER_CACHE.get(name)
        if hit and (now - hit[0]) < _BROWSER_CACHE_TTL_SEC:
            return hit[1], hit[2]
        if now < _BROWSER_FAIL_UNTIL.get(name, 0):
            raise RuntimeError(f"browser cookies cached failure ({name})")

    jar = extract_cookies_from_browser(name, logger=_QuietCookieLogger())
    http = requests.Session()
    http.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            " AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    })
    for cookie in jar:
        http.cookies.set(
            cookie.name,
            cookie.value,
            domain=cookie.domain or ".youtube.com",
            path=cookie.path or "/",
        )
    header = _cookie_header_from_jar(http.cookies)
    with _BROWSER_CACHE_LOCK:
        _BROWSER_CACHE[name] = (now, header or "", http)
    return header, http


def strengthen_youtube_session(
    session: "YouTubeSession",
    video_id: Optional[str],
    *,
    auto_auth: bool = True,
    fetch_pot: Optional[bool] = None,
) -> "YouTubeSession":
    """Upgrade session with explicit browser cookies before retry."""
    del video_id, fetch_pot
    from services.youtube_session import YouTubeSession

    browser = session.cookies_from_browser
    cookie_header = session.cookie_header
    http_session = session.http_session
    if browser and not cookie_header:
        try:
            cookie_header, http_session = browser_cookie_session(browser)
        except Exception as exc:
            logger.debug("browser_cookie_session(%s) failed: %s", browser, exc)

    if (
        cookie_header == session.cookie_header
        and http_session is session.http_session
    ):
        return session

    return dataclasses.replace(
        session,
        cookie_header=cookie_header or session.cookie_header,
        http_session=http_session or session.http_session,
        anonymous=False,
    )


def auth_status(auto_auth: bool, cookies_from_browser: str = "") -> dict:
    """Snapshot for Settings UI / diagnostics."""
    browser = resolve_youtube_browser(cookies_from_browser, auto_auth)
    return {
        "auto_auth": auto_auth,
        "browser": browser,
        "pot_providers": [],
        "pot_auto_available": False,
    }


assert resolve_youtube_browser("chrome", True) == "chrome"
assert pot_minting_enabled() is False
