"""archive_db.search() result-cache contract.

search() memoises the FINAL hit list per (db path, content generation, every
argument). These tests pin the three properties that make that safe:

  1. an identical repeat call is served from the cache (no re-run);
  2. ANY write to searchable content retires the entry — inserts AND the
     delete/update paths, which is why invalidation rides the execute() write
     funnel rather than a hand-picked list of _bump_content_ref call sites;
  3. the cache is bounded, and the inputs that change the RESULT (not just
     how it is reported) are part of the key.

The two traps found while building this, both pinned here:
  - the implicit channel-hint pass (enabled by passing _channel_hint_out)
    rewrites q and applies a channel filter, so hint=True and hint=False are
    different searches, not the same search reported differently;
  - a semantic (embedding) search depends on the embed model's fingerprint,
    which changes with no DB write, so semantic results are never memoised.

Run from backend/: python -m pytest tests/test_archive_search_cache.py
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="vodrip-archive-search-cache-"))
_DB = _TMP / "archive.db"
sqlite3.connect(str(_DB)).close()

# Before the first services.archive_db import (module-level self-check opens
# the DB on import); conftest.py guarantees the real archive is never hit.
os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)

from services import archive_db  # noqa: E402

WORD = "cachechip"
CHAN = "cachechipchan"
VID = "cache-vid"


@pytest.fixture(scope="module", autouse=True)
def _cache_scratch_db():
    """Rebind the global connection to THIS module's scratch DB regardless of
    import/collection order (later modules clobber the env var at import)."""
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


@pytest.fixture
def seed():
    """Wipe and re-insert this module's single video. Idempotent."""
    archive_db.execute("DELETE FROM messages WHERE video_id=?", (VID,))
    archive_db.execute("DELETE FROM transcripts WHERE video_id=?", (VID,))
    archive_db.execute("DELETE FROM videos WHERE video_id=?", (VID,))
    archive_db.upsert_video({
        "platform": "twitch",
        "video_id": VID,
        "channel": CHAN,
        "title": f"{WORD} title",
        "started_at": "2026-08-01T12:00:00Z",
        "kind": "vod",
    })
    archive_db.insert_transcript("twitch", VID, [{
        "seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": f"first {WORD}",
    }])
    return VID


