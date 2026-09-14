"""Relevance of a long colloquial query: real matches must outrank
stopword/fuzzy noise.

Regression (real archive, 3.0M transcript + 7.6M chat rows): searching
'dai a cesar o que é de cesaras' returned SEVEN chat messages and THIRTEEN
unrelated video titles, and zero transcripts — although FTS5 found 61
transcript segments mentioning César. Two independent defects:

1. The multi-token relevance floor compared the query's rarest tokens with
   row tokens tokenized by `re.findall(r"[^\\W_]+", text.casefold())`, which
   does NOT fold diacritics, while the FTS5 index IS diacritic-insensitive.
   The rows spelled 'César', so they matched the tier pattern and were then
   silently dropped by the floor: 60 of 61 real hits vanished.
2. The video-title pass scored coverage over EVERY 3+ char query token, so
   the stopword 'que' (corpus frequency 673k) gave 403 titles a score of 1/4
   and they filled ranks 11-60 of the result page.

All fixes are narrow. The floor compares TOKEN SETS in the same spelling
space the index used: an NFD accent-strip (the exact equivalent of FTS5's
unicode61 tokenizer, ç→c), NOT the phonetic _ACCENT_FOLD (which folds ç→s
and silently dropped every 'transcrição' row for a 'transcricao' query);
raw spellings are kept alongside, so the fold only ever gets more
permissive. Title COVERAGE is scored on content words with an all-stopword
query falling back to the old behaviour ('que' alone ranks exactly as
before), but `partial` reports coverage of EVERY typed token — a title
matching only 'vale' of 'vale meu' stays flagged partial even at score 1.0.
Fuzzy expansion is gated to seeds of ≥4 chars that are not stopwords:
'dai' must not drag the rare dist-1 transposition 'dia' into results, and
stopword seeds ('que', 'como', ...) expand into half the catalog. The
`exact` title branch and the phrase/AND content patterns are untouched: a
literal phrase still requires every word the user typed.

Run from backend/: python -m pytest tests/test_archive_search_relevance.py
"""
from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="archive-relevance-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")

from services import archive_db as db  # noqa: E402

QUERY = "dai a cesar o que é de cesaras"
CHAN = "relevancechan"

# Accented spellings are the point: FTS5 matches them for 'cesar', the old
# floor did not recognize them as carrying the token.
CESAR_SEGS = [
    ("t-cesar-1", "Miguel Gustavo César aqui. Aí, grande Gustavo César."),
    ("t-cesar-2", "Nosso amigo César. Quem diabo é? Quem diabo é César?"),
    ("t-cesar-3", "cinza. É o final do César."),
    ("t-cesar-4", "Alexandre Cesar, né? Ele tem canal no YouTube"),  # unaccented
]

# Chat rows that carry ONLY the common words of the query — the noise the
# relevance floor exists to cull (mirrors the real corpus, where 'que' and
# 'dai' are the two most frequent tokens of this query).
NOISE_MSGS = [f"o que dai meu brother {i}" for i in range(6)]

# Titles carrying nothing but stopwords of the query — the 403 that each
# scored 1/4 on the real archive. The 'zeta<i>' tail marks them.
NOISE_TITLES = [f"O QUE FALTA DE SKILL SOBRA NO CARISMA" for _ in range(20)]

# Enough rows that 'que'/'dai' clear _SUPPRESS_DIST1_FREQ and are therefore
# NOT rare enough to keep: corpus frequency, not length, decides what the
# floor and the title score consider a content word.
_COMMON_ROWS = db._SUPPRESS_DIST1_FREQ + 50


def _tokens(text: str) -> set[str]:
    """Tokenize the way the relevance floor does: \\W split of the casefolded
    text, then the NFD accent-strip the FTS5 index itself applies."""
    toks = set(re.findall(r"[^\W_]+", text.casefold()))
    return toks | {db._strip_diacritics(t) for t in toks}


def _carries_cesar(text: str) -> bool:
    return bool(_tokens(text) & {"cesar", "cesaras"})


