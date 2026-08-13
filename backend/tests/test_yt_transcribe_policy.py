"""YouTube transcription policy — captions-first with ASR fallback.

Decision matrix under test (settings.yt_subtitles_first, default True):
  transcript rows exist                        -> skip (captions ARE the
                                                 transcript), job done
  no rows + captions_unavailable_at set        -> ASR: bestaudio downloaded
                                                 at transcribe time
  no rows + no marker (extract still pending)  -> requeue 'waiting for
                                                 caption decision', never
                                                 done, never ASR
  music/no-speech (VAD speech fraction <
    VODRIP_MUSIC_SPEECH_FRAC)                  -> transcript_kind='music',
                                                 done, never re-enqueued
  bot-gate during audio download               -> requeued (gate-aware),
                                                 never failed
  DRM/permanent download failure               -> transcript_kind='blocked',
                                                 terminal failed job
  yt_subtitles_first OFF                       -> always ASR (override)

Scratch env only: archive DB rebound to a temp file per module, settings
redirected to a scratch file, audio download + decode + VAD + ASR stubbed.
Run from backend/: python -m pytest tests/test_yt_transcribe_policy.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from deps import settings_mgr
from models.schemas import AppSettings
from services import archive_db, archive_scheduler, archive_transcribe, archive_ytdlp


@pytest.fixture(scope="module", autouse=True)
def _policy_scratch_db():
    """Rebind the shared archive conn to THIS module's scratch DB (same
    pattern as test_archive_yt_captions) and restore the env after."""
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(
        Path(tempfile.mkdtemp(prefix="yt-policy-test-")) / "archive.db")
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    if prev is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev
    archive_db._conn = None
    archive_db._schema_ready = False


@pytest.fixture(autouse=True)
def _reset_settings(tmp_path):
    """Redirect the shared settings manager to a scratch file (no real
    %APPDATA% writes; mirrors test_whisper_model_settings)."""
    original_file = settings_mgr._settings_file
    original_dir = settings_mgr._settings_dir
    scratch_dir = tmp_path / "VOD.RIP"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    settings_mgr._settings_dir = scratch_dir
    settings_mgr._settings_file = scratch_dir / "settings.json"
    settings_mgr._settings = AppSettings()
    yield
    settings_mgr._settings_file = original_file
    settings_mgr._settings_dir = original_dir


def _seed_youtube(vid: str) -> None:
    archive_db.upsert_video({
        "platform": "youtube",
        "video_id": vid,
        "channel": "chan",
        "title": f"yt {vid}",
        "kind": "vod",
        "status": "known",   # metadata-only: no archive_path (real YouTube)
        "duration_sec": 600.0,
    })


def _seed_job(vid: str) -> str:
    job_id = f"transcribe-youtube-{vid}"
    archive_db.enqueue_job(job_id, "transcribe", "youtube", vid, priority=0)
    return job_id


def _job_status(job_id: str) -> str:
    rows = archive_db.query("SELECT status FROM archive_jobs WHERE id = ?", (job_id,))
    return rows[0]["status"] if rows else None


def _job_rows(vid: str) -> list[dict]:
    return list(archive_db.query(
        "SELECT id FROM archive_jobs WHERE kind='transcribe' "
        "AND platform='youtube' AND video_id=?", (vid,),
    ))


def _fake_download(video_id: str, outdir):
    """download_bestaudio stand-in: write a fake audio file, return its path."""
    f = Path(outdir) / f"{video_id}.webm"
    f.write_bytes(b"fake audio")
    return f


# --- caption rows present -> skip (captions ARE the transcript) ------------

def test_caption_rows_present_skip():
    _seed_youtube("vp-skip")
    archive_db.insert_transcript("youtube", "vp-skip", [
        {"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "legenda."},
    ])
    job_id = _seed_job("vp-skip")
    with patch("deps.settings_mgr") as mgr, \
         patch.object(archive_transcribe, "_transcribe_youtube_captionless",
                      side_effect=AssertionError("ASR must not run")) as asr:
        mgr.get.return_value = SimpleNamespace(yt_subtitles_first=True)
        stats = archive_transcribe._process_job(
            {"id": job_id, "platform": "youtube", "video_id": "vp-skip"})
    assert stats["skipped"] == "captions-first"
    asr.assert_not_called()
    assert _job_status(job_id) == "done"


# --- captions_unavailable_at set -> worker runs the ASR pipeline -----------

def test_captionless_marker_runs_asr_full_pipeline(monkeypatch):
    """Marker set + no transcript rows -> the worker downloads bestaudio at
    transcribe time, decodes, VADs, ASRs, persists rows and finishes done."""
    _seed_youtube("vp-asr")
    archive_db.mark_captions_unavailable("youtube", "vp-asr")
    job_id = _seed_job("vp-asr")

    batch_out = [([{
        "start_sec": 0.0, "end_sec": 10.0, "text": "olá mundo",
        "words": [{"word": "olá", "start": 0.0, "end": 1.0}],
    }], "pt")]
    with patch("deps.settings_mgr") as mgr, \
         patch.object(archive_ytdlp, "download_bestaudio",
                      side_effect=_fake_download), \
         patch.object(archive_transcribe, "_should_shard", return_value=False), \
         patch.object(archive_transcribe, "decode_audio",
                      return_value=np.zeros(
                          archive_transcribe.SAMPLE_RATE * 10, dtype=np.float32)), \
         patch.object(archive_transcribe, "vad_speech_seconds",
                      return_value=[(0.0, 10.0)]), \
         patch.object(archive_transcribe, "_job_engine", return_value="whisper"), \
         patch.object(archive_transcribe, "_current_model", return_value=object()), \
         patch.object(archive_transcribe, "_transcribe_batch",
                      return_value=batch_out) as batch, \
         patch.object(archive_transcribe, "_effective_device",
                      return_value=("cpu", "int8")):
        mgr.get.return_value = SimpleNamespace(yt_subtitles_first=True)
        stats = archive_transcribe._process_job(
            {"id": job_id, "platform": "youtube", "video_id": "vp-asr"})
    assert "skipped" not in stats
    assert stats["segments"] == 1
    batch.assert_called_once()
    assert _job_status(job_id) == "done"
    rows = archive_db.transcript_for("youtube", "vp-asr")
    assert rows and rows[0]["text"] == "olá mundo", "ASR rows must be persisted"


# --- no captions + no marker -> wait, never done, never ASR ----------------

def test_no_captions_no_marker_waits_and_scheduler_skips():
    _seed_youtube("vp-wait")
    job_id = _seed_job("vp-wait")
    with patch("deps.settings_mgr") as mgr, \
         patch.object(archive_transcribe, "_transcribe_youtube_captionless",
                      side_effect=AssertionError("ASR must not run")) as asr:
        mgr.get.return_value = SimpleNamespace(yt_subtitles_first=True)
        stats = archive_transcribe._process_job(
            {"id": job_id, "platform": "youtube", "video_id": "vp-wait"})
    assert stats["requeued"] == "waiting-caption"
    asr.assert_not_called()
    assert _job_status(job_id) == "queued", "never resolved done"
    row = archive_db.query(
        "SELECT error, next_retry_at FROM archive_jobs WHERE id = ?", (job_id,))[0]
    assert "waiting for caption decision" in row["error"]
    assert row["next_retry_at"] is not None, "must re-check after a delay"

    # Scheduler: captions pending (no marker) -> never creates a job.
    archive_db.execute("DELETE FROM archive_jobs WHERE id = ?", (job_id,))
    archive_scheduler._enqueue_transcriptions()
    assert archive_db.latest_job("youtube", "vp-wait", kind="transcribe") is None


# --- music/no-speech -> transcript_kind='music', done, never re-enqueued ---

def test_music_fraction_marks_music_and_never_reenqueues():
    """A captionless video whose VAD speech fraction is below
    VODRIP_MUSIC_SPEECH_FRAC (instrumental music) is marked
    transcript_kind='music', resolved done with NO ASR run, and never
    re-enqueued by the scheduler."""
    _seed_youtube("vp-music")
    archive_db.mark_captions_unavailable("youtube", "vp-music")
    job_id = _seed_job("vp-music")

    with patch("deps.settings_mgr") as mgr, \
         patch.object(archive_ytdlp, "download_bestaudio",
                      side_effect=_fake_download), \
         patch.object(archive_transcribe, "_should_shard", return_value=False), \
         patch.object(archive_transcribe, "decode_audio",
                      return_value=np.zeros(
                          archive_transcribe.SAMPLE_RATE * 600, dtype=np.float32)), \
         patch.object(archive_transcribe, "vad_speech_seconds",
                      return_value=[(0.0, 1.0)]), \
         patch.object(archive_transcribe, "_job_engine", return_value="whisper"), \
         patch.object(archive_transcribe, "_transcribe_batch",
                      side_effect=AssertionError("ASR must not run")) as batch:
        mgr.get.return_value = SimpleNamespace(yt_subtitles_first=True)
        stats = archive_transcribe._process_job(
            {"id": job_id, "platform": "youtube", "video_id": "vp-music"})
    assert stats["skipped"] == "music"
    batch.assert_not_called()
    assert _job_status(job_id) == "done"
    assert archive_db.video_transcript_kind("youtube", "vp-music") == "music"

    # Terminal verdict: the scheduler never enqueues it again.
    archive_scheduler._enqueue_transcriptions()
    assert [r["id"] for r in _job_rows("vp-music")] == [job_id]


# --- yt_subtitles_first OFF -> always ASR (explicit override) --------------

def test_toggle_off_asr_even_with_captions():
    _seed_youtube("vp-off")
    archive_db.insert_transcript("youtube", "vp-off", [
        {"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "legenda."},
    ])
    job_id = _seed_job("vp-off")
    with patch("deps.settings_mgr") as mgr, \
         patch.object(archive_transcribe, "_transcribe_youtube_captionless",
                      return_value={"segments": 1}) as asr:
        mgr.get.return_value = SimpleNamespace(yt_subtitles_first=False)
        stats = archive_transcribe._process_job(
            {"id": job_id, "platform": "youtube", "video_id": "vp-off"})
    assert "skipped" not in stats
    asr.assert_called_once()
    assert _job_status(job_id) == "done"


# --- bot-gate during the audio download -> gate-aware requeue --------------

def test_bot_gate_during_download_requeues():
    """The gate arming MID-download must requeue (never fail) — no retry
    storm: status stays queued, attempts stay 0, the error names the gate."""
    _seed_youtube("vp-gate")
    archive_db.mark_captions_unavailable("youtube", "vp-gate")
    job_id = _seed_job("vp-gate")
    with patch("deps.settings_mgr") as mgr, \
         patch.object(archive_ytdlp, "download_bestaudio",
                      side_effect=RuntimeError(
                          "Sign in to confirm you're not a bot")):
        mgr.get.return_value = SimpleNamespace(yt_subtitles_first=True)
        stats = archive_transcribe._process_job(
            {"id": job_id, "platform": "youtube", "video_id": "vp-gate"})
    assert stats["requeued"] == "youtube-gate"
    assert _job_status(job_id) == "queued"
    row = archive_db.query(
        "SELECT error, attempts FROM archive_jobs WHERE id = ?", (job_id,))[0]
    assert "bot-gate" in row["error"]
    assert row["attempts"] == 0, "requeue must not count as a failure"


# --- DRM / permanent download failure -> terminal 'blocked' ----------------

def test_drm_permanent_failure_marks_blocked():
    """A permanent audio-download failure (DRM/protected) marks the video
    transcript_kind='blocked' (terminal) and fails the job with the real
    reason — the scheduler never re-enqueues it, not even a stale-failed
    row."""
    _seed_youtube("vp-drm")
    archive_db.mark_captions_unavailable("youtube", "vp-drm")
    job_id = _seed_job("vp-drm")
    with patch("deps.settings_mgr") as mgr, \
         patch.object(archive_ytdlp, "download_bestaudio",
                      side_effect=RuntimeError(
                          "This video is protected by DRM")):
        mgr.get.return_value = SimpleNamespace(yt_subtitles_first=True)
        stats = archive_transcribe._process_job(
            {"id": job_id, "platform": "youtube", "video_id": "vp-drm"})
    assert "error" in stats
    assert _job_status(job_id) == "failed", "DownloadError is terminal"
    assert archive_db.video_transcript_kind("youtube", "vp-drm") == "blocked"
    row = archive_db.query("SELECT error FROM archive_jobs WHERE id = ?", (job_id,))[0]
    assert row["error"].startswith("DownloadError"), "real reason, terminal class"
    assert "protected by DRM" in row["error"]

    # Stale-failed row: ONLY the terminal verdict keeps it from the
    # scheduler's stale-failed requeue.
    archive_db.execute(
        "UPDATE archive_jobs SET updated_at='2020-01-01T00:00:00Z' WHERE id=?",
        (job_id,))
    archive_scheduler._enqueue_transcriptions()
    assert [r["id"] for r in _job_rows("vp-drm")] == [job_id], (
        "blocked video must never be re-enqueued"
    )


# --- scheduler create rule -------------------------------------------------

def test_scheduler_creates_youtube_job_only_with_marker_and_no_transcripts():
    """The scheduler creates a YouTube transcribe job ONLY when the video
    has no transcript rows AND captions_unavailable_at is set. Never while
    captions are pending (no marker); never when transcript rows exist."""
    _seed_youtube("vp-sched")
    archive_scheduler._enqueue_transcriptions()
    assert archive_db.latest_job("youtube", "vp-sched", kind="transcribe") is None, (
        "no marker -> captions pending -> never created"
    )

    archive_db.mark_captions_unavailable("youtube", "vp-sched")
    archive_scheduler._enqueue_transcriptions()
    job = archive_db.latest_job("youtube", "vp-sched", kind="transcribe")
    assert job is not None, "marker + no transcripts -> ASR candidate"
    assert job["id"] == "transcribe-youtube-vp-sched", "stable job id"
    assert job["status"] == "queued"

    archive_db.insert_transcript("youtube", "vp-sched", [
        {"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "legenda."},
    ])
    archive_scheduler._enqueue_transcriptions()
    assert len(_job_rows("vp-sched")) == 1, "transcripts exist -> no second job"


def test_scheduler_skips_music_and_blocked_videos():
    """Terminal transcript_kind verdicts (music / blocked) keep the
    scheduler from creating a transcribe job even when the marker is set."""
    _seed_youtube("vp-music2")
    archive_db.mark_captions_unavailable("youtube", "vp-music2")
    archive_db.mark_video_transcript_kind("youtube", "vp-music2", "music")
    archive_scheduler._enqueue_transcriptions()
    assert archive_db.latest_job("youtube", "vp-music2", kind="transcribe") is None

    _seed_youtube("vp-blocked2")
    archive_db.mark_captions_unavailable("youtube", "vp-blocked2")
    archive_db.mark_video_transcript_kind("youtube", "vp-blocked2", "blocked")
    archive_scheduler._enqueue_transcriptions()
    assert archive_db.latest_job("youtube", "vp-blocked2", kind="transcribe") is None
