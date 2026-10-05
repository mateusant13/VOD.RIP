"""Adaptive request-rate governor: a learned per-platform ceiling with
AUTO/USER token pools underneath it.

Why predictive, not header-driven
--------------------------------
This app never sees a proactive rate-limit header. There is no Twitch Helix
traffic (so no ``Ratelimit-Remaining``), YouTube sends no quota header, and
Kick's ``Retry-After`` presence is unverified. Every signal we get is
*reactive* — a 429 after the fact. So the ceiling here is learned from our own
observed history: how many requests we actually issued, over what interval,
before the limiter fired. Nothing in this module parses a header.

The governor is a PREDICTIVE layer IN FRONT of ``yt_gate`` / ``kick_gate``,
which remain the last-resort reactive backstop and are unchanged. This module
never freezes anything, never sleeps on its own, and never raises: callers ask
whether they may proceed and decide what to do about it.

The ceiling update rule (asymmetric on purpose)
----------------------------------------------
A rate limit is expensive (a 30 min freeze, a requeue, a stalled preview);
under-using the API is nearly free. So:

* **Down fast.** On a limit event we compute the rate we were actually
  travelling at when we got limited (``requests_since_event`` over the window
  that just closed) and aim at ``SAFETY_FRACTION`` of it. We keep the
  *smallest* trip rate ever seen as the frontier — the least aggressive rate
  that is known to have been punished is the best evidence of where the real
  wall is. An event can only ever lower the ceiling, never raise it.
* **Up slowly.** After ``CLEAN_WINDOW_S`` with no event, the ceiling grows by
  ``UP_FACTOR`` per window (1.05, i.e. ~+5% per 5 min), with catch-up steps
  capped so a long idle does not spring back to a rate we were never proved
  safe at.
* **Cold start is conservative** — see ``_PLATFORM_CEILING_RPM``. We do not
  assume a high rate for a platform we have never been limited on.

Two pools, one shared ceiling — the on-demand reservation
--------------------------------------------------------
The ceiling is shared; the *capacity* is split:

    AUTO  bucket  capacity/refill =  0.70 * ceiling
    USER  bucket  capacity/refill =  1.00 * ceiling

AUTO can never hold or regenerate more than 70% of the ceiling, so a
background storm structurally cannot consume the on-demand reservation no
matter how hard it runs — the headroom USER needs is never *spent*, only left
unspent. USER is checked first at consumption and is never hard-refused (a
user waiting on a preview must not be blocked by a budget), it just gets an
honest ``user_exhausted`` decision in the log.

This is the whole mechanism the user asked for: background work paces itself
*before* the limit instead of discovering it with a 429.

Learning counts TOTAL requests; the pools split *capacity*. The limiter
counts every request on the IP regardless of who sent it, so the trip rate
must be measured on the total. The reservation is about who gets to spend the
budget, not about pretending background traffic is invisible.

What the governor learns from — and why the loop cannot run away
----------------------------------------------------------------
A history row written here is an **observed platform trip**, never one of our
own admission decisions. ``note_limit`` is called from exactly one kind of
place: a caller that just received a real ``status=429`` (twitch_gql_service,
kick_api_service, archive_twitch, youtube_innertube). The governor pacing
itself — ``auto_exhausted`` / ``user_exhausted`` out of ``acquire`` — never
calls ``note_limit`` and never writes a row; it goes to the in-memory decision
ring only. So the 70% figure is applied to a *measurement of the platform's
wall*, not to an echo of our own budgeting. That is the decision, and it is
why this loop is not the "governor grades its own homework" failure.

There IS still a genuine self-coupling, and it is stated here rather than
assumed away: the ceiling sets the rate we admit, the admitted rate is what we
measure, and that measurement feeds the ceiling. Two properties bound it.

* **It only ever closes on a real 429.** No trip, no row, no coupling.
* **It cannot run away downward.** ``prime_from_history`` lowers only
  (``if target < st.ceiling_rpm``) and never below ``_FLOOR_CEILING_RPM``, so
  a long run of low-rate trips settles at the floor and stops. It cannot run
  away upward either: ``_recent_requests_value`` bounds the stored reading by
  the number of requests actually issued, so no fabricated burst can teach the
  history a rate we never travelled at and switch the throttle off.

Test ``test_learned_ceiling_cannot_ratchet_past_the_floor`` in
``test_rate_budget_persist_recent_requests.py`` is the guard on the first
property; ``..._never_claims_a_load_it_did_not_produce`` guards the second.

Concurrency
-----------
Each platform owns its own ``threading.Lock``. No lock is ever shared between
platforms, so a Twitch stall (or a slow refill) can never serialise Kick or
YouTube. The decision log has its own short-lived lock and is only touched on
throttles, not on every request. No lock is held across DB IO — persistence
happens after the platform lock is released.

Cross-process
-------------
Like ``yt_gate``/``kick_gate``, the buckets are per-process (the app and the
detached worker each hold their own). What crosses the process boundary is
*learning*, not pacing: limit events are persisted through ``archive_db`` (the
one store both processes already share) and seeded back in by
``prime_from_history()``. If the persistence layer is unavailable — e.g. the
``agent/rl-history`` lane has not landed yet — every call degrades to a
no-op and the governor runs purely on in-process state. It never raises
because of a missing dependency.

Cost
----
Hot paths (HLS segments: 12 parallel fetchers, ~2,400 calls for a 4h VOD)
call ``note_hot_call``, which bumps bucketed counters and writes **no rows**
and takes no token. Rows are written only for limit *events*, which are rare
by definition — that is the data the learning is made of.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

Source = Literal["auto", "user"]

# --- learning tuning ---------------------------------------------------------
# Cold-start ceilings are requests/minute, deliberately low: a wrong guess
# upward costs a 30 min freeze, a guess downward costs nothing but patience.
_PLATFORM_CEILING_RPM: Dict[str, float] = {
    "youtube": 20.0,
    "twitch": 30.0,
    "kick": 20.0,
}
_DEFAULT_CEILING_RPM = 10.0  # any platform we have not calibrated yet
_FLOOR_CEILING_RPM = 4.0     # never learn below this, however many events
_MAX_CEILING_RPM = 120.0     # never climb above this

_SAFETY_FRACTION = 0.70      # aim at 70% of the rate that actually tripped us
_UP_FACTOR = 1.05            # ~+5% per clean window
_CLEAN_WINDOW_S = 300.0      # 5 min event-free before one up-step
_MAX_CATCHUP_STEPS = 4       # bounded spring-back after a long idle

# The history table records `requests_at_limit_*` as a COUNT of requests
# issued before the limiter fired, not a per-minute rate. When no request
# counter is running (observed_rate_per_min is NULL) we can only guess the
# window that count spanned. 60 min is the conservative guess: it assumes the
# count was accumulated over an hour, which yields the SMALLEST defensible rpm
# from a given count. A count is a hint that may only ever lower a ceiling,
# never raise it, so erring small is the safe direction. Set to 0 to disable
# the count fallback entirely and trust only a measured rate.
_HISTORY_COUNT_WINDOW_MIN = 60.0

# --- pool split --------------------------------------------------------------
AUTO_SHARE = 0.70            # AUTO may hold/regenerate this much of the ceiling
_MIN_POOL_TOKENS = 2.0       # always allow a small burst so a page fetch is
                             # never starved by a fractional bucket

# --- backoff bound -----------------------------------------------------------
MAX_AUTO_WAIT_S = 30.0       # longest a background caller should ever be told
                             # to wait; it proceeds after this (never errors)

_DECISION_LOG_MAX = 200
_HOT_WINDOW_S = 60.0         # counters roll up per minute

_clock: Callable[[], float] = time.monotonic


def set_clock(fn: Callable[[], float]) -> None:
    """Inject the time source (tests). All pacing maths reads this."""
    global _clock
    _clock = fn


def _now() -> float:
    return _clock()


@dataclass(frozen=True)
class Decision:
    """Outcome of one admission check. Cheap to construct, JSON-ready."""
    platform: str
    source: str
    allowed: bool
    wait_s: float
    ceiling_rpm: float
    tokens: float
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "source": self.source,
            "allowed": self.allowed,
            "wait_s": round(self.wait_s, 3),
            "ceiling_rpm": round(self.ceiling_rpm, 3),
            "tokens": round(self.tokens, 3),
            "reason": self.reason,
        }


@dataclass
class _PlatformState:
    """Per-platform governor state. Guarded by this platform's own lock."""
    platform: str
    ceiling_rpm: float
    default_rpm: float
    # learning
    requests_since_event: int = 0
    window_start: float = 0.0
    last_event_ts: float = 0.0
    last_ramp_ts: float = 0.0
    trip_rpm: float = 0.0
    min_trip_rpm: float = 0.0
    events: int = 0
    # pools
    auto_tokens: float = 0.0
    auto_refill_ts: float = 0.0
    user_tokens: float = 0.0
    user_refill_ts: float = 0.0
    # hot-path counters (bucketed, never rows)
    hot_calls: int = 0
    hot_limited: int = 0
    hot_window_start: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


