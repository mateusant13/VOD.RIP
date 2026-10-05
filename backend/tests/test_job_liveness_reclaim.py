"""Job-liveness: a heartbeat that advances is not a job that works, and a
reclaim that costs nothing never ends.

Production evidence this exists for (live H:\\VOD.RIP-data\\archive.db, read-only):

    id=chat-youtube-0FH0wZfZ82Q  kind=chat  created 2026-08-07T15:13:42Z
                              heartbeat 2026-10-04T09:41:11Z  progress 0.0
                              attempts 0  max_attempts 3  next_retry_at NULL

A two-month-old job with a same-day heartbeat and a zero attempt count.

The failure is NOT that the old rule cannot see the row: at read time the
heartbeat was 12.8 h old, well past the 2 h chat window, so _claim_next_job
reclaims it on schedule. The failure was that reclaiming was FREE: its
compare-and-set flips running -> running, re-stamps the heartbeat, and never
touches attempts or next_retry_at. So attempts stays 0, max_attempts (3) is
unreachable, and the row is relaunched every 2 h indefinitely. The advancing
heartbeat is the previous cycle's re-stamp, not a live executor, and because
a heartbeat refresh costs nothing, a job can be kept 'running' indefinitely
by something that computes nothing.

Two fixes, and they are not the same fix.

THE RECLAIM COSTS AN ATTEMPT (archive_transcribe._claim_next_job). A relaunch is
an attempt, so the reclaim's CAS charges the budget, sets a _retry_delay_sec
backoff, and parks the row at max_attempts. This is what bounds the loop, and
it is the load-bearing half: without it the loop is unbounded however good the
detection is. Its trigger is unchanged, so the reclaim is no keener to fire
than it was.

A LIVENESS TOUCH IS NOT WORK (archive_db.work_heartbeat). The reclaim only ever
saw the coarse heartbeat, which every bare `update_job(job_id)` watchdog
refreshes for free, so a stuck download could hold its row open indefinitely
and "no work happened" had to be inferred from a timestamp anyone can stamp.
work_heartbeat advances only on a work-bearing update or a real claim, so the
two are distinguishable. This is the deeper root cause, but it is deliberately
NOT wired into the reclaim's trigger: firing on the work signal would reclaim
a legitimately slow fetch, which is worse than the bug.

The predicate under test (worker_server._job_liveness_state, driven through
the real _reclaim_lifeless_jobs tick) condemns a 'running' row only when its
WORK marks are frozen past the bound AND the owning worker burned no CPU AND
the heartbeat advanced in that window — the re-stamp that keeps the row alive
is the evidence that it is dead. That reaper is unchanged by the fix above; it
is the other route to the same budget.

No network, no real worker: a fresh scratch DB per module, a stub `proc`, and
an injected CPU reading. Mirrors test_asr_stall_supervisor's fixture.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.setdefault("VODRIP_NO_DAEMONS", "1")

from services import archive_db  # noqa: E402  (env must be set first)
import worker_server as ws  # noqa: E402


# --- fixtures -------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _scratch_db():
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(
        Path(tempfile.mkdtemp(prefix="job-liveness-")) / "archive.db")
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    if prev is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev
    archive_db._conn = None
    archive_db._schema_ready = False


@pytest.fixture(autouse=True)
def _isolate():
    archive_db.execute("DELETE FROM archive_jobs")
    archive_db.execute("DELETE FROM worker_heartbeats")
    yield


@pytest.fixture()
def logf(tmp_path):
    fh = (tmp_path / "liveness.log").open("w+", encoding="utf-8")
    yield fh
    fh.close()


class _StubProc:
    """Stands in for the supervised child. The reaper only reads .pid."""

    pid = 424242

    def poll(self):
        return None


@pytest.fixture()
def cpu(monkeypatch):
    """Injectable CPU reading for the child. `cpu.value` is what the reaper
    sees on every tick; set it to simulate a worker burning (or not burning)
    CPU. None models an unreadable pid, which _stall_state also treats as
    'no CPU consumed'."""
    box = {"value": 100.0}
    monkeypatch.setattr(ws, "_process_cpu_seconds", lambda pid: box["value"])
    return box


def _running_chat_job(job_id: str = "chat-test-immortal") -> str:
    """A claimed 'chat' job on youtube, shaped like the production row."""
    archive_db.enqueue_job(job_id, "chat", "youtube", "0FH0wZfZ82Q")
    archive_db.update_job(job_id, status="running", progress=0.0)
    return job_id


def _stale_running_chat_job(job_id: str, platform: str = "twitch") -> tuple[str, str]:
    """A claimed 'chat' job whose row is already past its reclaim window —
    the state a wedged executor leaves behind. Returns (job_id, backdated
    stamp) so a caller can assert the stamp MOVED. Twitch by default because
    the YouTube claim path is behind the bot gate; the reclaim CAS under test
    is platform-independent."""
    archive_db.enqueue_job(job_id, "chat", platform, "0FH0wZfZ82Q")
    archive_db.update_job(job_id, status="running", progress=0.0)
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(
        timespec="seconds")
    archive_db.execute(
        "UPDATE archive_jobs SET heartbeat = ?, updated_at = ? WHERE id = ?",
        (old, old, job_id))
    return job_id, old


def _row(job_id: str) -> dict:
    rows = archive_db.query("SELECT * FROM archive_jobs WHERE id = ?", (job_id,))
    return dict(rows[0]) if rows else {}


def _age_the_heartbeat(job_id: str, hours: int = 3) -> None:
    """Backdate a row's liveness stamps past every reclaim window, so the
    next _claim_next_job() sees the same 'wedged executor' state a reclaim
    cycle would."""
    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds")
    archive_db.execute(
        "UPDATE archive_jobs SET heartbeat = ?, updated_at = ? WHERE id = ?",
        (old, old, job_id))


def _reclaim(holder_map, *, bound_s=1.0, proc=None):
    return ws._reclaim_lifeless_jobs(
        _LOGF_SINK, holder_map, proc or _StubProc(), bound_s=bound_s)


class _Sink:
    def write(self, _s):
        pass

    def flush(self):
        pass


_LOGF_SINK = _Sink()


# --- 1. the core case: heartbeat advances, nothing else does ---------------

def test_reclaims_a_job_whose_heartbeat_advances_with_no_progress_and_no_cpu(
        logf, cpu):
    """The production shape. The heartbeat is re-stamped while progress stays
    0.0 and the worker burns nothing: a timestamp is being refreshed to keep
    a job that computes nothing."""
    job = _running_chat_job()
    holders: dict = {}

    # Tick 1 arms the per-job holder.
    assert _reclaim(holders) == []
    assert job in holders, "first tick must arm the holder, not condemn the job"

    # A progress-free touch: update_job stamps the heartbeat on every call.
    before = _row(job)["heartbeat"]
    time.sleep(1.05)  # let the 1-second ISO stamp actually differ
    archive_db.update_job(job)
    assert _row(job)["heartbeat"] != before, "heartbeat did not advance"

    # Work marks are untouched, and the worker is motionless.
    assert _row(job)["progress"] == 0.0
    reclaimed = _reclaim(holders)
    assert reclaimed == [job], (
        "a heartbeat advancing with no progress and no CPU must be reclaimed")


def test_does_not_reclaim_a_job_with_a_fresh_heartbeat_and_advancing_progress(
        logf, cpu):
    """Clause 1: work marks moving is the one unconditional reset."""
    job = _running_chat_job()
    holders: dict = {}
    assert _reclaim(holders) == []

    time.sleep(1.05)
    archive_db.update_job(job, progress=0.42)  # real work + fresh heartbeat
    time.sleep(0.05)
    assert _reclaim(holders) == [], "a job making progress must never be released"
    assert _row(job)["status"] == "running"


def test_does_not_reclaim_a_throttled_but_working_job(logf, cpu):
    """Clause 2: the Steady Watcher throttles this box to 7-14% CPU with a
    ~13.6x wall multiplier, so a working job can report no progress for a
    long time. Burning CPU counts as work and restarts the clock — a slow
    worker must never be mistaken for a dead one."""
    job = _running_chat_job()
    holders: dict = {}
    assert _reclaim(holders) == []

    time.sleep(1.05)
    archive_db.update_job(job)          # heartbeat advances...
    cpu["value"] = 100.0 + 1.5          # ...while the worker burns 1.5s CPU
    time.sleep(0.05)
    assert _reclaim(holders) == [], (
        "a throttled-but-working job must not be reclaimed")

    # And it stays alive across several such windows, then is released only
    # once the CPU ALSO goes flat.
    for _ in range(3):
        time.sleep(1.05)
        archive_db.update_job(job)
        cpu["value"] += 1.5
        assert _reclaim(holders) == []
    cpu["value"] += 0.0
    time.sleep(1.05)
    archive_db.update_job(job)
    assert _reclaim(holders) == [job], (
        "once the worker stops burning CPU too, the row must be released")


# --- 2. the reclaimed job lands in a state the retry path understands ------

def test_reclaimed_job_is_retryable_with_attempts_incremented(logf, cpu):
    """Requirement: never a job left 'running' with no owner. The release goes
    through update_job(status='failed'), the EXISTING retry machinery, so it
    gets attempts+1 and a next_retry_at backoff."""
    job = _running_chat_job()
    before = _row(job)
    assert before["attempts"] == 0

    holders: dict = {}
    assert _reclaim(holders) == []
    time.sleep(1.05)
    archive_db.update_job(job)
    assert _reclaim(holders) == [job]

    row = _row(job)
    assert row["status"] == "queued", f"job left in {row['status']} with no owner"
    assert row["attempts"] == 1, f"attempt not counted: {row['attempts']}"
    assert row["next_retry_at"], "requeued without a retry deadline"
    assert "not working" in (row["error"] or ""), row["error"]

    # And the real claim path really offers it again once the deadline passes.
    from services.archive_transcribe import _claim_next_job
    archive_db.execute(
        "UPDATE archive_jobs SET next_retry_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", job))
    claimed = _claim_next_job()
    assert claimed and claimed["id"] == job, "requeued job is not claimable"


def test_repeated_reclaim_terminates_instead_of_looping_forever(logf, cpu):
    """The immortality breaker. Pre-fix, a wedged 'chat' row was re-claimed
    every 2 h by a running->running CAS that counted no attempt, so attempts
    stayed 0 and max_attempts was never reached — the two-month-old job in
    the module docstring. Every reclaim here costs an attempt, so the row
    reaches a terminal state instead of looping forever."""
    job = _running_chat_job()
    terminal = None
    for _ in range(6):
        archive_db.update_job(job, status="running", progress=0.0)
        holders: dict = {}
        _reclaim(holders)          # arm
        time.sleep(1.05)
        archive_db.update_job(job)  # heartbeat advances, no progress
        _reclaim(holders)
        if _row(job)["status"] == "failed":
            terminal = _row(job)
            break
    assert terminal is not None, (
        "repeated reclaims never reached a terminal state — the row would "
        "be relaunched forever, which is the bug being fixed")
    assert terminal["attempts"] >= terminal["max_attempts"]


# --- 3. conservatism: what the predicate must never touch ------------------

def test_queued_backlog_is_never_touched(logf, cpu):
    """The healthy 157-job transcribe backlog has attempts=0 and
    next_retry_at=NULL — genuinely waiting. A queue is not a fault, so a
    queued row must be invisible to the reaper no matter how long it waits."""
    for i in range(5):
        archive_db.enqueue_job(f"queued-{i}", "transcribe", "twitch", f"v{i}")
    holders: dict = {}
    for _ in range(3):
        time.sleep(1.05)
        assert _reclaim(holders) == [], "the reaper claimed a QUEUED job"
    rows = archive_db.query(
        "SELECT status, attempts FROM archive_jobs ORDER BY id")
    assert all(dict(r)["status"] == "queued" for r in rows)
    assert all(dict(r)["attempts"] == 0 for r in rows)


def test_a_silent_frozen_job_is_left_to_the_existing_reclaim(logf, cpu):
    """A row nobody touches at all is NOT this reaper's case: the existing
    stale-window reclaim (_claim_next_job) owns that, and duplicating it here
    would be a second liveness vocabulary fighting the first."""
    job = _running_chat_job()
    holders: dict = {}
    assert _reclaim(holders) == []
    for _ in range(3):
        time.sleep(1.05)
        assert _reclaim(holders) == [], (
            "the reaper stole a job that is merely untouched")
    assert _row(job)["status"] == "running"


def test_release_is_a_compare_and_set_on_running(logf, cpu):
    """If an executor moved the row between the reaper's read and its write,
    the release must match nothing rather than clobber real work."""
    job = _running_chat_job()
    assert archive_db.update_job(job, progress=0.0) is True
    # Still 'running' -> the CAS writes.
    assert archive_db.update_job(job, status="queued", expect_status="running") is True
    # Now 'queued' -> a release expecting 'running' must be a no-op.
    assert archive_db.update_job(job, status="failed",
                                 expect_status="running") is False
    assert _row(job)["status"] == "queued", "the CAS clobbered a moved row"


def test_work_marks_exclude_the_heartbeat_column():
    """The whole fix rests on this: a heartbeat advance must not register as
    work, or the predicate is the old one with extra steps."""
    job = _running_chat_job("chat-test-marks")
    first = archive_db.running_job_work_marks()[job]
    time.sleep(1.05)
    archive_db.update_job(job)  # progress-free: stamps heartbeat only
    assert _row(job)["heartbeat"], "sanity: the heartbeat did advance"
    second = archive_db.running_job_work_marks()[job]
    assert first == second, (
        "a progress-free heartbeat refresh leaked into the work marks")
    archive_db.update_job(job, progress=0.25)
    assert archive_db.running_job_work_marks()[job] != first, (
        "real progress must move the work marks")


# --- 4. the reclaim itself charges the relaunch ---------------------------

def test_reclaim_costs_an_attempt_and_sets_a_retry_backoff():
    """THE CONTRACT CHANGE. This test used to be
    `test_pre_fix_reclaim_relaunches_the_row_without_counting_an_attempt` and
    it asserted the BUG:

        assert row["attempts"] == 0, "... if this ever becomes true the loop
        terminates on its own and this fix is redundant"
        assert row["next_retry_at"] is None

    That assertion is the defect, so it was deliberately inverted rather than
    deleted. It runs the real, unmodified reclaim
    (services.archive_transcribe._claim_next_job) against a stale twitch chat
    row — the CAS is platform-independent; twitch avoids the YouTube bot gate
    — so the fix is demonstrated, not merely described.

    The production row is NOT invisible to the old rule: at 2026-10-04T22:30Z
    its 09:41Z heartbeat is 12.8 h old, well past the 2 h chat window, so
    _claim_next_job DID reclaim it, on schedule, every 2 h. The relaunched
    row re-stamped its own heartbeat, which is what made the advancing
    heartbeat look like liveness. Now the relaunch spends an attempt and sets
    a backoff, so max_attempts is reachable and the loop is bounded."""
    from services.archive_transcribe import _claim_next_job

    job, old = _stale_running_chat_job("chat-test-prefix-loop")

    assert _row(job)["attempts"] == 0, "precondition: a fresh row has no budget spent"
    reclaimed = _claim_next_job()
    assert reclaimed and reclaimed["id"] == job, (
        "precondition: the stale-window reclaim owns this row")

    row = _row(job)
    assert row["status"] == "running", "expected the running->running relaunch"
    assert row["heartbeat"] != old, "the relaunch re-stamped the heartbeat"
    # --- the inverted assertions -------------------------------------------
    assert row["attempts"] == 1, (
        f"a reclaim is a relaunch and must cost an attempt, got "
        f"{row['attempts']}")
    assert row["next_retry_at"], (
        "a reclaim must leave a retry deadline; NULL means the next claim is "
        "unguarded")
    # The deadline is in the FUTURE: the row is already claimed, so this only
    # bounds the NEXT claim if the row falls back to 'queued'.
    assert row["next_retry_at"] > row["heartbeat"], (
        f"backoff {row['next_retry_at']} is not after the claim "
        f"{row['heartbeat']}")


def test_reclaim_is_terminal_at_max_attempts():
    """The immortality breaker, on the reclaim path itself. The two-month-old
    row would have gone attempts 0 -> 1 on its first cycle, 1 -> 2 on its
    second, and been PARKED on the third instead of relaunched a fourth."""
    from services.archive_transcribe import _claim_next_job

    job, _old = _stale_running_chat_job("chat-test-reclaim-exhaust")
    max_attempts = _row(job)["max_attempts"]
    assert max_attempts == 3

    statuses = []
    # The max_attempts-th reclaim is the one that exhausts the budget, and it
    # PARKS instead of relaunching — so the row is handed out
    # max_attempts-1 times, never max_attempts times.
    for cycle in range(1, max_attempts):
        claimed = _claim_next_job()
        assert claimed and claimed["id"] == job, (
            f"cycle {cycle}: the stale row stopped being claimable")
        assert _row(job)["attempts"] == cycle, (
            f"cycle {cycle} charged {_row(job)['attempts']} attempts")
        statuses.append(_row(job)["status"])
        _age_the_heartbeat(job)

    # The reclaim that spends the last attempt parks the row and hands it to
    # nobody. This is the assertion that was impossible pre-fix: with attempts
    # frozen at 0 this call returned the row again, forever.
    assert _claim_next_job() is None, (
        "an exhausted row was relaunched again — the loop is not bounded")
    row = _row(job)
    assert row["status"] == "failed", f"expected a parked row, got {row['status']}"
    assert row["attempts"] == max_attempts, (
        f"attempts must reach max_attempts exactly, got {row['attempts']}")
    assert row["next_retry_at"] is None, "a parked row keeps no retry deadline"
    assert statuses == ["running"] * (max_attempts - 1)


def test_parked_reclaim_reports_no_work_not_a_user_cause():
    """A reclaim is NOT a crash. Parking must not borrow update_job's
    error-string classifiers (DownloadError / bot-gate / age gate / '429'),
    which would blame a cause the user never produced, and it must not demand
    any action. The verdict is the budget plus what the reclaim observed."""
    from services.archive_transcribe import _claim_next_job

    job, _old = _stale_running_chat_job("chat-test-park-reason")
    for _ in range(_row(job)["max_attempts"]):
        _age_the_heartbeat(job)
        _claim_next_job()          # the last one parks instead of returning
    row = _row(job)
    assert row["status"] == "failed", f"precondition: the row parks ({row['status']})"
    err = row["error"] or ""
    assert "no recorded work" in err, err
    assert "3/3" in err, f"the message must state the budget it spent: {err}"
    for marker in ("429", "rate limit", "bot-gate", "DownloadError",
                   "age gate", "sign in", "log in", "credentials"):
        assert marker not in err, (
            f"a reclaim borrowed the failure path's blame: {marker!r} in {err!r}")


def test_ordinary_claim_of_a_queued_row_is_free():
    """Only a RECLAIM costs an attempt. A normal queued -> running claim must
    not spend budget, or an ordinary retried job would exhaust max_attempts
    without ever failing — and the crash path's arithmetic would silently
    change underneath it."""
    from services.archive_transcribe import _claim_next_job

    archive_db.enqueue_job("chat-test-free-claim", "chat", "twitch", "vfree")
    for i in range(5):
        archive_db.update_job("chat-test-free-claim", status="running", progress=0.0)
        archive_db.execute(
            "UPDATE archive_jobs SET status = 'queued' WHERE id = ?",
            ("chat-test-free-claim",))
        claimed = _claim_next_job()
        assert claimed and claimed["id"] == "chat-test-free-claim", i
        assert _row("chat-test-free-claim")["attempts"] == 0, (
            "an ordinary claim must not spend the reclaim budget")


def test_a_working_job_is_not_reclaimed():
    """Nothing about the fix may cost a healthy job anything. A row whose
    executor is touching it (fresh coarse heartbeat) is invisible to the
    reclaim, so it is neither charged nor parked."""
    from services.archive_transcribe import _claim_next_job

    job = _running_chat_job("chat-test-working")
    for _ in range(3):
        time.sleep(1.05)
        archive_db.update_job(job, progress=0.25)   # real work
        assert _claim_next_job() is None, "a working job was reclaimed"
    row = _row(job)
    assert row["status"] == "running"
    assert row["attempts"] == 0, "a working job was charged an attempt"


def test_a_throttled_but_working_job_is_not_reclaimed():
    """Steady Watcher holds this box to 7-14% CPU with a ~13.6x wall
    multiplier, so a working job can legitimately be quiet for a long time.
    Its only defence is that it keeps touching the row — which must stay free,
    since a throttle must never be what drains a job's budget."""
    from services.archive_transcribe import _claim_next_job

    job = _running_chat_job("chat-test-throttled")
    for _ in range(3):
        time.sleep(1.05)
        archive_db.update_job(job)   # liveness touch only, like a watchdog
        assert _claim_next_job() is None, (
            "a throttled-but-working job was reclaimed")
    assert _row(job)["status"] == "running"
    assert _row(job)["attempts"] == 0, "a working job was charged an attempt"


