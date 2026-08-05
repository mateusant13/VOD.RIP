"""Ingest-time Twitch chat backfill tests — _kick_ingest_chat_backfills.

No network: routers.archive._kick_backfill is stubbed to record calls, so no
background task is ever spawned and nothing leaves the test process. The
archive DB is a fresh temp file per test (VODRIP_ARCHIVE_DB env isolation).
Covers:
  - only chat-less numeric Twitch items get kicked; with-chat rows, Kick
    rows and synthetic watchdog ids never do
  - the newest-4 burst cap is respected with 6 candidates
  - an exception inside the kick path is swallowed (returns 0)
"""

import os
import pathlib
import tempfile

import pytest

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="ingest-chat-backfill-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")

from services import archive_db  # noqa: E402  (env must be set first)
from routers import archive as archive_router  # noqa: E402
from routers.channels import _kick_ingest_chat_backfills  # noqa: E402


@pytest.fixture
def scratch_db(monkeypatch, tmp_path):
    """Fresh archive DB per test; module connection rebound to tmp path."""
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(tmp_path / "archive.db"))
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    archive_db._conn = None
    archive_db._schema_ready = False


def _item(vid: str, created_at: str, platform: str = "Twitch") -> dict:
    return {"id": vid, "platform": platform, "title": f"vod {vid}", "created_at": created_at}


def _seed_video(vid: str, started_at: str) -> None:
    archive_db.upsert_video({
        "platform": "twitch",
        "video_id": vid,
        "channel": "cellbit",
        "title": f"vod {vid}",
        "started_at": started_at,
        "kind": "vod",
    })


def _seed_chat(vid: str) -> None:
    archive_db.insert_messages("twitch", vid, [
        {"offset_sec": 1.0, "username": "u", "text": "hi",
         "badges": [], "emotes": []},
    ])


def _kick_stub(calls: list):
    def _stub(video_id: str, channel: str) -> str:
        calls.append((video_id, channel))
        return "queued"
    return _stub


def test_only_chatless_numeric_twitch_items_kicked(scratch_db, monkeypatch):
    """With-chat rows, synthetic watchdog ids and other platforms never kick."""
    _seed_video("1001", "2026-08-01T00:00:00Z")          # chat-less -> kick
    _seed_video("1002", "2026-08-01T01:00:00Z")
    _seed_chat("1002")                                   # already has chat
    _seed_video("twitch-live-cellbit-123", "2026-08-01T02:00:00Z")  # watchdog
    calls: list = []
    monkeypatch.setattr(archive_router, "_kick_backfill", _kick_stub(calls))

    items = [
        _item("1001", "2026-08-01T00:00:00Z"),
        _item("1002", "2026-08-01T01:00:00Z"),
        _item("twitch-live-cellbit-123", "2026-08-01T02:00:00Z"),
        _item("k1", "2026-08-01T03:00:00Z", platform="Kick"),
    ]
    queued = _kick_ingest_chat_backfills(items, "cellbit")

    assert queued == 1
    assert calls == [("1001", "cellbit")]


def test_burst_cap_respected_with_six_candidates(scratch_db, monkeypatch):
    """Six chat-less numeric candidates -> only the newest 4 are kicked."""
    for i in range(6):
        _seed_video(f"200{i}", f"2026-08-01T0{i}:00:00Z")
    calls: list = []
    monkeypatch.setattr(archive_router, "_kick_backfill", _kick_stub(calls))

    items = [_item(f"200{i}", f"2026-08-01T0{i}:00:00Z") for i in range(6)]
    queued = _kick_ingest_chat_backfills(items, "cellbit")

    assert queued == 4
    assert [vid for vid, _ in calls] == ["2005", "2004", "2003", "2002"]


def test_kick_exception_is_swallowed(scratch_db, monkeypatch):
    """A failing kick path returns 0 and never propagates."""
    _seed_video("3001", "2026-08-01T00:00:00Z")

    def _boom(video_id: str, channel: str) -> str:
        raise RuntimeError("kick failed")

    monkeypatch.setattr(archive_router, "_kick_backfill", _boom)
    queued = _kick_ingest_chat_backfills(
        [_item("3001", "2026-08-01T00:00:00Z")], "cellbit"
    )

    assert queued == 0
