"""Structured preview timing — logs to npm run dev / uvicorn console."""
from __future__ import annotations

import logging
import sys
from typing import Optional

logger = logging.getLogger("VOD.RIP.preview_timing")


def _safe_stdout_write(line: str) -> None:
    """Write a line to stdout in a way that survives non-UTF-8 consoles.

    On Windows the inherited console is typically cp1252, which cannot
    encode characters like the U+2192 arrow ("\u2192"). A bare ``print``
    raises ``UnicodeEncodeError`` and aborts the caller. Encode as UTF-8
    with replacement and write to the raw buffer, falling back to a
    lossy ``print`` if even that is unavailable.
    """
    try:
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            buf.write((line + "\n").encode("utf-8", "replace"))
            buf.flush()
            return
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        # Last-resort: drop unencodable chars and retry.
        try:
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


def _platform_label(platform: str) -> str:
    p = (platform or "").strip()
    if not p:
        return "unknown"
    low = p.lower()
    if low == "youtube":
        return "YouTube"
    if low == "kick":
        return "Kick"
    if low == "twitch":
        return "Twitch"
    return p


def log_preview_timing(
    *,
    platform: str,
    surface: str,
    event: str,
    open_ms: Optional[float] = None,
    seek_ms: Optional[float] = None,
    session_id: str = "",
    server_ms: Optional[float] = None,
    detail: str = "",
) -> None:
    """One grep-friendly line per milestone (dev console)."""
    parts = [
        "PREVIEW_TIMING",
        f"platform={_platform_label(platform)}",
        f"surface={surface or 'main'}",
        f"event={event}",
    ]
    if open_ms is not None:
        parts.append(f"open_ms={open_ms:.0f}")
    if seek_ms is not None:
        parts.append(f"seek_ms={seek_ms:.0f}")
    if server_ms is not None:
        parts.append(f"server_ms={server_ms:.0f}")
    sid = (session_id or "")[:8]
    if sid:
        parts.append(f"sid={sid}")
    if detail:
        parts.append(detail)
    line = " ".join(parts)
    logger.info(line)
    # ponytail: uvicorn dev config swallows child loggers — mirror to inherited stdout.
    # Use a console-safe writer so non-UTF-8 consoles (e.g. Windows cp1252) don't
    # raise UnicodeEncodeError on characters like the U+2192 arrow.
    _safe_stdout_write(line)


def log_server_session_created(
    session,
    *,
    resolve_ms: float,
) -> None:
    log_preview_timing(
        platform=getattr(session, "platform", ""),
        surface="server",
        event="session_created",
        server_ms=resolve_ms,
        session_id=getattr(session, "session_id", ""),
        detail=f"kind={getattr(session, 'kind', '?')} dash_window={getattr(session, 'dash_window_hls', False)}",
    )


def log_server_seg0_ready(session, *, since_create_ms: float) -> None:
    log_preview_timing(
        platform=getattr(session, "platform", ""),
        surface="server",
        event="seg0_ready",
        server_ms=since_create_ms,
        session_id=getattr(session, "session_id", ""),
    )


def log_server_seek_seg0(session, *, since_seek_ms: float, position_sec: float) -> None:
    log_preview_timing(
        platform=getattr(session, "platform", ""),
        surface="server",
        event="seek_seg0_ready",
        server_ms=since_seek_ms,
        session_id=getattr(session, "session_id", ""),
        detail=f"pos={position_sec:.1f}s",
    )


assert _platform_label("youtube") == "YouTube"
