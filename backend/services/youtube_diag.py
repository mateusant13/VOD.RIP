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
_AGE_GATE_MARKERS = (
    "sign in to confirm your age",
    "confirm your age",
    "age-restricted",
    "age restricted",
    "age-gate",
    "age gate",
    "age_verification_required",
    "inappropriate for some users",
)


def is_age_gate_error(exc: BaseException) -> bool:
    """True when the error is a definitive YouTube age gate (login required).

    Distinct from the transient bot gate ("Sign in to confirm you're not a
    bot") — retrying an age-gated video NEVER succeeds without logged-in
    cookies. Verified against yt-dlp 2026.07.04 + current wiki (2026-08-12):
    no anonymous player client (web, web_embedded, android_vr, web_safari)
    passes the age gate anymore; the app's cookie_bridge login flow is the
    only path.
    """
    low = str(exc).lower()
    return any(marker in low for marker in _AGE_GATE_MARKERS)


def youtube_http_status(exc: BaseException) -> int:
    """Map a sanitized YouTube error to an HTTP status code.

    Returns 403 for permanent member/permission errors, 404 for unavailable
    videos, 503 for transient bot/cookie/auth issues, 500 for unknown.
    """
    low = str(exc).lower()
    if is_age_gate_error(exc):
        return 403  # needs a logged-in account — retrying never helps
    if (
        "members-only content" in low
        or "join this channel" in low
        or "private video" in low
    ):
        return 403
    if (
        "video has been removed" in low
        or "video is not available" in low
        or "video is unavailable" in low
        or "video unavailable" in low
    ):
        return 404
    if any(
        x in low
        for x in (
            "cookie", "blocked", "bot", "dpapi", "decrypt", "po_token", "sign in",
            "preview unavailable",  # soft-gate chain collapse — transient, not 404
        )
    ):
        return 503
    return 500


def youtube_user_message(exc: BaseException, *, preview: bool = False) -> str:
    """Sanitize YouTube errors for API/UI — never mention cookies or bot jargon."""
    low = str(exc).lower()
    # Definitive age gate — retrying never helps without a logged-in account;
    # the app's YouTube sign-in flow (cookie_bridge) is the only unlock.
    if is_age_gate_error(exc):
        return (
            "This video is age-restricted — sign in to YouTube to watch it."
            if preview
            else "This video is age-restricted — sign in to YouTube to download it."
        )
    if any(
        x in low
        for x in (
            "cookie", "blocked", "bot", "dpapi", "decrypt", "po_token", "sign in",
            "preview unavailable",  # ytdlp_hls soft-gate collapse (bot-gated box)
        )
    ):
        return (
            "YouTube preview is temporarily restricted. Try again in a few minutes."
            if preview
            else "Could not load this YouTube video — try again in a moment."
        )
    # Definitive unplayable states — retrying will never help; say so.
    if "members-only content" in low or "join this channel" in low:
        return "Members-only video — requires channel membership."
    if "private video" in low:
        return "This video is private."
    if (
        "video has been removed" in low
        or "video is not available" in low
        or "video is unavailable" in low
        or "video unavailable" in low
    ):
        return "This video is unavailable."
    if preview:
        return "Preview failed — try again."
    return "Could not load video info — try again."


assert "restricted" in youtube_user_message(RuntimeError("cookie database locked"), preview=True).lower()
assert "restricted" in youtube_user_message(RuntimeError("Sign in to confirm you're not a bot"), preview=True).lower()
# Age gate is definitive (needs login, 403, explicit message) — bot gate stays
# transient (503, "try again"). Verified 2026-08-12 against yt-dlp 2026.07.04:
# no anonymous client passes the age gate, so retry messaging would be a lie.
assert is_age_gate_error(RuntimeError("Sign in to confirm your age"))
assert is_age_gate_error(RuntimeError("This video is age-restricted"))
assert not is_age_gate_error(RuntimeError("Sign in to confirm you're not a bot"))
assert not is_age_gate_error(RuntimeError("This video is unavailable"))
assert "age-restricted" in youtube_user_message(RuntimeError("Sign in to confirm your age"), preview=True).lower()
assert "age-restricted" in youtube_user_message(RuntimeError("This video is age-restricted"), preview=False).lower()
assert "try again" not in youtube_user_message(RuntimeError("Sign in to confirm your age"), preview=False).lower()
# Transient gate collapses map to 503, definitive dead-video messages to 404 —
# a soft "Video unavailable" must never surface as a hard 404.
assert youtube_http_status(RuntimeError("YouTube preview unavailable for this video")) == 503
assert youtube_http_status(RuntimeError("Sign in to confirm you're not a bot")) == 503
assert youtube_http_status(RuntimeError("Sign in to confirm your age")) == 403
assert youtube_http_status(RuntimeError("This video is unavailable")) == 404
assert youtube_http_status(RuntimeError("This video has been removed by the uploader")) == 404
assert youtube_http_status(RuntimeError("members-only content")) == 403


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