@pytest.fixture
def counted(monkeypatch):
    """Count real search executions (cache misses) for the wrapped call."""
    calls: list[dict] = []
    real = archive_db._search_uncached

    def wrapper(*args, **kwargs):
        calls.append(dict(kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(archive_db, "_search_uncached", wrapper)
    return calls


def _texts(hits):
    return sorted(h["text"] for h in hits)


# --- 1. memoisation -------------------------------------------------------

def test_repeat_query_is_served_from_cache(seed, counted):
    a = archive_db.search(WORD)
    assert a, "seed must match"
    b = archive_db.search(WORD)
    assert _texts(a) == _texts(b)
    assert len(counted) == 1, "second identical search must not re-run"


def test_different_limit_is_a_different_key(seed, counted):
    archive_db.search(WORD, limit=5)
    archive_db.search(WORD, limit=50)
    assert len(counted) == 2, "limit changes the result set, so it is in the key"


def test_channel_hint_flag_is_part_of_the_key(seed, counted):
    """hint=True auto-scopes to the channel and rewrites q — a DIFFERENT
    search. A cache that only replayed the hint would serve the scoped hits
    to a hint=False (UI-dismissed) call."""
    archive_db.search(f"{CHAN} {WORD}", _channel_hint_out=[])
    archive_db.search(f"{CHAN} {WORD}")
    assert len(counted) == 2, "hint on/off must not share a cache entry"


def test_hint_is_replayed_on_a_cache_hit(seed, counted):
    box_a: list[str] = []
    archive_db.search(f"{CHAN} {WORD}", _channel_hint_out=box_a)
    assert box_a == [CHAN], "seed query must auto-scope to the channel"
    box_b: list[str] = []
    archive_db.search(f"{CHAN} {WORD}", _channel_hint_out=box_b)
    assert box_b == [CHAN], "a cache hit must replay the hint out-param"
    assert len(counted) == 1


# --- 2. invalidation on write --------------------------------------------

def test_transcript_insert_invalidates(seed, counted):
    before = _texts(archive_db.search(WORD))
    assert len(counted) == 1
    archive_db.insert_transcript("twitch", VID, [{
        "seg_idx": 1, "start_sec": 2.0, "end_sec": 3.0, "text": f"second {WORD}",
    }])
    after = _texts(archive_db.search(WORD))
    assert len(counted) == 2, "a content write must retire the entry"
    assert len(after) == len(before) + 1, "the new segment must be visible"


def test_transcript_delete_invalidates(seed, counted):
    archive_db.insert_transcript("twitch", VID, [{
        "seg_idx": 1, "start_sec": 2.0, "end_sec": 3.0, "text": f"second {WORD}",
    }])
    # Two transcript hits (seg 0 + seg 1) plus the video's TITLE hit, which
    # also contains WORD — the title pass is on by default.
    assert len(archive_db.search(WORD)) == 3
    archive_db.delete_transcripts("twitch", VID)
    after = _texts(archive_db.search(WORD))
    # delete_transcripts removes EVERY transcript row for the video, so both
    # segments go and only the video's title hit survives. This is the
    # invalidation check: if the entry had not been retired, the two deleted
    # segments would still be in `after`.
    assert after == [f"{WORD} title"], after


def test_chat_insert_invalidates(seed, counted):
    assert archive_db.search(WORD, source="chat") == []
    archive_db.insert_messages("twitch", VID, [{
        "offset_sec": 5.0, "username": "someone", "text": f"chat {WORD}",
    }])
    assert archive_db.search(WORD, source="chat"), "new chat row must be visible"


def test_video_title_write_invalidates(seed, counted):
    assert archive_db.search(WORD, source="video")
    archive_db.upsert_video({
        "platform": "twitch", "video_id": VID, "channel": CHAN,
        "title": "renamed away", "started_at": "2026-08-01T12:00:00Z",
        "kind": "vod",
    })
    assert archive_db.search(WORD, source="video") == [], "title pass must re-run"


def test_job_write_does_not_invalidate(seed, counted):
    """archive_jobs rows are not searchable: a scheduler heartbeat must not
    flush the cache (that would make it useless under load)."""
    archive_db.search(WORD)
    archive_db.execute(
        "INSERT OR REPLACE INTO archive_jobs (id, kind, platform, video_id, "
        "status, priority, created_at, updated_at) "
        "VALUES ('job-1','transcribe','twitch',?,'queued',0,'x','x')", (VID,))
    archive_db.search(WORD)
    assert len(counted) == 1, "a non-content write must not retire the entry"


# --- 3. bounds and the semantic exemption --------------------------------

def test_cache_is_bounded(seed, monkeypatch):
    monkeypatch.setattr(archive_db, "_SEARCH_CACHE_MAX", 4)
    for i in range(20):
        archive_db.search(f"{WORD}{i}")
    assert len(archive_db._search_cache) <= 4


def test_oversized_result_is_not_cached(seed, monkeypatch):
    monkeypatch.setattr(archive_db, "_SEARCH_CACHE_MAX_HITS", 0)
    archive_db.search(WORD)
    assert archive_db._search_cache == {}, "a result over the cap is not memoised"


def test_semantic_search_is_never_cached(seed, counted):
    """The embed model's fingerprint changes with no DB write, so a cached
    semantic hit list would survive a re-embed."""
    archive_db.search(WORD, semantic=True)
    archive_db.search(WORD, semantic=True)
    assert len(counted) == 2, "semantic results bypass the cache entirely"
    assert archive_db._search_cache == {}
