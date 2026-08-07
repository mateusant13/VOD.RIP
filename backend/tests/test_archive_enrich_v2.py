"""Search-v2 slice — merge fix, channel_hint understanding, targeted enrichment.

Covers the WT-A contracts with a scratch archive DB and no network:
  * merge fix: two videos (one chat-heavy, one transcript-heavy) produce
    merged hits containing BOTH kinds, and no video exceeds the 3-hit cap;
  * (a) enrichment candidates: title-token relevance picks the right videos;
  * (b) channel_hint fires for a first-token channel slug and NOT for a
        non-channel token (and explicit channel param wins);
  * (c) transcribe enqueue is skipped when worker_live() is False, and runs
        (deterministic job id) once a worker is live;
  * archive_smart_enrich=False disables the whole enrichment pass.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="archive-enrich-v2-"))
_DB = _TMP / "archive.db"
os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)

from services import archive_db  # noqa: E402  (env must be set first)
from routers.archive import _maybe_enrich, archive_search  # noqa: E402


def _upsert(platform: str, video_id: str, **kw) -> None:
    archive_db.upsert_video({
        "platform": platform,
        "video_id": video_id,
        "channel": kw.get("channel", "chan"),
        "title": kw.get("title", "title"),
        "status": kw.get("status", "known"),
        "kind": kw.get("kind", "vod"),
        "started_at": kw.get("started_at"),
        "duration_sec": kw.get("duration_sec"),
        "archive_path": kw.get("archive_path"),
    })


def _reset_enrichment_state() -> None:
    """Zero all throttle/cooldown/in-flight clocks between tests."""
    import routers.archive as ar

    with ar._backfill_lock:
        ar._last_auto_kick = 0.0
        ar._backfill_inflight.clear()
        ar._backfill_attempted_at.clear()
    with ar._transcribe_lock:
        ar._last_transcribe_kick = 0.0
        ar._transcribe_attempted_at.clear()


# --- merge fix --------------------------------------------------------------


def test_merge_keeps_both_kinds_and_caps_three_per_video():
    archive_db.execute("DELETE FROM messages WHERE video_id IN ('merge-a','merge-b')")
    archive_db.execute("DELETE FROM transcripts WHERE video_id IN ('merge-a','merge-b')")
    _upsert("twitch", "merge-a", channel="aa", title="alpha",
            started_at="2026-08-01T00:00:00Z")
    _upsert("youtube", "merge-b", channel="bb", title="beta",
            started_at="2026-08-02T00:00:00Z")
    archive_db.insert_messages(
        "twitch", "merge-a",
        [{"offset_sec": float(i), "username": "u", "text": "mergeterm"} for i in range(4)],
    )
    archive_db.insert_transcript(
        "youtube", "merge-b",
        [{"seg_idx": i, "start_sec": float(i), "end_sec": float(i + 1),
          "text": "mergeterm"} for i in range(4)],
    )
    hits = archive_db.search("mergeterm", limit=10)
    assert {"message", "transcript"} <= {h["kind"] for h in hits}, (
        "merged hits must contain BOTH chat and transcript kinds"
    )
    per_video: dict[str, int] = {}
    for h in hits:
        per_video[h["video_id"]] = per_video.get(h["video_id"], 0) + 1
    assert set(per_video) == {"merge-a", "merge-b"}
    assert max(per_video.values()) <= 3, "no video may exceed the 3-hit cap"
    # Newest-first contract: scores are normalized per table (best hit of a
    # table scores 1.0) but the merged list orders by video date desc first
    # (score desc only breaks within-date ties), so merge-b (newer) leads
    # and the dates are monotonically descending.
    assert hits[0]["video_id"] == "merge-b", "the newer video must lead"
    assert [h["date"] for h in hits] == sorted(
        (h["date"] for h in hits), reverse=True
    ), "text-search hits must be newest-first"
    assert {h["kind"] for h in hits[:6]} == {"message", "transcript"}
    # BM25 is corpus-relative: leftover rows would shift avgdl and flip
    # cross-table ties in OTHER tests sharing this DB — clean up.
    archive_db.execute("DELETE FROM messages WHERE video_id='merge-a'")
    archive_db.execute("DELETE FROM transcripts WHERE video_id='merge-b'")
    archive_db.execute("DELETE FROM videos WHERE video_id IN ('merge-a','merge-b')")


# --- (a) enrichment candidates: title-token relevance -----------------------


async def test_chat_enrichment_picks_relevance_matches(monkeypatch):
    import routers.archive as ar

    for vid in ("1234001", "1234002", "1234003", "twitch-live-fake-999"):
        archive_db.execute("DELETE FROM videos WHERE video_id=?", (vid,))
        archive_db.execute("DELETE FROM messages WHERE video_id=?", (vid,))
    _upsert("twitch", "1234001", channel="caedrel", title="gaming review marathon",
            started_at="2026-08-01T00:00:00Z")
    _upsert("twitch", "1234002", channel="caedrel", title="gaming stream",
            started_at="2026-08-02T00:00:00Z")
    _upsert("twitch", "1234003", channel="caedrel", title="cooking show",
            started_at="2026-08-03T00:00:00Z")
    # Watchdog rows are chat-less but not real VODs — must never be kicked.
    _upsert("twitch", "twitch-live-fake-999", channel="caedrel", title="gaming now",
            started_at="2026-08-04T00:00:00Z")
    release = threading.Event()
    calls: list[str] = []

    def fake_backfill(channel, video_id, **kwargs):
        calls.append(video_id)
        release.wait(5)
        return {"inserted": 0}

    monkeypatch.setattr("services.archive_twitch.backfill_chat", fake_backfill)
    _reset_enrichment_state()
    try:
        kicked = _maybe_enrich(platform="twitch", channel=None, source="both",
                               q="gaming review")
        await asyncio.sleep(0.05)  # background tasks need a loop tick
        assert sorted(calls) == ["1234001", "1234002"], (
            "the two videos whose titles carry the query tokens must be kicked"
        )
        assert "twitch-live-fake-999" not in calls, (
            "watchdog synthetic rows must be excluded from auto-backfill"
        )
        assert [e["video_id"] for e in kicked] == ["1234001", "1234002"]
        assert all(e["kind"] == "chat" for e in kicked)
        assert kicked[0]["title"] == "gaming review marathon"
    finally:
        release.set()
        for _ in range(100):
            with ar._backfill_lock:
                if not ar._backfill_inflight:
                    break
            await asyncio.sleep(0.02)
        _reset_enrichment_state()
        for vid in ("1234001", "1234002", "1234003", "twitch-live-fake-999"):
            archive_db.execute("DELETE FROM videos WHERE video_id=?", (vid,))


# --- (b) channel_hint understanding -----------------------------------------


def test_channel_hint_fires_for_first_token_channel_only():
    _upsert("youtube", "hint-vid", channel="TiTiltei", title="beta")
    archive_db.insert_messages(
        "youtube", "hint-vid",
        [{"offset_sec": 1.0, "username": "u", "text": "zebra stripe"}],
    )
    try:
        box: list[str] = []
        hits = archive_db.search("titiltei zebra", _channel_hint_out=box)
        assert box == ["TiTiltei"], "hint must carry the slug as stored in the DB"
        assert hits and all(h["video_id"] == "hint-vid" for h in hits), (
            "the implicit channel filter must scope the hits"
        )
        box2: list[str] = []
        archive_db.search("notachannel zebra", _channel_hint_out=box2)
        assert box2 == [], "a non-channel first token must NOT fire the hint"
        # single-token queries never fire (nothing would be left to search)
        box3: list[str] = []
        archive_db.search("titiltei", _channel_hint_out=box3)
        assert box3 == []
    finally:
        archive_db.execute("DELETE FROM messages WHERE video_id='hint-vid'")
        archive_db.execute("DELETE FROM videos WHERE video_id='hint-vid'")


def test_channel_hint_explicit_param_wins():
    _upsert("youtube", "hint-other", channel="other", title="beta")
    archive_db.insert_messages(
        "youtube", "hint-other",
        [{"offset_sec": 1.0, "username": "u", "text": "zebra stripe"}],
    )
    try:
        box: list[str] = []
        hits = archive_db.search("titiltei zebra", channel="other", _channel_hint_out=box)
        assert box == [], "an explicit channel param must suppress the hint"
        assert hits and all(h["video_id"] == "hint-other" for h in hits)
    finally:
        archive_db.execute("DELETE FROM messages WHERE video_id='hint-other'")
        archive_db.execute("DELETE FROM videos WHERE video_id='hint-other'")


async def test_endpoint_hint_false_disables_auto_scope():
    """hint=0 (UI dismissed the chip) must disable the implicit channel
    scope entirely: no channel_hint field and hits from other channels."""
    _upsert("youtube", "hint-vid", channel="TiTiltei", title="beta")
    _upsert("youtube", "other-vid", channel="other", title="beta")
    archive_db.insert_messages(
        "youtube", "hint-vid",
        [{"offset_sec": 1.0, "username": "u", "text": "zebra stripe"}],
    )
    archive_db.insert_messages(
        "youtube", "other-vid",
        [{"offset_sec": 1.0, "username": "u", "text": "zebra stripe"}],
    )
    try:
        with patch("deps.settings_mgr") as fake_mgr:
            fake_mgr.get.return_value = SimpleNamespace(archive_smart_enrich=False)
            resp = await archive_search(q="titiltei zebra", source="chat", limit=10)
        assert resp["channel_hint"] == "TiTiltei"
        assert {h["video_id"] for h in resp["hits"]} == {"hint-vid"}
        with patch("deps.settings_mgr") as fake_mgr:
            fake_mgr.get.return_value = SimpleNamespace(archive_smart_enrich=False)
            resp2 = await archive_search(
                q="titiltei zebra", source="chat", limit=10, hint=False)
        assert "channel_hint" not in resp2, "hint=0 must suppress the hint field"
        assert {h["video_id"] for h in resp2["hits"]} == {"hint-vid", "other-vid"}, (
            "hint=0 must drop the implicit scope, not just the field"
        )
    finally:
        archive_db.execute(
            "DELETE FROM messages WHERE video_id IN ('hint-vid','other-vid')")
        archive_db.execute("DELETE FROM videos WHERE video_id IN ('hint-vid','other-vid')")


# --- (c) transcribe enqueue gated on worker_live ----------------------------


def _seed_ready_youtube(video_id: str) -> Path:
    media = _TMP / f"{video_id}.mp4"
    media.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    _upsert("youtube", video_id, channel="chan", title="ready vod",
            status="ready", duration_sec=60.0, archive_path=str(media))
    return media


async def test_transcribe_enqueue_skipped_when_worker_not_live(monkeypatch):
    import routers.archive as ar

    vid = "transcribe-1"
    media = _seed_ready_youtube(vid)
    archive_db.execute("DELETE FROM archive_jobs WHERE platform='youtube' AND video_id=?", (vid,))
    _reset_enrichment_state()
    monkeypatch.setattr(archive_db, "worker_live", lambda age_s=30: False)
    try:
        enriching = _maybe_enrich(platform="youtube", channel=None, source="transcript",
                                  q="ready")
        assert not any(e["kind"] == "transcribe" for e in enriching), (
            "no transcribe enqueue when the worker is not live"
        )
        assert archive_db.latest_job("youtube", vid, kind="transcribe") is None
        # live worker → deterministic job id + honest enriching entry
        monkeypatch.setattr(archive_db, "worker_live", lambda age_s=30: True)
        _reset_enrichment_state()
        enriching = _maybe_enrich(platform="youtube", channel=None, source="transcript",
                                  q="ready")
        assert any(e["kind"] == "transcribe" and e["video_id"] == vid for e in enriching)
        job = archive_db.latest_job("youtube", vid, kind="transcribe")
        assert job is not None and job["id"] == f"transcribe-youtube-{vid}"
        assert job["status"] == "queued"
    finally:
        _reset_enrichment_state()
        archive_db.execute("DELETE FROM archive_jobs WHERE platform='youtube' AND video_id=?", (vid,))
        archive_db.execute("DELETE FROM videos WHERE video_id=?", (vid,))
        media.unlink(missing_ok=True)


async def test_transcribe_skips_covered_and_recently_failed(monkeypatch):
    import routers.archive as ar

    vid = "transcribe-2"
    media = _seed_ready_youtube(vid)
    archive_db.execute("DELETE FROM archive_jobs WHERE platform='youtube' AND video_id=?", (vid,))
    _reset_enrichment_state()
    monkeypatch.setattr(archive_db, "worker_live", lambda age_s=30: True)
    try:
        # transcripts exist → captions_cover + no-transcript gate both skip
        archive_db.insert_transcript(
            "youtube", vid,
            [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "already captioned"}],
        )
        enriching = _maybe_enrich(platform="youtube", channel=None, source="transcript", q="x")
        assert not any(e["kind"] == "transcribe" for e in enriching)
        archive_db.execute("DELETE FROM transcripts WHERE video_id=?", (vid,))
        # latest job failed < 1h ago → skip re-enqueue forever
        archive_db.enqueue_job(f"transcribe-youtube-{vid}", "transcribe", "youtube", vid)
        archive_db.update_job(f"transcribe-youtube-{vid}", status="failed", error="boom")
        enriching = _maybe_enrich(platform="youtube", channel=None, source="transcript", q="x")
        assert not any(e["kind"] == "transcribe" for e in enriching), (
            "a job failed within the hour must not be re-enqueued"
        )
    finally:
        _reset_enrichment_state()
        archive_db.execute("DELETE FROM archive_jobs WHERE platform='youtube' AND video_id=?", (vid,))
        archive_db.execute("DELETE FROM transcripts WHERE video_id=?", (vid,))
        archive_db.execute("DELETE FROM videos WHERE video_id=?", (vid,))
        media.unlink(missing_ok=True)


# --- endpoint gate: archive_smart_enrich ------------------------------------


async def test_endpoint_archive_smart_enrich_gate(monkeypatch):
    import routers.archive as ar

    vid = "transcribe-3"
    media = _seed_ready_youtube(vid)
    archive_db.execute("DELETE FROM archive_jobs WHERE platform='youtube' AND video_id=?", (vid,))
    _reset_enrichment_state()
    monkeypatch.setattr(archive_db, "worker_live", lambda age_s=30: True)
    try:
        # toggle off → hits-only response, no job, chat half disabled too
        with patch("deps.settings_mgr") as fake_mgr:
            fake_mgr.get.return_value = SimpleNamespace(archive_smart_enrich=False)
            resp = await archive_search(q="probe", source="both", limit=10)
        assert resp["enriching"] == []
        assert "channel_hint" not in resp
        assert archive_db.latest_job("youtube", vid, kind="transcribe") is None
        # toggle on (default) → enrichment runs
        with patch("deps.settings_mgr") as fake_mgr:
            fake_mgr.get.return_value = SimpleNamespace(archive_smart_enrich=True)
            resp = await archive_search(q="probe", source="both", limit=10)
        assert any(e["kind"] == "transcribe" and e["video_id"] == vid for e in resp["enriching"])
        job = archive_db.latest_job("youtube", vid, kind="transcribe")
        assert job and job["id"] == f"transcribe-youtube-{vid}"
    finally:
        _reset_enrichment_state()
        archive_db.execute("DELETE FROM archive_jobs WHERE platform='youtube' AND video_id=?", (vid,))
        archive_db.execute("DELETE FROM videos WHERE video_id=?", (vid,))
        media.unlink(missing_ok=True)
