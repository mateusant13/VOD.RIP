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
import sqlite3
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


# ---------------------------------------------------------------- F5.2
def test_exact_skips_fuzzy_bigram_work(_seeded, monkeypatch):
    """F5.2 exact fast-path: exact + transcript-in-scope must NOT walk the
    fuzzy expansion machinery (_token_expansions / _load_bigrams) nor call
    _fuzzy_pattern — the OR pattern is discarded by the exact override, so
    those COUNT(*) probes on million-row tables are pure waste. Split-phrase
    span recall for vocab-absent tokens is still preserved via the
    span_exact_tokens probe (asserted separately)."""
    import time as _t

    # A transcript corpus where 'vale'/'estranheza' are present but a term
    # the exact probe needs is absent, so the span gate has real work.
    archive_db.insert_transcript(
        "youtube", "f5-span",
        [
            {"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "todo mundo cai no vale"},
            {"seg_idx": 1, "start_sec": 1.0, "end_sec": 2.0, "text": "da estranheza quando fala"},
        ],
    )
    archive_db.upsert_video({
        "platform": "youtube", "video_id": "f5-span", "channel": "gaveta",
        "title": "F5 span", "started_at": "2026-08-01T12:00:00Z", "kind": "vod",
    })
    # Re-prime the vocab so the split-phrase span gate has data to probe.
    archive_db._load_vocab_uncached("transcripts", _t.monotonic())

    bigram_calls: list = []
    orig_bigrams = archive_db._load_bigrams

    def counting_bigrams(tables):
        bigram_calls.append(tables)
        return orig_bigrams(tables)

    monkeypatch.setattr(archive_db, "_load_bigrams", counting_bigrams)
    orig_fuzzy = archive_db._fuzzy_pattern

    def no_fuzzy(*a, **k):
        raise AssertionError("_fuzzy_pattern must not run on the exact fast path")

    monkeypatch.setattr(archive_db, "_fuzzy_pattern", no_fuzzy)

    hits = _exact("vale da estranheza", source="transcript", video_id="f5-span", limit=1000)
    # Split phrase across adjacent segments must still be found (span gate).
    span = next(
        (h for h in hits if h["video_id"] == "f5-span" and h["kind"] == "transcript"),
        None,
    )
    assert span is not None, "exact split-phrase span recall must survive the fast path"
    assert "estranheza" in span["text"].casefold()
    # And the fuzzy bigram machinery must NOT have been touched.
    assert bigram_calls == [], f"_load_bigrams ran on the exact fast path: {bigram_calls}"


# ---------------------------------------------------------------- F5.3
def test_rowcount_cache_reflects_writes(_seeded):
    """F5.3: the row-count cache is flushed by content writes, so a search
    immediately after insert_transcript / insert_messages sees the new rows
    (no 2s stale cache serving a pre-insert vocab)."""
    # Clear the cache so the baseline probes below are REAL counts, not
    # whatever a prior test left cached (module-scoped DB: counts grow).
    archive_db._rowcount_cache.clear()

    n0 = archive_db._table_row_count("transcripts")
    assert "transcripts" in archive_db._rowcount_cache
    archive_db.insert_transcript(
        "youtube", "rc-vid",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0,
          "text": "frase unica e deterministica para contagem de transcricao rc38"}],
        lang="pt",
    )
    # The write hook popped the cache entry -> next probe re-counts.
    assert "transcripts" not in archive_db._rowcount_cache
    assert archive_db._table_row_count("transcripts") == n0 + 1

    n1 = archive_db._table_row_count("messages")
    assert "messages" in archive_db._rowcount_cache
    archive_db.insert_messages(
        "youtube", "rc-vid",
        [{"offset_sec": 1.0, "username": "u", "text": "mensagem unica e deterministica rc38"}],
    )
    assert "messages" not in archive_db._rowcount_cache
    assert archive_db._table_row_count("messages") == n1 + 1


# ---------------------------------------------------------------- F5.1
async def test_search_runs_in_worker_thread(monkeypatch):
    """F5.1: the search handler must run archive_db.search in a non-main
    thread via asyncio.to_thread (Rule 2026-09-13: no sync sqlite inside an
    async handler). Proves the running thread is not the event-loop/main
    thread, matching the existing asyncio handler test style."""
    from routers.archive import archive_search

    called_in = {"thread": None}
    orig = archive_db.search

    def tracked(*a, **k):
        import threading as _th
        called_in["thread"] = _th.current_thread()
        return orig(*a, **k)

    monkeypatch.setattr(archive_db, "search", tracked)
    resp = await archive_search(q="vale da estranheza", limit=20, source="both")
    assert "hits" in resp
    t = called_in["thread"]
    assert t is not None, "archive_db.search must have been called"
    assert t is not threading_main(), "search must run off the main thread"
    assert t.name != "MainThread", "search must run off the main thread"


