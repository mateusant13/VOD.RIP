"""Live-caption speech translation — NLLB-200 MT (ctranslate2 int8) + whisper-tiny SLID gating.

The live captioner runs parakeet ASR (auto-language) over 2 s windows. When
the stream's language differs from the app UI language (pt-BR by default),
captions should arrive in the app language: this module translates the ASR
text with Meta's NLLB-200-distilled-600M (``JustFrederik/nllb-200-distilled-600M-ct2-int8``
— the ct2 int8 conversion, 622 MB, runs on CPU and CUDA via the already-pinned
``ctranslate2`` + ``tokenizers`` deps; covers en/es/de/fr/pt and 190+ more,
target forced with the NLLB ``por_Latn``/``spa_Latn``/``eng_Latn`` token, no
source token needed) and decides WHEN to translate:

* channel-language evidence first (``channel_language.aggregate_channel_language``
  — Twitch GQL / Kick payload language stamped at fetch time): known-different
  family -> translate, known-same -> pass through;
* no evidence -> per-window spoken-language identification with sherpa-onnx's
  whisper-tiny SLID (``csukuangfj/sherpa-onnx-whisper-tiny``, int8, CPU), then
  a 3-of-last-5 majority vote decides. The majority threshold absorbs the
  tiny model's pt<->es confusion (measured: 1 of 4 pt slices mislabelled es;
  a pt stream needs 3/5 mislabels to flip — a NLLB pt->pt re-render garbles
  loanwords, so the gate is deliberately conservative).

Every failure degrades to the raw ASR text — captions never block on
translation. Measured on this host: NLLB ct2-int8 0.15-0.4 s/window on CUDA,
~0.9 s on CPU (a 2 s caption window budgets both comfortably); SLID ~0.08 s.

Models live ONLY in the models folder (``<whisper-cache>/translate/``):
``nllb-200-distilled-600M-ct2-int8/`` (ct2 int8 checkpoint, ~622 MB) and
``sherpa-onnx-whisper-tiny/`` (SLID int8, ~104 MB). Like the parakeet gate,
the captioner never triggers a multi-GB download mid-stream: translation
activates only when the model files are already present (use
``download_models()`` to fetch them on demand).
"""
from __future__ import annotations

import functools
import logging
import os
import threading
import time
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Deque, Optional

logger = logging.getLogger(__name__)

# --- knobs ---------------------------------------------------------------

TRANSLATE_ENV = "VODRIP_CAPTION_TRANSLATE"  # "0" = hard kill switch
MODEL_DIR_ENV = "VODRIP_TRANSLATE_MODEL_DIR"  # override for <cache>/translate

NLLB_REPO = "JustFrederik/nllb-200-distilled-600M-ct2-int8"
NLLB_SUBDIR = "nllb-200-distilled-600M-ct2-int8"
_NLLB_FILES = ("model.bin", "config.json", "tokenizer.json")

SLID_REPO = "csukuangfj/sherpa-onnx-whisper-tiny"
SLID_SUBDIR = "sherpa-onnx-whisper-tiny"
_SLID_FILES = ("tiny-encoder.int8.onnx", "tiny-decoder.int8.onnx")

# Repeated phrases ("welcome back", "let me know in chat") dominate streams —
# cache last translations so a repeat never pays a GPU call.
CACHE_MAX = 128
# SLID majority: >= 3 of the last 5 speech windows must agree before the gate
# flips a no-evidence stream (absorbs tiny-model pt<->es confusion).
SLID_VOTE_SPAN = 5
SLID_VOTE_MIN = 3
# Cooldown before re-attempting a failed model load (a mid-session install of
# the model files is picked up without a per-window retry storm).
LOAD_RETRY_SEC = 300.0

_UI_FAMILY = {"pt-BR": "pt", "pt": "pt", "es": "es", "en": "en"}
_TARGET_TOKEN = {"pt": "por_Latn", "es": "spa_Latn", "en": "eng_Latn"}
_SPECIAL_TOKENS = {"<s>", "</s>", "<unk>", ""}


def enabled() -> bool:
    """Translation on at all? VODRIP_CAPTION_TRANSLATE=0 is a hard kill."""
    return os.environ.get(TRANSLATE_ENV, "1").strip() != "0"


