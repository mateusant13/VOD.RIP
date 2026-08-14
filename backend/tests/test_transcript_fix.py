"""Champion-name transcript post-fix — gazetteer, banded matching, choke point.

Pure-logic tests against a synthetic (but realistic) ddragon-shaped gazetteer
(no network), plus the wired choke point in archive_transcribe
(*_transcribe_audio_source row-build loop, parakeet confidence threading)
and the captions strong-only path. Per the approved gate spec:
blocklist regressions, window tests, punctuation reattachment, join==text,
idempotency, numeral variants, empty-words no-op, parakeet log-prob
aggregation, offline no-op, stats counter.
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

_FIX_DB = str(Path(tempfile.mkdtemp(prefix="transcript-fix-")) / "archive.db")
_FIX_APP = str(Path(tempfile.mkdtemp(prefix="transcript-fix-app-")))
os.environ["VODRIP_ARCHIVE_DB"] = _FIX_DB
os.environ["VODRIP_APP_DATA"] = _FIX_APP

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import archive_db, archive_transcribe as at, transcript_fix  # noqa: E402

# Synthetic gazetteer in the live-ddragon shape: {locale: {key: name}}.
_NAMES: dict[str, dict[str, str]] = {
    "pt_BR": {
        "Diana": "Diana", "Senna": "Senna", "Graves": "Graves", "Mel": "Mel",
        "Twitch": "Twitch", "Sona": "Sona", "Aurora": "Aurora",
        "Nautilus": "Nautilus", "Lux": "Lux", "Vi": "Vi", "Caitlyn": "Caitlyn",
        "Evelynn": "Evelynn", "Lillia": "Lillia", "Katarina": "Katarina",
        "Nunu": "Nunu e Willump", "JarvanIV": "Jarvan IV",
        "DrMundo": "Dr. Mundo", "MissFortune": "Miss Fortune",
        "LeBlanc": "LeBlanc", "BelVeth": "Bel'Veth", "MasterYi": "Master Yi",
        "AurelionSol": "Aurelion Sol", "LeeSin": "Lee Sin",
        "XinZhao": "Xin Zhao", "RenataGlasc": "Renata Glasc",
        "TahmKench": "Tahm Kench", "TwistedFate": "Twisted Fate",
    },
    "en_US": {
        "Nunu": "Nunu & Willump", "DrMundo": "Dr. Mundo",
        "MasterYi": "Master Yi",
    },
    "es_ES": {
        "Nunu": "Nunu y Willump", "MasterYi": "Maestro Yi",
    },
}


def _words(*items: tuple[str, float, float]) -> list[dict]:
    return [{"word": w, "start": s, "end": e} for w, s, e in items]


def _seg(text: str, words: list[dict]) -> dict:
    return {"text": text, "words": words}


@pytest.fixture(scope="module", autouse=True)
def _scratch_db():
    """Rebind the shared archive conn to THIS module's scratch DB."""
    prev_db = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = _FIX_DB
    archive_db._schema_ready = False
    yield
    if prev_db is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev_db
    archive_db._schema_ready = False


@pytest.fixture
def fixer(monkeypatch):
    """Inject the synthetic gazetteer as the process-global fixer."""
    f = transcript_fix.ChampionFixer(_NAMES)
    monkeypatch.setattr(transcript_fix, "_fixer", f)
    monkeypatch.setattr(transcript_fix, "_fix_failed", False)
    monkeypatch.delenv("VODRIP_TRANSCRIPT_FIX", raising=False)
    monkeypatch.delenv("VODRIP_TRANSCRIPT_FIX_CONF", raising=False)
    return f


def _fix(segment: dict, *, engine: str = "whisper", language: str | None = None,
         stats: dict | None = None) -> bool:
    return transcript_fix.fix_segment(
        segment, engine=engine, language=language, stats=stats,
    )


# --- normalization + banded distance ---------------------------------------

