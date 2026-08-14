"""Semantic-search embeddings: local int8 ONNX models via onnxruntime.

Backend (measured best on this box): multilingual-e5-small quantized to int8
(118MB, CPU, ~8ms/query) for query + passage embeddings, and the multilingual
cross-encoder mmarco-mMiniLMv2-L12-H384-v1 (int8, 119MB, XLMR tokenizer) as
the pair reranker — same size class as the English ms-marco model it replaced,
but trained on mMARCO's 60+ languages, so Brazilian-Portuguese queries get
real relevance scores instead of the English model's near-zero collapse.
Tokenization is the `tokenizers` lib — no transformers/torch at runtime.
Vectors stay float32 L2-normalized (matching the existing corpus); only the
MODELS are int8.

The model is loaded lazily (first semantic search) so the app boots without
onnxruntime cost; inference stays CPU-only by design (118MB int8, ~8ms/query —
a GPU provider would add driver/VRAM complexity for no measurable gain). The
heavy GPU path is the torch fp16 scan in archive_db, not this module. Vectors
are stored per transcript segment in the archive DB
(transcript_embeddings table) and scanned with a cosine pass; no separate
vector service.

Any failure (model missing, corrupt file, OOM) returns None and the search
degrades to lexical BM25 — semantic search is an enhancement, never a
blocker. Model dirs live under the AI-models folder (same disk as the
whisper/parakeet weights — the heavy cache disk is ephemeral data only):
<AI-models>/embed-models/{e5-small-int8,mmarco-mMiniLMv2-L12-int8}/ (each
holds model.onnx + tokenizer.json).

ponytail: full cosine scan over embedded segments is fine well past the
"thousands of hours" target on this hardware with the GPU matmul scan
(~53ms at 1.79M segments) + RAM matrix cache in archive_db; an ANN index
(sqlite-vec / Qdrant local) is the upgrade path beyond tens of millions of
segments. The reranker is the multilingual mMARCO cross-encoder; a
stronger multilingual reranker (bge-reranker-base int8, ~280MB) is the
upgrade path if ranking precision ever needs another step.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

# Env override selects the embed-model DIRECTORY under the model cache
# (was an HF repo id when this module ran transformers — same env name).
_EMBED_MODEL_DIR = "e5-small-int8"
_RERANK_MODEL_DIR = "mmarco-mMiniLMv2-L12-int8"
MODEL_ID = os.environ.get("VODRIP_EMBED_MODEL", _EMBED_MODEL_DIR)
_QUERY_PREFIX = "query: "
_PASSAGE_PREFIX = "passage: "
_BATCH = 128
_MAX_TOKENS = 512


def _cache_dir() -> Path:
    # Precedence: VODRIP_EMBED_CACHE env -> AI-models folder (whisper cache
    # root) /embed-models -> legacy homes <cache root>/embed-models or
    # %APPDATA%/VOD.RIP/embed-models (migration: reuse already-downloaded
    # ONNX weights — no re-download, see disk_hygiene._migrated_model_dir).
    # Model weights never resolve under the cache disk.
    env = os.environ.get("VODRIP_EMBED_CACHE", "").strip()
    if env:
        return Path(env)
    from services.disk_hygiene import _migrated_model_dir, whisper_cache_dir
    from services.settings import _get_appdata_dir, cache_root

    legacy_root = cache_root()
    legacy = (legacy_root or _get_appdata_dir()) / "embed-models"
    return _migrated_model_dir(whisper_cache_dir() / "embed-models", legacy, "embed")


_lock = threading.Lock()
_loaded: Optional[tuple] = None  # (session, tokenizer) for the embedder


def _load():
    """Lazy singleton (onnx session, tokenizer). Returns None on any
    failure — callers degrade to lexical search."""
    global _loaded
    if _loaded is not None:
        return _loaded
    with _lock:
        if _loaded is not None:
            return _loaded
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            d = _cache_dir() / MODEL_ID
            sess = ort.InferenceSession(
                str(d / "model.onnx"), providers=["CPUExecutionProvider"]
            )
            tok = Tokenizer.from_file(str(d / "tokenizer.json"))
            _loaded = (sess, tok)
        except Exception:  # missing model, corrupt file — semantic is optional
            _loaded = None
        return _loaded


def embed_texts(texts: list[str], prefix: str) -> Optional[object]:
    """Mean-pooled, L2-normalized embeddings (numpy float32, rows=texts).

    Returns None when the model is unavailable; short batches are padded by
    the tokenizer. Used for both passage and query prefixes. The session
    feed is built from the ONNX graph's actual input names, so both BERT-
    style exports (token_type_ids present) and XLM-R-style exports (no
    token-type embeddings) run through the same code path."""
    loaded = _load()
    if loaded is None or not texts:
        return None
    sess, tok = loaded
    try:
        import numpy as np

        in_names = {i.name for i in sess.get_inputs()}
        pad_id = tok.token_to_id("<pad>") or tok.token_to_id("[PAD]") or 1
        tok.enable_padding(pad_id=pad_id, pad_token="<pad>" if tok.token_to_id("<pad>") is not None else "[PAD]")
        tok.enable_truncation(max_length=_MAX_TOKENS)
        out = []
        for i in range(0, len(texts), _BATCH):
            encs = tok.encode_batch([prefix + t for t in texts[i : i + _BATCH]])
            ids = np.asarray([e.ids for e in encs], dtype=np.int64)
            mask = np.asarray([e.attention_mask for e in encs], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in in_names:
                feed["token_type_ids"] = np.zeros_like(ids)
            hidden = sess.run(None, feed)[0]
            m = mask.astype(np.float32)[..., None]
            pooled = (hidden * m).sum(1) / m.sum(1).clip(min=1e-9)
            out.append(pooled / np.linalg.norm(pooled, axis=1, keepdims=True))
        return np.vstack(out).astype(np.float32)
    except Exception:
        return None


def embed_query(q: str) -> Optional[object]:
    return embed_texts([q], _QUERY_PREFIX)


_rerank_loaded: Optional[tuple] = None  # (session, tokenizer, has_token_ids) | False = failed
_rerank_lock = threading.Lock()
_rerank_degenerate_logged = False
# Sigmoid-score spread below this means the cross-encoder cannot tell the
# candidates apart for this query/language (the old English reranker on
# Portuguese collapsed to ~0.000 for everything) — ordering by such noise is
# worse than cosine order, so the caller falls back to it.
_RERANK_DEGENERATE_SPREAD = 0.01


def rerank(query: str, texts: list[str]) -> Optional[list[float]]:
    """Pairwise relevance scores (0..1) of texts vs query (multilingual
    mMARCO cross-encoder int8).

    None on any failure — callers fall back to cosine order. The reranker
    is optional: search works without it, just with flatter ranking. The
    ONNX feed adapts to the graph's actual input names (BERT exports carry
    token_type_ids; XLM-R exports — like this model — do not), and a
    degenerate score spread (the model cannot discriminate the candidates)
    is reported as None so cosine order wins instead of noise."""
    global _rerank_loaded, _rerank_degenerate_logged
    if _rerank_loaded is None:
        with _rerank_lock:
            if _rerank_loaded is None:
                try:
                    import onnxruntime as ort
                    from tokenizers import Tokenizer

                    d = _cache_dir() / _RERANK_MODEL_DIR
                    sess = ort.InferenceSession(
                        str(d / "model.onnx"), providers=["CPUExecutionProvider"]
                    )
                    tok = Tokenizer.from_file(str(d / "tokenizer.json"))
                    tok.enable_truncation(max_length=_MAX_TOKENS)
                    pad_tok = "<pad>" if tok.token_to_id("<pad>") is not None else "[PAD]"
                    tok.enable_padding(
                        pad_id=tok.token_to_id(pad_tok) or 0, pad_token=pad_tok
                    )
                    has_tids = "token_type_ids" in {i.name for i in sess.get_inputs()}
                    _rerank_loaded = (sess, tok, has_tids)
                except Exception:
                    _rerank_loaded = False  # tried once, don't retry every query
    if not _rerank_loaded or not texts or not query:
        return None
    sess, tok, has_tids = _rerank_loaded
    try:
        import numpy as np

        encs = tok.encode_batch([(query, t) for t in texts])
        feed = {
            "input_ids": np.asarray([e.ids for e in encs], dtype=np.int64),
            "attention_mask": np.asarray([e.attention_mask for e in encs], dtype=np.int64),
        }
        if has_tids:
            feed["token_type_ids"] = np.asarray(
                [e.type_ids for e in encs], dtype=np.int64
            )
        logits = sess.run(None, feed)[0].reshape(-1)
        scores = (1.0 / (1.0 + np.exp(-logits))).tolist()
        if _scores_degenerate(scores):
            if not _rerank_degenerate_logged:
                _rerank_degenerate_logged = True
                import logging

                logging.getLogger("vodrip.search").warning(
                    "semantic rerank: score spread %.3f < %.3f — the cross-"
                    "encoder cannot discriminate query %r; falling back to "
                    "cosine order",
                    max(scores) - min(scores), _RERANK_DEGENERATE_SPREAD,
                    query[:60],
                )
            return None
        return scores
    except Exception:
        return None


def _scores_degenerate(scores: list[float]) -> bool:
    """True when the reranker's score spread is below the discrimination
    floor — the model cannot tell the candidates apart for this query/
    language, so ordering by the scores would be noise."""
    return bool(scores) and (max(scores) - min(scores) < _RERANK_DEGENERATE_SPREAD)


def _model_fingerprint(model_dir: str) -> Optional[str]:
    """Stable identity of one model's files (dir + size + mtime of each
    file the runtime loads). None when the model is unavailable. Stored
    next to corpus vectors so a model swap (or a re-exported file) is
    detected and triggers a full re-embed instead of silently mixing
    vector spaces."""
    try:
        root = _cache_dir() / model_dir
        parts = []
        for f in ("model.onnx", "tokenizer.json"):
            st = (root / f).stat()
            parts.append(f"{f}:{st.st_size}:{int(st.st_mtime)}")
        return f"{model_dir}|{'|'.join(parts)}"
    except Exception:
        return None


def embed_fingerprint() -> Optional[str]:
    """Fingerprint of the embedder's files (see _model_fingerprint)."""
    return _model_fingerprint(MODEL_ID)


