"""Review fixes (batch P1-2, P2-3, P2-4) around the transcribe audio path.

Covers:

  * P1-2: the fetch watchdog — a slow HLS/ffmpeg download refreshes the job
    row's heartbeat mid-run, so the 45 min reclaim window can never fire
    while a legitimate fetch is in flight; the transcribe stale window is
    now 45 min (was: 30 min == the fetch timeout, so a reclaim could
    double-download the same VOD);
  * P2-3: the Twitch chat sweep heartbeats BEFORE every page fetch — a
    429-stormed page (backoff sleeps) can no longer push the row past
    _CHAT_HEARTBEAT_STALE and let a second lane claim the job mid-sweep;
  * P2-4: the parakeet->whisper fallback reuses the audio downloaded by the
    first engine attempt (one HLS fetch for the whole job, not two).

No network: the fetch (sp.run / GQL playback / _post_comments_page) is
monkeypatched everywhere. Fresh VODRIP_ARCHIVE_DB per test.
"""
from __future__ import annotations

import os
import pathlib
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="archive-transcribe-download-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")

from services import archive_db  # noqa: E402  (env must be set first)
from services import archive_twitch  # noqa: E402
from services import archive_transcribe as at  # noqa: E402


@pytest.fixture
def scratch_db(monkeypatch, tmp_path):
    """Fresh archive DB per test; module connection rebound to tmp path."""
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(tmp_path / "archive.db"))
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    archive_db._conn = None
    archive_db._schema_ready = False


def _seed_twitch(vid: str, duration_sec: float = 600.0) -> None:
    archive_db.upsert_video({
        "platform": "twitch",
        "video_id": vid,
        "channel": "cellbit",
        "title": f"vod {vid}",
        "started_at": "2026-08-01T00:00:00Z",
        "kind": "vod",
        "duration_sec": duration_sec,
    })


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")


def _node(offset: float, text: str = "hi") -> dict:
    return {
        "id": f"c{int(offset)}",
        "contentOffsetSeconds": offset,
        "createdAt": "2026-08-01T20:00:00Z",
        "commenter": {"id": f"u{int(offset)}", "login": "lubu", "displayName": "lubu"},
        "message": {"fragments": [{"text": text}], "userBadges": []},
    }


# --- P1-2: fetch watchdog + widened reclaim window -------------------------


def test_fetch_heartbeat_watchdog_refreshes_job_mid_fetch(scratch_db, monkeypatch, tmp_path):
    """A slow ffmpeg HLS fetch must refresh the job row's heartbeat while
    it runs — the 45 min reclaim window can never fire mid-download."""
    _seed_twitch("hb-0001")
    job_id = "transcribe-twitch-hb-0001"
    archive_db.enqueue_job(job_id, "transcribe", "twitch", "hb-0001", priority=0)
    archive_db.update_job(job_id, status="running")
    at._job_id_tls.job_id = job_id
    try:
        monkeypatch.setattr(at, "_FETCH_HEARTBEAT_INTERVAL_S", 0.02)
        monkeypatch.setattr(
            "services.twitch_gql_service.get_vod_playback_sync",
            lambda video_id: (
                "http://hls.test/master.m3u8",
                {"Origin": "https://www.twitch.tv"},
                [{"name": "audio_only", "tbr": 128, "url": "http://hls.test/a.m3u8"}],
            ),
        )

        def fake_run(cmd, **kwargs):
            time.sleep(0.15)  # a throttled CDN — sp.run blocks for 150 ms
            pathlib.Path(cmd[-1]).write_bytes(b"\x00" * 64)  # the out wav
            return type("Proc", (), {"returncode": 0, "stderr": b""})()

        monkeypatch.setattr(at.sp, "run", fake_run)
        # Backdate the heartbeat: the watchdog (not the claim-time stamp)
        # must be what refreshes it during the fetch.
        stale = _iso(timedelta(minutes=-40))
        archive_db.execute(
            "UPDATE archive_jobs SET heartbeat=?, updated_at=? WHERE id=?",
            (stale, stale, job_id),
        )

        at._fetch_remote_audio_wav("twitch", "hb-0001", "cellbit", tmp_path / "a.wav")

        row = archive_db.query(
            "SELECT heartbeat FROM archive_jobs WHERE id=?", (job_id,)
        )[0]
        assert row["heartbeat"] and row["heartbeat"] > stale, (
            "the watchdog must refresh the job row while the fetch is in flight"
        )
    finally:
        at._job_id_tls.job_id = None