@functools.lru_cache(maxsize=None)
def _resolve_translate_dir(override: str) -> Path:
    """The env-keyed resolution underneath translate_dir — cached so the
    live-captioner's ~2 s flush never re-runs the disk_inventory() syscall
    ladder (whisper_cache_dir -> best_model_cache_drive enumerates drives
    and queries free space). The env value is the cache key, so an override
    change (tests, runtime) invalidates naturally; the presence checks in
    nllb_dir()/slid_dir() stay LIVE (cheap is_file stats), so models that
    appear mid-process are picked up on the next flush. No drive-letter
    fallback scan: models resolve from VODRIP_TRANSLATE_MODEL_DIR or the
    Settings models folder only — the configured dir wins over stale copies
    on other drives."""
    if override:
        return Path(override)
    from services.disk_hygiene import whisper_cache_dir  # lazy: keeps import light

    # ponytail: mkdir on resolve is benign (download_models()/hf local_dir
    # create it anyway) but keeps translate_dir() an existing-dir contract;
    # upgrade path: defer creation to download_models() and make
    # translate_dir() a pure resolver.
    primary = whisper_cache_dir() / "translate"
    primary.mkdir(parents=True, exist_ok=True)
    return primary



def translate_dir() -> Path:
    """<whisper cache>/translate — the models-folder subdir for this feature.

    VODRIP_TRANSLATE_MODEL_DIR overrides (tests pin scratch dirs); the
    default follows the whisper model cache (Settings > Disk "AI Models
    Folder"), so ALL models live under the models folder. The resolution
    is memoized per env value (P2-7) — the settings-driven default only
    changes on a settings change, which is rare/restart-level."""
    return _resolve_translate_dir(os.environ.get(MODEL_DIR_ENV, "").strip())


def nllb_dir() -> Optional[Path]:
    """The NLLB checkpoint dir when the files are present locally, else None.

    Presence check only — never downloads (the captioner must not trigger a
    multi-GB fetch mid-stream; mirrors the parakeet gate)."""
    d = translate_dir() / NLLB_SUBDIR
    return d if all((d / f).is_file() for f in _NLLB_FILES) else None


def slid_dir() -> Optional[Path]:
    """The SLID model dir when the files are present locally, else None."""
    d = translate_dir() / SLID_SUBDIR
    return d if all((d / f).is_file() for f in _SLID_FILES) else None


def app_language_family() -> str:
    """The caption target family: settings.ui_language mapped ('pt-BR'->'pt'),
    defaulting to 'pt' — the repo-wide default family (mirrors
    subtitles.py / youtube_session.py)."""
    try:
        from deps import settings_mgr

        ui = (getattr(settings_mgr.get(), "ui_language", "") or "").strip()
    except Exception:
        ui = ""
    return _UI_FAMILY.get(ui, "pt")


_SOURCE_TOKEN = {"pt": "por_Latn", "es": "spa_Latn", "en": "eng_Latn"}


def source_token(family: str) -> Optional[str]:
    """NLLB source token for a family ('pt' -> 'por_Latn'). None when unknown."""
    return _SOURCE_TOKEN.get(family)


def target_token(family: str) -> str:
    """NLLB target token for a family ('pt' -> 'por_Latn')."""
    return _TARGET_TOKEN.get(family, "por_Latn")


def needs_translation(
    evidence: Optional[str], app: str, votes: Deque[str]
) -> bool:
    """Decide whether the caption text must be translated.

    evidence = channel-language family (None = unknown); votes = SLID
    detections for the recent speech windows. Known-different evidence wins
    outright; without evidence the majority of >= SLID_VOTE_MIN recent votes
    (must differ from the app family) flips the gate — otherwise pass
    through. A pt stream mislabelled es once or twice never flips, so a
    same-language stream is never re-rendered by the translator."""
    if evidence:
        return evidence != app
    if not votes:
        return False
    fam, n = Counter(votes).most_common(1)[0]
    return n >= SLID_VOTE_MIN and fam != app


def lock_source_family(
    evidence: Optional[str],
    votes: Deque[str],
    *,
    asr_lang: Optional[str] = None,
) -> Optional[str]:
    """Resolve the source language family for the session: evidence wins if
    known; else majority >= SLID_VOTE_MIN among votes + asr_lang; else None."""
    if evidence and evidence in _TARGET_TOKEN:
        return evidence
    if asr_lang and asr_lang in _TARGET_TOKEN:
        all_votes = list(votes) + [asr_lang]
    else:
        all_votes = list(votes)
    if not all_votes:
        return None
    fam, n = Counter(all_votes).most_common(1)[0]
    if n >= SLID_VOTE_MIN and fam in _TARGET_TOKEN:
        return fam
    return None


