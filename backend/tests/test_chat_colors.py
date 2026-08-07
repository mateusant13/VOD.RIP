"""Chat username-color capture (messages.color) — migration + ingest + read.

Real-path guard: the preview panel chat payload and archive search chat
window both surface messages.color; Twitch rows stay NULL (GQL VOD comments
carry no color) and the UI falls back to a deterministic palette.
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# Env isolation: point the archive DB at a scratch file BEFORE the first
# import of services.archive_db (module-level connection caches at import).
_TMP = Path(tempfile.mkdtemp(prefix="vodrip-chatcolor-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")
os.environ["VODRIP_APP_DATA"] = str(_TMP / "appdata")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import archive_db
from services.archive_ytdlp import _parse_live_chat


@pytest.fixture(autouse=True)
def _isolated_db():
    """Fresh random DB path per test.

    The module connection cache is path-keyed: pointing VODRIP_ARCHIVE_DB at
    a NEW path closes the previous connection and opens the new one (never
    close it manually — a closed non-None conn never reopens)."""
    os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / f"archive-{os.urandom(4).hex()}.db")
    yield


def _cols() -> set:
    conn = sqlite3.connect(os.environ["VODRIP_ARCHIVE_DB"])
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    finally:
        conn.close()


def test_fresh_db_has_color_column():
    # Touch the DB so migrations run.
    archive_db.query("SELECT 1 FROM messages LIMIT 1")
    assert "color" in _cols()


def test_legacy_db_gets_color_column():
    # Simulate a pre-color DB: the real SCHEMA with the color line removed,
    # created BEFORE the module touches this path (so its _schema_ready flag
    # is False and the _ensure_* chain runs, adding only the color column).
    legacy = archive_db.SCHEMA.replace("  color      TEXT,\r\n", "").replace("  color      TEXT,\n", "")
    assert "color      TEXT" not in legacy and "spam_count" in legacy
    conn = sqlite3.connect(os.environ["VODRIP_ARCHIVE_DB"])
    conn.executescript(legacy)
    conn.commit()
    conn.close()
    archive_db.get_conn()  # runs migrations -> adds messages.color
    assert "color" in _cols()


def test_insert_persists_color():
    archive_db.insert_messages("youtube", "v1", [
        {"offset_sec": 1.0, "username": "Alice", "text": "oi", "color": "#FF0033"},
        {"offset_sec": 2.0, "username": "Bob", "text": "olá"},
    ])
    rows = archive_db.chat_for("youtube", "v1")
    assert rows[0]["color"] == "#FF0033"
    assert rows[1]["color"] is None


def test_spam_collapse_keeps_latest_color():
    archive_db.insert_messages("youtube", "v1", [
        {"offset_sec": 1.0, "username": "Alice", "text": "gg", "color": "#FF0033"},
    ])
    # Continuation flush: same user/text within 60s, NEW color wins; NULL keeps old.
    archive_db.insert_messages("youtube", "v1", [
        {"offset_sec": 30.0, "username": "Alice", "text": "gg", "color": "#00D24D"},
    ])
    archive_db.insert_messages("youtube", "v1", [
        {"offset_sec": 31.0, "username": "Alice", "text": "gg"},  # no color -> keep
    ])
    rows = archive_db.chat_for("youtube", "v1")
    assert len(rows) == 1
    assert rows[0]["spam_count"] == 3
    assert rows[0]["color"] == "#00D24D"


_NDJSON_SAMPLE = """{"replayChatItemAction":{"videoOffsetTimeMsec":"12345","actions":[{"addChatItemAction":{"item":{"liveChatTextMessageRenderer":{"authorName":{"simpleText":"Viewer1"},"authorNameTextColor":"#FF0033","authorExternalChannelId":"UC123","message":{"runs":[{"text":"hello"}]},"timestampUsec":"1700000000000000"}}}}]}}
{"replayChatItemAction":{"videoOffsetTimeMsec":"22345","actions":[{"addChatItemAction":{"item":{"liveChatTextMessageRenderer":{"authorName":{"simpleText":"Viewer2"},"authorExternalChannelId":"UC456","message":{"runs":[{"text":"hi"}]},"timestampUsec":"1700000001000000"}}}}]}}
"""


def test_ytdlp_parse_captures_color():
    rows = _parse_live_chat(_NDJSON_SAMPLE)
    assert len(rows) == 2
    assert rows[0]["color"] == "#FF0033"
    assert rows[0]["username"] == "Viewer1"
    # No authorNameTextColor -> NULL, not garbage.
    assert rows[1]["color"] is None


def test_ytdlp_parse_rejects_malformed_color():
    bad = _NDJSON_SAMPLE.replace("#FF0033", "notacolor")
    rows = _parse_live_chat(bad)
    assert rows[0]["color"] is None


def test_chat_window_includes_color():
    archive_db.insert_messages("youtube", "v1", [
        {"offset_sec": 5.0, "username": "Alice", "text": "oi", "color": "#0057E7"},
    ])
    rows, truncated = archive_db.chat_window("youtube", "v1", 10.0)
    assert rows[0]["color"] == "#0057E7"
    assert truncated is False


def test_chat_window_from_offset_mode():
    """half <= 0 → the whole history from offset_sec onward, time-ordered."""
    archive_db.insert_messages("youtube", "v2", [
        {"offset_sec": 10.0, "username": "a", "text": "before"},
        {"offset_sec": 20.0, "username": "b", "text": "hit"},
        {"offset_sec": 20.0, "username": "c", "text": "same second"},
        {"offset_sec": 35.0, "username": "d", "text": "later"},
        {"offset_sec": 60.0, "username": "e", "text": "end"},
    ])
    rows, truncated = archive_db.chat_window("youtube", "v2", 20.0, half=0)
    assert truncated is False
    assert [r["offset_sec"] for r in rows] == [20.0, 20.0, 35.0, 60.0]
    assert rows[0]["text"] == "hit"

    # The ±half mode is unchanged (BETWEEN 25±30 → rows 10..55; 60 excluded;
    # cap, truncated False).
    rows2, truncated2 = archive_db.chat_window("youtube", "v2", 25.0)
    assert truncated2 is False
    assert [r["offset_sec"] for r in rows2] == [10.0, 20.0, 20.0, 35.0]


def test_chat_window_from_offset_truncates_at_cap():
    """A history longer than the from-offset cap reports truncated=True and
    returns exactly the first CHAT_FROM_OFFSET_LIMIT rows."""
    total = archive_db.CHAT_FROM_OFFSET_LIMIT + 1
    archive_db.insert_messages("youtube", "v3", [
        {"offset_sec": float(i), "username": f"u{i}", "text": f"msg {i}"}
        for i in range(total)
    ])
    rows, truncated = archive_db.chat_window("youtube", "v3", 0.0, half=0)
    assert truncated is True
    assert len(rows) == archive_db.CHAT_FROM_OFFSET_LIMIT
    assert rows[0]["offset_sec"] == 0.0
    assert rows[-1]["offset_sec"] == float(archive_db.CHAT_FROM_OFFSET_LIMIT - 1)

    # Exactly-at-cap (offset 1.0 → rows 1..5000, no tail left) stays
    # untruncated; only a longer tail reports it.
    rows2, truncated2 = archive_db.chat_window("youtube", "v3", 1.0, half=0)
    assert truncated2 is False
    assert len(rows2) == archive_db.CHAT_FROM_OFFSET_LIMIT


def test_chat_window_limit_paginates_without_gaps_or_duplicates():
    """The from-offset mode accepts an explicit `limit` and a truncated tail
    is fully loadable by re-fetching from the last row's offset_sec: the
    inclusive >= boundary re-includes equal-offset rows (the client dedupes
    them), so paging never skips a mid-run boundary or re-drops rows."""
    archive_db.insert_messages("youtube", "v9", [
        {"offset_sec": float(i), "username": f"u{i}", "text": f"msg {i}"}
        for i in range(120)
    ])
    page1, truncated1 = archive_db.chat_window("youtube", "v9", 0.0, half=0, limit=50)
    assert truncated1 is True
    assert len(page1) == 50
    assert page1[0]["offset_sec"] == 0.0

    page2, truncated2 = archive_db.chat_window(
        "youtube", "v9", page1[-1]["offset_sec"], half=0, limit=50
    )
    assert truncated2 is True
    assert len(page2) == 50
    # Boundary row re-included (inclusive >=): the client dedupes it.
    assert page2[0]["offset_sec"] == page1[-1]["offset_sec"]

    page3, truncated3 = archive_db.chat_window(
        "youtube", "v9", page2[-1]["offset_sec"], half=0, limit=50
    )
    assert truncated3 is False
    assert len(page3) == 22  # rows 98..119 (98/99 re-included from page 2)

    # Page 1 + the non-boundary tail of pages 2/3 cover the history exactly once.
    all_offsets = [r["offset_sec"] for r in page1 + page2[1:] + page3[1:]]
    assert all_offsets == [float(i) for i in range(120)]

    # The +1 probe survives an explicit limit: exactly-at-cap stays clean
    # (offset 70 → rows 70..119 = exactly 50 rows).
    page_exact, truncated_exact = archive_db.chat_window(
        "youtube", "v9", 70.0, half=0, limit=50
    )
    assert truncated_exact is False
    assert len(page_exact) == 50

    # limit is clamped to >= 1 (a 0/negative page is nonsense, not a crash).
    tiny, tiny_trunc = archive_db.chat_window("youtube", "v9", 0.0, half=0, limit=0)
    assert len(tiny) == 1
    assert tiny_trunc is True


def test_chat_window_limit_respects_same_offset_runs():
    """A page boundary cutting through a run of messages sharing one
    offset_sec must not lose the run's tail: the next page re-fetches from
    the boundary offset and the run's remaining rows are all present."""
    # 3 rows at 5.0, 3 rows at 10.0, then spaced rows — cap 4 cuts inside
    # the 10.0 run (its rows straddle the page boundary).
    rows = [{"offset_sec": 5.0, "username": f"u{i}", "text": f"five {i}"} for i in range(3)]
    rows += [{"offset_sec": 10.0, "username": f"u{i}", "text": f"ten {i}"} for i in range(3)]
    rows += [{"offset_sec": float(20 + i), "username": f"u{i}", "text": f"later {i}"} for i in range(10)]
    archive_db.insert_messages("youtube", "v10", rows)
    page1, truncated1 = archive_db.chat_window("youtube", "v10", 0.0, half=0, limit=4)
    assert truncated1 is True
    assert [r["offset_sec"] for r in page1] == [5.0, 5.0, 5.0, 10.0]
    page2, truncated2 = archive_db.chat_window(
        "youtube", "v10", page1[-1]["offset_sec"], half=0, limit=4
    )
    assert truncated2 is True
    # Boundary row re-included (the client dedupes it); the 10.0 run's
    # remaining two rows come back — the tail is never skipped — and the
    # page continues past the run.
    assert [r["offset_sec"] for r in page2] == [10.0, 10.0, 10.0, 20.0]
    # Page 1 + page 2's non-boundary rows cover the timeline exactly once.
    all_offsets = [r["offset_sec"] for r in page1 + page2[1:]]
    assert all_offsets == [5.0, 5.0, 5.0, 10.0, 10.0, 10.0, 20.0]
