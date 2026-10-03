"""Adaptive rate governor: ceiling learning, AUTO/USER pools, hot-path cost.

Every timing assertion uses an injected clock (``rate_budget.set_clock``) and
every test starts from ``rate_budget.reset()``, so nothing here depends on
wall-clock pacing — the whole suite runs in well under a second and gives the
same result on a busy box.

DB isolation: a scratch VODRIP_ARCHIVE_DB, and the persistence calls are
exercised both with the contract absent (agent/rl-history not landed) and with
a stub, since the governor must degrade gracefully in either case.
"""
import os
import pathlib
import tempfile
import threading

import pytest

_DB = pathlib.Path(tempfile.mkdtemp(prefix="rate-budget-")) / "archive.db"
os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)
# The governor primes from history on first use; the tests drive learning
# explicitly, so keep that cross-process read out of the way.
os.environ["VODRIP_RATE_BUDGET_PRIME"] = "0"

from services import archive_db  # noqa: E402
from services import rate_budget  # noqa: E402


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
    rate_budget.set_clock(__import__("time").monotonic)
    rate_budget.reset()


def _ceiling(platform: str) -> float:
    return rate_budget.platform_status(platform)["ceiling_rpm"]


# --- cold start --------------------------------------------------------------

def test_cold_start_defaults_are_conservative(clock):
    """No history yet: a low, sane per-platform default — never a high guess."""
    for plat, expected in (("kick", 20.0), ("twitch", 30.0), ("youtube", 20.0)):
        assert _ceiling(plat) == expected
    # An uncalibrated platform gets the conservative generic default.
    assert _ceiling("some-new-site") == rate_budget._DEFAULT_CEILING_RPM


def test_pool_split_leaves_on_demand_headroom(clock):
    """AUTO is structurally capped below the ceiling; USER is not.

    This is the reservation: background work cannot hold the tokens a user
    needs, so the headroom is never *spent*, only left unspent.
    """
    st = rate_budget.platform_status("twitch")
    assert st["auto"]["capacity"] < st["user"]["capacity"]
    assert st["auto"]["refill_rpm"] == pytest.approx(st["ceiling_rpm"] * rate_budget.AUTO_SHARE)
    assert st["user"]["refill_rpm"] == pytest.approx(st["ceiling_rpm"])


# --- learning: fail fast down, recover slowly up ----------------------------

def _burn(clock, platform, requests, seconds, source="auto"):
    """Issue *requests* calls spread evenly across *seconds* of fake time.

    Rate is therefore ``requests / seconds * 60`` rpm — asserted explicitly
    by the tests below so a ceiling expectation can be read off the maths.
    """
    assert requests > 0
    step = seconds / requests
    for _ in range(requests):
        rate_budget.acquire(platform, source)
        clock.advance(step)


def _ceiling_after_trip(clock, platform, requests, seconds=60.0, source="auto"):
    """Burn at a known rate, trip, and return the resulting ceiling."""
    _burn(clock, platform, requests, seconds, source=source)
    rate_budget.note_limit(platform, kind="gql", status=429, source=source)
    return _ceiling(platform)


def test_ceiling_drops_fast_on_limit_event(clock):
    """20 rpm got us limited -> aim at 70% of it."""
    start = _ceiling("twitch")  # 30
    # 20 requests in 60s = 20 rpm, which tripped us.
    after = _ceiling_after_trip(clock, "twitch", 20)
    assert after == pytest.approx(14.0)  # 20 * 0.70
    assert after < start
    assert rate_budget.platform_status("twitch")["learning"]["events"] == 1
    assert rate_budget.platform_status("twitch")["learning"]["trip_rpm"] == pytest.approx(20.0)


def test_ceiling_recovers_slowly_not_jumpily(clock):
    floor = _ceiling_after_trip(clock, "twitch", 20)

    # One clean window buys one small step, not a return to the old ceiling.
    clock.advance(rate_budget._CLEAN_WINDOW_S)
    one_step = _ceiling("twitch")
    assert one_step == pytest.approx(floor * rate_budget._UP_FACTOR)
    assert one_step < 30.0  # nowhere near the pre-trip default

    # Recovery is a ~5% step; the drop that caused it was ~53%. Asymmetric.
    gain = (one_step - floor) / floor
    assert gain == pytest.approx(0.05, abs=1e-9)
    assert gain < 0.5