def download_models() -> None:
    """Fetch the NLLB checkpoint + SLID model into the models folder.

    Called by an install/self-check path, never by the live captioner (a
    multi-GB download must not happen mid-stream). Idempotent: files already
    present are left alone."""
    from huggingface_hub import snapshot_download

    if nllb_dir() is None:
        logger.info("Downloading NLLB translation model (%s) ...", NLLB_REPO)
        snapshot_download(
            repo_id=NLLB_REPO,
            local_dir=str(translate_dir() / NLLB_SUBDIR),
            allow_patterns=["*.json", "*.model", "*.txt", "model.bin"],
        )
    if slid_dir() is None:
        logger.info("Downloading SLID model (%s) ...", SLID_REPO)
        for f in _SLID_FILES:
            from huggingface_hub import hf_hub_download

            hf_hub_download(
                repo_id=SLID_REPO, filename=f,
                local_dir=str(translate_dir() / SLID_SUBDIR),
            )


# --- translator singleton (shared across captioner threads) ---------------

_translator: Optional["_CaptionTranslator"] = None
# RLock: the module-level prewarm()/detect_language()/translate() hold the
# lock while calling _get_translator(), which re-acquires it — a plain Lock
# deadlocks on first use.
_translator_lock = threading.RLock()


def _get_translator() -> "_CaptionTranslator":
    """The process-wide translator, lazily created (guarded)."""
    global _translator
    with _translator_lock:
        if _translator is None:
            _translator = _CaptionTranslator()
        return _translator


def prewarm() -> None:
    """Load SLID + NLLB off the captioner's critical path (non-fatal)."""
    with _translator_lock:
        _get_translator().prewarm()


def detect_language(audio: "Any") -> Optional[str]:
    """SLID detection for one 16 kHz window (serialized, best-effort)."""
    with _translator_lock:
        return _get_translator().detect_language(audio)


def translate(text: str, family: str, *, source_family: Optional[str] = None) -> Optional[str]:
    """Translate *text* into the app family; None on any failure (raw)."""
    with _translator_lock:
        return _get_translator().translate(text, family, source_family=source_family)