_states: Dict[str, _PlatformState] = {}
_registry_lock = threading.Lock()
_log_lock = threading.Lock()
_decisions: Deque[Dict[str, Any]] = deque(maxlen=_DECISION_LOG_MAX)
_prime_lock = threading.Lock()
_history_primed = False


def _default_rpm(platform: str) -> float:
    return _PLATFORM_CEILING_RPM.get(platform, _DEFAULT_CEILING_RPM)


def _state(platform: str) -> _PlatformState:
    platform = (platform or "unknown").strip().lower() or "unknown"
    st = _states.get(platform)
    if st is not None:
        return st
    with _registry_lock:
        st = _states.get(platform)
        if st is None:
            st = _PlatformState(
                platform=platform,
                ceiling_rpm=_default_rpm(platform),
                default_rpm=_default_rpm(platform),
                window_start=_now(),
                last_ramp_ts=_now(),
                hot_window_start=_now(),
            )
            _states[platform] = st
        return st


def _coerce_source(source: Any) -> str:
    """Unknown/absent origin is treated as AUTO — the conservative pool.

    Serving background work out of the on-demand reservation is the failure
    mode we care about; a mislabelled user request merely gets paced like
    background work for one request.
    """
    s = str(source or "").strip().lower()
    return s if s in ("auto", "user") else "auto"


