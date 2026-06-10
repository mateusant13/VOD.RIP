"""Kick Playwright service — uses a headless browser to fetch Kick info and download VODs.

Kick's API is behind Cloudflare with TLS fingerprinting / JS challenges that
block direct HTTP clients and even Playwright's own API calls. However, the
page itself is server-side rendered: the full VOD catalog is embedded in the
Next.js hydration payload (`self.__next_f.push([1, ...])`). We load the page
in a real Chromium browser (via Playwright), parse the embedded JSON, and
extract the m3u8 playlist URL for downloading.

This replaces the dead `kickvodinfo.twitcharchives.workers.dev` API used by
the original `lay295/KickDownloader` project.
"""

import asyncio
import json
import os
import re
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from playwright.async_api import Browser, Page, Response, async_playwright
# ---------------------------------------------------------------------------
# Headless-shell pinning
# ---------------------------------------------------------------------------
#
# The user's real browser is `chrome.exe`; Playwright ships a separate
# headless binary at `chrome-headless-shell.exe` (under
# `%LOCALAPPDATA%\ms-playwright\chromium_headless_shell-*\...`).
# We MUST only ever launch the headless shell, and any cleanup of
# leftover processes MUST target `chrome-headless-shell.exe` by name —
# never the generic `chrome.exe`, which is the user's browser.
#
# A blanket `taskkill /IM chrome.exe` in a previous turn also killed
# the user's real Chrome. That command is not used anywhere in this
# module; the helper below uses the headless shell's exact image name
# and additionally scopes to processes whose parent is `python.exe`
# (i.e. spawned by us, not by the user clicking a desktop shortcut).
# Image name of Playwright's headless Chromium. Use this constant
# everywhere a process lookup happens.
HEADLESS_BROWSER_IMAGE = "chrome-headless-shell.exe"
def _find_headless_shell_executable() -> Optional[str]:
    """Return the absolute path to Playwright's headless-shell binary.
    Searches `%LOCALAPPDATA%\\ms-playwright\\chromium_headless_shell-*\\\
chrome-headless-shell-*\\chrome-headless-shell.exe` and returns the most
    recently modified match. Returns `None` when Playwright's browser
    isn't installed — the caller falls back to Playwright's default
    lookup in that case (which still uses the same headless shell, but
    we can't pin to a specific path for targeted cleanup).
    """
    local_app = os.environ.get("LOCALAPPDATA")
    if not local_app:
        return None
    base = Path(local_app) / "ms-playwright"
    try:
        candidates = sorted(
            base.glob(f"chromium_headless_shell-*/chrome-headless-shell-*/{HEADLESS_BROWSER_IMAGE}"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return None
    return str(candidates[0]) if candidates else None
def _kill_leftover_headless_shells() -> int:
    """Force-kill any `chrome-headless-shell.exe` processes we own.
    Targets ONLY `chrome-headless-shell.exe` — never `chrome.exe`. Use
    this at shutdown or after a failed call to make sure the user's
    Chrome isn't touched if a previous run leaked a headless shell.
    On non-Windows platforms the equivalent `pkill` invocation is
    used, still scoped to the headless-shell image name.
    Returns the number of processes killed.
    """
    import subprocess
    image = HEADLESS_BROWSER_IMAGE  # `chrome-headless-shell.exe`
    try:
        if os.name == "nt":
            # `taskkill /IM <image>` matches the exact image name. Using
            # any other name (e.g. `chrome.exe`) would risk killing the
            # user's real browser, so don't.
            out = subprocess.run(
                ["taskkill", "/F", "/T", "/IM", image],
                capture_output=True, text=True, timeout=10,
            )
            # Parse the count of "ÊXITO" / "SUCCESS" lines.
            return sum(
                1 for line in (out.stdout + out.stderr).splitlines()
                if "ÊXITO" in line or "SUCCESS" in line or "INFO" in line
            )
        else:
            out = subprocess.run(
                ["pkill", "-9", "-f", image],
                capture_output=True, text=True, timeout=10,
            )
            return out.returncode == 0 and 1 or 0
    except Exception:
        return 0
# ---------------------------------------------------------------------------


@dataclass
class KickVideo:
    """Minimal Kick VOD metadata parsed from the page's hydration data."""

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
    """Minimal Kick channel metadata."""

    slug: str
    username: Optional[str] = None
    channel_id: Optional[int] = None
    followers: Optional[int] = None
    is_live: bool = False
    live_title: Optional[str] = None


# ---------------------------------------------------------------------------
# Lightweight browser lifecycle
# ---------------------------------------------------------------------------
#
# Each call gets its own headless Chromium. The browser is killed as soon
# as the call's async context exits, AND has a hard 30-second lifetime
# cap so a stuck call can't keep a Chromium process alive forever.
#
# The old design pooled one browser across requests. That leaked
# resources (the pool could hold the browser open indefinitely if a
# call raised mid-acquire) and didn't bound lifetime at all. Per-call
# browsers are slightly slower on cold starts (~200 ms) but always
# release resources promptly, and the cost is dwarfed by the network
# time we already spend on the page itself.
# How long a browser may live before we force-kill it, regardless of
# whether the call has finished. Matches the user-facing 30 s budget.
BROWSER_MAX_LIFETIME_SEC = 30.0
# Chromium args applied to every browser. Tuned for "scrape a page's
# hydration data, then die" — we don't need images, fonts, GPU, or any
# of the heavyweight subsystems that Chrome spins up by default. Kept
# narrow so the browser actually launches on the headless-shell binary
# Playwright uses (some flags — `--headless=new`, `--single-process` —
# conflict with it and cause an immediate exit).
_LIGHTWEIGHT_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    # Smaller working set: skip GPU, dev-shm, and the renderer pool.
    "--disable-gpu",
    "--disable-dev-shm-usage",
    # Don't run any of the heavy subsystems we never use.
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-domain-reliability",
    "--disable-features=AudioServiceOutOfProcess,BackForwardCache,InterestFeedContentSuggestions,Translate,MediaRouter",
    "--disable-hang-monitor",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-renderer-backgrounding",
    "--disable-sync",
    # Cap V8's old-space so a stuck page doesn't balloon the renderer.
    "--js-flags=--max-old-space-size=128",
]
class _BoundedBrowser:
    """One-shot browser with a hard 30 s lifetime cap.
    Spawns Chromium on `__aenter__`, kills it on `__aexit__` *and* runs
    a watchdog that force-kills the process if it lives longer than
    `BROWSER_MAX_LIFETIME_SEC`. Both paths run the same teardown, so
    the only state we leak is the underlying OS process, and that's
    gone the moment the call ends (or the watchdog fires).
    """
    def __init__(self) -> None:
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._spawned_at: float = 0.0
        self._watchdog_task: Optional[asyncio.Task] = None
        self._closed = False
    async def __aenter__(self) -> Browser:
        self._playwright = await async_playwright().start()
        # Pin the executable path so the launched process is
        # `chrome-headless-shell.exe`, never the user's `chrome.exe`.
        # `_find_headless_shell_executable()` returns None if the
        # Playwright browser isn't installed in the standard location,
        # in which case we let Playwright pick — the default is still
        # the headless shell, but we lose the path for cleanup.
        executable_path = _find_headless_shell_executable()
        launch_kwargs: Dict[str, Any] = dict(
            headless=True,
            args=_LIGHTWEIGHT_CHROMIUM_ARGS,
        )
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        self._spawned_at = time.monotonic()
        self._watchdog_task = asyncio.create_task(self._watchdog())
        return self._browser
    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._close()
    async def _watchdog(self) -> None:
        """Force-kill the browser if it lives past the lifetime cap."""
        try:
            await asyncio.sleep(BROWSER_MAX_LIFETIME_SEC)
            if not self._closed:
                # Don't raise — closing errors here would just mask
                # the caller's exception. The teardown path runs below.
                await self._close()
        except asyncio.CancelledError:
            pass
    async def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                # Browser may already be in a bad state. Fall through
                # to a hard process kill via playwright.stop().
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
@asynccontextmanager
async def bounded_browser():
    """Async context manager that yields a fresh, lifetime-capped browser.
    Usage:
        async with bounded_browser() as browser:
            ctx = await browser.new_context(...)
            ...
        # browser is closed here, watchdog cancelled, playwright stopped.
    The viewport / user agent are set on the *context* (not the
    browser) so the process itself stays as small as possible; a 800x600
    viewport is plenty for the Next.js hydration scrape we do.
    """
    async with _BoundedBrowser() as browser:
        yield browser
# Default context options for our scrape. Smaller than the defaults so
# the per-page heap stays small; the user agent is a recent Chrome
# (Cloudflare is suspicious of anything older).
_DEFAULT_CONTEXT_OPTS: Dict[str, Any] = {
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "viewport": {"width": 800, "height": 600},
    "locale": "en-US",
    # Don't run the service worker or store any state — this is a
    # one-shot scrape, not a logged-in session.
    "service_workers": "block",
    # Block images / fonts / media so the page doesn't waste time or
    # bandwidth on assets we won't render. The Next.js hydration data
    # we need is in HTML, not the DOM tree after resource fetch.
    "bypass_csp": False,
}
@asynccontextmanager
async def lightweight_context():
    """Yield a new BrowserContext with our lightweight defaults.
    Bundles `bounded_browser()` and `browser.new_context(...)` so the
    call sites stay short and the lifecycle stays bounded.
    """
    async with bounded_browser() as browser:
        ctx = await browser.new_context(**_DEFAULT_CONTEXT_OPTS)
        try:
            yield ctx
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
# Asset types we never need. The route handler aborts these requests
# before they hit the network, which dramatically cuts page weight
# (Kick's HTML alone is ~30 KB; without blocking images/fonts, a
# typical VOD page pulls in 5-10 MB of assets we throw away).
_BLOCKED_RESOURCE_TYPES = frozenset({
    "image", "imageset", "font", "media", "stylesheet",
    "ping", "beacon", "csp_report", "object", "websocket",
})
async def _block_heavy_assets(ctx) -> None:
    """Install a route handler that aborts every asset we don't render.
    Kept tiny on purpose: a single closure, registered once per
    context, no extra dependencies.
    """
    async def _route(route, request):
        try:
            if request.resource_type in _BLOCKED_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            # Don't let a stray abort handler take down the whole page.
            try:
                await route.continue_()
            except Exception:
                pass
    try:
        await ctx.route("**/*", _route)
    except Exception:
        # Older playwright builds may not support ctx.route for the
        pass
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_duration(value: Any) -> Optional[float]:
    """Accept seconds (int/float) or an H:MM:SS string; return seconds or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.isdigit():
            return float(s)
        parts = s.split(":")
        try:
            parts = [float(p) for p in parts]
        except ValueError:
            return None
        seconds = 0.0
        for p in parts:
            seconds = seconds * 60 + p
        return seconds
    return None


def _format_duration(seconds: Optional[float]) -> Optional[str]:
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


def _extract_slug(url: str) -> Optional[str]:
    """Return the channel slug from a Kick URL.
    Accepts both bare channel URLs (`https://kick.com/foo`) and VOD URLs
    (`https://kick.com/foo/videos/<uuid>`). Uses urlparse to avoid the
    classic regex bug of capturing `videos` from `kick.com/foo/videos`.
    """
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if "kick.com" not in (parsed.netloc or "").lower():
        return None
    path = (parsed.path or "").strip("/")
    if not path:
        return None
    # First path segment is the channel slug
    return path.split("/")[0] or None


def _extract_vod_id(url: str) -> Optional[str]:
    m = re.search(r"/videos/([\da-f]{8}-(?:[\da-f]{4}-){3}[\da-f]{12})", url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Next.js hydration data parser
# ---------------------------------------------------------------------------


def _parse_next_data(html: str) -> List[dict]:
    """Extract and decode all Next.js `__next_f.push` payloads from the page.
    Kick uses Next.js with React Server Components. The dehydrated state is
    streamed into the page as `self.__next_f.push([1, "...escaped JSON..."])`
    calls. Each payload has the format `MODULE_ID:JSON_DATA` where MODULE_ID
    is a numeric ID and JSON_DATA is a JSON string (often a list).
    We decode them into a flat list of objects for easy searching.
    """
    payloads: List[dict] = []
    pattern = re.compile(r"self\.__next_f\.push\(\[1,\"(.*?)\"\]\)", re.DOTALL)
    for match in pattern.finditer(html):
        raw = match.group(1)
        try:
            decoded = json.loads(f'"{raw}"')
        except (json.JSONDecodeError, ValueError):
            continue
        # Format: '55:[...]' or 'I[...]' or other Next.js RSC prefixes.
        # Strip the leading module ID if present.
        json_str = decoded
        module_match = re.match(r"^(\d+|I|T|H):(.*)", decoded, re.DOTALL)
        if module_match:
            json_str = module_match.group(2)
        try:
            obj = json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, list):
            payloads.extend(p for p in obj if isinstance(p, dict))
        elif isinstance(obj, dict):
            payloads.append(obj)
    return payloads
def _extract_vods_from_queries(payloads: List[dict]) -> List[dict]:
    """Walk all payloads and collect VOD objects from React Query state.
    Next.js with TanStack Query stores dehydrated state in payloads shaped like:
        ['$', '$Ld1', null, {"state": {"queries": [{"state": {"data": [VOD, ...]}}]}}]
    Each VOD object has fields like `session_title`, `source` (m3u8), `duration`,
    and a nested `video.uuid` (the 36-char video ID).
    """
    vods: List[dict] = []
    for p in payloads:
        if not isinstance(p, dict):
            continue
        state = p.get("state")
        if not isinstance(state, dict):
            continue
        queries = state.get("queries")
        if not isinstance(queries, list):
            continue
        for q in queries:
            if not isinstance(q, dict):
                continue
            qstate = q.get("state")
            if not isinstance(qstate, dict):
                continue
            data = qstate.get("data")
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and ("session_title" in item or "source" in item):
                        vods.append(item)
            elif isinstance(data, dict) and ("session_title" in data or "source" in data):
                vods.append(data)
    return vods


def _find_video_objects(payloads: List[dict], vod_id: Optional[str] = None) -> List[dict]:
    """Find VOD objects in the Next.js payloads.
    Looks for VOD objects in React Query state, at the top level, nested under
    `video`, or inside a `videos` array. If *vod_id* is given, returns only
    matching entries (by the nested `video.uuid`).
    """
    # First try the React Query state path (most common for Kick's /videos page)
    candidates = _extract_vods_from_queries(payloads)
    # Also check direct paths for individual VOD pages
    for p in payloads:
        if not isinstance(p, dict):
            continue
        # Top-level VOD object with uuid
        uuid = p.get("uuid")
        if isinstance(uuid, str) and re.match(
            r"^[\da-f]{8}-(?:[\da-f]{4}-){3}[\da-f]{12}$", uuid
        ):
            if vod_id is None or uuid == vod_id:
                candidates.append(p)
            continue
        # Sometimes wrapped in a 'video' field
        video = p.get("video")
        if isinstance(video, dict):
            uuid = video.get("uuid")
            if isinstance(uuid, str) and re.match(
                r"^[\da-f]{8}-(?:[\da-f]{4}-){3}[\da-f]{12}$", uuid
            ):
                if vod_id is None or uuid == vod_id:
                    candidates.append(video)
        # Or nested under 'videos' array
        videos = p.get("videos")
        if isinstance(videos, list):
            for v in videos:
                if not isinstance(v, dict):
                    continue
                inner = v.get("video") if isinstance(v.get("video"), dict) else v
                uuid = inner.get("uuid")
                if isinstance(uuid, str) and re.match(
                    r"^[\da-f]{8}-(?:[\da-f]{4}-){3}[\da-f]{12}$", uuid
                ):
                    if vod_id is None or uuid == vod_id:
                        candidates.append(inner)
    if vod_id is None:
        return candidates
    # Filter by vod_id
    out = []
    for v in candidates:
        video = v.get("video") if isinstance(v.get("video"), dict) else None
        uuid = (video.get("uuid") if video else None) or v.get("uuid")
        if uuid == vod_id:
            out.append(v)
    return out


def _find_m3u8_url(html: str) -> Optional[str]:
    """Find the best m3u8 URL in the page HTML.

    Prefer `master.m3u8` from `stream.kick.com` (Kick's own CDN) over the IVS
    playback URL, because the latter is signed and may expire.
    """
    urls = re.findall(r"https?://[^\s\"'\\]+\.m3u8[^\s\"'\\]*", html)
    if not urls:
        return None
    # Prefer master.m3u8
    for u in urls:
        if "master.m3u8" in u and "stream.kick.com" in u:
            return u.rstrip("\\")
    for u in urls:
        if "master.m3u8" in u:
            return u.rstrip("\\")
    for u in urls:
        if "stream.kick.com" in u:
            return u.rstrip("\\")
    return urls[0].rstrip("\\")


# ---------------------------------------------------------------------------
# VOD builder
# ---------------------------------------------------------------------------


def _build_video_from_next_data(
    vod_obj: dict, payloads: List[dict], html: str, slug: Optional[str]
) -> KickVideo:
    """Build a KickVideo from a VOD object found in the Next.js payloads.
    Two structures are supported:
    1. /videos page (livestream-level object):
        {
            "id": <livestream_id>,
            "session_title": "...",
            "source": "https://stream.kick.com/.../master.m3u8",
            "duration": <milliseconds>,
            "thumbnail": {"src": "...", "srcset": "..."},
            "views": <count>,
            "created_at": "...",
            "video": {"uuid": "<36-char-id>", ...},
            "categories": [{"name": "..."}]
        }
    2. /videos/<uuid> page (video-level object with nested livestream):
        {
            "id": <video_id>,
            "uuid": "<36-char-id>",
            "views": <count>,
            "source": "https://stream.kick.com/.../master.m3u8",
            "livestream": {
                "session_title": "...",
                "duration": <milliseconds>,
                "thumbnail": "...",
                "channel": {"slug": "...", "user": {"username": "..."}},
                "categories": [{"name": "..."}]
            }
        }
    """
    # Detect structure: does the object have a nested 'video' field (type 1)
    # or a nested 'livestream' field (type 2)?
    video_obj = vod_obj.get("video") if isinstance(vod_obj.get("video"), dict) else None
    livestream = vod_obj.get("livestream") if isinstance(vod_obj.get("livestream"), dict) else None
    # Video UUID (the 36-char ID used in URLs)
    if video_obj and video_obj.get("uuid"):
        video_id = video_obj["uuid"]
    else:
        video_id = vod_obj.get("uuid", "")
    # Title
    title = (
        vod_obj.get("session_title")
        or (livestream.get("session_title") if livestream else None)
        or vod_obj.get("title")
        or "Untitled"
    )
    # Duration: Kick stores it in milliseconds
    duration: Optional[float] = None
    d = vod_obj.get("duration")
    if d is None and livestream:
        d = livestream.get("duration")
    if d is not None:
        try:
            duration = float(d) / 1000.0
        except (TypeError, ValueError):
            pass
    if duration is None:
        duration = _parse_duration(vod_obj.get("duration"))
    # Thumbnail
    thumbnail = None
    images = vod_obj.get("thumbnail")
    if isinstance(images, dict) and images.get("src"):
        thumbnail = images["src"]
    elif isinstance(images, str):
        thumbnail = images
    if not thumbnail and livestream:
        ls_thumb = livestream.get("thumbnail")
        if isinstance(ls_thumb, str):
            thumbnail = ls_thumb
        elif isinstance(ls_thumb, dict):
            thumbnail = ls_thumb.get("url") or ls_thumb.get("src")
    # Views: prefer the video-level views (type 2), fall back to livestream views
    views = vod_obj.get("views")
    if not isinstance(views, (int, float)):
        if video_obj:
            views = video_obj.get("views")
        if not isinstance(views, (int, float)) and livestream:
            views = livestream.get("viewer_count")
    if not isinstance(views, (int, float)):
        views = None
    # Created at
    created_at = vod_obj.get("created_at") or vod_obj.get("start_time")
    if not created_at and livestream:
        created_at = livestream.get("created_at") or livestream.get("start_time")
    # Channel: from livestream.channel.slug or livestream.channel.user.username
    channel = slug
    if livestream:
        ls_channel = livestream.get("channel")
        if isinstance(ls_channel, dict):
            channel = ls_channel.get("slug") or slug
            user = ls_channel.get("user")
            if isinstance(user, dict) and user.get("username"):
                channel = user["username"]
    if not channel and video_obj:
        v_channel = video_obj.get("channel")
        if isinstance(v_channel, dict):
            channel = v_channel.get("slug") or slug
    # Category
    category = None
    cats = vod_obj.get("categories")
    if not isinstance(cats, list) and livestream:
        cats = livestream.get("categories")
    if isinstance(cats, list) and cats:
        first = cats[0]
        if isinstance(first, dict):
            category = first.get("name")
    # m3u8 URL: prefer the `source` field, fall back to scanning the HTML
    m3u8 = vod_obj.get("source")
    if livestream and (not m3u8 or ".m3u8" not in m3u8):
        m3u8 = livestream.get("source")
    if not m3u8 or ".m3u8" not in m3u8:
        m3u8 = _find_m3u8_url(html)
    url = f"https://kick.com/{channel}/videos/{video_id}" if channel and video_id else None
    return KickVideo(
        id=video_id,
        title=str(title) if title else "Untitled",
        duration=duration,
        thumbnail=thumbnail,
        views=views,
        created_at=created_at,
        channel=channel,
        url=url,
        category=category,
        m3u8_url=m3u8,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class KickPlaywrightService:
    """Headless-browser service for fetching Kick info and HLS streams."""

    def __init__(self) -> None:
        # No long-lived pool. Each call gets its own bounded browser so
        # resources are released promptly and nothing lives past 30 s.
        pass
    # ----- Channel -----

    async def get_channel(self, url: str) -> KickChannel:
        """Return channel metadata via Kick API (~1s)."""
        from services.kick_api_service import get_channel_api

        return await asyncio.to_thread(get_channel_api, url)

    async def _get_channel_browser(self, url: str) -> KickChannel:
        """Legacy browser fallback (unused on hot path)."""
        slug = _extract_slug(url)
        if not slug:
            raise ValueError(f"Not a Kick channel URL: {url}")
        async with lightweight_context() as ctx:
            # Abort any request for an asset we'll never render. Cuts
            # both bandwidth and the per-page memory footprint.
            await _block_heavy_assets(ctx)
            page = await ctx.new_page()
            try:
                await page.goto(
                    f"https://kick.com/{slug}",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                await page.wait_for_timeout(5000)
                html = await page.content()
            finally:
                await page.close()

        payloads = _parse_next_data(html)
        # Find channel info in payloads
        channel = KickChannel(slug=slug)
        for p in payloads:
            if p.get("slug") == slug and "id" in p and "user" in p:
                channel.username = p.get("user", {}).get("username") or slug
                channel.channel_id = p.get("id")
                channel.followers = p.get("followersCount") or p.get("followers_count")
                ls = p.get("livestream")
                if isinstance(ls, dict):
                    channel.is_live = True
                    channel.live_title = ls.get("session_title") or ls.get("slug")
                break
        return channel

    # ----- Channel videos listing -----
    async def list_channel_videos(self, url: str, limit: int = 20) -> List[KickVideo]:
        """List channel VODs via Kick API (~1-2s)."""
        from services.kick_api_service import list_channel_videos_api

        slug = _extract_slug(url)
        if not slug:
            raise ValueError(f"Not a Kick channel URL: {url}")
        return await asyncio.to_thread(list_channel_videos_api, slug, limit)

    async def _list_channel_videos_browser(self, url: str, limit: int = 20) -> List[KickVideo]:
        """Legacy browser fallback (unused on hot path)."""
        slug = _extract_slug(url)
        if not slug:
            raise ValueError(f"Not a Kick channel URL: {url}")
        for attempt in range(3):
            async with lightweight_context() as ctx:
                await _block_heavy_assets(ctx)
                page = await ctx.new_page()
                try:
                    await page.goto(
                        f"https://kick.com/{slug}/videos",
                        wait_until="domcontentloaded",
                        timeout=20000,
                    )
                    await page.wait_for_timeout(8000 + attempt * 4000)
                    html = await page.content()
                finally:
                    await page.close()
            payloads = _parse_next_data(html)
            vod_objects = _find_video_objects(payloads)
            if vod_objects:
                # De-duplicate by video ID, preserving order
                seen = set()
                unique = []
                for v in vod_objects:
                    video_obj = v.get("video") if isinstance(v.get("video"), dict) else None
                    vid = (video_obj.get("uuid") if video_obj else None) or v.get("uuid")
                    if vid and vid not in seen:
                        seen.add(vid)
                        unique.append(v)
                videos: List[KickVideo] = []
                for v in unique[:limit]:
                    videos.append(_build_video_from_next_data(v, payloads, html, slug))
                return videos
        # All attempts failed: return empty list rather than raising
        return []

    # ----- Single VOD info -----

    async def get_video_info(self, url: str) -> KickVideo:
        """Return VOD metadata + m3u8 via Kick API (~0.5s)."""
        from services.kick_api_service import get_video_info_api

        return await asyncio.to_thread(get_video_info_api, url)

    async def _get_video_info_browser(self, url: str) -> KickVideo:
        """Legacy browser fallback (unused on hot path)."""
        video_id = _extract_vod_id(url)
        if not video_id:
            raise ValueError(f"Not a Kick VOD URL: {url}")
        slug_m = re.search(r"kick\.com/([\w-]+)/videos/", url)
        slug = slug_m.group(1) if slug_m else None
        last_error: Optional[ValueError] = None
        for attempt in range(3):
            async with lightweight_context() as ctx:
                await _block_heavy_assets(ctx)
                page = await ctx.new_page()
                try:
                    await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=20000,
                    )
                    # Give the player a moment to bootstrap and request the
                    # playlist. Longer waits help when Cloudflare is slow.
                    await page.wait_for_timeout(10000 + attempt * 5000)
                    html = await page.content()
                finally:
                    await page.close()
            payloads = _parse_next_data(html)
            vod_objects = _find_video_objects(payloads, vod_id=video_id)
            if vod_objects:
                return _build_video_from_next_data(
                    vod_objects[0], payloads, html, slug
                )
            last_error = ValueError(
                f"Could not find VOD {video_id} in page hydration data "
                f"(attempt {attempt + 1}/3)"
            )
        raise last_error  # type: ignore[misc]

    # ----- Download -----
    async def download_vod(
        self,
        url: str,
        output_path: str,
        quality: Optional[str] = None,
        progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
        crop_start: Optional[float] = None,
        crop_end: Optional[float] = None,
    ) -> str:
        """Download a Kick VOD via HLS. m3u8 URL comes from the Kick API (~0.5s)."""
        from services.kick_api_service import get_video_info_api

        info = await asyncio.to_thread(get_video_info_api, url)
        if not info.m3u8_url:
            raise RuntimeError("Kick API returned no HLS playlist URL for this VOD")
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        # Locate ffmpeg: prefer full path detection (same logic as ytdlp_service).
        from services.ytdlp_service import _find_ffmpeg
        ffmpeg_dir = _find_ffmpeg()
        if ffmpeg_dir:
            ffmpeg_exe = os.path.join(
                ffmpeg_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
            )
            if not os.path.isfile(ffmpeg_exe):
                ffmpeg_exe = "ffmpeg"
        else:
            ffmpeg_exe = "ffmpeg"
        # Build FFmpeg command.
        # We use `-c copy` for speed (no re-encoding), which means we can't
        # apply video filters. Quality is selected by the HLS playlist the
        # browser captured; if the user asked for a specific height, we rely
        # on the master.m3u8 already pointing at the right variant.
        cmd = [ffmpeg_exe, "-y", "-loglevel", "error",
               "-reconnect", "1",
               "-reconnect_streamed", "1",
               "-reconnect_delay_max", "5"]
        if crop_start is not None:
            cmd += ["-ss", f"{crop_start}"]
        cmd += ["-i", info.m3u8_url]
        if crop_start is not None and crop_end is not None:
            # Use -t (duration) rather than -to (absolute timestamp) so the
            # cut length doesn't depend on ffmpeg's input-seek interpretation.
            cmd += ["-t", f"{max(0.0, crop_end - crop_start)}"]
        elif crop_start is not None:
            cmd += ["-t", "3600"]  # default 1h, same as DEFAULT_CLIP_SECONDS
        cmd += ["-c", "copy", str(out)]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        est_sec = 30.0
        if crop_start is not None and crop_end is not None:
            est_sec = max(5.0, float(crop_end - crop_start))
        elif info.duration:
            est_sec = max(5.0, float(info.duration))

        async def _tick_progress() -> None:
            if not progress_hook:
                return
            started = time.monotonic()
            while proc.returncode is None:
                elapsed = time.monotonic() - started
                pct = min(95, int(10 + (elapsed / est_sec) * 85))
                try:
                    progress_hook({"status": "downloading", "percent": pct})
                except Exception:
                    pass
                await asyncio.sleep(0.5)

        tick_task = asyncio.create_task(_tick_progress())
        try:
            if progress_hook:
                try:
                    progress_hook({"status": "downloading", "percent": 5})
                except Exception:
                    pass
            _, stderr = await proc.communicate()
        finally:
            tick_task.cancel()
            try:
                await tick_task
            except asyncio.CancelledError:
                pass
        if proc.returncode != 0:
            raise RuntimeError(
                f"FFmpeg failed (exit {proc.returncode}): {stderr.decode(errors='ignore')[:500]}"
            )
        if progress_hook:
            try:
                progress_hook({"status": "downloading", "percent": 100})
                progress_hook({"status": "finished"})
            except Exception:
                pass
        if not out.is_file():
            raise RuntimeError(
                f"Download finished but output file is missing at {out}"
            )
        return str(out)


# ---------------------------------------------------------------------------
# Module-level helpers for the existing service to call
# ---------------------------------------------------------------------------


def _run_async(coro: Any) -> Any:
    """Run an async coroutine to completion in a fresh event loop.
    Called from worker threads (FastAPI `run_in_executor`, download pool).
    Each call gets an isolated loop so Playwright objects never leak across
    invocations or onto uvicorn's main loop.
    On Windows, uvicorn --reload sets `WindowsSelectorEventLoopPolicy`
    process-wide. Selector loops cannot spawn subprocesses, so Playwright
    raises `NotImplementedError()` (empty message) when we used plain
    `asyncio.run()`. We therefore mint an explicit `ProactorEventLoop`
    here instead of relying on the process event-loop policy.
    """
    try:
        asyncio.set_event_loop(None)
    except Exception:
        pass
    if sys.platform == "win32":
        loop: asyncio.AbstractEventLoop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.run_until_complete(loop.shutdown_default_executor())
        except Exception:
            pass
        loop.close()
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass

# Public sync wrappers used by main.py / download_manager.
# Hot paths use the Kick JSON API (curl_cffi) — no browser, ~0.5-2s.
def get_channel_info_sync(url: str) -> Dict[str, Any]:
    from services.kick_api_service import get_channel_api

    ch = get_channel_api(url)
    return {
        "slug": ch.slug,
        "username": ch.username,
        "channel_id": ch.channel_id,
        "followers": ch.followers,
        "is_live": ch.is_live,
        "live_title": ch.live_title,
    }


def list_channel_videos_sync(url: str, limit: int = 20) -> List[Dict[str, Any]]:
    from services.kick_api_service import list_channel_videos_api

    slug = _extract_slug(url)
    if not slug:
        raise ValueError(f"Not a Kick channel URL: {url}")
    vids = list_channel_videos_api(slug, limit)
    return [
        {
            "id": v.id,
            "title": v.title,
            "url": v.url,
            "thumbnail": v.thumbnail,
            "duration": v.duration,
            "duration_string": _format_duration(v.duration),
            "created_at": v.created_at,
            "views": v.views,
            "channel": v.channel or slug,
        }
        for v in vids
    ]


def get_video_info_sync(url: str) -> Dict[str, Any]:
    from services.kick_api_service import get_video_info_api

    v = get_video_info_api(url)
    return {
        "id": v.id,
        "title": v.title,
        "uploader": v.channel,
        "channel": v.channel,
        "duration": v.duration,
        "duration_string": _format_duration(v.duration),
        "thumbnail": v.thumbnail,
        "views": v.views,
        "category": v.category,
        "webpage_url": v.url,
        "qualities": [],  # Populated after we have an m3u8
        "platform": "Kick",
        "created_at": v.created_at,
    }


def download_vod_sync(
    url: str,
    output_path: str,
    quality: Optional[str] = None,
    crop_start: Optional[float] = None,
    crop_end: Optional[float] = None,
    progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> str:
    svc = KickPlaywrightService()
    return _run_async(
        svc.download_vod(
            url,
            output_path,
            quality=quality,
            progress_hook=progress_hook,
            crop_start=crop_start,
            crop_end=crop_end,
        )
    )
