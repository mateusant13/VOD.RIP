"""Search filter + kind migration tests against a scratch archive DB.

The env var MUST be set before the first services.archive_db import anywhere
in the pytest session; this module is the only importer, so it is set at
module top (before `from services import archive_db`), binding the global
connection to the temp DB.

The DB is pre-created with the LEGACY videos schema (no kind column) AND
legacy regular-FTS5 indexes (no content= option), plus one legacy row each,
so both idempotent migrations (ALTER TABLE kind + external-content FTS
rebuild) run for real on first connect — exactly the upgrade path of an
existing user DB.

Run from backend/: python -m pytest tests/test_archive_search_filters.py
"""
from __future__ import annotations

import asyncio
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
CREATE TABLE messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  platform   TEXT NOT NULL,
  video_id   TEXT NOT NULL,
  offset_sec REAL NOT NULL,
  user_id    TEXT,
  username   TEXT NOT NULL,
  text       TEXT NOT NULL,
  badges     TEXT NOT NULL DEFAULT '[]',
  emotes     TEXT NOT NULL DEFAULT '[]',
  ts         TEXT
);
CREATE TABLE transcripts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  platform   TEXT NOT NULL,
  video_id   TEXT NOT NULL,
  seg_idx    INTEGER NOT NULL,
  start_sec  REAL NOT NULL,
  end_sec    REAL NOT NULL,
  text       TEXT NOT NULL,
  words_json TEXT NOT NULL DEFAULT '[]'
);
-- Legacy regular FTS5 (pre-contentless): text duplicated in the index.
CREATE VIRTUAL TABLE messages_fts USING fts5(text);
CREATE VIRTUAL TABLE transcripts_fts USING fts5(text);
""")
_legacy.execute(
    """INSERT INTO videos (platform, video_id, channel, title, started_at,
       created_at, updated_at)
       VALUES ('twitch', 'legacy-vid', 'legacychan', 'legacy', '2026-07-30T10:00:00Z',
       '2026-07-30T10:00:00Z', '2026-07-30T10:00:00Z')"""
)
# Legacy rows indexed the old way (content + duplicate FTS insert): the
# rebuild must carry these into the external-content index untouched.
_legacy.execute(
    """INSERT INTO messages (platform, video_id, offset_sec, username, text,
       badges, emotes)
       VALUES ('twitch', 'legacy-chat', 1.0, 'u', 'legacy shaco chat', '[]', '[]')"""
)
_legacy.execute(
    "INSERT INTO messages_fts (rowid, text) VALUES (?, ?)",
    (_legacy.execute("SELECT last_insert_rowid()").fetchone()[0], "legacy shaco chat"),
)
_legacy.execute(
    """INSERT INTO transcripts (platform, video_id, seg_idx, start_sec, end_sec,
       text, words_json)
       VALUES ('twitch', 'legacy-trans', 0, 0.0, 1.0, 'legacy bronzinhos transcript', '[]')"""
)
_legacy.execute(
    "INSERT INTO transcripts_fts (rowid, text) VALUES (?, ?)",
    (_legacy.execute("SELECT last_insert_rowid()").fetchone()[0], "legacy bronzinhos transcript"),
)
_legacy.commit()
_legacy.close()

os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)

from services import archive_db  # noqa: E402  (env must be set first)


@pytest.fixture(scope="module", autouse=True)
def _search_scratch_db():
    """Rebind the global connection to THIS module's legacy scratch DB so
    the migration + legacy-vid tests stay honest regardless of import or
    collection order (later modules clobber the env var at import time).
    Runs the real legacy->current migrations (kind column + external-content
    FTS rebuild) on first connect. conftest.py already guarantees the real
    %APPDATA% archive.db is never the target."""
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False
    archive_db.get_conn()  # rebind now: legacy ALTER TABLE migration runs here
    yield
    if prev is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False

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


def _fts_schema() -> dict[str, str]:
    rows = archive_db.query(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name LIKE '%_fts'"
    )
    return {r["name"]: r["sql"] for r in rows}


def test_fts_migrated_to_external_content():
    # Legacy regular-FTS tables must be converted to external-content mode:
    # text lives once in the content table (no _content shadow), triggers
    # own the index, and the pre-migration rows are still searchable.
    sql = _fts_schema()
    assert "content='messages'" in sql["messages_fts"]
    assert "content='transcripts'" in sql["transcripts_fts"]
    for fts, content in (("messages_fts", "messages"), ("transcripts_fts", "transcripts")):
        shadows = {
            r[0] for r in archive_db.query(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?",
                (fts + "%",),
            )
        }
        assert not any(s.endswith("_content") for s in shadows), f"{fts} still stores text"
        trigs = {
            r[0] for r in archive_db.query(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
                (fts[:-4] + "_%",),
            )
        }
        assert {f"{fts}_ai", f"{fts}_ad", f"{fts}_au"} <= trigs
        # Rebuilt index is complete: one entry per content row, no dupes.
        assert archive_db.query(f"SELECT count(*) FROM {fts}")[0][0] == \
            archive_db.query(f"SELECT count(*) FROM {content}")[0][0]
    hits = archive_db.search("shaco")
    assert any(h["kind"] == "message" and h["video_id"] == "legacy-chat" for h in hits)
    hits = archive_db.search("bronzinhos")
    assert any(h["kind"] == "transcript" and h["video_id"] == "legacy-trans" for h in hits)


def test_fts_migration_idempotent_on_second_call():
    # Second pass must be a no-op: same DDL, no rebuild, no leftover tables.
    before = _fts_schema()
    assert archive_db._migrate_fts_contentless(archive_db.get_conn()) is False
    assert _fts_schema() == before
    assert not archive_db.query("SELECT name FROM sqlite_master WHERE name LIKE '%_new'")
    archive_db.execute("SELECT count(*) FROM messages_fts")


def test_fts_delete_cascades_to_index():
    # The AFTER DELETE triggers own the index: deleting content rows must
    # remove their entries (self-check scrub relies on this) with no orphans.
    archive_db.insert_messages(
        "twitch", "fts-cascade",
        [{"offset_sec": 1.0, "username": "u", "text": "cascadeprobe chat text"}],
    )
    archive_db.insert_transcript(
        "twitch", "fts-cascade",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "cascadeprobe transcript"}],
    )
    assert archive_db.search("cascadeprobe")  # both kinds indexed via triggers
    archive_db.execute("DELETE FROM messages WHERE video_id='fts-cascade'")
    archive_db.execute("DELETE FROM transcripts WHERE video_id='fts-cascade'")
    assert not archive_db.search("cascadeprobe")
    assert archive_db.query("SELECT count(*) FROM messages_fts")[0][0] == \
        archive_db.query("SELECT count(*) FROM messages")[0][0]
    assert archive_db.query("SELECT count(*) FROM transcripts_fts")[0][0] == \
        archive_db.query("SELECT count(*) FROM transcripts")[0][0]


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
    # FTS index entries cascade via AFTER DELETE triggers; content deletes only.
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
    # case-insensitive: "titiltei" also matches youtube's "TiTiltei"
    hits = archive_db.search("zebra", channel="titiltei")
    assert _hit_ids(hits) == {("message", "filter-t-titiltei"), ("message", "filter-t-yt")}


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


# --- source / video_id / comma-channel (search panel v2) ------------------


def test_search_source_chat_only_messages():
    _seed_search_fixture()
    assert _hit_ids(archive_db.search("zebra", source="chat")) == {
        ("message", "filter-t-lubu"), ("message", "filter-t-titiltei"),
        ("message", "filter-t-yt"),
    }


def test_search_source_transcript_only():
    _seed_search_fixture()
    assert _hit_ids(archive_db.search("zebra", source="transcript")) == {
        ("transcript", "filter-t-lubu"),
    }


def test_search_source_both_is_default():
    _seed_search_fixture()
    assert len(archive_db.search("zebra")) == 4
    assert len(archive_db.search("zebra", source="both")) == 4


def test_search_video_id_scopes_to_one_video():
    _seed_search_fixture()
    # A louder hit on ANOTHER video would outrank every scoped hit — the
    # video_id filter must exclude it at SQL level, not by post-filtering.
    archive_db.insert_messages(
        "twitch", "filter-t-louder",
        [{"offset_sec": 1.0, "username": "u", "text": "zebra zebra zebra zebra"}],
    )
    try:
        assert archive_db.search("zebra", limit=1)[0]["video_id"] == "filter-t-louder"
        hits = archive_db.search("zebra", video_id="filter-t-lubu")
        assert _hit_ids(hits) == {
            ("message", "filter-t-lubu"), ("transcript", "filter-t-lubu"),
        }
        assert all(h["video_id"] == "filter-t-lubu" for h in hits)
    finally:
        archive_db.execute("DELETE FROM messages WHERE video_id='filter-t-louder'")


def test_search_video_id_composes_with_source_and_channel():
    _seed_search_fixture()
    assert _hit_ids(archive_db.search("zebra", video_id="filter-t-lubu", source="chat")) == {
        ("message", "filter-t-lubu"),
    }
    assert _hit_ids(archive_db.search("zebra", video_id="filter-t-lubu", source="transcript")) == {
        ("transcript", "filter-t-lubu"),
    }
    assert _hit_ids(archive_db.search(
        "zebra", video_id="filter-t-lubu", channel="titiltei",
    )) == set()


def test_search_channel_comma_list_matches_any_slug():
    _seed_search_fixture()
    hits = archive_db.search("zebra", channel="lubu,titiltei")
    assert _hit_ids(hits) == {
        ("message", "filter-t-lubu"), ("transcript", "filter-t-lubu"),
        ("message", "filter-t-titiltei"), ("message", "filter-t-yt"),
    }
    # case-insensitive per slug: "titiltei" matches "TiTiltei" (youtube)
    assert any(h["video_id"] == "filter-t-yt" for h in hits)


def test_search_channel_single_value_keeps_exact_match():
    _seed_search_fixture()
    assert _hit_ids(archive_db.search("zebra", channel="lubu")) == {
        ("message", "filter-t-lubu"), ("transcript", "filter-t-lubu"),
    }
    # empty segments are dropped defensively at the db layer
    assert _hit_ids(archive_db.search("zebra", channel="lubu,")) == {
        ("message", "filter-t-lubu"), ("transcript", "filter-t-lubu"),
    }


async def test_search_source_router_validation_and_passthrough():
    from fastapi import HTTPException
    from routers.archive import archive_search

    _seed_search_fixture()
    resp = await archive_search(q="zebra", source="chat", limit=20)
    assert _hit_ids(resp["hits"]) == {
        ("message", "filter-t-lubu"), ("message", "filter-t-titiltei"),
        ("message", "filter-t-yt"),
    }
    resp = await archive_search(q="zebra", source="TRANSCRIPT", limit=20)
    assert _hit_ids(resp["hits"]) == {("transcript", "filter-t-lubu")}
    resp = await archive_search(q="zebra", source="both", limit=20)
    assert len(resp["hits"]) == 4
    for bad in ("streamer", "chat,transcript"):
        with pytest.raises(HTTPException) as exc:
            await archive_search(q="zebra", source=bad, limit=20)
        assert exc.value.status_code == 400


async def test_search_video_id_router_passthrough():
    from routers.archive import archive_search

    _seed_search_fixture()
    resp = await archive_search(q="zebra", video_id="filter-t-lubu", limit=20)
    assert _hit_ids(resp["hits"]) == {
        ("message", "filter-t-lubu"), ("transcript", "filter-t-lubu"),
    }


async def test_search_channel_empty_segment_router_400():
    from fastapi import HTTPException
    from routers.archive import archive_search

    _seed_search_fixture()
    with pytest.raises(HTTPException) as exc:
        await archive_search(q="zebra", channel="lubu,", limit=20)
    assert exc.value.status_code == 400
    resp = await archive_search(q="zebra", channel="lubu,titiltei", limit=20)
    # case-insensitive: titiltei also matches youtube's "TiTiltei"
    assert len(resp["hits"]) == 4


# --- fuzzy expansion / lang filter / backfill (archive search overhaul) -----


@pytest.fixture(autouse=True)
def _no_network_backfill(monkeypatch):
    """archive_search auto-kicks background backfill tasks that would hit
    gql.twitch.tv; stub backfill_chat so every router test stays offline."""
    def _fake(channel, video_id, **kwargs):
        return {"inserted": 0}

    monkeypatch.setattr("services.archive_twitch.backfill_chat", _fake)


def test_fuzzy_expansion_arthur_to_artur():
    # Contract: the token-candidate function expands "arthur" to "artur" and
    # search() finds the misspelled query via the FTS5 vocab.
    archive_db.insert_messages(
        "twitch", "fuzzy-artur",
        [{"offset_sec": 1.0, "username": "u", "text": "artur cabral futebol"}],
    )
    try:
        with archive_db._vocab_lock:
            archive_db._vocab_cache.pop("messages", None)
            archive_db._token_cache.clear()
        vocab = archive_db._load_vocab("messages")
        assert vocab is not None, "fts5vocab must be readable for messages"
        cands = archive_db._token_expansions(
            "arthur", [vocab], archive_db._load_bigrams(["messages"])
        )
        assert any(t == "artur" for t, _ in cands), \
            f"arthur must fuzzy-expand to artur, got {cands}"
        hits = archive_db.search("arthur")
        assert any(h["video_id"] == "fuzzy-artur" for h in hits), "search must match via expansion"
    finally:
        archive_db.execute("DELETE FROM messages WHERE video_id='fuzzy-artur'")
        with archive_db._vocab_lock:
            archive_db._vocab_cache.pop("messages", None)
            archive_db._token_cache.clear()


def test_phonetic_fold_hard_c_contract():
    # R1: hard c folds to k before a/u or word end; soft c stays s before
    # e/i/o so diacritic-stripped ç tokens keep bridging.
    f = archive_db._phonetic_fold
    assert f("cata") == f("kata") == "kata"
    assert f("catarina") == "katarina"
    assert f("aco") == "asu"     # 'aço' stripped by FTS5 unicode61
    assert f("nasco") == "nasu"  # 'nasço' stripped
    assert f("shaco") == "shasu"


def test_token_expansions_suppress_high_freq_dist1():
    # R3: a dist>=1 candidate that is corpus-common must not be offered —
    # 'cara' (freq 2000) is chat noise, never the intended fuzzy target.
    vocab = {4: [("cata", 5), ("cara", 2000), ("kata", 10)]}
    with archive_db._vocab_lock:
        archive_db._token_cache.clear()
    try:
        cands = archive_db._token_expansions("cata", [vocab], None)
        assert not any(t == "cara" for t, _ in cands), \
            f"'cara' must be suppressed, got {cands}"
        assert any(t == "cata" for t, _ in cands), "exact token always survives"
        assert any(t == "kata" for t, _ in cands), "fold-equal survives at dist 0"
    finally:
        with archive_db._vocab_lock:
            archive_db._token_cache.clear()


def test_token_expansions_rare_short_pair_survives_suppression():
    # R3 must not kill legit rare fuzzy pairs: 'shen' -> ('suen', 1).
    vocab = {4: [("shen", 10), ("suen", 3)]}
    with archive_db._vocab_lock:
        archive_db._token_cache.clear()
    try:
        cands = archive_db._token_expansions("shen", [vocab], None)
        assert ("suen", 1) in cands, f"rare dist-1 pair must survive, got {cands}"
    finally:
        with archive_db._vocab_lock:
            archive_db._token_cache.clear()


def test_token_expansions_tier0_prefix_raw_and_folded():
    # R2: 'cata' reaches 'catarina' by raw prefix; 'kata' via the folded
    # prefix (fold('catarina') == 'katarina' after the hard-c fold).
    vocab = {4: [("cata", 5), ("kata", 10)], 8: [("catarina", 30)]}
    for tok in ("cata", "kata"):
        with archive_db._vocab_lock:
            archive_db._token_cache.clear()
        cands = archive_db._token_expansions(tok, [vocab], None)
        assert ("catarina", 0) in cands, \
            f"{tok} must reach catarina at tier 0, got {cands}"


def test_token_expansions_prefix_gate_blocks_high_freq_token():
    # R2 gate: 'cara' (merged freq 3106 > 300) must NOT emit prefix terms —
    # ungated it would flood tier 0 with caralho/caramba/carrasco.
    vocab = {
        4: [("cara", 3106)],
        7: [("caralho", 5), ("caramba", 3), ("carrasco", 2)],
    }
    with archive_db._vocab_lock:
        archive_db._token_cache.clear()
    try:
        cands = archive_db._token_expansions("cara", [vocab], None)
        assert all(t == "cara" for t, _ in cands), \
            f"high-freq token must not emit prefix terms, got {cands}"
    finally:
        with archive_db._vocab_lock:
            archive_db._token_cache.clear()


def test_search_cata_suppresses_cara_noise():
    # R2+R3 end-to-end: 'cara' is chat-spam common (~1500 corpus rows) and
    # 'catarina' is the rare intended target; search('cata') / search('kata')
    # must surface the 'catarina' rows at tier 0 and never the 'cara' rows
    # ('cara' is dropped from the dist-1 expansions by merged-freq
    # suppression, and no prefix term leaks it back in).
    cara_vid, cat_vid = "fuzzy-cara-spam", "fuzzy-catarina"
    archive_db.insert_messages(
        "twitch", cara_vid,
        [{"offset_sec": 1.0 + i * 0.5, "username": "u", "text": f"cara x{i}"}
         for i in range(1500)],
    )
    archive_db.insert_messages(
        "twitch", cat_vid,
        [{"offset_sec": 1.0, "username": "u", "text": "catarina joga bem"},
         {"offset_sec": 2.0, "username": "u", "text": "a catarina ganhou"}],
    )
    try:
        for tok in ("cata", "kata"):
            with archive_db._vocab_lock:
                archive_db._vocab_cache.pop("messages", None)
                archive_db._token_cache.clear()
            hits = archive_db.search(tok, limit=30)
            assert any(h["video_id"] == cat_vid for h in hits), \
                f"'{tok}' must find the catarina rows, hits: {[h['video_id'] for h in hits]}"
            assert not any(h["video_id"] == cara_vid for h in hits), \
                f"'{tok}' must not surface the cara-spam rows, hits: {[h['video_id'] for h in hits]}"
    finally:
        archive_db.execute(
            "DELETE FROM messages WHERE video_id IN (?, ?)", (cara_vid, cat_vid)
        )
        with archive_db._vocab_lock:
            archive_db._vocab_cache.pop("messages", None)
            archive_db._token_cache.clear()


def test_search_lang_filter_pt_en_and_hits_carry_lang():
    _seed_search_fixture()
    archive_db.insert_transcript(
        "twitch", "lang-pt",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "zebra lang pt"}],
        lang="pt-br",  # normalized to 'pt' on write
    )
    archive_db.insert_transcript(
        "twitch", "lang-en",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "zebra lang en"}],
        lang="en",
    )
    archive_db.insert_transcript(
        "twitch", "lang-none",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "zebra lang none"}],
    )
    archive_db.insert_transcript(
        "twitch", "lang-es",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "zebra lang es"}],
        lang="es",
    )
    try:
        # pt matches pt rows AND untagged (whisper) rows, incl. the seeded
        # transcript whose lang is NULL; en matches only en rows.
        assert _hit_ids(archive_db.search("zebra", lang="pt")) == {
            ("message", "filter-t-lubu"), ("message", "filter-t-titiltei"),
            ("message", "filter-t-yt"),
            ("transcript", "filter-t-lubu"), ("transcript", "lang-pt"),
            ("transcript", "lang-none"),
        }
        assert _hit_ids(archive_db.search("zebra", lang="en")) == {
            ("message", "filter-t-lubu"), ("message", "filter-t-titiltei"),
            ("message", "filter-t-yt"),
            ("transcript", "lang-en"),
        }
        # lang values other than pt/en are ignored -> all 8 hits
        assert len(archive_db.search("zebra", lang="es")) == 8
        # hits carry lang: transcripts the stored tag, messages None
        by_id = {}
        for h in archive_db.search("zebra"):
            if h["kind"] == "transcript":
                by_id[h["video_id"]] = h
        assert by_id["lang-pt"]["lang"] == "pt", "pt-br must normalize to pt"
        assert by_id["lang-en"]["lang"] == "en"
        assert by_id["lang-none"]["lang"] is None
        msg_hit = next(h for h in archive_db.search("zebra") if h["kind"] == "message")
        assert msg_hit["lang"] is None, "message hits must carry lang=None"
        # lang filters transcripts only: chat source is unaffected
        assert _hit_ids(archive_db.search("zebra", lang="en", source="chat")) == {
            ("message", "filter-t-lubu"), ("message", "filter-t-titiltei"),
            ("message", "filter-t-yt"),
        }
    finally:
        for vid in ("lang-pt", "lang-en", "lang-none", "lang-es"):
            archive_db.execute("DELETE FROM transcripts WHERE video_id=?", (vid,))


def test_search_channel_matching_is_case_insensitive():
    _seed_search_fixture()
    # uppercase slug matches lowercase stored channel...
    assert _hit_ids(archive_db.search("zebra", channel="LUBU")) == {
        ("message", "filter-t-lubu"), ("transcript", "filter-t-lubu"),
    }
    # ...and a lowercase slug matches a mixed-case stored channel (TiTiltei)
    assert _hit_ids(archive_db.search("zebra", channel="titiltei")) == {
        ("message", "filter-t-titiltei"), ("message", "filter-t-yt"),
    }


async def test_backfill_route_statuses_and_dedupe(monkeypatch):
    import threading

    from fastapi import HTTPException
    from routers.archive import (_backfill_inflight, _backfill_lock,
                                 archive_chat_backfill)

    archive_db.upsert_video({
        "platform": "twitch", "video_id": "123456", "channel": "caedrel",
        "title": "t", "kind": "vod",
    })
    archive_db.upsert_video({
        "platform": "twitch", "video_id": "654321", "channel": "caedrel",
        "title": "t2", "kind": "vod",
    })
    archive_db.insert_messages(
        "twitch", "654321", [{"offset_sec": 1.0, "username": "u", "text": "hello"}],
    )
    release = threading.Event()
    calls: list[tuple[str, str, int]] = []

    def fake_backfill(channel, video_id, **kwargs):
        calls.append((channel, video_id, kwargs.get("max_messages")))
        release.wait(5)
        return {"inserted": 1}

    monkeypatch.setattr("services.archive_twitch.backfill_chat", fake_backfill)
    try:
        resp = await archive_chat_backfill(platform="twitch", video_id="123456")
        assert resp == {"ok": True, "status": "queued"}
        # in-flight dedupe: second kick reports running, no new task
        resp = await archive_chat_backfill(platform="twitch", video_id="123456")
        assert resp["status"] == "running"
        # chat rows exist -> already, no kick
        resp = await archive_chat_backfill(platform="twitch", video_id="654321")
        assert resp["status"] == "already"
        # non-twitch platform -> 400; non-numeric id -> 400; missing row -> 404
        with pytest.raises(HTTPException) as exc:
            await archive_chat_backfill(platform="kick", video_id="123456")
        assert exc.value.status_code == 400
        with pytest.raises(HTTPException) as exc:
            await archive_chat_backfill(platform="twitch", video_id="abc")
        assert exc.value.status_code == 400
        with pytest.raises(HTTPException) as exc:
            await archive_chat_backfill(platform="twitch", video_id="999999")
        assert exc.value.status_code == 404
        # Route bodies are fully synchronous, so the background task only runs
        # when this test yields the loop — give it a tick before asserting.
        await asyncio.sleep(0.05)
        assert calls == [("caedrel", "123456", 100_000)], (
            "exactly one background kick, with the full-chat cap"
        )
    finally:
        release.set()
        for _ in range(100):  # drain the task so the loop closes cleanly
            with _backfill_lock:
                if "123456" not in _backfill_inflight:
                    break
            await asyncio.sleep(0.02)
        with _backfill_lock:
            _backfill_inflight.discard("123456")
        archive_db.execute("DELETE FROM messages WHERE video_id IN ('123456','654321')")
        archive_db.execute("DELETE FROM videos WHERE video_id IN ('123456','654321')")


async def test_search_auto_backfill_kicks_chatless_twitch_videos(monkeypatch):
    import threading

    import routers.archive as archive_router

    archive_db.upsert_video({
        "platform": "twitch", "video_id": "1111", "channel": "caedrel",
        "title": "a", "started_at": "2026-08-01T00:00:00Z", "kind": "vod",
    })
    archive_db.upsert_video({
        "platform": "twitch", "video_id": "2222", "channel": "caedrel",
        "title": "b", "started_at": "2026-08-02T00:00:00Z", "kind": "vod",
    })
    archive_db.upsert_video({
        "platform": "twitch", "video_id": "3333", "channel": "other",
        "title": "c", "started_at": "2026-08-03T00:00:00Z", "kind": "vod",
    })
    release = threading.Event()
    calls: list[str] = []

    def fake_backfill(channel, video_id, **kwargs):
        calls.append(video_id)
        release.wait(5)
        return {"inserted": 0}

    monkeypatch.setattr("services.archive_twitch.backfill_chat", fake_backfill)
    with archive_router._backfill_lock:
        archive_router._last_auto_kick = 0.0
    try:
        # channel-scoped: only caedrel's two chat-less videos get kicked
        archive_router._maybe_auto_backfill(
            platform="twitch", channel="caedrel", source="both")
        await asyncio.sleep(0.05)  # background tasks need a loop tick
        assert sorted(calls) == ["1111", "2222"]
        # in-flight dedupe + 15s throttle: second pass kicks nothing new
        archive_router._maybe_auto_backfill(
            platform="twitch", channel="caedrel", source="both")
        await asyncio.sleep(0.05)
        assert sorted(calls) == ["1111", "2222"]
        # non-chat source / platform filter without twitch: never kick
        archive_router._maybe_auto_backfill(
            platform="youtube", channel="caedrel", source="both")
        archive_router._maybe_auto_backfill(
            platform=None, channel=None, source="transcript")
        await asyncio.sleep(0.05)
        assert sorted(calls) == ["1111", "2222"]
        # platform unset counts as twitch-in-scope
        with archive_router._backfill_lock:
            archive_router._last_auto_kick = 0.0
        archive_router._maybe_auto_backfill(
            platform=None, channel="other", source="chat")
        await asyncio.sleep(0.05)
        assert sorted(calls) == ["1111", "2222", "3333"]
    finally:
        release.set()
        for _ in range(100):
            with archive_router._backfill_lock:
                if not archive_router._backfill_inflight:
                    break
            await asyncio.sleep(0.02)
        with archive_router._backfill_lock:
            archive_router._backfill_inflight.clear()
            archive_router._last_auto_kick = 0.0
        archive_db.execute("DELETE FROM videos WHERE video_id IN ('1111','2222','3333')")
