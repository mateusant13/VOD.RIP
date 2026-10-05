"""An interactive preview must not queue behind background walks. (B)

MEASURED DEFECT: ``services/ytdlp_guard.py`` held a single process-wide
``threading.Lock`` per lane, and its own comment recorded the gap verbatim --
"Why no priority/bounded acquire here: a plain Lock has no priority". Two
consequences visible in tmp/vodrip-devall-api.log:

  * ``rate_budget: pacing yt-dlp yt_channel_list for 21.2s`` fires constantly at
    a learned ceiling of 4.04 rpm, and the owner's preview sessions measured
    ``server_ms=22875`` (a real preview in the same window).
  * A background channel walk that keeps RE-OFFERING work can take the next
    grant every time, so a waiting preview starves.

THE SHAPE THAT IS DEFENSIBLE (and what these tests pin): background walks YIELD
to an interactive request that is ALREADY WAITING, but an in-progress download
is NEVER preempted. A 2-hour VOD legitimately holds the guard for minutes; a
blanket "preview always wins" would break that. So:

  * interactive overtakes a WAITING background walk        (test_overtakes)
  * interactive does NOT preempt an IN-PROGRESS download  (test_no_preempt)
  * a long download still completes and releases           (test_long_holder)
  * background ordering among background callers is FIFO  (test_background_fifo)
  * the gate is still a mutex                               (test_single_holder)

THE GATE IS LOOKED UP FROM THE REAL SURFACE, not imported, so that on the
UNFIXED tree these tests fail by ASSERTING the defect (first walker wins)
instead of erroring on a missing name. `_resolve_gate` falls back to the plain
un-prioritised lock that main has, which is exactly the behaviour being
replaced.
"""
from __future__ import annotations

import contextlib
import threading
import time

import pytest

from services import ytdlp_guard

#: Wall-clock budget for every wait in this file.
#:
#: These tests assert ORDERING, not speed, so the budget only has to outlast a
#: descheduled thread. It is deliberately generous because the box throttles:
#: during this work two python processes were observed with ALL threads in
#: Wait/Suspended and the machine sampling 0% CPU, which made a 5 s wait fail
#: for a thread that had simply not been scheduled yet. A too-short timeout
#: would report a throttle as a defect.
_CLOCK_TIMEOUT = 60.0


def _resolve_gate():
    """The priority gate, or main's un-prioritised lock as a stand-in.

    The fallback is deliberate and is what makes the RED run meaningful: with
    it, the ordering tests exercise a gate with NO priority and FAIL on the
    assertion, instead of blowing up on a missing attribute.
    """
    gate = getattr(ytdlp_guard, "priority_lock", None)
    if gate is not None:
        return gate

    class _PlainGate:
        """What main has: a plain Lock. No priority, FIFO by luck of scheduling."""

        def __init__(self) -> None:
            self._lock = threading.Lock()

        def reset(self) -> None:
            pass

        @contextlib.contextmanager
        def acquire(self, kind: str = "background"):
            with self._lock:
                yield

    return _PlainGate()


@pytest.fixture
def gate():
    g = _resolve_gate()
    g.reset()
    yield g
    g.reset()


def _wait_queued(g, count: int, timeout: float = _CLOCK_TIMEOUT) -> None:
    """Block until ``count`` callers are parked WAITING on the gate.

    Asserts it, because a timeout here is a broken test, not a broken gate.
    Uses the gate's own waiter list when it exposes one (the priority gate);
    the plain-Lock fallback exposes nothing, so it falls back to a short settle
    — that fallback is main's behaviour and is only ever used on the RED run.
    """
    waiters = getattr(g, "_waiters", None)
    if waiters is None:
        time.sleep(0.5)
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with g._cond:
            if len(waiters) >= count:
                return
        time.sleep(0.01)
    raise AssertionError(
        f"only {len(waiters)} caller(s) queued on the gate, expected {count}"
    )