def rerank_fingerprint() -> Optional[str]:
    """Fingerprint of the reranker's files (see _model_fingerprint)."""
    return _model_fingerprint(_RERANK_MODEL_DIR)


def warmup_if_indexed() -> None:
    """Background-warm the semantic-search stack when the archive already
    holds vectors (the user has run a semantic search before): the ONNX
    session (~2s), the full-corpus matrix (~16s mmap load) and its fp16
    CUDA tensor (~4s) — the first semantic query of a fresh boot then
    answers in well under a second instead of ~30-180s. Archives without
    vectors never pay the load: semantic search stays fully lazy for them
    (and degrades to lexical anyway)."""
    try:
        from services import archive_db  # lazy: no import cycle at boot

        n = archive_db.query(
            "SELECT COUNT(*) AS n FROM transcript_embeddings WHERE vec IS NOT NULL"
        )[0]["n"]
    except Exception:
        return
    if not n:
        return

    def _warm() -> None:
        _load()
        try:
            archive_db._embed_matrix()  # RAM matrix + GPU tensor
        except Exception:
            pass  # scan stays lazy; first search pays the build

    threading.Thread(target=_warm, name="embed-warmup", daemon=True).start()


def backfill_missing(
    interrupt: Optional[threading.Event] = None,
    min_missing: int = 0,
) -> int:
    """Background job: embed every transcript segment that lacks a vector.

    Returns the number of segments embedded (0 = nothing to do, model
    unavailable, or interrupted before the first batch). Batches follow
    _BATCH; each batch is upserted in one transaction. min_missing gates
    the work (tiny scratch archives aren't worth a model load) and the
    caller passes a shutdown interrupt to stop mid-pass. Idempotent: after
    a complete pass every later call returns 0 immediately.
    """
    from services import archive_db  # lazy: no import cycle at module scope

    missing = archive_db.missing_embedding_segments()
    if not missing or len(missing) < min_missing:
        return 0
    # Model load is the expensive part — fail fast before any writes.
    probe = embed_texts(["ok"], _PASSAGE_PREFIX)
    if probe is None:
        return 0
    done = 0
    for i in range(0, len(missing), _BATCH):
        if interrupt is not None and interrupt.is_set():
            break
        batch = missing[i : i + _BATCH]
        vecs = embed_texts([r["text"] for r in batch], _PASSAGE_PREFIX)
        if vecs is None:
            break
        archive_db.set_transcript_embeddings(
            [
                (r["transcript_id"], v.astype("<f4").tobytes())
                for r, v in zip(batch, vecs)
            ]
        )
        done += len(batch)
    return done
