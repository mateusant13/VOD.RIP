"""How hard were we hitting each platform when it said no?

`archive_db.rate_limit_events` persists WHEN we get rate-limited and by
whom (`auto` background work vs `user` on-demand work), but on its own it
cannot answer the question the owner actually asked: *a média de quando
atingimos rate limit* — at what request rate does each platform start
saying no? The rows carry `recent_requests` / `in_flight` for exactly
that, and the only honest way to fill them is to COUNT the requests we
make. This module is that counter.

Design constraints (all of them come from the call sites, not taste):

* **In-memory only, never a DB write per request.** The instrumented
  chokepoints are extremely hot: `_download_one_segment` runs ~2,400
  times for a single 4h VOD across 12 parallel fetchers, and
  `_post_comments_page` pages a whole chat replay. `archive_db.execute`
  takes a process-global write lock, so a per-request INSERT would both
  serialize the app and be wrong. Nothing here touches SQLite; the
  counter's only durable consumer is `record_rate_limit`, which runs
  once per *limit event* (tens per day).
* **Thread-safe.** `archive_transcribe` uses a thread pool, the HLS
  downloader runs 12 fetchers, and the gates fire from request threads.
  Each platform owns its own `threading.Lock`; no lock is ever held
  across IO, and no other lock is taken while one is held (no nesting,
  no lock ordering, no deadlock surface).
* **Bounded memory.** A fixed ring of `WINDOW_BUCKETS` buckets per
  platform, allocated once and reused forever, over a fixed platform
  set. Bucket slots are overwritten in place — a 24/7 worker counting
  every segment for a week grows by exactly zero bytes.
* **NULL stays NULL.** `recent_requests()` returns `None` for a platform
  this process has never issued a request for, so the DB column keeps
  meaning "not measured" and never fabricates a clean window of zero.

Time is read through `_clock()` (monotonic) and injected with
`set_clock()`, so tests are deterministic and no wall-clock or
sleep-and-hope is involved.

ponytail: counts are per PROCESS. The app and the detached worker each
count their own traffic, same as the gates' per-process deadlines. That
is the right granularity for single-IP boxes; cross-process totals would
need a shared file and buy nothing until two workers run at once.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

# Platforms we bucket by. Fixed set — it is also the memory bound (see
# _counters). 'hls' is the bucket for HLS segment traffic whose platform
# the download layer could not attribute (a non-YouTube m3u8 from an
# unknown caller); it is deliberately NOT folded into a real platform, so
# an unattributed segment burst can never inflate a Kick or YouTube
# number. 'other' is the catch-all for genuinely unknown platforms.
PLATFORMS = ("youtube", "twitch", "kick", "hls", "other")

# Bucket width. The window we report is WINDOW_SEC; keeping buckets much
# finer than the window means the reported number is off by at most one
# bucket's worth of traffic (a small OVER-count, i.e. the conservative
# direction for a throttle: it makes a limit look like it landed at a
# slightly higher rate than it really did).
BUCKET_SEC = 10.0
# The reported window: "requests in the last minute" is the unit the
# `recent_requests` column documents, and it is what makes
# `rate_limit_summary.observed_rate_per_min` a real requests-per-minute
# number instead of a null.
WINDOW_SEC = 60.0
WINDOW_BUCKETS = max(2, int(WINDOW_SEC / BUCKET_SEC))

_clock: Callable[[], float] = time.monotonic

# Guards the REGISTRY only (creating a platform's counters, and reset()).
# Never held while counting: _counters_for takes it, returns, and the
# per-platform lock is the only lock on the hot path.
_registry_lock = threading.Lock()
_counters: dict[str, "_PlatformCounters"] = {}


class _Bucket:
    """One aligned slice of the trailing window (reused, never freed)."""

    __slots__ = ("index", "count", "at")

    def __init__(self) -> None:
        self.index = -1  # never written; a stale index is skipped
        self.count = 0
        self.at = 0.0


class _PlatformCounters:
    """Request counts + in-flight gauge for one platform.

    The lock guards the ring and the gauge and NOTHING else. Every
    critical section below is a handful of arithmetic ops on already
    resident objects: no allocation, no IO, no other lock.
    """

    __slots__ = ("_lock", "_buckets", "_in_flight", "_total", "_last")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets = [_Bucket() for _ in range(WINDOW_BUCKETS)]
        self._in_flight = 0
        self._total = 0
        self._last = 0.0

    def count(self, n: int = 1) -> int:
        """Record *n* requests issued now. Returns the live minute count.

        Cost: one lock acquire, one clock read, one integer division, one
        list index and two int adds. No allocation, no IO.
        """
        with self._lock:
            return self._bump_locked(n)

    def _bump_locked(self, n: int) -> int:
        """Body of count(). The caller MUST already hold _lock."""
        now = _clock()
        index = int(now // BUCKET_SEC)
        slot = index % WINDOW_BUCKETS
        bucket = self._buckets[slot]
        if bucket.index != index:
            # Rollover: reuse this slot. Bounded by construction —
            # WINDOW_BUCKETS slots exist and none are ever allocated
            # on this path.
            bucket.index = index
            bucket.count = 0
            bucket.at = now
        bucket.count += n
        self._total += n
        self._last = now
        return bucket.count

    def _recent_locked(self, span: int, newest: int) -> int:
        """Body of recent(). The caller MUST already hold _lock.

        Split out because _lock is a plain, non-reentrant Lock: the
        diagnostics path needs the same numbers while already holding it,
        and a self-deadlock here would wedge every count for that
        platform on the first snapshot().
        """
        total = 0
        for bucket in self._buckets:
            age = newest - bucket.index
            if 0 <= age < span:
                total += bucket.count
        return total

    def recent(self, window_sec: float = WINDOW_SEC) -> int:
        """Requests counted in the trailing *window_sec*.

        Sums the buckets whose window overlaps the trailing span, so the
        result is an upper bound on the exact sliding window by at most
        one bucket (BUCKET_SEC) of traffic — the conservative direction.
        """
        span = max(1, int(window_sec / BUCKET_SEC))
        # Anchor the window to NOW, not to the last counted request: a
        # platform that went quiet must report 0 (a measured, empty
        # window), not the same stale number for an hour.
        newest = int(_clock() // BUCKET_SEC)
        with self._lock:
            return self._recent_locked(span, newest)

    def begin(self) -> int:
        """A request STARTED: count it AND mark it in flight.

        One lock acquire for both — this is the hot path (every segment,
        chat page and GQL call), and splitting it into count_request +
        begin_request would take the same non-reentrant lock twice per
        request, which under 12 parallel fetchers is real contention for
        no benefit. A request that is counted but never ended still shows
        up in the load, which is the honest reading: the platform saw it.
        """
        with self._lock:
            self._in_flight += 1
            return self._bump_locked(1)

    def end(self) -> int:
        with self._lock:
            # Floor at 0: an unbalanced end() (a caller that forgot the
            # scope) must not make the gauge negative and poison later
            # rows, and the gauge is only a diagnostic.
            if self._in_flight > 0:
                self._in_flight -= 1
            return self._in_flight

    def reset(self) -> None:
        with self._lock:
            for bucket in self._buckets:
                bucket.index = -1
                bucket.count = 0
                bucket.at = 0.0
            self._in_flight = 0
            self._total = 0
            self._last = 0.0


def normalize_platform(platform: str) -> str:
    """Fold a platform label onto a bounded bucket name.

    Bounds the registry: an unexpected label can only ever land on an
    existing PLATFORMS entry, so the counter's footprint is fixed at
    len(PLATFORMS) entries no matter what a caller passes.
    """
    p = str(platform or "").strip().lower()
    if p in PLATFORMS:
        return p
    if p in ("yt", "youtube.com", "googlevideo", "youtu.be"):
        return "youtube"
    if p in ("tw", "twitch.tv"):
        return "twitch"
    return "other"


def _counters_for(platform: str) -> "_PlatformCounters":
    """Counters for *platform*, creating them on first use.

    The registry lock is taken only on the miss path (once per process
    per platform); a hit is a plain dict lookup.
    """
    key = normalize_platform(platform)
    hit = _counters.get(key)
    if hit is not None:
        return hit
    with _registry_lock:
        made = _counters.get(key)
        if made is None:
            made = _PlatformCounters()
            _counters[key] = made
        return made


def peek(platform: str) -> Optional["_PlatformCounters"]:
    """Counters for *platform*, or None when this process never issued a
    request for it. None is the "not measured" signal the DB wants."""
    return _counters.get(normalize_platform(platform))


def count_request(platform: str, n: int = 1) -> None:
    """Record *n* requests to *platform*. In-memory, no IO, no DB."""
    try:
        _counters_for(platform).count(n)
    except Exception:  # noqa: BLE001 — instrumentation must never break egress
        pass


def recent_requests(platform: str, window_sec: float = WINDOW_SEC) -> Optional[int]:
    """Requests in the trailing *window_sec*, or None if never measured.

    None (not 0) is the whole point: a platform this process has not
    talked to has no load to report, and a fabricated zero would read as
    a clean window.
    """
    counters = peek(platform)
    if counters is None:
        return None
    return counters.recent(window_sec)


def in_flight(platform: str) -> Optional[int]:
    """Concurrent requests right now, or None if never measured."""
    counters = peek(platform)
    if counters is None:
        return None
    with counters._lock:
        return counters._in_flight


def total_requests(platform: str) -> Optional[int]:
    """Every request counted since process start (diagnostics/tests)."""
    counters = peek(platform)
    if counters is None:
        return None
    with counters._lock:
        return counters._total


def begin_request(platform: str) -> None:
    """Mark one request in flight. Pair with end_request() in a finally."""
    try:
        _counters_for(platform).begin()
    except Exception:  # noqa: BLE001
        pass


def end_request(platform: str) -> None:
    try:
        _counters_for(platform).end()
    except Exception:  # noqa: BLE001
        pass


class request_scope:
    """Count one in-flight request for *platform* for the block's duration.

    A plain class rather than a @contextmanager generator: this runs on
    the segment/comment hot paths, and a generator-based context manager
    costs several microseconds per entry against a plain method call.

        with rl_counter.request_scope("kick"):
            requests.get(...)
    """

    __slots__ = ("_platform",)

    def __init__(self, platform: str) -> None:
        self._platform = platform

    def __enter__(self) -> "request_scope":
        begin_request(self._platform)
        return self

    def __exit__(self, *exc: object) -> bool:
        end_request(self._platform)
        return False  # never swallow


def snapshot() -> dict:
    """Per-platform diagnostics: windowed rate, in-flight, lifetime total."""
    out: dict = {}
    with _registry_lock:
        items = list(_counters.items())
    span = max(1, int(WINDOW_SEC / BUCKET_SEC))
    newest = int(_clock() // BUCKET_SEC)
    for key, counters in items:
        with counters._lock:
            out[key] = {
                # _recent_locked, NOT recent(): the lock is already held
                # here and is not reentrant.
                "recent_requests": counters._recent_locked(span, newest),
                "in_flight": counters._in_flight,
                "total": counters._total,
                "last_at": counters._last,
            }
    return out


def set_clock(fn: Callable[[], float]) -> None:
    """Inject the clock (tests). Must be a monotonic-seconds callable."""
    global _clock
    _clock = fn


def reset_clock() -> None:
    set_clock(time.monotonic)


def reset() -> None:
    """Drop every counter. Tests only — a live process must not do this."""
    with _registry_lock:
        for counters in _counters.values():
            counters.reset()
        _counters.clear()
