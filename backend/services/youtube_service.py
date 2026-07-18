"""YouTube channel listings via yt-dlp flat playlists (ponytail: no YouTube Data API key)."""

from __future__ import annotations

import logging
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
        return "clip"
    if kind == "streams":
        return "stream"
    return "vod"


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


def _youtube_row_needs_enrich(row: dict[str, Any]) -> bool:
    if not row.get("created_at"):
        return True
    if row.get("views") is None:
        return True
    dur = row.get("duration")
    if dur is None:
        return True
    try:
        if int(float(dur)) <= 0:
            return True
    except (TypeError, ValueError):
        return True
    return False


def _apply_youtube_row_metadata(row: dict[str, Any], meta: dict[str, Any]) -> None:
    if not row.get("created_at") and meta.get("created_at"):
        row["created_at"] = meta["created_at"]
    if row.get("views") is None and meta.get("views") is not None:
        row["views"] = meta["views"]
    if not row.get("duration") and meta.get("duration"):
        row["duration"] = meta["duration"]
        if not row.get("duration_string"):
            try:
                row["duration_string"] = _duration_string_from_sec(int(meta["duration"]))
            except (TypeError, ValueError):
                pass


_YOUTUBE_ENRICH_MAX = 24  # ponytail: cap — visible rows only; full list on scroll later


def _enrich_youtube_channel_rows(rows: list[dict[str, Any]]) -> None:
    """Fill missing date/views/duration via lightweight InnerTube (flat playlist is spotty)."""
    need = [r for r in rows if _youtube_row_needs_enrich(r)][: _YOUTUBE_ENRICH_MAX]
    if not need:
        return
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from services.youtube_innertube import innertube_video_row_metadata
    from services.youtube_session import youtube_session_from_settings

    session = youtube_session_from_settings()
    by_id = {r["id"]: r for r in need if r.get("id")}
    workers = min(6, len(by_id))

    def _fetch(vid: str) -> tuple[str, Optional[dict[str, Any]]]:
        return vid, innertube_video_row_metadata(vid, session=session)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_fetch, vid) for vid in by_id]
        for fut in as_completed(futs):
            try:
                vid, meta = fut.result()
            except Exception as exc:
                logger.debug("youtube row enrich failed: %s", exc)
                continue
            if not meta:
                continue
            row = by_id.get(vid)
            if not row:
                continue
            _apply_youtube_row_metadata(row, meta)


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
    3. Duration: < 60s -> short, > 1 hour -> stream
    4. Playlist source as fallback
    """
    # 1. URL pattern (strongest signal for shorts)
    if "/shorts/" in url:
        return "clip"
    
    # 2. Live status (strongest signal for streams)
    if live_status in ("live", "is_live", "is_upcoming", "was_live", "post_live"):
        return "stream"
    
    # 3. Duration-based classification
    if duration is not None:
        if duration < 60:
            return "clip"  # Under 60s = short
        if duration > 3600:  # Over 1 hour
            return "stream"
    
    # 4. Playlist source as final fallback
    if playlist_source == "shorts":
        return "clip"
    if playlist_source == "streams":
        return "stream"
    return "vod"


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

    for pl in ("videos", "shorts", "streams"):
        pl_url = channel_playlist_url(channel_ref, pl)
        try:
            with guarded_youtube_dl_channel(base_opts) as ydl:
                info = ydl.extract_info(pl_url, download=False)
            if channel_id is None:
                channel_id = (info or {}).get("channel_id") or (info or {}).get("uploader_id")
        except Exception as exc:
            logger.debug("youtube playlist %s failed: %s", pl, exc)
            continue

        entries = (info or {}).get("entries") or []
        for e in entries:
            if not e:
                continue
            vid = (e.get("id") or "").strip()
            if not vid:
                continue
            if vid in all_videos:
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

            all_videos[vid] = {
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
            }

    # Filter by requested playlist type
    if playlist == "videos":
        filtered = [v for v in all_videos.values() if v["content_kind"] == "vod"]
    elif playlist == "shorts":
        filtered = [v for v in all_videos.values() if v["content_kind"] == "clip"]
    elif playlist == "streams":
        filtered = [v for v in all_videos.values() if v["content_kind"] == "stream"]
    else:
        filtered = list(all_videos.values())

    # Sort by date newest first
    filtered.sort(key=lambda v: _parse_video_ts(v.get("created_at")) or 0, reverse=True)
    filtered = filtered[:limit]

    # Enrich with RSS dates/views where available
    if enrich and channel_id:
        _enrich_with_rss_dates(filtered, channel_id)

    # Innertube enrichment
    if enrich:
        _enrich_youtube_channel_rows(filtered)

    return filtered


assert channel_playlist_url("cellbit", "videos").endswith("/videos")
assert channel_playlist_url("@cellbit", "shorts").endswith("/shorts")
assert channel_playlist_url("UCxyz1234567890abcdefghijk", "streams").endswith("/streams")
assert _created_at_from_entry({"upload_date": "20240511"}) is not None
assert _created_at_from_entry({"timestamp": 1_700_000_000}) is not None
assert _youtube_row_needs_enrich({"id": "x", "views": 1, "created_at": "2024-01-01"}) is True
assert _YOUTUBE_ENRICH_MAX == 24
assert _duration_string_from_sec(125) == "2:05"
