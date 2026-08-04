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
