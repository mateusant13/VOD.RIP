"""Common-search robustness: quoted/NUL queries never 500, the span and
title passes are bounded (long queries return instead of hanging), the
bigram index is loaded once per search, and partial words of >= 7 chars
reach chat/transcript content via native FTS5 prefix matching.

Regressions covered:
- 'a"b' / '"' built a malformed FTS5 MATCH pattern -> sqlite3.OperationalError
  propagated out of search() (a 500). NUL bytes truncated the pattern the
  same way.
- A 2000-token query hung the span pass for minutes (per-token expansion
  reloaded the bigram index + a O(tokens^2) split loop) and the title pass
  for ~4 min (O(q_tokens x videos x title_tokens)).
- 'estranh' (7 chars, absent from the corpus) reached titles but never
  chat/transcript content — the Damerau bridge stops at 1-2 edits.

Run from backend/: python -m pytest tests/test_archive_search_robust.py
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="archive-robust-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")

from services import archive_db as db  # noqa: E402


@pytest.fixture()
def scratch(monkeypatch, tmp_path):
    """ISOLATED corpus (an existing DB file suppresses the real-archive
    copy in _migrate_db_to_data_dir, so assertions are deterministic):
    one video whose rows carry the exact-phrase word, an ASR-neighbor word,
    accents/apostrophes and an embedded-quote message."""
    db_path = tmp_path / "archive.db"
    seed = sqlite3.connect(str(db_path))
    seed.execute("CREATE TABLE IF NOT EXISTS _seed (x INTEGER)")
    seed.commit()
    seed.close()
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(db_path))
    db.upsert_video(
        {
            "platform": "twitch",
            "video_id": "rob1",
            "channel": "gaveta",
            "title": "Vale da Estranheza",
            "started_at": "2026-01-01T10:00:00Z",
            "kind": "vod",
        }
    )
    db.insert_messages(
        "twitch",
        "rob1",
        [
            {"offset_sec": 10, "username": "alguem", "text": "a estranheza chegou de novo"},
            {"offset_sec": 20, "username": "alguem", "text": "catarina manda bem"},
            {"offset_sec": 30, "username": "alguem", "text": "don't stop the café"},
            {"offset_sec": 40, "username": "alguem", "text": 'ele disse "oi" de novo'},
        ],
    )
    db.insert_transcript(
        "twitch",
        "rob1",
        [
            {
                "seg_idx": 0,
                "start_sec": 0,
                "end_sec": 5,
                "text": "bem vindo ao vale da estranheza",
                "words": [],
            }
        ],
        lang="pt",
    )
    # Prime the vocab synchronously: the request path serves cold searches
    # with exact tokens and rebuilds in the background, but these tests
    # assert the fuzzy/prefix contracts that need a warm vocab.
    import time as _t

    for t in ("transcripts", "messages"):
        db._load_vocab_uncached(t, _t.monotonic())
    return db


def _content_texts(hits) -> list[str]:
    return [
        (h.get("text") or "").lower()
        for h in hits
        if h.get("kind") in ("transcript", "message")
    ]


# --- quotes / NUL never crash ---------------------------------------------

def test_embedded_quote_never_crashes(scratch):
    # 'a"b' used to raise sqlite3.OperationalError: unterminated string.
    for q in ('a"b', '"', 'foo"bar baz', 'estranheza"', 'x"y"z', '""'):
        hits = db.search(q, limit=10)
        assert isinstance(hits, list), q


def test_nul_byte_never_crashes(scratch):
    assert db.search("\x00", limit=10) == []
    hits = db.search("estranheza\x00", limit=10)
    assert hits, "NUL sanitized to a separator; the word still matches"


def test_quote_pattern_still_matches_literally(scratch):
    # The escaped pattern must match, not just fail to crash: the phrase
    # pass and the AND pass both find the row with the quoted message.
    hits = db.search('disse "oi"', limit=10)
    assert any('disse "oi"' in (h.get("text") or "") for h in hits)


# --- long queries return promptly -----------------------------------------

def test_long_query_returns_bounded(scratch):
    # A 2000-token query used to hang the span pass for minutes and the
    # title pass for ~4 min. It must return (quickly) with a list.
    q = ("word " * 2000).strip()
    hits = db.search(q, limit=5)
    assert isinstance(hits, list)


def test_span_pass_capped_at_max_tokens(scratch):
    tokens = [f"tok{i}" for i in range(10)]
    assert len(tokens) > db._SPAN_MAX_TOKENS
    assert (
        db._phrase_span_rows(
            tokens, 10, span_variants={}, platforms=[], video_id=None,
            channel=None, kinds=[], date_from=None, date_to=None, lang=None,
        )
        == []
    )


def test_span_variants_single_bigram_load(scratch, monkeypatch):
    # The span-variant map used to call _expand_query per token, reloading
    # the bigram index (2 COUNT(*) on million-row tables) for each one —
    # an N-token query cost N x ~1s on the real archive. Now the whole
    # query shares ONE bigram load (plus the one inside _fuzzy_pattern).
    calls: list[list[str]] = []
    orig = db._load_bigrams

    def counting(tables):
        calls.append(tables)
        return orig(tables)

    monkeypatch.setattr(db, "_load_bigrams", counting)
    db.search("vale da estranheza hoje", limit=10)
    assert len(calls) <= 2, f"bigram index reloaded {len(calls)}x for one search"


def test_title_pass_bounded_tokens(scratch):
    # _titles_search iterates query tokens x videos x title tokens; the
    # processed tokens are capped at _TITLES_MAX_TOKENS so a huge query
    # cannot blow up the pass, while the score denominator stays the full
    # count (a title matching only the first few tokens stays noise).
    q = ("estranheza " * 100).strip()
    rows = db._titles_search(
        q, 10, q_freq={}, platforms=[], video_id=None, channel=None,
        kinds=[], date_from=None, date_to=None,
    )
    assert isinstance(rows, list)


# --- native prefix reach for partial words --------------------------------

def test_long_partial_word_reaches_content(scratch):
    # 'estranh' (7 chars, absent from the corpus) must find 'estranheza'
    # in chat/transcript content — before the fix only the title pass
    # reached it (its substring gate >= 4 chars).
    hits = db.search("estranh", limit=20)
    texts = _content_texts(hits)
    assert texts, "content rows must surface for a long partial word"
    assert any("estranheza" in t for t in texts), texts


def test_present_token_prefix_sits_at_tier1(scratch):
    # 'vale' IS in the corpus (complete word) -> its prefix term must sit
    # at tier 1, never tier 0 (the valendo/valeu flood regression). An
    # absent token ('estranh') is a partial word -> tier 0.
    pat = db._fuzzy_pattern("vale", ["transcripts", "messages"])
    assert pat is not None
    assert '"vale"' in pat.get(0, "")
    assert '"vale"*' not in pat.get(0, ""), "present token must not prefix at tier 0"
    assert '"vale"*' in pat.get(1, ""), "present token prefix reach sits at tier 1"
    pat = db._fuzzy_pattern("estranh", ["transcripts", "messages"])
    assert pat is not None
    assert '"estranh"*' in pat.get(0, ""), "absent token prefix reach sits at tier 0"


# --- accents / apostrophes (PT-EN mixed text) -----------------------------

def test_accent_and_apostrophe_queries(scratch):
    hits = db.search("café", limit=10)
    assert any("café" in (h.get("text") or "").lower() for h in hits)
    hits = db.search("don't stop", limit=10)
    assert any("don't" in (h.get("text") or "").lower() for h in hits)


# --- router bounds --------------------------------------------------------

async def test_router_rejects_oversized_query():
    from fastapi import HTTPException
    from routers.archive import archive_search

    with pytest.raises(HTTPException) as exc:
        await archive_search(q="a" * 501, limit=20)
    assert exc.value.status_code == 400
    assert "too long" in exc.value.detail