def test_recovery_catchup_is_bounded_after_long_idle(clock):
    floor = _ceiling_after_trip(clock, "twitch", 20)

    # Two hours idle: bounded catch-up, never a spring back past the cap.
    clock.advance(2 * 3600)
    assert _ceiling("twitch") <= rate_budget._MAX_CEILING_RPM
    # abs=1e-3: the status view rounds to 3 decimals.
    assert _ceiling("twitch") == pytest.approx(
        floor * rate_budget._UP_FACTOR ** rate_budget._MAX_CATCHUP_STEPS, abs=1e-3
    )


def test_ceiling_never_learns_below_the_floor(clock):
    # A single request in a sub-second window would imply an absurd rate.
    rate_budget.acquire("kick", "auto")
    rate_budget.note_limit("kick", kind="api", status=429)
    assert _ceiling("kick") >= rate_budget._FLOOR_CEILING_RPM


def test_slowest_trip_rate_wins_as_the_frontier(clock):
    """The least aggressive rate ever punished is the best evidence we have."""
    # 20 rpm trips -> 14.
    assert _ceiling_after_trip(clock, "twitch", 20) == pytest.approx(14.0)

    # Recover, then get limited at only 10 rpm — that is the real wall.
    clock.advance(rate_budget._CLEAN_WINDOW_S)
    assert _ceiling_after_trip(clock, "twitch", 10) == pytest.approx(7.0)
    assert rate_budget.platform_status("twitch")["learning"]["min_trip_rpm"] == pytest.approx(10.0)


def test_event_never_raises_the_ceiling_above_current(clock):
    """A limit event may only ever lower the ceiling.

    Trips at 60 rpm while the cold-start ceiling is 30: the 70%-of-trip rule
    would compute 42, which is ABOVE what we are already willing to spend. The
    event must not hand back headroom.
    """
    start = _ceiling("twitch")  # 30
    _burn(clock, "twitch", 60, 60.0)  # 60 rpm
    rate_budget.note_limit("twitch", kind="gql", status=429)
    assert _ceiling("twitch") == start
    assert _ceiling("twitch") < 42.0


# --- pools: the on-demand reservation ---------------------------------------

def test_auto_exhaustion_backs_off_background_while_user_still_served(clock):
    """The core acceptance behaviour.

    Drain the AUTO pool with background work; it must start being denied
    (with a usable wait hint) while a user request is still admitted.
    """
    st = rate_budget.platform_status("twitch")
    ceiling = st["ceiling_rpm"]
    auto_cap = st["auto"]["capacity"]

    decisions = [rate_budget.acquire("twitch", "auto") for _ in range(int(auto_cap))]
    assert all(d.allowed for d in decisions)

    denied = rate_budget.acquire("twitch", "auto")
    assert not denied.allowed
    assert denied.reason == "auto_exhausted"
    assert denied.wait_s > 0
    assert denied.wait_s <= rate_budget.MAX_AUTO_WAIT_S

    # The user still has its own reservation intact.
    user = rate_budget.acquire("twitch", "user")
    assert user.allowed
    assert user.reason == "ok"

    # And the scheduler-facing signal reports the exhaustion.
    assert rate_budget.auto_exhausted("twitch") is True
    assert rate_budget.scheduler_hint()["twitch"]["auto_exhausted"] is True
    assert ceiling > 0


def test_background_pacing_never_errors_and_never_exceeds_the_bound(clock):
    """A denied AUTO call still returns a decision — never an exception."""
    st = rate_budget.platform_status("kick")
    for _ in range(int(st["auto"]["capacity"]) + 25):
        d = rate_budget.acquire("kick", "auto")
        assert d.platform == "kick"
    wait = rate_budget.backoff_seconds("kick", "auto")
    assert 0 < wait <= rate_budget.MAX_AUTO_WAIT_S


def test_user_requests_are_never_waited_on(clock):
    st = rate_budget.platform_status("youtube")
    for _ in range(int(st["user"]["capacity"]) + 10):
        d = rate_budget.acquire("youtube", "user")
        # Served either way; when the pool runs dry it is recorded, not denied
        # as a failure. The caller decides, and a user must not be blocked.
        assert d.reason in ("ok", "user_exhausted")
    assert rate_budget.backoff_seconds("youtube", "user") >= 0


