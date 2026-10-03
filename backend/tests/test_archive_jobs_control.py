"""User control over the transcribe queue: pause / resume / prioritise / cancel.

Covers the three gaps this closes:
  * 'paused' is a real status a claim can never pick up;
  * the four control endpoints exist and refuse to preempt a RUNNING job
    (the invariant test_ws1_queue_priority.py pins for the preview hook);
  * the schema migration widens the CHECK on a REAL legacy table without
    dropping rows, columns or the retry-queue state.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

os.environ["VODRIP_ARCHIVE_DB"] = str(
    Path(tempfile.mkdtemp(prefix="archive-jobs-control-")) / "archive.db")

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app import app  # noqa: E402
from services import archive_db  # noqa: E402
from services.archive_transcribe import _claim_next_job  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    archive_db.execute("DELETE FROM archive_jobs WHERE id LIKE 'ctl-%'")
    yield
    archive_db.execute("DELETE FROM archive_jobs WHERE id LIKE 'ctl-%'")


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- paused is never claimed ---------------------------------------------
def test_paused_job_is_never_claimed():
    archive_db.enqueue_job("ctl-paused", "transcribe", "twitch", "ctl-v1")
    assert archive_db.get_job("ctl-paused")["status"] == "queued"
    assert archive_db.pause_job("ctl-paused") is True
    assert archive_db.get_job("ctl-paused")["status"] == "paused"
    assert _claim_next_job() is None, "a paused job must not be claimable"
    # ...and it is still claimable after an explicit resume.
    assert archive_db.resume_job("ctl-paused") is True
    claimed = _claim_next_job()
    assert claimed is not None and claimed["id"] == "ctl-paused"


def test_paused_job_keeps_its_place_in_the_priority_order():
    """A paused job is held, not deprioritised into oblivion: a higher
    priority sibling still outranks it once both are claimable again."""
    archive_db.enqueue_job("ctl-low", "transcribe", "twitch", "ctl-v1", priority=0)
    archive_db.enqueue_job("ctl-high", "transcribe", "twitch", "ctl-v2", priority=200)
    archive_db.pause_job("ctl-low")
    first = _claim_next_job()
    assert first["id"] == "ctl-high"
    assert _claim_next_job() is None
    archive_db.resume_job("ctl-low")
    second = _claim_next_job()
    assert second["id"] == "ctl-low", "priority is preserved across a pause"


def test_pause_is_idempotent_and_resume_requires_paused():
    archive_db.enqueue_job("ctl-idem", "transcribe", "twitch", "ctl-v1")
    assert archive_db.pause_job("ctl-idem") is True
    assert archive_db.pause_job("ctl-idem") is False, "already paused"
    assert archive_db.resume_job("ctl-idem") is True
    assert archive_db.resume_job("ctl-idem") is False, "not paused any more"


def test_resume_clears_a_stale_retry_deadline():
    """A job paused under a backoff must actually run when resumed."""
    archive_db.enqueue_job("ctl-retry", "transcribe", "twitch", "ctl-v1")
    archive_db.execute(
        "UPDATE archive_jobs SET status='paused', next_retry_at='2099-01-01T00:00:00+00:00' "
        "WHERE id='ctl-retry'")
    assert _claim_next_job() is None
    assert archive_db.resume_job("ctl-retry") is True
    claimed = _claim_next_job()
    assert claimed is not None and claimed["id"] == "ctl-retry"


# --- endpoints -----------------------------------------------------------
@pytest.mark.asyncio
async def test_pause_resume_prioritise_cancel_round_trip():
    archive_db.enqueue_job("ctl-api", "transcribe", "twitch", "ctl-v1")
    async with _client() as client:
        r = await client.post("/api/archive/jobs/ctl-api/pause")
        assert r.status_code == 200 and r.json()["status"] == "paused"

        r = await client.post("/api/archive/jobs/ctl-api/priority",
                              json={"tier": "focus"})
        assert r.status_code == 200
        assert archive_db.get_job("ctl-api")["priority"] == 300

        r = await client.post("/api/archive/jobs/ctl-api/resume")
        assert r.status_code == 200
        assert archive_db.get_job("ctl-api")["status"] == "queued"

        r = await client.post("/api/archive/jobs/ctl-api/cancel")
        assert r.status_code == 200
        row = archive_db.get_job("ctl-api")
        assert row["status"] == "failed" and row["error"] == "cancelled by user"
        # A cancelled job must NOT come back on the retry queue.
        assert row["next_retry_at"] is None


@pytest.mark.asyncio
async def test_priority_endpoint_accepts_tier_names_and_raw_ints():
    archive_db.enqueue_job("ctl-tier", "transcribe", "twitch", "ctl-v1")
    async with _client() as client:
        for tier, expected in (("background", 0), ("search", 100),
                               ("preview", 200), ("focus", 300)):
            r = await client.post("/api/archive/jobs/ctl-tier/priority",
                                  json={"tier": tier})
            assert r.status_code == 200, tier
            assert archive_db.get_job("ctl-tier")["priority"] == expected
        r = await client.post("/api/archive/jobs/ctl-tier/priority",
                              json={"priority": 42})
        assert r.status_code == 200
        assert archive_db.get_job("ctl-tier")["priority"] == 42
        r = await client.post("/api/archive/jobs/ctl-tier/priority",
                              json={"tier": "nonsense"})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_running_job_is_never_preempted():
    """The no-preemption invariant, on the new endpoints.

    test_ws1_queue_priority.py pins it for the preview hook; the control
    surface must honour the same rule or a user click could throw away an
    in-flight decode chunk."""
    archive_db.enqueue_job("ctl-running", "transcribe", "twitch", "ctl-v1")
    archive_db.update_job("ctl-running", status="running", progress=0.5)
    async with _client() as client:
        for path in ("pause", "cancel", "priority"):
            r = await client.post(f"/api/archive/jobs/ctl-running/{path}",
                                  json={"tier": "focus"})
            assert r.status_code == 409, path
            assert "never preempted" in r.json()["detail"]
    row = archive_db.get_job("ctl-running")
    assert row["status"] == "running" and row["progress"] == 0.5
    assert row["priority"] == 0


@pytest.mark.asyncio
async def test_control_endpoints_404_on_unknown_job():
    async with _client() as client:
        for path in ("pause", "resume", "cancel"):
            r = await client.post(f"/api/archive/jobs/ctl-missing/{path}")
            assert r.status_code == 404, path


@pytest.mark.asyncio
async def test_enqueue_endpoint_exposes_priority():
    async with _client() as client:
        r = await client.post("/api/archive/jobs", json={
            "id": "ctl-enq", "kind": "transcribe", "platform": "twitch",
            "video_id": "ctl-v1", "priority": 100,
        })
        assert r.status_code == 200
    assert archive_db.get_job("ctl-enq")["priority"] == 100


@pytest.mark.asyncio
async def test_focus_endpoint_stamps_and_releases():
    async with _client() as client:
        r = await client.post("/api/archive/focus",
                              json={"platform": "twitch", "video_id": "ctl-focus"})
        assert r.status_code == 200
        assert r.json()["focus"] == {"platform": "twitch", "video_id": "ctl-focus"}
        from services import queue_policy
        assert queue_policy.active_focus() == ("twitch", "ctl-focus")
        # An empty body is the 'user navigated away' signal.
        r = await client.post("/api/archive/focus", json={})
        assert r.status_code == 200 and r.json()["focus"] is None
        assert queue_policy.active_focus() is None


@pytest.mark.asyncio
async def test_focus_bumps_the_focused_job_priority():
    archive_db.enqueue_job("transcribe-twitch-ctl-hot", "transcribe",
                           "twitch", "ctl-hot")
    async with _client() as client:
        await client.post("/api/archive/focus",
                          json={"platform": "twitch", "video_id": "ctl-hot"})
    assert archive_db.get_job("transcribe-twitch-ctl-hot")["priority"] == 300


# --- schema migration on a legacy DB -------------------------------------
def test_paused_migration_widens_check_and_keeps_every_row(tmp_path):
    """A pre-'paused' table must gain the status WITHOUT losing anything.

    Built by hand to the exact pre-migration shape, including the retry
    columns and a live heartbeat — the two things an earlier rebuild
    migration dropped (NULL AS heartbeat)."""
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """CREATE TABLE archive_jobs (
             id         TEXT PRIMARY KEY,
             kind       TEXT NOT NULL CHECK (kind IN ('ingest','chat','transcribe','events')),
             platform   TEXT NOT NULL,
             video_id   TEXT NOT NULL,
             status     TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','done','failed')),
             progress   REAL NOT NULL DEFAULT 0,
             error      TEXT,
             priority   INTEGER NOT NULL DEFAULT 0,
             created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL,
             heartbeat  TEXT,
             attempts     INTEGER NOT NULL DEFAULT 0,
             max_attempts INTEGER NOT NULL DEFAULT 3,
             next_retry_at TEXT
           );"""
    )
    conn.executemany(
        "INSERT INTO archive_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("ctl-legacy-queued", "transcribe", "twitch", "v1", "queued", 0.0,
             None, 0, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
             None, 0, 3, None),
            ("ctl-legacy-running", "chat", "youtube", "v2", "running", 0.5,
             None, 200, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:05:00+00:00", 2, 5, "2026-01-01T01:00:00+00:00"),
            ("ctl-legacy-done", "transcribe", "kick", "v3", "done", 1.0,
             None, 0, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
             None, 0, 3, None),
        ],
    )
    conn.commit()
    conn.close()

    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(legacy)
    archive_db._conn = None
    archive_db._schema_ready = False
    try:
        archive_db.execute("SELECT 1")  # triggers the migration chain
        rows = {r["id"]: dict(r) for r in archive_db.query("SELECT * FROM archive_jobs")}
        assert set(rows) == {"ctl-legacy-queued", "ctl-legacy-running",
                             "ctl-legacy-done"}
        running = rows["ctl-legacy-running"]
        # Every column survived, INCLUDING the ones the older rebuilds
        # nulled out: a live heartbeat must not be lost across an upgrade.
        assert running["priority"] == 200
        assert running["heartbeat"] == "2026-01-01T00:05:00+00:00"
        assert running["attempts"] == 2 and running["max_attempts"] == 5
        assert running["next_retry_at"] == "2026-01-01T01:00:00+00:00"
        assert running["progress"] == 0.5
        # ...and the status CHECK now accepts 'paused'.
        assert archive_db.pause_job("ctl-legacy-queued") is True
        assert archive_db.get_job("ctl-legacy-queued")["status"] == "paused"
        # Migration is idempotent: re-running is a no-op.
        archive_db._schema_ready = False
        archive_db._conn = None
        archive_db.execute("SELECT 1")
        assert archive_db.query("SELECT id FROM archive_jobs WHERE id='ctl-legacy-done'")
    finally:
        archive_db._conn = None
        archive_db._schema_ready = False
        if prev is None:
            os.environ.pop("VODRIP_ARCHIVE_DB", None)
        else:
            os.environ["VODRIP_ARCHIVE_DB"] = prev


def test_paused_migration_tolerates_a_pre_retry_column_db(tmp_path):
    """A DB predating the retry columns must still migrate.

    The copy is driven by PRAGMA table_info, so absent columns take the new
    table's DEFAULT instead of raising 'no such column'."""
    legacy = tmp_path / "oldest.db"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """CREATE TABLE archive_jobs (
             id         TEXT PRIMARY KEY,
             kind       TEXT NOT NULL CHECK (kind IN ('ingest','chat','transcribe','events')),
             platform   TEXT NOT NULL,
             video_id   TEXT NOT NULL,
             status     TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','done','failed')),
             progress   REAL NOT NULL DEFAULT 0,
             error      TEXT,
             priority   INTEGER NOT NULL DEFAULT 0,
             created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL,
             heartbeat  TEXT
           );
           INSERT INTO archive_jobs VALUES
             ('ctl-oldest-1','transcribe','twitch','v1','queued',0,NULL,0,
              '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',NULL);"""
    )
    conn.commit()
    conn.close()

    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(legacy)
    archive_db._conn = None
    archive_db._schema_ready = False
    try:
        archive_db.execute("SELECT 1")
        row = archive_db.query("SELECT * FROM archive_jobs WHERE id='ctl-oldest-1'")[0]
        assert row["status"] == "queued"
        assert row["attempts"] == 0 and row["max_attempts"] == 3
        assert row["next_retry_at"] is None
    finally:
        archive_db._conn = None
        archive_db._schema_ready = False
        if prev is None:
            os.environ.pop("VODRIP_ARCHIVE_DB", None)
        else:
            os.environ["VODRIP_ARCHIVE_DB"] = prev
