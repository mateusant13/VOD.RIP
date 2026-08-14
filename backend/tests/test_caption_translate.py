"""Self-check for live-caption translation (caption_translate.py).

Run from the backend directory with:
    python -m pytest tests/test_caption_translate.py -q

Covers (all model I/O stubbed — no real SLID/NLLB inference in unit tests):
- The translate-gate decision (channel evidence, SLID majority vote,
  same-language pass-through).
- Segment cache behavior (repeat hit, LRU cap).
- Degrade paths: missing model dir, translation failure, detection failure
  all yield raw text / None (captions never block on translation).
- The _maybe_translate wiring used by LiveCaptioner._flush.
- NLLB hypothesis decode (forced target token + special-token stripping).
"""
from __future__ import annotations

import collections
import os

import pytest

from services import caption_translate as ct
from services import live_captions


# --- gate ------------------------------------------------------------------


def test_gate_evidence_wins():
    """Known channel language decides outright: different -> translate,
    same -> pass through; votes are irrelevant when evidence exists."""
    votes = collections.deque(["es", "es", "es", "es", "es"])
    assert ct.needs_translation("en", "pt", votes) is True
    assert ct.needs_translation("es", "pt", votes) is True
    assert ct.needs_translation("pt", "pt", votes) is False  # same family
    assert ct.needs_translation("pt", "es", votes) is True


def test_gate_no_evidence_needs_majority():
    """Without evidence, fewer than SLID_VOTE_MIN agreeing votes never flips
    the gate; a majority of a different family does; a majority of the app
    family never flips (a pt stream mislabelled es 1-2x stays raw)."""
    votes = collections.deque(["es", "es"])
    assert ct.needs_translation(None, "pt", votes) is False  # 2 < 3 of 5

    votes = collections.deque(["es", "es", "es"])
    assert ct.needs_translation(None, "pt", votes) is True

    votes = collections.deque(["es", "es", "pt", "pt", "pt"])
    assert ct.needs_translation(None, "pt", votes) is False  # majority = pt

    assert ct.needs_translation(None, "pt", collections.deque()) is False
    assert ct.needs_translation(None, "pt", collections.deque(["pt", "pt", "pt"])) is False


def test_gate_votes_slide_with_maxlen():
    """The vote deque is bounded (SLID_VOTE_SPAN) — old windows age out."""
    votes = collections.deque(["en"] * 5, maxlen=ct.SLID_VOTE_SPAN)
    assert ct.needs_translation(None, "pt", votes) is True
    votes.append("pt")
    votes.append("pt")
    votes.append("pt")
    # 3 newest are pt; only 2 en remain -> majority flips to pass-through
    assert ct.needs_translation(None, "pt", votes) is False


# --- families / tokens ------------------------------------------------------


def test_app_language_family_defaults_to_pt(monkeypatch):
    monkeypatch.setattr(ct, "app_language_family", lambda: "pt")
    assert ct.app_language_family() == "pt"
    assert ct.target_token("pt") == "por_Latn"
    assert ct.target_token("es") == "spa_Latn"
    assert ct.target_token("en") == "eng_Latn"
    assert ct.target_token("xx") == "por_Latn"  # unknown -> pt fallback


def test_model_dirs_presence_based(tmp_path, monkeypatch):
    """nllb_dir/slid_dir are pure presence checks under the models folder —
    no download, no inference; VODRIP_TRANSLATE_MODEL_DIR pins the root."""
    monkeypatch.setenv("VODRIP_TRANSLATE_MODEL_DIR", str(tmp_path))
    assert ct.nllb_dir() is None
    assert ct.slid_dir() is None

    nllb = tmp_path / ct.NLLB_SUBDIR
    nllb.mkdir()
    for f in ct._NLLB_FILES:
        (nllb / f).write_text("x")
    assert ct.nllb_dir() == nllb

    slid = tmp_path / ct.SLID_SUBDIR
    slid.mkdir()
    for f in ct._SLID_FILES:
        (slid / f).write_text("x")
    assert ct.slid_dir() == slid


