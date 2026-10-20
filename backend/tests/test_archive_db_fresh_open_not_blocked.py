"""A fresh archive open must never wait on somebody else's reader.

``_init_schema`` checkpoints the -wal on a fresh open so a ``-wal`` left behind
by a killed process gets folded back into the main file. It used to do that
with ``PRAGMA wal_checkpoint(TRUNCATE)``, and TRUNCATE is the one mode that
requires EVERY reader to have drained first.

A live archive always has a reader - the app's own worker thread, a second
VOD.RIP process, or a read-only probe - so the checkpoint spent the whole
busy_timeout (10 s) waiting and then gave up. Measured 2026-10-20 against the
live ``H:\\VOD.RIP-data\\archive.db``::

    PRAGMA wal_checkpoint(TRUNCATE)      10914 ms  (1, 209, 0)

``busy=1`` means the checkpoint did not run at all: eleven seconds of boot
cost, zero frames moved. That is why ``GET /api/channel/outcome-parks`` took
>60 s from the UI and the channels surface rendered "Could not read the
learned channel state" on a healthy backend.

The fix is PASSIVE, which takes whatever no reader is pinning and returns at
once. This test holds a real read transaction open on a second connection and
proves a fresh open still completes promptly - the exact shape that used to
hang. It fails against TRUNCATE with the same 10 s wait it caused in
production.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from services import archive_db


@pytest.fixture()
def scratch_archive(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(db_path))
    archive_db.close_connections()
    yield db_path
    archive_db.close_connections()


def test_fresh_open_is_not_blocked_by_a_reader_holding_the_wal(scratch_archive):
    # 1. Build a real archive through the module's own open path.
    archive_db.query("SELECT 1")
    archive_db.execute(
        "CREATE TABLE IF NOT EXISTS t_pin (id INTEGER PRIMARY KEY, blob BLOB)"
    )
    archive_db.execute("INSERT INTO t_pin (blob) VALUES (?)", (b"x" * 4096,))

    # 2. A SECOND connection opens a read transaction and stays in it. This is
    #    the live reader that made TRUNCATE park for the whole busy timeout.
    reader = sqlite3.connect(str(scratch_archive), timeout=1.0)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM t_pin").fetchone()

    try:
        # 3. Drop every connection the module holds and force a fresh open.
        archive_db.close_connections()

        started = time.perf_counter()
        rows = archive_db.query("SELECT COUNT(*) AS n FROM t_pin")
        elapsed = time.perf_counter() - started

        assert rows[0]["n"] == 1, "the read still has to work"
        # The old TRUNCATE path spent the full 10 s busy_timeout here. 3 s is a
        # generous ceiling that no correct implementation can approach.
        assert elapsed < 3.0, (
            f"fresh open blocked for {elapsed:.1f}s behind a live reader - "
            "the startup checkpoint must be PASSIVE, not TRUNCATE"
        )
    finally:
        reader.rollback()
        reader.close()
