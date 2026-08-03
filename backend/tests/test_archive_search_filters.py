"""Search filter + kind migration tests against a scratch archive DB.

The env var MUST be set before the first services.archive_db import anywhere
in the pytest session; this module is the only importer, so it is set at
module top (before `from services import archive_db`), binding the global
connection to the temp DB.

The DB is pre-created with the LEGACY videos schema (no kind column) and one
legacy row, so the idempotent ALTER TABLE migration is exercised for real on
first connect — exactly the upgrade path of an existing user DB.

Run from backend/: python -m pytest tests/test_archive_search_filters.py
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="archive-search-filters-"))
_DB = _TMP / "archive.db"

# Legacy schema: exactly what pre-kind archive.db files look like.
_legacy = sqlite3.connect(str(_DB))
_legacy.executescript("""
CREATE TABLE videos (
  platform      TEXT NOT NULL,
  video_id      TEXT NOT NULL,
  channel       TEXT NOT NULL,
  title         TEXT NOT NULL,
  started_at    TEXT,
  ended_at      TEXT,
  duration_sec  REAL,
  archive_path  TEXT,
  canonical_key TEXT,
  status        TEXT NOT NULL DEFAULT 'known',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (platform, video_id)
);
""")
_legacy.execute(
    """INSERT INTO videos (platform, video_id, channel, title, started_at,
       created_at, updated_at)
       VALUES ('twitch', 'legacy-vid', 'legacychan', 'legacy', '2026-07-30T10:00:00Z',
       '2026-07-30T10:00:00Z', '2026-07-30T10:00:00Z')"""
)
_legacy.commit()
_legacy.close()

os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)

from services import archive_db  # noqa: E402  (env must be set first)

# Belt-and-braces for merged runs: an earlier test module (alphabetically
# first, e.g. test_api_integration.py via `from app import app`) may already
# have imported services.archive_db and bound the shared connection to the
# conftest scratch DB. Rebind the global connection to THIS module's legacy
# scratch DB so the migration + legacy-vid tests stay honest regardless of
# import order; conftest.py already guarantees the real %APPDATA% archive.db
# is never the target.
with archive_db._lock:
    archive_db._conn = None
    archive_db._schema_ready = False
archive_db.get_conn()  # rebind now: legacy ALTER TABLE migration runs here

VIDEO = "filter-video-1"


def _insert_video(
    video_id: str,
    *,
    platform: str = "twitch",
    channel: str = "lubu",
    started_at: str = "2026-07-30T12:00:00Z",
    kind: str = "vod",
) -> None:
    archive_db.upsert_video({
        "platform": platform,
        "video_id": video_id,
        "channel": channel,
        "title": f"title {video_id}",
        "started_at": started_at,
        "kind": kind,
    })


def _insert_msg(video_id: str, text: str, platform: str = "twitch") -> None:
    archive_db.insert_messages(
        platform, video_id,
        [{"offset_sec": 1.0, "username": "u", "text": text}],
    )


def test_migration_adds_kind_column_and_backfills():
    cols = {r["name"] for r in archive_db.query("PRAGMA table_info(videos)")}
    assert "kind" in cols
    legacy = archive_db.query("SELECT kind FROM videos WHERE video_id='legacy-vid'")
    assert legacy and legacy[0]["kind"] == "vod"


def test_migration_idempotent_on_second_call():
    # Second pass must be a no-op (no duplicate column / no error).
    archive_db._ensure_kind_column(archive_db.get_conn())
    cols = {r["name"] for r in archive_db.query("PRAGMA table_info(videos)")}
    assert "kind" in cols
    # SCHEMA re-run (CREATE TABLE IF NOT EXISTS path) also stays a no-op.
    archive_db.execute("SELECT kind FROM videos LIMIT 1")


def test_upsert_video_kind_defaults_and_normalizes():
    _insert_video("kind-default")
    _insert_video("kind-clip", kind="CLIP")  # uppercase normalized
    _insert_video("kind-bogus", kind="movie")  # invalid -> honest 'vod'
    assert archive_db.query("SELECT kind FROM videos WHERE video_id='kind-default'")[0]["kind"] == "vod"
    assert archive_db.query("SELECT kind FROM videos WHERE video_id='kind-clip'")[0]["kind"] == "clip"
    assert archive_db.query("SELECT kind FROM videos WHERE video_id='kind-bogus'")[0]["kind"] == "vod"
    # upsert updates kind on conflict
    archive_db.upsert_video({
        "platform": "twitch", "video_id": "kind-default",
        "channel": "lubu", "title": "t", "kind": "live",
    })
    assert archive_db.query("SELECT kind FROM videos WHERE video_id='kind-default'")[0]["kind"] == "live"


def test_list_videos_returns_kind():
    rows = {v["video_id"]: v for v in archive_db.list_videos()}
    assert rows["kind-clip"]["kind"] == "clip"


def _seed_search_fixture():
    archive_db.execute(
        "DELETE FROM messages_fts WHERE rowid IN "
        "(SELECT id FROM messages WHERE video_id LIKE 'filter-%')"
    )
    archive_db.execute(
        "DELETE FROM transcripts_fts WHERE rowid IN "
        "(SELECT id FROM transcripts WHERE video_id LIKE 'filter-%')"
    )
    archive_db.execute("DELETE FROM messages WHERE video_id LIKE 'filter-%'")
    archive_db.execute("DELETE FROM transcripts WHERE video_id LIKE 'filter-%'")
    _insert_video("filter-t-lubu", channel="lubu", started_at="2026-07-30T12:00:00Z", kind="vod")
    _insert_video("filter-t-titiltei", channel="titiltei", platform="kick",
                  started_at="2026-07-31T12:00:00Z", kind="clip")
    _insert_video("filter-t-yt", channel="TiTiltei", platform="youtube",
                  started_at="2026-08-01T12:00:00Z", kind="live")
    _insert_msg("filter-t-lubu", "zebra filter word", "twitch")
    _insert_msg("filter-t-titiltei", "zebra filter word", "kick")
    _insert_msg("filter-t-yt", "zebra filter word", "youtube")
    archive_db.insert_transcript(
        "twitch", "filter-t-lubu",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "zebra filter word"}],
    )


def _hit_ids(hits: list[dict]) -> set[str]:
    return {(h["kind"], h["video_id"]) for h in hits}


def test_search_channel_filter():
    _seed_search_fixture()
    hits = archive_db.search("zebra", channel="lubu")
    assert _hit_ids(hits) == {("message", "filter-t-lubu"), ("transcript", "filter-t-lubu")}
    hits = archive_db.search("zebra", channel="titiltei")
    assert _hit_ids(hits) == {("message", "filter-t-titiltei")}


def test_search_platform_filter_single_and_multi():
    _seed_search_fixture()
    assert _hit_ids(archive_db.search("zebra", platform="twitch")) == {
        ("message", "filter-t-lubu"), ("transcript", "filter-t-lubu"),
    }
    assert _hit_ids(archive_db.search("zebra", platform="kick,youtube")) == {
        ("message", "filter-t-titiltei"), ("message", "filter-t-yt"),
    }


def test_search_date_range_filter():
    _seed_search_fixture()
    # inclusive bounds on the started_at date part
    assert _hit_ids(archive_db.search("zebra", date_from="2026-07-31", date_to="2026-07-31")) == {
        ("message", "filter-t-titiltei"),
    }
    assert _hit_ids(archive_db.search("zebra", date_from="2026-07-30", date_to="2026-07-31")) == {
        ("message", "filter-t-lubu"), ("message", "filter-t-titiltei"),
        ("transcript", "filter-t-lubu"),
    }
    assert _hit_ids(archive_db.search("zebra", date_from="2026-08-02")) == set()


def test_search_kind_filter_single_and_multi():
    _seed_search_fixture()
    assert _hit_ids(archive_db.search("zebra", kind="clip")) == {("message", "filter-t-titiltei")}
    assert _hit_ids(archive_db.search("zebra", kind="vod,live")) == {
        ("message", "filter-t-lubu"), ("message", "filter-t-yt"),
        ("transcript", "filter-t-lubu"),
    }
    # unknown kinds are dropped, not fatal
    assert len(archive_db.search("zebra", kind="vod,movie")) == 2


def test_search_all_filters_combined():
    _seed_search_fixture()
    hits = archive_db.search(
        "zebra", channel="lubu", platform="twitch",
        date_from="2026-07-30", date_to="2026-07-30", kind="vod",
    )
    assert _hit_ids(hits) == {("message", "filter-t-lubu"), ("transcript", "filter-t-lubu")}
    assert archive_db.search(
        "zebra", channel="lubu", platform="twitch",
        date_from="2026-08-01", date_to="2026-08-01", kind="vod",
    ) == []


def test_search_hits_carry_video_extras():
    _seed_search_fixture()
    hit = next(h for h in archive_db.search("zebra", kind="clip") if h["video_id"] == "filter-t-titiltei")
    assert hit["channel"] == "titiltei"
    assert hit["title"] == "title filter-t-titiltei"
    assert hit["date"] == "2026-07-31T12:00:00Z"
    assert hit["video_kind"] == "clip"
    # legacy contract fields still present
    for field in ("kind", "platform", "video_id", "offset_sec", "text", "score"):
        assert field in hit


def test_search_missing_video_rows_still_surface_unfiltered():
    # Messages whose video row is absent (e.g. pre-video ingestion order)
    # must still be found when no video-backed filter is active (LEFT JOIN).
    archive_db.insert_messages(
        "twitch", "orphan-vid", [{"offset_sec": 2.0, "username": "u", "text": "orphan zebra"}],
    )
    try:
        assert any(h["video_id"] == "orphan-vid" for h in archive_db.search("orphan"))
        # ...but a video-backed filter excludes them (no video row to match).
        assert not any(
            h["video_id"] == "orphan-vid" for h in archive_db.search("orphan", channel="lubu")
        )
    finally:
        archive_db.execute("DELETE FROM messages WHERE video_id='orphan-vid'")


def test_search_empty_kind_is_no_filter():
    _seed_search_fixture()
    assert len(archive_db.search("zebra", kind="")) == 4


async def test_search_kind_uppercase_passes_router_validation():
    # kind=CLIP must pass the router's membership check (search() lowercases
    # internally); only truly invalid kinds get a 400.
    from fastapi import HTTPException
    from routers.archive import archive_search

    _seed_search_fixture()
    resp = await archive_search(q="zebra", kind="CLIP", limit=20)
    assert _hit_ids(resp["hits"]) == {("message", "filter-t-titiltei")}
    resp = await archive_search(q="zebra", kind="Vod,LIVE", limit=20)
    assert _hit_ids(resp["hits"]) == {
        ("message", "filter-t-lubu"), ("message", "filter-t-yt"),
        ("transcript", "filter-t-lubu"),
    }
    with pytest.raises(HTTPException) as exc:
        await archive_search(q="zebra", kind="movie", limit=20)
    assert exc.value.status_code == 400


async def test_search_date_requires_strict_iso():
    # Python 3.11 date.fromisoformat() accepts 'YYYYMMDD' (SQLite date()
    # turns it into NULL -> silent 0 hits); the router must 400 on it and on
    # non-calendar dates, and accept a valid strict date.
    from fastapi import HTTPException
    from routers.archive import _is_iso_date, archive_search

    assert _is_iso_date("2026-08-01")
    assert not _is_iso_date("20260802")
    assert not _is_iso_date("2026-02-30")
    assert not _is_iso_date("2026-13-01")
    assert not _is_iso_date("2026-8-1")
    assert not _is_iso_date("abc")
    for bad in ("20260802", "2026-02-30", "2026-13-01", "abc"):
        with pytest.raises(HTTPException) as exc:
            await archive_search(q="zebra", date_from=bad, limit=20)
        assert exc.value.status_code == 400
    # valid strict date passes validation and filters normally
    _seed_search_fixture()
    resp = await archive_search(q="zebra", date_from="2026-08-01", limit=20)
    assert _hit_ids(resp["hits"]) == {("message", "filter-t-yt")}
