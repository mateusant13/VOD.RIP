"""Session-scoped HLS proxy for in-browser VOD trim preview (no ffmpeg)."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from services.ytdlp_service import (
    _build_ydl_opts,
    _extract_hls_info,
    _find_hls_format,
    build_url,
    detect_platform,
)

logger = logging.getLogger(__name__)

SESSION_TTL_SEC = 30 * 60
PLAYLIST_REWRITE_TTL_SEC = 20 * 60
PREWARM_SEGMENT_COUNT = 3
MAX_SEGMENT_BYTES = 100 * 1024 * 1024
SESSION_CACHE_MAX_BYTES = 100 * 1024 * 1024
_PREVIEW_ROOT = Path(os.environ.get("TEMP", os.environ.get("TMP", "/tmp"))) / "kd_preview"
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_ALLOWED_HOST_SUFFIXES = (
    "kick.com",
    "twitch.tv",
    "ttvnw.net",
    "jtvnw.net",
    "cloudfront.net",
    "amazonaws.com",
    "akamaized.net",
    "fastly.net",
    "llnwi.net",
    "edgecastcdn.net",
)

_URI_IN_TAG = re.compile(r'URI="([^"]+)"')
_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)")
_RESOLUTION_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)")

_lock = threading.Lock()
_sessions: Dict[str, "PreviewSession"] = {}


@dataclass
class PreviewSession:
    session_id: str
    vod_url: str
    master_url: str
    entry_url: str
    platform: str
    http_headers: Dict[str, str] = field(default_factory=dict)
    allowed_hosts: Set[str] = field(default_factory=set)
    resource_map: Dict[str, str] = field(default_factory=dict)
    rewritten_playlists: Dict[str, Tuple[bytes, float]] = field(default_factory=dict)
    custom_master: Optional[str] = None
    variant_entries: List[Tuple[int, str]] = field(default_factory=list)
    cache_bytes: int = 0
    last_access: float = field(default_factory=time.time)
    cache_dir: Path = field(default_factory=Path)

    def touch(self) -> None:
        self.last_access = time.time()


def _hosts_for_url(url: str) -> Set[str]:
    host = urlparse(url).hostname
    return {host} if host else set()


def _host_allowed(host: str, session: PreviewSession) -> bool:
    if not host:
        return False
    if host in session.allowed_hosts:
        return True
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in _ALLOWED_HOST_SUFFIXES)


def _request_headers(session: PreviewSession, range_header: Optional[str] = None) -> dict:
    headers = dict(session.http_headers)
    headers.setdefault("User-Agent", _DEFAULT_UA)
    if range_header:
        headers["Range"] = range_header
    return headers


def _is_playlist_url(url: str) -> bool:
    return ".m3u8" in urlparse(url).path.lower()


def _guess_content_type(url: str, header_ct: str = "") -> str:
    if header_ct and header_ct != "application/octet-stream":
        return header_ct
    path = urlparse(url).path.lower()
    if path.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if path.endswith((".ts", ".mpeg")):
        return "video/mp2t"
    if path.endswith(".m4s"):
        return "video/iso.segment"
    if path.endswith(".mp4"):
        return "video/mp4"
    return "application/octet-stream"


def _http_get_bytes(
    session: PreviewSession,
    url: str,
    range_header: Optional[str] = None,
) -> Tuple[bytes, str, dict, int]:
    """Fetch upstream bytes. curl_cffi must use stream=False or .content is empty."""
    host = urlparse(url).hostname or ""
    if not _host_allowed(host, session):
        raise PermissionError(f"URL host not allowed for preview: {host}")

    headers = _request_headers(session, range_header)
    try:
        from curl_cffi import requests as cffi_requests

        resp = cffi_requests.get(
            url,
            headers=headers,
            impersonate="chrome",
            stream=False,
            timeout=60,
        )
    except ImportError:
        import requests

        resp = requests.get(url, headers=headers, timeout=60)

    resp.raise_for_status()
    data = resp.content or b""
    ctype = _guess_content_type(url, resp.headers.get("Content-Type", ""))
    out_headers: dict = {"Accept-Ranges": "bytes"}
    for key in ("Content-Range", "Content-Length"):
        if key in resp.headers:
            out_headers[key] = resp.headers[key]
    if not out_headers.get("Content-Length") and data:
        out_headers["Content-Length"] = str(len(data))
    session.touch()
    return data, ctype, out_headers, resp.status_code


def _deduped_hls_variants(info: dict) -> List[dict]:
    formats = info.get("formats") or []
    hls = [
        f for f in formats
        if f.get("protocol") in ("m3u8", "m3u8_native", "m3u8_ffmpeg") and f.get("url")
    ]
    hls.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
    seen_heights: set[int] = set()
    out: List[dict] = []
    for fmt in hls:
        height = int(fmt.get("height") or 0)
        if height and height in seen_heights:
            continue
        if height:
            seen_heights.add(height)
        out.append(fmt)
    return out


def _url_looks_like_master(url: str) -> bool:
    lower = url.lower()
    return "master" in lower or "multivariant" in lower


def _pick_variant_by_height(entries: List[Tuple[int, str]], prefer_height: int) -> Optional[str]:
    if not entries:
        return None
    by_height = sorted(entries, key=lambda t: t[0])
    for height, url in by_height:
        if height == prefer_height:
            return url
    at_or_below = [entry for entry in by_height if entry[0] and entry[0] <= prefer_height]
    if at_or_below:
        return at_or_below[-1][1]
    return by_height[0][1]


def _build_synthetic_master_playlist(session: PreviewSession, variants: List[dict]) -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-INDEPENDENT-SEGMENTS"]
    for fmt in variants:
        height = int(fmt.get("height") or 0)
        if not height:
            continue
        width = int(fmt.get("width") or 0)
        if not width:
            width = int(height * 16 / 9)
        bandwidth = int((fmt.get("tbr") or 0) * 1000) or int((fmt.get("vbr") or 0) * 1000) or 1_000_000
        upstream = fmt.get("url") or ""
        session.allowed_hosts.update(_hosts_for_url(upstream))
        lines.append(f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={width}x{height}")
        lines.append(_proxy_url(session, upstream))
    return "\n".join(lines) + "\n"


def resolve_stream_info(url: str, oauth: Optional[str] = None) -> Tuple[str, dict, str, List[dict]]:
    platform = detect_platform(url)
    headers: dict = {}

    if platform == "Kick":
        from services.kick_api_service import get_video_info_api

        info = get_video_info_api(url)
        if not info.m3u8_url:
            raise RuntimeError("Kick VOD has no HLS stream URL")
        headers = {
            "referer": "https://kick.com/",
            "origin": "https://kick.com",
        }
        return info.m3u8_url, headers, platform, []

    full_url = build_url(url, platform)
    opts = _build_ydl_opts(full_url, os.devnull, oauth=oauth)
    hls_info = _extract_hls_info(full_url, opts)
    variants = _deduped_hls_variants(hls_info)
    with_height = [fmt for fmt in variants if int(fmt.get("height") or 0) > 0]

    if len(with_height) >= 2:
        for fmt in variants:
            stream_url = fmt.get("url") or ""
            if stream_url and _url_looks_like_master(stream_url):
                headers = fmt.get("http_headers") or hls_info.get("http_headers") or {}
                return stream_url, headers, platform, []

        first = with_height[0]
        stream_url = first.get("url") or ""
        if not stream_url:
            raise RuntimeError("Twitch VOD has no HLS stream URL")
        headers = first.get("http_headers") or hls_info.get("http_headers") or {}
        return stream_url, headers, platform, with_height

    fmt = _find_hls_format(hls_info)
    stream_url = fmt.get("url") or hls_info.get("url") or ""
    if not stream_url:
        raise RuntimeError("Twitch VOD has no HLS stream URL")
    headers = fmt.get("http_headers") or hls_info.get("http_headers") or {}
    return stream_url, headers, platform, []


def _pick_preview_variant(master_text: str, master_url: str, prefer_height: int = 480) -> Optional[str]:
    """Pick a preview variant (default ~480p) for faster startup and prewarm."""
    lines = master_text.splitlines()
    variants: list[tuple[int, int, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            bw_m = _BANDWIDTH_RE.search(line)
            res_m = _RESOLUTION_RE.search(line)
            bw = int(bw_m.group(1)) if bw_m else 0
            height = int(res_m.group(2)) if res_m else 0
            if i + 1 < len(lines):
                nxt = lines[i + 1].strip()
                if nxt and not nxt.startswith("#"):
                    variants.append((height, bw, nxt))
                    i += 2
                    continue
        i += 1

    if not variants:
        return None

    for height, _bw, path in variants:
        if height == prefer_height:
            return urljoin(master_url, path)

    at_or_below = [v for v in variants if v[0] and v[0] <= prefer_height]
    if at_or_below:
        at_or_below.sort(key=lambda t: t[0], reverse=True)
        return urljoin(master_url, at_or_below[0][2])

    with_height = [v for v in variants if v[0]]
    if with_height:
        with_height.sort(key=lambda t: t[0])
        return urljoin(master_url, with_height[0][2])

    variants.sort(key=lambda t: t[1])
    return urljoin(master_url, variants[0][2])


def _resolve_preview_entry(session: PreviewSession, entry_url: str, prefer_height: int = 480) -> str:
    """Follow master playlist to a single media playlist for prewarm."""
    if session.variant_entries:
        picked = _pick_variant_by_height(session.variant_entries, prefer_height)
        if picked:
            return picked

    data, _, _, _ = _http_get_bytes(session, entry_url)
    if not data or not data.lstrip().startswith(b"#EXTM3U"):
        raise RuntimeError("Upstream returned an empty or invalid HLS playlist")

    text = data.decode("utf-8", errors="replace")
    if "#EXT-X-STREAM-INF" not in text:
        return entry_url

    variant_url = _pick_preview_variant(text, entry_url, prefer_height)
    if not variant_url:
        return entry_url

    logger.info("Preview resolved %s -> %s", entry_url[:80], variant_url[:80])
    return variant_url


def _resource_id(session: PreviewSession, upstream: str) -> str:
  # Stable per-URL id within session so playlist re-fetches stay consistent.
    digest = hashlib.sha256(upstream.encode()).hexdigest()[:16]
    session.resource_map[digest] = upstream
    return digest


def _proxy_url(session: PreviewSession, upstream: str) -> str:
    rid = _resource_id(session, upstream)
    return f"/api/preview/hls/{session.session_id}/resource?id={rid}"


def _parse_playlist_assets(text: str, playlist_url: str) -> Tuple[list[str], list[str]]:
    """Return (init/key URLs, media segment URLs in playlist order)."""
    base = playlist_url.rsplit("/", 1)[0] + "/"
    inits: list[str] = []
    segments: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if 'URI="' in stripped:
                m = _URI_IN_TAG.search(stripped)
                if m:
                    inits.append(urljoin(playlist_url, m.group(1)))
            continue
        segments.append(urljoin(base, stripped))
    return inits, segments


def _segment_index_for_time(text: str, target_sec: float) -> int:
    """Map a VOD timestamp to a segment index using EXTINF durations."""
    index = 0
    pos = 0.0
    pending_duration: Optional[float] = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXTINF:"):
            pending_duration = float(stripped.split(":")[1].split(",")[0])
        elif stripped and not stripped.startswith("#") and pending_duration is not None:
            if target_sec < pos + pending_duration or index == 0:
                return index
            pos += pending_duration
            index += 1
            pending_duration = None
    return max(0, index - 1)


def _rewrite_playlist(content: str, session: PreviewSession, playlist_url: str) -> str:
    base = playlist_url.rsplit("/", 1)[0] + "/"
    out: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if stripped.startswith("#"):
            if 'URI="' in stripped:
                def _sub(m: re.Match) -> str:
                    abs_url = urljoin(playlist_url, m.group(1))
                    return f'URI="{_proxy_url(session, abs_url)}"'
                out.append(_URI_IN_TAG.sub(_sub, line))
            else:
                out.append(line)
            continue
        abs_url = urljoin(base, stripped)
        out.append(_proxy_url(session, abs_url))
    return "\n".join(out) + "\n"


def _cache_path(session: PreviewSession, url: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:32]
    path = urlparse(url).path.lower()
    if path.endswith(".m4s"):
        ext = ".m4s"
    elif path.endswith(".mp4"):
        ext = ".mp4"
    elif _is_playlist_url(url):
        ext = ".m3u8"
    else:
        ext = ".ts"
    return session.cache_dir / f"{digest}{ext}"


def _evict_cache_if_needed(session: PreviewSession) -> None:
    if session.cache_bytes <= SESSION_CACHE_MAX_BYTES:
        return
    files: list[tuple[float, Path, int]] = []
    for entry in session.cache_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            st = entry.stat()
            files.append((st.st_atime, entry, st.st_size))
        except OSError:
            continue
    files.sort(key=lambda t: t[0])
    for _atime, path, size in files:
        if session.cache_bytes <= SESSION_CACHE_MAX_BYTES:
            break
        try:
            path.unlink()
            session.cache_bytes = max(0, session.cache_bytes - size)
        except OSError:
            pass


def _read_cache(session: PreviewSession, url: str) -> Optional[bytes]:
    path = _cache_path(session, url)
    if path.is_file():
        try:
            return path.read_bytes()
        except OSError:
            return None
    return None


def _write_cache(session: PreviewSession, url: str, data: bytes) -> None:
    if len(data) > MAX_SEGMENT_BYTES or _is_playlist_url(url):
        return
    path = _cache_path(session, url)
    try:
        path.write_bytes(data)
        session.cache_bytes += len(data)
        _evict_cache_if_needed(session)
    except OSError:
        pass


def _cleanup_stale_sessions() -> None:
    now = time.time()
    stale = [sid for sid, s in _sessions.items() if now - s.last_access > SESSION_TTL_SEC]
    for sid in stale:
        delete_session(sid)


def delete_session(session_id: str) -> bool:
    with _lock:
        session = _sessions.pop(session_id, None)
    if not session:
        return False
    try:
        import shutil
        if session.cache_dir.is_dir():
            shutil.rmtree(session.cache_dir, ignore_errors=True)
    except OSError:
        pass
    return True


def _prewarm_session(session_id: str, crop_start: float) -> None:
    """Background: cache rewritten playlist + segments near trim start."""
    try:
        session = get_session(session_id)
        if not session:
            return

        raw, _, _, _ = _http_get_bytes(session, session.entry_url)
        if not raw:
            return

        text = raw.decode("utf-8", errors="replace")
        inits, segments = _parse_playlist_assets(text, session.entry_url)
        targets: list[str] = list(dict.fromkeys(inits))

        if segments:
            idx = _segment_index_for_time(text, max(0.0, crop_start))
            idx = min(idx, len(segments) - 1)
            start = max(0, idx - 1)
            end = min(len(segments), start + PREWARM_SEGMENT_COUNT)
            targets.extend(segments[start:end])

        for upstream in targets:
            try:
                proxy_segment(session_id, upstream)
            except Exception as exc:
                logger.debug("Prewarm segment skipped %s: %s", upstream[:80], exc)

        logger.info(
            "Prewarm done session=%s segments=%d inits=%d at=%.1fs",
            session_id[:8],
            len(targets) - len(inits),
            len(inits),
            crop_start,
        )
    except Exception as exc:
        logger.warning("Prewarm failed session=%s: %s", session_id[:8], exc)


def create_session(
    url: str,
    crop_start: float = 0.0,
    crop_end: float = 0.0,
    oauth: Optional[str] = None,
    prefer_height: int = 480,
) -> PreviewSession:
    del crop_end
    _cleanup_stale_sessions()
    raw_entry, headers, platform, variant_formats = resolve_stream_info(url, oauth=oauth)
    session_id = secrets.token_hex(8)
    cache_dir = _PREVIEW_ROOT / session_id
    cache_dir.mkdir(parents=True, exist_ok=True)

    session = PreviewSession(
        session_id=session_id,
        vod_url=url,
        master_url=raw_entry,
        entry_url=raw_entry,
        platform=platform,
        http_headers=headers,
        allowed_hosts=_hosts_for_url(raw_entry),
        cache_dir=cache_dir,
    )

    if variant_formats:
        session.variant_entries = [
            (int(fmt.get("height") or 0), fmt.get("url") or "")
            for fmt in variant_formats
            if int(fmt.get("height") or 0) > 0 and fmt.get("url")
        ]
        session.custom_master = _build_synthetic_master_playlist(session, variant_formats)
        for _height, upstream in session.variant_entries:
            session.allowed_hosts.update(_hosts_for_url(upstream))

    session.entry_url = _resolve_preview_entry(session, raw_entry, prefer_height)
    session.allowed_hosts.update(_hosts_for_url(session.entry_url))

    with _lock:
        _sessions[session_id] = session

    # Warm master + default variant playlists so the first hls.js fetches are instant.
    try:
        proxy_playlist(session_id, session.master_url)
        if session.entry_url != session.master_url:
            proxy_playlist(session_id, session.entry_url)
    except Exception as exc:
        logger.warning("Playlist warm failed: %s", exc)

    threading.Thread(
        target=_prewarm_session,
        args=(session_id, crop_start),
        daemon=True,
        name=f"kd-prewarm-{session_id[:8]}",
    ).start()

    return session


def get_session(session_id: str) -> Optional[PreviewSession]:
    with _lock:
        session = _sessions.get(session_id)
    if session:
        session.touch()
    return session


def resolve_upstream(session_id: str, resource_id: Optional[str], raw_url: Optional[str]) -> str:
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")
    if resource_id:
        upstream = session.resource_map.get(resource_id)
        if not upstream:
            raise ValueError("Unknown preview resource")
        return upstream
    if raw_url and raw_url.startswith("http"):
        host = urlparse(raw_url).hostname or ""
        if not _host_allowed(host, session):
            raise PermissionError(f"URL host not allowed for preview: {host}")
        return raw_url
    raise ValueError("Missing preview resource id")


def get_master_playlist(session_id: str) -> str:
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")
    if session.custom_master:
        return session.custom_master
    body, _, _, _ = proxy_playlist(session_id, session.master_url)
    return body.decode("utf-8")


def proxy_playlist(session_id: str, upstream_url: str) -> Tuple[bytes, str, dict, int]:
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")

    now = time.time()
    cached = session.rewritten_playlists.get(upstream_url)
    if cached and now - cached[1] < PLAYLIST_REWRITE_TTL_SEC:
        return cached[0], "application/vnd.apple.mpegurl", {"Cache-Control": "no-cache"}, 200

    data, _, _, status = _http_get_bytes(session, upstream_url)

    if not data:
        raise RuntimeError("Upstream playlist is empty")

    if data.lstrip().startswith(b"#EXTM3U") or _is_playlist_url(upstream_url):
        text = data.decode("utf-8", errors="replace")
        rewritten = _rewrite_playlist(text, session, upstream_url)
        data = rewritten.encode("utf-8")
        session.rewritten_playlists[upstream_url] = (data, now)
        return data, "application/vnd.apple.mpegurl", {"Cache-Control": "no-cache"}, status

    ctype = _guess_content_type(upstream_url)
    return data, ctype, {"Cache-Control": "no-cache"}, status


def proxy_segment(
    session_id: str,
    upstream_url: str,
    range_header: Optional[str] = None,
) -> Tuple[bytes, str, dict, int]:
    """Fetch a segment/key/init file (buffered — typical HLS segments are a few MB)."""
    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")

    if range_header is None:
        cached = _read_cache(session, upstream_url)
        if cached is not None:
            ctype = _guess_content_type(upstream_url)
            return cached, ctype, {
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(cached)),
                "Cache-Control": "public, max-age=3600",
            }, 200

    data, ctype, headers, status = _http_get_bytes(session, upstream_url, range_header=range_header)
    if len(data) > MAX_SEGMENT_BYTES:
        raise RuntimeError("Preview segment exceeds size limit")

    if range_header is None and data and not _is_playlist_url(upstream_url):
        _write_cache(session, upstream_url, data)
        headers["Cache-Control"] = "public, max-age=3600"

    return data, ctype, headers, status