def test_user_pool_refills_faster_than_auto(clock):
    """After an event empties AUTO, USER recovers first — the reservation."""
    _ceiling_after_trip(clock, "twitch", 20)
    # The event empties AUTO, so background work is dry (the bucket only
    # starts refilling from that instant).
    assert rate_budget.auto_exhausted("twitch") is True

    clock.advance(60.0)  # one minute
    st1 = rate_budget.platform_status("twitch")
    # USER regenerates at the full ceiling, AUTO only at 70% of it.
    assert st1["user"]["tokens"] > st1["auto"]["tokens"]
    assert st1["user"]["refill_rpm"] > st1["auto"]["refill_rpm"]


# --- unknown origin ----------------------------------------------------------

def test_unknown_origin_defaults_to_auto(clock):
    """Unrecognised/absent origin is coerced to the conservative AUTO pool."""
    d = rate_budget.acquire("kick", "wat")
    assert d.source == "auto"

    st = rate_budget.platform_status("kick")
    for _ in range(int(st["auto"]["capacity"]) + 5):
        last = rate_budget.acquire("kick", "banana")
    # Drained the AUTO pool, not the (larger) USER one.
    assert not last.allowed
    assert rate_budget.platform_status("kick")["user"]["tokens"] > 0


def test_missing_origin_defaults_to_auto(clock):
    d = rate_budget.acquire("kick")
    assert d.source == "auto"


# --- hot path: counters, not rows --------------------------------------------

def test_hot_call_is_counter_only_never_writes_a_row(clock, monkeypatch):
    """A 4h VOD is ~2,400 segment calls. No token, no sleep, no DB row."""
    calls = []
    monkeypatch.setattr(
        archive_db, "record_rate_limit", lambda *a, **k: calls.append((a, k)), raising=False
    )

    for _ in range(2400):
        rate_budget.note_hot_call("twitch", "auto")

    assert calls == [], "hot segment path must not write per-call rows"
    st = rate_budget.platform_status("twitch")
    assert st["hot"]["calls_last_min"] == 2400
    assert st["hot"]["limited_last_min"] == 0


def test_hot_call_does_not_consume_the_auto_pool(clock):
    st = rate_budget.platform_status("kick")
    before = st["auto"]["tokens"]
    for _ in range(500):
        rate_budget.note_hot_call("kick", "auto")
    assert rate_budget.platform_status("kick")["auto"]["tokens"] == pytest.approx(before)


def test_hot_counters_roll_up_each_minute(clock):
    for _ in range(100):
        rate_budget.note_hot_call("youtube", "auto")
    rate_budget.note_hot_call("youtube", "auto", limited=True)
    assert rate_budget.platform_status("youtube")["hot"] == {
        "calls_last_min": 101, "limited_last_min": 1,
    }
    clock.advance(61.0)
    assert rate_budget.platform_status("youtube")["hot"]["calls_last_min"] == 0


# --- persistence contract (agent/rl-history) ---------------------------------

def test_limit_event_persists_one_row(clock, monkeypatch):
    calls = []
    monkeypatch.setattr(
        archive_db, "record_rate_limit", lambda *a, **k: calls.append((a, k)), raising=False
    )
    _burn(clock, "kick", 20, 60.0)
    rate_budget.note_limit("kick", kind="api /v2", status=429, source="auto")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "kick"
    assert kwargs["origin"] == "auto"


def test_governor_degrades_when_history_contract_is_absent(clock, monkeypatch):
    """Missing record_rate_limit / rate_limit_summary must not break anything."""
    import services.rate_budget as rb

    monkeypatch.delattr(archive_db, "record_rate_limit", raising=False)
    monkeypatch.delattr(archive_db, "rate_limit_summary", raising=False)
    _burn(clock, "kick", 20, 60.0)  # 20 rpm
    d = rate_budget.note_limit("kick", kind="api", status=429)  # must not raise
    assert d.reason.startswith("limit_event")
    assert _ceiling("kick") == pytest.approx(14.0)
    assert rb.prime_from_history() == {}


