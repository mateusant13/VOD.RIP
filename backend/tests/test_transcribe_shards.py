"""Bounded-RAM sharded transcription check (decode-once, shards on disk).

Builds a ~18 s synthetic fixture (4.5 s silence + TTS speech + 3 s silence +
TTS speech) whose speech straddles the 5 s shard boundaries, then runs the
SAME audio through BOTH decode paths of transcribe_video:

  * sharded (VODRIP_TRANSCRIBE_SHARD_SEC=5, VODRIP_TRANSCRIBE_SHARD_MIN_SEC=5):
    PCM spilled to 5 s disk shards, VAD per shard with overlap + cross-shard
    merge, whisper fed from concatenated clip shards, events hook handed the
    shards instead of a full array;
  * non-sharded (VODRIP_TRANSCRIBE_SHARD_MIN_SEC=3600): the legacy full-array
    path.

Asserts:
  (a) bounded RAM — shard files are fixed-duration (each <= 5 s * 16k * 4 B,
      only the tail may be short) and the decode iterator yields one array
      per shard;
  (b) joined transcript TEXT is identical on both paths (whisper output must
      not depend on the decode strategy);
  (c) cleanup — no vodrip-shards-* temp dir survives the job, on the success
      AND the failure path; the events stage scores shard-fed regions with
      the same absolute rows as the full-array path.

Run standalone (recommended — isolated process):
    python tests/test_transcribe_shards.py
Under pytest (skips when archive_db is bound to another DB):
    python -m pytest tests/test_transcribe_shards.py -s
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess as sp
import sys
import tempfile

# Scope the temp namespace: _shard_dirs() scans gettempdir() by name and the
# transcribe subprocesses mkdtemp('vodrip-shards-') there too. The OS temp is
# SHARED with any live archive worker (real VOD sharding) — a foreign
# vodrip-shards-* dir made the leak-check flaky. Pointing TMP/TEMP (inherited
# by the subprocesses) + tempfile.tempdir at a private dir makes all four
# leak checks hermetic. (ponytail: pytest's tmp_* land here too — harmless.)
os.environ["TMP"] = os.environ["TEMP"] = str(
    pathlib.Path(tempfile.mkdtemp(prefix="vodrip-shardtest-scope-"))
)
tempfile.tempdir = os.environ["TMP"]
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="vodrip-shardtest-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")
os.environ.setdefault("VODRIP_WHISPER_MODEL", "small")
os.environ.setdefault("VODRIP_WHISPER_IDLE_CLOSE", "60")
# Events are default-on now; this test exercises sharding, not the PANNs
# stage (the fake-SED comparisons at the bottom pin the shard-fed path).
os.environ["VODRIP_EVENTS_ENABLED"] = "0"

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import archive_db, archive_transcribe  # noqa: E402
from services.os_services import _NO_WINDOW  # noqa: E402
from services.archive_transcribe import (  # noqa: E402
    SAMPLE_RATE,
    _ShardedAudio,
    _decode_to_shards,
    _resolve_ffmpeg_exe,
    _should_shard,
    transcribe_video,
)

PLATFORM = "twitch"
SHARD_SEC = 5.0
SPEECH1 = _TMP / "speech1.wav"
SPEECH2 = _TMP / "speech2.wav"
FIXTURE = _TMP / "fixture.wav"
TEXT1 = "Welcome to the VOD dot RIP archive system, this is a test."
TEXT2 = "Second speech segment spoken after ten seconds of silence."


def _tts_speech(wav: pathlib.Path, text: str) -> pathlib.Path:
    """Record-free speech via Windows System.Speech (no mic, no downloads)."""
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        f"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SetOutputToWaveFile('{wav}'); "
        f"$s.Speak('{text.replace(chr(39), '')}'); $s.Dispose()"
    )
    proc = sp.run(["powershell", "-NoProfile", "-Command", ps],
                  capture_output=True, timeout=120, creationflags=_NO_WINDOW)
    if proc.returncode != 0 or not wav.is_file():
        raise RuntimeError(
            "Windows TTS unavailable — need speech-like content for the fixture: "
            + (proc.stderr or b"").decode("utf-8", "replace")[-300:]
        )
    return wav


def _build_fixture() -> pathlib.Path:
    """~18 s fixture: 4.5 s silence + speech1 + 3 s silence + speech2.

    With SHARD_SEC=5 the shard boundaries land at 5/10/15 s — inside speech1
    (~4.5-9 s) and speech2 (~12-17 s) — so the cross-shard VAD merge and the
    sharded whisper feed are exercised, not just the happy path."""
    ffmpeg = _resolve_ffmpeg_exe()
    _tts_speech(SPEECH1, TEXT1)
    _tts_speech(SPEECH2, TEXT2)
    cmd = [
        ffmpeg, "-y", "-v", "error",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=4.5",
        "-i", str(SPEECH1),
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=3",
        "-i", str(SPEECH2),
        "-filter_complex",
        "[0]aformat=sample_rates=16000:channel_layouts=mono[a0];"
        "[1]aformat=sample_rates=16000:channel_layouts=mono[a1];"
        "[2]aformat=sample_rates=16000:channel_layouts=mono[a2];"
        "[3]aformat=sample_rates=16000:channel_layouts=mono[a3];"
        "[a0][a1][a2][a3]concat=n=4:v=0:a=1[a]",
        "-map", "[a]", "-ar", "16000", "-ac", "1", str(FIXTURE),
    ]
    proc = sp.run(cmd, capture_output=True, timeout=120, creationflags=_NO_WINDOW)
    if proc.returncode != 0 or not FIXTURE.is_file():
        raise RuntimeError(f"fixture build failed: {proc.stderr.decode('utf-8', 'replace')}")
    return FIXTURE


def _shard_dirs() -> set:
    return {p for p in os.listdir(tempfile.gettempdir()) if p.startswith("vodrip-shards-")}


def _transcribe(video_id: str) -> dict:
    archive_db.upsert_video({
        "platform": PLATFORM,
        "video_id": video_id,
        "channel": "selftest",
        "title": "shards fixture",
        "status": "ready",
        "archive_path": str(FIXTURE),
        "duration_sec": 18.0,
    })
    return transcribe_video(PLATFORM, video_id)


def _run() -> None:
    fixture = _build_fixture()
    full_shard_bytes = int(SHARD_SEC * SAMPLE_RATE) * 4

    # --- (a) decode iterator + shard file contract -------------------------
    check_dir = pathlib.Path(tempfile.mkdtemp(prefix="vodrip-shardcheck-"))
    try:
        shards = list(_decode_to_shards(str(fixture), shard_sec=SHARD_SEC, out_dir=str(check_dir)))
        files = sorted(check_dir.glob("shard_*.f32"))
        assert len(shards) >= 2, f"fixture must span >= 2 shards: {len(shards)}"
        assert len(files) == len(shards), "one file per yielded shard"
        for i, ((start, arr), fpath) in enumerate(zip(shards, files)):
            assert start == i * SHARD_SEC, (i, start)
            assert arr.size * 4 == fpath.stat().st_size, "file and array must agree"
            assert arr.size <= int(SHARD_SEC * SAMPLE_RATE), (
                "per-shard array must be bounded by the fixed shard duration"
            )
            assert fpath.stat().st_size <= full_shard_bytes, f"shard too long: {fpath}"
        # fixed duration: every shard but the tail is exactly one shard long
        assert all(f.stat().st_size == full_shard_bytes for f in files[:-1]), (
            "non-tail shards must be exactly fixed-duration"
        )
        assert files[-1].stat().st_size <= full_shard_bytes
    finally:
        shutil.rmtree(check_dir, ignore_errors=True)

    before = _shard_dirs()

    # --- (b) sharded transcribe -------------------------------------------
    os.environ["VODRIP_TRANSCRIBE_SHARD_SEC"] = "5"
    os.environ["VODRIP_TRANSCRIBE_SHARD_MIN_SEC"] = "5"
    assert _should_shard(str(fixture)) is True, "17 s fixture must take the sharded path"
    stats_a = _transcribe("__shards_a__")
    rows_a = archive_db.transcript_for(PLATFORM, "__shards_a__")
    text_a = " ".join(r["text"] for r in rows_a)
    assert rows_a, "sharded run must produce segments"
    assert stats_a["total_sec"] > SHARD_SEC, f"fixture duration missing: {stats_a}"
    idxs = [int(r["seg_idx"]) for r in rows_a]
    assert idxs == list(range(len(rows_a))), f"seg_idx must stay contiguous: {idxs}"
    words = [w for r in rows_a for w in json.loads(r["words_json"] or "[]")]
    assert words and all({"word", "start", "end"} <= set(w) for w in words), (
        "word timestamps must survive the sharded path"
    )
    assert _shard_dirs() == before, "sharded job must clean up its shard dir"

    # --- (c) non-sharded transcribe — same audio, same text ----------------
    os.environ["VODRIP_TRANSCRIBE_SHARD_MIN_SEC"] = "3600"
    assert _should_shard(str(fixture)) is False, "same fixture must stay on the full-array path"
    stats_b = _transcribe("__shards_b__")
    rows_b = archive_db.transcript_for(PLATFORM, "__shards_b__")
    text_b = " ".join(r["text"] for r in rows_b)
    assert text_a == text_b, (
        "sharded and non-sharded transcripts must match\n"
        f"  sharded:    {text_a!r}\n"
        f"  non-sharded: {text_b!r}"
    )
    assert _shard_dirs() == before, "no shard dir may leak"

    # --- (d) events stage: shard-fed regions == full-array rows -------------
    from unittest.mock import patch

    from services import archive_events as ev

    class _FakeSed:
        labels = ["Laughter", "Clapping"]

        def inference(self, batch):  # every window is one long Laughter run
            import numpy as np

            n_frames = batch.shape[-1] // 320  # 10 ms SED hop @ 32 kHz
            out = np.zeros((batch.shape[0], n_frames, len(self.labels)), dtype=np.float32)
            out[:, :, 0] = 0.9
            return out

    ev_dir = pathlib.Path(tempfile.mkdtemp(prefix="vodrip-shardcheck-"))
    try:
        list(_decode_to_shards(str(fixture), shard_sec=SHARD_SEC, out_dir=str(ev_dir)))
        shards_obj = _ShardedAudio(sorted(ev_dir.glob("shard_*.f32")), SHARD_SEC)
        with patch.object(ev, "_sed_model", return_value=_FakeSed()):
            audio16 = archive_transcribe.decode_audio(str(fixture))
            speech = archive_transcribe.vad_speech_seconds(audio16)
            full_events = ev.detect_events(audio16, speech, ["Laughter"])
            shard_events = ev.detect_events(None, speech, ["Laughter"], shards=shards_obj)
        assert full_events and shard_events, "fake SED must produce events"
        assert [e["event"] for e in shard_events] == [e["event"] for e in full_events], (
            "shard-fed events must carry the same labels"
        )
        for se, fe in zip(shard_events, full_events):
            assert abs(se["start_sec"] - fe["start_sec"]) <= 0.06, (se, fe)
            assert abs(se["end_sec"] - fe["end_sec"]) <= 0.06, (se, fe)
        assert all(e["start_sec"] >= speech[0][0] for e in shard_events), (
            "shard-fed events must carry absolute offsets"
        )
    finally:
        shutil.rmtree(ev_dir, ignore_errors=True)
    assert _shard_dirs() == before, "events shard decode must clean up"

    # --- (e) failure path still cleans up -----------------------------------
    os.environ["VODRIP_TRANSCRIBE_SHARD_MIN_SEC"] = "5"
    with patch.object(archive_db, "insert_transcript", side_effect=RuntimeError("boom")):
        try:
            _transcribe("__shards_fail__")
        except RuntimeError:
            pass
        else:
            raise AssertionError("failing insert must abort the sharded job")
    assert _shard_dirs() == before, "failed sharded job must clean up its shard dir"

    print("=== sharded vs non-sharded transcription ===")
    print(f"  fixture: {fixture} ({fixture.stat().st_size} bytes, {stats_a['total_sec']:.1f}s)")
    print(f"  shards: {len(files)} x {SHARD_SEC}s (<= {full_shard_bytes} B each)")
    print(f"  sharded text:   {text_a!r}")
    print(f"  non-sharded:    {text_b!r}")
    print(f"  events: full={len(full_events)} shard-fed={len(shard_events)} (abs offsets)")
    print("  cleanup: success + failure paths verified (no vodrip-shards-* left)")

    shutil.rmtree(_TMP, ignore_errors=True)


def test_transcribe_shards() -> None:
    if pathlib.Path(os.environ["VODRIP_ARCHIVE_DB"]) != archive_db._db_path():
        import pytest

        pytest.skip(
            "archive_db already bound to another DB in this process — "
            "run standalone: python tests/test_transcribe_shards.py"
        )
    _run()


if __name__ == "__main__":
    import logging

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    _run()
    print("\nSHARDS OK — bounded-RAM sharded path verified.")
