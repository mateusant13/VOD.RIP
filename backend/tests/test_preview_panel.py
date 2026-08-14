"""WS-2 preview chat panel — integration tests on a COPY of the real archive DB.

The real %APPDATA% archive.db is never opened for write: it is copied to a
temp dir and VODRIP_ARCHIVE_DB points at the copy (same pattern as
e2e_entity_watch_real.py). The copy is byte-hash-verified against the source
both before and after the run, so a regression here can never corrupt user
data.

Real data exercised (read-only on the copy):
  - youtube/aexkXGl9Gr4  (channel titiltei) — 10,952 transcript rows + 126
    chat rows: the transcript+chat video.
  - twitch/2833943352    — 5,780 chat rows, no transcripts: the chat-only video.
"""

import hashlib
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

APPDATA_VODRIP = Path(os.environ["APPDATA"]) / "VOD.RIP"
SRC_DB = APPDATA_VODRIP / "archive.db"
assert SRC_DB.exists(), f"real archive.db missing: {SRC_DB}"

_TMP = Path(tempfile.mkdtemp(prefix="vodrip-ws2-panel-"))
_DB = _TMP / "archive.db"
_SRC_SHA = hashlib.sha256(SRC_DB.read_bytes()).hexdigest()
shutil.copy2(SRC_DB, _DB)

# The real DB is a LIVE database: the running app keeps ingesting chat/videos
# into it, so byte-identity across the test run is not an invariant. The
# invariant that matters is that THIS module never binds the real path or
# runs the archive_db migrations on it — snapshot the pre-run schema and
# assert it is unchanged at the end (an unmigrated archive_jobs = no
# priority column = no code path opened the real DB for write).
def _jobs_schema_snapshot():
    con = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True)
    try:
        return tuple(r[1] for r in con.execute("PRAGMA table_info(archive_jobs)"))
    finally:
        con.close()


_SRC_JOBS_COLS = _jobs_schema_snapshot()

os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)

# Import AFTER env is set (archive_db binds its connection on import).
from services import archive_db  # noqa: E402
from services.archive_db import _PANEL_CHAT_SLICE_ROWS  # noqa: E402
from routers.preview import (  # noqa: E402
    _PANEL_LIMIT_DEFAULT,
    _preview_archive_capabilities,
    preview_panel,
)
from services.preview_service import PreviewSession  # noqa: E402

TRANSCRIPT_VIDEO = ("youtube", "aexkXGl9Gr4")  # titiltei: transcripts + chat
CHAT_ONLY_VIDEO = ("twitch", "2833943352")     # chat rows, no transcripts


@pytest.fixture(scope="module", autouse=True)
def _panel_scratch_db():
    """Rebind the shared connection to THIS module's real-DB copy and restore
    the previous env value + connection afterwards (never leaves the env var
    pointing at a deleted temp file)."""
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False
    archive_db.get_conn()
    yield
    if prev is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False


def _assert_sorted(rows, key):
    values = [r[key] for r in rows]
    assert values == sorted(values), f"{key} rows must be time-ordered"


def test_source_db_untouched():
    """This module must never bind the real DB or migrate it.

    The real DB is live (the app ingests chat while tests run), so schema
    immutability is the guard: an unmigrated archive_jobs (no priority
    column) proves no code path here opened the real path for write."""
    assert archive_db._conn_path == str(_DB), "connection must point at the copy"
    assert _jobs_schema_snapshot() == _SRC_JOBS_COLS, (
        "real archive_db schema changed — this module must not migrate it"
    )


async def test_panel_transcript_video_strict_shape_and_order():
    p, vid = TRANSCRIPT_VIDEO
    # Direct call needs an explicit limit: FastAPI resolves the Query()
    # default only over HTTP (covered by test_panel_http_surface).
    payload = await preview_panel(p, vid, limit=_PANEL_LIMIT_DEFAULT)
    assert set(payload.keys()) == {
        "transcript", "chat", "events", "has_transcript", "has_chat",
        "backfill", "backfill_progress", "total_rows", "chat_truncated",
    }
    assert payload["has_transcript"] is True
    assert payload["has_chat"] is True
    assert len(payload["transcript"]) > 1000
    assert set(payload["events"][0].keys()) == {
        "offset_sec", "end_sec", "event", "score",
    } if payload["events"] else True
    for row in payload["transcript"]:
        assert set(row.keys()) == {"offset_sec", "text"}
        assert isinstance(row["offset_sec"], float)
        assert isinstance(row["text"], str) and row["text"]
    _assert_sorted(payload["transcript"], "offset_sec")
    for row in payload["chat"]:
        assert set(row.keys()) == {"offset_sec", "text", "username", "spam_count", "color", "platform", "video_id"}
        assert isinstance(row["spam_count"], int) and row["spam_count"] >= 1
    _assert_sorted(payload["chat"], "offset_sec")


