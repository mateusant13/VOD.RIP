"""Multi-word ranking tests: the all-tokens AND tier, the partial-match
flag, and the prefix-expansion gate (present tokens must not flood tier 0).

Regression: "vale da estranheza" used to return only vale*/valendo/valeu
noise at score 1.0 — the phrase was absent from the corpus and every word
starting with the 3-char phonetic fold 'val' was treated as a distance-0
equal match. FTS5's implicit multi-word AND semantics are now an explicit
tier (phrase > AND > OR), OR-only hits carry partial=True, and prefix
expansion only fires for tokens ABSENT from the corpus.

Run from backend/: python -m pytest tests/test_archive_search_ranking.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="archive-ranking-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")

from services import archive_db as db  # noqa: E402


@pytest.fixture()
def scratch(monkeypatch, tmp_path):
    """Three videos with distinct match shapes for 'vale da estranheza':
    A = exact phrase (one row), C = both content words, separated, B = only
    'vale' forms (distractor that used to flood the top)."""
    db_path = tmp_path / "archive.db"
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(db_path))
    for vid in ("vra", "vrb", "vrc"):
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

    add("vra", "vale da estranheza aconteceu no fim")
    add("vrb", "valeu galera valendo demais hoje")
    add("vrc", "estranheza veio depois do vale")
    # Prime the search caches synchronously: the request path no longer
    # builds vocab/bigrams inline (cold searches serve exact tokens and
    # rebuild in background threads), but these tests assert the fuzzy
    # expansion and tier contracts that need a warm vocab.
    import time as _t

    for t in ("transcripts", "messages"):
        db._load_vocab_uncached(t, _t.monotonic())
    tables = ["transcripts", "messages"]
    db._load_bigrams(tables)  # cold -> kicks a background rebuild
    key = str(db._db_path()) + "|" + "|".join(tables)
    for _ in range(1000):
        with db._vocab_lock:
            if key in db._bigram_cache:
                break
        _t.sleep(0.005)
    return db


def test_phrase_then_and_then_partial(scratch):
    hits = db.search("vale da estranheza", limit=20)
    texts = [h.get("text", "").lower() for h in hits]
    # Exact-phrase row first, all-words row second, vale-only noise last.
    assert texts[0].startswith("vale da estranheza")
    assert any("estranheza veio depois do vale" in t for t in texts)
    assert hits[0]["partial"] is False
    and_row = next(
        h for h in hits if "estranheza veio depois do vale" in h.get("text", "")
    )
    assert and_row["partial"] is False, "both query words present -> exact"
    # The vale-only distractor is now a flagged partial match, not a 1.0 tie.
    or_row = next(h for h in hits if "valeu galera" in h.get("text", ""))
    assert or_row["partial"] is True
    assert or_row["score"] < hits[0]["score"]


def test_missing_word_marks_all_partial(scratch):
    # "estranheza" absent from the corpus -> every hit is a partial match.
    hits = db.search("estranheza fantasma", limit=20)
    assert hits, "vale* forms still surface as closest matches"
    assert all(h["partial"] for h in hits)


def test_single_word_never_partial(scratch):
    hits = db.search("valeu", limit=20)
    assert hits
    # The exact-token row leads and is not partial; fuzzy twins ("vale" is
    # a dist-1 twin of "valeu") are correctly flagged as partial matches.
    assert hits[0]["partial"] is False
    assert "valeu" in hits[0].get("text", "").lower()


def test_present_token_no_prefix_flood(scratch):
    # 'vale' EXISTS in the corpus vocab -> complete word: prefix forms are
    # DEMOTED to tier 1 (never distance 0), so they can't tie with the
    # exact token. Distance-1 typo tolerance still applies.
    terms = dict(db._expand_query("vale", ["transcripts"]))
    assert terms["vale"] == 0
    assert terms.get("valendo") == 1, "prefix form must not sit at tier 0"
    assert terms.get("valeu") == 1
    assert 0 not in {d for t, d in db._expand_query("vale", ["transcripts"]) if t != "vale"}


def test_absent_token_still_prefix_expands(scratch):
    # 'kata' is not in this corpus -> partial word -> prefix expansion
    # still reaches 'katarina' when the corpus has it.
    db.insert_transcript(
        "youtube",
        "vra",
        [
            {
                "seg_idx": 9,
                "start_sec": 9.0,
                "end_sec": 10.0,
                "text": "a Catarina feedou de novo",
                "words": [],
            }
        ],
        lang="pt",
    )
    # The new word is corpus-only until the vocab rebuild lands; prime it
    # synchronously (production reaches the same state ~1s after ingestion
    # via the background rebuild).
    import time as _t

    db._load_vocab_uncached("transcripts", _t.monotonic())
    terms = dict(db._expand_query("kata", ["transcripts"]))
    assert any(t == "catarina" for t in terms), "absent token keeps prefix reach"


def test_title_partial_flag(scratch):
    db.upsert_video(
        {
            "platform": "youtube",
            "video_id": "vrt1",
            "channel": "chan",
            "title": "VALE DA ESTRANHEZA - episódio 3",
            "canonical_key": "chan-vrt1",
        }
    )
    db.upsert_video(
        {
            "platform": "youtube",
            "video_id": "vrt2",
            "channel": "chan",
            "title": "CAMPEONATO DO BOGUR VALENDO 75 MIL DÓLARES",
            "canonical_key": "chan-vrt2",
        }
    )
    hits = db.search("vale da estranheza", limit=20)
    title_hits = [h for h in hits if h["kind"] == "title"]
    assert title_hits, "titles pass still runs"
    exact = next(h for h in title_hits if "episódio" in h.get("text", ""))
    partial = next(h for h in title_hits if "BOGUR" in h.get("text", "").upper())
    assert exact["partial"] is False
    assert partial["partial"] is True


def test_and_tier_any_order(scratch):
    # Same row, reversed order: phrase fails (order), AND still matches.
    hits = db.search("estranheza vale", limit=20)
    row = next(h for h in hits if "estranheza veio depois do vale" in h.get("text", ""))
    assert row["partial"] is False, "all words present regardless of order"


# --- fuzzy admission gate (R4) -------------------------------------------

def test_fuzzy_gate_blocks_fold_only_neighbors(scratch):
    """'caralho' must not expand to 'cavalo'/'carrasco' — those are fold-
    near but raw-far (raw Damerau 3); the phonetic fold collapses distinct
    words and used to flood the tier-1 OR pattern with unrelated hits."""
    for vid, text in (
        ("fz1", "que cavalo bonito"),
        ("fz2", "o carrasco não perdoa"),
        ("fz3", "caralho que jogada"),
    ):
        db.insert_transcript(
            "youtube", vid,
            [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": text, "words": []}],
            lang="pt",
        )
    terms = dict(db._expand_query("caralho", ["transcripts"]))
    assert "cavalo" not in terms, "fold-only neighbor must not expand"
    assert "carrasco" not in terms, "fold-only neighbor must not expand"
    hits = db.search("caralho", limit=20)
    texts = [h.get("text", "").lower() for h in hits]
    assert any("caralho que jogada" in t for t in texts)
    assert not any("cavalo" in t for t in texts)
    assert not any("carrasco" in t for t in texts)


def test_fuzzy_gate_keeps_true_typos_and_prefix_stretch(scratch):
    """Raw-near variants still expand: 'estranheza'->'estranhesa' (raw 1)
    and 'caraio'->'cara' (raw 2 + shared 3-char prefix)."""
    db.insert_transcript(
        "youtube", "ty1",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0,
          "text": "aquela estranhesa de sempre, coisa estranha", "words": []}],
        lang="pt",
    )
    db.insert_transcript(
        "youtube", "ty2",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0,
          "text": "cara que loucura carai", "words": []}],
        lang="pt",
    )
    # New corpus words need the rebuilt vocab (see absent-token test above).
    import time as _t

    db._load_vocab_uncached("transcripts", _t.monotonic())
    terms = dict(db._expand_query("estranheza", ["transcripts"]))
    assert terms.get("estranhesa") == 1, "raw dist-1 ASR variant kept"
    terms = dict(db._expand_query("caraio", ["transcripts"]))
    assert terms.get("carai") == 1, "raw dist-1 typo kept"
    # Raw dist-2 + shared 3-char prefix: 'estranheza'->'estranha' (same
    # word family, ASR variant) survives the admission gate.
    terms = dict(db._expand_query("estranheza", ["transcripts"]))
    assert terms.get("estranha") == 2, "raw dist-2 prefix stretch kept"


# --- title pass predicate -------------------------------------------------

def test_title_no_reverse_substring(scratch):
    """'caralho' must not match a title containing 'cara' or 'car': the old
    bidirectional-substring rule surfaced 'Pé na porta, I.A. na cara!' at
    score 1.0 above real fuzzy chat hits."""
    db.upsert_video({
        "platform": "youtube", "video_id": "tnoise", "channel": "chan",
        "title": "Pé na porta, I.A. na cara! | Gaveta", "canonical_key": "chan-tnoise",
    })
    db.upsert_video({
        "platform": "youtube", "video_id": "tgood", "channel": "chan",
        "title": "CARALHO O QUE ACONTECEU", "canonical_key": "chan-tgood",
    })
    hits = db.search("caralho", limit=20)
    title_hits = [h for h in hits if h["kind"] == "title"]
    texts = [h.get("text", "").lower() for h in title_hits]
    assert any("caralho o que aconteceu" in t for t in texts)
    assert not any("na cara" in t for t in texts), "reverse substring must not match"


def test_title_prefix_and_asr_variants(scratch):
    """Partial-word prefixes ('estranh' -> 'ESTRANHEZA') and dist-1 ASR
    variants ('estranheza' ~ 'estranhesa') still match titles."""
    db.upsert_video({
        "platform": "youtube", "video_id": "tp1", "channel": "chan",
        "title": "ESTRANHEZA TOTAL", "canonical_key": "chan-tp1",
    })
    db.upsert_video({
        "platform": "youtube", "video_id": "tp2", "channel": "chan",
        "title": "A ESTRANHESA DO DIA", "canonical_key": "chan-tp2",
    })
    for q in ("estranh", "estranheza"):
        hits = db.search(q, limit=20)
        title_texts = [h.get("text", "").lower() for h in hits if h["kind"] == "title"]
        assert any("estranheza total" in t for t in title_texts), q
        assert any("estranhesa do dia" in t for t in title_texts), q


# --- search-first vocab reload -------------------------------------------

def test_vocab_stale_served_and_background_rebuild(scratch, monkeypatch):
    """A corpus row-count change must not block the next search with the
    25k-row vocab rebuild: the stale vocab is served immediately and the
    rebuild lands in the background (generation bumps, cache updates)."""
    import threading
    import time as _t

    before = dict(db._expand_query("vale", ["transcripts"]))
    cached_count = db._vocab_cache["transcripts"][3]
    db.insert_transcript(
        "youtube", "vw",
        [{"seg_idx": 99, "start_sec": 99.0, "end_sec": 100.0,
          "text": "zorknovo surgiu aqui", "words": []}],
        lang="pt",
    )
    # Hold the background rebuild hostage so the stale window is
    # deterministic: the count-mismatch lookup must serve the cached vocab
    # while the rebuild thread is blocked, then release and watch it land.
    gate = threading.Event()
    orig = db._load_vocab_uncached

    def gated(table, now):
        gate.wait(10)
        return orig(table, now)

    monkeypatch.setattr(db, "_load_vocab_uncached", gated)
    db._expand_query("vale", ["transcripts"])  # count mismatch observed here
    assert db._vocab_cache["transcripts"][3] == cached_count, "stale served, not rebuilt inline"
    n = db.query("SELECT COUNT(*) AS n FROM transcripts")[0]["n"]
    assert n == cached_count + 1, "insert landed"
    gate.set()
    deadline = _t.monotonic() + 10.0
    while db._vocab_cache["transcripts"][3] != n and _t.monotonic() < deadline:
        _t.sleep(0.05)
    assert db._vocab_cache["transcripts"][3] == n, "background rebuild catches up"
    # The new word is reachable once the rebuilt vocab knows it.
    terms = dict(db._expand_query("zorknovo", ["transcripts"]))
    assert terms.get("zorknovo") == 0
    assert before  # sanity: the stale snapshot was non-empty