def test_prime_from_history_only_lowers_ceilings(clock, monkeypatch):
    """Cross-process learning seed: history may teach down, never up."""
    monkeypatch.setattr(
        archive_db, "rate_limit_summary",
        lambda **k: [{"platform": "twitch", "min_trip_rpm": 100.0}], raising=False,
    )
    # 100 rpm in history would imply a 70 rpm ceiling — above the 30 default,
    # so it must NOT be applied.
    assert rate_budget.prime_from_history() == {}
    assert _ceiling("twitch") == 30.0

    # A genuinely punishing history does lower the ceiling.
    monkeypatch.setattr(
        archive_db, "rate_limit_summary",
        lambda **k: [{"platform": "twitch", "min_trip_rpm": 10.0}], raising=False,
    )
    assert rate_budget.prime_from_history() == {"twitch": pytest.approx(7.0)}
    assert _ceiling("twitch") == pytest.approx(7.0)


# --- concurrency -------------------------------------------------------------

def test_concurrent_acquire_is_consistent(clock):
    """Many threads, one platform: no lost updates, no crash, no negative leak."""
    threads = 8
    per_thread = 200
    decisions = []
    lock = threading.Lock()

    def worker():
        local = [rate_budget.acquire("twitch", "auto") for _ in range(per_thread)]
        with lock:
            decisions.extend(local)

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    total = len(decisions)
    assert total == threads * per_thread
    st = rate_budget.platform_status("twitch")
    # Every request is counted exactly once for learning, despite the races.
    assert st["learning"]["events"] == 0
    assert rate_budget._states["twitch"].requests_since_event == total


def test_platforms_do_not_share_a_lock(clock):
    """No lock is shared between platforms, so one cannot stall another."""
    rate_budget._state("twitch")
    rate_budget._state("kick")
    rate_budget._state("youtube")
    locks = {name: rate_budget._states[name].lock for name in ("twitch", "kick", "youtube")}
    assert len({id(l) for l in locks.values()}) == 3


def test_one_platform_exhaustion_does_not_affect_another(clock):
    """A drained Twitch AUTO pool leaves Kick's untouched."""
    st = rate_budget.platform_status("twitch")
    for _ in range(int(st["auto"]["capacity"]) + 10):
        rate_budget.acquire("twitch", "auto")
    assert rate_budget.auto_exhausted("twitch") is True
    assert rate_budget.auto_exhausted("kick") is False
    assert rate_budget.acquire("kick", "auto").allowed is True


# --- observability -----------------------------------------------------------

def test_status_shape_is_json_ready(clock):
    import json

    _burn(clock, "twitch", 20, 60.0)
    rate_budget.note_limit("twitch", kind="gql", status=429)
    snap = rate_budget.status()
    json.dumps(snap)  # must not raise
    assert {p["platform"] for p in snap["platforms"]} >= {"twitch", "kick", "youtube"}
    assert snap["auto_share"] == rate_budget.AUTO_SHARE
    assert "scheduler" in snap
    assert "recent_decisions" in snap


def test_recent_decisions_log_throttles_only(clock):
    assert rate_budget.recent_decisions() == []
    rate_budget.acquire("twitch", "auto")  # allowed -> not logged
    assert rate_budget.recent_decisions() == []
    st = rate_budget.platform_status("twitch")
    for _ in range(int(st["auto"]["capacity"]) + 1):
        rate_budget.acquire("twitch", "auto")
    log = rate_budget.recent_decisions()
    assert log and log[-1]["reason"] == "auto_exhausted"
    assert log[-1]["source"] == "auto"


def test_decision_log_is_bounded(clock):
    st = rate_budget.platform_status("kick")
    for _ in range(rate_budget._DECISION_LOG_MAX + 50):
        rate_budget.acquire("kick", "auto")
        rate_budget.acquire("kick", "user")
    assert len(rate_budget.recent_decisions(limit=10_000)) <= rate_budget._DECISION_LOG_MAX


# --- gates untouched ---------------------------------------------------------

def test_gates_are_untouched_by_the_governor():
    """The governor is a layer IN FRONT of the gates, not a replacement.

    A denied budget must not freeze yt_gate/kick_gate, and arming either gate
    must not move a ceiling: the reactive backstop stays reactive.
    """
    from services import kick_gate, yt_gate

    rate_budget.reset()
    rate_budget.acquire("youtube", "auto")
    before = _ceiling("youtube")

    yt_gate.note_youtube_gate("test")
    assert yt_gate.youtube_gate_active() is True
    assert _ceiling("youtube") == before
    yt_gate.clear_youtube_gate()
    assert yt_gate.youtube_gate_active() is False

    assert kick_gate.gate_remaining_sec() == 0.0
    assert _ceiling("youtube") == before
