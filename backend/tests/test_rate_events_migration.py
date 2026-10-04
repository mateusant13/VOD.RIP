"""rate_limit_events — the migration itself, and what it is FOR.

Companion to test_rate_limit_history.py (which covers the recorder, the
read endpoints and the gates). This file pins the part that made the whole
feature invisible on a long-lived archive: getting the table onto a DB that
predates it, and proving the governor then actually learns.

What is pinned here:

  1. a FRESH db has the table with the full column set;
  2. _ensure_rate_limit_events CREATES the table when it is absent, without
     needing SCHEMA to have run first (the regression — see below);
  3. an EXISTING db written by an older build gains the table in place on
     open, and loses no videos/messages/transcripts;
  4. running that migration twice is safe (the app may open the same archive
     from more than one process);
  5. once events exist, prime_from_history() returns real rows and moves the
     ceiling off its cold-start default — the whole point of the table.

The regression this file exists for: _ensure_rate_limit_events used to
`return` when the table was absent, deferring to SCHEMA's executescript. The
failure that produced was invisible, because record_rate_limit() catches
every exception at debug level — a missing table is indistinguishable from a
platform that simply never got rate-limited, and prime_from_history() returns
{} forever. The migration named after the table must be able to create it.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

os.environ["VODRIP_ARCHIVE_DB"] = str(
    Path(tempfile.mkdtemp(prefix="rate-events-migration-")) / "archive.db")

import pytest  # noqa: E402

from services import archive_db, rate_budget  # noqa: E402


# --- fixtures --------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _scratch_db():
    """Pin the shared archive connection to THIS module's scratch DB."""
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(
        Path(tempfile.mkdtemp(prefix="rate-events-migration-")) / "archive.db")
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    if prev is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev
    archive_db._conn = None
    archive_db._schema_ready = False


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}


