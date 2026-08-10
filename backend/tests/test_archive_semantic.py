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
import time
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
    # Force THIS module's scratch DB at setup: get_conn() keys on the env
    # path, so a module imported later in the batch wins the env at
    # collection end — without this, this suite silently ran against (and
    # _clean_slate wiped) whichever DB that was, failing its real-data
    # assertions (e.g. test_channel_language's titiltei copy).
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)
    yield
    # Restore, don't pop: module-level env sets happen at collection for ALL
    # modules, so removing the key here would KeyError later modules'
    # teardowns (test_archive_transcribe_*_real reads it back).
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


def test_semantic_response_cache_serves_repeat_submit(_patch_embedder):
    # The whole-pass response cache (not just the vector piece cache) must
    # make an identical repeat submit instant: zero embedder work. The
    # counting wrapper is installed BEFORE the first call so the cache key
    # (which stamps the callable identity) stays stable across both calls.
    import services.archive_embed

    _add_video("sem-resp-cache", "gaveta")
    _add_seg("sem-resp-cache", 0, "o gato felino corre")
    orig = services.archive_embed.embed_query
    counts = {"embed_query": 0}

    def _counting(q):
        counts["embed_query"] += 1
        return orig(q)

    services.archive_embed.embed_query = _counting
    try:
        first = archive_db.search("criatura peluda", semantic=True)
        assert first and first[0]["semantic"] is True
        n1 = counts["embed_query"]
        assert n1 >= 1
        second = archive_db.search("criatura peluda", semantic=True)
        assert second == first
        assert counts["embed_query"] == n1  # repeat submit: whole pass cached
        # Evict the piece cache too — the response cache alone still serves
        # (the key's callable stamp is unchanged).
        archive_db._embed_query_cache.clear()
        third = archive_db.search("criatura peluda", semantic=True)
        assert third == first
        assert counts["embed_query"] == n1
        # A different query misses and re-runs the embedder.
        archive_db.search("outra coisa", semantic=True)
        assert counts["embed_query"] > n1
    finally:
        services.archive_embed.embed_query = orig


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


def test_semantic_query_spelling_corrected_before_embedding(_patch_embedder, monkeypatch):
    import services.archive_embed

    _add_video("sem-typo", "gaveta")
    _add_seg("sem-typo", 0, "o gato preto corre")
    # Force the transcript vocab warm; the cold path returns None and the
    # spelling correction would no-op on the raw query.
    archive_db._load_vocab_uncached("transcripts", time.monotonic())

    seen = []
    orig = services.archive_embed.embed_query
    monkeypatch.setattr(
        services.archive_embed, "embed_query", lambda q: (seen.append(q), orig(q))[1]
    )
    hits = archive_db.search("preato gato", semantic=True, limit=10)
    assert any(h["semantic"] and h["video_id"] == "sem-typo" for h in hits)
    # the embedded query used the corpus spelling, not the typo
    assert seen and seen[0] == "preto gato", seen


def test_semantic_typo_and_exact_queries_agree(_patch_embedder, monkeypatch):
    import services.archive_embed

    _add_video("sem-receita", "gaveta")
    _add_seg("sem-receita", 0, "vamos fazer uma receita de molho")
    archive_db._load_vocab_uncached("transcripts", time.monotonic())

    exact = archive_db.search("receita de molho", semantic=True, limit=10)
    seen = []
    orig = services.archive_embed.embed_query
    monkeypatch.setattr(
        services.archive_embed, "embed_query", lambda q: (seen.append(q), orig(q))[1]
    )
    typo = archive_db.search("recita de molho", semantic=True, limit=10)
    assert seen and seen[0] == "receita de molho", seen
    # same embedded text -> same rerank cache key -> identical top hit
    assert typo and typo[0]["semantic"] is True
    assert typo[0]["offset_sec"] == exact[0]["offset_sec"]


def test_semantic_hybrid_exact_lexical_leads(_patch_embedder):
    _add_video("sem-hy-a", "gaveta")
    _add_seg("sem-hy-a", 0, "o gato felino corre")
    _add_video("sem-hy-b", "gaveta")
    _add_seg("sem-hy-b", 0, "uma criatura peluda anda")
    hits = archive_db.search("felino gato", semantic=True, limit=10)
    assert len(hits) >= 2
    # literal (all-words) lexical match leads; the concept-only hit follows
    assert hits[0]["video_id"] == "sem-hy-a"
    assert hits[0]["partial"] is False
    assert any(h["video_id"] == "sem-hy-b" and h["semantic"] for h in hits[1:])


def test_semantic_fingerprint_invalidation_reembeds(_patch_embedder, monkeypatch):
    import services.archive_embed

    calls = _patch_embedder
    _add_video("sem-fp", "gaveta")
    _add_seg("sem-fp", 0, "o gato felino corre")
    _add_seg("sem-fp", 1, "a lua brilha")
    archive_db.search("criatura peluda", semantic=True)
    n1 = len(calls)
    assert n1 > 0
    # model files changed -> stored vectors belong to another vector space
    monkeypatch.setattr(
        services.archive_embed, "embed_fingerprint", lambda: "other-model-v2"
    )
    archive_db.search("criatura peluda", semantic=True)
    assert len(calls) > n1  # full re-embed happened
    n2 = len(calls)
    # a different query with the same fingerprint: no re-embed
    archive_db.search("outro conceito", semantic=True)
    assert len(calls) == n2


def test_semantic_noise_rows_never_rank(_patch_embedder):
    _add_video("sem-noise", "gaveta")
    _add_seg("sem-noise", 0, "o gato felino corre")
    _add_seg("sem-noise", 1, "[&nbsp;__&nbsp;]")
    hits = archive_db.search("criatura peluda", semantic=True, limit=10)
    assert hits
    assert all("[&nbsp;__&nbsp;]" not in (h.get("text") or "") for h in hits)
    # unit-level contract of the noise predicate
    assert archive_db._semantic_noise("[&nbsp;__&nbsp;]") is True
    assert archive_db._semantic_noise("Ã,") is True
    assert archive_db._semantic_noise("[risadas]") is False
    assert archive_db._semantic_noise("o gato corre") is False


def test_semantic_rerank_degenerate_scores_detected(_patch_embedder):
    import services.archive_embed

    assert services.archive_embed._scores_degenerate([]) is False
    assert services.archive_embed._scores_degenerate([0.5, 0.5, 0.501]) is True
    assert services.archive_embed._scores_degenerate([0.1, 0.9]) is False
