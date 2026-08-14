"""Instant-preview PREFETCH — first ~8s of the 5 newest VODs per (channel, platform).

The archive scheduler kicks a background pass (``kick_prefetch_pass``) that,
for every saved channel x platform, takes the 5 most recent VODs (from the
archive ``videos`` table — the same rows the ingest legs wrote) and resolves
each through the SAME preview machinery the browser uses
(``resolve_stream_info`` + preview-session helpers), then stores the RAW
first segments of the default preview variant (720p twitch/kick, 360p
youtube) under ``<data_dir>/prefetch/<platform>/<video_id>/``.

Serve side (in ``services/preview/hls.py``):

* ``proxy_segment`` consults ``lookup_prefetched_segment`` first — a segment
  whose exact upstream URL was prefetched is served from disk byte-identical
  to what upstream would have returned. Everything past the prefix (or any
  URL never prefetched) falls through to the normal upstream path.
* ``proxy_playlist`` consults ``lookup_prefetched_playlist`` — the media
  playlist text of COMPLETED VODs (has #EXT-X-ENDLIST) is stored verbatim and
  re-rewritten per session. A multi-hour VOD's playlist is 0.5-2MB, the
  dominant first-play cost once the segments are cached.

Storage format = raw bytes (segments + init/key + the playlist text). No
ffmpeg, no A/V mux — byte-identical passthrough, zero sync risk by
construction. Verified against the live platforms: Twitch VOD variant/segment
URLs (cloudfront) and Kick m3u8 URLs are STABLE across resolves (only the
usher MASTER is per-session tokenized), so a URL-keyed cache hits across
sessions. YouTube googlevideo URLs are stable within the resolved-stream
cache window (1h) — the freshness gate matches that window.

Eviction: each pass keeps only the 5 most recent per (channel, platform)
(union across saved channels); anything else on that platform is dropped when
a newer VOD appears. A global byte budget bounds disk. Per-platform totals are
logged each pass.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from services import archive_db
from services.disk_hygiene import data_dir
from services.preview.hls import _http_get_bytes
from services.preview.session import (
    PreviewSession,
    _hosts_for_url,
    _is_playlist_url,
    _parse_playlist_assets,
    _resolve_preview_entry,
    _rewrite_playlist,
    resolve_stream_info,
)

logger = logging.getLogger(__name__)

PLATFORM_ORDER = ("twitch", "kick", "youtube")
_SLUG_KEYS = {"twitch": "twitchSlug", "kick": "kickSlug", "youtube": "youtubeSlug"}

PREFETCH_PER_PLATFORM = 5      # most recent VODs per (channel, platform)
PREFETCH_PREFIX_SEC = 8.0      # first-play seconds to cache
PREFETCH_MAX_SEGMENTS = 6      # hard cap on segments per VOD (segments can be 10s+)
PREFETCH_MAX_VIDEO_BYTES = 16 * 1024 * 1024  # per-VOD byte cap (1x 720p60 10s seg ≈ 4.3MB)
PREFETCH_FRESH_SEC = 3600.0    # re-fetch at most hourly — matches resolve-cache TTL
PREFETCH_FAILED_BACKOFF_SEC = 3600.0  # don't retry a failed VOD within an hour
PREFETCH_MAX_BYTES = 750 * 1024 * 1024  # global disk budget
PREFETCH_PER_PASS = 4          # fetch budget per scheduler pass (like other legs)
# The variant the frontend pins for the FIRST play (previewPlayerUtils:
# twitch/kick start at 720p, youtube fast-starts at 360p).
PREFETCH_HEIGHT_BY_PLATFORM = {"twitch": 720, "kick": 720, "youtube": 360}

_TWITCH_VOD_RE = re.compile(r"twitch\.tv/(?:[^/]+/)?videos/(\d+)", re.I)
_KICK_VOD_RE = re.compile(r"/videos/([\da-f]{8}-(?:[\da-f]{4}-){3}[\da-f]{12})", re.I)

_worker_thread: Optional[threading.Thread] = None
_worker_lock = threading.Lock()
_manifest_lock = threading.Lock()


def prefetch_root() -> Path:
    """Root of the prefetch cache — the shared data root (data_dir)."""
    return data_dir() / "prefetch"


def _video_dir(platform: str, video_id: str) -> Path:
    return prefetch_root() / platform / str(video_id)


def _segment_path(platform: str, video_id: str, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return _video_dir(platform, video_id) / f"seg_{digest}.bin"


def _playlist_path(platform: str, video_id: str, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return _video_dir(platform, video_id) / f"pl_{digest}.m3u8"


def _manifest_path(platform: str, video_id: str) -> Path:
    return _video_dir(platform, video_id) / "manifest.json"


def _load_manifest(platform: str, video_id: str) -> dict:
    try:
        return json.loads(_manifest_path(platform, video_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Serve path (imported by services/preview/hls.py)
# ---------------------------------------------------------------------------

def _platform_video_id(session: PreviewSession) -> Optional[Tuple[str, str]]:
    """(platform, video_id) of the session's VOD, or None when not prefetchable.

    Only VOD previews carry a stable video id in ``vod_url``; clips and live
    sessions (no vod_url / non-matching shape) return None and skip the cache.
    """
    url = (getattr(session, "vod_url", None) or "").strip()
    if not url:
        return None
    platform = (getattr(session, "platform", None) or "").strip().lower()
    if platform == "twitch":
        m = _TWITCH_VOD_RE.search(url)
        return ("twitch", m.group(1)) if m else None
    if platform == "kick":
        m = _KICK_VOD_RE.search(url)
        return ("kick", m.group(1)) if m else None
    if platform == "youtube":
        from services.youtube_innertube import extract_video_id

        vid = extract_video_id(url)
        return ("youtube", vid) if vid else None
    return None


def lookup_prefetched_segment(
    session: PreviewSession, upstream_url: str
) -> Optional[bytes]:
    """Raw bytes of a prefetched segment URL, or None (miss → upstream fetch).

    Called on the proxy hot path: one stat + one read. Exact-URL match only —
    the stored bytes are byte-identical to what upstream would have served.
    """
    if not upstream_url or _is_playlist_url(upstream_url):
        return None
    key = _platform_video_id(session)
    if not key:
        return None
    path = _segment_path(key[0], key[1], upstream_url)
    try:
        if path.is_file():
            return path.read_bytes()
    except OSError:
        return None
    return None


def lookup_prefetched_playlist(
    session: PreviewSession, upstream_url: str
) -> Optional[bytes]:
    """Rewritten media-playlist bytes for a prefetched COMPLETED VOD, else None.

    Only playlists of completed VODs are stored (immutable → no staleness), so
    a file hit is safe to serve; the per-session rewrite is what the normal
    upstream path would produce anyway.
    """
    if not upstream_url or not _is_playlist_url(upstream_url):
        return None
    key = _platform_video_id(session)
    if not key:
        return None
    path = _playlist_path(key[0], key[1], upstream_url)
    try:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        if "#EXT-X-ENDLIST" not in text:  # belt-and-suspenders: complete only
            return None
        rewritten = _rewrite_playlist(text, session, upstream_url)
    except Exception:
        return None
    if not rewritten.lstrip().startswith("#EXTM3U"):
        return None
    return rewritten.encode("utf-8")


# ---------------------------------------------------------------------------
# Fetch leg — reuse the preview session machinery directly (no session registry)
# ---------------------------------------------------------------------------

def _prefetch_url_for(platform: str, video_id: str, channel: dict) -> str:
    if platform == "twitch":
        return f"https://www.twitch.tv/videos/{video_id}"
    if platform == "kick":
        slug = (channel.get(_SLUG_KEYS["kick"]) or "").strip().lower()
        return f"https://kick.com/{slug}/videos/{video_id}"
    return f"https://www.youtube.com/watch?v={video_id}"


def _make_hls_session(url: str, raw_entry: str, headers: dict, platform: str, prefer_height: int) -> PreviewSession:
    """Throwaway (unregistered) session for the fetch leg — never in the registry."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
    return PreviewSession(
        session_id=f"prefetch-{platform}-{digest}",
        vod_url=url,
        master_url=raw_entry,
        entry_url=raw_entry,
        platform=platform,
        http_headers=headers,
        allowed_hosts=_hosts_for_url(raw_entry),
        cache_dir=prefetch_root(),  # unused by the fetch path (no _write_cache)
        kind="hls",
        crop_start=0.0,
        crop_end=PREFETCH_PREFIX_SEC,
        prefer_height=prefer_height,
    )


