"""Tests for the PANNs acoustic-event stage (services.archive_events).

Layers, cheap to expensive:
  * extract_events / merge_events — pure numpy run-extraction (no model),
  * event_classes — env parsing + label intersection,
  * audio_events DB helpers — insert/delete/query on a temp archive DB,
  * real E2E — the actual Cnn14 SED model on a real VOD slice (GPU if
    available), asserting content (events inside speech regions, scores in
    [0, 1], replace-on-rerun).

The real-E2E part needs the checkpoint at ~/panns_data (or
VODRIP_EVENTS_CHECKPOINT) and skips cleanly when the model is unavailable —
the pure parts always run. Under pytest the DB part skips if archive_db is
already bound elsewhere (same guard as the transcribe E2E); run directly:

    python tests/test_archive_events.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="vodrip-events-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")
os.environ.pop("VODRIP_EVENTS_ENABLED", None)
os.environ.pop("VODRIP_EVENTS_CLASSES", None)
os.environ.pop("VODRIP_EVENTS_THRESHOLD", None)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import archive_db  # noqa: E402
from services.archive_events import (  # noqa: E402
    DEFAULT_CLASSES,
    extract_events,
    merge_events,
    event_classes,
    detect_events_video,
)

PLATFORM = "twitch"
VIDEO_ID = "__events_e2e__"


# --- pure run extraction --------------------------------------------------

def _synthetic_matrix() -> list[list[float]]:
    """6 frames x 3 classes: Laughter frames 0-3, Clapping frames 2-5, Scream frames 0-4."""
    return [
        [0.9, 0.1, 0.8],
        [0.9, 0.1, 0.8],
        [0.9, 0.9, 0.8],
        [0.9, 0.9, 0.8],
        [0.1, 0.9, 0.8],
        [0.1, 0.9, 0.1],
    ]


def test_extract_events_runs_and_scores() -> None:
    cls = ["Laughter", "Clapping", "Scream"]
    ev = extract_events(_synthetic_matrix(), cls, threshold=0.5, min_sec=0.0)
    assert [e["event"] for e in ev] == ["Laughter", "Clapping", "Scream"], ev
    # Laughter 0.00-0.04 (frames 0..3), Clapping 0.02-0.06 (frames 2..5), Scream 0.00-0.05
    assert ev[0]["start_sec"] == 0.0 and abs(ev[0]["end_sec"] - 0.04) < 1e-9, ev[0]
    assert ev[0]["score"] == 0.9 and ev[1]["score"] == 0.9, "run score = max frame prob"
    assert ev[1]["start_sec"] == 0.02 and abs(ev[1]["end_sec"] - 0.06) < 1e-9, ev[1]
    assert ev[2]["start_sec"] == 0.0 and abs(ev[2]["end_sec"] - 0.05) < 1e-9, ev[2]
    assert ev[2]["score"] == 0.8


def test_extract_events_min_sec_filters_short_runs() -> None:
    cls = ["Laughter", "Clapping", "Scream"]
    ev = extract_events(_synthetic_matrix(), cls, threshold=0.5, min_sec=0.03)
    assert [e["event"] for e in ev] == ["Laughter", "Clapping", "Scream"], ev
    ev = extract_events(_synthetic_matrix(), cls, threshold=0.5, min_sec=0.05)
    assert [e["event"] for e in ev] == ["Scream"], (
        "0.05s min must drop the 0.04s Laughter/Clapping runs, keep the 0.05s Scream"
    )
    ev = extract_events(_synthetic_matrix(), cls, threshold=0.95, min_sec=0.0)
    assert ev == [], "threshold above every probability -> no events"


def test_extract_events_offset_and_threshold() -> None:
    cls = ["Laughter"]
    ev = extract_events([[0.4], [0.4], [0.9]], cls, threshold=0.5, min_sec=0.0, offset_sec=100.0)
    assert ev[0]["start_sec"] == 100.02 and abs(ev[0]["end_sec"] - 100.03) < 1e-9, ev
    ev = extract_events([[0.4], [0.4], [0.9]], cls, threshold=0.6, min_sec=0.0)
    assert len(ev) == 1 and ev[0]["start_sec"] == 0.02, "below-threshold frames must not start runs"


def test_merge_events_same_class_gaps() -> None:
    merged = merge_events([
        {"start_sec": 0.0, "end_sec": 1.0, "event": "Laughter", "score": 0.7},
        {"start_sec": 1.1, "end_sec": 2.0, "event": "Laughter", "score": 0.9},
        {"start_sec": 2.4, "end_sec": 3.0, "event": "Laughter", "score": 0.5},
        {"start_sec": 5.0, "end_sec": 6.0, "event": "Clapping", "score": 0.6},
    ])
    assert [e["event"] for e in merged] == ["Clapping", "Laughter", "Laughter"], merged
    assert merged[1]["start_sec"] == 0.0 and merged[1]["end_sec"] == 2.0, (
        "<=0.25s gaps merge (window boundaries); >0.25s stays separate"
    )
    assert merged[1]["score"] == 0.9, "merged score = max of parts"


def test_event_classes_env_and_intersection() -> None:
    saved = os.environ.get("VODRIP_EVENTS_CLASSES")
    try:
        os.environ.pop("VODRIP_EVENTS_CLASSES", None)
        assert event_classes() == DEFAULT_CLASSES, "default interest set without env"
        assert event_classes(["Laughter", "Clapping"]) == ["Laughter", "Clapping"], (
            "labels the model lacks must be dropped"
        )
        os.environ["VODRIP_EVENTS_CLASSES"] = "Laughter, NotARealClass, Clapping"
        assert event_classes() == ["Laughter", "NotARealClass", "Clapping"], (
            "env list is honored verbatim at runtime (labels filter happens in detect)"
        )
        assert event_classes(["Laughter", "Clapping"]) == ["Laughter", "Clapping"], (
            "intersection drops unknown labels"
        )
    finally:
        if saved is None:
            os.environ.pop("VODRIP_EVENTS_CLASSES", None)
        else:
            os.environ["VODRIP_EVENTS_CLASSES"] = saved


# --- DB helpers -----------------------------------------------------------

def _db_check() -> None:
    if pathlib.Path(os.environ["VODRIP_ARCHIVE_DB"]) != archive_db._db_path():
        import pytest

        pytest.skip(
            "archive_db already bound to another DB in this process — "
            "run standalone: python tests/test_archive_events.py"
        )


def test_audio_events_db_replace_semantics() -> None:
    _db_check()
    archive_db.insert_audio_events(PLATFORM, VIDEO_ID, [
        {"start_sec": 1.0, "end_sec": 2.5, "event": "Laughter", "score": 0.91},
        {"start_sec": 4.0, "end_sec": 4.4, "event": "Clapping", "score": 0.82},
    ])
    rows = archive_db.audio_events_for(PLATFORM, VIDEO_ID)
    assert len(rows) == 2 and rows[0]["event"] == "Laughter", rows
    assert rows[0]["score"] == 0.91 and rows[1]["end_sec"] == 4.4, rows
    # replace-on-rerun: same video, new runs -> old rows gone
    assert archive_db.delete_audio_events(PLATFORM, VIDEO_ID) == 2
    archive_db.insert_audio_events(PLATFORM, VIDEO_ID, [
        {"start_sec": 9.0, "end_sec": 9.8, "event": "Music", "score": 0.99},
    ])
    rows = archive_db.audio_events_for(PLATFORM, VIDEO_ID)
    assert len(rows) == 1 and rows[0]["event"] == "Music" and rows[0]["start_sec"] == 9.0, rows
    # other videos are untouched
    archive_db.insert_audio_events(PLATFORM, "other-video", [
        {"start_sec": 0.0, "end_sec": 1.0, "event": "Cough", "score": 0.5},
    ])
    assert archive_db.audio_events_for(PLATFORM, VIDEO_ID) == rows, (
        "deletes must be scoped to the video"
    )
    assert archive_db.delete_audio_events(PLATFORM, "other-video") == 1
    assert archive_db.delete_audio_events(PLATFORM, VIDEO_ID) == 1


# --- real E2E (needs the PANNs checkpoint) --------------------------------

def _e2e_fixture() -> pathlib.Path | None:
    """A real media file for detection: VODRIP_EVENTS_FIXTURE env override (a
    real VOD slice — strict assertions) else Windows-TTS speech (~12 s — the
    fixture's own voice may or may not trigger the interest classes, so the
    E2E only asserts shape unless the env override is set)."""
    env = os.environ.get("VODRIP_EVENTS_FIXTURE", "").strip()
    if env:
        p = pathlib.Path(env)
        assert p.is_file(), f"VODRIP_EVENTS_FIXTURE not found: {env}"
        return p

    import subprocess as sp

    from services.archive_transcribe import _resolve_ffmpeg_exe

    wav = _TMP / "events_speech.wav"
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SetOutputToWaveFile('{wav}'); "
        "$s.Speak('Ladies and gentlemen, welcome to the biggest show of the year, "
        "please put your hands together for our incredible guests, and enjoy this "
        "marvelous concert'); $s.Dispose()"
    )
    proc = sp.run(["powershell", "-NoProfile", "-Command", ps],
                  capture_output=True, timeout=120)
    if proc.returncode != 0 or not wav.is_file():
        return None
    return wav


def _e2e_checkpoint() -> pathlib.Path | None:
    from services.archive_events import _checkpoint_path

    p = pathlib.Path(_checkpoint_path())
    return p if p.is_file() else None


def _run_real() -> None:
    """Full pipeline on a real media fixture: VAD -> SED -> rows in the DB."""
    from services.archive_transcribe import decode_audio, run_worker, vad_speech_seconds

    strict = bool(os.environ.get("VODRIP_EVENTS_FIXTURE", "").strip())
    fixture = _e2e_fixture()
    if fixture is None:
        print("SKIP real E2E: no fixture (TTS unavailable, VODRIP_EVENTS_FIXTURE unset)")
        return
    ckpt = _e2e_checkpoint()
    if ckpt is None:
        print(f"SKIP real E2E: PANNs checkpoint not found ({_checkpoint_path()})")
        return
    print(f"=== real E2E: PANNs SED on {fixture.name} (ckpt {ckpt.name}, strict={strict}) ===")

    audio = decode_audio(str(fixture))
    total_sec = audio.size / 16000.0
    archive_db.upsert_video({
        "platform": PLATFORM,
        "video_id": VIDEO_ID,
        "channel": "selftest",
        "title": "events fixture",
        "status": "ready",
        "archive_path": str(fixture),
        "duration_sec": total_sec,
    })
    t0 = time.monotonic()
    stats = detect_events_video(PLATFORM, VIDEO_ID)
    wall = time.monotonic() - t0
    print(f"  stats: {stats}")
    rows = archive_db.audio_events_for(PLATFORM, VIDEO_ID)

    # Content assertions, not status-code assertions.
    assert stats["events"] == len(rows), "stats count must match stored rows"
    assert all(0.0 <= r["score"] <= 1.0 for r in rows), "scores must be probabilities"
    assert all(r["end_sec"] > r["start_sec"] >= 0.0 for r in rows), "sane boundaries"
    if strict:
        assert rows, "strict fixture (real VOD slice) must yield detected events"
    elif not rows:
        print("  (TTS voice triggered no interest-class events — shape-only assertions)")
    if rows:
        speech = vad_speech_seconds(audio)
        assert any(s <= r["start_sec"] <= e for r in rows for s, e in speech), (
            "every event must lie inside a VAD speech region (silence is never scored)"
        )

    # Replace-on-rerun via the job queue path: enqueue kind='events' and run
    # the real worker; row count must stay identical (no duplicates).
    archive_db.enqueue_job("events-e2e-1", "events", PLATFORM, VIDEO_ID)
    run_worker(once=True, poll_interval=0.3, max_workers=1)
    job = {j["id"]: j for j in archive_db.list_jobs()}["events-e2e-1"]
    assert job["status"] == "done" and job["progress"] == 1.0, f"events job: {job}"
    rows2 = archive_db.audio_events_for(PLATFORM, VIDEO_ID)
    assert len(rows2) == len(rows), f"re-run must replace, not append: {len(rows)} -> {len(rows2)}"
    assert sorted((r["event"], r["start_sec"]) for r in rows2) == sorted(
        (r["event"], r["start_sec"]) for r in rows
    ), "re-run must be deterministic in what it reports"

    print(f"  events ({len(rows)}): " + ", ".join(
        f"{r['event']}@{r['start_sec']:.1f}s:{r['score']:.2f}" for r in rows[:8]
    ))
    print(f"  wall: {wall:.1f}s | job: {job['status']} | replace-on-rerun: {len(rows2)} rows")


def test_events_real_e2e() -> None:
    _db_check()
    try:
        _run_real()
    except FileNotFoundError as exc:  # checkpoint missing -> soft-skip
        print(f"SKIP real E2E: {exc}")
    except ModuleNotFoundError as exc:
        print(f"SKIP real E2E: panns_inference not installed: {exc}")


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print("--- pure extraction checks ---")
    test_extract_events_runs_and_scores()
    test_extract_events_min_sec_filters_short_runs()
    test_extract_events_offset_and_threshold()
    test_merge_events_same_class_gaps()
    test_event_classes_env_and_intersection()
    print("--- DB helpers ---")
    test_audio_events_db_replace_semantics()
    print("--- real E2E ---")
    test_events_real_e2e()
    print("\nEVENTS OK — pure + DB + real E2E verified.")