# --- capacity ----------------------------------------------------------------

def _auto_capacity(ceiling: float) -> float:
    return max(_MIN_POOL_TOKENS, ceiling * AUTO_SHARE)


def _user_capacity(ceiling: float) -> float:
    return max(_MIN_POOL_TOKENS, ceiling)


def _refill(st: _PlatformState, now: float) -> None:
    """Fill both buckets toward capacity. Caller holds the platform lock."""
    auto_cap = _auto_capacity(st.ceiling_rpm)
    user_cap = _user_capacity(st.ceiling_rpm)
    auto_rate = st.ceiling_rpm * AUTO_SHARE
    user_rate = st.ceiling_rpm

    if now > st.auto_refill_ts:
        st.auto_tokens = min(auto_cap, st.auto_tokens + auto_rate * (now - st.auto_refill_ts) / 60.0)
    st.auto_refill_ts = now
    if now > st.user_refill_ts:
        st.user_tokens = min(user_cap, st.user_tokens + user_rate * (now - st.user_refill_ts) / 60.0)
    st.user_refill_ts = now


def _seconds_for_one_token(rate_per_min: float) -> float:
    return 60.0 / rate_per_min if rate_per_min > 0 else MAX_AUTO_WAIT_S


# --- ceiling learning --------------------------------------------------------

