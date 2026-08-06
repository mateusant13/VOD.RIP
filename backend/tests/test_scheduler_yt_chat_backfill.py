"""Scheduler YouTube retro chat-backfill leg tests — _backfill_youtube_chat.

No network: services.archive_ytdlp.backfill_live_chat is never called —
the thread target _backfill_one_youtube is stubbed to record video_ids, so
no background thread fetches anything. The archive DB is a fresh temp file
per test (VODRIP_ARCHIVE_DB env isolation).

Covers:
  - chat-less kind='stream' YouTube videos (non-live ids) get backfilled
  - videos that already have chat rows are skipped
  - synthetic live-capture ids (youtube-live-*) are never backfilled
  - a 'done' chat_backfill job retires the video
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
    archive_scheduler._backfill_inflight.clear()
    yield
    archive_db._conn = None
    archive_db._schema_ready = False
    archive_scheduler._backfill_inflight.clear()


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


def test_chatless_streams_backfilled(scratch_db, monkeypatch):
    _seed_stream("aaaaaaaaaaa", chat=False)
    _seed_stream("bbbbbbbbbbb", chat=True)   # has chat -> skip
    _seed_stream("youtube-live-1785955673345", chat=False)  # watchdog -> skip
    kicked: list = []
    monkeypatch.setattr(archive_scheduler, "_backfill_one_youtube",
                        lambda vid: kicked.append(vid))
    archive_scheduler._backfill_youtube_chat()
    assert kicked == ["aaaaaaaaaaa"]


def test_done_job_retires_video(scratch_db, monkeypatch):
    _seed_stream("aaaaaaaaaaa", chat=False)
    archive_db.enqueue_job("yt-chat-backfill-aaaaaaaaaaa-1", "chat_backfill",
                           "youtube", "aaaaaaaaaaa", priority=0)
    archive_db.update_job("yt-chat-backfill-aaaaaaaaaaa-1", status="done")
    kicked: list = []
    monkeypatch.setattr(archive_scheduler, "_backfill_one_youtube",
                        lambda vid: kicked.append(vid))
    archive_scheduler._backfill_youtube_chat()
    assert kicked == []


def test_failed_job_fresh_skipped_stale_retried(scratch_db, monkeypatch):
    _seed_stream("aaaaaaaaaaa", chat=False)
    _seed_stream("bbbbbbbbbbb", chat=False)
    _seed_stream("ccccccccccc", chat=False)
    fresh_id = "yt-chat-backfill-aaaaaaaaaaa-1"
    archive_db.enqueue_job(fresh_id, "chat_backfill", "youtube", "aaaaaaaaaaa", priority=0)
    archive_db.update_job(fresh_id, status="failed", error="extract error")
    archive_db.execute(
        """UPDATE archive_jobs SET updated_at = ? WHERE id = ?""",
        ("2020-01-01T00:00:00Z", fresh_id),  # stale -> retried
    )
    stale_id = "yt-chat-backfill-bbbbbbbbbbb-1"
    archive_db.enqueue_job(stale_id, "chat_backfill", "youtube", "bbbbbbbbbbb", priority=0)
    archive_db.update_job(stale_id, status="failed", error="extract error")
    kicked: list = []
    monkeypatch.setattr(archive_scheduler, "_backfill_one_youtube",
                        lambda vid: kicked.append(vid))
    archive_scheduler._backfill_youtube_chat()
    # aaaaaaaaaaa: stale failed job -> retried; bbbbbbbbbbb: fresh failed ->
    # skipped (still recent); ccccccccccc: no job -> backfilled.
    assert kicked == ["aaaaaaaaaaa", "ccccccccccc"]


def test_kind_vod_never_backfilled(scratch_db, monkeypatch):
    archive_db.upsert_video({
        "platform": "youtube",
        "video_id": "ddddddddddd",
        "channel": "titiltei",
        "title": "vod d",
        "started_at": "2026-08-01T00:00:00Z",
        "kind": "vod",  # plain upload — legitimately has no chat
    })
    kicked: list = []
    monkeypatch.setattr(archive_scheduler, "_backfill_one_youtube",
                        lambda vid: kicked.append(vid))
    archive_scheduler._backfill_youtube_chat()
    assert kicked == []
