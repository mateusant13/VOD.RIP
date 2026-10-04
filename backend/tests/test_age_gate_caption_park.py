"""Age-gated caption fetch: park it, once, reversibly.

The defect this pins: the caption sweep classified an age-gate refusal as the
IP-level bot gate (the refusal text "Sign in to confirm your age" CONTAINS the
bot gate's "sign in to confirm" marker), and that branch deliberately writes no
per-video marker because a bot wall is IP state, not a verdict about the video.
Correct reasoning, wrong verdict: an age gate IS a per-video verdict. So the
same video was re-attempted on an endless loop — 33 identical rate-limit events
in 24h, all naming one video, and it could never succeed without cookies.

The fix parks it the way the transcribe path already parks an age-gated JOB
(archive_ytdlp.ingest_video -> AGE_GATE_JOB_MARKER -> archive_db.update_job ->
archive_scheduler._requeue_failed_transcribe_job): terminal now, reversible
later. Reversibility here means the per-video captions_unavailable_at marker
plus an explicit un-park once an authenticated YouTube session exists.

The bot-gate branch is deliberately NOT changed — a genuine IP bot wall must
still avoid a per-video marker. That is pinned too, so this fix cannot be
"reverted" by removing the distinction.

Seams monkeypatched (_deep_enumerate / _deep_fetch_transcript) — no network.

Run from backend/: python -m pytest tests/test_age_gate_caption_park.py
"""
from __future__ import annotations

import asyncio
import time

import pytest

from routers import archive
from services import archive_db

# The real yt-dlp failure text for an age-gated video fetched anonymously
# (same string the transcribe path sees).
AGE_GATE_ERROR = (
    "[youtube] VzuPKrGl0z8: Sign in to confirm your age. This video may be "
    "inappropriate for some users. Use --cookies-from-browser or --cookies "
    "for the authentication."
)
# The genuine IP-level bot wall, which must KEEP its no-marker behaviour.
BOT_GATE_ERROR = (
    "[youtube] VzuPKrGl0z8: Sign in to confirm you're not a bot. "
    "Use --cookies-from-browser or --cookies for the authentication."
)


def _video(vid: str, created: str = "2024-01-03T00:00:00+00:00") -> dict:
    return {
        "id": vid,
        "title": "age gated",
        "url": f"https://www.youtube.com/watch?v={vid}",
        "created_at": created,
        "channel": "deepchan",
        "content_kind": "video",
        "duration": 60,
        "duration_string": "1:00",
        "views": 10,
        "thumbnail_url": None,
    }


@pytest.fixture()
def fast_pace(monkeypatch):
    """No pacing sleep in tests — the gate/pacing itself is covered elsewhere."""
    monkeypatch.setattr(archive, "_DEEP_MIN_GAP_S", 0.0)


@pytest.fixture(autouse=True)
def isolate_deep_jobs():
    """Deep jobs and the age-park registry are module-global; the no-captions
    marker is PERSISTED per video. Clear all three, or one test's park
    pre-skips the next test's video (they share a video id) and the whole
    suite silently measures the wrong thing.
    """
    def _clear() -> None:
        with archive._deep_jobs_lock:
            archive._deep_jobs.clear()
        with archive._age_park_lock:
            archive._age_parked.clear()
        try:
            archive_db.execute(
                "UPDATE videos SET captions_unavailable_at = NULL "
                "WHERE platform = 'youtube'"
            )
        except Exception:  # noqa: BLE001 — schema/DB probe, never a test failure
            pass

    _clear()
    yield
    with archive._deep_jobs_lock:
        for j in archive._deep_jobs.values():
            j["cancel"].set()
    _clear()


@pytest.fixture()
def no_session(monkeypatch):
    """No signed-in YouTube session (the state the 24h events were recorded in)."""
    import services.youtube_session as ys

    monkeypatch.setattr(ys, "youtube_session_configured", lambda: False, raising=False)
    return False


@pytest.fixture()
def signed_in(monkeypatch):
    """An authenticated YouTube session exists."""
    import services.youtube_session as ys

    monkeypatch.setattr(ys, "youtube_session_configured", lambda: True, raising=False)
    return True