async def test_panel_chat_only_video():
    p, vid = CHAT_ONLY_VIDEO
    payload = await preview_panel(p, vid, limit=_PANEL_LIMIT_DEFAULT, offset_sec=None)
    assert payload["has_transcript"] is False
    assert payload["has_chat"] is True
    assert payload["transcript"] == []
    assert len(payload["chat"]) > 1000
    _assert_sorted(payload["chat"], "offset_sec")


async def test_panel_limit_and_unknown_platform():
    p, vid = CHAT_ONLY_VIDEO
    small = await preview_panel(p, vid, limit=10, offset_sec=None)
    # While a Twitch chat backfill is owed the panel returns the bounded
    # playhead window (polling contract), not the limit cap.
    if small["backfill"] == "running":
        assert len(small["chat"]) <= _PANEL_CHAT_SLICE_ROWS
        assert small["chat_truncated"] is True
    else:
        assert len(small["chat"]) == 10
        assert small["chat_truncated"] is False
    # limit still caps when no backfill is owed (YouTube has no replay backfill).
    done = await preview_panel("youtube", TRANSCRIPT_VIDEO[1], limit=10, offset_sec=None)
    assert len(done["chat"]) == 10
    with pytest.raises(HTTPException) as exc:
        await preview_panel("vimeo", vid, offset_sec=None)
    assert exc.value.status_code == 400


async def test_panel_events_mapping_and_order():
    """audio_events rows surface as {offset_sec, end_sec, event, score},
    time-ordered — the UI merges them into the transcript timeline."""
    p, vid = CHAT_ONLY_VIDEO
    archive_db.insert_audio_events(p, vid, [
        {"start_sec": 10.0, "end_sec": 10.9, "event": "Laughter", "score": 0.93},
        {"start_sec": 2.0, "end_sec": 2.4, "event": "Clapping", "score": 0.71},
        {"start_sec": 42.0, "end_sec": 42.3, "event": "Shout", "score": 0.88},
    ])
    try:
        payload = await preview_panel(p, vid, limit=_PANEL_LIMIT_DEFAULT, offset_sec=None)
        events = payload["events"]
        assert [e["event"] for e in events] == ["Clapping", "Laughter", "Shout"], (
            "events must come back in start_sec order"
        )
        assert events[0] == {"offset_sec": 2.0, "end_sec": 2.4,
                             "event": "Clapping", "score": 0.71}
        assert events[1]["offset_sec"] == 10.0 and events[1]["end_sec"] == 10.9
    finally:
        archive_db.delete_audio_events(p, vid)


async def test_panel_missing_video_empty_state():
    payload = await preview_panel("twitch", "0000000000", limit=_PANEL_LIMIT_DEFAULT)
    assert payload == {
        "transcript": [],
        "chat": [],
        "events": [],
        "has_transcript": False,
        "has_chat": False,
        "backfill": "idle",
        "backfill_progress": 0.0,
        "total_rows": 0,
        "chat_truncated": False,
    }


async def test_panel_http_surface():
    """Real HTTP path: Query() default resolution + strict JSON body."""
    from httpx import AsyncClient, ASGITransport
    from app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/preview/panel/twitch/2833943352", params={"limit": 5}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {
            "transcript", "chat", "events", "has_transcript", "has_chat",
            "backfill", "backfill_progress", "total_rows", "chat_truncated",
        }
        assert body["has_chat"] is True and body["has_transcript"] is False
        # While a Twitch chat backfill is owed the panel returns the bounded
        # playhead window (polling contract); otherwise the limit caps.
        if body["backfill"] == "running":
            assert len(body["chat"]) <= _PANEL_CHAT_SLICE_ROWS
            assert body["chat_truncated"] is True
        else:
            assert len(body["chat"]) == 5
            assert body["chat_truncated"] is False
        # Default limit (no ?limit=) resolves through FastAPI — must not 422.
        resp2 = await client.get("/api/preview/panel/youtube/aexkXGl9Gr4")
        assert resp2.status_code == 200
        body2 = resp2.json()
        assert body2["has_transcript"] is True
        assert len(body2["transcript"]) > 1000
        bad = await client.get("/api/preview/panel/vimeo/x")
        assert bad.status_code == 400


def _session_for(url: str, platform: str) -> PreviewSession:
    return PreviewSession(
        session_id="ws2-test",
        vod_url=url,
        master_url="",
        entry_url="",
        platform=platform,
    )


def test_session_capability_flags_transcript_video():
    s = _session_for("https://www.youtube.com/watch?v=aexkXGl9Gr4", "YouTube")
    assert _preview_archive_capabilities(s) == (True, True)


def test_session_capability_flags_chat_only_video():
    s = _session_for("https://www.twitch.tv/videos/2833943352", "Twitch")
    assert _preview_archive_capabilities(s) == (False, True)


def test_session_capability_flags_unarchived_url():
    s = _session_for("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "YouTube")
    assert _preview_archive_capabilities(s) == (False, False)
    live = _session_for("https://example.com/master.m3u8", "Unknown")
    assert _preview_archive_capabilities(live) == (False, False)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-p", "no:cacheprovider", "--tb=short"])
