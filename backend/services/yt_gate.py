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

The DEADLINE is per-process, but the HISTORY is not: every arm/extend
appends a row to ``archive_db.rate_limit_events`` so the app can learn
*when* YouTube limits us and whether the request that tripped it was
background work or something a user is waiting on. That is what makes an
adaptive throttle possible later; today this module only records.
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

# Marker -> event class for the history table. The gate's default is
# 'bot_gate' (this module is only ever called for gate-classified errors);
# these refine it so a plain 429 and a ytdlp soft-negative don't pollute the
# "how often does YouTube bot-wall us" answer.
_KIND_MARKERS = (
    ("429", "http_429"),
    ("too many requests", "http_429"),
    ("rate-limited by youtube", "http_429"),
    ("403", "http_403"),
    ("captcha", "captcha"),
    ("preview unavailable for this video", "soft_neg"),
)


def _classify_gate_kind(reason: str) -> str:
    """Map an error string onto a rate_limit_events.kind class."""
    msg = (reason or "").lower()
    for marker, kind in _KIND_MARKERS:
        if marker in msg:
            return kind
    return "bot_gate"


def _record_history(
    reason: str, *, kind: str, surface: str, origin: str, backoff_s: float
) -> None:
    """Append the event to the durable history. Never raises.

    Lazy import: archive_db is a heavy module and this is a cold path, but
    the import itself can still fail in a half-initialized process — a
    network error handler must not become a crash.

    The load columns come from rl_counter, which counts this process's
    YouTube egress (ytdlp_guard's funnel). It answers None for a process
    that never issued a YouTube request, and None is written straight
    through: 'not measured' must never become 0, because a fabricated
    zero reads as a clean window and poisons the summary's mean/p95.
    """
    try:
        from services import archive_db, rl_counter

        archive_db.record_rate_limit(
            "youtube", kind,
            surface=surface, origin=origin,
            context=reason, backoff_s=backoff_s,
            recent_requests=rl_counter.recent_requests("youtube"),
            in_flight=rl_counter.in_flight("youtube"),
        )
    except Exception:  # noqa: BLE001 — instrumentation must never break a fetch
        logger.debug("YouTube rate-limit history not recorded", exc_info=True)


def youtube_gate_active() -> bool:
    """True while the cooldown freeze is in effect."""
    return time.monotonic() < _until


def gate_remaining_sec() -> float:
    """Seconds until the freeze lifts (0 when inactive)."""
    return max(0.0, _until - time.monotonic())


def note_youtube_gate(
    reason: str,
    *,
    freeze_sec: Optional[float] = None,
    surface: str = "other",
    origin: str = "auto",
) -> None:
    """Arm/extend the freeze (longest-wins). Logs the first arm of each run.

    *surface* (metadata/chat/captions/download/live-status/other) and
    *origin* ('auto' = background worker, 'user' = someone is waiting on
    it in the UI) are recorded, not acted on. Both are keyword-only with
    conservative defaults so every existing call site keeps its exact
    behaviour; the longest-wins early return is untouched, and the history
    write happens OUTSIDE _lock (it is a SQLite commit under archive_db's
    own global lock — never hold the gate's critical section across it).
    """
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
    _record_history(
        reason,
        kind=_classify_gate_kind(reason),
        surface=surface,
        origin=origin,
        backoff_s=new_until - now,
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