def _session_from_snapshot(snap: dict, url: str, prefer_height: int) -> PreviewSession:
    """Throwaway session rebuilt from a YouTube warm snapshot (mirror of
    PreviewManager._reuse_youtube_snapshot, without registering)."""
    session = PreviewSession(
        session_id=snap["session_id"],
        vod_url=url,
        master_url=snap["master_url"],
        entry_url=snap["entry_url"],
        platform=snap["platform"],
        http_headers=dict(snap.get("http_headers") or {}),
        allowed_hosts=set(snap.get("allowed_hosts") or set()),
        cache_dir=Path(snap.get("cache_dir") or prefetch_root()),
        kind=snap["kind"],
        crop_start=0.0,
        crop_end=PREFETCH_PREFIX_SEC,
        preview_audio_url=snap.get("preview_audio_url"),
        variant_entries=list(snap.get("variant_entries") or []),
        custom_master=snap.get("custom_master"),
        prefer_height=prefer_height,
    )
    if snap.get("dash_window_hls"):
        session.dash_window_hls = True
    return session


def _prefix_segment_count(text: str, target_sec: float, max_segments: int) -> int:
    """Leading segments covering [0, target_sec) — at least 1, capped.

    Local EXTINF walk (the session helper's ``_segment_index_for_time``
    short-circuits to 0 on the first segment, which is fine for prewarm's
    "always start at 0" intent but useless for prefix sizing).
    """
    count = 0
    pos = 0.0
    pending: Optional[float] = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXTINF:"):
            pending = float(stripped.split(":")[1].split(",")[0])
        elif stripped and not stripped.startswith("#") and pending is not None:
            count += 1
            pos += pending
            pending = None
            if pos >= target_sec or count >= max_segments:
                break
    return max(1, count)