def _make_old_db(path: Path) -> None:
    """A DB shaped like the production archive BEFORE the rate-history lane:
    real content tables, and NO rate_limit_events anywhere.

    Built with raw sqlite3 so the file genuinely has the old shape rather
    than being a fresh archive_db DB with a table dropped.
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE videos ("
        " platform TEXT NOT NULL, video_id TEXT NOT NULL, channel TEXT NOT NULL,"
        " title TEXT NOT NULL, started_at TEXT, ended_at TEXT,"
        " duration_sec REAL, archive_path TEXT, canonical_key TEXT,"
        " status TEXT NOT NULL DEFAULT 'known',"
        " kind TEXT NOT NULL DEFAULT 'vod',"
        " created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
        " PRIMARY KEY (platform, video_id));"
        "CREATE TABLE messages ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL,"
        " video_id TEXT NOT NULL, offset_sec REAL NOT NULL, user_id TEXT,"
        " username TEXT NOT NULL, text TEXT NOT NULL, badges TEXT NOT NULL"
        " DEFAULT '[]', emotes TEXT NOT NULL DEFAULT '[]', ts TEXT);"
        "CREATE TABLE transcripts ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL,"
        " video_id TEXT NOT NULL, seg_idx INTEGER NOT NULL,"
        " start_sec REAL NOT NULL, end_sec REAL NOT NULL, text TEXT NOT NULL,"
        " words_json TEXT NOT NULL DEFAULT '[]', lang TEXT);"
    )
    conn.executescript(
        "INSERT INTO videos (platform, video_id, channel, title,"
        "                    created_at, updated_at)"
        " VALUES ('twitch', 'old-vid', 'chan', 'Old VOD',"
        "         '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00');"
        "INSERT INTO messages (platform, video_id, offset_sec, username, text)"
        " VALUES ('twitch', 'old-vid', 1.0, 'someone', 'pre-migration chat');"
        "INSERT INTO transcripts (platform, video_id, seg_idx, start_sec,"
        "                          end_sec, text)"
        " VALUES ('twitch', 'old-vid', 0, 0.0, 1.0, 'pre-migration transcript');"
    )
    conn.commit()
    assert "rate_limit_events" not in _tables(conn), (
        "fixture must genuinely predate the table, or the test proves nothing")
    conn.close()


# --- 1. fresh db ------------------------------------------------------------

def test_fresh_db_has_the_table_with_full_columns():
    conn = archive_db.get_conn()
    assert "rate_limit_events" in _tables(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(rate_limit_events)")}
    assert cols == {c for c, _ in archive_db._RL_EVENT_COLUMNS}


# --- 2. the regression: the ensure path can CREATE the table ---------------

def test_ensure_creates_the_table_when_absent_without_schema():
    """_ensure_rate_limit_events must not depend on SCHEMA having run.

    Drop the table, call the migration ALONE (no executescript anywhere near
    it), and require the table back with both indexes. This is the assertion
    that fails against the old `if not cols: return` body.
    """
    with archive_db._lock:
        conn = archive_db.get_conn()
        conn.executescript(
            "DROP TABLE IF EXISTS rate_limit_events;"
        )
        conn.commit()
    assert "rate_limit_events" not in _tables(archive_db.get_conn())

    # No SCHEMA. Just the migration, on its own.
    archive_db._ensure_rate_limit_events(conn=archive_db.get_conn())
    archive_db.get_conn().commit()

    conn = archive_db.get_conn()
    assert "rate_limit_events" in _tables(conn), (
        "the migration named after the table must be able to create it")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(rate_limit_events)")}
    assert cols == {c for c, _ in archive_db._RL_EVENT_COLUMNS}
    assert {"idx_rl_events_ts", "idx_rl_events_platform_ts"} <= _indexes(conn), (
        "the create path must bring the indexes too, or the reads scan")


def test_ensure_is_a_noop_when_the_table_is_already_right():
    """Second call on a correct table changes nothing and does not raise."""
    archive_db._ensure_rate_limit_events(conn=archive_db.get_conn())
    archive_db.get_conn().commit()
    before = archive_db.query("SELECT COUNT(*) c FROM rate_limit_events")[0][0]
    archive_db._ensure_rate_limit_events(conn=archive_db.get_conn())
    archive_db.get_conn().commit()
    after = archive_db.query("SELECT COUNT(*) c FROM rate_limit_events")[0][0]
    assert after == before


def test_ensure_creates_the_table_without_committing_the_callers_work(tmp_path):
    """The create path must not move the caller's transaction boundary.

    This migration runs in the middle of a larger batch in _init_schema, so
    it must not commit what the caller had pending. `executescript` would:
    it implicitly COMMITs before it runs. The guard here is that each DDL
    statement goes through conn.execute instead.
    """
    db = tmp_path / "txn.db"
    conn = sqlite3.connect(db, isolation_level="DEFERRED")
    try:
        conn.executescript(
            "CREATE TABLE videos (platform TEXT NOT NULL, video_id TEXT NOT NULL,"
            " channel TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL"
            " DEFAULT 'known', kind TEXT NOT NULL DEFAULT 'vod',"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
            " PRIMARY KEY (platform, video_id));"
        )
        # A pending change the caller has NOT committed yet.
        conn.execute(
            "INSERT INTO videos (platform, video_id, channel, title,"
            " created_at, updated_at)"
            " VALUES ('twitch', 'pending', 'c', 'uncommitted', 'x', 'x')")
        assert conn.in_transaction, "precondition: work is pending"

        archive_db._ensure_rate_limit_events(conn=conn)

        assert conn.in_transaction, (
            "the migration committed the caller's pending work — it must "
            "leave the transaction for _init_schema to commit")
        conn.rollback()
        # The rolled-back row is gone AND the table this migration created
        # came back with the rollback: both shared the caller's transaction.
        assert "rate_limit_events" not in _tables(conn)
        conn.rollback()
        # Re-run outside any transaction: the table must then land for good.
        archive_db._ensure_rate_limit_events(conn=conn)
        conn.commit()
        assert "rate_limit_events" in _tables(conn)
    finally:
        conn.close()


# --- 3. an existing DB is migrated forward, in place ------------------------

def test_existing_db_without_the_table_gains_it_and_keeps_everything(tmp_path):
    old_db = tmp_path / "old.db"
    _make_old_db(old_db)
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(old_db)
    archive_db._conn = None
    archive_db._schema_ready = False
    try:
        # Open the way the app opens it: one public call is enough to pull
        # the schema + migrations through _ensure_schema_ready.
        assert archive_db.record_rate_limit(
            "kick", "http_403", surface="metadata", origin="auto")

        conn = archive_db.get_conn()
        assert "rate_limit_events" in _tables(conn)
        # Content is untouched — this is the "do not make the user recreate
        # a 485MB archive" requirement.
        assert archive_db.video_channel("twitch", "old-vid") == "chan"
        assert archive_db.count_messages("twitch", "old-vid") == 1
        assert len(archive_db.transcript_for("twitch", "old-vid")) == 1
        assert len(archive_db.recent_rate_limits(platform="kick")) == 1
    finally:
        if prev is None:
            os.environ.pop("VODRIP_ARCHIVE_DB", None)
        else:
            os.environ["VODRIP_ARCHIVE_DB"] = prev
        archive_db._conn = None
        archive_db._schema_ready = False


# --- 4. running the migration twice is safe --------------------------------

def test_two_opens_of_the_same_old_db_are_both_safe(tmp_path):
    """The app may open one archive from several processes. Run the whole
    open+migrate cycle twice on one file and require both to succeed, with
    the first run's rows intact."""
    old_db = tmp_path / "twice.db"
    _make_old_db(old_db)
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(old_db)
    try:
        for attempt in (1, 2):
            archive_db._conn = None
            archive_db._schema_ready = False
            assert archive_db.record_rate_limit(
                "youtube", "http_429", surface="metadata", origin="auto")
            assert "rate_limit_events" in _tables(archive_db.get_conn()), (
                f"open #{attempt} did not leave the table in place")
            assert archive_db.video_channel("twitch", "old-vid") == "chan"
        assert len(archive_db.recent_rate_limits(platform="youtube")) == 2, (
            "the second open must not have dropped the first run's event")
    finally:
        if prev is None:
            os.environ.pop("VODRIP_ARCHIVE_DB", None)
        else:
            os.environ["VODRIP_ARCHIVE_DB"] = prev
        archive_db._conn = None
        archive_db._schema_ready = False


