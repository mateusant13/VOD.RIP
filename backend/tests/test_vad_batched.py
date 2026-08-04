"""Stateful batched VAD self-check: regions must match silero's
get_speech_timestamps (the legacy per-window path) within tolerance.

Runs the new batched pass on real TTS speech (Windows System.Speech — the
same fixture approach as test_archive_transcribe_e2e_real.py) and asserts:
  * per-window probabilities match the legacy per-window loop (~1e-5),
  * returned regions match get_speech_timestamps exactly (same rounding),
  * edge cases (empty, sub-window, silence) stay region-free,
  * with VODRIP_VAD_ONNX=1, the onnxruntime path yields the same regions
    (tolerance 0.1 s) and provider selection does not crash.

CPU-only by default: the ONNX subtest runs only when VODRIP_VAD_ONNX=1.
Run standalone (recommended):
    python tests/test_vad_batched.py
or under pytest:
    python -m pytest tests/test_vad_batched.py -q
"""
from __future__ import annotations

import os
import pathlib
import subprocess as sp
import sys
import tempfile
import time

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="vodrip-vad-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from services import archive_transcribe as at  # noqa: E402
from services.os_services import _NO_WINDOW  # noqa: E402

SAMPLE_RATE = 16000
WINDOW = at._VAD_WINDOW
REGION_TOL = 0.1  # seconds — region boundary tolerance vs the legacy path


def _tts_audio() -> np.ndarray:
    """Real speech via Windows System.Speech -> wav -> 16 kHz float32 mono."""
    from services.archive_transcribe import _resolve_ffmpeg_exe

    wav = _TMP / "speech.wav"
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        f"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SetOutputToWaveFile('{wav}'); "
        "$s.Speak('Welcome to the VOD dot RIP archive system, this is a test. "
        "Second speech segment spoken after ten seconds of silence.'); $s.Dispose()"
    )
    proc = sp.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, timeout=120, creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0 or not wav.is_file():
        raise RuntimeError(
            "Windows TTS unavailable — the VAD self-check needs speech-like "
            "content: " + (proc.stderr or b"").decode("utf-8", "replace")[-300:]
        )
    raw = sp.run(
        [_resolve_ffmpeg_exe(), "-v", "error", "-i", str(wav),
         "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True, timeout=60, creationflags=_NO_WINDOW,
    )
    if raw.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed: {raw.stderr.decode('utf-8', 'replace')}")
    return np.frombuffer(raw.stdout, dtype=np.float32).copy()


def _legacy(audio: np.ndarray) -> tuple[np.ndarray, list[tuple[float, float]]]:
    """The exact legacy path: per-window .item() loop + get_speech_timestamps."""
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad

    torch.set_num_threads(1)
    vad = load_silero_vad(onnx=False)
    vad.reset_states()
    probs = []
    with torch.no_grad():
        for i in range(0, len(audio), WINDOW):
            chunk = torch.from_numpy(audio[i:i + WINDOW])
            if chunk.shape[0] < WINDOW:
                chunk = torch.nn.functional.pad(chunk, (0, WINDOW - chunk.shape[0]))
            probs.append(vad(chunk, SAMPLE_RATE).item())
    ts = get_speech_timestamps(
        torch.from_numpy(audio), vad, sampling_rate=SAMPLE_RATE,
        threshold=0.5, min_speech_duration_ms=250, min_silence_duration_ms=200,
        speech_pad_ms=30, return_seconds=True,
    )
    return np.array(probs), [(float(t["start"]), float(t["end"])) for t in ts]


def _run() -> None:
    audio = _tts_audio()
    audio_len_sec = len(audio) / SAMPLE_RATE
    print(f"=== batched VAD on {audio_len_sec:.1f}s TTS speech ===")

    # --- legacy reference, then the active path -----------------------------
    legacy_probs, legacy_regions = _legacy(audio)
    vad = at._get_vad()
    torch_mode = not isinstance(vad, at._OnnxVad)

    if torch_mode:
        # Warm up the model + one-time torch init so the wall clock reflects
        # the steady-state VAD pass, not process first-use costs.
        at._vad_probs_torch(np.zeros(SAMPLE_RATE, np.float32), vad)
    t0 = time.monotonic()
    regions = at.vad_speech_seconds(audio)
    wall = time.monotonic() - t0
    assert regions, "TTS speech must produce at least one region"

    if torch_mode:
        probs = at._vad_probs_torch(audio, vad)
        diff = float(np.abs(probs - legacy_probs).max())
        assert diff < 1e-4, f"batched probs deviate from the legacy loop: {diff}"
        assert regions == legacy_regions, (
            f"regions must match get_speech_timestamps exactly: "
            f"{regions} vs {legacy_regions}"
        )
        print(f"  max |prob| diff vs legacy loop: {diff:.2e}")
    else:
        assert len(regions) == len(legacy_regions)
        for (s1, e1), (s2, e2) in zip(regions, legacy_regions):
            assert abs(s1 - s2) <= REGION_TOL and abs(e1 - e2) <= REGION_TOL, (
                f"ONNX region {s1}-{e1} vs legacy {s2}-{e2} exceeds {REGION_TOL}s"
            )
        print("  torch path skipped (VODRIP_VAD_ONNX=1); ONNX regions match legacy")

    # --- edge cases ---------------------------------------------------------
    assert at.vad_speech_seconds(None) == []
    assert at.vad_speech_seconds(np.zeros(0, np.float32)) == []
    assert at.vad_speech_seconds(np.zeros(100, np.float32)) == []
    assert at.vad_speech_seconds(np.zeros(SAMPLE_RATE * 2, np.float32)) == []
    # a sub-window speech fragment must still run (no region: < 250 ms)
    assert at.vad_speech_seconds(audio[:300]) == []

    # --- ONNX path (opt-in): same regions, provider selection must not crash
    if os.environ.get("VODRIP_VAD_ONNX", "").strip() == "1":
        old = at._vad
        at._vad = None
        try:
            onnx_vad = at._get_vad()
            assert isinstance(onnx_vad, at._OnnxVad), type(onnx_vad)
            assert onnx_vad.session.get_providers(), "session must have a provider"
            onnx_probs = at._vad_probs_onnx(audio, onnx_vad)
            onnx_regions = at._vad_regions(onnx_probs, len(audio))
        finally:
            at._vad = old
        if torch_mode:
            assert abs(float(np.max(onnx_probs)) - float(np.max(probs))) < 1e-3
        assert len(onnx_regions) == len(legacy_regions)
        for (s1, e1), (s2, e2) in zip(legacy_regions, onnx_regions):
            assert abs(s1 - s2) <= REGION_TOL and abs(e1 - e2) <= REGION_TOL, (
                f"ONNX region {s1}-{e1} vs legacy {s2}-{e2} exceeds {REGION_TOL}s"
            )
        print(f"  onnx regions: {onnx_regions} "
              f"(providers {onnx_vad.session.get_providers()})")
    else:
        print("  (ONNX path skipped — set VODRIP_VAD_ONNX=1 to exercise it)")

    print(f"  regions ({len(regions)}): {regions}")
    if torch_mode:
        print(f"  max |prob| diff vs legacy loop: {diff:.2e}")
    print(f"  wall: {wall * 1000:.0f} ms for {audio_len_sec:.1f}s "
          f"({audio_len_sec / wall:.0f}x realtime)")
    print("\nVAD BATCHED OK")


def test_vad_batched_regions_match_legacy() -> None:
    _run()


if __name__ == "__main__":
    import logging

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    _run()
