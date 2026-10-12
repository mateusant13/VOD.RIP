"""The merged InnerTube walk must obey its caller's budget as a WALL-CLOCK bound.

Measured live 2026-10-12 on a cold preview click: `_collect_merged_innertube_info`
was called with read_timeout=12.0 and returned after **142,048 ms** with 8 formats
-- 8.3x the table's nominal worst case (8 x _READ_TIMEOUT_PLAYER_SEC +
_TV_TIMEOUT_SEC = 17.1 s). The HTTP request abandoned at ~27 s and told the user
"This video is unavailable."

Cause: `read_timeout` bounds one socket read, not one request. A client that
drips bytes overruns it without limit, and this walk is sequential over
_CLIENT_ORDER, so the overrun is paid once per client.

THE CONTRACT, stated exactly (and it is deliberately not "kill an in-flight
request" -- nothing here can interrupt a blocked socket read):
  1. no probe STARTS once the budget is spent;
  2. no probe is HANDED more time than the budget has left.
Tests 1 and 2 pin those two, deterministically, with a fake clock. Test 3 is a
coarse real-clock guard on the original 142 s regression.
"""
import time as _time

import services.youtube_innertube as yt

_BUDGET = 2.0
_START = 1000.0


class _Clock:
    def __init__(self):
        self.now = _START

    def advance(self, dt):
        self.now += dt


class _FakeTime:
    """The real time module, with only monotonic() faked."""

    def __init__(self, clock):
        self._clock = clock

    def monotonic(self):
        return self._clock.now

    def __getattr__(self, name):
        return getattr(_time, name)


def _install(monkeypatch, overrun=0.4):
    """A probe that answers, but takes longer than its read timeout allows."""
    clock = _Clock()
    monkeypatch.setattr(yt, "time", _FakeTime(clock))
    starts = []

    def probe(video_id, profile, timeout, *, session=None, http=None, source="auto"):
        starts.append((clock.now, timeout))
        clock.advance(timeout + overrun)
        return (
            {"playabilityStatus": {"status": "OK"}, "streamingData": {"formats": []}},
            200,
            "ok",
        )

    monkeypatch.setattr(yt, "_player_request", probe)
    return clock, starts


def test_no_probe_starts_after_the_budget_is_spent(monkeypatch):
    _clock, starts = _install(monkeypatch)
    yt._collect_merged_innertube_info("jNQXAC9IVRw", None, _BUDGET, "test")

    deadline = _START + _BUDGET
    late = [(at, t) for at, t in starts if at >= deadline]
    assert not late, (
        "%d probe(s) started at or past the %.1fs deadline, e.g. at +%.2fs "
        "with a %.2fs timeout" % (len(late), _BUDGET, late[0][0] - _START, late[0][1])
    )


def test_no_probe_is_handed_more_time_than_the_budget_has_left(monkeypatch):
    _clock, starts = _install(monkeypatch)
    yt._collect_merged_innertube_info("jNQXAC9IVRw", None, _BUDGET, "test")

    assert starts, "the walk never probed a client"
    deadline = _START + _BUDGET
    for at, timeout in starts:
        remaining = deadline - at
        assert timeout <= remaining + 1e-9, (
            "probe at +%.2fs was handed %.2fs with only %.2fs of budget left"
            % (at - _START, timeout, remaining)
        )


def test_the_deadline_ends_the_walk_not_the_client_table(monkeypatch):
    """A table-length comparison alone proves nothing (profiles may be absent),
    so require the walk to stop strictly inside the table AND to have been
    stopped by time: the next client's start would have been past the deadline.
    """
    _clock, starts = _install(monkeypatch)
    yt._collect_merged_innertube_info("jNQXAC9IVRw", None, _BUDGET, "test")

    assert len(starts) < len(yt._CLIENT_ORDER), (
        "probed all %d clients on a %.1fs budget" % (len(starts), _BUDGET)
    )
    last_started, last_timeout = starts[-1]
    assert last_started + last_timeout >= _START + _BUDGET, (
        "the walk stopped after %.2fs with budget unspent -- something other "
        "than the deadline ended it" % (last_started - _START)
    )


def test_walk_does_not_pay_an_overrun_once_per_client(monkeypatch):
    """Real-clock guard on the measured regression: 9 probes x 2.4s = 22.1s."""
    calls = []

    def probe(video_id, profile, timeout, *, session=None, http=None, source="auto"):
        calls.append(timeout)
        _time.sleep(timeout + 0.4)
        return (
            {"playabilityStatus": {"status": "OK"}, "streamingData": {"formats": []}},
            200,
            "ok",
        )

    monkeypatch.setattr(yt, "_player_request", probe)
    t0 = _time.monotonic()
    yt._collect_merged_innertube_info("jNQXAC9IVRw", None, _BUDGET, "test")
    elapsed = _time.monotonic() - t0

    assert elapsed < 6.0, (
        "a %.1fs budget took %.1fs: every probe overran and the walk paid it "
        "once per client" % (_BUDGET, elapsed)
    )
