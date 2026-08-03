"""Phonetic fuzzy-search tests: fold table, Damerau budgets, distance-tier
expansion, and the folded-bigram bridge, against a scratch archive DB.

The module self-check runs at import, so the env must be set BEFORE the
first import in this process; the per-test fixture rebinds to its own
scratch DB (get_conn() re-opens on path change, and the vocab/bigram
caches carry row-count gates, so cross-DB staleness cannot leak).

Run from backend/: python -m pytest tests/test_archive_phonetic_search.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="archive-phonetic-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")

from services import archive_db as db  # noqa: E402


# --- phonetic fold table --------------------------------------------------

def test_fold_bridges_phonetic_pairs():
    f = db._phonetic_fold
    # c/k and ç/ss collapse; the folded pair stays within one Damerau edit
    assert f("katarina") == "katarina"
    assert f("catarina") == "satarina"
    assert db._damerau_levenshtein(f("katarina"), f("catarina"), 1) == 1
    assert f("ambessa") == f("ambeça") == "ambesa"
    # ph->f, y->i and final unstressed vowels
    assert f("seraphine") == f("serafine") == "serafini"
    assert f("yasuo") == "iasu"
    # FTS5 unicode61 strips diacritics before indexing: 'aço' arrives as
    # 'aco', so the c-before-vowel rule must fold stripped forms identically
    assert f("aço") == f("aco") == "asu"
    assert f("nasco") == "nasu"


def test_fold_keeps_foreign_sh_digraph():
    f = db._phonetic_fold
    # the champion name must NOT collapse onto the common words ('shaco'
    # and 'caso'/'saco' stay one edit apart, not zero)
    assert f("shaco") == "shasu"
    assert f("caso") == f("saco") == "sasu"
    assert db._damerau_levenshtein(f("shaco"), f("caso"), 1) == 1
    assert f("shen") == "shen"
    # ...while a sibilant ASR artifact after a vowel still folds away
    assert f("nasho") == "nasu"
    assert db._damerau_levenshtein("nasus", "nasu", 1) == 1


def test_damerau_budgets():
    dl = db._damerau_levenshtein
    assert dl("abc", "acb", 1) == 1, "adjacent transposition is one edit"
    assert dl("asu", "sasu", 1) == 1, "prefix insertion fits the budget"
    assert dl("suen", "sen", 1) == 1
    assert dl("shen", "suen", 1) == 1
    assert dl("aurora", "aunara", 2) == 2
    assert dl("aurora", "aunara", 1) is None, "over-budget bails"


# --- search against a scratch DB -----------------------------------------

@pytest.fixture()
def scratch(monkeypatch, tmp_path):
    """Two videos: A carries the ASR variant rows, B carries high-frequency
    common-word distractors so IDF separates the misheard champion form
    from the noise ('chaco' is rare, 'caso' is not)."""
    db_path = tmp_path / "archive.db"
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(db_path))
    for vid in ("phonv1", "phonv2"):
        db.upsert_video(
            {
                "platform": "youtube",
                "video_id": vid,
                "channel": "chan",
                "title": "t",
                "canonical_key": f"chan-{vid}",
            }
        )

    def add(video_id: str, text: str) -> None:
        db.insert_transcript(
            "youtube",
            video_id,
            [
                {
                    "seg_idx": 0,
                    "start_sec": 0.0,
                    "end_sec": 1.0,
                    "text": text,
                    "words": [],
                }
            ],
            lang="pt",
        )

    add("phonv1", "a Catarina feedou de novo")
    add("phonv1", "yasuo é o melhor campeão")
    add("phonv1", "e aço pegou o quadra kill de novo")  # misheard 'yasuo'
    add("phonv1", "olha o iaso dando dano")
    add("phonv1", "jogando de Chaco na lane")  # misheard 'shaco'
    add("phonv1", "Nautilus, Maokai, você precisa")
    add("phonv1", "o nutilos dele é bom")  # misheard 'Nautilus'
    for _ in range(12):  # common-word distractors, frequent on purpose
        add("phonv2", "no caso, né")
        add("phonv2", "quebra casco")
        add("phonv2", "o saco encheu")
    return db


def _texts(hits) -> list[str]:
    return [(h.get("text") or "").lower() for h in hits]


def test_expansion_distances(scratch):
    terms = dict(db._expand_query("shaco", ["transcripts"]))
    assert terms["shaco"] == 0
    assert terms["chaco"] == 1
    assert terms["caso"] == 1, "caso must not fold equal to shaco"
    # the folded pair index bridges the single query token to the corpus
    # phrase with the same pronunciation
    terms = dict(db._expand_query("yasuo", ["transcripts"]))
    assert terms["e aço"] == 1
    assert db._phonetic_fold("e") + db._phonetic_fold("aço") == "iasu"


def test_search_finds_asr_variants(scratch):
    assert any("catarina" in t for t in _texts(db.search("katarina")))
    assert any("nutilos" in t for t in _texts(db.search("nautilus")))


def test_bigram_bridge_finds_cross_token_variant(scratch):
    hits = _texts(db.search("yasuo", limit=10))
    assert any("yasuo" in t for t in hits)
    assert any("e aço" in t for t in hits), "folded pair must surface 'e aço'"
    assert any("iaso" in t for t in hits)


def test_champion_outranks_common_word_noise(scratch):
    hits = db.search("shaco", limit=10)
    texts = _texts(hits)
    assert any("chaco" in t for t in texts), "misheard champion form is found"
    # the champion form (rare) ranks above the frequent 'caso' distractors
    assert "chaco" in texts[0]
    assert texts[0].count("caso") == 0
