"""Semantic search pass against a scratch archive DB (fake embedder).

archive_db._semantic_search imports services.archive_embed lazily, so the
tests monkeypatch embed_query/embed_texts on that module with a numpy word-
hash embedder — no torch/transformers involved. Dims come from a md5 hash
(stable across processes), and the fixture tokens were chosen so the test
queries deterministically collide with them (e.g. 'criatura' and 'felino'
both land on dim 20), giving known-positive cosine scores.

Run from backend/: python -m pytest tests/test_archive_semantic.py
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
from pathlib import Path

import numpy as np
import pytest

_TMP = Path(tempfile.mkdtemp(prefix="archive-semantic-"))
_DB = _TMP / "archive.db"

# Empty scratch DB: the app's own DDL (incl. transcript_embeddings + its
# DELETE trigger) is applied on first connect by archive_db.
sqlite3.connect(str(_DB)).close()

os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)

from services import archive_db  # noqa: E402  (env must be set first)

_DIM = 64


def _dim(token: str) -> int:
    return int(hashlib.md5(token.encode()).hexdigest()[:6], 16) % _DIM


def _fake_vec(texts: list[str], prefix: str) -> np.ndarray:
    """Bag-of-word-hash vectors: cosine ≈ shared-vocabulary overlap."""
    out = np.zeros((len(texts), _DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        for tok in re.findall(r"[a-z0-9]+", (prefix + t).lower()):
            out[i, _dim(tok)] += 1.0
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return out / norms


@pytest.fixture(scope="module", autouse=True)
def _semantic_scratch_db():
    yield
    # Restore, don't pop: module-level env sets happen at collection for ALL
    # modules, so removing the key here would KeyError later modules'
    # teardowns (test_archive_transcribe_*_real reads it back).
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    if prev is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False


@pytest.fixture(autouse=True)
def _clean_slate():
    # Tests share one scratch DB; wipe content rows before each test (the
    # AFTER DELETE triggers cascade FTS + embeddings rows).
    with archive_db.get_conn():
        archive_db.get_conn().execute("DELETE FROM transcripts")
        archive_db.get_conn().execute("DELETE FROM videos")


@pytest.fixture(autouse=True)
def _patch_embedder(monkeypatch):
    import services.archive_embed

    calls: list[list[str]] = []

    def _embed_texts(texts, prefix):
        calls.append(texts)
        return _fake_vec(texts, prefix)

    monkeypatch.setattr(services.archive_embed, "embed_texts", _embed_texts)
    monkeypatch.setattr(
        services.archive_embed, "embed_query",
        lambda q: _fake_vec([q], "query: "),
    )
    # Rerank is real-model (mmarco) — irrelevant to these logic tests and
    # machine-dependent; force the cosine fallback path.
    monkeypatch.setattr(services.archive_embed, "rerank", lambda q, texts: None)
    yield calls


def _add_video(video_id: str, channel: str) -> None:
    archive_db.upsert_video({
        "platform": "youtube",
        "video_id": video_id,
        "channel": channel,
        "title": f"title {video_id}",
        "started_at": "2026-07-30T12:00:00Z",
        "kind": "vod",
    })


def _add_seg(video_id: str, seg_idx: int, text: str) -> None:
    archive_db.insert_transcript(
        "youtube", video_id,
        [{"seg_idx": seg_idx, "start_sec": float(seg_idx), "end_sec": float(seg_idx + 1),
          "text": text}],
    )


def test_semantic_finds_concept_hit_lexical_misses(_patch_embedder):
    # "criatura" shares dim 20 with "felino" (cos > 0) but shares no
    # spelling/pronunciation with any fixture token, so every lexical pass
    # (FTS fuzzy tiers, phrase, span, titles) comes back empty.
    _add_video("sem-gato", "gaveta")
    _add_seg("sem-gato", 0, "o gato felino corre")
    hits = archive_db.search("criatura peluda", semantic=True)
    assert len(hits) == 1
    h = hits[0]
    assert h["kind"] == "transcript"
    assert h["video_id"] == "sem-gato"
    assert h["semantic"] is True
    assert h["score"] > 0.0
    # without the semantic flag the same query is empty
    assert archive_db.search("criatura peluda") == []


def test_semantic_degrades_to_lexical(_patch_embedder, monkeypatch):
    _add_video("sem-lex", "gaveta")
    _add_seg("sem-lex", 0, "zebra correndo")
    # embedder unavailable -> lexical results only, no crash, no flag
    import services.archive_embed

    monkeypatch.setattr(services.archive_embed, "embed_query", lambda q: None)
    hits = archive_db.search("zebra", semantic=True)
    assert hits and hits[0]["video_id"] == "sem-lex"
    assert all("semantic" not in h or not h["semantic"] for h in hits)


def test_semantic_backfills_then_reuses(_patch_embedder):
    calls = _patch_embedder
    _add_video("sem-cache", "gaveta")
    _add_seg("sem-cache", 0, "um foguete decola")
    _add_seg("sem-cache", 1, "a lua brilha")
    hits = archive_db.search("espaco sideral", semantic=True)
    assert len(hits) >= 1
    assert calls  # passage embedding happened once
    n = len(calls)
    rows = archive_db.query("SELECT transcript_id FROM transcript_embeddings")
    assert len(rows) == 2
    # second search: no passage re-embedding (cache hit)
    archive_db.search("espaco sideral", semantic=True)
    assert len(calls) == n


def test_embeddings_delete_cascade(_patch_embedder):
    _add_video("sem-del", "gaveta")
    _add_seg("sem-del", 0, "cachorro late alto")
    archive_db.search("animal", semantic=True)
    assert archive_db.query("SELECT COUNT(*) AS n FROM transcript_embeddings")[0]["n"] == 1
    archive_db.delete_transcripts("youtube", "sem-del")
    assert archive_db.query("SELECT COUNT(*) AS n FROM transcript_embeddings")[0]["n"] == 0


def test_semantic_scopes_by_channel(_patch_embedder):
    _add_video("sem-ch-a", "gaveta")
    _add_seg("sem-ch-a", 0, "gato dorme")
    _add_video("sem-ch-b", "outra")
    _add_seg("sem-ch-b", 0, "gato pula")
    hits = archive_db.search("felino", channel="outra", semantic=True)
    assert [h["video_id"] for h in hits] == ["sem-ch-b"]


def test_semantic_skipped_for_chat_source(_patch_embedder):
    calls = _patch_embedder
    _add_video("sem-chat", "gaveta")
    _add_seg("sem-chat", 0, "gato mia")
    archive_db.search("felino", source="chat", semantic=True)
    assert calls == []  # embedder never touched


async def test_semantic_router_param_passes_through(_patch_embedder):
    from routers.archive import archive_search

    _add_video("sem-router", "gaveta")
    _add_seg("sem-router", 0, "gato no sofa")
    resp = await archive_search(q="felino", semantic=True, limit=20)
    assert resp["hits"] and resp["hits"][0]["semantic"] is True
    # without the flag the same query is empty (lexical miss); semantic
    # must be passed explicitly — direct calls see the Query() marker
    # object (truthy) instead of FastAPI's substituted value.
    resp = await archive_search(q="felino", semantic=False, limit=20)
    assert resp["hits"] == []
