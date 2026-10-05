"""ASR stall watchdog — the external bound on a GIL-held decode deadlock.

The parakeet decode can hard-deadlock inside the native sherpa/onnxruntime
call. The wedged call HOLDS THE GIL, so nothing inside the child can detect
or time out the hang: an in-process heartbeat thread stops ticking, no
timeout fires. These tests therefore drive the REAL supervisor watchdog
(worker_server._stall_watchdog) against a REAL child process that reproduces
that exact signature — a native call entered through ctypes.PyDLL, which by
construction does NOT release the GIL, blocking forever on an event nobody
sets. The child runs a heartbeat thread first, so the test can also assert
the in-process watchdog provably CANNOT fire (the premise of the fix).

What is asserted is never "a timer fired":
  * the child process is actually GONE, verified by exit code against
    STILL_ACTIVE (OpenProcess still succeeds on a terminated-but-unreaped
    pid, and tasklist still lists it — both are why the previous lane's kill
    path reported killed processes as alive);
  * the job row is actually in a state the existing retry path understands
    (requeued with attempts+1 and a next_retry_at, or terminal 'failed' once
    max_attempts is spent) — never left 'running' with no owner;
  * a healthy-but-throttled child is NOT killed;
  * recovery lets the next job run.

No network, no ASR model, no production DB. Fresh VODRIP_ARCHIVE_DB per
module, mirroring test_archive_jobs_retry.py.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["VODRIP_ARCHIVE_DB"] = str(
    Path(tempfile.mkdtemp(prefix="asr-stall-sup-")) / "archive.db")

import pytest  # noqa: E402

from services import archive_db  # noqa: E402  (env must be set first)
import worker_server as ws  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent

# --- the wedged child -----------------------------------------------------
# mode "wedge":  park forever inside a GIL-HELD native call.
# mode "busy":   keep burning CPU and stamping the job heartbeat (a healthy
#                worker under a throttle — must never be killed).
# mode "quiet":  stay alive and healthy on CPU but stamp NO progress at all
#                for a while, then stamp again (a slow chunk / a network
#                fetch) — also must never be killed, because a throttled box
#                legitimately goes seconds without a completed 60 s chunk.
_WEDGE_CHILD = r'''
import ctypes, os, sys, threading, time

mode = sys.argv[1]
db = os.environ["VODRIP_ARCHIVE_DB"]
sys.path.insert(0, os.environ["VODRIP_BACKEND_DIR"])
from services import archive_db

_job = os.environ.get("VODRIP_TEST_JOB_ID") or ""
_alive = threading.Event()


def _stamp():
    """Stands in for the per-chunk update_job() heartbeat."""
    if _job:
        archive_db.update_job(_job, progress=0.5)


def _heartbeat():
    """A live in-process liveness signal, like worker_heartbeats.
    It is here to PROVE the premise: under 'wedge' this thread stops
    writing, so nothing inside the child could have timed the hang out."""
    while not _alive.is_set():
        archive_db.worker_heartbeat("test-wedge-child")
        time.sleep(0.25)


threading.Thread(target=_heartbeat, daemon=True).start()
time.sleep(1.0)
_stamp()
time.sleep(0.3)

if mode == "wedge":
    # ctypes.PyDLL does NOT release the GIL for the duration of the call —
    # the same property as the sherpa/onnxruntime call that deadlocks.
    # WaitForSingleObject on a real, never-signaled event blocks forever, so
    # the GIL is held for the rest of the process's life: the heartbeat
    # thread above stops writing and the main thread never returns.
    k32 = ctypes.PyDLL("kernel32", use_last_error=True)
    k32.CreateEventW.restype = ctypes.c_void_p
    k32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_wchar_p]
    ev = k32.CreateEventW(None, 1, 0, None)
    assert ev, "CreateEventW failed - cannot build the wedge"
    k32.WaitForSingleObject.restype = ctypes.c_ulong
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    rc = k32.WaitForSingleObject(ev, 0xFFFFFFFF)   # never returns
    sys.stderr.write("WaitForSingleObject unexpectedly returned %s\n" % rc)
    sys.exit(99)

if mode == "busy":
    # A healthy worker: burns CPU AND advances the job heartbeat.
    end = time.monotonic() + float(os.environ.get("VODRIP_TEST_BUSY_S", "20"))
    while time.monotonic() < end:
        x = 0
        for i in range(200000):
            x += i * i
        _stamp()
        time.sleep(0.2)
    sys.exit(0)

if mode == "quiet":
    # Alive and doing real CPU work, but silent on progress for
    # VODRIP_TEST_QUIET_S, then it stamps again. The worst false-positive
    # shape: slow chunk / network fetch under a throttle.
    end = time.monotonic() + float(os.environ.get("VODRIP_TEST_TOTAL_S", "25"))
    quiet_until = time.monotonic() + float(
        os.environ.get("VODRIP_TEST_QUIET_S", "12"))
    while time.monotonic() < end:
        x = 0
        for i in range(200000):
            x += i * i
        if time.monotonic() > quiet_until:
            _stamp()
            time.sleep(0.2)
    sys.exit(0)

sys.exit(2)
'''


@pytest.fixture(scope="module", autouse=True)
def _scratch_db():
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(
        Path(tempfile.mkdtemp(prefix="asr-stall-sup-")) / "archive.db")
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
def _isolate_running_rows():
    """Each test owns an empty job table.

    _mark_stalled_job_failed deliberately releases EVERY running transcribe
    row (the wedged child owns whatever it was running), and _claim_next_job
    returns the first CLAIMABLE row — so on a shared module-scoped DB one
    test's leftovers would be released or claimed by the next test's
    assertions."""
    archive_db.execute("DELETE FROM archive_jobs")
    archive_db.execute("DELETE FROM worker_heartbeats")
    yield


@pytest.fixture()
def logf(tmp_path):
    fh = (tmp_path / "wd.log").open("w+", encoding="utf-8")
    yield fh
    fh.close()


def _logtext(logf) -> str:
    logf.flush()
    logf.seek(0)
    return logf.read()


def _job_row(job_id: str) -> dict:
    rows = archive_db.query("SELECT * FROM archive_jobs WHERE id = ?", (job_id,))
    return dict(rows[0]) if rows else {}


def _make_running_job(job_id: str = "transcribe-test-wedge") -> str:
    """A claimed (status='running') transcribe job, as the child would hold."""
    archive_db.enqueue_job(job_id, "transcribe", "twitch", "testvid1")
    archive_db.update_job(job_id, status="running", progress=0.1)
    return job_id


def _spawn(mode: str, env_extra: dict | None = None):
    env = dict(os.environ)
    env["VODRIP_BACKEND_DIR"] = str(BACKEND)
    env["VODRIP_NO_DAEMONS"] = "1"
    env.update(env_extra or {})
    return subprocess.Popen(
        [sys.executable, "-c", _WEDGE_CHILD, mode],
        cwd=str(BACKEND), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace",
    )


# --- 1. the premise: the wedge really freezes the child's own heartbeat ----

def test_wedged_child_freezes_its_own_heartbeat_and_burns_no_cpu(logf):
    """The in-process watchdog provably CANNOT fire — the premise of an
    external supervisor. The child's own GIL-dependent heartbeat thread
    stops writing while it is wedged, and it consumes no CPU."""
    job = _make_running_job("transcribe-test-premise")
    proc = _spawn("wedge", {"VODRIP_TEST_JOB_ID": job})
    try:
        # Wait for the child to actually come up and start heartbeating.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if archive_db.worker_heartbeat_age("test-wedge-child") is not None:
                break
            time.sleep(0.2)
        beat0 = archive_db.worker_heartbeat_age("test-wedge-child")
        assert beat0 is not None, "child heartbeat never started"
        cpu0 = ws._process_cpu_seconds(proc.pid)
        assert cpu0 is not None

        # Let the child get into the native call, then sample.
        time.sleep(4.0)
        # Check it is STILL wedged BEFORE trusting any heartbeat reading —
        # otherwise a child that bailed out of the native call would look
        # like a perfect freeze and assert vacuously.
        assert proc.poll() is None, (
            f"child exited (rc={proc.poll()}) instead of wedging — read "
            f"{proc.stderr.read() if proc.stderr else ''!r}")
        time.sleep(6.0)
        age1 = archive_db.worker_heartbeat_age("test-wedge-child")
        cpu1 = ws._process_cpu_seconds(proc.pid)

        assert age1 is not None and age1 > 4.0, (
            f"child heartbeat kept advancing while wedged (age {age1}) — "
            "the wedge is not reproducing the GIL hold")
        assert (cpu1 - cpu0) < 0.25, (
            f"wedged child burned {(cpu1 - cpu0):.3f}s CPU — a spinning wedge, "
            "not the blocked signature this fix targets")
        assert proc.poll() is None, "wedged child exited; it must stay wedged"
    finally:
        proc.kill()
        proc.wait(timeout=15)


# --- 2. a wedged worker is detected and killed, and really dies -----------

def test_wedged_child_is_killed_within_the_bound_and_is_really_gone(logf):
    job = _make_running_job("transcribe-test-kill")
    proc = _spawn("wedge", {"VODRIP_TEST_JOB_ID": job})
    stop = threading.Event()
    thread = threading.Thread(
        target=ws._stall_watchdog,
        args=(logf, proc, stop), kwargs={"bound_s": 4.0, "poll_s": 1.0},
        daemon=True,
    )
    t0 = time.monotonic()
    try:
        thread.start()
        proc.wait(timeout=60)
        elapsed = time.monotonic() - t0
    finally:
        stop.set()
        thread.join(timeout=10)

    # Bounded: bound 4 s + up to 2 poll intervals of slack for arming.
    assert elapsed < 30, f"kill took {elapsed:.1f}s — not bounded"
    # The child is GONE — proved by exit code, not by OpenProcess/tasklist.
    assert proc.poll() is not None, "child still running after watchdog"
    assert not ws._process_is_alive(proc.pid), (
        "watchdog reported success but the process still reports ALIVE")
    assert ws._process_exit_code(proc.pid) != ws._STILL_ACTIVE
    assert "confirmed dead" in _logtext(logf), "kill was not confirmed in the log"


def test_liveness_probe_does_not_report_a_killed_process_as_alive(logf):
    """Guards the exact defect a previous lane shipped: a liveness probe
    that called killed processes alive. OpenProcess still succeeds on a
    terminated-but-unreaped pid, so assert the exit-code path disagrees."""
    proc = _spawn("quiet", {"VODRIP_TEST_TOTAL_S": "30", "VODRIP_TEST_QUIET_S": "25"})
    try:
        assert ws._process_is_alive(proc.pid), "live child reported dead"
        proc.kill()
        proc.wait(timeout=15)
        assert proc.poll() is not None
        assert not ws._process_is_alive(proc.pid), (
            "terminated child still reported alive — the liveness probe is "
            "trusting OpenProcess, which succeeds on unreaped pids")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=15)


# --- 3. the killed job lands in a state the retry path understands --------

def test_stall_killed_job_is_requeued_for_retry_not_left_running(logf):
    """Requirement: a stall-kill must be recorded as a failure the EXISTING
    retry machinery understands — never a job stuck 'running' with no owner."""
    job = _make_running_job("transcribe-test-requeue")
    assert _job_row(job)["status"] == "running"

    released = ws._mark_stalled_job_failed("ASR worker wedged: test", logf)
    assert released == [job]

    row = _job_row(job)
    assert row["status"] == "queued", f"job left in {row['status']}"
    assert row["attempts"] == 1, f"retry attempt not counted: {row['attempts']}"
    assert row["next_retry_at"], "requeued without a retry deadline"
    assert "wedged" in (row["error"] or "")
    # And the queue really offers it again once the deadline passes.
    from services.archive_transcribe import _claim_next_job
    archive_db.execute(
        "UPDATE archive_jobs SET next_retry_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", job))
    claimed = _claim_next_job()
    assert claimed and claimed["id"] == job, "requeued job is not claimable"


def test_repeated_stalls_exhaust_attempts_into_terminal_failed(logf):
    """The stall path must ride the same max_attempts curve as any other
    failure, so a video that wedges the worker every time ends 'failed'."""
    job = _make_running_job("transcribe-test-exhaust")
    terminal = None
    for _ in range(4):
        archive_db.update_job(job, status="running")
        ws._mark_stalled_job_failed("ASR worker wedged: test", logf)
        row = _job_row(job)
        if row["status"] == "failed":
            terminal = row
            break
    assert terminal is not None, "stall retries never reached a terminal state"
    assert terminal["attempts"] >= terminal["max_attempts"]
    # NB: a terminal row keeps a STALE next_retry_at from an earlier attempt —
    # update_job only sets it on the requeue branch, never clears it. That is
    # harmless (the claim query only selects status='queued') and is existing
    # retry-path behaviour, not something the stall path should change. What
    # matters is that the row is no longer claimable.


def test_stall_kill_and_release_together_leave_a_recoverable_job(logf):
    """End-to-end: real wedged child + real watchdog + real retry path."""
    job = _make_running_job("transcribe-test-e2e")
    proc = _spawn("wedge", {"VODRIP_TEST_JOB_ID": job})
    stop = threading.Event()
    thread = threading.Thread(
        target=ws._stall_watchdog,
        args=(logf, proc, stop), kwargs={"bound_s": 4.0, "poll_s": 1.0},
        daemon=True,
    )
    try:
        thread.start()
        proc.wait(timeout=60)
    finally:
        stop.set()
        thread.join(timeout=10)

    assert not ws._process_is_alive(proc.pid), "wedged child survived the kill"
    row = _job_row(job)
    assert row["status"] in ("queued", "failed"), (
        f"stall-killed job left in {row['status']} with no owner")
    assert row["status"] == "queued" and row["next_retry_at"], (
        f"first stall should requeue for retry, got {row}")
    # Recovery: the retry deadline passed, the next worker can claim it.
    archive_db.execute(
        "UPDATE archive_jobs SET next_retry_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", job))
    from services.archive_transcribe import _claim_next_job
    claimed = _claim_next_job()
    assert claimed and claimed["id"] == job, "recovery did not let the job run again"


# --- 4. healthy workers are never killed ---------------------------------

def test_busy_healthy_child_is_not_killed(logf):
    """A working child advances its heartbeat and burns CPU: healthy."""
    job = _make_running_job("transcribe-test-busy")
    proc = _spawn("busy", {"VODRIP_TEST_JOB_ID": job, "VODRIP_TEST_BUSY_S": "9"})
    stop = threading.Event()
    thread = threading.Thread(
        target=ws._stall_watchdog,
        args=(logf, proc, stop), kwargs={"bound_s": 4.0, "poll_s": 1.0},
        daemon=True,
    )
    try:
        thread.start()
        proc.wait(timeout=60)
    finally:
        stop.set()
        thread.join(timeout=10)
    assert proc.returncode == 0, (
        f"healthy busy child was killed (rc={proc.returncode})")
    assert "wedged" not in _logtext(logf), "healthy child flagged as wedged"


def test_throttled_child_that_stalls_progress_is_not_killed(logf):
    """The false-positive trap: alive and burning CPU, but no progress mark
    for longer than the bound (a slow chunk, or a network fetch on a
    throttled box). CPU consumption counts as progress, so no kill."""
    job = _make_running_job("transcribe-test-throttled")
    proc = _spawn("quiet", {
        "VODRIP_TEST_JOB_ID": job,
        "VODRIP_TEST_QUIET_S": "7",    # silent 7 s, bound is 4 s
        "VODRIP_TEST_TOTAL_S": "11",
    })
    stop = threading.Event()
    thread = threading.Thread(
        target=ws._stall_watchdog,
        args=(logf, proc, stop), kwargs={"bound_s": 4.0, "poll_s": 1.0},
        daemon=True,
    )
    try:
        thread.start()
        proc.wait(timeout=90)
    finally:
        stop.set()
        thread.join(timeout=10)
    assert proc.returncode == 0, (
        f"throttled-but-healthy child was killed (rc={proc.returncode}) — "
        "the watchdog cannot tell throttled from wedged")
    assert ws._process_is_alive(proc.pid) is False  # exited on its own, cleanly
    assert "wedged" not in _logtext(logf), "throttled child flagged as wedged"


# --- 6. recovery must not walk into the 15 min give-up cooldown -----------

def test_recovered_stall_is_not_counted_as_a_crash(logf):
    """main() gives up (and parks the queue for 15 min) after
    MAX_CONSECUTIVE_CRASHES non-zero exits. A stall the watchdog killed and
    released is a RECOVERY, so it must not consume that budget — otherwise
    three wedged videos idle the queue for a quarter hour, which is exactly
    the cost this fix exists to remove."""
    job = _make_running_job("transcribe-test-recover-count")
    for _ in range(3):
        # Re-arm BEFORE each wedge so the row the watchdog releases is the
        # one this iteration wedged on.
        archive_db.update_job(job, status="running")
        proc = _spawn("wedge", {"VODRIP_TEST_JOB_ID": job})
        stop = threading.Event()
        thread = threading.Thread(
            target=ws._stall_watchdog,
            args=(logf, proc, stop), kwargs={"bound_s": 4.0, "poll_s": 1.0},
            daemon=True,
        )
        try:
            thread.start()
            proc.wait(timeout=60)
        finally:
            stop.set()
            thread.join(timeout=10)
        assert not ws._process_is_alive(proc.pid), "wedged child survived"
        # Each recovered stall re-arms the flag the supervisor loop reads.
        assert ws._STALL_RECOVERED.is_set(), (
            "recovered stall did not signal the supervisor to keep serving")
        ws._STALL_RECOVERED.clear()

    # The job's own attempts still bound a video that always wedges, and the
    # row never sits 'running' with no owner.
    row = _job_row(job)
    assert row["attempts"] == 3
    assert row["status"] in ("queued", "failed"), (
        f"job left in {row['status']} with no owner")
    assert row["status"] == "failed", "a video that always wedges must end failed"


def test_unexplained_crash_still_counts_toward_the_give_up():
    """The pre-existing crash budget must be untouched for crashes the
    watchdog did not handle."""
    assert not ws._STALL_RECOVERED.is_set()
    assert ws.MAX_CONSECUTIVE_CRASHES == len(ws.BACKOFF_SECONDS) == 3


def test_kill_never_targets_a_pid_after_the_child_exited(logf):
    """Windows recycles PIDs, and the tree kill is necessarily PID-addressed.
    If the child already exited, taskkill /PID would hit a stranger — so an
    already-reaped child must be reported, never killed."""
    proc = _spawn("quiet", {"VODRIP_TEST_TOTAL_S": "30", "VODRIP_TEST_QUIET_S": "25"})
    try:
        proc.kill()
        proc.wait(timeout=15)
        rc = proc.poll()
        assert rc is not None
        # A second call must short-circuit on the Popen handle, not taskkill.
        got = ws._kill_child_tree(proc, logf)
        assert got == rc
        text = _logtext(logf)
        assert "not killing by pid" in text, (
            "an exited child was re-targeted by pid: " + text)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=15)


def test_end_to_end_recovery_bound_is_a_constant_sum():
    """The recovery must be a HARD bound, not a nominal one: every term from
    wedge to 'job back in the retry queue' is a constant we can add up."""
    assert ws.STALL_BOUND_S == 137.1
    assert ws.STALL_RECOVERY_BOUND_S == (
        ws._STALL_POLL_S + ws.STALL_BOUND_S + 30.0 + 15.0 + 1.0)
    # Comfortably below the 45 min stale-reclaim window it now pre-empts,
    # and far below the 13 h a wedged job was observed to sit 'running'.
    assert ws.STALL_RECOVERY_BOUND_S < 300.0


# --- 5. the pure decision function ---------------------------------------

def _holder():
    return {"armed": False, "last_marks": None, "last_progress_wall": 0.0,
            "cpu_baseline": None, "error": None}


def test_pure_state_needs_frozen_marks_and_no_cpu():
    h = _holder()
    assert ws._stall_state(h, 0.0, marks="a", cpu_seconds=10.0) is None
    # Marks frozen but the child is burning CPU -> progress, not a stall.
    assert ws._stall_state(h, 200.0, marks="a", cpu_seconds=10.5) is None
    # Marks frozen AND no CPU for longer than the bound -> stall.
    reason = ws._stall_state(h, 400.0, marks="a", cpu_seconds=10.5)
    assert reason and "wedged" in reason


def test_pure_state_resets_on_moving_marks():
    h = _holder()
    ws._stall_state(h, 0.0, marks="a", cpu_seconds=10.0)
    # Marks move at t=100 -> the clock (and the CPU baseline) reset there.
    assert ws._stall_state(h, 100.0, marks="b", cpu_seconds=10.0) is None
    assert ws._stall_state(h, 150.0, marks="b", cpu_seconds=10.0) is None
    # Frozen from t=100, so the verdict lands only past the bound from t=100.
    assert ws._stall_state(h, 100.0 + ws.STALL_BOUND_S - 1,
                           marks="b", cpu_seconds=10.0) is None
    assert ws._stall_state(h, 100.0 + ws.STALL_BOUND_S,
                           marks="b", cpu_seconds=10.0) is not None


def test_pure_state_never_stalls_before_the_bound():
    h = _holder()
    ws._stall_state(h, 0.0, marks="a", cpu_seconds=10.0)
    assert ws._stall_state(h, 0.0 + ws.STALL_BOUND_S - 0.1,
                           marks="a", cpu_seconds=10.0) is None


def test_pure_state_latches_and_inactive_never_stalls():
    h = _holder()
    ws._stall_state(h, 0.0, marks="a", cpu_seconds=10.0)
    reason = ws._stall_state(h, 500.0, marks="a", cpu_seconds=10.0)
    assert reason
    assert ws._stall_state(h, 600.0, marks="b", cpu_seconds=99.0) == reason
    assert ws._stall_state(h, 10**6, marks="a", cpu_seconds=10.0,
                           active=False) is None


def test_progress_marks_tolerate_an_unreachable_db(monkeypatch):
    """A DB hiccup must read as 'no evidence', never as a stall."""
    def boom(*_a, **_kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(archive_db, "query", boom)
    assert ws._progress_marks() == ""
    # And a dead child must not be reportable as stalled either.
    assert ws._process_cpu_seconds(99999999) is None


