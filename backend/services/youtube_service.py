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


def _enrich_youtube_channel_rows(rows: list[dict[str, Any]]) -> None:
    """Fill missing date/views via lightweight InnerTube (flat playlist is spotty)."""
    need = [r for r in rows if not r.get("created_at") or r.get("views") is None]
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
        futs = [pool.submit(_fetch, vid) for vid in list(by_id)[:50]]
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
            if not row.get("created_at") and meta.get("created_at"):
                row["created_at"] = meta["created_at"]
            if row.get("views") is None and meta.get("views") is not None:
                row["views"] = meta["views"]
            if not row.get("duration") and meta.get("duration"):
                row["duration"] = meta["duration"]


def list_channel_videos_sync(
    channel_ref: str,
    limit: int = 50,
    *,
    playlist: PlaylistKind = "videos",
) -> list[dict[str, Any]]:
    import yt_dlp

    from services.ytdlp_guard import guarded_youtube_dl
    from services.youtube_session import (
        resolve_ytdlp_cookiefile,
        youtube_session_from_settings,
        ytdlp_extractor_args,
    )

    url = channel_playlist_url(channel_ref, playlist)
    session = youtube_session_from_settings()
    try:
        from deps import settings_mgr
        auto_auth = getattr(settings_mgr.get(), "youtube_auto_auth", True)
    except Exception:
        auto_auth = True
    ext_args = ytdlp_extractor_args(session, auto_auth=auto_auth)
    opts: dict[str, Any] = {
        "playlistend": max(1, min(int(limit), 100)),
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

    apply_ytdlp_cookie_opts(opts, session, auto_auth=auto_auth)
    with guarded_youtube_dl(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = (info or {}).get("entries") or []
    content_kind = _content_kind_for_playlist(playlist)
    out: list[dict[str, Any]] = []
    for e in entries:
        if not e:
            continue
        vid = (e.get("id") or "").strip()
        if not vid:
            continue
        if playlist == "shorts":
            webpage = f"https://www.youtube.com/shorts/{vid}"
        else:
            webpage = e.get("url") or f"https://www.youtube.com/watch?v={vid}"
        if not str(webpage).startswith("http"):
            webpage = f"https://www.youtube.com/watch?v={vid}"
        created_at = _created_at_from_entry(e)
        thumb = e.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
        dur = e.get("duration")
        dur_str = None
        if dur is not None:
            try:
                sec = int(float(dur))
                m, s = divmod(sec, 60)
                h, m = divmod(m, 60)
                dur_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
            except (TypeError, ValueError):
                pass
        out.append({
            "id": vid,
            "platform": "YouTube",
            "title": e.get("title") or "Untitled",
            "duration": dur,
            "duration_string": dur_str,
            "created_at": created_at,
            "views": e.get("view_count"),
            "thumbnail_url": thumb,
            "url": webpage,
            "channel": e.get("channel") or e.get("uploader") or channel_ref,
            "content_kind": content_kind,
        })
    _enrich_youtube_channel_rows(out)
    return out


assert channel_playlist_url("cellbit", "videos").endswith("/videos")
assert channel_playlist_url("@cellbit", "shorts").endswith("/shorts")
assert channel_playlist_url("UCxyz1234567890abcdefghijk", "streams").endswith("/streams")
assert _created_at_from_entry({"upload_date": "20240511"}) is not None
assert _created_at_from_entry({"timestamp": 1_700_000_000}) is not None