def threading_main():
    import threading as _th
    return _th.main_thread()


# ---------------------------------------------------------------- F6
def test_load_vocab_refuses_fts_suffix_table():
    """F6: _load_vocab_uncached must refuse a table ending in '_fts' — the
    broken live-DB messages_fts_vocab was built by feeding the FTS index
    name, which formed a virtual table referencing a non-existent
    '{table}_fts_fts'. The guard returns the 'vocab unavailable' fallback
    (None) instead of poisoning the schema."""
    import time as _t
    assert archive_db._load_vocab_uncached("messages_fts", _t.monotonic()) is None
    assert archive_db._load_vocab_uncached("transcripts_fts", _t.monotonic()) is None
    # Real content tables still work.
    assert archive_db._load_vocab_uncached("messages", _t.monotonic()) is not None


def test_orphan_messages_fts_vocab_dropped_on_schema_ensure():
    """F6: the orphaned broken messages_fts_vocab view (references the
    non-existent messages_fts_fts) is DROPPED at schema-ensure, so the
    name is reclaimed and the regression query stops raising."""
    conn = archive_db.get_conn()
    with conn:
        # Reproduce the live-DB orphan: the broken view, whose construction
        # the guard now prevents.
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_vocab "
            "USING fts5vocab(messages_fts_fts, 'row')"
        )
    # Re-run schema-ensure (idempotent path) so the DROP executes.
    with archive_db._lock:
        with archive_db._init_lock:
            archive_db._schema_ready = False
    archive_db._ensure_schema_ready()
    # The orphan must be gone -> the query now raises no such table (not the
    # old 'no such fts5 table' referencing the phantom _fts index).
    with pytest.raises(sqlite3.OperationalError) as exc:
        archive_db.query("SELECT count(*) AS n FROM messages_fts_vocab")
    assert "no such table" in str(exc.value).lower()
    assert "messages_fts_vocab" in str(exc.value)


# ---------------------------------------------------------------- F-qfreq
def test_exact_chat_only_substring_branch_disabled():
    """Gate F-qfreq: the F5.2 skip leaves `q_freq = {}` when EXACT mode has no
    content loop to walk (chat-only, and video-only — `loops` is empty there,
    so the walk iterates nothing). An empty dict must NOT read as "every token
    is absent/rare" (freq 0 <= _PREFIX_GATE_FREQ), or the >=4-char substring
    branch of the exact post-filter would open to any token: exact acceptance
    with unknown frequencies is token EQUALITY / _tok_eq only.

    Pin (1) FAILS PRE-FIX: `source="video"` exact reaches the post-filter with
    q_freq={}, while `_titles_search` (which gets the same q_freq) accepts the
    title "valeuzaoextra" for query "valeuzao" through its own ungated
    substring branch. Pre-fix `_exact_covers` had no q_freq_known term, so the
    substring hit survived and the video was returned. Post-fix the
    post-filter is the rejecting component: the row must not come back.

    Pin (2) is a reachability INVARIANT (honest caveat): the chat row holding
    only "valeuzaoextra" never reaches `merged` at all — EXACT chat/transcript
    candidates come from the quoted full-phrase FTS MATCH (span pass is
    capped at _SPAN_MAX_TOKENS=8), which requires every typed token as an
    exact contiguous token, so a substring-only row is unreachable both pre-
    and post-fix. It is asserted because it is the contract the gate protects
    (and it fails if the FTS phrase or the post-filter ever loosens), with the
    equality positive control guarding the over-strict direction.

    Pin (3) DOCUMENTS WHICH WAY IT LANDS with frequencies known and pins it:
    transcript+video scope walks the vocab, 'valeuzao' is present at freq 1
    (<= _PREFIX_GATE_FREQ), so the gated substring branch is ALIVE and the
    "valeuzaoextra" title IS accepted. This leg is pre/post-neutral (freq 1
    passed the same gate before the fix); it exists to prove pin (1) is the
    q_freq_known term and not a wholesale removal of substring reach.
    """
    import time as _t

    q = "valeuzao"  # 8 chars >= 4, substring of the planted longer word
    assert archive_db._tok_eq("valeuzao", "valeuzaoextra") is False

    def _v(vid: str, title: str) -> None:
        archive_db.upsert_video({
            "platform": "twitch", "video_id": vid, "channel": "pinexact",
            "title": title, "started_at": "2026-08-01T12:00:00Z", "kind": "vod",
        })

    _v("px-equality", "valeuzao obrigado")      # literal token -> must match
    _v("px-substring", "valeuzaoextra obrigado")  # substring-only -> gate pin
    # Chat-only leg: the long word lives on a video whose title carries no
    # token containing 'valeuzao' (the post-filter folds text + title).
    _v("px-chat-neutral", "titulo sem qualquer coincidencia")
    archive_db.insert_messages(
        "twitch", "px-chat-neutral",
        [{"offset_sec": 1.0, "username": "pa1", "text": "valeuzaoextra obrigado"}],
    )
    archive_db.insert_messages(
        "twitch", "px-equality",
        [{"offset_sec": 2.0, "username": "pa2", "text": "valeuzao obrigado"}],
    )
    archive_db.insert_transcript(
        "twitch", "px-substring",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0,
          "text": "valeuzaoextra obrigado"}],
        lang="pt",
    )
    # Prime the vocabularies so the content scopes have KNOWN frequencies
    # (cold _load_vocab serves None + a background rebuild).
    archive_db._load_vocab_uncached("transcripts", _t.monotonic())
    archive_db._load_vocab_uncached("messages", _t.monotonic())

    # (1) video-only: q_freq = {} -> substring title must NOT be accepted.
    vids = {h["video_id"] for h in _exact(q, source="video", channel="pinexact", limit=100)}
    assert "px-equality" in vids
    assert "px-substring" not in vids, (
        "F-qfreq: empty q_freq read as freq-0 and opened the substring branch"
    )

    # (2) chat-only: equality only.
    chat = _exact(q, source="chat", channel="pinexact", limit=100)
    assert "px-equality" in {h["video_id"] for h in chat}
    assert "px-chat-neutral" not in {h["video_id"] for h in chat}

    # (3) transcript in scope -> frequencies known -> gated branch alive.
    both = {h["video_id"] for h in _exact(q, source="both", channel="pinexact", limit=100)}
    assert "px-substring" in both, (
        "substring reach must survive for a low-freq token when q_freq is known"
    )
    assert "px-equality" in both


