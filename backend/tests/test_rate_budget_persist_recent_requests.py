"""The governor must persist the `recent_requests` it measures.

THE DEFECT THIS FILE EXISTS FOR
-------------------------------
`rate_budget._persist_event` never passed `recent_requests` to
`archive_db.record_rate_limit`, so every row the governor wrote about its own
decisions landed with the column NULL. That is not a cosmetic gap: the column
is what `rate_limit_summary` means into `observed_rate_per_min`, and NULL is
excluded from that mean, so the governor's own observations of its own
budgeting were persisted and then silently ignored. On the live archive
(H:\\VOD.RIP-data\\archive.db, read-only) 30 of 33 rows carried a reading and
the 3 that did not were exactly the governor's — identifiable by the
`context='ceiling_rpm=... trip_rpm=... events=...'` fingerprint that
`_persist_event` alone formats.

THE UNIT IS THE TRAP
--------------------
The naive one-line fix — hand over `st.requests_since_event` — is wrong, and
wrong in both directions. The column is rl_counter's: "requests in the
trailing 60 s window", which is ALREADY a requests-per-minute number
(rl_counter.py:63-67, WINDOW_SEC=60). The governor's window runs from the first
request after the last event to this one, so it is not 60 s long:

  * 400 requests over 20 minutes is 20 rpm, not 400. Storing the count would
    inflate the group mean, and the learned ceiling with it.
  * 31 requests inside 0.4 s is 31 requests in the last minute. `_record_trip`
    clamps the window to 1 s and calls that 1860 rpm; storing the
    extrapolation would tell the history we tripped at 1860 rpm, which
    switches the throttle OFF — a runaway in the direction that removes the
    protection this module exists for.

So `_recent_requests_value` takes the smaller of (count, window average rate),
which satisfies both branches, and returns None — never 0 — when the window
measured nothing.

THE INVARIANTS THESE TESTS GUARD
--------------------------------
  1. A governor-written event persists a real `recent_requests`, and
     `prime_from_history` incorporates it (not written-and-ignored).
  2. EVERY writer of the table supplies the field, not just the governor.
  3. Floor-bounded, non-oscillating ratchet: replaying self-written events
     settles on `_FLOOR_CEILING_RPM` and stops there.
  4. No runaway upward: a stored reading never exceeds the requests actually
     issued, so history cannot learn a load this process never produced.
  5. Unmeasured is NULL. Never a fabricated 0.

Timing uses an injected clock and a scratch DB, so nothing here depends on
wall-clock pacing or on the production archive.
"""
import math
import pathlib
import tempfile
import time

import pytest

# archive_db._db_path() re-reads this env var on every call, so scoping it to a
# fixture keeps this module independent of whatever import order pytest picks.
_SCRATCH_DB = pathlib.Path(tempfile.mkdtemp(prefix="rate-budget-rr-")) / "archive.db"

from services import archive_db  # noqa: E402
from services import kick_gate, rate_budget, yt_gate  # noqa: E402


