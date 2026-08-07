"""YouTube Data API v3 — optional official-API layer (issue #4).

Slots in FRONT of the unofficial YouTube paths (yt-dlp / InnerTube) when a
Data API key is configured: ``search.list`` for channel search,
``videos.list`` for video metadata, ``captions.list`` + ``captions.download``
for caption tracks. Every entry point raises on failure so the caller falls
back to the current unofficial path silently.

Quota-aware: per-key daily usage is persisted to an appdata JSON file
(atomic, thread-safe). The official layer degrades itself at 80% of the
10,000-unit daily quota — callers check :func:`available` and skip the
official path, and the Settings UI surfaces the degraded state.

Notes:
- ``captions.list`` / ``captions.download`` are OAuth-only endpoints in the
  Data API (an API key alone gets HTTP 403) — with a key-only setup the
  captions path typically falls back to the unofficial path, which is the
  designed behavior. The wiring is exercised by unit tests.
- Historical live chat is NOT retrievable via the Data API — chat stays on
  the paced unofficial path (archive_ytdlp / chat sinks), untouched.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"
HTTP_TIMEOUT_S = 12.0

# Official daily quota and the degrade threshold (80%).
DAILY_QUOTA = 10_000
DEGRADE_RATIO = 0.8
DEGRADE_AT = int(DAILY_QUOTA * DEGRADE_RATIO)  # 8000

# Per-method unit costs (developers.google.com/youtube/v3/getting-started#quota).
COST_SEARCH = 100
COST_VIDEOS = 1
COST_CHANNELS = 1
COST_CAPTIONS_LIST = 50
COST_CAPTIONS_DOWNLOAD = 200

# ISO-8601 durations: "PT4H21M33S", "PT1H2M", "PT33S", "PT1M30.5S".
_ISO_DURATION_RE = re.compile(
    r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?$"
)

# SRT: "N\nHH:MM:SS,mmm --> HH:MM:SS,mmm\nTEXT\n\n".
_SRT_CUE_RE = re.compile(
    r"(?:^|\n)\s*(\d+)\s*\n"
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})"
    r"\n(.*?)(?=\n\s*\d+\s*\n\d{2}:|\Z)",
    re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")

# Persisted per-key daily usage: {"date": "YYYY-MM-DD", "keys": {key: units}}.
_QUOTA_FILE = "youtube_data_api_quota.json"
_QUOTA_LOCK = threading.Lock()
_usage_cache: Optional[dict] = None  # {key: units} for today (lazy-loaded)


def api_key() -> str:
    try:
        from deps import settings_mgr

        return (getattr(settings_mgr.get(), "youtube_data_api_key", "") or "").strip()
    except Exception:
        return ""


def _quota_path() -> Path:
    from services import settings as _settings_mod  # lazy: test-friendly monkeypatch

    return _settings_mod._get_appdata_dir() / _QUOTA_FILE


def _load_usage() -> dict:
    """{key: units} for today, or {} when stale/absent. Never raises."""
    global _usage_cache
    with _QUOTA_LOCK:
        if _usage_cache is not None:
            return dict(_usage_cache)
        try:
            raw = json.loads(_quota_path().read_text(encoding="utf-8"))
            if raw.get("date") == date.today().isoformat():
                _usage_cache = {str(k): int(v) for k, v in (raw.get("keys") or {}).items()}
                return dict(_usage_cache)
        except Exception:
            pass
        _usage_cache = {}
        return {}


def _save_usage(usage: dict) -> None:
    global _usage_cache
    with _QUOTA_LOCK:
        _usage_cache = dict(usage)
        try:
            import tempfile

            path = _quota_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix="yt_quota_", suffix=".tmp", dir=str(path.parent)
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"date": date.today().isoformat(), "keys": usage}, fh)
            os.replace(tmp, str(path))
        except OSError as exc:
            logger.debug("youtube quota persist failed: %s", exc)


def quota_used() -> int:
    """Units charged to the current key today (0 when unset)."""
    key = api_key()
    if not key:
        return 0
    return int(_load_usage().get(key, 0))


def degraded() -> bool:
    """True when the configured key has consumed >= 80% of the daily quota."""
    key = api_key()
    if not key:
        return False
    return quota_used() >= DEGRADE_AT


def available() -> bool:
    """Official path usable: key set AND quota not degraded."""
    return bool(api_key()) and not degraded()


def _charge(cost: int) -> None:
    """Record *cost* units for the current key (successful calls only)."""
    key = api_key()
    if not key:
        return
    usage = _load_usage()
    usage[key] = int(usage.get(key, 0)) + cost
    _save_usage(usage)


def _require_available() -> None:
    """Defense in depth: callers check :func:`available` first, but a direct
    call while degraded must raise so it falls back instead of burning quota."""
    if not available():
        raise RuntimeError("youtube data api degraded or no key configured")


def _http_get_raw(path: str, params: Dict[str, Any]) -> str:
    """One Data API GET returning the raw body (captions.download serves
    SRT text, not JSON). Raises RuntimeError on any failure."""
    key = api_key()
    if not key:
        raise RuntimeError("no youtube data api key configured")
    params = dict(params)
    params["key"] = key
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "text/plain, */*"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"YouTube Data API HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"YouTube Data API request failed: {e}") from e


def _http_get_json(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """One Data API GET with the key; raises RuntimeError on any failure."""
    key = api_key()
    if not key:
        raise RuntimeError("no youtube data api key configured")
    params = dict(params)
    params["key"] = key
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"YouTube Data API HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"YouTube Data API request failed: {e}") from e


def _iso_duration_to_sec(value: Any) -> Optional[int]:
    if not value:
        return None
    m = _ISO_DURATION_RE.match(str(value).strip())
    if not m:
        return None
    h, mm, s = m.groups()
    return int(h or 0) * 3600 + int(mm or 0) * 60 + int(float(s or 0))


def _channel_id_for_handle(handle: str) -> str:
    """Resolve a channel reference to a UC id: direct when it already is
    one, else channels.list (forHandle). Raises when unresolvable."""
    ref = (handle or "").strip()
    if ref.startswith("UC") and len(ref) >= 10 and re.fullmatch(r"[A-Za-z0-9_-]+", ref):
        return ref
    data = _http_get_json("/channels", {"part": "snippet", "forHandle": ref.lstrip("@")})
    items = (data or {}).get("items") or []
    _charge(COST_CHANNELS)
    if not items:
        raise RuntimeError(f"youtube channel not found: {handle}")
    return str(items[0]["id"])


def search_videos(handle: str, query: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Channel-scoped title search via search.list, in the same row shape as
    :func:`services.youtube_service.search_channel_videos_sync`. Durations
    and view counts come from one batched videos.list call. Raises on any
    failure (caller falls back to the unofficial path)."""
    _require_available()
    limit = max(1, min(int(limit), 50))
    channel_id = _channel_id_for_handle(handle)
    data = _http_get_json("/search", {
        "part": "snippet",
        "channelId": channel_id,
        "q": query,
        "type": "video",
        "maxResults": limit,
    })
    _charge(COST_SEARCH)
    items = (data or {}).get("items") or []
    if not items:
        return []

    vids = [str(it["id"].get("videoId") or "") for it in items if (it.get("id") or {}).get("videoId")]
    durations: Dict[str, int] = {}
    views: Dict[str, int] = {}
    if vids:
        batch = _http_get_json("/videos", {
            "part": "contentDetails,statistics",
            "id": ",".join(vids),
        })
        _charge(COST_VIDEOS)
        for v in (batch or {}).get("items") or []:
            vid = str(v.get("id") or "")
            durations[vid] = _iso_duration_to_sec((v.get("contentDetails") or {}).get("duration"))
            try:
                views[vid] = int((v.get("statistics") or {}).get("viewCount") or 0)
            except (TypeError, ValueError):
                views[vid] = 0

    rows: List[Dict[str, Any]] = []
    for it in items:
        snippet = it.get("snippet") or {}
        vid = str((it.get("id") or {}).get("videoId") or "")
        if not vid:
            continue
        duration = durations.get(vid)
        dur_str = None
        if duration is not None:
            m, s = divmod(int(duration), 60)
            h, m = divmod(m, 60)
            dur_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        thumbs = snippet.get("thumbnails") or {}
        thumb = (thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url")
        rows.append({
            "id": vid,
            "platform": "YouTube",
            "title": snippet.get("title") or "Untitled",
            "duration": duration,
            "duration_string": dur_str,
            "created_at": snippet.get("publishedAt"),
            "views": views.get(vid),
            "thumbnail_url": thumb or f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            "url": f"https://www.youtube.com/watch?v={vid}",
            "channel": snippet.get("channelTitle") or handle,
        })
    return rows


def video_metadata(video_id: str) -> Dict[str, Any]:
    """Metadata for one video via videos.list. Raises when the video is
    missing or the request fails (caller falls back to the unofficial path)."""
    _require_available()
    data = _http_get_json("/videos", {
        "part": "snippet,contentDetails,statistics",
        "id": str(video_id),
    })
    _charge(COST_VIDEOS)
    items = (data or {}).get("items") or []
    if not items:
        raise RuntimeError(f"youtube video not found: {video_id}")
    v = items[0]
    snippet = v.get("snippet") or {}
    content = v.get("contentDetails") or {}
    stats = v.get("statistics") or {}
    try:
        views = int(stats.get("viewCount") or 0)
    except (TypeError, ValueError):
        views = None
    return {
        "title": snippet.get("title") or "Untitled",
        "channel": snippet.get("channelTitle") or "",
        "started_at": snippet.get("publishedAt"),
        "duration_sec": _iso_duration_to_sec(content.get("duration")),
        "views": views,
    }


def list_caption_tracks(video_id: str) -> List[Tuple[str, str, str]]:
    """[(track_id, lang, kind)] via captions.list; kind is 'asr' (auto) or
    'standard' (manual). Raises on failure — the caller falls back to the
    unofficial caption path."""
    _require_available()
    data = _http_get_json("/captions", {
        "part": "snippet",
        "videoId": str(video_id),
    })
    _charge(COST_CAPTIONS_LIST)
    out: List[Tuple[str, str, str]] = []
    for it in (data or {}).get("items") or []:
        tid = str(it.get("id") or "")
        snip = it.get("snippet") or {}
        lang = (snip.get("language") or "").strip()
        if not tid or not lang:
            continue
        kind = (snip.get("trackKind") or "").strip().lower()
        out.append((tid, lang, kind))
    return out


def download_caption(track_id: str) -> str:
    """SRT body for one caption track via captions.download (raw text, not
    JSON). Raises on any failure — the caller skips the track / falls back
    to the unofficial path."""
    data = _http_get_raw(f"/captions/{str(track_id)}", {})
    _charge(COST_CAPTIONS_DOWNLOAD)
    return str(data)


def fetch_captions(
    video_id: str,
    *,
    prefer: Tuple[str, ...] = ("pt", "pt-br", "en", "en-orig"),
    families: Optional[Tuple[str, ...]] = ("pt", "en"),
    one_per_family: bool = True,
    max_tracks: Optional[int] = None,
) -> List[Tuple[str, str, str]]:
    """Best caption tracks via captions.list + captions.download.

    Returns [(lang, kind, srt), ...] ranked by *prefer* (exact codes first,
    manual over auto at equal rank), limited to *families* (None = all) and
    at most one track per family when *one_per_family*. *max_tracks* caps
    the number of downloaded tracks (the subtitles router wants exactly the
    best one; the ingest path wants both language families). A track whose
    download fails is skipped; raises only when the LIST call fails (the
    caller then falls back to the unofficial path)."""
    tracks = list_caption_tracks(video_id)
    if not tracks:
        return []

    def _rank_key(t: Tuple[str, str, str]) -> Tuple[int, int, int]:
        lang, kind = t[1], t[2]
        lower = lang.lower()
        exact = prefer.index(lower) if lower in prefer else len(prefer)
        manual = 0 if kind == "standard" else 1
        return (exact, manual)

    out: List[Tuple[str, str, str]] = []
    taken_families = set()
    for tid, lang, kind in sorted(tracks, key=_rank_key):
        fam = lang.lower().split("-")[0]
        if families is not None and fam not in families:
            continue
        if one_per_family and fam in taken_families:
            continue
        try:
            srt = download_caption(tid)
        except Exception as exc:
            logger.debug("caption download %s (%s) failed: %s", tid, lang, exc)
            continue
        out.append((lang, kind, srt))
        taken_families.add(fam)
        if max_tracks is not None and len(out) >= max_tracks:
            break
    return out


def parse_srt(text: str) -> List[Dict[str, Any]]:
    """SRT caption document -> transcript segments in the archive row shape:
    {seg_idx, start_sec, end_sec, text, words}. words stays [] (SRT has no
    word timestamps — the unofficial VTT path keeps that advantage)."""
    segments: List[Dict[str, Any]] = []
    for m in _SRT_CUE_RE.finditer(text):
        _idx, sh, sm, ss, _s_ms, eh, em, es, _e_ms, raw = m.groups()
        start = int(sh) * 3600 + int(sm) * 60 + int(ss)
        end = int(eh) * 3600 + int(em) * 60 + int(es)
        cleaned = re.sub(r"\s+", " ", _TAG_RE.sub("", raw)).strip()
        if not cleaned:
            continue
        segments.append({
            "seg_idx": len(segments) + 1,
            "start_sec": start,
            "end_sec": end,
            "text": cleaned,
            "words": [],
        })
    return segments


def quota_status() -> Dict[str, Any]:
    """Status payload for the Settings UI (never raises)."""
    return {
        "youtube_api_key_set": bool(api_key()),
        "youtube_quota_used": quota_used(),
        "youtube_quota_limit": DAILY_QUOTA,
        "youtube_degraded": degraded(),
    }


# --- module self-check (pure parsing — no I/O, no network) -----------------
assert _iso_duration_to_sec("PT4H21M33S") == 4 * 3600 + 21 * 60 + 33
assert _iso_duration_to_sec("PT1H2M") == 3720
assert _iso_duration_to_sec("PT33S") == 33
assert _iso_duration_to_sec("PT1M30.5S") == 90
assert _iso_duration_to_sec("") is None
assert _iso_duration_to_sec(None) is None
assert _iso_duration_to_sec("4h21m33s") is None  # helix format is not ISO-8601

_srt = (
    "1\n00:00:01,000 --> 00:00:03,500\nOlá mundo!\n\n"
    "2\n00:00:03,500 --> 00:00:05,000\n<i>Segunda</i> linha\n\n"
    "3\n00:00:06,000 --> 00:00:06,500\n\n"
)
_srt_segs = parse_srt(_srt)
assert _srt_segs[0] == {"seg_idx": 1, "start_sec": 1, "end_sec": 3, "text": "Olá mundo!", "words": []}
assert _srt_segs[1]["text"] == "Segunda linha"
assert len(_srt_segs) == 2  # empty cue dropped
assert parse_srt("garbage") == []
assert DEGRADE_AT == 8000