def _prefetch_hls_leg(session: PreviewSession, platform: str, video_id: str, channel: dict) -> bool:
    """Fetch the session's media playlist + first segments and store them raw."""
    try:
        data, _, _, _ = _http_get_bytes(session, session.entry_url)
    except Exception:
        return False
    if not data or not data.lstrip().startswith(b"#EXTM3U"):
        return False
    text = data.decode("utf-8", errors="replace")
    complete = "#EXT-X-ENDLIST" in text
    inits, segments = _parse_playlist_assets(text, session.entry_url)
    if not segments:
        return False
    idx = min(
        _prefix_segment_count(text, PREFETCH_PREFIX_SEC, PREFETCH_MAX_SEGMENTS),
        len(segments),
    )
    targets: List[str] = list(dict.fromkeys(inits)) + segments[:idx]
    total = 0
    stored = 0
    for upstream in targets:
        try:
            seg_data, _, _, _ = _http_get_bytes(session, upstream)
        except Exception:
            continue  # best-effort: one bad segment skips it, the rest store
        if not seg_data:
            continue
        total += len(seg_data)
        _write_segment(platform, video_id, channel, upstream, seg_data)
        stored += 1
        if total >= PREFETCH_MAX_VIDEO_BYTES:
            break
    if stored == 0:
        return False
    # Playlist text is cached ONLY for completed VODs (immutable → safe to
    # serve cross-session; growing VODs change and stay upstream-fetched).
    if complete:
        _write_playlist(platform, video_id, session.entry_url, text)
    _touch_manifest(platform, video_id, channel, fetched=True)
    logger.info(
        "prefetch %s/%s: %d segment(s) %.1fMB%s",
        platform, video_id[:12], stored, total / 1e6,
        " +playlist" if complete else "",
    )
    return True