# ------------------------------------------------- F-exact-token-truncation
_NATO20 = [
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
    "india", "juliett", "kilo", "lima", "mike", "november", "oscar", "papa",
    "quebec", "romeo", "sierra", "tango",
]


def test_exact_long_query_no_truncation():
    """Gate F-exact-token-truncation: EXACT literal-phrase acceptance is no
    longer capped at _TITLES_MAX_TOKENS (16) — neither in `_titles_search`'s
    exact branch (`q_tokens = q_folded`) nor in the global post-filter
    (`exact_q = _fold_tokens(q)`). A 20-token query must impose all 20
    requirements; pre-fix tokens 17-20 were dropped by the [:16] slice, so a
    title carrying only the first 16 (or 17) tokens was accepted as a full
    match.

    The token set is pairwise non-overlapping: no query token is a substring
    of another, none is within edit distance 1 of another, so neither the
    substring branch nor _tok_eq can rescue a missing token — the only way to
    cover token 17 is to carry it (asserted below).

    FAILS PRE-FIX on the two partial-title rows. Honest scope note: video
    titles are the ONLY surface where a candidate can be missing tokens 17+
    (chat/transcript rows arrive through the quoted full-phrase FTS MATCH,
    which already requires every token contiguously — the span pass is capped
    at _SPAN_MAX_TOKENS=8), so this pins the uncapping of the title pass; the
    post-filter's own uncapping is not independently observable for a
    >16-token query and is asserted here indirectly.
    """
    q = " ".join(_NATO20)
    assert len(_NATO20) == 20
    for i, a in enumerate(_NATO20):
        for j, b in enumerate(_NATO20):
            if i != j:
                assert a not in b, (a, b)
                assert archive_db._tok_eq(a, b) is False, (a, b)

    def _v(vid: str, title: str) -> None:
        archive_db.upsert_video({
            "platform": "youtube", "video_id": vid, "channel": "pinlong",
            "title": title, "started_at": "2026-08-01T12:00:00Z", "kind": "vod",
        })

    _v("pl-full", q)                                    # all 20 -> must match
    _v("pl-first16", " ".join(_NATO20[:16]))            # pre-fix full match
    _v("pl-first17", " ".join(_NATO20[:17]))            # pre-fix full match
    archive_db.insert_transcript(
        "youtube", "pl-full",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 4.0, "text": q}],
        lang="en",
    )

    for source in ("video", "both"):
        vids = {h["video_id"] for h in _exact(q, source=source, channel="pinlong", limit=100)}
        assert "pl-full" in vids, source
        assert "pl-first16" not in vids, (
            f"F-exact-token-truncation ({source}): tokens past 16 imposed no requirement"
        )
        assert "pl-first17" not in vids, (
            f"F-exact-token-truncation ({source}): token 17 was inside the [:16] cap"
        )

    # Positive control on the content side: a row carrying all 20 tokens is
    # still accepted (the uncapped requirement set must not over-reject).
    tr = _exact(q, source="transcript", channel="pinlong", limit=100)
    assert [h["video_id"] for h in tr if h["kind"] == "transcript"] == ["pl-full"], tr
    assert all(h["partial"] is False for h in tr), tr