def test_transcribe_reclaim_window_is_45min(scratch_db):
    """P1-2 contract: _STALE_JOB_TIMEDELTA must sit ABOVE the fetch timeout
    (30 min) + watchdog gap — a running job with a 40-min-old heartbeat is
    still live; only past 45 min is it reclaimable."""
    assert at._STALE_JOB_TIMEDELTA == timedelta(minutes=45), (
        "the transcribe reclaim window must exceed the 30 min fetch timeout"
    )
    _seed_twitch("wnd-0001")
    job_id = "transcribe-twitch-wnd-0001"
    archive_db.enqueue_job(job_id, "transcribe", "twitch", "wnd-0001", priority=0)
    archive_db.update_job(job_id, status="running")

    archive_db.execute(
        "UPDATE archive_jobs SET heartbeat=?, updated_at=? WHERE id=?",
        (_iso(timedelta(minutes=-40)), _iso(timedelta(minutes=-40)), job_id),
    )
    assert at._claim_next_job() is None, (
        "a 40-min-old heartbeat is inside the 45 min window — must NOT be reclaimed"
    )

    archive_db.execute(
        "UPDATE archive_jobs SET heartbeat=?, updated_at=? WHERE id=?",
        (_iso(timedelta(minutes=-46)), _iso(timedelta(minutes=-46)), job_id),
    )
    claimed = at._claim_next_job()
    assert claimed is not None and claimed["id"] == job_id, (
        "past the 45 min window a wedged job IS reclaimed"
    )


# --- P2-3: chat sweep heartbeats BEFORE every page fetch -------------------


def test_chat_backfill_heartbeats_before_page_fetch(scratch_db, monkeypatch):
    """A stormed page (long backoff inside the fetch) must find the job row
    already refreshed by the pre-fetch heartbeat — a stale row could
    otherwise be claimed by a second lane mid-sweep."""
    _seed_twitch("hbch-0001", duration_sec=600.0)
    job_id = "tw-backfill-hbch-0001"
    archive_db.enqueue_job(job_id, "chat", "twitch", "hbch-0001")
    archive_db.update_job(job_id, status="running")
    # 19 min stale heartbeat: inside the 20 min _CHAT_HEARTBEAT_STALE window,
    # but ONLY the pre-fetch heartbeat keeps it fresh through the storm.
    stale = _iso(timedelta(minutes=-19))
    archive_db.execute(
        "UPDATE archive_jobs SET heartbeat=?, updated_at=? WHERE id=?",
        (stale, stale, job_id),
    )

    seen = {"fetches": 0, "hb_at_first_fetch": None}

    def fake_page(vid, offset, size):
        seen["fetches"] += 1
        if seen["fetches"] == 1:
            row = archive_db.query(
                "SELECT heartbeat FROM archive_jobs WHERE id=?", (job_id,)
            )[0]
            seen["hb_at_first_fetch"] = row["heartbeat"]
        time.sleep(0.05)  # the storm: no stored-row progress while fetching
        return [_node(5.0), _node(10.0)]

    monkeypatch.setattr(archive_twitch, "_post_comments_page", fake_page)
    archive_twitch._backfill_locked(
        job_id, "hbch-0001", "cellbit", 0.0, 600.0,
        max_messages=2, page_size=2, seed_offset_sec=None,
        progress_cb=None, interactive=False,
    )

    hb = seen["hb_at_first_fetch"]
    assert hb and hb > stale, (
        "P2-3: the pre-fetch heartbeat must refresh the row before the page fetch"
    )
    row = archive_db.query(
        "SELECT heartbeat, status FROM archive_jobs WHERE id=?", (job_id,)
    )[0]
    assert row["heartbeat"] > stale and row["status"] == "done"


# --- P2-4: whisper fallback reuses the downloaded audio --------------------


def test_whisper_fallback_reuses_downloaded_audio(scratch_db, monkeypatch):
    """parakeet fails mid-job -> whisper retries the SAME job — the audio
    downloaded for the first attempt must be reused, not fetched twice."""
    _seed_twitch("reuse-0001")
    job_id = "transcribe-twitch-reuse-0001"
    archive_db.enqueue_job(job_id, "transcribe", "twitch", "reuse-0001", priority=0)

    calls = {"fetch": 0, "core": 0}
    fetch_outdirs = []

    def fake_fetch(platform, video_id, channel, out_wav):
        calls["fetch"] += 1
        fetch_outdirs.append(out_wav.parent)
        out_wav.write_bytes(b"\x00" * 64)

    monkeypatch.setattr(at, "_fetch_remote_audio_wav", fake_fetch)
    monkeypatch.setattr(at, "_should_shard", lambda path, ffmpeg_bin=None: False)
    monkeypatch.setattr(at, "_job_engine", lambda language: "parakeet")
    monkeypatch.setattr(at, "_resolve_job_language", lambda p, v: None)

    def fake_core(platform, video_id, audio_path, language, progress_cb, events_cb,
                  t0, sharded=False, shard_dir=None):
        calls["core"] += 1
        if calls["core"] == 1:
            raise RuntimeError("parakeet decode error: index out of range")
        return {"segments": 5, "lang": "pt"}

    monkeypatch.setattr(at, "_transcribe_audio_source", fake_core)

    stats = at._process_job({
        "id": job_id, "kind": "transcribe",
        "platform": "twitch", "video_id": "reuse-0001",
    })

    assert calls["core"] == 2, "parakeet fails once, whisper succeeds"
    assert calls["fetch"] == 1, (
        "P2-4: the whisper retry must reuse the stashed audio — one fetch per job"
    )
    assert stats["segments"] == 5
    assert not fetch_outdirs[0].exists(), "the job-level finally releases the stash dir"
    job = archive_db.latest_job("twitch", "reuse-0001", kind="transcribe")
    assert job is not None and job["status"] == "done"