def test_translate_dir_follows_override(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_TRANSLATE_MODEL_DIR", str(tmp_path / "m"))
    assert ct.translate_dir() == tmp_path / "m"


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("VODRIP_CAPTION_TRANSLATE", "0")
    assert ct.enabled() is False
    monkeypatch.setenv("VODRIP_CAPTION_TRANSLATE", "1")
    assert ct.enabled() is True


def test_translate_dir_memoized_per_env(tmp_path, monkeypatch):
    """P2-7: the env-keyed resolution is cached — the disk_inventory syscall
    ladder under whisper_cache_dir runs ONCE per env value, never on every
    ~2 s caption flush; an override key bypasses it entirely."""
    import services.disk_hygiene as dh

    ct._resolve_translate_dir.cache_clear()
    calls = {"n": 0}
    real = dh.whisper_cache_dir

    def counting_cache_dir():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(dh, "whisper_cache_dir", counting_cache_dir)
    monkeypatch.delenv("VODRIP_TRANSLATE_MODEL_DIR", raising=False)

    assert ct.translate_dir() == real() / "translate"
    assert ct.translate_dir() == real() / "translate"
    assert calls["n"] == 1, (
        "repeated translate_dir() must hit the cache, not the syscall ladder"
    )

    monkeypatch.setenv("VODRIP_TRANSLATE_MODEL_DIR", str(tmp_path / "m"))
    assert ct.translate_dir() == tmp_path / "m"
    assert calls["n"] == 1, "the override path must not touch whisper_cache_dir"


def test_nllb_cuda_failure_falls_back_to_cpu(tmp_path, monkeypatch):
    """P2-5: CUDA load fails (VRAM already held by whisper/parakeet) — the
    translator must fall back to CPU with a log, NOT flip the component
    broken for LOAD_RETRY_SEC (raw captions for 5 min)."""
    import sys
    import types

    devices_seen = []

    class _FakeTranslator:
        def __init__(self, path, device="cpu", compute_type=None):
            devices_seen.append(device)
            if device == "cuda":
                raise RuntimeError("CUDA out of memory: 0 bytes available")

    fake_ct2 = types.SimpleNamespace(
        get_cuda_device_count=lambda: 1,  # CUDA is "available"...
        Translator=_FakeTranslator,  # ...but the load fails
    )
    monkeypatch.setitem(sys.modules, "ctranslate2", fake_ct2)
    monkeypatch.setitem(
        sys.modules, "tokenizers",
        types.SimpleNamespace(
            Tokenizer=types.SimpleNamespace(from_file=lambda p: "tok")
        ),
    )
    d = tmp_path / ct.NLLB_SUBDIR
    d.mkdir()
    for f in ct._NLLB_FILES:
        (d / f).write_text("x")
    monkeypatch.setenv("VODRIP_TRANSLATE_MODEL_DIR", str(tmp_path))

    t = ct._CaptionTranslator()
    nllb = t._ensure_nllb()

    assert devices_seen == ["cuda", "cpu"], "CUDA tried first, CPU is the fallback"
    assert nllb is not None and nllb[2] == "cpu", (
        "the CPU fallback must succeed — not degrade to raw captions"
    )
    assert t._nllb_broken_at == 0.0, "no broken-state cooldown after a CUDA-only miss"


def test_nllb_cpu_failure_still_degrades(tmp_path, monkeypatch):
    """A CPU load failure (missing libs / corrupt checkpoint) is still a
    hard degrade — the fallback must not loop or mask real errors."""
    import sys
    import types

    class _FakeTranslator:
        def __init__(self, path, device="cpu", compute_type=None):
            raise OSError("libcudnn not found")

    monkeypatch.setitem(
        sys.modules, "ctranslate2",
        types.SimpleNamespace(get_cuda_device_count=lambda: 0, Translator=_FakeTranslator),
    )
    monkeypatch.setitem(
        sys.modules, "tokenizers",
        types.SimpleNamespace(
            Tokenizer=types.SimpleNamespace(from_file=lambda p: "tok")
        ),
    )
    d = tmp_path / ct.NLLB_SUBDIR
    d.mkdir()
    for f in ct._NLLB_FILES:
        (d / f).write_text("x")
    monkeypatch.setenv("VODRIP_TRANSLATE_MODEL_DIR", str(tmp_path))

    t = ct._CaptionTranslator()
    assert t._ensure_nllb() is None
    assert t._nllb_broken_at > 0.0, "CPU failure must arm the LOAD_RETRY_SEC cooldown"


# --- translator cache + degrade ---------------------------------------------


class _FakeNllb:
    """In-process NLLB stand-in: returns a deterministic translation."""

    def __init__(self, out="Ola mundo", fail=False):
        self.out = out
        self.fail = fail
        self.calls = 0

    def run(self, text, tgt):
        self.calls += 1
        if self.fail:
            return None  # mirrors _run_translate's internal catch -> degrade
        return self.out


def _make_translator(monkeypatch, fake):
    t = ct._CaptionTranslator()
    monkeypatch.setattr(t, "_ensure_nllb", lambda: ("tok", fake, "cpu"))
    monkeypatch.setattr(t, "_run_translate", fake.run)
    return t


def test_translate_caches_and_bounds(monkeypatch):
    fake = _FakeNllb()
    t = _make_translator(monkeypatch, fake)

    assert t.translate("hello", "pt") == "Ola mundo"
    assert t.translate("hello", "pt") == "Ola mundo"  # cache hit
    assert fake.calls == 1
    # different family -> different key -> new call
    assert t.translate("hello", "es") == "Ola mundo"
    assert fake.calls == 2

    # LRU cap: CACHE_MAX+50 unique texts through translate() keep the cache
    # bounded by CACHE_MAX
    fake.calls = 0
    for i in range(ct.CACHE_MAX + 50):
        t.translate(f"txt-{i}", "pt")
    assert len(t._cache) == ct.CACHE_MAX
    assert t.translate("", "pt") is None  # empty input never calls
    assert fake.calls == ct.CACHE_MAX + 50  # no extra call from the empty input


def test_translate_failure_returns_none(monkeypatch):
    fake = _FakeNllb(fail=True)
    t = _make_translator(monkeypatch, fake)
    assert t.translate("hello", "pt") is None  # degrade to raw


def test_detect_language_missing_model_returns_none(monkeypatch):
    t = ct._CaptionTranslator()
    monkeypatch.setattr(t, "_ensure_slid", lambda: None)
    assert t.detect_language(object()) is None


def test_detect_language_failure_returns_none(monkeypatch):
    class _BrokenSlid:
        def create_stream(self):
            return self

        def accept_waveform(self, *a):
            pass

        def compute(self, stream):
            raise RuntimeError("infer failed")

    t = ct._CaptionTranslator()
    monkeypatch.setattr(t, "_ensure_slid", lambda: _BrokenSlid())
    assert t.detect_language(object()) is None


def test_detect_language_returns_family(monkeypatch):
    class _FakeSlid:
        def create_stream(self):
            return self

        def accept_waveform(self, *a):
            pass

        def compute(self, stream):
            return "pt"

    t = ct._CaptionTranslator()
    monkeypatch.setattr(t, "_ensure_slid", lambda: _FakeSlid())
    assert t.detect_language(object()) == "pt"


# --- _maybe_translate wiring -------------------------------------------------


def _captioner():
    c = live_captions.LiveCaptioner.__new__(live_captions.LiveCaptioner)
    c.platform = "twitch"
    c.channel = "srdogg"
    c._evidence_family = None
    c._target_family = None  # no per-session ?lang= override → app language
    c._lang_votes = collections.deque(maxlen=ct.SLID_VOTE_SPAN)
    return c


def test_maybe_translate_passes_through_when_disabled(monkeypatch):
    monkeypatch.setattr(ct, "enabled", lambda: False)
    c = _captioner()
    assert live_captions._maybe_translate(c, "raw text", b"audio") == ("raw text", False)


def test_maybe_translate_passes_through_without_models(monkeypatch):
    monkeypatch.setattr(ct, "enabled", lambda: True)
    monkeypatch.setattr(ct, "nllb_dir", lambda: None)
    c = _captioner()
    assert live_captions._maybe_translate(c, "raw text", b"audio") == ("raw text", False)


def test_maybe_translate_same_language_evidence(monkeypatch):
    monkeypatch.setattr(ct, "enabled", lambda: True)
    monkeypatch.setattr(ct, "nllb_dir", lambda: object())
    monkeypatch.setattr(ct, "app_language_family", lambda: "pt")
    c = _captioner()
    c._evidence_family = "pt"  # stream known to be in the app language
    assert live_captions._maybe_translate(c, "fala em portugues", b"audio") == (
        "fala em portugues", False,
    )
    # no SLID call happened (evidence is known)
    assert len(c._lang_votes) == 0


def test_maybe_translate_translates_different_language(monkeypatch):
    monkeypatch.setattr(ct, "enabled", lambda: True)
    monkeypatch.setattr(ct, "nllb_dir", lambda: object())
    monkeypatch.setattr(ct, "app_language_family", lambda: "pt")
    monkeypatch.setattr(ct, "translate", lambda text, fam: f"PT({text})")
    c = _captioner()
    c._evidence_family = "en"
    out, translated = live_captions._maybe_translate(c, "hello world", b"audio")
    assert translated is True
    assert out == "PT(hello world)"


def test_maybe_translate_translate_failure_keeps_raw(monkeypatch):
    monkeypatch.setattr(ct, "enabled", lambda: True)
    monkeypatch.setattr(ct, "nllb_dir", lambda: object())
    monkeypatch.setattr(ct, "app_language_family", lambda: "pt")
    monkeypatch.setattr(ct, "translate", lambda text, fam: None)  # NLLB failed
    c = _captioner()
    c._evidence_family = "en"
    out, translated = live_captions._maybe_translate(c, "hello world", b"audio")
    assert translated is False
    assert out == "hello world"


def test_maybe_translate_slid_vote_flips_gate(monkeypatch):
    """No evidence -> SLID majority decides: 3 en votes translate, 2 do not."""
    monkeypatch.setattr(ct, "enabled", lambda: True)
    monkeypatch.setattr(ct, "nllb_dir", lambda: object())
    monkeypatch.setattr(ct, "slid_dir", lambda: object())
    monkeypatch.setattr(ct, "app_language_family", lambda: "pt")
    monkeypatch.setattr(ct, "detect_language", lambda audio: "en")
    monkeypatch.setattr(ct, "translate", lambda text, fam: f"PT({text})")

    c = _captioner()
    assert live_captions._maybe_translate(c, "hi", b"a") == ("hi", False)
    assert live_captions._maybe_translate(c, "hi", b"a") == ("hi", False)
    assert live_captions._maybe_translate(c, "hi", b"a") == ("PT(hi)", True)


# --- NLLB hypothesis decode --------------------------------------------------


def test_hypothesis_decode_strips_forced_target_and_specials(monkeypatch):
    """ctranslate2 emits the forced target token first and </s>/<unk> at the
    end; decode must strip both and restore sentencepiece ▁ as spaces."""
    class _Tok:
        def encode(self, text):
            return type("e", (), {"tokens": ["▁Hello", "▁world"]})()

    class _Tr:
        def translate_batch(self, src, target_prefix=None, **kw):
            return [type("r", (), {"hypotheses": [
                [target_prefix[0][0], "▁Ola", "▁mundo", ",", "▁este", "▁e", "▁um", "▁teste", "<unk>", "<unk>"],
            ]})()]

    t = ct._CaptionTranslator()
    monkeypatch.setattr(t, "_ensure_nllb", lambda: (_Tok(), _Tr(), "cpu"))
    assert t._run_translate("hello", "por_Latn") == "Ola mundo, este e um teste"


def test_hypothesis_decode_empty_ok(monkeypatch):
    class _Tok:
        def encode(self, text):
            return type("e", (), {"tokens": ["▁Hello"]})()

    class _Tr:
        def translate_batch(self, src, target_prefix=None, **kw):
            return [type("r", (), {"hypotheses": [["</s>"]]})()]

    t = ct._CaptionTranslator()
    monkeypatch.setattr(t, "_ensure_nllb", lambda: (_Tok(), _Tr(), "cpu"))
    assert t._run_translate("hello", "por_Latn") == ""  # never raises
