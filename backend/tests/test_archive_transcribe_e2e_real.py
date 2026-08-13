"""Real end-to-end check for the archive transcription worker (VAD + whisper + queue).

Builds a 20 s synthetic fixture (5 s tone + 10 s pure silence + 5 s tone) with
ffmpeg, runs it through the REAL archive_jobs queue (run_worker) against a temp
archive DB, then verifies:
  * VAD stats: dead air (the 10 s silence) skipped and reported,
  * segments land only in speech regions, word timestamps present,
  * resume: a crash-state manifest (header + chunk 0 recorded) with the last
    segment row deleted re-runs only the missing chunk and restores the row
    at its old index without touching the others,
  * manifest lifecycle: a completed job deletes its resume manifest; a
    simulated crash state keeps it until the job completes,
  * job lifecycle: queued -> running -> done, progress ends at 1.0,
  * no-speech skip: a pure-silence fixture (< 3 s planned speech) completes
    as done with skipped='no-speech' and the whisper model is NEVER loaded
    (_get_model/_thread_model are patched to raise).

Run directly (isolated process — recommended):
    python tests/test_archive_transcribe_e2e_real.py
Under pytest it skips if archive_db is already bound to another DB:
    python -m pytest tests/test_archive_transcribe_e2e_real.py -s
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="vodrip-transcribe-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")
os.environ.setdefault("VODRIP_WHISPER_MODEL", "small")
os.environ.setdefault("VODRIP_WHISPER_IDLE_CLOSE", "60")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import archive_db, archive_transcribe  # noqa: E402  (env must be set before import)
from services.os_services import _NO_WINDOW  # noqa: E402
from services.archive_transcribe import (  # noqa: E402
    run_worker,
    _manifest_path,
    _resolve_ffmpeg_exe,
    decode_audio,
    vad_speech_seconds,
    _plan_chunks,
    _write_manifest_header,
    _append_manifest_entry,
    _job_engine,
    _resolve_job_language,
    model_name,
)

PLATFORM = "twitch"
VIDEO_ID = "__transcribe_e2e__"
SILENT_VIDEO = "__transcribe_e2e_silent__"
FIXTURE = _TMP / "fixture.wav"
SPEECH1 = _TMP / "speech1.wav"
SPEECH2 = _TMP / "speech2.wav"
SILENCE_SEC = 10.0
TEXT1 = "Welcome to the VOD dot RIP archive system, this is a test."
TEXT2 = "Second speech segment spoken after ten seconds of silence."


def _tts_speech(wav: pathlib.Path, text: str) -> pathlib.Path:
    """Record-free speech via Windows System.Speech (no mic, no downloads)."""
    import subprocess as sp

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
    """~20 s fixture: ~5 s TTS speech + 10 s pure silence + ~5 s TTS speech."""
    import subprocess as sp

    ffmpeg = _resolve_ffmpeg_exe()
    _tts_speech(SPEECH1, TEXT1)
    _tts_speech(SPEECH2, TEXT2)
    cmd = [
        ffmpeg, "-y", "-v", "error",
        "-i", str(SPEECH1),
        "-f", "lavfi", "-i", f"anullsrc=r=16000:cl=mono:d={SILENCE_SEC}",
        "-i", str(SPEECH2),
        "-filter_complex",
        "[0]aformat=sample_rates=16000:channel_layouts=mono[a0];"
        "[1]aformat=sample_rates=16000:channel_layouts=mono[a1];"
        "[2]aformat=sample_rates=16000:channel_layouts=mono[a2];"
        "[a0][a1][a2]concat=n=3:v=0:a=1[a]",
        "-map", "[a]", "-ar", "16000", "-ac", "1", str(FIXTURE),
    ]
    proc = sp.run(cmd, capture_output=True, timeout=120, creationflags=_NO_WINDOW)
    if proc.returncode != 0 or not FIXTURE.is_file():
        raise RuntimeError(f"fixture build failed: {proc.stderr.decode('utf-8', 'replace')}")
    return FIXTURE


def _worker_engine(platform: str, video_id: str) -> str:
    """The engine run_worker's _process_job picks for this video — mirror its
    settings override mapping + _job_engine so the simulated crash manifest
    matches the engine that resumes the job (a whisper manifest on a parakeet
    run reads as stale and full re-runs, breaking the resume contract)."""
    try:
        from deps import settings_mgr
        pref = getattr(settings_mgr.get(), "asr_engine", "parakeet") or "parakeet"
    except Exception:
        pref = "parakeet"
    if pref == "whisper":
        return "whisper"
    return _job_engine(_resolve_job_language(platform, video_id))


def _enqueue_and_run(job_id: str) -> dict:
    archive_db.enqueue_job(job_id, "transcribe", PLATFORM, VIDEO_ID)
    run_worker(once=True, poll_interval=0.5)
    jobs = {j["id"]: j for j in archive_db.list_jobs()}
    return jobs[job_id]


def _run_no_speech() -> None:
    """A pure-silence fixture (< 3 s planned speech) must complete with
    skipped='no-speech' WITHOUT loading the whisper model: _get_model and
    _thread_model are patched to raise, so any model load fails the job."""
    import subprocess as sp
    from unittest.mock import patch

    silent = _TMP / "silent.wav"
    ffmpeg = _resolve_ffmpeg_exe()
    proc = sp.run(
        [ffmpeg, "-y", "-v", "error", "-f", "lavfi",
         "-i", "anullsrc=r=16000:cl=mono:d=8", str(silent)],
        capture_output=True, timeout=60, creationflags=_NO_WINDOW,
    )
    assert proc.returncode == 0 and silent.is_file(), (
        proc.stderr.decode("utf-8", "replace")
    )
    archive_db.upsert_video({
        "platform": PLATFORM,
        "video_id": SILENT_VIDEO,
        "channel": "selftest",
        "title": "silence fixture",
        "status": "ready",
        "archive_path": str(silent),
        "duration_sec": 8.0,
    })
    archive_db.enqueue_job("transcribe-e2e-silent", "transcribe", PLATFORM, SILENT_VIDEO)
    # max_workers=1 -> single-global-model path, so a model load would go
    # through _get_model; _thread_model is patched too (belt & braces for
    # multi-copy mode). Either being called means the skip did not fire.
    with patch.object(archive_transcribe, "_get_model",
                      side_effect=AssertionError("no-speech skip must not load a model")), \
         patch.object(archive_transcribe, "_thread_model",
                      side_effect=AssertionError("no-speech skip must not load a model")):
        t0 = time.monotonic()
        run_worker(once=True, poll_interval=0.5, max_workers=1)
        wall = time.monotonic() - t0
    job = {j["id"]: j for j in archive_db.list_jobs()}["transcribe-e2e-silent"]
    assert job["status"] == "done", f"no-speech job must finish done: {job}"
    assert job["progress"] == 1.0, f"no-speech job progress must end at 1.0: {job}"
    assert archive_db.transcript_for(PLATFORM, SILENT_VIDEO) == [], (
        "no-speech skip must write no transcript rows"
    )
    assert wall < 30, f"no-speech skip must not load the model ({wall:.1f}s wall)"
    print("=== run 3 (no-speech skip) ===")
    print(f"  job: {job}")
    print(f"  wall: {wall:.2f}s | model load: never (patched _get_model/_thread_model)")


def _run_once_requeue_exit() -> None:
    """--once must exit 0 (not spin) when a job requeues: the YouTube
    bot-gate cooldown requeues the same row until the freeze lifts, so the
    queue never drains — a --once worker that waits for a drain hangs
    forever holding the whisper model."""
    import threading
    from unittest.mock import patch

    job_id = "__requeue_e2e__"
    archive_db.upsert_video({
        "platform": "youtube",
        "video_id": "__requeue_vid__",
        "channel": "requeue-channel",
        "title": "requeue fixture",
        "status": "ready",
    })
    archive_db.enqueue_job(job_id, "transcribe", "youtube", "__requeue_vid__")
    with patch.object(
        archive_transcribe, "_process_job",
        side_effect=lambda job, multi=False: {"requeued": "youtube-gate"},
    ):
        t = threading.Thread(
            target=run_worker, kwargs={"once": True, "poll_interval": 0.05}
        )
        t.start()
        t.join(timeout=15.0)
    assert not t.is_alive(), "--once must exit after a requeue (no spin)"
    print("  --once: exits rc 0 on a requeued job (no spin)")


def _run() -> None:
    # Isolation guard: the scratch DB must start EMPTY. _migrate_db_to_data_dir
    # used to seed a fresh VODRIP_ARCHIVE_DB target with a copy of the real
    # %APPDATA% archive, so the worker claimed real user jobs instead of the
    # fixture's and this run failed on them, not on the fixture.
    _n = archive_db.query("SELECT COUNT(*) AS n FROM archive_jobs")[0]["n"]
    assert _n == 0, f"scratch DB not empty ({_n} jobs) — real archive leaked in"
    fixture = _build_fixture()
    archive_db.upsert_video({
        "platform": PLATFORM,
        "video_id": VIDEO_ID,
        "channel": "selftest",
        "title": "transcribe fixture",
        "status": "ready",
        "archive_path": str(fixture),
        "duration_sec": 20.0,
    })

    # --- run 1: full transcription through the real queue -----------------
    t0 = time.monotonic()
    job = _enqueue_and_run("transcribe-e2e-1")
    wall1 = time.monotonic() - t0
    assert job["status"] == "done", f"job 1 not done: {job}"
    assert job["progress"] == 1.0, f"job 1 progress must end at 1.0: {job}"

    # raw=True: this test asserts the STORAGE contract (contiguous seg_idx,
    # every written row) — the display read path dedupes overlapping cues.
    segs = archive_db.transcript_for(PLATFORM, VIDEO_ID, raw=True)
    assert segs, "no transcript segments written"
    idxs = [int(s["seg_idx"]) for s in segs]
    assert idxs == list(range(len(segs))), f"seg_idx must be contiguous 0..n: {idxs}"
    words = [
        w for s in segs
        for w in json.loads(s["words_json"] or "[]")
    ]
    assert words, "word-level timestamps missing (word_timestamps must be on)"
    assert all({"word", "start", "end"} <= set(w) for w in words), (
        "each word must carry word/start/end"
    )

    # Nothing may land inside the 10 s silence (margin 1 s at each edge).
    import subprocess as _sp
    import numpy as np

    ffmpeg = _resolve_ffmpeg_exe()
    raw = _sp.run(
        [ffmpeg, "-v", "error", "-i", str(SPEECH1), "-f", "f32le", "-ac", "1",
         "-ar", "16000", "-"],
        capture_output=True, timeout=60, creationflags=_NO_WINDOW,
    )
    speech1_sec = len(np.frombuffer(raw.stdout, dtype=np.float32)) / 16000.0
    silence_lo, silence_hi = speech1_sec + 1.0, speech1_sec + SILENCE_SEC - 1.0
    total_raw = _sp.run(
        [ffmpeg, "-v", "error", "-i", str(FIXTURE), "-f", "f32le", "-ac", "1",
         "-ar", "16000", "-"],
        capture_output=True, timeout=60, creationflags=_NO_WINDOW,
    )
    total_sec = len(np.frombuffer(total_raw.stdout, dtype=np.float32)) / 16000.0
    for s in segs:
        start, end = s["start_sec"], s["end_sec"]
        assert start >= -0.01 and end <= total_sec + 0.01, f"segment out of bounds: {s}"
        assert not (silence_lo <= start <= silence_hi), f"segment started inside silence: {s}"

    job1 = job
    # Disk-hygiene contract: a COMPLETED job deletes its resume manifest.
    assert not _manifest_path(PLATFORM, VIDEO_ID).exists(), (
        "completed job must delete its resume manifest"
    )
    assert job1["progress"] == 1.0

    # --- run 2: resume after deleting the last segment row ---------------
    # Simulate a crash mid-job: recreate the resume manifest with chunk 0
    # recorded (exactly what a job killed after chunk 0 would leave), then
    # re-run — only the missing chunk may re-run, and the manifest must be
    # deleted again once the job completes.
    audio = decode_audio(str(fixture))
    chunks = _plan_chunks(vad_speech_seconds(audio))
    chunk0_count = sum(1 for s in segs if s["end_sec"] <= speech1_sec + 0.01)
    assert chunk0_count > 0 and chunk0_count < len(segs), chunk0_count
    manifest = _manifest_path(PLATFORM, VIDEO_ID)
    _write_manifest_header(manifest, chunks, engine=_worker_engine(PLATFORM, VIDEO_ID))
    _append_manifest_entry(manifest, 0, int(segs[0]["seg_idx"]), chunk0_count)
    assert manifest.is_file(), "crash-state manifest must exist before resume run"

    last = segs[-1]
    last_id = archive_db.query(
        "SELECT id FROM transcripts WHERE platform=? AND video_id=? AND seg_idx=?",
        (PLATFORM, VIDEO_ID, last["seg_idx"]),
    )[0]["id"]
    # FTS index entry cascades via the AFTER DELETE trigger.
    archive_db.execute("DELETE FROM transcripts WHERE id=?", (last_id,))
    before_resume = archive_db.transcript_for(PLATFORM, VIDEO_ID, raw=True)
    assert len(before_resume) == len(segs) - 1

    job2 = _enqueue_and_run("transcribe-e2e-2")
    assert job2["status"] == "done", f"job 2 not done: {job2}"
    after_resume = archive_db.transcript_for(PLATFORM, VIDEO_ID, raw=True)
    # Every pre-existing row must be byte-identical (only the missing chunk ran).
    before_by_id = {r["id"]: r for r in before_resume}
    after_by_id = {r["id"]: r for r in after_resume}
    assert all(after_by_id[i] == r for i, r in before_by_id.items()), (
        "resume must not touch pre-existing rows"
    )
    # The deleted row's seg_idx must be restored at its old index, contiguous.
    after_idxs = [int(s["seg_idx"]) for s in after_resume]
    assert after_idxs == list(range(len(after_resume))), f"seg_idx gaps after resume: {after_idxs}"
    assert last["seg_idx"] in after_idxs, (
        f"resume must re-add the deleted seg_idx {last['seg_idx']}: {after_idxs}"
    )
    assert len(after_resume) > len(before_resume), "resume must re-add at least the deleted row"
    assert not manifest.exists(), (
        "completed resume run must delete its manifest again"
    )

    print("\n=== run 1 (full) ===")
    print(f"  wall: {wall1:.1f}s | job: {job1}")
    print(f"  segments: {len(segs)} | first: {segs[0]['start_sec']}s "
          f"last: {segs[-1]['end_sec']}s | words: {len(words)}")
    print("=== run 2 (resume) ===")
    print(f"  deleted seg_idx {last['seg_idx']} restored; pre-existing rows untouched "
          f"({len(before_resume)} -> {len(after_resume)} rows)")
    print(f"  manifest: {_manifest_path(PLATFORM, VIDEO_ID)}")
    print(f"  fixture: {fixture} ({fixture.stat().st_size} bytes)")

    _run_no_speech()
    _run_once_requeue_exit()

    # model cache size (download footprint)
    from services.archive_transcribe import _cache_dir

    cache = _cache_dir()
    size = 0
    for root, _dirs, files in os.walk(cache):
        size += sum((pathlib.Path(root) / f).stat().st_size for f in files)
    print(f"  model cache: {cache} ({size / 1e6:.0f} MB)")

    # Disk hygiene: the test created _TMP — remove it on success. (A crash
    # mid-run leaves it behind; the startup sweep reclaims those.)
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


def test_transcribe_worker_e2e_real() -> None:
    if pathlib.Path(os.environ["VODRIP_ARCHIVE_DB"]) != archive_db._db_path():
        import pytest

        pytest.skip(
            "archive_db already bound to another DB in this process — "
            "run standalone: python tests/test_archive_transcribe_e2e_real.py"
        )
    _run()


if __name__ == "__main__":
    import logging

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    _run()
    print("\nE2E OK — VAD stats above, resume verified.")