def _hold(gate, *, kind, ready, release, entered):
    """Acquire the gate as `kind`, announce entry, then block until `release`.

    The wait MUST be on ``release`` (the caller's "you may let go" signal), not
    on ``ready``. An earlier version waited on ``ready``, which the helper's own
    caller sets to the same event as ``entered`` -- so the holder returned
    immediately, nothing was ever in flight, and every ordering assertion was
    measuring thread-startup luck instead of the gate.
    """
    with gate.acquire(kind=kind):
        entered.set()
        assert release.wait(_CLOCK_TIMEOUT), "holder was never released"


def test_interactive_overtakes_a_waiting_background_walk(gate) -> None:
    """RED on main: with a plain Lock the walk goes first, whatever arrived first.

    The preview must win the moment it is waiting, without the long download
    ever being interrupted.
    """
    order: list[str] = []
    order_lock = threading.Lock()

    def _record(who: str) -> None:
        # Recorded INSIDE the gate, at the moment of entry. Appending after
        # the `with` block would measure which thread's post-block code ran
        # first -- a scheduling race that has nothing to do with priority.
        with order_lock:
            order.append(who)

    # A background walk is IN PROGRESS (short, like a real channel-list call).
    bg_running = threading.Event()
    bg_done = threading.Event()
    walk = threading.Thread(
        target=lambda: _hold(
            gate, kind="background", ready=bg_running, release=bg_done, entered=bg_running
        )
    )
    walk.start()
    assert bg_running.wait(_CLOCK_TIMEOUT), "background walk never entered the gate"

    # A background walk QUEUES (second walk, as a walk loop would re-offer).
    walk2_queued = threading.Event()
    walk2_done = threading.Event()

    def _walk2():
        walk2_queued.set()
        with gate.acquire(kind="background"):
            _record("walk2")
        walk2_done.set()

    walk2 = threading.Thread(target=_walk2)
    walk2.start()
    assert walk2_queued.wait(_CLOCK_TIMEOUT)
    _wait_queued(gate, 1)  # walk2 is INSIDE the wait list, not still starting up

    # Now the owner clicks a video: an INTERACTIVE request arrives and waits.
    preview_done = threading.Event()

    def _preview():
        with gate.acquire(kind="interactive"):
            _record("preview")
        preview_done.set()

    preview = threading.Thread(target=_preview)
    preview.start()
    # A fixed sleep here is a race under load: the preview may not have
    # reached the gate before the walk below is released, and then the test
    # would pass for the wrong reason on the unfixed tree. Wait for the
    # INTERACTIVE ticket to actually be queued instead.
    _wait_queued(gate, 2)

    # The in-progress walk finishes. The interactive request must be next --
    # not the already-waiting walk2.
    bg_done.set()
    walk.join(_CLOCK_TIMEOUT)

    assert preview_done.wait(_CLOCK_TIMEOUT), "interactive request never got the gate"
    # JOIN both before reading `order`. Waiting on preview_done alone is not
    # enough: that event is set after the `with` block, so the ordering list
    # can still be mid-write when we read it. Joining is the only way to know
    # both entries exist before comparing them.
    preview.join(_CLOCK_TIMEOUT)
    walk2_done.wait(_CLOCK_TIMEOUT)
    walk2.join(_CLOCK_TIMEOUT)
    assert not preview.is_alive() and not walk2.is_alive(), "a gate holder never finished"

    assert order and order[0] == "preview", (
        f"interactive request must overtake a waiting background walk; got {order!r}. "
        f"With a plain Lock the background walk re-offered work and starved the preview."
    )
    assert "walk2" in order, "the waiting background walk must still run afterwards"


