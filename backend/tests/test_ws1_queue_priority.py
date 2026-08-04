"""WS-1 preview-queue priority — archive_jobs.priority.

Covers:
  * rebuild migration: fresh AND legacy DBs both end with
    ``priority INTEGER NOT NULL DEFAULT 0`` (PRAGMA table_info assert) and
    the (status, priority, created_at) index;
  * enqueue_job(priority=...) threading + PK dedupe still preventing double jobs;
  * worker pick order: priority DESC, created_at ASC (FIFO within priority);
  * preview hook: transcript-less archived video -> priority-1 transcribe job;
    existing queued job bumped; running job untouched; not-archived /
    disabled / already-transcribed guards.

Env note: VODRIP_ARCHIVE_DB must be set before the first services.archive_db
import (the module binds its connection at import), and the previous env
value must be RESTORED in teardown (pop-with-default breaks later modules).

Run from backend/:  python -m pytest tests/test_ws1_queue_priority.py -q
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["VODRIP_ARCHIVE_DB"] = str(
    Path(tempfile.mkdtemp(prefix="ws1-queue-")) / "archive.db")

import pytest  # noqa: E402

from services import archive_db  # noqa: E402  (env must be set first)
from services.archive_transcribe import _claim_next_job  # noqa: E402
from routers.preview import (  # noqa: E402
    _preview_video_id,
    _priority_transcribe_for_preview,
)


@pytest.fixture(scope="module", autouse=True)
def _ws1_scratch_db():
    """Rebind the shared archive conn to THIS module's scratch DB at module
    start (collection-order independent) and restore the env + unbind after,
    so the next module rebinds fresh."""
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(
        Path(tempfile.mkdtemp(prefix="ws1-queue-")) / "archive.db")
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    if prev is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev
    archive_db._conn = None
    archive_db._schema_ready = False


class _FakeSession:
    """Minimal stand-in for a PreviewSession (platform + vod_url only)."""

    def __init__(self, platform: str, vod_url: str):
        self.platform = platform
        self.vod_url = vod_url


def _upsert(platform: str, video_id: str, **kw) -> None:
    archive_db.upsert_video({
        "platform": platform,
        "video_id": video_id,
        "channel": kw.get("channel", "chan"),
        "title": kw.get("title", "title"),
        "status": kw.get("status", "ready"),
        "archive_path": kw.get("archive_path", "C:/__ws1__/no-file.mp4"),
    })


def _insert_job(job_id: str, created_at: str, *, priority: int = 0,
                status: str = "queued") -> None:
    """Insert a job with an explicit created_at (FIFO determinism — the
    enqueue helper stamps second-resolution timestamps, which tie within a
    second)."""
    archive_db.execute(
        "INSERT INTO archive_jobs (id, kind, platform, video_id, status, progress, error, priority, created_at, updated_at) "
        "VALUES (?, 'transcribe', 'youtube', 'pick-vid', ?, 0, NULL, ?, ?, ?)",
        (job_id, status, priority, created_at, created_at),
    )


def _priority_of(job_id: str) -> int:
    return archive_db.query(
        "SELECT priority FROM archive_jobs WHERE id = ?", (job_id,)
    )[0]["priority"]


# --- 1. migration: fresh + legacy DBs both get the column -------------------


def test_fresh_db_has_priority_column_and_index():
    rows = archive_db.query("PRAGMA table_info(archive_jobs)")
    cols = {r["name"]: r for r in rows}
    assert "priority" in cols, "fresh SCHEMA must carry archive_jobs.priority"
    assert cols["priority"]["notnull"] == 1 and cols["priority"]["dflt_value"] == "0", (
        "priority must be INTEGER NOT NULL DEFAULT 0"
    )
    idx = archive_db.query(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_jobs_status_priority'"
    )
    assert idx and "status, priority, created_at" in idx[0]["sql"], (
        "fresh SCHEMA must create the (status, priority, created_at) index"
    )


def test_legacy_db_migration_adds_priority():
    """A pre-priority archive_jobs (current kind CHECK, no priority column)
    converges to the new shape with legacy rows defaulting to 0."""
    conn = archive_db.get_conn()
    with conn:
        conn.execute("DROP TABLE archive_jobs")
        conn.execute(
            """CREATE TABLE archive_jobs (
                 id         TEXT PRIMARY KEY,
                 kind       TEXT NOT NULL CHECK (kind IN ('ingest','chat_backfill','transcribe','events')),
                 platform   TEXT NOT NULL,
                 video_id   TEXT NOT NULL,
                 status     TEXT NOT NULL DEFAULT 'queued'
                            CHECK (status IN ('queued','running','done','failed')),
                 progress   REAL NOT NULL DEFAULT 0,
                 error      TEXT,
                 created_at TEXT NOT NULL,
                 updated_at TEXT NOT NULL
               )"""
        )
        conn.execute(
            "INSERT INTO archive_jobs (id, kind, platform, video_id, status, progress, created_at, updated_at) "
            "VALUES ('legacy-1', 'transcribe', 'youtube', 'legacy-vid', 'queued', 0, "
            "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')"
        )
    archive_db._ensure_jobs_priority(conn)
    conn.commit()

    rows = archive_db.query("PRAGMA table_info(archive_jobs)")
    cols = {r["name"]: r for r in rows}
    assert "priority" in cols, "migration must add archive_jobs.priority"
    assert cols["priority"]["notnull"] == 1 and cols["priority"]["dflt_value"] == "0"
    legacy = archive_db.query("SELECT * FROM archive_jobs WHERE id = 'legacy-1'")[0]
    assert legacy["priority"] == 0 and legacy["video_id"] == "legacy-vid", (
        "legacy rows must survive the rebuild with priority=0"
    )
    idx = archive_db.query(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_jobs_status_priority'"
    )
    assert idx and "status, priority, created_at" in idx[0]["sql"]

    # post-migration enqueues default to 0 (column NOT NULL DEFAULT applies)
    archive_db.enqueue_job("legacy-2", "transcribe", "youtube", "legacy-vid2")
    assert _priority_of("legacy-2") == 0

    # idempotent: a second run is a no-op
    archive_db._ensure_jobs_priority(conn)
    conn.commit()
    assert "priority" in {
        r["name"] for r in archive_db.query("PRAGMA table_info(archive_jobs)")
    }


def test_ancient_legacy_db_survives_both_rebuilds():
    """A pre-events, pre-priority table (kind CHECK without 'events') goes
    through _ensure_jobs_kind_events THEN _ensure_jobs_priority — the exact
    init-chain order on the oldest DBs — and converges in two rebuilds."""
    conn = archive_db.get_conn()
    with conn:
        conn.execute("DROP TABLE archive_jobs")
        conn.execute(
            """CREATE TABLE archive_jobs (
                 id         TEXT PRIMARY KEY,
                 kind       TEXT NOT NULL CHECK (kind IN ('ingest','chat_backfill','transcribe')),
                 platform   TEXT NOT NULL,
                 video_id   TEXT NOT NULL,
                 status     TEXT NOT NULL DEFAULT 'queued'
                            CHECK (status IN ('queued','running','done','failed')),
                 progress   REAL NOT NULL DEFAULT 0,
                 error      TEXT,
                 created_at TEXT NOT NULL,
                 updated_at TEXT NOT NULL
               )"""
        )
        conn.execute(
            "INSERT INTO archive_jobs (id, kind, platform, video_id, status, progress, created_at, updated_at) "
            "VALUES ('ancient-1', 'transcribe', 'youtube', 'ancient-vid', 'queued', 0, "
            "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')"
        )
    archive_db._ensure_jobs_kind_events(conn)
    archive_db._ensure_jobs_priority(conn)
    conn.commit()

    rows = archive_db.query("PRAGMA table_info(archive_jobs)")
    cols = {r["name"]: r for r in rows}
    assert "priority" in cols and cols["priority"]["dflt_value"] == "0"
    legacy = archive_db.query("SELECT * FROM archive_jobs WHERE id = 'ancient-1'")[0]
    assert legacy["priority"] == 0 and legacy["status"] == "queued"
    # the widened kind CHECK survived too: 'events' rows are accepted
    archive_db.enqueue_job("ancient-events", "events", "youtube", "ancient-vid")
    assert _priority_of("ancient-events") == 0


# --- 2. enqueue_job priority threading + dedupe ------------------------------


def test_enqueue_job_threads_priority_and_still_dedupes():
    archive_db.execute("DELETE FROM archive_jobs WHERE id IN ('prio-1','prio-0')")
    archive_db.enqueue_job("prio-1", "transcribe", "youtube", "vid-a", priority=1)
    archive_db.enqueue_job("prio-0", "transcribe", "youtube", "vid-b")
    assert _priority_of("prio-1") == 1, "explicit priority must be stored"
    assert _priority_of("prio-0") == 0, "default priority must be 0"
    with pytest.raises(sqlite3.IntegrityError):
        archive_db.enqueue_job("prio-1", "transcribe", "youtube", "vid-a", priority=1)
    archive_db.execute("DELETE FROM archive_jobs WHERE id IN ('prio-1','prio-0')")


# --- 3. worker pick order: priority DESC, FIFO within priority --------------


def test_worker_pick_orders_priority_then_fifo():
    archive_db.execute("DELETE FROM archive_jobs")
    _insert_job("normal-1", "2026-08-01T00:00:01+00:00", priority=0)
    _insert_job("normal-2", "2026-08-01T00:00:02+00:00", priority=0)
    # high-priority job enqueued FIRST — priority must still win over age
    _insert_job("high-1", "2026-08-01T00:00:00+00:00", priority=1)

    first = _claim_next_job()
    assert first is not None and first["id"] == "high-1", (
        f"the high-priority job must be picked first, got {first and first['id']}"
    )
    second = _claim_next_job()
    assert second is not None and second["id"] == "normal-1", (
        f"FIFO within priority 0 must pick the oldest job, got {second and second['id']}"
    )
    third = _claim_next_job()
    assert third is not None and third["id"] == "normal-2", (
        f"FIFO within priority 0 must pick the next-oldest job, got {third and third['id']}"
    )
    assert _claim_next_job() is None, "queue must be drained"
    archive_db.execute("DELETE FROM archive_jobs")


# --- 4. preview hook ---------------------------------------------------------

_YT1 = "pvyoutube11"     # 11-char YouTube id
_YT2 = "pvyoutube22"
_YT3 = "pvyoutube33"
_YT4 = "pvyoutube44"
_YT5 = "pvyoutube55"
_TT1 = "12345678901"     # Twitch numeric VOD id


def test_preview_video_id_extraction():
    assert _preview_video_id("YouTube", f"https://www.youtube.com/watch?v={_YT1}") == _YT1
    assert _preview_video_id("youtube", f"https://youtu.be/{_YT1}") == _YT1
    assert _preview_video_id("Twitch", f"https://www.twitch.tv/videos/{_TT1}") == _TT1
    assert _preview_video_id("Kick", "https://kick.com/chan/videos/1f2e3d4c-5b6a-7c8d-9e0f-1a2b3c4d5e6f") == (
        "1f2e3d4c-5b6a-7c8d-9e0f-1a2b3c4d5e6f"
    )
    assert _preview_video_id("Unknown", "https://example.com/x") is None
    assert _preview_video_id("Twitch", "https://www.twitch.tv/videos/not-digits") is None


def test_preview_hook_enqueues_priority_one_for_transcript_less():
    for vid in (_YT1, _TT1):
        archive_db.execute("DELETE FROM videos WHERE video_id=?", (vid,))
        archive_db.execute("DELETE FROM transcripts WHERE video_id=?", (vid,))
        archive_db.execute("DELETE FROM archive_jobs WHERE video_id=?", (vid,))
    _upsert("youtube", _YT1)
    _upsert("twitch", _TT1)
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(archive_smart_enrich=True)
        _priority_transcribe_for_preview(
            _FakeSession("YouTube", f"https://www.youtube.com/watch?v={_YT1}"))
        _priority_transcribe_for_preview(
            _FakeSession("Twitch", f"https://www.twitch.tv/videos/{_TT1}"))
    job = archive_db.query(
        "SELECT * FROM archive_jobs WHERE id = ?", (f"transcribe-youtube-{_YT1}",)
    )
    assert job and job[0]["status"] == "queued" and job[0]["priority"] == 1, (
        f"transcript-less previewed video must get a priority-1 job: {job}"
    )
    job2 = archive_db.query(
        "SELECT * FROM archive_jobs WHERE id = ?", (f"transcribe-twitch-{_TT1}",)
    )
    assert job2 and job2[0]["priority"] == 1, (
        f"twitch previews must enqueue priority-1 transcribe jobs: {job2}"
    )
    # a second preview must not double-enqueue (PK dedupe)
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(archive_smart_enrich=True)
        _priority_transcribe_for_preview(
            _FakeSession("YouTube", f"https://www.youtube.com/watch?v={_YT1}"))
    rows = archive_db.query(
        "SELECT * FROM archive_jobs WHERE platform='youtube' AND video_id=?",
        (_YT1,),
    )
    assert len(rows) == 1, "has_job dedupe must prevent double jobs"


def test_preview_hook_bumps_queued_job():
    archive_db.execute("DELETE FROM videos WHERE video_id=?", (_YT2,))
    archive_db.execute("DELETE FROM transcripts WHERE video_id=?", (_YT2,))
    archive_db.execute("DELETE FROM archive_jobs WHERE video_id=?", (_YT2,))
    _upsert("youtube", _YT2)
    archive_db.enqueue_job(f"transcribe-youtube-{_YT2}", "transcribe", "youtube", _YT2)  # priority 0
    assert _priority_of(f"transcribe-youtube-{_YT2}") == 0
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(archive_smart_enrich=True)
        _priority_transcribe_for_preview(
            _FakeSession("YouTube", f"https://www.youtube.com/watch?v={_YT2}"))
    job = archive_db.query(
        "SELECT * FROM archive_jobs WHERE id = ?", (f"transcribe-youtube-{_YT2}",)
    )[0]
    assert job["status"] == "queued" and job["priority"] == 1, (
        "an existing queued job must be bumped to priority 1"
    )


def test_preview_hook_never_touches_running_job():
    archive_db.execute("DELETE FROM videos WHERE video_id=?", (_YT3,))
    archive_db.execute("DELETE FROM transcripts WHERE video_id=?", (_YT3,))
    archive_db.execute("DELETE FROM archive_jobs WHERE video_id=?", (_YT3,))
    _upsert("youtube", _YT3)
    archive_db.enqueue_job(f"transcribe-youtube-{_YT3}", "transcribe", "youtube", _YT3)
    archive_db.update_job(f"transcribe-youtube-{_YT3}", status="running")
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(archive_smart_enrich=True)
        _priority_transcribe_for_preview(
            _FakeSession("YouTube", f"https://www.youtube.com/watch?v={_YT3}"))
    job = archive_db.query(
        "SELECT * FROM archive_jobs WHERE id = ?", (f"transcribe-youtube-{_YT3}",)
    )[0]
    assert job["status"] == "running" and job["priority"] == 0, (
        "a running job must never be touched or re-enqueued"
    )
    rows = archive_db.query(
        "SELECT * FROM archive_jobs WHERE platform='youtube' AND video_id=?", (_YT3,)
    )
    assert len(rows) == 1, "a running job must not spawn a duplicate row"


def test_preview_hook_guards():
    # not archived -> nothing enqueued
    archive_db.execute("DELETE FROM archive_jobs WHERE video_id=?", (_YT4,))
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(archive_smart_enrich=True)
        _priority_transcribe_for_preview(
            _FakeSession("YouTube", f"https://www.youtube.com/watch?v={_YT4}"))
    assert archive_db.query(
        "SELECT 1 FROM archive_jobs WHERE video_id=?", (_YT4,)
    ) == [], "a video not in the archive DB must not get a job"

    # already transcribed -> nothing enqueued
    archive_db.execute("DELETE FROM videos WHERE video_id=?", (_YT4,))
    archive_db.execute("DELETE FROM transcripts WHERE video_id=?", (_YT4,))
    _upsert("youtube", _YT4)
    archive_db.insert_transcript(
        "youtube", _YT4,
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "hello"}],
    )
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(archive_smart_enrich=True)
        _priority_transcribe_for_preview(
            _FakeSession("YouTube", f"https://www.youtube.com/watch?v={_YT4}"))
    assert archive_db.query(
        "SELECT 1 FROM archive_jobs WHERE video_id=?", (_YT4,)
    ) == [], "a transcribed video must not get a job"

    # transcription disabled (archive_smart_enrich off) -> nothing enqueued
    archive_db.execute("DELETE FROM videos WHERE video_id=?", (_YT5,))
    archive_db.execute("DELETE FROM transcripts WHERE video_id=?", (_YT5,))
    _upsert("youtube", _YT5)
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(archive_smart_enrich=False)
        _priority_transcribe_for_preview(
            _FakeSession("YouTube", f"https://www.youtube.com/watch?v={_YT5}"))
    assert archive_db.query(
        "SELECT 1 FROM archive_jobs WHERE video_id=?", (_YT5,)
    ) == [], "the hook must respect the transcription toggle"
