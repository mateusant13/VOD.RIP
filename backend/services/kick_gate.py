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
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Union

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


def kick_gate_active() -> bool:
    """True while the cooldown/freeze is in effect (fail-fast window)."""
    return time.monotonic() < _until


def gate_remaining_sec() -> float:
    """Seconds until the cooldown/freeze lifts (0 when inactive)."""
    return max(0.0, _until - time.monotonic())


def note_kick_gate_event(reason: str) -> None:
    """Record a Cloudflare/rate-limit classification.

    Arms a short cooldown; on the Nth CONSECUTIVE event (no success in
    between) escalates to the long freeze (longest-wins). Logs the first
    arm of each run.
    """
    global _until, _consecutive
    with _lock:
        _consecutive += 1
        now = time.monotonic()
        if _consecutive >= _GATE_TRIP_COUNT:
            _until = max(_until, now + _GATE_FREEZE_SEC)
            _consecutive = 0
            logger.warning(
                "Kick Cloudflare/rate-limit gate frozen until +%ds (%s)",
                int(_GATE_FREEZE_SEC), reason,
            )
        else:
            _until = max(_until, now + _SHORT_COOLDOWN_SEC)
            logger.warning(
                "Kick Cloudflare/rate-limit cooldown until +%ds (%d/%d consecutive: %s)",
                int(_SHORT_COOLDOWN_SEC), _consecutive, _GATE_TRIP_COUNT, reason,
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
