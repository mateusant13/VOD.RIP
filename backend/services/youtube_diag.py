"""INFO/WARNING logs for YouTube preview + download (visible without DEBUG)."""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("VOD.RIP.youtube")

_LAST_EXTRACT_SOURCE: dict[str, str] = {}


def auth_hint(session: Any = None) -> str:
    if session is None:
        return "anonymous"
    bits: list[str] = []
    if getattr(session, "cookie_file", None):
        bits.append("cookies_file")
    browser = getattr(session, "cookies_from_browser", None)
    if browser:
        bits.append(f"browser={browser}")
    if getattr(session, "po_token", None):
        bits.append("po_token")
    if getattr(session, "visitor_data", None):
        bits.append("visitor_data")
    return "+".join(bits) if bits else "anonymous"


def format_summary(info: Optional[dict]) -> str:
    if not info:
        return "formats=0"
    fmts = info.get("formats") or []
    heights = sorted({int(f.get("height") or 0) for f in fmts if f.get("height")})
    heights = [h for h in heights if h > 0]
    muxed = sum(
        1 for f in fmts
        if f.get("acodec") not in ("none", None) and f.get("vcodec") not in ("none", None)
    )
    hls = sum(1 for f in fmts if "m3u8" in str(f.get("protocol") or ""))
    dash = sum(
        1 for f in fmts
        if (f.get("url") or "").startswith("https://") and "m3u8" not in str(f.get("protocol") or "")
    )
    return (
        f"formats={len(fmts)} heights={heights[:10]} muxed={muxed} hls={hls} dash_https={dash}"
    )


def last_extract_source(video_id: str) -> str:
    return _LAST_EXTRACT_SOURCE.get(video_id, "")


def log_extract_ok(video_id: str, source: str, info: dict, session: Any = None) -> None:
    _LAST_EXTRACT_SOURCE[video_id] = source
    log.info(
        "extract ok video=%s source=%s auth=%s %s",
        video_id,
        source,
        auth_hint(session),
        format_summary(info),
    )


def youtube_user_message(exc: BaseException, *, preview: bool = False) -> str:
    """Sanitize YouTube errors for API/UI — never mention cookies or bot jargon."""
    low = str(exc).lower()
    if any(
        x in low
        for x in ("cookie", "blocked", "bot", "dpapi", "decrypt", "po_token", "sign in", "oauth")
    ):
        return (
            "Preview unavailable for this video — try again in a moment."
            if preview
            else "Could not load this YouTube video — try again in a moment."
        )
    if preview:
        return "Preview failed — try again."
    return "Could not load video info — try again."


assert "cookie" not in youtube_user_message(RuntimeError("cookie database locked")).lower()


def log_extract_fail(
    video_id: str,
    reason: str,
    session: Any = None,
    *,
    exc: Optional[BaseException] = None,
    detail: str = "",
    final: bool = False,
) -> None:
    msg = f"extract fail video={video_id} reason={reason} auth={auth_hint(session)}"
    if detail:
        msg = f"{msg} {detail}"
    sink = log.warning if final else log.debug
    if exc is not None:
        sink("%s: %s", msg, exc)
    else:
        sink(msg)


def log_preview_resolve(
    platform: str,
    kind: str,
    heights: list[int],
    *,
    custom_master: bool,
    entry_url: str,
) -> None:
    log.info(
        "preview resolve platform=%s kind=%s heights=%s synthetic_master=%s entry=%s",
        platform,
        kind,
        heights[:12],
        custom_master,
        (entry_url or "")[:120],
    )


def log_preview_session(
    session_id: str,
    platform: str,
    kind: str,
    heights: list[int],
    *,
    custom_master: bool,
    entry_url: str,
) -> None:
    log.info(
        "preview session=%s platform=%s kind=%s heights=%s synthetic_master=%s entry=%s",
        session_id[:8],
        platform,
        kind,
        heights[:12],
        custom_master,
        (entry_url or "")[:120],
    )


def log_preview_upstream(
    route: str,
    session_id: str,
    upstream_status: int,
    nbytes: int,
    ctype: str,
    upstream_url: str,
    *,
    note: str = "",
) -> None:
    suspicious = (
        upstream_status >= 400
        or (upstream_status == 200 and nbytes == 0)
        or (route.endswith("playlist") and nbytes > 0 and not upstream_url.lower().endswith((".m3u8", ".mp4", ".m4s", ".ts")))
    )
    msg = (
        f"preview {route} session={session_id[:8]} upstream_http={upstream_status} "
        f"bytes={nbytes} ctype={ctype or '-'} url={(upstream_url or '')[:120]}"
    )
    if note:
        msg = f"{msg} {note}"
    if suspicious:
        log.warning(msg)
    else:
        log.info(msg)


def log_download(
    download_id: str,
    event: str,
    *,
    url: str = "",
    platform: str = "",
    detail: str = "",
) -> None:
    log.info(
        "download id=%s event=%s platform=%s url=%s %s",
        download_id[:12],
        event,
        platform,
        (url or "")[:100],
        detail.strip(),
    )


assert auth_hint(None) == "anonymous"
