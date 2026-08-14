"""Real-model semantic search e2e (multilingual e5 embedder + mMARCO reranker).

Builds a tiny scratch archive and asserts with REAL ONNX vectors that:
  * the multilingual reranker ranks the relevant PT segment first and its
    scores discriminate (non-degenerate) on a Portuguese query,
  * ASR placeholder rows ([&nbsp;__&nbsp;]) never reach the hits,
  * semantic search returns hits (embedder + rerank both functional).

Skips at collection when the ONNX models are absent — point
VODRIP_EMBED_CACHE at a real model cache to run it (the default pytest env
pins VODRIP_CACHE_DIR to scratch, so the settings-resolved root is empty).
CI without the models runs the fake-embedder logic tests in
test_archive_semantic.py instead.

Run from backend/:
  python -m pytest tests/test_archive_semantic_e2e_real.py
  VODRIP_EMBED_CACHE=I:/path/to/embed-models python -m pytest tests/test_archive_semantic_e2e_real.py
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="archive-semantic-real-"))
_DB = _TMP / "archive.db"

# Empty scratch DB: the app's own DDL is applied on first connect.
sqlite3.connect(str(_DB)).close()

# VODRIP_ARCHIVE_DB is applied by the _semantic_scratch_db fixture below
# (module-level writes would leak into every pytest session at collection).
from services import archive_db, archive_embed  # noqa: E402


def _models_present() -> bool:
    # The test env (tests/conftest.py) pins VODRIP_CACHE_DIR to scratch, so
    # the settings-resolved cache root has no models during a pytest run;
    # the explicit VODRIP_EMBED_CACHE override is the opt-in that points at
    # a real model cache (set it in the invocation to exercise this file).
    roots = []
    env = os.environ.get("VODRIP_EMBED_CACHE", "").strip()
    if env:
        roots.append(Path(env))
    try:
        roots.append(archive_embed._cache_dir())
    except Exception:
        pass
    return any(
        all(
            (root / d / "model.onnx").is_file()
            and (root / d / "tokenizer.json").is_file()
            for d in (archive_embed.MODEL_ID, archive_embed._RERANK_MODEL_DIR)
        )
        for root in dict.fromkeys(roots)  # dedupe, keep order
    )


pytestmark = pytest.mark.skipif(
    not _models_present(), reason="embed ONNX models not in cache (set VODRIP_EMBED_CACHE)"
)


@pytest.fixture(scope="module", autouse=True)
def _semantic_scratch_db():
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)
    yield
    if prev is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False


@pytest.fixture(autouse=True)
def _clean_slate():
    with archive_db.get_conn():
        archive_db.get_conn().execute("DELETE FROM transcripts")
        archive_db.get_conn().execute("DELETE FROM videos")


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


def test_real_model_ranks_relevant_pt_segment_first():
    _add_video("real-1", "gaveta")
    _add_seg("real-1", 0, "vamos fazer um molho de tomate caseiro com manjericão")
    _add_seg("real-1", 1, "hoje tem campeonato de futebol na televisão")
    _add_seg("real-1", 2, "o gato felino corre pelo quintal")

    hits = archive_db.search("qual a melhor receita de molho", semantic=True, limit=5)
    assert hits, "semantic search must return hits with the real model"
    assert hits[0]["semantic"] is True
    assert "molho" in (hits[0]["text"] or "").lower(), hits[0]["text"]

    # the reranker itself must discriminate on this PT pair (spread above
    # the degenerate floor — the old English model collapsed to ~0.000 here)
    rk = archive_embed.rerank("qual a melhor receita de molho", [
        "vamos fazer um molho de tomate caseiro com manjericão",
        "hoje tem campeonato de futebol na televisão",
    ])
    assert rk is not None and rk[0] > rk[1], rk


def test_real_model_filters_placeholder_rows():
    _add_video("real-2", "gaveta")
    _add_seg("real-2", 0, "vamos fazer um molho de tomate caseiro com manjericão")
    _add_seg("real-2", 1, "[&nbsp;__&nbsp;]")

    hits = archive_db.search("molho de tomate", semantic=True, limit=5)
    assert hits
    assert all("[&nbsp;__&nbsp;]" not in (h.get("text") or "") for h in hits)
    assert any("molho" in (h.get("text") or "").lower() for h in hits)
