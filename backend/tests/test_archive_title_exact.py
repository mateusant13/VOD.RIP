"""Title exact-mode search correctness regressions (research doc F1 + F2).

Covers three distinct bugs from the doc:

- F1: the exact-mode title prefilter probed `lower(title) LIKE ?` with an
  already accent-folded query token against a raw (only-lowercased) column,
  so accented titles never reached the Python walk -> 0 hits for kick
  'derrota flexões', twitch 'investigação póstuma', youtube 'gráficos
  ruins…' / 'Edição de vídeo nos GAMES'. The fix drops the LIKE prefilter;
  the Python walk folds BOTH sides.
- F2a: the exact acceptance required a CONTIGUOUS needle ("needle in
  ' '.join(toks)"), so 'games vida' never matched the scattered-token title
  "TOP 10 GAMES da vida!" and 'estranheza games' never matched
  "Gráficos ruins e o vale da estranheza nos games". Fixed to token
  COVERAGE (every query token covered).
- F2b: the global exact post-filter required the contiguous phrase over
  text+title, cutting cross-segment / repeated lines. Fixed to token
  coverage.

The literal-phrase invariant is preserved and asserted: a query with an
EXTRA word must NOT match a phrase title (every typed token still must be
covered).

Requires a scratch DB; run from backend/:
    python -m pytest tests/test_archive_title_exact.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="archive-title-exact-"))
_DB = _TMP / "archive.db"

os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)

from services import archive_db  # noqa: E402  (env must be set first)


@pytest.fixture(scope="module", autouse=True)
def _search_scratch_db():
    """Rebind the global connection to THIS module's scratch DB so title
    tests stay isolated from any other module's env clobbering."""
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


# Real Gaveta titles from the live H: archive (verified 2026-09-15).
def _seed_videos() -> None:
    rows = [
        # platform, video_id, channel, title, kind
        ("kick", "kick-derrota", "lubu", "As piores DERROTAS nas FLEXÕES", "vod"),
        ("twitch", "tw-investig", "lubu", "Investigação Póstuma do Mistério", "vod"),
        ("youtube", "rq7SIAPbgX4", "gaveta",
         "Gráficos ruins e o vale da estranheza nos games | Gaveta #shorts", "short"),
        ("youtube", "KY-53BOuWDc", "gaveta", "Edição de vídeo nos GAMES | Gaveta", "vod"),
        ("youtube", "TbxMVi3m5Rc", "gaveta", "TOP 10 GAMES da vida!", "vod"),
        # A phrase title to pin the literal-phrase invariant.
        ("youtube", "phrase-vid", "gaveta", "O Vale da Estranheza completo", "vod"),
    ]
    for platform, video_id, channel, title, kind in rows:
        archive_db.upsert_video({
            "platform": platform,
            "video_id": video_id,
            "channel": channel,
            "title": title,
            "started_at": "2026-08-01T12:00:00Z",
            "kind": kind,
        })


@pytest.fixture(scope="module")
def _seeded():
    _seed_videos()


def _exact(q: str, **kw):
    return archive_db.search(q, mode="exact", **kw)


# ---------------------------------------------------------------- F1
def test_exact_accents_kick_title_hit(_seeded):
    # kick title with accented FLEXÕES: pre-fix LIKE prefilter folded the
    # query token but not the column -> 0 hits.
    hits = _exact("derrota flexões", source="video", platform="kick")
    kinds = {h["kind"] for h in hits}
    vids = {h["video_id"] for h in hits}
    assert "kick-derrota" in vids
    assert "title" in kinds


def test_exact_accents_twitch_title_hit(_seeded):
    hits = _exact("investigação póstuma", source="video", platform="twitch")
    assert "tw-investig" in {h["video_id"] for h in hits}


def test_exact_accent_youtube_graficos_ruins(_seeded):
    # 'gráficos ruins e o vale da estranheza' — the doc's canonical regressions.
    hits = _exact("gráficos ruins e o vale da estranheza", source="video", channel="gaveta")
    assert "rq7SIAPbgX4" in {h["video_id"] for h in hits}


def test_exact_accent_youtube_edicao_games(_seeded):
    hits = _exact("video nos games", source="video", channel="gaveta")
    assert "KY-53BOuWDc" in {h["video_id"] for h in hits}


# ---------------------------------------------------------------- F2a
def test_exact_token_coverage_games_vida(_seeded):
    # 'games vida' (scattered tokens) must match "TOP 10 GAMES da vida!".
    for q in ("games vida", "vida games"):
        hits = _exact(q, source="video", channel="gaveta")
        assert "TbxMVi3m5Rc" in {h["video_id"] for h in hits}, q
        # every typed token covered -> partial=False, score 1.0
        h = next(x for x in hits if x["video_id"] == "TbxMVi3m5Rc")
        assert h["partial"] is False
        assert h["score"] == 1.0


def test_exact_token_coverage_estranheza_games(_seeded):
    # 'estranheza games' must match rq7SIAPbgX4 (phrase split across the
    # title's middle, not contiguous).
    hits = _exact("estranheza games", source="video", channel="gaveta")
    assert "rq7SIAPbgX4" in {h["video_id"] for h in hits}


def test_exact_token_coverage_graficos_ruins_vale(_seeded):
    hits = _exact("graficos ruins vale", source="video", channel="gaveta")
    assert "rq7SIAPbgX4" in {h["video_id"] for h in hits}


# ---------------------------------------------------- invariant preserved
def test_exact_phrase_extra_word_still_rejected(_seeded):
    # A phrase title must still require EVERY typed word: adding a word the
    # title lacks must drop it (doc F2 keeps the literal-phrase invariant).
    hits = _exact("o vale da estranheza completo inexistente", source="video", channel="gaveta")
    assert "phrase-vid" not in {h["video_id"] for h in hits}