# --- 5. the watchdog's liveness touch is no longer work -------------------

def test_watchdog_liveness_touch_does_not_reset_the_staleness_window():
    """The deeper half. `update_job` stamps heartbeat+updated_at on EVERY
    call, and every bare `update_job(job_id)` in the backend is a watchdog
    refreshing the row so the coarse stale window does not fire mid-download
    (archive_transcribe._dl_progress, _fetch_heartbeat, archive_twitch's page
    heartbeat). Because a refresh costs nothing, a STUCK download kept its
    row alive forever on that signal alone.

    work_heartbeat is the second stamp: it advances only on a work-bearing
    update (progress and/or status) or a real claim, so a progress-free touch
    cannot move it. That is the discriminator — 'no work happened' is a stored
    fact, not something a reaper has to infer from a timestamp anyone can
    refresh for free."""
    job = _running_chat_job("chat-test-watchdog")
    claimed = _row(job)
    assert claimed["work_heartbeat"], "precondition: a claimed row has the stamp"

    before_beat = _row(job)["heartbeat"]
    before_work = _row(job)["work_heartbeat"]
    time.sleep(1.05)

    # Exactly what the download watchdog issues every 5 min: no work.
    archive_db.update_job(job)

    row = _row(job)
    assert row["heartbeat"] != before_beat, (
        "sanity: the coarse liveness heartbeat must still advance — the fetch "
        "watchdog's job is to hold the coarse window open")
    assert row["work_heartbeat"] == before_work, (
        "a progress-free liveness touch reset the staleness window — a stuck "
        "download can never age out while a watchdog refreshes it")

    # Real work DOES move it, so the window is not merely frozen.
    time.sleep(1.05)
    archive_db.update_job(job, progress=0.3)
    assert _row(job)["work_heartbeat"] != before_work, (
        "real work must advance the staleness window")


