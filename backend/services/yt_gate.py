"""YouTube bot-gate cooldown — process-wide freeze for archive YouTube work.

IP-level gate signals ("Sign in to confirm you're not a bot", YouTube
rate-limits) freeze ALL YouTube jobs in the worker (chat backfills + YouTube
transcribes) for ``VODRIP_YT_GATE_FREEZE_SEC`` (default 1800) while
transcribe/events work on other platforms continues. Gated jobs are
REQUEUED by the worker, never failed — they drain once the cooldown lifts.

Separate module so ``archive_ytdlp`` (signal source: every guarded yt-dlp
extract) and ``archive_transcribe`` (consumer: job requeue decisions) share
one state without importing each other.

ponytail: state is per-process. The app + one detached worker can each see
the gate independently (correct for single-IP boxes — each process's own
requests trip it). Cross-process coordination would need a shared lock
file; not worth it while at most one worker runs.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    _GATE_FREEZE_SEC = max(60.0, float(os.environ.get("VODRIP_YT_GATE_FREEZE_SEC", "1800") or "1800"))
except ValueError:
    _GATE_FREEZE_SEC = 1800.0

# yt-dlp error markers meaning the IP/session is gated (not the video). The
# first three mirror ytdlp_hls._YT_SOFT_NEG_MARKERS; the last two are the
# archive path's rate-limit spellings (session rate-limited for up to an
# hour / plain 429).
_GATE_MARKERS = (
    "sign in to confirm",
    "not a bot",
    "preview unavailable for this video",
    "rate-limited by youtube",
    "too many requests",
)

_until = 0.0  # monotonic deadline of the freeze; 0 = not gated
_lock = threading.Lock()


def youtube_gate_active() -> bool:
    """True while the cooldown freeze is in effect."""
    return time.monotonic() < _until


def gate_remaining_sec() -> float:
    """Seconds until the freeze lifts (0 when inactive)."""
    return max(0.0, _until - time.monotonic())


def note_youtube_gate(reason: str, *, freeze_sec: Optional[float] = None) -> None:
    """Arm/extend the freeze (longest-wins). Logs the first arm of each run."""
    global _until
    with _lock:
        now = time.monotonic()
        new_until = now + (freeze_sec if freeze_sec is not None else _GATE_FREEZE_SEC)
        if new_until <= _until:
            return  # already frozen for longer — no state change
        _until = new_until
        logger.warning(
            "YouTube bot-gate cooldown until +%ds (%s)",
            int(new_until - now), reason,
        )


def clear_youtube_gate() -> None:
    """Lift the freeze (tests / operator escape hatch)."""
    global _until
    with _lock:
        _until = 0.0


def classify_youtube_gate_error(exc: BaseException) -> bool:
    """True when the exception text signals the IP-level YouTube gate."""
    msg = (str(exc) or "").lower()
    if any(m in msg for m in _GATE_MARKERS):
        return True
    try:
        # Canonical soft-negative classifier lives in ytdlp_hls (edited by
        # the HLS-fix owner) — reuse, don't duplicate. Lazy: ytdlp_hls is a
        # heavy import and the gate fires rarely.
        from services.ytdlp_hls import _youtube_soft_neg_error

        return _youtube_soft_neg_error(exc)
    except Exception:
        return False