def test_normalization_forms():
    assert transcript_fix._split_edges("diana,") == ("", "diana", ",")
    assert transcript_fix._split_edges("(diana)") == ("(", "diana", ")")
    assert transcript_fix._split_edges("K'Sante") == ("", "K'Sante", "")
    assert transcript_fix._norm_core("Bel'Veth", keep_hyphens=False) == "belveth"
    assert transcript_fix._norm_core("K'Sante", keep_hyphens=False) == "ksante"
    assert transcript_fix._norm_core("NUNU", keep_hyphens=False) == "nunu"
    assert transcript_fix._norm_core("Renatá", keep_hyphens=False) == "renata"
    assert transcript_fix._norm_core("bem-vindo", keep_hyphens=False) == "bemvindo"
    assert transcript_fix._norm_core("bem-vindo", keep_hyphens=True) == "bem-vindo"
    assert transcript_fix._map_token("iv") == "4"
    assert transcript_fix._map_token("quatro") == "4"
    assert transcript_fix._map_token("four") == "4"
    assert transcript_fix._map_token("doutor") == "dr"
    assert transcript_fix._map_token("mestre") == "master"
    assert transcript_fix._map_token("mundial") == "mundial"  # no partial alias


def test_banded_levenshtein():
    assert transcript_fix._banded("diana", "diana", 1) == 0
    assert transcript_fix._banded("diana", "Diana", 1) == 1, "case-sensitive; norm upstream"
    assert transcript_fix._banded("dianna", "diana", 1) == 1
    assert transcript_fix._banded("diana", "diiana", 1) == 1  # one insert
    assert transcript_fix._banded("diana", "diiiana", 1) is None  # two inserts
    assert transcript_fix._banded("diana", "diiiana", 2) == 2
    assert transcript_fix._banded("sena", "senna", 1) == 1
    assert transcript_fix._banded("kitten", "sitting", 2) is None
    assert transcript_fix._banded("", "ab", 2) == 2
    assert transcript_fix._banded("abc", "", 1) is None  # length diff > band


# --- strong path: single tokens --------------------------------------------

def test_single_token_strong_path(fixer):
    seg = _seg("diana joga", _words(("diana", 0.0, 0.5), ("joga", 0.6, 1.0)))
    assert _fix(seg) is True
    assert seg["text"] == "Diana joga"
    assert seg["words"][0]["word"] == "Diana"
    assert seg["words"][1]["word"] == "joga"  # not a champion: untouched


def test_single_token_dist1_correction(fixer):
    seg = _seg("a dianna venceu", _words(("a", 0.0, 0.2), ("dianna", 0.3, 0.8), ("venceu", 0.9, 1.4)))
    assert _fix(seg) is True
    assert seg["text"] == "a Diana venceu"


def test_dist0_case_only_collisions_stay_unblocked(fixer):
    # dist-0 case-only name collisions that are NOT blocklisted stay
    # unblocked — the replacement is harmless (canonical ddragon casing).
    for word in ("diana", "katarina", "evelynn", "lillia", "caitlyn", "senna"):
        seg = _seg(word, _words((word, 0.0, 0.5)))
        assert _fix(seg) is True, word
        assert seg["words"][0]["word"] == word.capitalize(), word


# --- blocklist regressions --------------------------------------------------

def test_blocklist_strong_path_never_replaces(fixer):
    for word in ("mel", "vi", "graves", "sena", "twitch", "mundo", "sol",
                 "aurora", "sona", "bardo", "eco", "milho", "karma", "set",
                 "atroz", "rise", "ash", "pike", "vain", "lock", "kale",
                 "brand", "rumble", "jinx", "kindred", "talon", "smolder",
                 "singed", "briar", "bard", "nautilus", "lux",
                 "caitlin", "evelyn", "lilia", "nico", "dana", "lena",
                 "sonia", "elisa"):
        seg = _seg(word, _words((word, 0.0, 0.5)))
        assert _fix(seg) is False, f"{word} must not be replaced via strong path"
        assert seg["words"][0]["word"] == word