@pytest.fixture()
def scratch(monkeypatch):
    """Corpus shaped like the real archive for this query: rare accented
    'cesar' content, common 'que'/'dai' chat noise, stopword-only titles."""
    monkeypatch.setenv(
        "VODRIP_ARCHIVE_DB",
        str(Path(tempfile.mkdtemp(prefix="rel-")) / "archive.db"),
    )
    for vid, _ in CESAR_SEGS:
        db.upsert_video({
            "platform": "youtube", "video_id": vid, "channel": CHAN,
            "title": "base", "started_at": "2026-08-01T12:00:00Z",
            "kind": "vod",
        })
        db.insert_transcript("youtube", vid, [{
            "seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0,
            "text": _, "words": [],
        }], lang="pt")

    # One video holding the flood of common-word rows: 1050 transcript
    # segments and 6 chat messages, none of them mentioning 'cesar'.
    db.upsert_video({
        "platform": "youtube", "video_id": "t-flood", "channel": CHAN,
        "title": "base", "started_at": "2026-08-01T12:00:00Z", "kind": "vod",
    })
    db.insert_transcript("youtube", "t-flood", [
        {"seg_idx": i, "start_sec": float(i), "end_sec": float(i + 1),
         "text": f"que dai meu brother {i}", "words": []}
        for i in range(_COMMON_ROWS)
    ], lang="pt")
    db.insert_messages("twitch", "t-flood", [
        {"offset_sec": float(i), "username": "viewer", "text": t}
        for i, t in enumerate(NOISE_MSGS)
    ])
    db.insert_messages("twitch", "t-cesar-1", [
        {"offset_sec": 1.0, "username": "viewer", "text": "césar?"},
        {"offset_sec": 2.0, "username": "viewer", "text": "CESAR NÃO"},
    ])
    for i, title in enumerate(NOISE_TITLES):
        db.upsert_video({
            "platform": "youtube", "video_id": f"n{i}", "channel": CHAN,
            "title": f"{title} zeta{i}", "started_at": "2026-08-01T12:00:00Z",
            "kind": "vod",
        })
    # Seeds for the review-lens pins: 'dia' is the rare dist-1
    # transposition of 'dai' (fuzzy expansion must never pull it in), and
    # 'transcrição' exercises the index-equivalent floor fold. The
    # stopword-bearing title probes the full-token `partial` rule.
    db.upsert_video({
        "platform": "twitch", "video_id": "t-dia-noise", "channel": CHAN,
        "title": "base", "started_at": "2026-08-01T12:00:00Z", "kind": "vod",
    })
    db.insert_messages("twitch", "t-dia-noise", [
        {"offset_sec": 1.0, "username": "viewer",
         "text": "bom dia gente pessoal"},
    ])
    db.upsert_video({
        "platform": "youtube", "video_id": "t-transc", "channel": CHAN,
        "title": "base", "started_at": "2026-08-01T12:00:00Z", "kind": "vod",
    })
    db.insert_transcript("youtube", "t-transc", [{
        "seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0,
        "text": "A transcrição do jogo", "words": [],
    }], lang="pt")
    db.upsert_video({
        "platform": "youtube", "video_id": "t-vale-tudo", "channel": CHAN,
        "title": "VALE DE TUDO", "started_at": "2026-08-01T12:00:00Z",
        "kind": "vod",
    })
    # The request path no longer builds the vocab inline (a cold search serves
    # exact tokens and rebuilds in the background); these tests assert the
    # floor and coverage contracts, which need a warm snapshot.
    for table in ("transcripts", "messages"):
        db._load_vocab_uncached(table, time.monotonic())
    return db


def _freq_of(token: str) -> int:
    total = 0
    for table in ("transcripts", "messages"):
        for bucket in (db._load_vocab(table) or {}).values():
            total += sum(n for term, n in bucket if term == token)
    return total


def test_fixture_mirrors_the_real_token_frequencies(scratch):
    """Guard the rest of the file: 'que'/'dai' common, 'cesar' rare."""
    assert _freq_of("que") > db._SUPPRESS_DIST1_FREQ
    assert _freq_of("dai") > db._SUPPRESS_DIST1_FREQ
    assert 0 < _freq_of("cesar") <= db._SUPPRESS_DIST1_FREQ
    assert _freq_of("cesaras") == 0


def test_accented_cesar_transcripts_survive_the_floor(scratch):
    """The 60-rows-vanishing bug: every 'cesar' transcript segment must reach
    the page, not just the one spelled without the accent."""
    hits = db.search(QUERY, limit=50)
    transcripts = {h["video_id"] for h in hits if h["kind"] == "transcript"}
    for vid, _ in CESAR_SEGS:
        assert vid in transcripts, (
            f"{vid} dropped by the relevance floor; got {sorted(transcripts)}"
        )


def test_cesar_content_leads_and_noise_never_appears(scratch):
    """Every surfaced row carries the rare token: neither the chat rows that
    match only 'que'/'dai' nor the stopword-only titles are evidence."""
    hits = db.search(QUERY, limit=20)
    assert hits
    for rank, h in enumerate(hits, 1):
        assert _carries_cesar(h["text"]), (
            f"rank {rank} carries no content word of the query: "
            f"{h['text'][:60]!r}"
        )
    assert not any("zeta" in h["text"] for h in hits), (
        "stopword-only title must not surface for this query"
    )
    assert not any("brother" in h["text"] for h in hits), (
        "chat row matching only common words must not surface"
    )


def test_titles_carrying_the_rare_token_still_match(scratch):
    """Excluding stopwords narrows the denominator, it does not disable the
    title pass: a title with the query's content word still surfaces."""
    db.upsert_video({
        "platform": "youtube", "video_id": "t-cesar-title", "channel": CHAN,
        "title": "O CESAR e o que aconteceu",
        "started_at": "2026-08-01T12:00:00Z", "kind": "vod",
    })
    hits = db.search(QUERY, limit=30)
    title_hits = [h for h in hits if h["kind"] == "title"]
    assert any(_carries_cesar(h["text"]) for h in title_hits), title_hits
    # 'dai', 'cesar' and 'cesaras' are the content words; this title has one
    # of the three -> coverage 1/3 (the pre-fix denominator counted 'que' too,
    # reporting the same title as 1/4 noise).
    hit = next(h for h in title_hits if _carries_cesar(h["text"]))
    assert hit["partial"] is True
    assert abs(hit["score"] - 1 / 3) < 1e-9, hit["score"]


