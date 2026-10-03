"""Claim-time queue policy: one VOD at a time, and user focus.

These are the two gates inside _claim_next_job that turn the pool's lanes
from "one VOD per lane" into "every lane cooperating on one VOD", plus the
scheduler's autonomous-enqueue un-gate.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

# Pin this module to its OWN scratch DB before archive_db is imported. The
# suite shares one process-level scratch archive, so a job row another module
# left in 'running' (a GPU-gate scratch row, a leftover transcribe claim) makes
# the concurrency cap in _claim_next_job read as already-saturated and these
# tests fail depending on run order. An isolated DB removes the coupling
# entirely - same pattern as test_yt_gate.py.
_TMP = Path(tempfile.mkdtemp(prefix="queue-claim-"))
_DB = _TMP / "archive.db"
sqlite3.connect(str(_DB)).close()
os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)

import pytest  # noqa: E402

from services import archive_db, archive_scheduler, queue_policy  # noqa: E402
from services import archive_transcribe as at  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _module_scratch_db():
    """Re-bind archive_db's global connection to THIS module's DB.

    Setting the env at import is not enough: conftest.py runs first and may
    already have opened the shared scratch archive, so archive_db._conn still
    points there. Rebinding the connection is what actually retargets the
    module - otherwise job rows other modules left behind (a 'running'
    transcribe claim, a scratch GPU-gate row) make the concurrency cap read
    as saturated and these tests fail by run order."""
    prev_env = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)
    with archive_db._lock:
        prev_conn = archive_db._conn
        prev_ready = archive_db._schema_ready
        archive_db._conn = None
        archive_db._schema_ready = False
    archive_db.get_conn()
    yield
    with archive_db._lock:
        archive_db._conn = prev_conn
        archive_db._schema_ready = prev_ready
    if prev_env is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev_env


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(at, "_transcribe_cap", [0])
    # _claim_next_job's behaviour depends on the lane plan, and the plan
    # builder is lru_cache'd off device detection + env. A previous test
    # module that monkeypatched VODRIP_WHISPER_DEVICE / a GPU probe leaves a
    # cached verdict behind, so a claim can return None here for reasons that
    # have nothing to do with this test. Drop the cached plan on both sides.
    at._detect_device.cache_clear()
    # The scheduler enqueues under the derived id 'transcribe-<plat>-<vid>',
    # so the cleanup must match the vid AND the derived id — a leftover
    # queued row keeps the legacy idle gate open and would silently pass a
    # neighbouring test.
    archive_db.execute("DELETE FROM archive_jobs WHERE id LIKE '%clm-%'")
    archive_db.execute("DELETE FROM videos WHERE channel LIKE 'clm-%'")
    archive_db.clear_user_focus()
    monkeypatch.delenv(queue_policy.ENV_FOCUS_PAUSE, raising=False)
    monkeypatch.delenv(queue_policy.ENV_AUTO_TRANSCRIBE, raising=False)
    yield
    archive_db.execute("DELETE FROM archive_jobs WHERE id LIKE '%clm-%'")
    archive_db.execute("DELETE FROM videos WHERE channel LIKE 'clm-%'")
    archive_db.clear_user_focus()
    at._detect_device.cache_clear()


def _job(kind: str, vid: str, *, platform: str = "twitch", priority: int = 0) -> None:
    archive_db.enqueue_job(f"clm-{kind}-{vid}", kind, platform, vid, priority=priority)


def _running_transcribe_count() -> int:
    return int(archive_db.query(
        "SELECT COUNT(*) AS n FROM archive_jobs "
        "WHERE kind='transcribe' AND status='running'")[0]["n"])


# --- one VOD at a time ---------------------------------------------------
def test_cap_one_claims_a_single_transcribe_job_at_a_time():
    at._transcribe_cap[0] = 1
    _job("transcribe", "v1")
    _job("transcribe", "v2")
    first = at._claim_next_job()
    assert first is not None
    # The second VOD is NOT claimable while the first one runs.
    assert _running_transcribe_count() == 1
    assert at._claim_next_job() is None
    # Once it finishes, the next VOD flows.
    archive_db.update_job(first["id"], status="done", progress=1.0)
    second = at._claim_next_job()
    assert second is not None and second["id"] != first["id"]


def test_cap_never_starves_chat_jobs():
    """A 13-hour VOD must not block every chat backfill behind it."""
    at._transcribe_cap[0] = 1
    _job("transcribe", "v1")
    _job("chat", "c1")
    first = at._claim_next_job()
    assert first["kind"] == "transcribe"
    nxt = at._claim_next_job()
    assert nxt is not None and nxt["kind"] == "chat", (
        "chat/events are not transcribe jobs and must keep draining")


def test_cap_zero_restores_the_legacy_multi_vod_pool():
    at._transcribe_cap[0] = 0
    _job("transcribe", "v1")
    _job("transcribe", "v2")
    first = at._claim_next_job()
    second = at._claim_next_job()
    assert first is not None and second is not None
    assert _running_transcribe_count() == 2


def test_worker_reads_the_configured_cap(monkeypatch):
    """run_worker takes the cap from queue_policy, not from a literal."""
    monkeypatch.setenv(queue_policy.ENV_JOB_CONCURRENCY, "0")
    assert queue_policy.transcribe_job_concurrency() == 0
    monkeypatch.setenv(queue_policy.ENV_JOB_CONCURRENCY, "1")
    assert queue_policy.transcribe_job_concurrency() == 1


# --- user focus ----------------------------------------------------------
def test_focus_gives_the_queue_to_the_focused_vod():
    _job("transcribe", "v1", priority=0)
    _job("transcribe", "v2", priority=0)
    archive_db.set_user_focus("twitch", "v2")
    claimed = at._claim_next_job()
    assert claimed is not None and claimed["video_id"] == "v2", (
        "the focused item must win the transcribe queue")
    # The other VOD waits while the user is on v2.
    assert at._claim_next_job() is None


def test_focus_beats_a_higher_priority_queued_job():
    _job("transcribe", "v1", priority=200)
    _job("transcribe", "v2", priority=0)
    archive_db.set_user_focus("twitch", "v2")
    claimed = at._claim_next_job()
    assert claimed["video_id"] == "v2", (
        "focus is the strongest signal, above the preview tier")


def test_focus_does_not_block_chat_jobs():
    _job("transcribe", "v1")
    _job("chat", "c1")
    archive_db.set_user_focus("twitch", "v1")
    first = at._claim_next_job()
    assert first["kind"] == "transcribe"
    nxt = at._claim_next_job()
    assert nxt is not None and nxt["kind"] == "chat"


def test_expired_focus_releases_the_queue():
    _job("transcribe", "v1")
    _job("transcribe", "v2")
    archive_db.execute(
        "UPDATE user_focus SET focused_at = '2000-01-01T00:00:00+00:00'")
    claimed = at._claim_next_job()
    assert claimed is not None, "a stale focus must never wedge the queue"


def test_focus_disabled_leaves_the_queue_alone(monkeypatch):
    monkeypatch.setenv(queue_policy.ENV_FOCUS_PAUSE, "0")
    _job("transcribe", "v1", priority=0)
    _job("transcribe", "v2", priority=200)
    archive_db.set_user_focus("twitch", "v1")
    claimed = at._claim_next_job()
    assert claimed["video_id"] == "v2", "focus pause off -> plain priority order"


# --- scheduler: autonomous enqueue --------------------------------------
def _seed_channel_video(vid: str, started: str, *, platform: str = "twitch") -> None:
    archive_db.upsert_video({
        "platform": platform, "video_id": vid, "channel": "clm-chan",
        "title": vid, "started_at": started, "duration_sec": 1200.0,
        "status": "ready",
    })


def test_scheduler_enqueues_on_an_idle_queue():
    """The BOOT-02 regression: an idle queue used to enqueue NOTHING.

    Adding a channel ingests metadata; with the gate closed the video was
    simply never transcribed until the user opened or searched it."""
    _seed_channel_video("clm-fresh", "2026-08-01T00:00:00Z")
    assert archive_db.query(
        "SELECT 1 FROM archive_jobs WHERE id='transcribe-twitch-clm-fresh'") == []
    archive_scheduler._enqueue_transcriptions()
    assert archive_db.query(
        "SELECT 1 FROM archive_jobs WHERE id='transcribe-twitch-clm-fresh'") != [], (
        "a fresh channel must produce transcribe work on its own")


def test_scheduler_idle_gate_still_available_when_disabled(monkeypatch):
    """Opting out restores the strict opt-in behaviour."""
    monkeypatch.setenv(queue_policy.ENV_AUTO_TRANSCRIBE, "0")
    _seed_channel_video("clm-optout", "2026-08-01T00:00:00Z")
    archive_scheduler._enqueue_transcriptions()
    assert archive_db.query(
        "SELECT 1 FROM archive_jobs WHERE id='transcribe-twitch-clm-optout'") == []


def test_scheduler_prefers_the_latest_per_channel(monkeypatch):
    """Only the newest N of a channel become candidates."""
    monkeypatch.setenv(queue_policy.ENV_LATEST_PER_CHANNEL, "1")
    monkeypatch.setenv("VODRIP_TRANSCRIBE_QUEUE_PER_PASS", "50")
    for i in range(4):
        _seed_channel_video(f"clm-latest{i}", f"2026-08-0{i + 1}T00:00:00Z")
    archive_scheduler._enqueue_transcriptions()
    enqueued = {r["video_id"] for r in archive_db.query(
        "SELECT video_id FROM archive_jobs WHERE id LIKE 'transcribe-twitch-clm-latest%'")}
    assert enqueued == {"clm-latest3"}, enqueued