def test_blocklisted_sena_replaced_only_via_weak_path_low_conf(fixer):
    high = _seg("a sena e boa", _words(
        ("a", 0.0, 0.2), ("sena", 0.3, 0.7), ("e", 0.8, 0.9), ("boa", 1.0, 1.3)))
    high["words"][1]["conf"] = 0.9
    assert _fix(high) is False, "high confidence must keep 'sena' (real PT word)"
    low = _seg("a sena e boa", _words(
        ("a", 0.0, 0.2), ("sena", 0.3, 0.7), ("e", 0.8, 0.9), ("boa", 1.0, 1.3)))
    low["words"][1]["conf"] = 0.2
    stats = transcript_fix.new_stats()
    assert _fix(low, engine="parakeet", stats=stats) is True
    assert low["text"] == "a Senna e boa"
    assert stats["weak_replaced"] == 1 and stats["strong_replaced"] == 0


def test_blocked_hits_counter(fixer):
    stats = transcript_fix.new_stats()
    seg = _seg("mel graves", _words(("mel", 0.0, 0.4), ("graves", 0.5, 1.0)))
    assert _fix(seg, stats=stats) is False
    assert stats["blocked_hits"] == 2, "both words had dist<=1 candidates blocked"
    assert stats["segments_touched"] == 0


# --- weak path: dist 2 -----------------------------------------------------

def test_weak_path_dist2_low_conf(fixer):
    low = _seg("o leblnk venceu", _words(
        ("o", 0.0, 0.2), ("leblnk", 0.3, 0.9), ("venceu", 1.0, 1.4)))
    low["words"][1]["conf"] = 0.3
    assert _fix(low, engine="whisper") is True
    assert low["text"] == "o LeBlanc venceu"
    high = _seg("o leblnk venceu", _words(
        ("o", 0.0, 0.2), ("leblnk", 0.3, 0.9), ("venceu", 1.0, 1.4)))
    high["words"][1]["conf"] = 0.9
    assert _fix(high, engine="whisper") is False, "confident dist-2 stays untouched"


def test_weak_path_needs_confidence(fixer):
    seg = _seg("leblnk", _words(("leblnk", 0.0, 0.9)))
    assert _fix(seg, engine="whisper") is False, "no conf -> weak path never fires"
    assert _fix(seg, engine="captions") is False
    seg2 = _seg("leblnk", _words(("leblnk", 0.0, 0.9)))
    seg2["words"][0]["conf"] = 0.3
    assert _fix(seg2, engine="captions") is False, "captions are never weak-path eligible"


def test_conf_env_override(fixer, monkeypatch):
    seg = _seg("sena", _words(("sena", 0.0, 0.5)))
    seg["words"][0]["conf"] = 0.55
    # conf 0.55 < 0.9 override -> weak path fires even though the default
    # whisper threshold is 0.6 (0.55 < 0.6 would fire anyway; the override
    # is proven by raising the bar to 0.9 and still firing).
    monkeypatch.setenv("VODRIP_TRANSCRIPT_FIX_CONF", "0.9")
    assert _fix(seg) is True
    assert seg["words"][0]["word"] == "Senna"


# --- multi-token windows ----------------------------------------------------

def test_window_matches(fixer):
    cases = [
        ("doutor mundo", "Dr. Mundo", "pt"),
        ("doctor mundo", "Dr. Mundo", "en"),
        ("miss fortune", "Miss Fortune", "pt"),
        ("jarvan quatro", "Jarvan IV", "pt"),
        ("nunu e willump", "Nunu e Willump", "pt"),
        ("le blanc", "LeBlanc", "pt"),
        ("aurelion sol", "Aurelion Sol", "pt"),
        ("lee sin", "Lee Sin", "pt"),
        ("xin zhao", "Xin Zhao", "pt"),
        ("tahm kench", "Tahm Kench", "pt"),
        ("renata glasc", "Renata Glasc", "pt"),
        ("twisted fate", "Twisted Fate", "pt"),
        ("mestre yi", "Master Yi", "pt"),
        ("maestro yi", "Maestro Yi", "es"),
    ]
    for phrase, canon, lang in cases:
        seg = _seg(phrase, _words(
            *[(w, i * 0.4, i * 0.4 + 0.35) for i, w in enumerate(phrase.split())]))
        stats = transcript_fix.new_stats()
        assert _fix(seg, language=lang, stats=stats) is True, phrase
        assert seg["text"] == canon, phrase
        assert len(seg["words"]) == 1, phrase  # window collapses to one word
        assert seg["words"][0]["start"] == 0.0
        assert seg["words"][0]["end"] == (len(phrase.split()) - 1) * 0.4 + 0.35
        assert stats["strong_replaced"] == 1, phrase