def _prefetch_video(platform: str, video_id: str, channel: dict) -> bool:
    prefer_height = PREFETCH_HEIGHT_BY_PLATFORM.get(platform, 720)
    if platform == "youtube":
        return _prefetch_youtube(video_id, channel, prefer_height)
    if platform == "kick":
        from services.kick_gate import kick_gate_active

        if kick_gate_active():
            return False
    url = _prefetch_url_for(platform, video_id, channel)
    try:
        raw_entry, headers, plat, _variants, kind, _yt = resolve_stream_info(
            url, prefer_height=prefer_height
        )
    except Exception:
        return False
    if kind != "hls":
        return False  # progressive MP4 (twitch clip) — no HLS segments to cache
    session = _make_hls_session(url, raw_entry, headers, plat, prefer_height)
    try:
        # Follow the master to the default preview variant's media playlist
        # (the exact one the frontend pins for first play).
        session.entry_url = _resolve_preview_entry(session, raw_entry, prefer_height)
    except Exception:
        return False
    return _prefetch_hls_leg(session, platform, video_id, channel)


def _prefetch_youtube(video_id: str, channel: dict, prefer_height: int) -> bool:
    """YouTube leg — resolve through the warm snapshot path (cheap, and it
    POPULATES the session-snapshot cache the user's create_session reads, so
    the eventual open reuses the same URLs and hits the prefetch)."""
    from services.preview.warm import _resolve_and_cache_youtube_snapshot
    from services.yt_gate import youtube_gate_active

    if youtube_gate_active():
        return False
    url = _prefetch_url_for("youtube", video_id, channel)
    try:
        resolved = _resolve_and_cache_youtube_snapshot(url, prefer_height=prefer_height)
    except Exception as exc:
        _note_gate_if_youtube(exc)
        return False
    if not resolved:
        return False
    _vid, _height, snap = resolved
    if snap.get("kind") != "hls" or snap.get("dash_window_hls"):
        # Progressive MP4 / DASH window-HLS: segments never flow through
        # proxy_segment — the existing prog-head warm / preflight mux already
        # cover instant start for those tiers.
        return False
    session = _session_from_snapshot(snap, url, prefer_height)
    return _prefetch_hls_leg(session, "youtube", video_id, channel)


def _note_gate_if_youtube(exc: BaseException) -> None:
    from services.yt_gate import classify_youtube_gate_error, note_youtube_gate

    if classify_youtube_gate_error(exc):
        note_youtube_gate(str(exc)[:200])


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _write_segment(platform: str, video_id: str, channel: dict, url: str, data: bytes) -> None:
    d = _video_dir(platform, video_id)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    path = _segment_path(platform, video_id, url)
    tmp = path.with_suffix(".part")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)  # atomic — serve path never sees a half-written file
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return
    with _manifest_lock:
        man = _load_manifest(platform, video_id)
        man.setdefault("segments", {})[url] = len(data)
        try:
            _manifest_path(platform, video_id).write_text(json.dumps(man, indent=1))
        except OSError:
            pass


def _write_playlist(platform: str, video_id: str, url: str, text: str) -> None:
    path = _playlist_path(platform, video_id, url)
    tmp = path.with_suffix(".part")
    try:
        _video_dir(platform, video_id).mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _touch_manifest(platform: str, video_id: str, channel: dict, *, fetched: bool) -> None:
    with _manifest_lock:
        man = _load_manifest(platform, video_id)
        man.setdefault("channel", str(channel.get("id") or ""))
        if fetched:
            man["fetched_at"] = time.time()
            man.pop("failed_at", None)
        else:
            man["failed_at"] = time.time()
        try:
            _video_dir(platform, video_id).mkdir(parents=True, exist_ok=True)
            _manifest_path(platform, video_id).write_text(json.dumps(man, indent=1))
        except OSError:
            pass