def test_exact_phrase_stopwords_required(_seeded):
    # 'o vale da estranheza' — stopwords 'o'/'da' are filtered to len>=3
    # from q_tokens but plain coverage must still match the phrase title.
    hits = _exact("vale da estranheza", source="video", channel="gaveta")
    assert "phrase-vid" in {h["video_id"] for h in hits}


# ---------------------------------------------------------------- F2b
def test_exact_source_both_keeps_title_and_transcript(_seeded):
    # seed a transcript line for the phrase video so source=both merges it.
    archive_db.insert_transcript(
        "youtube", "phrase-vid",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 2.0, "text": "o vale da estranheza completo"}],
    )
    hits = _exact("vale da estranheza", source="both", channel="gaveta")
    kinds = {h["kind"] for h in hits}
    assert "transcript" in kinds
    assert "title" in kinds


# ---------------------------------------------------------------- F2c
# Real Gaveta "vale da estranheza" transcript rows from the live H: archive
# (verified 2026-09-15). Pairs like (76.789, 76.799) are YouTube auto-caption
# full + partial cues: DIFFERENT text a few ms apart, where one is a substring
# of the other. The pre-F2c collapse dropped the shorter (truncated) sibling,
# hiding legitimate repeat mentions. The doc's exhaustive truth counts both
# members (25 rows across 4 videos; e.g. 6xK0oVBaV0Q "76.8(×2)").
_F2C_SEGS = {
    "6xK0oVBaV0Q": [
        (62, 73.479, "Cheque, ele tá despertando um sentimento parecido com o que o vale da estranheza"),
        (63, 76.789, "parecido com o que o vale da estranheza"),
        (64, 76.799, "parecido com o que o vale da estranheza desperta nas pessoas. vale da"),
        (197, 212.640, "acho que ele não cai no tal do vale da estranheza, que eu tinha falado do"),
        (241, 253.799, "muito horrorosos, muito estranhos, assim, vale da estranheza, Lord Farquad,"),
        (242, 256.030, "assim, vale da estranheza, Lord Farquad,"),
        (243, 256.040, "assim, vale da estranheza, Lord Farquad, nossa, como ele ele é feito para ser"),
        (521, 537.360, "ele mudou. E eu acho que esse é o tal do Vale da estranheza que eles criaram,"),
        (522, 538.829, "Vale da estranheza que eles criaram,"),
        (523, 538.839, "Vale da estranheza que eles criaram, porque a gente tá vendo uma animação com"),
    ],
    "Mzybv0Yme-A": [
        (217, 229.879, "bem bonito, bem gostoso no vale da estranheza, se encher o vale da"),
        (218, 231.309, "estranheza, se encher o vale da"),
        (219, 231.319, "estranheza, se encher o vale da estranheza de água e fala assim"),
        (479, 469.599, "mas sem usar efeitos especiais. Quer ver um outro exemplo de Vale da estranheza?"),
        (480, 471.350, "um outro exemplo de Vale da estranheza?"),
        (481, 471.360, "um outro exemplo de Vale da estranheza? E esse esse é ofensivo"),
        (944, 916.839, "escolha artística terrível, que ele realmente entra no vale da estranheza,"),
        (945, 918.230, "realmente entra no vale da estranheza,"),
        (946, 918.240, "realmente entra no vale da estranheza, ele ele entra num problema que é o"),
    ],
    "S4ZB3xTaFDc": [
        (715, 729.440, "que é pior ainda, assim como o vale da estranheza, se você bota um negócio que"),
        (729, 743.240, "da sua consciência, do seu cérebro, que é onde vive o Vale da Estranheza, ele"),
        (730, 745.030, "é onde vive o Vale da Estranheza, ele"),
        (731, 745.040, "é onde vive o Vale da Estranheza, ele entra em circuito, ele"),
    ],
    "wsKyA8ifjMI": [
        (537, 681.079, "Para mim eles entram mais no vale da estranheza. Eu não consigo lembrar de"),
        (541, 685.120, "nenhum Pokémon que que entre no Vale da estranheza. Todos eles são muito"),
        (4487, 5625.360, "coisa com barba se cando no Vale da estranheza. Eu eu fiz um react disso."),
    ],
}


def test_exact_transcript_substring_pairs_survive(_seeded):
    """F2c: transcript rows that are substring pairs at ~same moment (whisper/
    auto-caption full+partial cues a few ms apart) are legitimate DISTINCT
    mentions. The pre-fix collapse dropped the shorter sibling; now each of
    the 25 real 'vale da estranheza' rows must surface in exact+both."""
    for vid, segs in _F2C_SEGS.items():
        archive_db.upsert_video({
            "platform": "youtube", "video_id": vid, "channel": "gaveta",
            "title": f"Video {vid}", "started_at": "2026-08-01T12:00:00Z", "kind": "vod",
        })
        archive_db.insert_transcript(
            "youtube", vid,
            [{"seg_idx": si, "start_sec": st, "end_sec": st + 1.5, "text": tx}
             for si, st, tx in segs],
        )
    hits = _exact("vale da estranheza", source="both", channel="gaveta", limit=1000)
    tr = [h for h in hits if h["kind"] == "transcript"]
    from collections import Counter
    by_video = Counter(h["video_id"] for h in tr)
    # Doc §0 exhaustive truth: 10, 8, 4, 3 rows per video — each distinct
    # transcript row (incl. the substring pairs) must survive the collapse.
    assert by_video["6xK0oVBaV0Q"] == 10, by_video
    assert by_video["Mzybv0Yme-A"] == 8, by_video
    assert by_video["S4ZB3xTaFDc"] == 4, by_video
    assert by_video["wsKyA8ifjMI"] == 3, by_video