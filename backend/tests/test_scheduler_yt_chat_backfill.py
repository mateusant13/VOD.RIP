"""Scheduler YouTube retro chat-backfill leg tests — _backfill_youtube_chat.

No network: the leg is now a pure PRODUCER — it enqueues kind='chat' jobs
and the archive worker does the fetching (services.archive_ytdlp.
backfill_live_chat is never called here). The archive DB is a fresh temp
file per test (VODRIP_ARCHIVE_DB env isolation).

Covers:
  - chat-less kind='stream' YouTube videos (non-live ids) get a 'chat' job
  - videos that already have chat rows are skipped
  - synthetic live-capture ids (youtube-live-*) are never backfilled
  - a 'done' chat job retires the video
  - a fresh 'failed' job is skipped (< FAILED_JOB_FRESH_S), a stale one retried
"""

import os
import pathlib
import tempfile

import pytest

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="scheduler-yt-chat-backfill-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")

from services import archive_db  # noqa: E402  (env must be set first)
from services import archive_scheduler  # noqa: E402


@pytest.fixture
def scratch_db(monkeypatch, tmp_path):
    """Fresh archive DB per test; module connection rebound to tmp path."""
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(tmp_path / "archive.db"))
    archive_db._conn = None
    archive_db._schema_ready = False
    archive_db._schema_ready = False
    yield
    archive_db._conn = None
    archive_db._schema_ready = False


def _seed_stream(vid: str, *, chat: bool = False) -> None:
    archive_db.upsert_video({
        "platform": "youtube",
        "video_id": vid,
        "channel": "titiltei",
        "title": f"stream {vid}",
        "started_at": "2026-08-01T00:00:00Z",
        "kind": "stream",
    })
    if chat:
        archive_db.insert_messages("youtube", vid, [
            {"offset_sec": 1.0, "username": "u", "text": "hi",
             "badges": [], "emotes": []},
        ])


def _queued_chat_video_ids() -> list:
    return sorted(
        j["video_id"]
        for j in archive_db.list_jobs(limit=500)
        if j["kind"] == "chat" and j["status"] == "queued"
    )


def test_chatless_streams_backfilled(scratch_db):
    _seed_stream("aaaaaaaaaaa", chat=False)
    _seed_stream("bbbbbbbbbbb", chat=True)   # has chat -> skip
    _seed_stream("youtube-live-1785955673345", chat=False)  # watchdog -> skip
    archive_scheduler._backfill_youtube_chat()
    assert _queued_chat_video_ids() == ["aaaaaaaaaaa"]


def test_done_job_retires_video(scratch_db):
    _seed_stream("aaaaaaaaaaa", chat=False)
    archive_db.enqueue_job("chat-youtube-aaaaaaaaaaa", "chat",
                           "youtube", "aaaaaaaaaaa", priority=0)
    archive_db.update_job("chat-youtube-aaaaaaaaaaa", status="done")
    archive_scheduler._backfill_youtube_chat()
    assert _queued_chat_video_ids() == []
    assert archive_db.latest_job("youtube", "aaaaaaaaaaa", kind="chat")["status"] == "done"


def test_failed_job_fresh_skipped_stale_retried(scratch_db):
    _seed_stream("aaaaaaaaaaa", chat=False)
    _seed_stream("bbbbbbbbbbb", chat=False)
    _seed_stream("ccccccccccc", chat=False)
    fresh_id = "chat-youtube-aaaaaaaaaaa"
    archive_db.enqueue_job(fresh_id, "chat", "youtube", "aaaaaaaaaaa", priority=0)
    archive_db.update_job(fresh_id, status="failed",
                          error="FileNotFound: archive evicted")
    archive_db.execute(
        """UPDATE archive_jobs SET updated_at = ? WHERE id = ?""",
        ("2020-01-01T00:00:00Z", fresh_id),  # stale -> retried
    )
    stale_id = "chat-youtube-bbbbbbbbbbb"
    archive_db.enqueue_job(stale_id, "chat", "youtube", "bbbbbbbbbbb", priority=0)
    archive_db.update_job(stale_id, status="failed",
                          error="FileNotFound: archive evicted")
    archive_scheduler._backfill_youtube_chat()
    # aaaaaaaaaaa: stale failed job -> re-queued; bbbbbbbbbbb: fresh failed ->
    # skipped (still recent); ccccccccccc: no job -> queued.
    assert _queued_chat_video_ids() == ["aaaaaaaaaaa", "ccccccccccc"]


def test_kind_vod_never_backfilled(scratch_db):
    archive_db.upsert_video({
        "platform": "youtube",
        "video_id": "ddddddddddd",
        "channel": "titiltei",
        "title": "vod d",
        "started_at": "2026-08-01T00:00:00Z",
        "kind": "vod",  # plain upload — legitimately has no chat
    })
    archive_scheduler._backfill_youtube_chat()
    assert _queued_chat_video_ids() == []


def test_enqueue_chat_job_dedupes_across_kinds(scratch_db):
    """The shared producer guard: has_chat / queued / running / done all
    suppress a new job; a stale failed job is re-enqueued; a live 'chat'
    job with a synthetic id never double-queues."""
    _seed_stream("aaaaaaaaaaa", chat=False)
    archive_db.enqueue_job("legacy-backfill-id", "chat", "youtube",
                           "aaaaaaaaaaa", priority=0)
    archive_db.update_job("legacy-backfill-id", status="running")
    assert archive_scheduler._enqueue_chat_job("youtube", "aaaaaaaaaaa") is False
    archive_db.update_job("legacy-backfill-id", status="failed",
                          error="FileNotFound: archive evicted")
    archive_db.execute(
        """UPDATE archive_jobs SET updated_at = ? WHERE id = ?""",
        ("2020-01-01T00:00:00Z", "legacy-backfill-id"),
    )
    assert archive_scheduler._enqueue_chat_job("youtube", "aaaaaaaaaaa") is True
    # idempotent under a second call (stable PK)
    assert archive_scheduler._enqueue_chat_job("youtube", "aaaaaaaaaaa") is False