class FakeClock:
    """Monotonic-by-construction test clock. Nothing sleeps for real."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


@pytest.fixture()
def clock():
    c = FakeClock()
    rate_budget.set_clock(c)
    rate_budget.reset()
    yield c
    rate_budget.set_clock(time.monotonic)
    rate_budget.reset()
    yt_gate.clear_youtube_gate()


@pytest.fixture()
def db(monkeypatch, clock):
    """A scratch archive.db with the history table emptied on both ends.

    Priming stays OFF in the env so a stale prime from a neighbouring module
    cannot pre-seed a ceiling; the tests that need priming call
    `prime_from_history()` explicitly.
    """
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(_SCRATCH_DB))
    monkeypatch.setenv("VODRIP_RATE_BUDGET_PRIME", "0")
    archive_db.execute("DELETE FROM rate_limit_events")
    yield _SCRATCH_DB
    archive_db.execute("DELETE FROM rate_limit_events")


def _ceiling(platform: str) -> float:
    return rate_budget.platform_status(platform)["ceiling_rpm"]


def _burn(clock, platform, requests, seconds, source="auto"):
    """Issue *requests* calls evenly across *seconds* — i.e. `requests/seconds*60` rpm."""
    assert requests > 0
    step = seconds / requests
    for _ in range(requests):
        rate_budget.acquire(platform, source)
        clock.advance(step)


def _rows():
    return [dict(r) for r in archive_db.query(
        "SELECT platform, kind, origin, recent_requests, context"
        " FROM rate_limit_events ORDER BY id"
    )]


# --- 1. it is persisted, and priming actually uses it ------------------------

def test_governor_event_persists_the_measured_load(db, clock):
    """A 429 after 20 requests in 60 s is a 20 rpm observation. Store the 20.

    Pre-fix this row carried NULL: the kwarg was not passed at all, so every
    event the governor wrote about itself was invisible to the learning.
    """
    _burn(clock, "kick", 20, 60.0)
    rate_budget.note_limit("kick", kind="api /v2", status=429, source="auto")

    rows = _rows()
    assert len(rows) == 1, f"one event, one row; got {rows}"
    assert rows[0]["recent_requests"] == 20, (
        "the governor's own observation of the rate it was travelling at must "
        f"reach the row; got {rows[0]['recent_requests']!r}"
    )


def test_primed_ceiling_uses_the_governors_own_history(db, clock):
    """End to end: the persisted reading must move the learned ceiling.

    Written-and-ignored would pass the test above and fail this one, which is
    the whole point — the column only matters once prime_from_history means it.
    """
    _burn(clock, "kick", 20, 60.0)
    rate_budget.note_limit("kick", kind="api /v2", status=429, source="auto")
    # In-process: the trip is 20 rpm, so the frontier is 20 * SAFETY_FRACTION.
    assert _ceiling("kick") == pytest.approx(14.0)

    rate_budget.reset()  # cold start again — only history can re-teach it
    assert _ceiling("kick") == 20.0

    applied = rate_budget.prime_from_history()
    assert applied == {"kick": 14.0}, (
        f"history written by the governor must be learnable from; got {applied}"
    )
    assert _ceiling("kick") == pytest.approx(14.0)


# --- 2. every writer supplies the field --------------------------------------

def test_every_writer_of_the_table_supplies_recent_requests(db, clock, monkeypatch):
    """A row written without the field is a row that silently contributes nothing.

    NULL reads as "no measurement" while looking like data, so the *presence*
    of the kwarg is the contract. It may be None — yt_gate/kick_gate pass
    rl_counter.recent_requests(), which is legitimately None for a process
    that never issued a request — but it must always be passed.
    """
    calls = []
    monkeypatch.setattr(
        archive_db, "record_rate_limit",
        lambda *a, **k: calls.append((a, k)), raising=False,
    )

    # writer 1 — the governor
    _burn(clock, "youtube", 12, 60.0)
    rate_budget.note_limit("youtube", kind="innertube_bot_gate", status=429, source="auto")

    # writer 2 — yt_gate
    yt_gate.clear_youtube_gate()
    yt_gate.note_youtube_gate("Sign in to confirm you're not a bot",
                              surface="metadata", origin="auto")

    # writer 3 — kick_gate
    kick_gate.note_kick_gate_event("429 rate-limited on /api/v2/z",
                                   kind="http_429", surface="metadata", origin="auto")

    assert len(calls) == 3, f"all three writers reached record_rate_limit; got {len(calls)}"
    for args, kwargs in calls:
        assert "recent_requests" in kwargs, (
            f"writer for {args[0]}/{args[1]} omitted recent_requests; "
            "a NULL there is a row that contributes nothing to "
            "observed_rate_per_min while still looking like data"
        )
    # ...and when the governor DID measure something, its value is real.
    gov = next(kw for args, kw in calls if args[0] == "youtube")
    assert gov["recent_requests"] == 12, "12 requests in 60 s is 12 rpm"


# --- 3. the loop guard: floor-bounded, non-oscillating -----------------------

def test_learned_ceiling_cannot_ratchet_past_the_floor(db, clock):
    """INVARIANT (ratchet): replaying the governor's own history converges on
    the floor and stops there — it never falls below it, and never climbs back.

    This is the guard on the decision that makes persisting this column safe.
    The governor's ceiling sets the rate it admits; the admitted rate is what
    it measures; that measurement now comes back to it via history. Feed it a
    long run of self-written low-rate trips — the adversarial shape of that
    loop — and the learned ceiling must settle at _FLOOR_CEILING_RPM and stay
    there, because prime_from_history lowers only (`if target < ceiling`).
    """
    seen = []
    for _ in range(60):
        _burn(clock, "kick", 1, 60.0)  # 1 rpm: measurable, and absurdly low
        rate_budget.note_limit("kick", kind="api", status=429, source="auto")
        rate_budget.reset()
        rate_budget.prime_from_history()
        seen.append(_ceiling("kick"))

    floor = rate_budget._FLOOR_CEILING_RPM
    assert all(c >= floor for c in seen), f"ratcheted past the floor: {min(seen)}"
    assert seen[-1] == pytest.approx(floor), f"expected to settle on {floor}; got {seen[-1]}"
    # Converged, not oscillating: the tail is a constant, so the sequence is
    # not hunting between values.
    assert len(set(seen[-10:])) == 1, f"ceiling is oscillating: {seen[-10:]}"


# --- 4. the loop guard: no runaway upward ------------------------------------

def test_never_claims_a_load_it_did_not_produce(db, clock):
    """INVARIANT (runaway): a stored reading never exceeds the requests issued.

    `_record_trip` clamps the observation window to 1 s, so 31 requests inside
    0.4 s is reported in-process as trip_rpm=1860. That frontier is left
    exactly as it was — this is a persistence change, not a learning change —
    but the STORED reading is a trailing-60s count, so it is bounded by the 31
    requests we actually issued.

    Storing 1860 instead would be a runaway in the direction that DISABLES the
    throttle: the history would say "we tripped at 1860 rpm, stop pacing", and
    priming (which only lowers) would decline to lower at all.
    """
    for _ in range(31):
        rate_budget.acquire("youtube", "auto")
        clock.advance(0.4 / 31)
    rate_budget.note_limit("youtube", kind="innertube_bot_gate", status=429, source="auto")

    rows = _rows()
    assert rows[0]["recent_requests"] == 31, (
        "31 requests inside 0.4 s is 31 requests in the last minute, not 1860"
    )
    # The in-process frontier is deliberately untouched by this change.
    assert rate_budget.platform_status("youtube")["learning"]["min_trip_rpm"] > 1000, (
        "the governor's own 1 s-floor extrapolation must not change"
    )


def test_long_window_stores_the_rate_not_the_raw_window_count(db, clock):
    """400 requests over 20 min is 20 rpm. The column is a trailing-60s count,
    so the raw window count would read 20x too high and inflate the ceiling
    the governor learns from."""
    _burn(clock, "twitch", 400, 1200.0)
    rate_budget.note_limit("twitch", kind="gql", status=429, source="auto")

    rows = _rows()
    assert rows[0]["recent_requests"] == 20, (
        f"expected the window's average rate (20 rpm); got {rows[0]['recent_requests']!r}"
    )


# --- 5. unmeasured is NULL, never a fabricated 0 -----------------------------

def test_unmeasured_load_is_null_never_zero(db, clock):
    """A trip with no counted traffic behind it has no measured load.

    0 is a real reading of "a clean window": it would be averaged into the
    group mean and ratchet the ceiling to the floor. yt_gate/kick_gate take
    the same position ("None is written straight through").
    """
    rate_budget.note_limit("kick", kind="api", status=429, source="auto")

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["recent_requests"] is None, (
        f"unmeasured must be NULL, not 0; got {rows[0]['recent_requests']!r}"
    )


def test_below_resolution_load_is_null_not_one(db, clock):
    """One request an hour reads 0.0167 rpm. Rounding that up to a stored 1
    would invent a measurement at the exact resolution the ceiling cares
    about, so it stays NULL."""
    _burn(clock, "kick", 1, 3600.0)
    rate_budget.note_limit("kick", kind="api", status=429, source="auto")
    assert _rows()[0]["recent_requests"] is None


# --- the unit bridge on its own ----------------------------------------------

@pytest.mark.parametrize("requests,trip_rpm,expected", [
    (20, 20.0, 20),        # exactly one minute: count == rate
    (400, 20.0, 20),       # long window: the average rate, not the count
    (31, 1860.0, 31),      # sub-second burst: the count, not the extrapolation
    (1, 1.0, 1),           # floor-adjacent but measurable
    (0, 0.0, None),        # nothing measured
    (5, float("nan"), None),
    (5, float("inf"), None),
    (1, 0.0001, None),     # below one-request resolution
    (0, 55.0, None),       # stale trip rate with an empty window
])
def test_recent_requests_value(requests, trip_rpm, expected):
    assert rate_budget._recent_requests_value(requests, trip_rpm) == expected


def test_recent_requests_value_never_exceeds_requests_issued():
    """The runaway guard, stated as a property: for any measured window, the
    stored reading is at most the number of requests that were issued."""
    for requests in range(1, 200):
        for trip in (0.5, 1.0, 7.5, 60.0, 1860.0, 1e6):
            value = rate_budget._recent_requests_value(requests, trip)
            assert value is None or (0 < value <= requests), (
                f"stored {value} for {requests} requests at trip {trip}"
            )
            assert value is None or value == int(value), "must be a whole count"
    assert math.isnan(float("nan"))  # sanity: the NaN case above is meaningful
