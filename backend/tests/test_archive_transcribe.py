"""Tests for archive_transcribe live-stream guard and age-gate handling.

Covers:
  * Live stream IDs (twitch-live-*, kick-live-*, youtube-live-*) are skipped
    gracefully by _process_job — no crash, warning logged, job marked done;
  * YouTube age-gate errors in Portuguese are classified as permanent
    download errors (no infinite retry).
"""
from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="archive-transcribe-guard-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")

from services import archive_db  # noqa: E402
from services import archive_transcribe as at  # noqa: E402
from services.archive_ytdlp import _is_permanent_download_error  # noqa: E402


@pytest.fixture
def scratch_db(monkeypatch, tmp_path):
    """Fresh archive DB per test; module connection rebound to tmp path."""
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(tmp_path / "archive.db"))
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    archive_db._conn = None
    archive_db._schema_ready = False


def _seed_video(platform: str, vid: str, **kwargs) -> None:
    archive_db.upsert_video({
        "platform": platform,
        "video_id": vid,
        "channel": kwargs.get("channel", "test_channel"),
        "title": kwargs.get("title", "test title"),
        "started_at": kwargs.get("started_at", "2026-08-01T00:00:00Z"),
        "duration_sec": kwargs.get("duration_sec", 600.0),
        "kind": kwargs.get("kind", "vod"),
    })


def _make_running_job(platform: str, vid: str) -> dict:
    """Enqueue a transcribe job and force it to 'running' (bypasses the
    internal claim machinery so _process_job can be called directly)."""
    job_id = f"transcribe-{platform}-{vid}"
    archive_db.enqueue_job(job_id, "transcribe", platform, vid)
    archive_db.update_job(job_id, status="running")
    rows = archive_db.query("SELECT * FROM archive_jobs WHERE id=?", (job_id,))
    return dict(rows[0])


# --- _is_live_stream_id unit tests ----------------------------------------


def test_live_stream_id_detection():
    """Live-capture synthetic IDs are detected."""
    assert at._is_live_stream_id("twitch-live-kingsman265_twitch-1785814298042")
    assert at._is_live_stream_id("kick-live-cellbit-1785788650972")
    assert at._is_live_stream_id("youtube-live-@titiltei-1785650000000")


def test_real_vod_ids_not_flagged_live():
    """Numeric VOD IDs and non-live IDs are NOT flagged."""
    assert not at._is_live_stream_id("1785814298042")
    assert not at._is_live_stream_id("dQw4w9WgXcQ")
    assert not at._is_live_stream_id("kick-some-vod-123")
    assert not at._is_live_stream_id("youtube-some-vod-123")


# --- _process_job live-stream skip -----------------------------------------


def test_process_job_skips_live_stream_ids(scratch_db):
    """A transcribe job with a live-stream video_id is skipped — no crash,
    job marked done, 'skipped: live-stream' returned."""
    vid = "twitch-live-fake-12345678"
    _seed_video("twitch", vid, kind="live")
    job = _make_running_job("twitch", vid)
    result = at._process_job(job)
    assert result["skipped"] == "live-stream"
    row = archive_db.query(
        "SELECT status FROM archive_jobs WHERE id=?", (job["id"],)
    )
    assert row and row[0]["status"] == "done"


def test_process_job_skips_kick_live_stream(scratch_db):
    """Kick live-stream IDs are also skipped."""
    vid = "kick-live-fake-12345678"
    _seed_video("kick", vid, kind="live")
    job = _make_running_job("kick", vid)
    result = at._process_job(job)
    assert result["skipped"] == "live-stream"


def test_process_job_skips_youtube_live_stream(scratch_db):
    """YouTube live-stream IDs are also skipped (caught before YouTube verdict)."""
    vid = "youtube-live-@fake-12345678"
    _seed_video("youtube", vid, kind="live")
    job = _make_running_job("youtube", vid)
    result = at._process_job(job)
    assert result["skipped"] == "live-stream"


# --- YouTube age-gate classification ---------------------------------------


def test_portuguese_age_gate_is_permanent():
    """Portuguese age-gate message is classified as a permanent error."""
    exc = Exception("ERROR: [youtube] 0FH0wZfZ82Q: Faça login para confirmar sua idade")
    assert _is_permanent_download_error(exc)


def test_english_age_gate_is_permanent():
    """English age-gate message is still classified as permanent."""
    exc = Exception("ERROR: [youtube] abc123: Sign in to confirm your age")
    assert _is_permanent_download_error(exc)


def test_broad_portuguese_age_gate_is_permanent():
    """Broader Portuguese age-gate pattern is classified as permanent."""
    exc = Exception("Para visualizar este video, confirme sua idade.")
    assert _is_permanent_download_error(exc)


def test_network_error_not_permanent():
    """A transient network error is NOT classified as permanent."""
    exc = Exception("Connection reset by peer")
    assert not _is_permanent_download_error(exc)