def test_exact_title_phrase_still_requires_every_word(scratch):
    """The `exact` branch keeps the unfiltered token list: a literal title
    phrase must carry the stopwords too — nothing loosens here."""
    db.upsert_video({
        "platform": "youtube", "video_id": "t-phrase", "channel": CHAN,
        "title": "vale da estranheza",
        "started_at": "2026-08-01T12:00:00Z", "kind": "vod",
    })
    hits = db.search("vale da estranheza", mode="exact", limit=20)
    assert any(
        h["kind"] == "title" and "vale da estranheza" in h["text"]
        and h["partial"] is False and h["score"] == 1.0
        for h in hits
    ), [h["text"] for h in hits]
    rows = db._titles_search(
        "vale o da estranheza", 10, q_freq={}, platforms=[], video_id=None,
        channel=None, kinds=[], date_from=None, date_to=None, exact=True,
    )
    assert not any("vale da estranheza" in r["text"] for r in rows), (
        "an exact query with an extra word must not match the phrase title"
    )


def test_all_stopword_query_keeps_today_ranking(scratch):
    """'que' alone is all-stopword: the content-word set falls back to the
    full token list, so the pass keeps working with its old shape — every
    matching title scores 1/1 and is not flagged partial."""
    assert db.search("que", limit=20), "an all-stopword query must still hit"
    rows = db._titles_search(
        "que", 20, q_freq={}, platforms=[], video_id=None, channel=None,
        kinds=[], date_from=None, date_to=None,
    )
    assert rows, "an all-stopword query must still match titles"
    for h in rows:
        assert h["score"] == 1.0, h
        assert h["partial"] is False, h
    assert any("zeta" in h["text"] for h in rows)


def test_transcricao_query_survives_floor_via_index_equivalent_fold(scratch):
    """Review lens MUST-FIX 1: the floor fold must be the index fold. The
    FTS5 unicode61 tokenizer strips marks into the BASE letter (ç→c), so
    'transcricao' really does match 'A transcrição do jogo' — but the
    phonetic _ACCENT_FOLD maps ç→s, and the floor then dropped the row the
    tiers had just found. The 'que' anchor makes this a multi-token query:
    a single-token query skips the floor entirely, so without it this test
    would not exercise the regression."""
    hits = db.search("que transcricao", limit=20)
    vids = {h["video_id"] for h in hits if h["kind"] == "transcript"}
    assert "t-transc" in vids, (
        f"FTS-matched accented row dropped by the floor; got {sorted(vids)}"
    )


def test_short_seed_does_not_expand_into_rare_neighbor(scratch):
    """Review lens MUST-FIX 2: 'dai' is 3 chars; its dist-1 transposition
    'dia' (a different word) must never ride the fuzzy tier into the
    results — and on this corpus nothing else pushes the 'dia' row out of
    the page, because the 1050 'dai' rows all belong to ONE video and the
    per-video cap leaves ranks 4+ free."""
    hits = db.search("dai", limit=20)
    assert hits
    assert not any(
        _tokens(h["text"]) == _tokens("bom dia gente pessoal")
        for h in hits
    ), [h["text"] for h in hits]
    # Owner's phrasing, on the original query too.
    for h in db.search(QUERY, limit=20):
        assert _carries_cesar(h["text"]), h["text"]


def test_title_partial_flag_uses_full_query_coverage(scratch):
    """Review lens MUST-FIX 3: 'VALE DE TUDO' covers the only CONTENT word
    of 'vale meu' (score 1.0 by design) but not the stopword 'meu' — so it
    is a PARTIAL match. Promoting it to partial=False made stopword-light
    titles outrank genuine full-coverage content hits in the merge sort."""
    rows = db._titles_search(
        "vale meu", 10, q_freq={}, platforms=[], video_id=None,
        channel=None, kinds=[], date_from=None, date_to=None,
    )
    hit = next(r for r in rows if r["text"] == "VALE DE TUDO")
    assert hit["score"] == 1.0, hit
    assert hit["partial"] is True, hit
    # A title carrying both tokens stays complete.
    db.upsert_video({
        "platform": "youtube", "video_id": "t-vale-meu", "channel": CHAN,
        "title": "MEU VALE", "started_at": "2026-08-01T12:00:00Z",
        "kind": "vod",
    })
    rows = db._titles_search(
        "vale meu", 10, q_freq={}, platforms=[], video_id=None,
        channel=None, kinds=[], date_from=None, date_to=None,
    )
    full = next(r for r in rows if r["text"] == "MEU VALE")
    assert full["partial"] is False, full
