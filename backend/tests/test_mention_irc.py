"""Mention-IRC logger integration test — word-boundary matching + persist path.

The env var MUST be set before the first services.archive_db import anywhere
in the pytest session; this module sets it at module top (before imports),
binding the global connection to the temp DB.

Run from backend/: python -m pytest tests/test_mention_irc.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ["VODRIP_ARCHIVE_DB"] = str(
    Path(tempfile.mkdtemp(prefix="mention-test-")) / "archive.db")

import pytest  # noqa: E402

from services import archive_db  # noqa: E402  (env must be set first)
from services import mention_irc  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _mention_scratch_db():
    """Rebind the shared archive conn to THIS module's scratch DB at module
    start (collection-order independent), and drop it after so the next
    module rebinds fresh."""
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(
        Path(tempfile.mkdtemp(prefix="mention-test-")) / "archive.db")
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    if prev is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev
    archive_db._conn = None
    archive_db._schema_ready = False


NAMES = ["titiltei", "guiven", "srdogg", "mandiocaa", "arthur lanches", "gaveta"]


def test_word_boundary_exact_mentions() -> None:
    pat = mention_irc._mention_pattern(NAMES)
    assert pat is not None
    # exact whole-word matches hit
    assert pat.search("oi guiven tudo bem")
    assert pat.search("srdogg é gigante")
    assert pat.search("vai mandiocaa hoje")
    assert pat.search("assistindo o titiltei agora")
    # multi-word channel name hits as the whole phrase
    assert pat.search("arthur lanches voltou")
    # partial/embedded words MUST NOT match (mandiocaa != mandioca, srdogg != srdoggs)
    assert not pat.search("essa mandioca está podre")
    assert not pat.search("srdoggs hoje não")
    assert not pat.search("titilteiabc")
    assert not pat.search("xguiven")
    assert not pat.search("arthur lanche bom")


def test_persist_writes_numeric_offset_row() -> None:
    """A PRIVMSG row (offset_sec None from parse_privmsg) persists with a
    numeric wall-clock offset and is searchable back via messages FTS."""
    pat = mention_irc._mention_pattern(NAMES)
    row = {
        "offset_sec": None,
        "user_id": "123",
        "username": "viewer1",
        "text": "o guiven é muito bom",
        "badges": [],
        "emotes": [],
        "ts": "2026-08-12T20:00:00Z",
    }
    assert pat is not None and pat.search(row["text"])
    mention_irc._persist("titiltei", row, NAMES)

    vid = "mention-titiltei"
    rows, _ = archive_db.chat_window("twitch", vid, offset_sec=0.0, half=0.0, limit=20)
    assert len(rows) == 1, f"expected 1 mention row, got {len(rows)}"
    got = rows[0]
    assert got["text"] == "o guiven é muito bom"
    assert got["username"] == "viewer1"
    assert isinstance(got["offset_sec"], float) and got["offset_sec"] > 0

    # non-mention rows are dropped by _persist
    mention_irc._persist("titiltei", {
        "offset_sec": None,
        "username": "viewer2",
        "text": "sem menção nenhuma aqui",
        "badges": [],
        "emotes": [],
    }, NAMES)
    rows, _ = archive_db.chat_window("twitch", vid, offset_sec=0.0, half=0.0, limit=20)
    assert len(rows) == 1, "non-mention row must not persist"

    # searchable fallback: FTS finds the mention text under the video id
    hits = archive_db.search("guiven", source="chat", video_id=vid, limit=5)
    assert any(h["video_id"] == vid and "guiven" in (h.get("snippet") or h.get("text") or "")
               for h in hits), "mention row must be FTS-searchable"


def test_no_names_returns_none_pattern() -> None:
    assert mention_irc._mention_pattern([]) is not None  # never-matching pattern
