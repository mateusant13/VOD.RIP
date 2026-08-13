"""YouTube channel listings via yt-dlp flat playlists (ponytail: no YouTube Data API key)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

PlaylistKind = Literal["videos", "shorts", "streams"]


def channel_playlist_url(channel_ref: str, kind: PlaylistKind = "videos") -> str:
    """Build channel tab URL from handle, @handle, channel id, or full URL."""
    ref = (channel_ref or "").strip()
    if not ref:
        raise ValueError("YouTube channel is required")
    suffix = {"videos": "/videos", "shorts": "/shorts", "streams": "/streams"}[kind]
    if ref.startswith("http://") or ref.startswith("https://"):
        base = ref.rstrip("/")
        for tail in ("/videos", "/shorts", "/streams", "/featured", "/playlists"):
            if base.endswith(tail):
                base = base[: -len(tail)]
                break
    elif ref.startswith("@"):
        base = f"https://www.youtube.com/{ref}"
    elif ref.startswith("UC") and len(ref) >= 10:
        base = f"https://www.youtube.com/channel/{ref}"
    else:
        base = f"https://www.youtube.com/@{ref}"
    return f"{base}{suffix}"


def _content_kind_for_playlist(kind: PlaylistKind) -> str:
    if kind == "shorts":
        return "short"
    if kind == "streams":
        return "stream"
    return "video"


def _created_at_from_entry(e: dict) -> Optional[str]:
    upload_date = e.get("upload_date")
    if upload_date and len(str(upload_date)) == 8:
        try:
            return datetime.strptime(
                str(upload_date), "%Y%m%d",
            ).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    for key in ("timestamp", "release_timestamp"):
        ts = e.get(key)
        if ts is None:
            continue
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            continue
    return None


def _parse_video_ts(value: Optional[str]) -> int:
    """Parse ISO date string to epoch milliseconds. Returns 0 if invalid."""
    if not value:
        return 0
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def _duration_string_from_sec(sec: int) -> str:
    m, s = divmod(max(0, int(sec)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# yt-dlp's parse_count knows k/K/m/M/b/B suffixes and dot decimals, but not
# pt/es suffixes ("mil", "milhão", "bi") nor comma decimals. With the app's
# lang=pt translated tab fields ("8,7 mil visualizações") it emits 87 for an
# 8.7k-view short (comma stripped, "mil" treated as x1). parse_abbreviated_
# view_count repairs exactly those forms; anything else falls back to yt-dlp.
_LOCALE_COUNT_RE = re.compile(
    r"(?P<num>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>milh[oõ]es|milh[ãa]o|bilh[oõ]es|bilh[ãa]o|mil|mi|bi|k|m|b)?"
    r"(?=\s|$)",
    re.IGNORECASE,
)
_LOCALE_COUNT_UNITS = {
    "k": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
    "mil": 1_000,
    "mi": 1_000_000,
    "milhão": 1_000_000, "milhao": 1_000_000,
    "milhões": 1_000_000, "milhoes": 1_000_000,
    "bi": 1_000_000_000,
    "bilhão": 1_000_000_000, "bilhao": 1_000_000_000,
    "bilhões": 1_000_000_000, "bilhoes": 1_000_000_000,
}


def parse_abbreviated_view_count(text: Optional[str]) -> Optional[int]:
    """Parse abbreviated/locale view-count text into the TRUE integer count.

    Handles '8.7K', '8,7 mil', '19 mil', '1,2 mi', '1,2 bilhão', '8.700' and
    plain ints; returns None when no recognizable count is present (callers
    then fall back to yt-dlp's own parser).
    """
    if not text:
        return None
    m = _LOCALE_COUNT_RE.search(str(text).strip())
    if not m:
        return None
    num_str, unit = m.group("num"), (m.group("unit") or "").lower()
    if "," in num_str and "." in num_str:
        # pt/es: dot is thousands, comma is the decimal separator ("8.700,5")
        num = float(num_str.replace(".", "").replace(",", "."))
    elif "," in num_str:
        tail = num_str.split(",", 1)[1]
        if len(tail) == 3:
            num = float(num_str.replace(",", ""))  # en-style thousands "8,700"
        else:
            num = float(num_str.replace(",", "."))  # pt/es decimal "8,7"
    elif "." in num_str and not unit:
        groups = num_str.split(".")
        if 1 <= len(groups[0]) <= 3 and all(len(g) == 3 for g in groups[1:]):
            num = float(num_str.replace(".", ""))  # pt thousands "8.700"
        else:
            return None  # ambiguous — let yt-dlp's parser decide
    else:
        num = float(num_str)
    return int(round(num * _LOCALE_COUNT_UNITS.get(unit, 1)))


_LOCALIZED_PARSE_INSTALLED = False


def _install_localized_count_parser() -> None:
    """Teach yt-dlp's count parser pt/es suffixes + comma decimals.

    The app requests lang=pt translated tab fields (youtube_session), so flat
    tab entries carry strings like "8,7 mil visualizações"; yt-dlp's
    parse_count misparses them (87 for 8.7k). Patch both the canonical
    yt_dlp.utils.parse_count and the copy imported into the youtube extractor
    module (yt_dlp.extractor.youtube._base). Idempotent per process.
    """
    global _LOCALIZED_PARSE_INSTALLED
    if _LOCALIZED_PARSE_INSTALLED:
        return
    try:
        import yt_dlp.utils as _utils

        _orig = _utils.parse_count
        if getattr(_orig, "_vodrip_localized", False):
            _LOCALIZED_PARSE_INSTALLED = True
            return

        def _parse_count_localized(text):
            parsed = parse_abbreviated_view_count(text)
            return parsed if parsed is not None else _orig(text)

        _parse_count_localized._vodrip_localized = True  # type: ignore[attr-defined]
        _utils.parse_count = _parse_count_localized
        try:
            from yt_dlp.extractor.youtube import _base as _yt_base

            _yt_base.parse_count = _parse_count_localized
        except Exception:  # noqa: BLE001 — module layout drift must not break listings
            pass
    except Exception as exc:  # noqa: BLE001 — parser install is best-effort
        logger.debug("localized count parser install failed: %s", exc)
    _LOCALIZED_PARSE_INSTALLED = True


# RSS-shorts freshness: the /shorts tab (yt-dlp) can lag new uploads by hours
# or days, so the channel's atom RSS feed (authoritative newest-first uploads)
# is unioned into shorts listings. Candidates missing from the tab are probed
# (one flat extract each) to classify short vs stream vs video; probes are
# bounded per listing and cached module-wide across listings.
_RSS_SHORT_PROBE_BUDGET = 4
_RSS_SHORT_PROBE_CACHE: dict[str, Optional[dict[str, Any]]] = {}


def _make_rss_probe():
    """Return probe(vid) -> metadata dict | None for RSS-only shorts candidates.

    Probes run one FULL single-video extract: the flat-tab path's
    player_client override (ios/mweb/web) plus extract_flat breaks single-
    video metadata ("Requested format is not available"), so the probe builds
    its own opts with the default client set (same as archive_ytdlp).
    Results are cached module-wide across listings.
    """

    def probe(vid: str) -> Optional[dict[str, Any]]:
        if vid in _RSS_SHORT_PROBE_CACHE:
            return _RSS_SHORT_PROBE_CACHE[vid]
        from services.ytdlp_guard import guarded_youtube_dl_channel
        from services.youtube_session import (
            apply_ytdlp_cookie_opts,
            youtube_session_from_settings,
            ytdlp_extractor_args,
        )

        meta: Optional[dict[str, Any]] = None
        try:
            session = youtube_session_from_settings()
            try:
                from deps import settings_mgr
                auto_auth = getattr(settings_mgr.get(), "youtube_auto_auth", True)
            except Exception:
                auto_auth = True
            probe_opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "ignoreerrors": True,
                "socket_timeout": 12,
                "extractor_args": ytdlp_extractor_args(session, auto_auth=auto_auth),
            }
            apply_ytdlp_cookie_opts(probe_opts, session, auto_auth=auto_auth)
            with guarded_youtube_dl_channel(probe_opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
            dur = info.get("duration")
            duration_sec = int(float(dur)) if dur is not None else None
            live_status = info.get("live_status")
            if live_status in ("live", "is_live", "is_upcoming", "was_live", "post_live"):
                kind = "stream"
            elif duration_sec is not None and duration_sec < 180:
                # Shorts window: YouTube accepts up to 3-minute shorts. The tab
                # fetch classifies by /shorts/ URL, which flat probes lack.
                # ponytail: yt-dlp exposes no isShort flag here; upgrade path =
                # probe the /shorts/<id> URL and detect the reel marker.
                kind = "short"
            else:
                kind = "video"
            meta = {"content_kind": kind, "duration": duration_sec, "availability": info.get("availability")}
        except Exception as exc:  # noqa: BLE001 — probe is best-effort
            logger.debug("youtube rss-short probe %s failed: %s", vid, exc)
        _RSS_SHORT_PROBE_CACHE[vid] = meta
        if len(_RSS_SHORT_PROBE_CACHE) > 2000:
            _RSS_SHORT_PROBE_CACHE.clear()
        return meta

    return probe


def _union_rss_shorts(
    rows: list[dict[str, Any]],
    channel_id: Optional[str],
    probe,
    *,
    budget: int = _RSS_SHORT_PROBE_BUDGET,
) -> list[dict[str, Any]]:
    """Merge brand-new shorts from the channel RSS feed into a shorts listing.

    Returns the input rows plus any RSS-only entries the probe classifies as
    shorts (dedup by video id; streams/VODs and member-only entries excluded).
    The caller sorts and applies :limit afterwards, so union rows participate
    in the normal date ordering.
    """
    if not channel_id or not rows:
        return rows
    rss = _fetch_youtube_rss_rows(channel_id)
    if not rss:
        return rows
    have = {r.get("id") for r in rows if r.get("id")}
    merged = list(rows)
    probed = 0
    for r in rss:
        vid = r.get("id")
        if not vid or vid in have:
            continue
        if probed >= budget:
            break
        probed += 1
        meta = probe(vid) if probe else None
        if not meta or meta.get("content_kind") != "short":
            continue
        if meta.get("availability") == "subscriber_only":
            continue  # keep member-only filtering at the union boundary too
        duration_sec = meta.get("duration")
        merged.append({
            "_list_order": len(merged) + 1,
            "id": vid,
            "platform": "YouTube",
            "title": r.get("title") or "Untitled",
            "duration": duration_sec,
            "duration_string": _duration_string_from_sec(duration_sec) if duration_sec else None,
            "created_at": r.get("created_at"),
            "views": r.get("views"),  # RSS statistics views: exact ints
            "thumbnail_url": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            "url": f"https://www.youtube.com/shorts/{vid}",
            "channel": channel_id,
            "content_kind": "short",
            "availability": None,
        })
        have.add(vid)
    return merged










def _fetch_youtube_rss_rows(
    channel_id: str,
    content_kind: Optional[str] = "vod",
) -> list[dict[str, Any]]:
    """Fetch the channel's public RSS feed and return rows with reliable dates.

    The RSS feed (feeds/videos.xml?channel_id=) returns the channel's TRUE
    most-recent-first uploads with publish dates, view counts and NO auth/POT
    dependency. Returns [] on any failure (best-effort, never blocks the listing).
    """
    try:
        import re as _re
        import urllib.request
        import xml.etree.ElementTree as ET

        def _norm_title(t: str) -> str:
            return _re.sub(r"[^a-z0-9]", "", (t or "").lower())

        req = urllib.request.Request(
            f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        ns = {
            "a": "http://www.w3.org/2005/Atom",
            # YouTube's RSS feed declares xmlns:yt="http://www.youtube.com/xml/schemas/2015"
            "yt": "http://www.youtube.com/xml/schemas/2015",
            "media": "http://search.yahoo.com/mrss/",
        }
        root = ET.fromstring(raw)
        rows: list[dict[str, Any]] = []
        for e in root.findall("a:entry", ns):
            vid_el = e.find("yt:videoId", ns)
            pub_el = e.find("a:published", ns)
            title_el = e.find("a:title", ns)
            if vid_el is None or not vid_el.text or pub_el is None or not pub_el.text:
                continue
            created_at = pub_el.text
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                created_at = dt.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError):
                pass
            # RSS exposes a reliable view count (no auth needed).
            views = None
            stat = e.find("media:group/media:community/media:statistics", ns)
            if stat is not None:
                vc = stat.get("views")
                if vc:
                    try:
                        views = int(vc)
                    except (TypeError, ValueError):
                        pass
            rows.append({
                "id": vid_el.text,
                "platform": "YouTube",
                "title": (title_el.text if title_el is not None else "Untitled") or "Untitled",
                "duration": None,
                "duration_string": None,
                "created_at": created_at,
                "views": views,
                "thumbnail_url": f"https://i.ytimg.com/vi/{vid_el.text}/mqdefault.jpg",
                "url": f"https://www.youtube.com/watch?v={vid_el.text}",
                "channel": channel_id,
                "content_kind": content_kind,
            })
        return rows
    except Exception as exc:  # RSS is best-effort
        logger.debug("youtube rss fetch failed: %s", exc)
        return []


def _classify_youtube_video(
    *,
    vid: str,
    title: str,
    url: str,
    duration: Optional[int],
    live_status: Optional[str],
    playlist_source: str,
) -> str:
    """Classify YouTube video using multiple signals.

    Priority:
    1. URL pattern: /shorts/ in URL -> short
    2. Live status: live/completed -> stream
    3. Duration: < 60s -> short
    4. Playlist source as fallback
    """
    # 1. URL pattern (strongest signal for shorts)
    if "/shorts/" in url:
        return "short"
    
    # 2. Live status (strongest signal for streams)
    if live_status in ("live", "is_live", "is_upcoming", "was_live", "post_live"):
        return "stream"
    
    # 3. Duration-based classification
    if duration is not None:
        if duration < 60:
            return "short"  # Under 60s = short
        # NOTE: > 1 hour is NOT a stream signal — long VODs (2-8h recorded streams)
        # stay "vod". live_status is the correct stream indicator.
    
    # 4. Playlist source as final fallback
    if playlist_source == "shorts":
        return "short"
    if playlist_source == "streams":
        return "stream"
    return "video"


def _enrich_with_rss_dates(rows: list[dict[str, Any]], channel_id: Optional[str]) -> None:
    """Fill missing created_at and views from the public RSS feed."""
    if not channel_id or not rows:
        return
    rss = _fetch_youtube_rss_rows(channel_id)
    if not rss:
        return
    rss_by_id: dict[str, dict[str, Any]] = {r["id"]: r for r in rss if r.get("id")}
    for row in rows:
        r = rss_by_id.get(row.get("id") or "")
        if r:
            if not row.get("created_at") and r.get("created_at"):
                row["created_at"] = r["created_at"]
            if row.get("views") is None and r.get("views") is not None:
                row["views"] = r["views"]
            elif (
                row.get("views") is not None
                and r.get("views") is not None
                and abs(int(r["views"]) - int(row["views"])) > 3 * max(int(row["views"]), 1)
            ):
                # RSS counts are exact; a flat-tab value >3x off is a broken
                # localized parse — never ship a count off by ~10/100/1000.
                row["views"] = r["views"]


def list_channel_videos_sync(
    channel_ref: str,
    limit: int = 50,
    *,
    playlist: PlaylistKind = "videos",
    enrich: bool = True,
) -> list[dict[str, Any]]:
    import yt_dlp

    from services.ytdlp_guard import guarded_youtube_dl_channel
    from services.youtube_session import (
        resolve_ytdlp_cookiefile,
        youtube_session_from_settings,
        ytdlp_extractor_args,
    )

    _install_localized_count_parser()
    session = youtube_session_from_settings()
    try:
        from deps import settings_mgr
        auto_auth = getattr(settings_mgr.get(), "youtube_auto_auth", True)
    except Exception:
        auto_auth = True
    ext_args = ytdlp_extractor_args(session, auto_auth=auto_auth)

    base_opts: dict[str, Any] = {
        "playlistend": max(1, min(int(limit) * 3, 300)),
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
        "socket_timeout": 12,
        "extractor_args": {
            "youtube": {
                **ext_args["youtube"],
                "player_client": ["ios", "mweb", "web"],
            },
            **{k: v for k, v in ext_args.items() if k != "youtube"},
        },
    }
    from services.youtube_session import apply_ytdlp_cookie_opts

    apply_ytdlp_cookie_opts(base_opts, session, auto_auth=auto_auth)

    all_videos: dict[str, dict[str, Any]] = {}
    channel_id: Optional[str] = None
    list_order = 0

    # Fetch ONLY the requested tab — the router always passes an explicit
    # playlist kind, and 3 sequential extracts (~8s each) triple both latency
    # and YouTube request volume (bot-wall risk). ponytail: a was_live entry
    # that YouTube lists only under /videos (not /streams) no longer appears in
    # the streams response; upgrade path = fetch both tabs when playlist=streams.
    pl = playlist if playlist in ("videos", "shorts", "streams") else "videos"
    pl_url = channel_playlist_url(channel_ref, pl)
    try:
        with guarded_youtube_dl_channel(base_opts) as ydl:
            info = ydl.extract_info(pl_url, download=False)
        channel_id = (info or {}).get("channel_id") or (info or {}).get("uploader_id")
    except Exception as exc:
        logger.debug("youtube playlist %s failed: %s", pl, exc)
        info = None

    entries = (info or {}).get("entries") or []
    for e in entries:
        if not e:
            continue
        vid = (e.get("id") or "").strip()
        if not vid or vid in all_videos:
            continue

        if pl == "shorts":
            webpage = f"https://www.youtube.com/shorts/{vid}"
        else:
            webpage = e.get("url") or f"https://www.youtube.com/watch?v={vid}"
        if not str(webpage).startswith("http"):
            webpage = f"https://www.youtube.com/watch?v={vid}"

        created_at = _created_at_from_entry(e)
        thumb = e.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
        dur = e.get("duration")
        dur_str = None
        duration_sec = None
        if dur is not None:
            try:
                duration_sec = int(float(dur))
                dur_str = _duration_string_from_sec(duration_sec)
            except (TypeError, ValueError):
                pass

        content_kind = _classify_youtube_video(
            vid=vid,
            title=e.get("title") or "",
            url=webpage,
            duration=duration_sec,
            live_status=e.get("live_status"),
            playlist_source=pl,
        )

        list_order += 1
        all_videos[vid] = {
            "_list_order": list_order,
            "id": vid,
            "platform": "YouTube",
            "title": e.get("title") or "Untitled",
            "duration": duration_sec,
            "duration_string": dur_str,
            "created_at": created_at,
            "views": e.get("view_count"),
            "thumbnail_url": thumb,
            "url": webpage,
            "channel": e.get("channel") or e.get("uploader") or channel_ref,
            "content_kind": content_kind,
            "availability": e.get("availability"),  # yt-dlp sets this for member-only
        }

    # Filter by requested playlist type. Drop member-only entries at source.
    _memb_only = lambda v: v.get("availability") == "subscriber_only"  # noqa: E731
    if playlist == "videos":
        filtered = [v for v in all_videos.values() if v["content_kind"] == "video" and not _memb_only(v)]
    elif playlist == "shorts":
        filtered = [v for v in all_videos.values() if v["content_kind"] == "short" and not _memb_only(v)]
    elif playlist == "streams":
        filtered = [v for v in all_videos.values() if v["content_kind"] == "stream" and not _memb_only(v)]
    else:
        filtered = [v for v in all_videos.values() if not _memb_only(v)]

    # Shorts freshness: the /shorts tab can lag new uploads — union the
    # channel's atom RSS feed and keep only entries the probe classifies as
    # shorts. Best-effort and bounded (see _RSS_SHORT_PROBE_BUDGET).
    if pl == "shorts" and enrich and channel_id and filtered:
        filtered = _union_rss_shorts(filtered, channel_id, _make_rss_probe())

    # Sort by date newest first; items without dates preserve playlist order
    filtered.sort(key=lambda v: (_parse_video_ts(v.get("created_at")) or 0, -(v.get("_list_order", 0))), reverse=True)
    filtered = filtered[:limit]

    # Enrich with RSS dates/views where available
    if enrich and channel_id:
        _enrich_with_rss_dates(filtered, channel_id)

    return filtered


def channel_search_url(handle: str, query: str) -> str:
    """Channel-scoped YouTube search page URL for a handle or channel id."""
    from urllib.parse import quote
    h = handle.strip().lstrip("@")
    base = (
        f"https://www.youtube.com/channel/{h}"
        if h.startswith("UC")
        else f"https://www.youtube.com/@{h}"
    )
    return f"{base}/search?query={quote(query)}"


def search_channel_videos_sync(handle: str, query: str, limit: int = 20) -> list[dict]:
    """Channel-scoped title search (the @handle/search tab), flat entries.

    Used by the archive search UI as the remote fallback for saved YouTube
    channels: the local index only holds the newest ~100 uploads per channel,
    so old series ("vale da estranheza") are unreachable locally. Same
    guarded yt-dlp + cookie machinery as list_channel_videos_sync; returns
    [] on any fetch failure (the router surfaces the error).
    """
    from services.ytdlp_guard import guarded_youtube_dl_channel
    from services.youtube_session import (
        apply_ytdlp_cookie_opts,
        youtube_session_from_settings,
        ytdlp_extractor_args,
    )

    _install_localized_count_parser()
    session = youtube_session_from_settings()
    try:
        from deps import settings_mgr
        auto_auth = getattr(settings_mgr.get(), "youtube_auto_auth", True)
    except Exception:
        auto_auth = True
    ext_args = ytdlp_extractor_args(session, auto_auth=auto_auth)
    base_opts: dict[str, Any] = {
        "playlistend": max(1, min(int(limit), 50)),
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
        "socket_timeout": 12,
        "extractor_args": {
            "youtube": {
                **ext_args["youtube"],
                "player_client": ["ios", "mweb", "web"],
            },
            **{k: v for k, v in ext_args.items() if k != "youtube"},
        },
    }
    apply_ytdlp_cookie_opts(base_opts, session, auto_auth=auto_auth)
    url = channel_search_url(handle, query)
    out: list[dict] = []
    try:
        with guarded_youtube_dl_channel(base_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        logger.debug("youtube channel search %s failed: %s", url, exc)
        return out
    for e in (info or {}).get("entries") or []:
        if not e:
            continue
        vid = str(e.get("id") or "").strip()
        if not vid:
            continue
        dur = e.get("duration")
        duration_sec = None
        if dur is not None:
            try:
                duration_sec = int(float(dur))
            except (TypeError, ValueError):
                pass
        title = str(e.get("title") or "").strip()
        out.append({
            "id": vid,
            "platform": "YouTube",
            "title": title,
            "duration": duration_sec,
            "duration_string": _duration_string_from_sec(duration_sec) if duration_sec else None,
            "created_at": _created_at_from_entry(e),
            "views": e.get("view_count"),
            "thumbnail_url": e.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            "url": f"https://www.youtube.com/watch?v={vid}",
            "channel": e.get("channel") or e.get("uploader") or handle,
        })
    return out


assert channel_search_url("gaveta", "vale da estranheza").endswith("/search?query=vale%20da%20estranheza")
assert channel_search_url("UCabc123", "x").startswith("https://www.youtube.com/channel/UCabc123/search")
assert channel_search_url("@gaveta", "x").startswith("https://www.youtube.com/@gaveta/search")

assert channel_playlist_url("cellbit", "videos").endswith("/videos")
assert channel_playlist_url("@cellbit", "shorts").endswith("/shorts")
assert channel_playlist_url("UCxyz1234567890abcdefghijk", "streams").endswith("/streams")
assert _created_at_from_entry({"upload_date": "20240511"}) is not None
assert _created_at_from_entry({"timestamp": 1_700_000_000}) is not None
assert _duration_string_from_sec(125) == "2:05"
assert _enrich_with_rss_dates([], "dummy") is None  # self-check: no-op on empty input

# View-count parse self-checks (localized abbreviated forms -> true ints).
assert parse_abbreviated_view_count("8.7K views") == 8700
assert parse_abbreviated_view_count("8,7 mil visualizações") == 8700
assert parse_abbreviated_view_count("19 mil") == 19000
assert parse_abbreviated_view_count("2,5 mil") == 2500
assert parse_abbreviated_view_count("1 mil") == 1000
assert parse_abbreviated_view_count("1,2 mi") == 1_200_000
assert parse_abbreviated_view_count("8.700") == 8700
assert parse_abbreviated_view_count("8700") == 8700
assert parse_abbreviated_view_count("") is None
assert parse_abbreviated_view_count("no views") is None
assert _union_rss_shorts([], "UCdummy", probe=None) == []  # self-check: no-op on empty input