def test_window_single_token_glued_belveth(fixer):
    seg = _seg("a belveth top", _words(("a", 0.0, 0.2), ("belveth", 0.3, 0.9), ("top", 1.0, 1.3)))
    assert _fix(seg) is True
    assert seg["text"] == "a Bel'Veth top"


def test_window_glued_jarvan_singles(fixer):
    for word, canon in (("jarvaniv", "Jarvan IV"), ("jarvan4", "Jarvan IV"),
                        ("drmundo", "Dr. Mundo"), ("nunuewillump", "Nunu e Willump")):
        seg = _seg(word, _words((word, 0.0, 1.0)))
        assert _fix(seg, language="pt") is True, word
        assert seg["words"][0]["word"] == canon, word


def test_window_longest_first_and_consumed_spans(fixer):
    # 'nunu e willump' (3 tokens) must win over the en 2-token form at the
    # same start; consumed words never reach the single-token pass.
    seg = _seg("nunu e willump", _words(
        ("nunu", 0.0, 0.3), ("e", 0.3, 0.4), ("willump", 0.4, 0.9)))
    assert _fix(seg, language="pt") is True
    assert seg["text"] == "Nunu e Willump"
    # en form when the pt 3-token shape is absent
    seg2 = _seg("nunu willump", _words(("nunu", 0.0, 0.4), ("willump", 0.4, 0.9)))
    assert _fix(seg2, language="en") is True
    assert seg2["text"] == "Nunu & Willump"


def test_numeral_variants(fixer):
    for numeral in ("iv", "4", "quatro", "four"):
        seg = _seg(f"jarvan {numeral}", _words(
            ("jarvan", 0.0, 0.4), (numeral, 0.5, 0.9)))
        assert _fix(seg) is True, numeral
        assert seg["text"] == "Jarvan IV", numeral


def test_window_punctuation(fixer):
    seg = _seg("doutor mundo,", _words(("doutor", 0.0, 0.4), ("mundo,", 0.5, 0.9)))
    assert _fix(seg) is True
    assert seg["text"] == "Dr. Mundo,"
    seg2 = _seg("(doutor mundo)", _words(("(doutor", 0.0, 0.4), ("mundo)", 0.5, 0.9)))
    assert _fix(seg2) is True
    assert seg2["text"] == "(Dr. Mundo)"


def test_window_exempt_from_stopwords_and_len(fixer):
    # 'e' (stopword) and 'yi' (len 2, blocklisted component) inside windows
    seg = _seg("nunu e willump e master yi", _words(
        ("nunu", 0.0, 0.3), ("e", 0.3, 0.4), ("willump", 0.4, 0.7),
        ("e", 0.8, 0.9), ("master", 1.0, 1.4), ("yi", 1.5, 1.7)))
    assert _fix(seg, language="pt") is True
    assert seg["text"] == "Nunu e Willump e Master Yi"


# --- punctuation reattachment + join contract + idempotency ----------------

def test_punctuation_reattachment(fixer):
    for raw, want in (("diana,", "Diana,"), ("(diana)", "(Diana)"),
                      ("diana!", "Diana!"), ("—diana—", "—Diana—")):
        seg = _seg(raw, _words((raw, 0.0, 0.5)))
        assert _fix(seg) is True, raw
        assert seg["text"] == want, raw


def test_join_equals_text_after_correction(fixer):
    seg = _seg("eu jogo de diana, hoje", _words(
        ("eu", 0.0, 0.2), ("jogo", 0.3, 0.6), ("de", 0.7, 0.8),
        ("diana,", 0.9, 1.3), ("hoje", 1.4, 1.8)))
    assert _fix(seg) is True
    assert seg["text"] == "eu jogo de Diana, hoje"
    assert seg["text"] == " ".join(w["word"] for w in seg["words"])


