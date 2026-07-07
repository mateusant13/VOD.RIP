"""YouTube channel listings via yt-dlp flat playlists (ponytail: no YouTube Data API key)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal

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
        "extract_flat": "in_playlist",
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
        upload_date = e.get("upload_date")
        created_at = None
        if upload_date and len(str(upload_date)) == 8:
            try:
                created_at = datetime.strptime(
                    str(upload_date), "%Y%m%d",
                ).replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                pass
        thumb = e.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
        out.append({
            "id": vid,
            "platform": "YouTube",
            "title": e.get("title") or "Untitled",
            "duration": e.get("duration"),
            "duration_string": None,
            "created_at": created_at,
            "views": e.get("view_count"),
            "thumbnail_url": thumb,
            "url": webpage,
            "channel": e.get("channel") or e.get("uploader") or channel_ref,
            "content_kind": content_kind,
        })
    return out


assert channel_playlist_url("cellbit", "videos").endswith("/videos")
assert channel_playlist_url("@cellbit", "shorts").endswith("/shorts")
assert channel_playlist_url("UCxyz1234567890abcdefghijk", "streams").endswith("/streams")