def test_a_stuck_download_ages_out_on_the_work_signal_while_the_watchdog_runs():
    """The scenario the second stamp exists for, end to end. The coarse
    heartbeat is refreshed every tick (a healthy, non-suspending download);
    the work signal is never touched, because nothing is being computed. Only
    the second one freezes, so a watchdog cannot make a wedged download look
    alive indefinitely."""
    job = _running_chat_job("chat-test-stuck-download")
    work = _row(job)["work_heartbeat"]
    for _ in range(3):
        time.sleep(1.05)
        archive_db.update_job(job)  # the watchdog, forever
    row = _row(job)
    assert row["status"] == "running", "precondition: the watchdog holds it"
    assert row["heartbeat"], "the coarse heartbeat was refreshed"
    assert row["work_heartbeat"] == work, (
        "the stuck download's staleness window was reset by its own watchdog")

    # The reclaim is deliberately NOT built on this signal — that would fire
    # on a legitimately slow fetch, which the brief forbids — so the coarse
    # stale window is what governs it, unchanged. Assert the separation that
    # makes the distinction available at all.
    assert row["work_heartbeat"] < row["heartbeat"], (
        "precondition: the two signals are actually distinguishable")


def test_a_claim_stamps_the_work_signal_even_though_a_watchdog_touch_does_not():
    """An executor CLAIMING the job is the event the stamp means, and it is
    distinguishable from the watchdog that later refreshes the same row."""
    from services.archive_transcribe import _claim_next_job

    job = _running_chat_job("chat-test-claim-stamp")
    before = _row(job)["work_heartbeat"]
    time.sleep(1.05)
    archive_db.update_job(job)          # watchdog touch: must not move it
    assert _row(job)["work_heartbeat"] == before
    # Re-arm as claimable so the real claim path runs.
    archive_db.execute(
        "UPDATE archive_jobs SET status = 'queued', heartbeat = NULL, "
        "work_heartbeat = NULL, next_retry_at = NULL WHERE id = ?", (job,))
    claimed = _claim_next_job()
    assert claimed and claimed["id"] == job
    assert _row(job)["work_heartbeat"], "the claim did not stamp the work signal"