def test_idempotency(fixer):
    already = _seg("Diana joga", _words(("Diana", 0.0, 0.5), ("joga", 0.6, 1.0)))
    assert _fix(already) is False, "already-canonical segment is a no-op"
    assert already["text"] == "Diana joga"
    seg = _seg("jarvan quatro", _words(("jarvan", 0.0, 0.4), ("quatro", 0.5, 0.9)))
    assert _fix(seg) is True
    assert _fix(seg) is False, "second pass over corrected text must be a no-op"
    assert seg["text"] == "Jarvan IV"
    canonical = _seg("Jarvan IV", _words(("Jarvan", 0.0, 0.4), ("IV", 0.5, 0.9)))
    assert _fix(canonical) is False
    assert canonical["text"] == "Jarvan IV", "no-op window must not corrupt the text"


def test_empty_words_noop(fixer):
    seg = _seg("sem palavras", [])
    assert _fix(seg) is False
    assert seg["text"] == "sem palavras"


def test_join_mismatch_skipped(fixer):
    # words that do not reconstruct the text (captions without full inline
    # timestamps) are never touched — the join(words) == text contract holds.
    seg = _seg("diana aqui", _words(("diana", 0.0, 0.5)))
    assert _fix(seg) is False
    assert seg["text"] == "diana aqui"
    assert seg["words"][0]["word"] == "diana"


def test_untouched_segment_byte_identical(fixer):
    seg = _seg("nada de campeoes aqui", _words(
        ("nada", 0.0, 0.3), ("de", 0.4, 0.5), ("campeoes", 0.6, 1.1), ("aqui", 1.2, 1.5)))
    assert _fix(seg) is False
    assert seg["text"] == "nada de campeoes aqui"
    assert [w["word"] for w in seg["words"]] == ["nada", "de", "campeoes", "aqui"]


# --- stats counter ----------------------------------------------------------

def test_stats_counter(fixer):
    stats = transcript_fix.new_stats()
    seg = _seg("diana e sena e leblnk", _words(
        ("diana", 0.0, 0.3), ("e", 0.4, 0.5), ("sena", 0.6, 0.9),
        ("e", 1.0, 1.1), ("leblnk", 1.2, 1.7)))
    seg["words"][2]["conf"] = 0.2   # sena: weak (blocklisted, low conf)
    seg["words"][4]["conf"] = 0.3   # leblnk: weak (dist 2, low conf)
    assert _fix(seg, engine="whisper", stats=stats) is True
    assert seg["text"] == "Diana e Senna e LeBlanc"
    assert stats == {"segments_touched": 1, "strong_replaced": 1,
                     "weak_replaced": 2, "blocked_hits": 1}


# --- env toggle -------------------------------------------------------------

def test_env_toggle_disables_fix(fixer, monkeypatch):
    monkeypatch.setenv("VODRIP_TRANSCRIPT_FIX", "0")
    seg = _seg("diana", _words(("diana", 0.0, 0.5)))
    assert _fix(seg) is False
    assert seg["text"] == "diana"


def test_env_toggle_off_values(fixer, monkeypatch):
    for v in ("0", "false", "no", "off"):
        monkeypatch.setenv("VODRIP_TRANSCRIPT_FIX", v)
        assert transcript_fix.enabled() is False, v
    monkeypatch.delenv("VODRIP_TRANSCRIPT_FIX")
    assert transcript_fix.enabled() is True


# --- gazetteer fetch + cache (offline) -------------------------------------