def test_concurrent_opens_of_one_old_db_do_not_collide(tmp_path):
    """Two connections, same file, both migrating: CREATE TABLE IF NOT EXISTS
    plus the PRAGMA guard must let both through."""
    old_db = tmp_path / "race.db"
    _make_old_db(old_db)
    a = sqlite3.connect(old_db, timeout=10.0)
    b = sqlite3.connect(old_db, timeout=10.0)
    try:
        archive_db._ensure_rate_limit_events(conn=a)
        archive_db._ensure_rate_limit_events(conn=b)
        a.commit()
        b.commit()
        assert "rate_limit_events" in _tables(a)
        assert "rate_limit_events" in _tables(b)
    finally:
        a.close()
        b.close()


# --- 5. the point of the table: the governor then learns --------------------

def test_prime_from_history_learns_once_events_exist(tmp_path):
    """After the migration, recorded events must move the ceiling off its
    cold-start default. This is the assertion that a missing table makes
    impossible — prime_from_history() returns {} and every platform sits at
    its default forever, with nothing logged above debug."""
    db = tmp_path / "learn.db"
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(db)
    archive_db._conn = None
    archive_db._schema_ready = False
    try:
        rate_budget.reset()
        with archive_db._lock:
            conn = archive_db.get_conn()
            conn.executemany(
                "INSERT INTO rate_limit_events (ts, platform, surface, kind,"
                " origin, recent_requests, in_flight, context, backoff_s)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                [(archive_db._now_iso(), "youtube", "captions", "bot_gate",
                  "auto", 10, 1, "learned", 1800.0)] * 3,
            )
            conn.commit()

        default = rate_budget.platform_status("youtube")["default_ceiling_rpm"]
        assert rate_budget.platform_status("youtube")["ceiling_rpm"] == default, (
            "precondition: a fresh governor sits on its cold-start default")

        applied = rate_budget.prime_from_history()
        assert applied, (
            "prime_from_history() returned nothing — history was not readable")
        after = rate_budget.platform_status("youtube")
        assert after["ceiling_rpm"] < default, (
            f"still on the cold-start default ({default}) after priming")
        assert after["learning"]["min_trip_rpm"] > 0, (
            "the observed trip rate must be retained as what was learned")
    finally:
        rate_budget.reset()
        if prev is None:
            os.environ.pop("VODRIP_ARCHIVE_DB", None)
        else:
            os.environ["VODRIP_ARCHIVE_DB"] = prev
        archive_db._conn = None
        archive_db._schema_ready = False


def test_prime_from_history_on_an_empty_archive_is_harmless(tmp_path):
    """No events is a legitimate state (a platform that never got limited):
    priming must be a no-op, not an error and not a fabricated ceiling."""
    db = tmp_path / "empty.db"
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(db)
    archive_db._conn = None
    archive_db._schema_ready = False
    try:
        rate_budget.reset()
        assert "rate_limit_events" in _tables(archive_db.get_conn()), (
            "even an archive with no events needs the table to exist")
        assert rate_budget.prime_from_history() == {}
        assert rate_budget.platform_status("youtube")["ceiling_rpm"] == \
            rate_budget.platform_status("youtube")["default_ceiling_rpm"]
    finally:
        rate_budget.reset()
        if prev is None:
            os.environ.pop("VODRIP_ARCHIVE_DB", None)
        else:
            os.environ["VODRIP_ARCHIVE_DB"] = prev
        archive_db._conn = None
        archive_db._schema_ready = False