def _ramp_up(st: _PlatformState, now: float) -> None:
    """Slow recovery. Caller holds the platform lock."""
    if st.events == 0:
        return
    since_ramp = now - st.last_ramp_ts
    if since_ramp < _CLEAN_WINDOW_S:
        return
    steps = int(since_ramp // _CLEAN_WINDOW_S)
    if steps <= 0:
        return
    steps = min(steps, _MAX_CATCHUP_STEPS)
    st.last_ramp_ts = now
    st.ceiling_rpm = min(_MAX_CEILING_RPM, st.ceiling_rpm * (_UP_FACTOR ** steps))


def _record_trip(st: _PlatformState, now: float) -> None:
    """Fold the window that just closed into the frontier estimate."""
    elapsed = now - st.window_start
    if elapsed < 1.0:
        elapsed = 1.0  # avoid a nonsense rate from a sub-second window
    if st.requests_since_event > 0:
        trip = (st.requests_since_event / elapsed) * 60.0
        st.trip_rpm = trip
        st.min_trip_rpm = trip if st.min_trip_rpm <= 0 else min(st.min_trip_rpm, trip)
    st.requests_since_event = 0
    st.window_start = now


# --- persistence (agent/rl-history contract) --------------------------------
# The history lane is expected to provide these on services.archive_db:
#   record_rate_limit(platform, kind, *, surface, origin, context,
#                     backoff_s, recent_requests)
#   rate_limit_summary(platform=None, since_hours=24)  -> per-platform/origin
#     aggregate
# They may not exist yet; every use is guarded so the governor still runs.


def _recent_requests_value(requests: int, trip_rpm: float) -> Optional[int]:
    """`recent_requests` for an event the governor wrote itself. Unit bridge.

    THE UNIT IS NOT OURS TO PICK. The column is documented as rl_counter's
    "requests in the trailing 60 s window" (rl_counter.py:63-67, BUCKET_SEC=10,
    WINDOW_SEC=60), which is ALREADY a requests-per-minute number, and
    ``rate_limit_summary`` means the column to produce ``observed_rate_per_min``
    (archive_db.py:3399-3402). yt_gate/kick_gate therefore store a
    trailing-60s COUNT. A row written here lands in the SAME
    ``(platform, origin)`` group as those rows and is averaged into the same
    mean, so it must carry the same quantity or it poisons the group.

    Our observation window runs from the first request after the last event to
    this one, so it is NOT 60 s long and the raw ``requests`` count must never
    be stored as-is: over a 20-minute window a count of 400 would be read as
    400 rpm, and the group mean — and with it the learned ceiling — would be
    off by the window length. The trailing-60s count is bounded from both sides
    and the two bounds are the two obvious candidates:

    * window SHORTER than 60 s -> the count itself; every request we issued is
      inside the last minute. A 31-request burst inside 0.4 s is 31 requests
      in the last minute, NOT the 1860 rpm that ``_record_trip``'s one-second
      floor extrapolates it to. Storing that extrapolation would tell the
      history "we tripped at 1860 rpm, stop throttling" — a runaway in the
      direction that DISABLES the very protection this module exists for.
    * window LONGER than 60 s -> the window's own average rate, our best
      estimate of the last minute's share of it.

    Taking the smaller satisfies both branches at once. It is also the reading
    that cannot run away upward: the value is never larger than the number of
    requests we really did issue, so history can never claim a load this
    process did not produce.

    Returns None — never 0 — when the window measured nothing, or when the
    estimate is below one-request resolution. An unmeasured load must not
    become a fabricated zero: 0 is a real reading of "a clean window", it
    drags the group mean down and ratchets the ceiling to the floor. The
    governor's own doctrine, and yt_gate/kick_gate's ("None is written
    straight through: 'not measured' must never become 0").
    """
    if requests is None or requests <= 0:
        return None
    if trip_rpm is None or not math.isfinite(trip_rpm):
        return None
    value = int(round(min(float(requests), trip_rpm)))
    return value if value > 0 else None


def _persist_event(platform: str, kind: str, origin: str, ceiling_rpm: float,
                   trip_rpm: float, events: int,
                   recent_requests: Optional[int] = None) -> None:
    try:
        from services import archive_db

        fn = getattr(archive_db, "record_rate_limit", None)
        if fn is None:
            logger.debug("rate_budget: archive_db.record_rate_limit absent — event not persisted")
            return
        fn(
            platform,
            kind or "governor",
            surface=platform,
            origin=origin,
            context=f"ceiling_rpm={ceiling_rpm:.2f} trip_rpm={trip_rpm:.2f} events={events}",
            recent_requests=recent_requests,
        )
    except Exception:  # noqa: BLE001 — persistence is best-effort, never fatal
        logger.debug("rate_budget: record_rate_limit failed", exc_info=True)


def _read_history() -> List[Dict[str, Any]]:
    """Per-(platform, origin) summary rows from the shared history table.

    The shape matters. archive_db.rate_limit_summary() returns
    ``{generated_at, since_hours, totals, groups}`` where ``groups`` is the
    LIST of per-(platform, origin) dicts. Iterating the dict's .values()
    instead yields 'totals' and 'groups' — neither is a platform row, so
    every lookup missed and cross-process priming silently never happened.
    """
    try:
        from services import archive_db

        fn = getattr(archive_db, "rate_limit_summary", None)
        if fn is None:
            return []
        payload = fn(since_hours=24)
    except Exception:  # noqa: BLE001
        logger.debug("rate_budget: rate_limit_summary failed", exc_info=True)
        return []
    if isinstance(payload, dict):
        rows = payload.get("groups") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        return []
    return [r for r in rows if isinstance(r, dict)]


def prime_from_history(platform: Optional[str] = None) -> Dict[str, Any]:
    """Seed ceilings from cross-process history. Idempotent, best-effort.

    This is how learning crosses the process boundary: the app and the
    detached worker each keep private buckets, but both read the same DB, so
    a limit the worker took teaches the app. Only ever LOWERS a ceiling.
    """
    applied: Dict[str, float] = {}
    for row in _read_history():
        plat = str(row.get("platform") or "").strip().lower()
        if not plat or (platform and plat != platform):
            continue
        # The history lane names these differently from this module's own
        # vocabulary. Prefer a real observed RATE when one exists (the
        # request counter makes it non-null); otherwise fall back to the
        # p95 of requests-at-limit, which is a COUNT over an unstated window
        # and is only a ceiling hint, never a measured rpm. The legacy names
        # are kept last so a caller supplying this module's own vocabulary
        # still works.
        trip = row.get("observed_rate_per_min")
        source = "observed_rate_per_min"
        if trip is None:
            trip = row.get("requests_at_limit_p95")
            source = "requests_at_limit_p95(count,not-a-rate)"
        if trip is None:
            trip = row.get("requests_at_limit_mean")
            source = "requests_at_limit_mean(count,not-a-rate)"
        if trip is None:
            # This module's own vocabulary (already a rate, no rescale).
            for legacy in ("min_trip_rpm", "trip_rpm", "rate_rpm"):
                if row.get(legacy) is not None:
                    trip, source = row[legacy], legacy
                    break
        try:
            trip_rpm = float(trip)
        except (TypeError, ValueError):
            trip_rpm = 0.0
        if source.endswith("(count,not-a-rate)"):
            # A raw request count is not a per-minute rate. Treating it as one
            # would set the ceiling absurdly high (a 24h count read as rpm)
            # and defeat the whole point. Scale it by a conservative assumed
            # sustained window and record the assumption.
            window = _HISTORY_COUNT_WINDOW_MIN or 0.0
            trip_rpm = (trip_rpm / window) if window else 0.0
            logger.debug(
                "rate_budget: %s priming %s from %s over an assumed %s-minute window",
                plat, source, trip, window,
            )
        if trip_rpm <= 0:
            continue
        target = max(_FLOOR_CEILING_RPM, trip_rpm * _SAFETY_FRACTION)
        st = _state(plat)
        with st.lock:
            if target < st.ceiling_rpm:
                st.min_trip_rpm = st.min_trip_rpm or trip_rpm
                st.ceiling_rpm = target
                st.last_ramp_ts = _now()
                applied[plat] = round(target, 2)
    if applied:
        logger.info("rate_budget: primed ceilings from history: %s", applied)
    return applied


def _maybe_prime() -> None:
    global _history_primed
    if _history_primed:
        return
    if str(os.environ.get("VODRIP_RATE_BUDGET_PRIME", "1")).strip() == "0":
        _history_primed = True
        return
    with _prime_lock:
        if _history_primed:
            return
        _history_primed = True  # set first: a failure must not retry per request
        try:
            prime_from_history()
        except Exception:  # noqa: BLE001
            logger.debug("rate_budget: prime_from_history failed", exc_info=True)


# --- public API --------------------------------------------------------------

def acquire(platform: str, source: Source = "auto", *, kind: Optional[str] = None) -> Decision:
    """Admit one request and learn from it. The instrumented hot seam.

    ``source`` is the origin tag ("auto" for scheduled/background work,
    "user" for preview / manual transcribe / clip). Anything unrecognised is
    coerced to "auto" — the conservative pool.

    Never raises and never sleeps. AUTO callers that get ``allowed=False``
    should back off for at most ``min(wait_s, MAX_AUTO_WAIT_S)`` and then
    proceed; USER callers are never expected to wait at all.
    """
    _maybe_prime()
    src = _coerce_source(source)
    st = _state(platform)
    decision: Decision
    with st.lock:
        now = _now()
        _ramp_up(st, now)
        _refill(st, now)
        # The limiter sees every request, so the rate we are learning is the
        # total — not the pool we drew the token from.
        if st.requests_since_event == 0:
            # Open the observation window at the FIRST request after the last
            # event, not at the event itself. Otherwise a 5-minute clean
            # recovery window would be averaged into the "rate we were
            # travelling at when we got limited", and a 10 rpm burst after an
            # idle would read as ~2 rpm — teaching the ceiling far too low.
            st.window_start = now
        st.requests_since_event += 1

        if src == "user":
            st.user_tokens -= 1.0
            allowed = st.user_tokens >= 0
            tokens = st.user_tokens
            reason = "ok" if allowed else "user_exhausted"
            wait_s = 0.0 if allowed else _seconds_for_one_token(st.ceiling_rpm)
        else:
            st.auto_tokens -= 1.0
            allowed = st.auto_tokens >= 0
            tokens = st.auto_tokens
            reason = "ok" if allowed else "auto_exhausted"
            wait_s = 0.0 if allowed else _seconds_for_one_token(st.ceiling_rpm * AUTO_SHARE)
        decision = Decision(
            platform=st.platform,
            source=src,
            allowed=allowed,
            wait_s=wait_s,
            ceiling_rpm=st.ceiling_rpm,
            tokens=tokens,
            reason=reason,
        )

    if not decision.allowed:
        _log_decision(decision)
    return decision


def note_limit(platform: str, *, kind: Optional[str] = None, status: Optional[int] = None,
               source: Source = "auto", backoff_s: Optional[float] = None) -> Decision:
    """Record a rate-limit event. Drops the ceiling fast.

    This is the only place a DB row is written, and it fires once per limit
    event, never per request. The DB write happens after the platform lock is
    released (no IO under lock).
    """
    _maybe_prime()
    src = _coerce_source(source)
    st = _state(platform)
    with st.lock:
        now = _now()
        before = st.ceiling_rpm
        # Capture the load BEFORE _record_trip zeroes the window; afterwards
        # st.trip_rpm holds THIS event's rate (the frontier min is a different
        # quantity and is not what the column means). Convert to the column's
        # trailing-60s unit while the count is still live.
        window_requests = st.requests_since_event
        _record_trip(st, now)
        recent_requests = _recent_requests_value(window_requests, st.trip_rpm)
        learned = st.min_trip_rpm * _SAFETY_FRACTION if st.min_trip_rpm > 0 else before * 0.5
        new_ceiling = max(_FLOOR_CEILING_RPM, learned)
        if new_ceiling > st.ceiling_rpm:
            new_ceiling = st.ceiling_rpm  # an event never raises the ceiling
        st.ceiling_rpm = new_ceiling
        st.events += 1
        st.last_event_ts = now
        st.last_ramp_ts = now
        # AUTO is emptied: it is the most likely source of the burst that
        # tripped us. The USER reservation is deliberately left intact — that
        # headroom is the point of the split, and we cannot prove who caused
        # the event anyway.
        st.auto_tokens = 0.0
        st.user_tokens = min(st.user_tokens, _user_capacity(st.ceiling_rpm))
        ceiling_rpm, trip_rpm, events = st.ceiling_rpm, st.min_trip_rpm, st.events
        plat = st.platform

    _persist_event(plat, kind or "", src, ceiling_rpm, trip_rpm, events, recent_requests)
    decision = Decision(
        platform=st.platform,
        source=src,
        allowed=False,
        wait_s=0.0,
        ceiling_rpm=ceiling_rpm,
        tokens=0.0,
        reason="limit_event" + (f" http_{status}" if status else ""),
    )
    _log_decision(decision)
    if before > ceiling_rpm:
        logger.info(
            "rate_budget: %s limit event #%d — ceiling %.1f -> %.1f rpm (trip %.1f rpm)",
            plat, events, before, ceiling_rpm, trip_rpm,
        )
    else:
        logger.info(
            "rate_budget: %s limit event #%d — ceiling held at %.1f rpm (trip %.1f rpm)",
            plat, events, ceiling_rpm, trip_rpm,
        )
    return decision


def note_hot_call(platform: str, source: Source = "auto", *, limited: bool = False,
                  kind: Optional[str] = None) -> None:
    """Counter-only accounting for volume paths (HLS segments).

    Deliberately does NOT consume a token, does NOT block, and does NOT write
    a row: a 4h VOD is ~2,400 segment calls across 12 parallel fetchers, so
    per-call rows would be a log firehose, and metering CDN segment fetches
    against the GQL/API ceiling would poison it (they are different limiters).
    429s here still feed the ceiling via ``note_limit`` at the seam above.
    """
    st = _state(platform)
    with st.lock:
        now = _now()
        if now - st.hot_window_start >= _HOT_WINDOW_S:
            st.hot_window_start = now
            st.hot_calls = 0
            st.hot_limited = 0
        st.hot_calls += 1
        if limited:
            st.hot_limited += 1


def backoff_seconds(platform: str, source: Source = "auto") -> float:
    """How long a caller should wait before its next request. Never negative.

    Advisory only — the caller decides whether to honour it.
    """
    st = _state(platform)
    src = _coerce_source(source)
    with st.lock:
        now = _now()
        _ramp_up(st, now)
        _refill(st, now)
        if src == "user":
            deficit = max(0.0, 1.0 - st.user_tokens)
            rate = st.ceiling_rpm
        else:
            deficit = max(0.0, 1.0 - st.auto_tokens)
            rate = st.ceiling_rpm * AUTO_SHARE
        if deficit <= 0:
            return 0.0
        return min(MAX_AUTO_WAIT_S, _seconds_for_one_token(rate))


def auto_exhausted(platform: str) -> bool:
    """True when background work has spent its share — the scheduler's signal.

    Exposed, not enforced: the 24/7 scheduler is not gated on this (see the
    module docstring / report). A caller that wants to skip a pass checks
    this first.
    """
    st = _state(platform)
    with st.lock:
        _refill(st, _now())
        return st.auto_tokens < 1.0


def scheduler_hint() -> Dict[str, Any]:
    """Per-platform 'should background work start a pass right now?' view."""
    out: Dict[str, Any] = {}
    for plat in _known_platforms():
        out[plat] = {
            "auto_exhausted": auto_exhausted(plat),
            "backoff_s": round(backoff_seconds(plat, "auto"), 2),
        }
    return out


def _known_platforms() -> List[str]:
    names = set(_PLATFORM_CEILING_RPM) | set(_states)
    return sorted(names)


# --- observability -----------------------------------------------------------

def _log_decision(decision: Decision) -> None:
    """In-memory ring buffer of the last N throttle decisions (no DB)."""
    try:
        with _log_lock:
            _decisions.append(decision.as_dict())
    except Exception:  # noqa: BLE001 — logging must never break a request
        pass


def recent_decisions(limit: int = 20) -> List[Dict[str, Any]]:
    with _log_lock:
        items = list(_decisions)
    return items[-max(1, int(limit)):]


def platform_status(platform: str) -> Dict[str, Any]:
    st = _state(platform)
    with st.lock:
        now = _now()
        # Advance the same way the request path does, so the endpoint reports
        # the numbers the governor would actually act on rather than a stale
        # snapshot. (auto_exhausted/backoff_seconds do this too.)
        _ramp_up(st, now)
        _refill(st, now)
        auto_cap = _auto_capacity(st.ceiling_rpm)
        user_cap = _user_capacity(st.ceiling_rpm)
        hot_age = max(0.0, now - st.hot_window_start)
        return {
            "platform": st.platform,
            "ceiling_rpm": round(st.ceiling_rpm, 3),
            "default_ceiling_rpm": st.default_rpm,
            "auto_share": AUTO_SHARE,
            "auto": {
                "tokens": round(max(0.0, st.auto_tokens), 3),
                "capacity": round(auto_cap, 3),
                "refill_rpm": round(st.ceiling_rpm * AUTO_SHARE, 3),
                "exhausted": st.auto_tokens < 1.0,
            },
            "user": {
                "tokens": round(max(0.0, st.user_tokens), 3),
                "capacity": round(user_cap, 3),
                "refill_rpm": round(st.ceiling_rpm, 3),
            },
            "learning": {
                "events": st.events,
                "trip_rpm": round(st.trip_rpm, 3),
                "min_trip_rpm": round(st.min_trip_rpm, 3),
                "seconds_since_event": (
                    round(now - st.last_event_ts, 1) if st.events else None
                ),
                "ramp_pending": bool(st.events and (now - st.last_ramp_ts) >= _CLEAN_WINDOW_S),
            },
            "hot": {
                "calls_last_min": st.hot_calls if hot_age < _HOT_WINDOW_S else 0,
                "limited_last_min": st.hot_limited if hot_age < _HOT_WINDOW_S else 0,
            },
        }


def status() -> Dict[str, Any]:
    """Read-only snapshot for humans watching the governor learn."""
    return {
        "auto_share": AUTO_SHARE,
        "max_auto_wait_s": MAX_AUTO_WAIT_S,
        "platforms": [platform_status(p) for p in _known_platforms()],
        "scheduler": scheduler_hint(),
        "recent_decisions": recent_decisions(20),
    }


def reset() -> None:
    """Drop all learned state (tests / operator reset)."""
    global _history_primed
    with _registry_lock:
        _states.clear()
    with _log_lock:
        _decisions.clear()
    with _prime_lock:
        _history_primed = False
