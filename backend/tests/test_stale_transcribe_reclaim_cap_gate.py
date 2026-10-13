"""A stale 'running' transcribe row must not block its own reclaim.

Production evidence (read-only from H:\\VOD.RIP-data\\archive.db, 2026-10-13):

    id=transcribe-twitch-2873292667  kind=transcribe  status=running
    heartbeat=2026-10-05T17:03:03Z  age=193.4 h  attempts=0  progress=0.053

ONE row, heartbeat frozen eight days, attempts still 0, and 480 transcribe
jobs queued behind it. The queue is not draining because of it.

WHY: _claim_next_job's job-concurrency gate (default 1, one VOD at a time)
counts EVERY 'running' transcribe row - including one whose heartbeat is
eight days past the 45-minute stale window. The gate opens
kinds_sql = 'events','chat', so the dead row is never even SELECTed, and the
stale-window reclaim (the crash-safety this repo's worker docstring promises)
can never fire for it. The job-liveness reaper defers to the same reclaim by
design ("not this reaper's case - the existing stale-window reclaim owns
it"), so between the two nothing releases the row. Measured on this box: a
controlled reproduction of that exact state leaves the row unclaimed after
120 s with the real worker running.

THE FIX: the gate counts only rows whose heartbeat is still inside the stale
window. A row past the window is not consuming a slot - it is an orphan - so
it must not count against the cap. The stale-reclaim CAS itself is untouched
(one attempt, one backoff, one winner), so one-VOD-at-a-time for LIVE work
and crash recovery for DEAD work both hold.

RED ARM: test_a_stale_running_row_is_reclaimable_under_the_default_cap is the
arm. Before the fix it fails (claim -> None, the production bug); after the
fix it passes (claim -> the stale row, attempts 0 -> 1). The other tests
pin the behaviour the fix must NOT change.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Pin this module to its OWN scratch DB before archive_db is imported - same
# pattern as test_queue_claim_policy.py (a leftover 'running' row from another
# module makes the concurrency cap read as saturated and the result depends on
# run order).
_TMP = Path(tempfile.mkdtemp(prefix="stale-reclaim-"))
_DB = _TMP / "archive.db"
sqlite3.connect(str(_DB)).close()
os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)

import pytest  # noqa: E402

from services import archive_db  # noqa: E402
from services import archive_transcribe as at  # noqa: E402
from services.archive_transcribe import _claim_next_job, _STALE_JOB_TIMEDELTA  # noqa: E402

STALE_SECONDS = int(_STALE_JOB_TIMEDELTA.total_seconds())


@pytest.fixture(scope="module", autouse=True)
def _module_scratch_db():
    prev_env = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)
    with archive_db._lock:
        prev_conn, prev_ready = archive_db._conn, archive_db._schema_ready
        archive_db._conn = None
        archive_db._schema_ready = False
    archive_db.get_conn()
    yield
    with archive_db._lock:
        archive_db._conn, archive_db._schema_ready = prev_conn, prev_ready
    if prev_env is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev_env


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    at._detect_device.cache_clear()
    archive_db.execute("DELETE FROM archive_jobs")
    archive_db.execute("DELETE FROM worker_heartbeats")
    yield
    archive_db.execute("DELETE FROM archive_jobs")
    archive_db.execute("DELETE FROM worker_heartbeats")
    at._detect_device.cache_clear()


def _stale(job_id: str, *, seconds: float) -> None:
    old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    archive_db.execute(
        "UPDATE archive_jobs SET heartbeat=?, updated_at=?, work_heartbeat=? WHERE id=?",
        (old, old, old, job_id))


def _row(job_id: str) -> dict:
    r = archive_db.query("SELECT * FROM archive_jobs WHERE id=?", (job_id,))
    return dict(r[0]) if r else {}


def _queued(job_id: str, vid: str, kind: str = "transcribe") -> None:
    archive_db.enqueue_job(job_id, kind, "twitch", vid)
    archive_db.update_job(job_id, status="queued")


# ---------------------------------------------------------------- the red arm
def test_a_stale_running_row_is_reclaimable_under_the_default_cap():
    """THE ARM. The live production state: one 'running' transcribe row whose
    heartbeat is past the stale window (8 days, like the live row), three
    queued jobs behind it, and the production cap of 1.

    The queue must FLOW, and the dead row must eventually be RECLAIMED - not
    sit there pinning every other transcribe job behind it forever.
    """
    at._transcribe_cap[0] = 1          # settings.archive_transcribe_concurrency
    dead = "transcribe-twitch-deadvod"
    archive_db.enqueue_job(dead, "transcribe", "twitch", "deadvod")
    archive_db.update_job(dead, status="running", progress=0.11)
    _stale(dead, seconds=8 * 86400)                        # the live row's age
    for i in range(3):
        _queued(f"transcribe-twitch-q{i}", f"q{i}")

    # 1. the queue starts flowing again (before the fix: None - nothing
    #    transcribe was claimable at all)
    claimed = _claim_next_job()
    assert claimed is not None and claimed["kind"] == "transcribe", (
        "the stale-window reclaim never fired: the dead row counts toward the "
        "concurrency cap, so _claim_next_job never selects it and the whole "
        "transcribe queue is blocked behind it")

    # 2. drain the live claims; the dead row is then handed to a real executor,
    #    charged one attempt, with a retry deadline - the crash-safety the
    #    worker docstring promises.
    nxt = claimed
    for _ in range(8):
        if nxt is None or nxt["id"] == dead:
            break
        archive_db.update_job(nxt["id"], status="done", progress=1.0)
        nxt = _claim_next_job()
    row = _row(dead)
    assert row["attempts"] == 1, (
        "the dead row was never reclaimed: attempts=" + str(row["attempts"]))
    assert row["next_retry_at"], "a reclaim must leave a retry deadline"

# ------------------------------------------------- what must NOT change
def test_a_live_running_row_still_enforces_one_vod_at_a_time():
    """The cap's whole purpose: a VOD that IS being transcribed blocks the
    next one. A fresh heartbeat means live work, so the row still counts."""
    at._transcribe_cap[0] = 1
    archive_db.enqueue_job("transcribe-twitch-livevod", "transcribe", "twitch", "livevod")
    archive_db.update_job("transcribe-twitch-livevod", status="running", progress=0.5)
    _queued("transcribe-twitch-next", "next")

    assert _claim_next_job() is None, (
        "a live running VOD must still block a second transcribe claim")
    # ... and it is not charged an attempt for merely running
    assert _row("transcribe-twitch-livevod")["attempts"] == 0


def test_a_row_inside_the_stale_window_still_counts_as_running():
    """The boundary: a row just inside the window (e.g. a slow decode chunk
    refreshing its heartbeat every few minutes) is live work, not an orphan."""
    at._transcribe_cap[0] = 1
    archive_db.enqueue_job("transcribe-twitch-slowvod", "transcribe", "twitch", "slowvod")
    archive_db.update_job("transcribe-twitch-slowvod", status="running", progress=0.5)
    _stale("transcribe-twitch-slowvod", seconds=STALE_SECONDS - 60)
    _queued("transcribe-twitch-next2", "next2")

    assert _claim_next_job() is None, (
        "a row inside its stale window is live work and must hold the cap")
    assert _row("transcribe-twitch-slowvod")["attempts"] == 0


def test_chat_jobs_are_never_blocked_by_the_transcribe_cap():
    at._transcribe_cap[0] = 1
    archive_db.enqueue_job("transcribe-twitch-deadvod2", "transcribe", "twitch", "deadvod2")
    archive_db.update_job("transcribe-twitch-deadvod2", status="running", progress=0.0)
    _stale("transcribe-twitch-deadvod2", seconds=8 * 86400)
    _queued("chat-twitch-c1", "c1", kind="chat")

    claimed = _claim_next_job()
    assert claimed is not None and claimed["kind"] == "chat"


def test_cap_zero_behaviour_is_unchanged():
    """The legacy multi-VOD pool (VODRIP_TRANSCRIBE_JOB_CONCURRENCY=0) must
    keep reclaiming stale rows exactly as before the fix."""
    at._transcribe_cap[0] = 0
    archive_db.enqueue_job("transcribe-twitch-deadvod3", "transcribe", "twitch", "deadvod3")
    archive_db.update_job("transcribe-twitch-deadvod3", status="running", progress=0.0)
    _stale("transcribe-twitch-deadvod3", seconds=8 * 86400)

    claimed = _claim_next_job()
    assert claimed is not None and claimed["id"] == "transcribe-twitch-deadvod3"
