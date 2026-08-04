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
    rows = archive_db.chat_window("youtube", "v1", 10.0)
    assert rows[0]["color"] == "#0057E7"