def test_offline_noop_empty_gazetteer(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript_fix, "_fetch_gazetteer_names", lambda: None)
    monkeypatch.setattr(transcript_fix, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(transcript_fix, "_fixer", None)
    monkeypatch.setattr(transcript_fix, "_fix_failed", False)
    monkeypatch.delenv("VODRIP_TRANSCRIPT_FIX", raising=False)
    assert transcript_fix.get_fixer() is None
    assert transcript_fix.get_fixer() is None, "failed load must latch (fetch ONCE per process)"
    seg = _seg("diana", _words(("diana", 0.0, 0.5)))
    assert _fix(seg) is False
    assert seg["text"] == "diana"
    assert not (tmp_path / "gazetteer.json").exists(), "failed fetch must not write a cache"


def test_fresh_cache_skips_fetch(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(transcript_fix, "_fetch_gazetteer_names", lambda: calls.append(1) or _NAMES)
    monkeypatch.setattr(transcript_fix, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(transcript_fix, "_fixer", None)
    monkeypatch.setattr(transcript_fix, "_fix_failed", False)
    # seed a fresh cache
    path = tmp_path / "gazetteer.json"
    path.write_text(json.dumps(
        {"version": "16.16.1", "fetched_at": time.time(), "names": _NAMES},
    ), encoding="utf-8")
    f1 = transcript_fix.get_fixer()
    f2 = transcript_fix.get_fixer()
    assert f1 is not None and f1 is f2
    assert calls == [], "fresh cache must be used without a fetch"
    seg = _seg("diana", _words(("diana", 0.0, 0.5)))
    assert _fix(seg) is True and seg["text"] == "Diana"


def test_stale_cache_refetches_and_falls_back(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(transcript_fix, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(transcript_fix, "_fixer", None)
    monkeypatch.setattr(transcript_fix, "_fix_failed", False)
    path = tmp_path / "gazetteer.json"
    path.write_text(json.dumps(
        {"version": "16.16.1", "fetched_at": time.time() - 8 * 86400, "names": _NAMES},
    ), encoding="utf-8")
    # fetch failure -> stale cache served (fix still works, never raises)
    monkeypatch.setattr(transcript_fix, "_fetch_gazetteer_names", lambda: calls.append(1) or None)
    f = transcript_fix.get_fixer()
    assert f is not None, "stale cache must be served on fetch failure"
    assert len(calls) == 1
    # fresh cache: no refetch even when stale-age check would refire
    path.write_text(json.dumps(
        {"version": "16.16.1", "fetched_at": time.time(), "names": _NAMES},
    ), encoding="utf-8")
    monkeypatch.setattr(transcript_fix, "_fixer", None)
    monkeypatch.setattr(transcript_fix, "_fix_failed", False)
    assert transcript_fix.get_fixer() is not None
    assert len(calls) == 1, "fresh cache must skip the fetch"


def test_get_fixer_single_flight(tmp_path, monkeypatch):
    calls = []
    start = threading.Barrier(4)  # all threads race in together

    def fake_fetch():
        calls.append(1)
        time.sleep(0.3)  # hold the lock so the other threads pile up
        return _NAMES

    monkeypatch.setattr(transcript_fix, "_fetch_gazetteer_names", fake_fetch)
    monkeypatch.setattr(transcript_fix, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(transcript_fix, "_fixer", None)
    monkeypatch.setattr(transcript_fix, "_fix_failed", False)
    results: list = []

    def worker():
        start.wait()
        results.append(transcript_fix.get_fixer())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(calls) == 1, "single-flight: concurrent callers must share one fetch"
    assert all(r is results[0] for r in results), "all callers must get the same fixer"


# --- parakeet confidence aggregation ---------------------------------------

def test_parakeet_logprob_aggregation_negative():
    toks = [" N", "eg", "an", " de", " ", "1", "0", " minut", "os", ",", " né", "?"]
    ts = [0.32, 0.48, 0.56, 0.8, 1.04, 1.12, 1.12, 1.2, 1.28, 1.36, 2.08, 2.24]
    lps = [-0.2, -0.4, -0.6, -0.1, -0.1, -0.1, -0.1, -0.3, -0.5, -0.2, -0.3, -0.7]
    words = at._parakeet_words(toks, ts, lps)
    assert [w["word"] for w in words] == ["Negan", "de 10", "minutos,", "né?"]
    # mock values are negative; conf = exp(mean of each word's token log-probs)
    assert words[0]["start"] == 0.32 and words[0]["end"] == 0.56
    assert abs(words[0]["conf"] - math.exp(-0.4)) < 1e-9
    assert abs(words[1]["conf"] - math.exp(-0.1)) < 1e-9
    assert abs(words[2]["conf"] - math.exp(-1 / 3)) < 1e-9
    assert abs(words[3]["conf"] - math.exp(-0.5)) < 1e-9


def test_parakeet_logprob_costs_inverted():
    # sherpa may emit COSTS (positive) — the sign convention is inverted so
    # the confidence stays in (0, 1].
    words = at._parakeet_words([" N", "eg"], [0.32, 0.48], [0.2, 0.4])
    assert abs(words[0]["conf"] - math.exp(-0.3)) < 1e-9


def test_parakeet_logprob_absent_keeps_legacy_shape():
    words = at._parakeet_words([" N", "eg"], [0.32, 0.48])
    assert words == [{"word": "Neg", "start": 0.32, "end": 0.48}], (
        "no log-probs (old sherpa) -> no conf key, weak path silently off")


def test_parakeet_batch_threads_ys_log_probs():
    class _Result:
        text = "diana sena"
        tokens = [" d", "iana", " s", "ena"]
        timestamps = [0.1, 0.3, 0.6, 0.9]
        ys_log_probs = [-0.1, -0.3, -0.2, -0.4]

    class _Stream:
        result = _Result()

        def accept_waveform(self, samples, sample_rate):
            pass

    class _Rec:
        def create_stream(self):
            return _Stream()

        def decode_stream(self, stream):
            pass

    import numpy as np
    out = at._transcribe_batch_parakeet(_Rec(), np.zeros(16000), [(0.0, 2.0)], "pt")
    words = out[0][0][0]["words"]
    assert [w["word"] for w in words] == ["diana", "sena"]
    assert abs(words[0]["conf"] - math.exp(-0.2)) < 1e-9
    assert abs(words[1]["conf"] - math.exp(-0.3)) < 1e-9


# --- choke point: _transcribe_audio_source row-build loop ------------------

def _fake_audio_source(tmp_path, monkeypatch, *, batch_out) -> dict:
    """Drive _transcribe_audio_source with a patched parakeet batch; returns stats."""
    import numpy as np
    monkeypatch.setattr(at, "decode_audio", lambda path: np.zeros(16000 * 4, dtype=np.float32))
    monkeypatch.setattr(at, "vad_speech_seconds", lambda audio: [(0.0, 4.0)])
    monkeypatch.setattr(at, "_job_engine", lambda lang: "parakeet")
    monkeypatch.setattr(at, "_parakeet_model", lambda: object())
    monkeypatch.setattr(at, "_parakeet_batch_size", lambda: 1)
    monkeypatch.setattr(at, "_read_manifest", lambda path: (None, {}))
    monkeypatch.setattr(at, "_write_manifest_header", lambda path, chunks: None)
    monkeypatch.setattr(at, "_append_manifest_entry", lambda path, ci, first, count: None)
    monkeypatch.setattr(at, "_transcribe_batch_parakeet", batch_out)
    return at._transcribe_audio_source(
        "twitch", "vid-fix", str(tmp_path / "x.wav"), None, None, None,
        time.monotonic(), sharded=False, shard_dir=None,
    )


def test_choke_point_parakeet_corrects_and_merges_stats(tmp_path, monkeypatch, fixer):
    """The ONE engine's confidence threading: parakeet log-prob confs reach
    fix_segment at the choke point (weak path fires at conf < 0.5)."""
    def batch_out(model, audio, chunks, language, **kw):
        return [([{
            "start_sec": 0.0, "end_sec": 2.0,
            "text": "diana sena",
            "words": [
                {"word": "diana", "start": 0.0, "end": 0.7, "conf": 0.9},
                {"word": "sena", "start": 0.8, "end": 1.3, "conf": 0.2},
            ],
        }], "pt")]

    stats = _fake_audio_source(tmp_path, monkeypatch, batch_out=batch_out)
    rows = archive_db.transcript_for("twitch", "vid-fix", raw=True)
    assert len(rows) == 1
    assert rows[0]["text"] == "Diana Senna", "weak path fires with the threaded low conf"
    stored_words = json.loads(rows[0]["words_json"])
    assert stored_words[0] == {"word": "Diana", "start": 0.0, "end": 0.7, "conf": 0.9}
    assert stats["transcript_fix"] == {"segments_touched": 1, "strong_replaced": 1,
                                       "weak_replaced": 1, "blocked_hits": 1}


def test_choke_point_fix_disabled(tmp_path, monkeypatch, fixer):
    monkeypatch.setenv("VODRIP_TRANSCRIPT_FIX", "0")

    def batch_out(model, audio, chunks, language, **kw):
        return [([{
            "start_sec": 0.0, "end_sec": 2.0,
            "text": "diana sena",
            "words": [
                {"word": "diana", "start": 0.0, "end": 0.7},
                {"word": "sena", "start": 0.8, "end": 1.3},
            ],
        }], "pt")]

    stats = _fake_audio_source(tmp_path, monkeypatch, batch_out=batch_out)
    rows = archive_db.transcript_for("twitch", "vid-fix", raw=True)
    assert rows[0]["text"] == "diana sena", "toggle off -> rows byte-identical"
    assert "transcript_fix" not in stats


# --- captions strong-only path ---------------------------------------------

def test_captions_strong_only(fixer):
    seg = _seg("diana sena", _words(("diana", 0.0, 0.7), ("sena", 0.8, 1.3)))
    seg["words"][0]["conf"] = 0.99  # strong path ignores confidence anyway
    seg["words"][1]["conf"] = 0.1   # weak path must NEVER fire for captions
    stats = transcript_fix.new_stats()
    assert _fix(seg, engine="captions", stats=stats) is True
    assert seg["text"] == "Diana sena"
    assert seg["words"][1]["word"] == "sena", "blocklisted + low conf stays (captions = strong only)"
    assert stats == {"segments_touched": 1, "strong_replaced": 1,
                     "weak_replaced": 0, "blocked_hits": 1}


def test_captions_segment_without_words_skipped(fixer):
    seg = _seg("diana sem timestamps", [])
    assert _fix(seg, engine="captions") is False
    assert seg["text"] == "diana sem timestamps"


def test_captions_join_mismatch_preserved(fixer):
    # YouTube auto-captions with untagged words: words do not reconstruct the
    # text -> the whole segment is skipped, no partial rewrite.
    seg = _seg("diana jogou muito", _words(("diana", 0.0, 0.5)))
    assert _fix(seg, engine="captions") is False
    assert seg["text"] == "diana jogou muito"
    assert seg["words"][0]["word"] == "diana"


def test_archive_ytdlp_captions_wiring(tmp_path, monkeypatch, fixer):
    """The captions path in ingest_video must fix segments before insert and
    report its stats under report['transcript_fix'] (strong path only)."""
    from services import archive_ytdlp

    class _FakeYdl:
        def extract_info(self, url, download=False):
            return {
                "id": "capvid", "title": "partida rankeada",
                "channel": "canal teste", "timestamp": 1767225600,
                "duration": 60, "webpage_url": "https://youtu.be/capvid",
            }

    class _FakeGate:
        def __enter__(self):
            return _FakeYdl()

        def __exit__(self, *exc):
            return False

    def fake_guarded(outdir, video_id=None):
        return _FakeGate()

    def fake_captions(ydl, info, **kw):
        return [("pt", "vtt", "fake-vtt")]

    def fake_parse(fmt, data):
        seg = _seg("diana sena", _words(("diana", 0.0, 0.7), ("sena", 0.8, 1.3)))
        seg["seg_idx"] = 0
        seg["start_sec"] = 0.0
        seg["end_sec"] = 1.3
        seg["words"][0]["conf"] = 0.99
        seg["words"][1]["conf"] = 0.1  # weak path must never fire for captions
        return [seg]

    monkeypatch.setattr(archive_ytdlp, "_guarded_youtube_dl", fake_guarded)
    monkeypatch.setattr(archive_ytdlp, "_fetch_captions", fake_captions)
    monkeypatch.setattr(archive_ytdlp, "_parse_caption", fake_parse)
    report = archive_ytdlp.ingest_video("https://youtu.be/capvid", temp_dir=tmp_path)
    assert report["transcript_segments"] == 1
    rows = archive_db.transcript_for("youtube", "capvid", raw=True)
    assert len(rows) == 1
    assert rows[0]["text"] == "Diana sena", "captions fixed before insert"
    stored_words = json.loads(rows[0]["words_json"])
    assert stored_words[1]["word"] == "sena", "blocklisted low-conf stays (captions = strong only)"
    assert report["transcript_fix"] == {"segments_touched": 1, "strong_replaced": 1,
                                        "weak_replaced": 0, "blocked_hits": 1}
