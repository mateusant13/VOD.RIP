"""Cross-platform transcription skip: a Kick/Twitch VOD whose mirrored live
exists on a higher-priority platform (youtube > twitch > kick) with transcript
rows already gets its whisper job skipped — the same canonical_key rule the
kick download dedupe uses (archive_kick.dedupe_decision).

Run from backend/: python -m pytest tests/test_transcribe_cross_platform.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ["VODRIP_ARCHIVE_DB"] = str(
    Path(tempfile.mkdtemp(prefix="transcribe-cross-")) / "archive.db"
)

from services import archive_db  # noqa: E402  (env must be set before import)
from services import archive_transcribe  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _cross_scratch_db():
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = os.environ["VODRIP_ARCHIVE_DB"]
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False
    archive_db.get_conn()
    yield
    if prev is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False


def _seed_video(platform: str, video_id: str, key: str) -> None:
    archive_db.upsert_video({
        "platform": platform,
        "video_id": video_id,
        "channel": "titiltei",
        "title": f"{platform} mirror {video_id}",
        "canonical_key": key,
        "started_at": "2026-08-03T17:24:00Z",
        "kind": "vod",
    })


def _seed_transcript(platform: str, video_id: str) -> None:
    archive_db.insert_transcript(
        platform, video_id,
        [{"seg_idx": 0, "text": "crosstalk", "start_sec": 0.0, "end_sec": 2.0}],
    )


def test_kick_skipped_when_youtube_mirror_transcribed():
    _seed_video("kick", "k1", "ck-shared")
    _seed_video("youtube", "y1", "ck-shared")
    _seed_transcript("youtube", "y1")
    assert archive_db.transcribed_on_higher_priority_platform("kick", "k1") is True


def test_kick_skipped_when_twitch_mirror_transcribed():
    _seed_video("kick", "k2", "ck-t2")
    _seed_video("twitch", "t2", "ck-t2")
    _seed_transcript("twitch", "t2")
    assert archive_db.transcribed_on_higher_priority_platform("kick", "k2") is True


def test_higher_priority_never_skipped_by_lower():
    # A youtube row is never blocked by a transcribed kick row (one-way rule).
    _seed_video("kick", "k3", "ck-oneway")
    _seed_video("youtube", "y3", "ck-oneway")
    _seed_transcript("kick", "k3")
    assert archive_db.transcribed_on_higher_priority_platform("youtube", "y3") is False


def test_no_group_or_no_transcript_is_false():
    _seed_video("kick", "k4", "ck-lonely")
    _seed_video("youtube", "y4", "ck-untranscribed")
    _seed_transcript("youtube", "y4")
    # Same key, but nothing transcribed yet on the youtube side.
    assert archive_db.transcribed_on_higher_priority_platform("kick", "k-lonely") is False
    assert archive_db.transcribed_on_higher_priority_platform("kick", "k4") is False
    # Different keys: no group match.
    _seed_video("kick", "k5", "ck-different")
    _seed_transcript("youtube", "y4")
    assert archive_db.transcribed_on_higher_priority_platform("kick", "k5") is False


def test_unknown_platform_is_false():
    assert archive_db.transcribed_on_higher_priority_platform("soundcloud", "s1") is False


# --- worker-level: the job completes done + skipped, whisper never runs ---

def _seed_job(platform: str, video_id: str) -> str:
    job_id = f"job-{platform}-{video_id}"
    archive_db.enqueue_job(job_id, "transcribe", platform, video_id)
    return job_id


def _job_status(job_id: str) -> str:
    rows = archive_db.query("SELECT status FROM archive_jobs WHERE id = ?", (job_id,))
    return rows[0]["status"] if rows else None


def test_process_job_skips_kick_when_mirror_transcribed(monkeypatch):
    _seed_video("kick", "k6", "ck-worker")
    _seed_video("youtube", "y6", "ck-worker")
    _seed_transcript("youtube", "y6")
    job_id = _seed_job("kick", "k6")
    with patch.object(
        archive_transcribe, "transcribe_video",
        side_effect=AssertionError("whisper must not run"),
    ) as tv:
        stats = archive_transcribe._process_job(
            {"id": job_id, "platform": "kick", "video_id": "k6"}
        )
    assert stats["skipped"] == "dedupe-transcribed"
    tv.assert_not_called()
    assert _job_status(job_id) == "done"


def test_process_job_runs_whisper_when_no_higher_priority_mirror(monkeypatch):
    _seed_video("kick", "k7", "ck-nomirror")
    job_id = _seed_job("kick", "k7")
    with patch.object(
        archive_transcribe, "transcribe_video",
        return_value={"segments": 1},
    ) as tv:
        stats = archive_transcribe._process_job(
            {"id": job_id, "platform": "kick", "video_id": "k7"}
        )
    assert "skipped" not in stats
    tv.assert_called_once()
    assert _job_status(job_id) == "done"


def test_process_job_youtube_not_skipped_by_kick_mirror(monkeypatch):
    _seed_video("kick", "k8", "ck-ytjob")
    _seed_video("youtube", "y8", "ck-ytjob")
    _seed_transcript("kick", "k8")
    job_id = _seed_job("youtube", "y8")
    with patch.object(
        archive_transcribe, "transcribe_video",
        return_value={"segments": 1},
    ) as tv:
        stats = archive_transcribe._process_job(
            {"id": job_id, "platform": "youtube", "video_id": "y8"}
        )
    assert "skipped" not in stats
    tv.assert_called_once()
    assert _job_status(job_id) == "done"
