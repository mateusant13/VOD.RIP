"""Kick Cloudflare/rate-limit gate — process-wide freeze for Kick work.

Kick's public JSON API (curl_cffi impersonation) is served behind
Cloudflare, which classifies the IP with 403 blocks and rate-limits with
429s. A single 403 arms a short per-process cooldown; N consecutive
classified events (403s, or 429 runs that exhausted the retry loop) freeze
ALL Kick requests for ``VODRIP_KICK_GATE_FREEZE_SEC`` (default 1800).
While frozen, ``kick_api_service._get_json`` fails fast with a clear error
instead of hammering Cloudflare, and the archive download path requeues the
job (never fails it) so it drains once the cooldown lifts.

Separate module so ``kick_api_service`` (signal source: every Kick request)
and ``archive_kick`` (consumer: retry/requeue decisions) share one state
without importing each other.

ponytail: state is per-process. The app + one detached worker can each see
the gate independently (correct for single-IP boxes — each process's own
requests trip it). Cross-process coordination would need a shared lock
file; not worth it while at most one worker runs.

The DEADLINE is per-process, but the HISTORY is not: every classified
event appends a row to ``archive_db.rate_limit_events`` so the app can
learn *when* Kick limits us and whether the request that tripped it was
background work or something a user is waiting on. That is what makes an
adaptive throttle possible later; today this module only records.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional, Union

logger = logging.getLogger(__name__)

try:
    _GATE_FREEZE_SEC = max(60.0, float(os.environ.get("VODRIP_KICK_GATE_FREEZE_SEC", "1800") or "1800"))
except ValueError:
    _GATE_FREEZE_SEC = 1800.0

# One classified event (403, or an exhausted 429 retry run) arms a short
# cooldown so a flaky request doesn't spin; only CONSECUTIVE events (no
# successful request in between) escalate to the long freeze.
_SHORT_COOLDOWN_SEC = 60.0
_GATE_TRIP_COUNT = 3

# Exception/message markers meaning the failure is TRANSIENT at the Kick
# layer (Cloudflare block, rate limit, transport flake) — the archive
# download path retries once, then requeues rather than marking 'failed'.
_TRANSIENT_MARKERS = (
    "kickgateerror",      # frozen / 403-classified (kick_api_service.KickGateError)
    "kickratelimiterror", # 429 retries exhausted (kick_api_service.KickRateLimitError)
    "429", "too many requests", "rate limit",
    "timeout", "timed out", "operation timed out",
    "cloudflare", "connection reset", "connection aborted", "connection refused",
    "could not resolve", "could not connect", "failed to connect", "curl error",
    "cooldown", "frozen",
)

_until = 0.0        # monotonic deadline of the cooldown/freeze; 0 = not gated
_consecutive = 0    # classified events since the last successful request
_lock = threading.Lock()

# Reason -> event class for the history table. The signal source
# (kick_api_service._get_json) knows the exact status and passes `kind`
# explicitly; this is the fallback for callers that only carry a message.
_KIND_MARKERS = (
    ("429", "http_429"),
    ("rate-limit", "http_429"),
    ("too many requests", "http_429"),
    ("403", "http_403"),
    ("cloudflare", "http_403"),
    ("captcha", "captcha"),
)


def _classify_gate_kind(reason: str) -> str:
    """Map an error string onto a rate_limit_events.kind class."""
    msg = (reason or "").lower()
    for marker, kind in _KIND_MARKERS:
        if marker in msg:
            return kind
    return "other"


def _record_history(
    reason: str, *, kind: str, surface: str, origin: str, backoff_s: float
) -> None:
    """Append the event to the durable history. Never raises.

    Lazy import: archive_db is a heavy module and this is a cold path, but
    the import itself can still fail in a half-initialized process — a
    Cloudflare 403 handler must not become a crash.

    The load columns come from rl_counter, which counts this process's
    Kick egress (kick_api_service._get_json is the single funnel). It
    answers None for a process that never issued a Kick request, and None
    is written straight through: 'not measured' must never become 0,
    because a fabricated zero reads as a clean window and poisons the
    summary's mean/p95.
    """
    try:
        from services import archive_db, rl_counter

        archive_db.record_rate_limit(
            "kick", kind,
            surface=surface, origin=origin,
            context=reason, backoff_s=backoff_s,
            recent_requests=rl_counter.recent_requests("kick"),
            in_flight=rl_counter.in_flight("kick"),
        )
    except Exception:  # noqa: BLE001 — instrumentation must never break a request
        logger.debug("Kick rate-limit history not recorded", exc_info=True)


def kick_gate_active() -> bool:
    """True while the cooldown/freeze is in effect (fail-fast window)."""
    return time.monotonic() < _until


def gate_remaining_sec() -> float:
    """Seconds until the cooldown/freeze lifts (0 when inactive)."""
    return max(0.0, _until - time.monotonic())


def note_kick_gate_event(
    reason: str,
    *,
    kind: Optional[str] = None,
    surface: str = "metadata",
    origin: str = "auto",
) -> None:
    """Record a Cloudflare/rate-limit classification.

    Arms a short cooldown; on the Nth CONSECUTIVE event (no success in
    between) escalates to the long freeze (longest-wins). Logs the first
    arm of each run.

    *kind* is the exact event class when the caller knows it (the 403 and
    the exhausted-429 branches of kick_api_service do); otherwise it is
    derived from the reason text. *surface* and *origin* are recorded, not
    acted on — keyword-only with defaults, so existing call sites keep
    their exact behaviour. The history write happens OUTSIDE _lock (a
    SQLite commit under archive_db's own global lock must never be held
    across the gate's critical section).
    """
    global _until, _consecutive
    with _lock:
        _consecutive += 1
        now = time.monotonic()
        if _consecutive >= _GATE_TRIP_COUNT:
            _until = max(_until, now + _GATE_FREEZE_SEC)
            _consecutive = 0
            backoff_s = _GATE_FREEZE_SEC
            logger.warning(
                "Kick Cloudflare/rate-limit gate frozen until +%ds (%s)",
                int(_GATE_FREEZE_SEC), reason,
            )
        else:
            _until = max(_until, now + _SHORT_COOLDOWN_SEC)
            backoff_s = _SHORT_COOLDOWN_SEC
            logger.warning(
                "Kick Cloudflare/rate-limit cooldown until +%ds (%d/%d consecutive: %s)",
                int(_SHORT_COOLDOWN_SEC), _consecutive, _GATE_TRIP_COUNT, reason,
            )
    _record_history(
        reason,
        kind=(kind or _classify_gate_kind(reason)),
        surface=surface,
        origin=origin,
        backoff_s=backoff_s,
    )


def note_kick_success() -> None:
    """A request got through — reset the consecutive-classification streak."""
    global _consecutive
    with _lock:
        _consecutive = 0


def clear_kick_gate() -> None:
    """Lift the cooldown/freeze (tests / operator escape hatch)."""
    global _until, _consecutive
    with _lock:
        _until = 0.0
        _consecutive = 0


def classify_transient_kick_error(exc: Union[BaseException, str]) -> bool:
    """True when the error text signals a transient Kick-layer failure.

    Used by the archive download path to decide retry-once-then-requeue vs
    terminal 'failed'. Marker-based on purpose: the archive layer receives
    download failures as "{TypeName}: {message}" strings.
    """
    msg = (exc if isinstance(exc, str) else str(exc) or "").lower()
    return any(m in msg for m in _TRANSIENT_MARKERS)