def _video_due(platform: str, video_id: str) -> bool:
    """True when the video should be (re)fetched this pass."""
    man = _load_manifest(platform, video_id)
    now = time.time()
    fetched = man.get("fetched_at")
    if isinstance(fetched, (int, float)) and now - fetched < PREFETCH_FRESH_SEC:
        return False
    failed = man.get("failed_at")
    if isinstance(failed, (int, float)) and now - failed < PREFETCH_FAILED_BACKOFF_SEC:
        return False
    return True


def _dir_size(root: Path) -> int:
    total = 0
    try:
        for p in root.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    except OSError:
        return 0
    return total


# ---------------------------------------------------------------------------
# Pass / eviction / budget
# ---------------------------------------------------------------------------

def _platform_enabled(channel: dict, platform: str) -> bool:
    if not (channel.get(_SLUG_KEYS[platform]) or "").strip():
        return False
    flag = {
        "twitch": "channel_twitch_enabled",
        "kick": "channel_kick_enabled",
        "youtube": "channel_youtube_enabled",
    }[platform]
    try:
        from deps import settings_mgr

        return bool(getattr(settings_mgr.get(), flag, True))
    except Exception:
        return True


def _recent_video_rows(platform: str, channel: dict) -> List[dict]:
    """The 5 most recent archived VODs of this saved channel on one platform."""
    slug = (channel.get(_SLUG_KEYS[platform]) or "").strip().lower()
    if not slug:
        return []
    try:
        return [
            dict(r)
            for r in archive_db.query(
                """SELECT video_id FROM videos
                   WHERE platform = ? AND lower(channel) = lower(?)
                     AND video_id <> '' AND video_id NOT LIKE 'youtube-live-%'
                   ORDER BY started_at DESC, created_at DESC LIMIT ?""",
                (platform, slug, PREFETCH_PER_PLATFORM),
            )
        ]
    except Exception:
        return []


def _evict_platform(platform: str, keep_ids: set) -> int:
    """Delete every prefetched video on *platform* not in *keep_ids* (the
    union of each saved channel's top-5). Returns the number of dirs removed."""
    root = prefetch_root() / platform
    if not root.is_dir():
        return 0
    removed = 0
    try:
        for entry in list(root.iterdir()):
            if entry.is_dir() and entry.name not in keep_ids:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
    except OSError:
        pass
    return removed


def _enforce_budget() -> Dict[str, float]:
    """Drop oldest (by fetched_at) video dirs past the global byte budget.
    Returns per-platform totals in MB for the pass log."""
    root = prefetch_root()
    totals: Dict[str, float] = {p: 0.0 for p in PLATFORM_ORDER}
    if not root.is_dir():
        return totals
    entries: List[Tuple[float, int, Path]] = []
    for platform_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        platform = platform_dir.name
        for vid_dir in sorted(p for p in platform_dir.iterdir() if p.is_dir()):
            size = _dir_size(vid_dir)
            totals[platform] = totals.get(platform, 0.0) + size
            fetched = _load_manifest(platform, vid_dir.name).get("fetched_at") or 0.0
            entries.append((float(fetched), size, vid_dir))
    total = sum(e[1] for e in entries)
    if total > PREFETCH_MAX_BYTES:
        for _fetched, size, d in sorted(entries):
            if total <= PREFETCH_MAX_BYTES:
                break
            shutil.rmtree(d, ignore_errors=True)
            total -= size
            d_platform = d.parent.name if d.parent and d.parent != root else "?"
            totals[d_platform] = max(0.0, totals.get(d_platform, 0.0) - size)
    return {p: round(b / 1e6, 1) for p, b in totals.items()}