class _CaptionTranslator:
    """Lazy NLLB + SLID models with cached-segment translation.

    One instance per process; every call is serialized by _translator_lock
    (ctranslate2 inference is not thread-safe, and the GPU-bound calls would
    contend anyway). Any failure flips the component to a broken state with a
    LOAD_RETRY_SEC cooldown — callers get None and degrade to raw text."""

    def __init__(self) -> None:
        self._nllb: Optional[tuple[Any, Any, str]] = None  # (tokenizer, translator, device)
        self._nllb_broken_at = 0.0
        self._slid: Any = None
        self._slid_broken_at = 0.0
        self._cache: "OrderedDict[tuple[str, str], str]" = OrderedDict()

    # -- public ----------------------------------------------------------

    def prewarm(self) -> None:
        """Load both models (SLID cheap, NLLB ~1 s int8) off the captioner's
        critical path. Failures are absorbed here — the first real call
        re-attempts after the cooldown."""
        try:
            self._ensure_slid()
        except Exception:
            pass
        try:
            self._ensure_nllb()
        except Exception:
            pass

    def detect_language(self, audio: "Any") -> Optional[str]:
        """Spoken-language family of one 16 kHz float32 window (SLID, CPU).
        None when the model is unavailable/failed. ~60-130 ms per 2 s window."""
        slid = self._ensure_slid()
        if slid is None:
            return None
        stream = slid.create_stream()
        stream.accept_waveform(16000, audio)
        try:
            lang = slid.compute(stream)
        except Exception as exc:
            logger.debug("SLID inference failed: %s", exc)
            return None
        return (lang or "").strip().lower() or None

    def translate(self, text: str, family: str, *, source_family: Optional[str] = None) -> Optional[str]:
        """Translate *text* to the app family. Cached by (family, source_family, text);
        None on any failure (caller falls back to the raw ASR text)."""
        text = (text or "").strip()
        if not text:
            return None
        key = (family, source_family, text)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        stok = source_token(source_family) if source_family else None
        out = self._run_translate(text, target_token(family), src_tok=stok)
        if not out:
            return None
        self._cache[key] = out
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_MAX:
            self._cache.popitem(last=False)
        return out

    # -- internals -------------------------------------------------------

    def _ensure_slid(self) -> Any:
        if self._slid is not None:
            return self._slid
        if self._slid_broken_at and time.monotonic() - self._slid_broken_at < LOAD_RETRY_SEC:
            return None
        d = slid_dir()
        if d is None:
            return None
        try:
            try:
                import torch  # noqa: F401  # load torch/lib's cudnn (9.25) FIRST — sherpa CUDA (9.24 wheels) otherwise loads first and torch's later import fails WinError 127
            except Exception:
                pass  # torch unavailable — SLID still works on CPU
            import sherpa_onnx
            from services.archive_transcribe import _ensure_cuda_libs

            # sherpa's CUDA EP + whisper-tiny SLID need the nvidia cu12 DLLs
            # (cublasLt/cublas/cudnn) resolvable — frozen bundles collect them
            # under _internal/nvidia/*/bin, which PATH prepend exposes.
            _ensure_cuda_libs()
            whisper_cfg = sherpa_onnx.SpokenLanguageIdentificationWhisperConfig(
                encoder=str(d / "tiny-encoder.int8.onnx"),
                decoder=str(d / "tiny-decoder.int8.onnx"),
            )
            for provider in ("cuda", "cpu"):
                try:
                    self._slid = sherpa_onnx.SpokenLanguageIdentification(
                        sherpa_onnx.SpokenLanguageIdentificationConfig(
                            whisper=whisper_cfg,
                            num_threads=2,
                            provider=provider,
                        )
                    )
                    return self._slid
                except Exception as exc:
                    if provider == "cuda":
                        # ponytail: CUDA EP failure (missing DLLs, driver)
                        # degrades to CPU — SLID is 0.08s/window either way
                        logger.warning("SLID CUDA load failed (%s) — CPU provider", exc)
            return None
        except Exception as exc:
            logger.warning("SLID model load failed (%s) — raw captions", exc)
            self._slid_broken_at = time.monotonic()
            return None

    def _ensure_nllb(self) -> Optional[tuple[Any, Any, str]]:
        if self._nllb is not None:
            return self._nllb
        if self._nllb_broken_at and time.monotonic() - self._nllb_broken_at < LOAD_RETRY_SEC:
            return None
        d = nllb_dir()
        if d is None:
            return None
        try:
            import ctranslate2
            import tokenizers
            from services.archive_transcribe import _ensure_cuda_libs

            _ensure_cuda_libs()  # ctranslate2 loads cublas/cudnn lazily at first CUDA inference

            # P2-5: CUDA is the fast path, but the whisper/parakeet lanes
            # hold most of the VRAM — a CUDA load can fail (OOM, driver
            # contention) even when cuda is "available". Fall back to CPU
            # with one log instead of dropping straight to raw captions for
            # LOAD_RETRY_SEC: ~0.9 s/window still fits a 2 s caption budget.
            devices = (
                ("cuda", "cpu") if ctranslate2.get_cuda_device_count() > 0 else ("cpu",)
            )
            # ponytail: the checkpoint is ct2-int8 (622 MB) — compute_type must
            # stay int8, matching the quantized weights; float16 would re-quantize
            # and is not available for this conversion.
            t0 = time.monotonic()
            tok = tokenizers.Tokenizer.from_file(str(d / "tokenizer.json"))
            model = None
            device = "cpu"
            for device in devices:
                try:
                    model = ctranslate2.Translator(
                        str(d), device=device, compute_type="int8",
                    )
                    break
                except Exception as exc:
                    if device == "cuda":
                        logger.warning(
                            "NLLB CUDA load failed — falling back to CPU: %s", exc
                        )
                        continue
                    raise
            logger.info(
                "NLLB translator loaded in %.1fs (%s int8)", time.monotonic() - t0, device,
            )
            self._nllb = (tok, model, device)
            return self._nllb
        except Exception as exc:
            logger.warning("NLLB translator load failed (%s) — raw captions", exc)
            self._nllb_broken_at = time.monotonic()
            return None

    def _run_translate(self, text: str, tgt: str, *, src_tok: Optional[str] = None) -> Optional[str]:
        nllb = self._ensure_nllb()
        if nllb is None:
            return None
        tok, model, _device = nllb
        try:
            tokens = tok.encode(text).tokens
            if src_tok and (not tokens or tokens[0] != src_tok):
                tokens = [src_tok] + tokens
            from services.archive_transcribe import transcription_cpu_limiter
            with transcription_cpu_limiter(1):
                out = model.translate_batch(
                    [tokens], target_prefix=[[tgt]], beam_size=1,
                    max_decoding_length=128,
                )
            toks = list(out[0].hypotheses[0])
            if toks and toks[0] == tgt:  # forced target token leads the hypothesis
                toks = toks[1:]
            return "".join(t for t in toks if t not in _SPECIAL_TOKENS).replace(
                "▁", " "
            ).strip()
        except Exception as exc:
            logger.debug("NLLB translation failed (%s) — raw caption", exc)
            return None