def test_reaper_release_and_reclaim_release_both_cost_exactly_one_attempt(
        logf, cpu):
    """The reaper's release is the OTHER route to the same budget, and it is
    unaffected by the reclaim fix. It still goes through the real
    update_job(status='failed') retry machinery, so attempts increments and a
    next_retry_at backoff appears.

    Renamed from `test_reaper_is_what_makes_the_relaunch_cost_an_attempt`:
    the reclaim now costs an attempt itself, so the reaper is no longer the
    only thing that charges one. Assertion values are unchanged — this is a
    rationale fix, not a contract relaxation.

    The final assertion is the one that keeps the two routes honest: the
    relaunch here is an ordinary queued -> running claim, NOT a reclaim, so
    it is free. If a future edit makes every claim charge the budget, an
    ordinary retried job would exhaust max_attempts without ever failing."""
    from services.archive_transcribe import _claim_next_job

    job, old = _stale_running_chat_job("chat-test-prefix-loop")

    holders: dict = {}
    assert _reclaim(holders) == []                     # arm
    time.sleep(1.05)
    archive_db.update_job(job)                         # re-stamp, no progress
    assert _reclaim(holders) == [job]                  # release
    row = _row(job)
    assert row["attempts"] == 1, f"attempt not counted: {row['attempts']}"
    assert row["next_retry_at"], "no retry deadline"
    # The relaunch path still works, and does not double-charge: the reaper
    # left the row 'queued', so this is a plain claim.
    archive_db.execute(
        "UPDATE archive_jobs SET next_retry_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", job))
    relaunched = _claim_next_job()
    assert relaunched and relaunched["id"] == job
    assert _row(job)["attempts"] == 1, (
        "an ordinary claim of a requeued row must not spend a second attempt")


def test_bound_is_derived_and_far_above_a_healthy_gap():
    """The bound must stay well clear of the longest healthy gap between work
    marks, or a slow job is a false positive waiting to happen."""
    assert ws.JOB_LIVENESS_BOUND_S == 1350.0
    # A twitch chat page under 429 backoff is the slowest healthy producer
    # (4-5 min, per archive_transcribe._CHAT_HEARTBEAT_STALE's derivation).
    assert ws.JOB_LIVENESS_BOUND_S > 300.0 * 4, "no headroom over a healthy gap"
    # Same CPU floor as the stall watchdog — one liveness vocabulary, not two.
    assert ws._JOB_LIVENESS_CPU_FLOOR_S == ws._STALL_CPU_FLOOR_S