def run_prefetch_pass(channels: list) -> dict:
    """One prefetch pass: evict stale entries, then fetch up to
    PREFETCH_PER_PASS due videos (priority-ordered channels first)."""
    stats = {"fetched": 0, "fresh": 0, "failed": 0, "evicted": 0}
    for platform in PLATFORM_ORDER:
        keep: set = set()
        for channel in channels or ():
            if not _platform_enabled(channel, platform):
                continue
            keep.update(r["video_id"] for r in _recent_video_rows(platform, channel))
        stats["evicted"] += _evict_platform(platform, keep)
    budget = PREFETCH_PER_PASS
    for platform in PLATFORM_ORDER:
        for channel in channels or ():
            if budget <= 0:
                break
            if not _platform_enabled(channel, platform):
                continue
            for row in _recent_video_rows(platform, channel):
                if budget <= 0:
                    break
                video_id = row["video_id"]
                if not _video_due(platform, video_id):
                    stats["fresh"] += 1
                    continue
                if _prefetch_video(platform, video_id, channel):
                    stats["fetched"] += 1
                    budget -= 1
                else:
                    stats["failed"] += 1
                    _touch_manifest(platform, video_id, channel, fetched=False)
    totals = _enforce_budget()
    if stats["fetched"] or stats["evicted"]:
        logger.info(
            "prefetch pass: fetched=%d fresh=%d failed=%d evicted=%d disk=%s",
            stats["fetched"], stats["fresh"], stats["failed"], stats["evicted"],
            {p: f"{m}MB" for p, m in totals.items()},
        )
    return stats


def _run_worker(channels: list) -> None:
    try:
        run_prefetch_pass(channels)
    except Exception:  # noqa: BLE001 — a pass must never kill the daemon
        logger.warning("prefetch pass failed", exc_info=True)


def kick_prefetch_pass(channels: Optional[list] = None) -> None:
    """Kick a background prefetch pass (one at a time — a slow fetch never
    queues behind itself). Called from the archive scheduler pass, so a
    channel add/edit (kick_scheduler_pass) also refreshes the cache."""
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_thread = threading.Thread(
            target=_run_worker,
            args=(list(channels or ()),),
            daemon=True,
            name="prefetch-pass",
        )
        _worker_thread.start()


# --- self-check (pure logic; no network, no DB, no data_dir writes) ---------

def _fake_session(url: str, platform: str) -> PreviewSession:
    return PreviewSession(
        session_id="sc",
        vod_url=url,
        master_url="",
        entry_url="",
        platform=platform,
        cache_dir=Path("."),
    )


assert _platform_video_id(_fake_session("https://www.twitch.tv/videos/1234567890", "Twitch")) == ("twitch", "1234567890")
assert _platform_video_id(_fake_session("https://www.twitch.tv/xqc/videos/1234567890", "Twitch")) == ("twitch", "1234567890")
assert _platform_video_id(_fake_session("https://kick.com/xqc/videos/dedcf0c6-1c74-4767-8049-9319f77fab6c", "Kick")) == ("kick", "dedcf0c6-1c74-4767-8049-9319f77fab6c")
assert _platform_video_id(_fake_session("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "YouTube")) == ("youtube", "dQw4w9WgXcQ")
assert _platform_video_id(_fake_session("https://youtu.be/dQw4w9WgXcQ", "YouTube")) == ("youtube", "dQw4w9WgXcQ")
assert _platform_video_id(_fake_session("https://www.twitch.tv/clip/Slug", "Twitch")) is None
assert _platform_video_id(_fake_session("https://www.twitch.tv/videos/1234567890", "Kick")) is None  # platform mismatch
assert _platform_video_id(_fake_session("", "Twitch")) is None
assert _platform_video_id(_fake_session("https://www.twitch.tv/videos/123", "Live")) is None
assert _segment_path("twitch", "1", "https://x/0.ts").name == _segment_path("twitch", "1", "https://x/0.ts").name
assert _segment_path("twitch", "1", "https://x/0.ts").parent == _video_dir("twitch", "1")
assert _segment_path("twitch", "2", "https://x/0.ts") != _segment_path("twitch", "1", "https://x/0.ts")