@pytest.fixture()
def no_bot_gate_freeze(monkeypatch):
    """Let a bot-gate sweep reach a terminal state inside the test.

    A real bot wall arms a 1800s process-wide freeze and _wait_gate parks the
    sweep until it lifts — correct production behaviour, and covered by its own
    suite. Only the FREEZE is neutralised here: the classifier still returns
    True, so the branch under test (a bot wall writes no per-video marker) runs
    exactly as it does in production. Nothing is asserted away.
    """
    from services import yt_gate

    monkeypatch.setattr(yt_gate, "note_youtube_gate", lambda *a, **kw: None)
    monkeypatch.setattr(yt_gate, "youtube_gate_active", lambda: False)
    yt_gate.clear_youtube_gate()
    yield
    yt_gate.clear_youtube_gate()


def _run(monkeypatch, videos, fetcher) -> dict:
    """Patch seams, start the sweep through the endpoint + thread, return job."""
    monkeypatch.setattr(
        archive, "_deep_enumerate", lambda handle: (videos, False, len(videos))
    )
    monkeypatch.setattr(archive, "_deep_fetch_transcript", fetcher)
    job_id = asyncio.run(
        archive.archive_search_deep_start(
            archive.DeepSearchRequest(channel="deepchan", query="cesar")
        )
    )["job_id"]
    for _ in range(400):
        with archive._deep_jobs_lock:
            job = dict(archive._deep_jobs[job_id])
        if job["status"] != "running":
            return job
        time.sleep(0.025)
    raise AssertionError("deep job did not settle in 10s")


def test_age_gated_video_is_attempted_once_then_parked(monkeypatch, fast_pace, no_session):
    """THE defect: one attempt, one park, no second attempt in the same loop.

    The old code classified this refusal as the bot gate and RETRIED it
    immediately (and stamped nothing), so every sweep re-attempted it.
    """
    calls: list[str] = []

    def fetcher(vid: str) -> dict:
        calls.append(vid)
        raise RuntimeError(AGE_GATE_ERROR)

    job = _run(monkeypatch, [_video("VzuPKrGl0z8")], fetcher)

    assert calls == ["VzuPKrGl0z8"], (
        f"age-gated video must be attempted exactly once, got {len(calls)} attempts"
    )
    assert job["status"] == "done"
    assert job["age_parked"] == 1, "the sweep must report WHY a video has no captions"
    # Counted as no-transcript too — the user asked for a transcript and got none.
    assert job["no_transcript"] == 1
    # The per-video marker is what stops the next sweep.
    assert archive_db.captions_unavailable_at("youtube", "VzuPKrGl0z8")


def test_parked_video_is_not_reattempted_by_a_later_sweep(
    monkeypatch, fast_pace, no_session
):
    """A re-sweep pre-skips the park: the endless ~30-minute loop is gone."""
    calls: list[str] = []

    def fetcher(vid: str) -> dict:
        calls.append(vid)
        raise RuntimeError(AGE_GATE_ERROR)

    videos = [_video("VzuPKrGl0z8")]
    _run(monkeypatch, videos, fetcher)
    _run(monkeypatch, videos, fetcher)
    _run(monkeypatch, videos, fetcher)

    assert calls == ["VzuPKrGl0z8"], (
        f"parked video re-attempted {len(calls)} times across 3 sweeps; must stay 1"
    )


def test_park_is_reversible_once_a_session_is_configured(
    monkeypatch, fast_pace, no_session, signed_in
):
    """Signing in releases the park: the video becomes processable again.

    This is what separates a park from the irreversible 'blocked' verdict — the
    owner signs in, and the video drains without a manual DB edit.
    """
    calls: list[str] = []

    def fetcher(vid: str) -> dict:
        calls.append(vid)
        raise RuntimeError(AGE_GATE_ERROR)

    videos = [_video("VzuPKrGl0z8")]
    _run(monkeypatch, videos, fetcher)
    assert len(calls) == 1
    assert archive_db.captions_unavailable_at("youtube", "VzuPKrGl0z8")

    # The session now exists (the `signed_in` fixture re-patched the predicate).
    assert archive._unpark_age_gated_if_authenticated() == 1
    assert not archive_db.captions_unavailable_at("youtube", "VzuPKrGl0z8")
    assert archive._age_parked_snapshot() == {}

    # ...so the next sweep genuinely re-attempts it.
    _run(monkeypatch, videos, fetcher)
    assert len(calls) == 2, "an authenticated sweep must re-attempt the un-parked video"