def test_interactive_does_not_preempt_an_in_progress_download(gate) -> None:
    """The guard-rail: a 2-hour VOD holds the gate; the preview waits for it.

    This is what makes priority safe to ship. A bounded-acquire fix that failed
    here would break every legitimate long download.
    """
    download_running = threading.Event()
    download_release = threading.Event()
    download_done = threading.Event()

    def _download():
        with gate.acquire(kind="download"):
            download_running.set()
            download_release.wait(10.0)
        download_done.set()

    dl = threading.Thread(target=_download)
    dl.start()
    assert download_running.wait(_CLOCK_TIMEOUT), "download never entered the gate"

    preview_entered = threading.Event()
    preview_done = threading.Event()

    def _preview():
        with gate.acquire(kind="interactive"):
            preview_entered.set()
        preview_done.set()

    pv = threading.Thread(target=_preview)
    pv.start()
    time.sleep(0.4)

    assert not preview_entered.is_set(), (
        "an interactive request PREEMPTED an in-progress download. A 2-hour VOD "
        "legitimately holds this gate for minutes; priority must apply only at "
        "the moment of granting, never by cancelling a running holder."
    )
    assert not download_done.is_set(), "the long download was cut short"

    download_release.set()
    assert download_done.wait(_CLOCK_TIMEOUT), "download never released the gate"
    assert preview_done.wait(_CLOCK_TIMEOUT), "preview never ran after the download finished"
    dl.join(_CLOCK_TIMEOUT)
    pv.join(_CLOCK_TIMEOUT)


def test_long_download_completes_and_releases(gate) -> None:
    """A long holder is not timed out, starved, or forced to abort."""
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    elapsed: list[float] = []

    def _holder():
        t0 = time.monotonic()
        with gate.acquire(kind="download"):
            started.set()
            release.wait(6.0)
        elapsed.append(time.monotonic() - t0)
        finished.set()

    t = threading.Thread(target=_holder)
    t.start()
    assert started.wait(_CLOCK_TIMEOUT)
    time.sleep(0.5)  # hold well past any sane "bounded acquire"
    release.set()
    assert finished.wait(_CLOCK_TIMEOUT), "a legitimate long holder must complete"
    assert elapsed and elapsed[0] >= 0.5, (
        f"the holder was cut short after {elapsed[0]:.2f}s; a bounded acquire that "
        f"fails a long download is explicitly out of bounds"
    )
    t.join(_CLOCK_TIMEOUT)


def test_background_walks_stay_fifo_among_themselves(gate) -> None:
    """Priority must not starve the background queue either.

    A walk that is re-offered forever is the defect; a walk that is NEVER run is
    a different one. Background callers keep their arrival order.
    """
    order: list[int] = []
    ready = threading.Event()
    release = threading.Event()

    blocker = threading.Thread(
        target=lambda: _hold(
            gate, kind="download", ready=ready, release=release, entered=ready
        )
    )
    blocker.start()
    assert ready.wait(_CLOCK_TIMEOUT)

    threads = []
    for i in range(4):
        def _run(i=i):
            with gate.acquire(kind="background"):
                order.append(i)
        th = threading.Thread(target=_run)
        th.start()
        threads.append(th)
        time.sleep(0.05)  # deterministic arrival order

    release.set()
    for th in threads:
        th.join(_CLOCK_TIMEOUT)
    blocker.join(_CLOCK_TIMEOUT)

    assert order == [0, 1, 2, 3], f"background callers lost arrival order: {order!r}"


def test_gate_admits_a_single_holder_at_a_time(gate) -> None:
    """Priority must not become concurrency: the guard is still a mutex."""
    concurrent = []
    live = 0
    lock = threading.Lock()

    def _run():
        nonlocal live
        with gate.acquire(kind="background"):
            with lock:
                live += 1
                concurrent.append(live)
            time.sleep(0.05)
            with lock:
                live -= 1

    threads = [threading.Thread(target=_run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(_CLOCK_TIMEOUT)

    assert max(concurrent) == 1, (
        f"two holders were inside the gate at once (peak {max(concurrent)}); "
        f"yt-dlp instances must stay serialized"
    )


