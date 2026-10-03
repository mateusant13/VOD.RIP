"""Queue policy — the knobs that decide WHAT gets transcribed and HOW MANY.

Covers the four rules that used to be inline in the scheduler/worker:
latest-N-per-channel selection, the single-sourced captions-first verdict
(with force-transcribe), the one-VOD-at-a-time job cap, and user focus.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ["VODRIP_ARCHIVE_DB"] = str(
    Path(tempfile.mkdtemp(prefix="queue-policy-")) / "archive.db")

import pytest  # noqa: E402

from services import archive_db, queue_policy  # noqa: E402


def _video(vid: str, channel: str, started: str, *, platform: str = "twitch",
           duration: float = 600.0, status: str = "ready") -> None:
    archive_db.upsert_video({
        "platform": platform,
        "video_id": vid,
        "channel": channel,
        "title": f"{channel} {vid}",
        "started_at": started,
        "duration_sec": duration,
        "status": status,
    })


@pytest.fixture
def _clean():
    def _wipe():
        archive_db.execute("DELETE FROM videos WHERE channel LIKE 'qp-%'")
        archive_db.execute("DELETE FROM archive_jobs WHERE id LIKE 'qp-%'")
        archive_db.clear_user_focus()
    _wipe()
    yield _wipe
    _wipe()


# --- latest N per channel ------------------------------------------------
def test_only_latest_n_per_channel_are_candidates(_clean, monkeypatch):
    """The 6th-newest video of a channel is NOT a candidate (N=5)."""
    for i in range(8):
        _video(f"qp-old{i}", "qp-chan-a", f"2026-01-{i + 1:02d}T00:00:00Z")
    got = queue_policy.latest_per_channel_candidates()
    vids = [r["video_id"] for r in got]
    assert len(vids) == 5, vids
    # The newest five, and specifically NOT the three oldest.
    assert vids == ["qp-old7", "qp-old6", "qp-old5", "qp-old4", "qp-old3"], vids


def test_candidates_are_recency_ordered_not_shortest_first(_clean):
    """The regression this replaces: `ORDER BY duration_sec ASC LIMIT 50`.

    The newest video here is the LONGEST one, so the old query would have
    ranked it last and (with a small pool) dropped it entirely."""
    _video("qp-rec-new", "qp-chan-b", "2026-06-01T00:00:00Z", duration=100_000.0)
    _video("qp-rec-old", "qp-chan-b", "2026-05-01T00:00:00Z", duration=5.0)
    got = [r["video_id"] for r in queue_policy.latest_per_channel_candidates()]
    assert got[0] == "qp-rec-new", got


def test_per_channel_cap_does_not_starve_other_channels(_clean):
    """A busy channel must not crowd out a quiet channel's recent VODs.

    This is the starvation bug: 50 shortest globally meant a channel with
    hundreds of clips evicted every recent video of every other channel."""
    for i in range(40):
        _video(f"qp-busy{i}", "qp-busy-chan", f"2026-03-{i + 1:02d}T00:00:00Z",
               duration=float(i + 1))
    _video("qp-quiet", "qp-quiet-chan", "2026-07-09T00:00:00Z", duration=900.0)
    got = [r["video_id"] for r in queue_policy.latest_per_channel_candidates()]
    assert "qp-quiet" in got, got
    assert len([v for v in got if v.startswith("qp-busy")]) == 5


def test_transcribed_videos_are_never_candidates(_clean):
    _video("qp-done", "qp-chan-c", "2026-07-01T00:00:00Z")
    archive_db.insert_transcript("twitch", "qp-done", [
        {"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "hi"}])
    got = [r["video_id"] for r in queue_policy.latest_per_channel_candidates()]
    assert "qp-done" not in got


def test_latest_per_channel_is_configurable(_clean, monkeypatch):
    for i in range(4):
        _video(f"qp-cfg{i}", "qp-chan-d", f"2026-04-{i + 1:02d}T00:00:00Z")
    monkeypatch.setenv(queue_policy.ENV_LATEST_PER_CHANNEL, "2")
    assert queue_policy.latest_per_channel() == 2
    assert len(queue_policy.latest_per_channel_candidates()) == 2
    # 0 disables candidate selection entirely (explicit enqueue only).
    monkeypatch.setenv(queue_policy.ENV_LATEST_PER_CHANNEL, "0")
    assert queue_policy.latest_per_channel_candidates() == []


# --- single-sourced routing verdict --------------------------------------
def test_verdict_wait_caption_then_run_asr(_clean):
    _video("qp-yt", "qp-chan-e", "2026-07-01T00:00:00Z", platform="youtube")
    # No captions and no availability marker -> the question is still open.
    assert queue_policy.transcript_route_verdict("youtube", "qp-yt") == "wait-caption"
    archive_db.execute(
        "UPDATE videos SET captions_unavailable_at = ? WHERE platform='youtube' "
        "AND video_id = ?", ("2026-07-01T00:00:00+00:00", "qp-yt"))
    assert queue_policy.transcript_route_verdict("youtube", "qp-yt") == "run-asr"


def test_verdict_skip_captions_when_rows_exist(_clean):
    _video("qp-yt2", "qp-chan-f", "2026-07-01T00:00:00Z", platform="youtube")
    archive_db.insert_transcript("youtube", "qp-yt2", [
        {"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "hi"}])
    assert queue_policy.transcript_route_verdict("youtube", "qp-yt2") == "skip-captions"


def test_force_transcribe_bypasses_caption_question_but_not_terminal(_clean):
    _video("qp-yt3", "qp-chan-g", "2026-07-01T00:00:00Z", platform="youtube")
    assert queue_policy.transcript_route_verdict("youtube", "qp-yt3") == "wait-caption"
    assert queue_policy.transcript_route_verdict(
        "youtube", "qp-yt3", force_transcribe=True) == "run-asr"
    # Terminal verdicts are facts about the media, not about captions: a
    # forced run must not turn a music-only or DRM'd video into ASR work.
    archive_db.execute(
        "UPDATE videos SET transcript_kind = 'music' WHERE platform='youtube' "
        "AND video_id = ?", ("qp-yt3",))
    assert queue_policy.transcript_route_verdict(
        "youtube", "qp-yt3", force_transcribe=True) == "music"


def test_non_youtube_always_runs_asr(_clean):
    assert queue_policy.transcript_route_verdict("twitch", "qp-x") == "run-asr"
    assert queue_policy.transcript_route_verdict("kick", "qp-x") == "run-asr"


def test_worker_verdict_wrapper_matches_shared_helper(_clean):
    """The worker must not carry its own copy of the matrix any more."""
    from services import archive_transcribe as at

    _video("qp-yt4", "qp-chan-h", "2026-07-01T00:00:00Z", platform="youtube")
    assert at._youtube_transcribe_verdict("youtube", "qp-yt4") == (
        queue_policy.transcript_route_verdict("youtube", "qp-yt4"))
    archive_db.execute(
        "UPDATE videos SET captions_unavailable_at = ? WHERE platform='youtube' "
        "AND video_id = ?", ("2026-07-01T00:00:00+00:00", "qp-yt4"))
    assert at._youtube_transcribe_verdict("youtube", "qp-yt4") == "run-asr"


# --- one VOD at a time ---------------------------------------------------
def test_concurrency_default_is_one_and_configurable(monkeypatch):
    monkeypatch.delenv(queue_policy.ENV_JOB_CONCURRENCY, raising=False)
    assert queue_policy.transcribe_job_concurrency() == 1
    monkeypatch.setenv(queue_policy.ENV_JOB_CONCURRENCY, "0")
    assert queue_policy.transcribe_job_concurrency() == 0
    monkeypatch.setenv(queue_policy.ENV_JOB_CONCURRENCY, "3")
    assert queue_policy.transcribe_job_concurrency() == 3
    # Garbage must not crash the worker loop.
    monkeypatch.setenv(queue_policy.ENV_JOB_CONCURRENCY, "abc")
    assert queue_policy.transcribe_job_concurrency() == 1


def test_auto_transcribe_defaults_on_and_toggleable(monkeypatch):
    monkeypatch.delenv(queue_policy.ENV_AUTO_TRANSCRIBE, raising=False)
    assert queue_policy.auto_transcribe_enabled() is True
    monkeypatch.setenv(queue_policy.ENV_AUTO_TRANSCRIBE, "0")
    assert queue_policy.auto_transcribe_enabled() is False


# --- user focus ----------------------------------------------------------
def test_focus_recorded_and_expires(_clean):
    assert queue_policy.active_focus() is None
    archive_db.set_user_focus("twitch", "qp-focus-1")
    assert queue_policy.active_focus() == ("twitch", "qp-focus-1")
    # A second focus REPLACES the first: exactly one item holds focus.
    archive_db.set_user_focus("twitch", "qp-focus-2")
    assert queue_policy.active_focus() == ("twitch", "qp-focus-2")
    # An old record is ignored (and pruned) — a crashed app cannot wedge the
    # queue. Backdated in the DB rather than with max_age_s=0: focused_at is
    # second-granular, so a zero window still counts the current second as
    # fresh (the comparison is inclusive by design).
    archive_db.execute(
        "UPDATE user_focus SET focused_at = '2000-01-01T00:00:00+00:00'")
    assert queue_policy.active_focus() is None
    assert queue_policy.active_focus() is None  # prune is idempotent
    assert archive_db.query("SELECT 1 FROM user_focus") == [], (
        "an expired focus row must be deleted, not just ignored")


def test_focus_can_be_disabled(_clean, monkeypatch):
    archive_db.set_user_focus("twitch", "qp-focus-3")
    monkeypatch.setenv(queue_policy.ENV_FOCUS_PAUSE, "0")
    assert queue_policy.active_focus() is None
    monkeypatch.delenv(queue_policy.ENV_FOCUS_PAUSE, raising=False)
    assert queue_policy.active_focus() == ("twitch", "qp-focus-3")
    archive_db.clear_user_focus()
    assert queue_policy.active_focus() is None
