"""Semantic-search embeddings for transcript segments (multilingual-e5-small).

The model is loaded lazily (first semantic search) so the app boots without
torch/transformers cost; inference is CUDA float16 on NVIDIA GPUs and CPU
float32 otherwise — mirroring the whisper device policy (detect_gpu_vendor
lives in archive_transcribe, so device choice is duplicated here to keep this
module import-light). Vectors are stored per transcript segment in the
archive DB (transcript_embeddings table) and scanned with a plain cosine
pass; no separate vector service.

Any failure (model missing, offline, OOM, unsupported device) returns None
and the search degrades to lexical BM25 — semantic search is an enhancement,
never a blocker.

ponytail: full cosine scan over embedded segments is fine well past the
"thousands of hours" target on this hardware (600k segments ~ 100 ms scan +
~1 GB matrix); an ANN index (sqlite-vec / Qdrant local) is the upgrade path
beyond tens of millions of segments.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

MODEL_ID = os.environ.get("VODRIP_EMBED_MODEL", "intfloat/multilingual-e5-small")
_QUERY_PREFIX = "query: "
_PASSAGE_PREFIX = "passage: "
_BATCH = 128


def _cache_dir() -> Path:
    env = os.environ.get("VODRIP_EMBED_CACHE", "").strip()
    if env:
        return Path(env)
    return Path(os.environ.get("APPDATA", ".")) / "VOD.RIP" / "embed-models"


_lock = threading.Lock()
_loaded: Optional[tuple] = None  # (tokenizer, model, device, dtype)


def _load():
    """Lazy singleton (tokenizer, model, device). Returns None on any
    failure — callers degrade to lexical search."""
    global _loaded
    if _loaded is not None:
        return _loaded
    with _lock:
        if _loaded is not None:
            return _loaded
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer

            device = "cuda" if torch.cuda.is_available() else "cpu"
            dtype = torch.float16 if device == "cuda" else torch.float32
            cache = str(_cache_dir())
            tok = AutoTokenizer.from_pretrained(MODEL_ID, cache_dir=cache)
            model = AutoModel.from_pretrained(MODEL_ID, cache_dir=cache)
            model.to(device)
            if dtype == torch.float16:
                model.half()
            model.eval()
            _loaded = (tok, model, device, dtype)
        except Exception:  # offline, missing model, OOM — semantic is optional
            _loaded = None
        return _loaded


def embed_texts(texts: list[str], prefix: str) -> Optional[object]:
    """Mean-pooled, L2-normalized embeddings (numpy float32, rows=texts).

    Returns None when the model is unavailable; short batches are padded by
    the tokenizer. Used for both passage and query prefixes."""
    loaded = _load()
    if loaded is None or not texts:
        return None
    tok, model, device, dtype = loaded
    try:
        import numpy as np
        import torch

        out = []
        with torch.no_grad():
            for i in range(0, len(texts), _BATCH):
                enc = tok(
                    [prefix + t for t in texts[i : i + _BATCH]],
                    padding=True, truncation=True, max_length=512,
                    return_tensors="pt",
                ).to(device)
                hidden = model(**enc).last_hidden_state
                mask = enc["attention_mask"].unsqueeze(-1).to(hidden.dtype)
                pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
                out.append(torch.nn.functional.normalize(pooled).float().cpu().numpy())
        return np.vstack(out)
    except Exception:
        return None


def embed_query(q: str) -> Optional[object]:
    return embed_texts([q], _QUERY_PREFIX)
