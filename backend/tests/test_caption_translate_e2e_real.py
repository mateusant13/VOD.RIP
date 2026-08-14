"""Real live-caption translation e2e — real EN speech audio → parakeet ASR →
NLLB ct2-int8 → pt-BR, asserted on content.

Proves the caption-translation contract the unit tests only stub:
  * a real 16 kHz English speech clip (the canonical JFK sample, downloaded
    at runtime from the openai/whisper test assets),
  * transcribed by the REAL parakeet engine (sherpa-onnx int8),
  * translated by the REAL NLLB ct2-int8 model (CPU or CUDA, whatever the
    host has) via the same public API the captioner uses,
  * the Portuguese output is asserted to contain the expected core
    translation (loans/function words may vary between models/versions —
    the assertion targets content, not exact match).

Needs the models installed (Settings > Disk > AI Models folder): parakeet in
the sherpa cache, NLLB + SLID under <models>/translate/. Skips otherwise.

Run directly (isolated process — recommended):
    python tests/test_caption_translate_e2e_real.py
Under pytest (opt-in `real` marker — excluded by default addopts):
    python -m pytest tests/test_caption_translate_e2e_real.py -m real -s
"""
from __future__ import annotations

import pathlib
import subprocess as sp
import sys
import tempfile
import time
import urllib.request

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import archive_transcribe as at  # noqa: E402
from services import caption_translate as ct  # noqa: E402

JFK_URL = "https://github.com/openai/whisper/raw/main/tests/jfk.flac"
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="vodrip-caption-translate-"))
_AUDIO = _TMP / "jfk.wav"


def _download_jfk() -> pathlib.Path:
    if _AUDIO.is_file():
        return _AUDIO
    flac = _TMP / "jfk.flac"
    if not flac.is_file():
        with urllib.request.urlopen(JFK_URL, timeout=60) as r, open(flac, "wb") as f:
            f.write(r.read())
    from services.ytdlp_ffmpeg import _resolve_ffmpeg_exe

    sp.run(
        [_resolve_ffmpeg_exe(), "-y", "-v", "error", "-i", str(flac),
         "-ar", "16000", "-ac", "1", str(_AUDIO)],
        check=True, capture_output=True, timeout=120,
    )
    return _AUDIO


@pytest.mark.real
def test_real_english_speech_translates_to_pt_br() -> None:
    if at._parakeet_resolve_dir() is None:
        pytest.skip("parakeet model not installed (run a transcription job to fetch it)")
    if ct.nllb_dir() is None:
        pytest.skip("NLLB translation model not installed (run download_models())")

    audio = at.decode_audio(str(_download_jfk()))
    assert len(audio) / 16000 > 8.0, "JFK clip should be ~11 s"

    t0 = time.monotonic()
    rec = at._parakeet_model()
    asr_load_s = time.monotonic() - t0

    chunks = at.vad_speech_seconds(audio)
    assert chunks, "VAD must find speech in the real clip"

    t0 = time.monotonic()
    res = at._transcribe_batch_parakeet(rec, audio, chunks, "en")
    asr_s = time.monotonic() - t0
    text = " ".join(it["text"] for items, _ in res for it in items)
    assert "fellow americans" in text.lower(), f"ASR drifted: {text!r}"

    ct.prewarm()
    t0 = time.monotonic()
    out = ct.translate(text, "pt")
    translate_ms = (time.monotonic() - t0) * 1000
    assert out, "translation must not fail on the real clip"

    low = out.lower()
    # content assertions — the JFK quote translated to pt-BR must keep the
    # core 'country can do for you' semantics (wording may vary by NLLB
    # version, so check the load-bearing words, not exact text)
    assert "país" in low or "pais" in low, f"missing 'país': {out!r}"
    assert "por você" in low or "para você" in low, f"missing 'por você': {out!r}"
    assert "fazer" in low, f"missing 'fazer': {out!r}"

    # the model is in the right size/precision class: int8 checkpoint
    assert ct.nllb_dir() is not None
    print(
        f"\nASR ({asr_load_s:.0f}s load, {asr_s:.1f}s): {text}\n"
        f"PT-BR ({translate_ms:.0f}ms): {out}\n"
        f"model: {ct.NLLB_REPO} ({ct.nllb_dir()})"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-m", "real", "-s"]))
