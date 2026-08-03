"""insert_messages spam collapse — merge rule, cross-flush, idempotency.

The env var MUST be set before the first services.archive_db import anywhere
in the session; this module is the only importer of its own scratch DB, so it
is set at module top (before `from services import archive_db`), binding the
global connection to a temp DB.

Run from backend/: python -m pytest tests/test_archive_spam_collapse.py -q
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="archive-spam-collapse-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")

import pytest  # noqa: E402

from services import archive_db  # noqa: E402

PLATFORM = "twitch"
VIDEO = "__spam_collapse__"


@pytest.fixture(autouse=True)
def _scrub_video():
    """Each test starts with an empty messages table for VIDEO."""
    archive_db.execute("DELETE FROM messages WHERE video_id=?", (VIDEO,))
    yield


def _spam_rows(n: int, start: float = 100.0, step: float = 0.5) -> list[dict]:
    return [
        {"offset_sec": start + i * step, "username": "spammer", "text": "SPAM SPAM"}
        for i in range(n)
    ]


def _counts() -> list[int]:
    rows = archive_db.query(
        "SELECT spam_count FROM messages WHERE platform=? AND video_id=? "
        "ORDER BY offset_sec, id",
        (PLATFORM, VIDEO),
    )
    return [int(r["spam_count"]) for r in rows]


def _offsets() -> list[float]:
    rows = archive_db.query(
        "SELECT offset_sec FROM messages WHERE platform=? AND video_id=? "
        "ORDER BY offset_sec, id",
        (PLATFORM, VIDEO),
    )
    return [float(r["offset_sec"]) for r in rows]


def test_collapse_within_batch() -> None:
    accepted = archive_db.insert_messages(PLATFORM, VIDEO, _spam_rows(50))
    assert accepted == 50, "accepted count must include collapsed rows"
    assert _counts() == [50], "50 identical rows must collapse to one stored row"
    assert _offsets() == [100.0], "anchor keeps the first row's offset"


def test_collapse_across_flushes() -> None:
    archive_db.insert_messages(PLATFORM, VIDEO, _spam_rows(3, start=10.0))
    accepted = archive_db.insert_messages(PLATFORM, VIDEO, _spam_rows(1, start=12.0))
    assert accepted == 1, "cross-flush continuation row must be accepted"
    assert _counts() == [4], "cross-flush identical row must merge into the stored row"


def test_collapse_idempotent_resend() -> None:
    """Re-sending the merged row (same offset, identical text) is consumed
    without bumping spam_count — replaying a flush never double-counts."""
    archive_db.insert_messages(PLATFORM, VIDEO, _spam_rows(3, start=20.0))
    archive_db.insert_messages(PLATFORM, VIDEO, _spam_rows(1, start=22.0))
    assert _counts() == [4]
    accepted = archive_db.insert_messages(PLATFORM, VIDEO, _spam_rows(1, start=22.0))
    assert accepted == 1, "re-sent row must still be accepted"
    assert _counts() == [4], "re-sent row must not double-merge"


def test_collapse_distinct_text_does_not_merge() -> None:
    archive_db.insert_messages(PLATFORM, VIDEO, _spam_rows(2, start=30.0))
    accepted = archive_db.insert_messages(
        PLATFORM, VIDEO,
        [{"offset_sec": 31.0, "username": "spammer", "text": "different"}],
    )
    assert accepted == 1
    assert _counts() == [2, 1], "different text must start a new row"


def test_collapse_outside_window_does_not_merge() -> None:
    archive_db.insert_messages(PLATFORM, VIDEO, _spam_rows(2, start=40.0))
    accepted = archive_db.insert_messages(PLATFORM, VIDEO, _spam_rows(1, start=101.0))
    assert accepted == 1
    assert _counts() == [2, 1], "identical text beyond 60 s must start a new row"


def test_collapse_within_batch_crosses_flush_anchor() -> None:
    """A whole-batch re-send collapses to the anchor; if the stored row's
    offset equals the anchor's, the re-send is consumed without a merge."""
    archive_db.insert_messages(PLATFORM, VIDEO, _spam_rows(50, start=50.0))
    assert _counts() == [50]
    accepted = archive_db.insert_messages(PLATFORM, VIDEO, _spam_rows(50, start=50.0))
    assert accepted == 50
    assert _counts() == [50], "re-sent batch must not double-merge"


def test_collapse_multiple_runs_one_batch() -> None:
    """A batch mixing two spam bursts collapses each run separately."""
    rows = _spam_rows(3, start=60.0) + [
        {"offset_sec": 61.0, "username": "bot2", "text": "GG GO NEXT"},
        {"offset_sec": 61.5, "username": "bot2", "text": "GG GO NEXT"},
    ]
    accepted = archive_db.insert_messages(PLATFORM, VIDEO, rows)
    assert accepted == 5
    assert _counts() == [3, 2], "each identical run must collapse to one row"