def test_park_stays_put_while_no_session_exists(monkeypatch, fast_pace, no_session):
    """The whole point: no credentials configured -> stay parked, don't churn."""
    calls: list[str] = []

    def fetcher(vid: str) -> dict:
        calls.append(vid)
        raise RuntimeError(AGE_GATE_ERROR)

    videos = [_video("VzuPKrGl0z8")]
    _run(monkeypatch, videos, fetcher)
    assert archive._unpark_age_gated_if_authenticated() == 0
    _run(monkeypatch, videos, fetcher)
    assert len(calls) == 1
    assert archive._age_parked_snapshot(), "the park must survive with no session"


def test_park_reason_names_the_cause_and_the_remedy(monkeypatch, fast_pace, no_session):
    """Parking must be visible: the video says WHY it has no captions and what
    would fix it, in the same two states the age gate reports everywhere else
    (cookie-bridge `youtube_authenticated` / age_gate_actionable_message)."""
    def fetcher(vid: str) -> dict:
        raise RuntimeError(AGE_GATE_ERROR)

    _run(monkeypatch, [_video("VzuPKrGl0z8")], fetcher)

    reason = archive._age_parked_snapshot()["VzuPKrGl0z8"]
    low = reason.lower()
    assert "age-restricted" in low, reason
    assert "no signed-in youtube session is configured" in low, reason
    assert "cookie bridge" in low, f"must name the remedy, got: {reason}"

    # And it reaches the user through the videos API.
    body = asyncio.run(archive.archive_videos(platform="youtube"))
    row = [v for v in body["videos"] if v.get("video_id") == "VzuPKrGl0z8"]
    assert row, "parked video missing from the videos API"
    assert row[0]["captions_parked_reason"] == reason


def test_sweep_status_endpoint_reports_the_park(monkeypatch, fast_pace, no_session):
    """The FE polls this every ~2s, so the why has to be on the job too."""
    def fetcher(vid: str) -> dict:
        raise RuntimeError(AGE_GATE_ERROR)

    videos = [_video("VzuPKrGl0z8")]
    monkeypatch.setattr(
        archive, "_deep_enumerate", lambda handle: (videos, False, len(videos))
    )
    monkeypatch.setattr(archive, "_deep_fetch_transcript", fetcher)
    job_id = asyncio.run(
        archive.archive_search_deep_start(
            archive.DeepSearchRequest(channel="deepchan", query="cesar")
        )
    )["job_id"]
    for _ in range(400):
        with archive._deep_jobs_lock:
            if archive._deep_jobs[job_id]["status"] != "running":
                break
        time.sleep(0.025)
    snap = asyncio.run(archive.archive_search_deep_status(job_id))
    assert snap["age_parked"] == 1


def test_channel_caption_pump_parks_age_gated_video(monkeypatch, no_session):
    """The channel-add caption pump is the OTHER fetch path that hit this."""
    calls: list[str] = []

    def fetcher(vid: str) -> dict:
        calls.append(vid)
        raise RuntimeError(AGE_GATE_ERROR)

    monkeypatch.setattr(archive, "_deep_fetch_transcript", fetcher)
    assert archive._paced_caption_fetch("VzuPKrGl0z8", "deepchan") is False
    assert calls == ["VzuPKrGl0z8"]
    assert archive_db.captions_unavailable_at("youtube", "VzuPKrGl0z8")
    assert "VzuPKrGl0z8" in archive._age_parked_snapshot()


def test_genuine_bot_gate_still_writes_no_per_video_marker(
    monkeypatch, fast_pace, no_session, no_bot_gate_freeze
):
    """The IP gate keeps its no-marker behaviour — that branch is correct for a
    bot wall, and this fix must not have flattened the two verdicts together."""

    def fetcher(vid: str) -> dict:
        raise RuntimeError(BOT_GATE_ERROR)

    _run(monkeypatch, [_video("VzuPKrGl0z8")], fetcher)

    assert not archive_db.captions_unavailable_at("youtube", "VzuPKrGl0z8"), (
        "a bot wall is IP state — stamping it would poison the video row"
    )
    assert archive._age_parked_snapshot() == {}, "a bot wall is not an age-gate park"
