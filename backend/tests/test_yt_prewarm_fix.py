#!/usr/bin/env python3
"""YouTube preview pre-warm: selection, per-video dead-skip, and cache-hit
consumption.

Root cause this guards (observed in prod logs 2026-08-13): one unplayable
VOD in a saved channel's recent list ("all fallbacks exhausted" ->
"YouTube preview unavailable for this video") was classified as a bot-gate
soft-negative. Every attempt re-armed the global 2h warm pause (14 pauses /
12h), killing warm for EVERY video — snapshots expired after 1h and every
preview open went cold ("Starting YouTube preview…" spinner).

Fix contract:
- "preview unavailable for this video" = per-video outcome -> warm dead-skip
  that vid (6h), NEVER the global pause, NEVER the gate streak.
- "sign in to confirm you're not a bot" / "not a bot" = IP-level gate ->
  global pause (unchanged).
- Warm selection mirrors the frontend listing (recent-first per channel,
  shorts excluded, per-channel cap) and skips dead vids + fresh snapshots.
- create_session consumes the warmed session snapshot without extracting.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import unittest.mock as um

from services import preview as _pv  # noqa: E402
from services.preview import warm as _w  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────

def _vid_url(vid: str) -> str:
    assert len(vid) == 11, f"test video ids must be 11 chars, got {vid!r}"
    return f"https://www.youtube.com/watch?v={vid}"


def _v(url: str, ck: str = "video", dt: str = "2026-01-01T00:00:00+00:00") -> dict:
    return {"url": url, "content_kind": ck, "created_at": dt, "platform": "YouTube"}


def _channels() -> list[dict]:
    return [
        {
            "id": "c1",
            "vodVideos": [
                _v(_vid_url("newestvid11"), "video", "2026-08-01T00:00:00+00:00"),
                _v(_vid_url("secondvid22"), "video", "2026-07-01T00:00:00+00:00"),
                _v(_vid_url("thirdvid333"), "stream", "2026-06-01T00:00:00+00:00"),
                _v("https://www.youtube.com/shorts/shortvid000", "short", "2026-08-02T00:00:00+00:00"),
                _v("https://kick.com/x/videos/1", "video", "2026-08-03T00:00:00+00:00"),
            ],
        },
        {
            "id": "c2",
            "clipVideos": [
                _v("https://youtu.be/clipvid0001", "clip", "2026-08-05T00:00:00+00:00"),
                _v(_vid_url("freshvid444"), "video", "2026-08-04T00:00:00+00:00"),
            ],
        },
    ]


@pytest.fixture(autouse=True)
def _clean_warm_state():
    """Isolate the process-global warm state between tests."""
    with _w._WARM_DEAD_VIDS_LOCK:
        _w._WARM_DEAD_VIDS.clear()
    with _w._YOUTUBE_WARM_LOCK:
        _w._YOUTUBE_WARM_INFLIGHT.clear()
    _w._warm_bot_gate_pause_until = 0.0
    _w._warm_soft_neg_streak = 0
    _w._full_warm_queued.clear()
    from services.preview.warm import _SESSION_SNAPSHOT

    _SESSION_SNAPSHOT.clear()
    yield


def _mark_dead(vid: str) -> None:
    _w._mark_vid_warm_dead(vid)


# ── warm selection ─────────────────────────────────────────────────────

def test_select_recent_per_channel():
    """Recent-first, per-channel cap, shorts + non-YouTube excluded, deduped."""
    sel = _w.select_youtube_warm_urls(_channels(), per_channel=5)
    urls = [u for u, _ in sel]
    assert len(urls) == 5, urls  # 3 from c1 (newest 3 yt vods) + 2 from c2
    assert urls[0] == _vid_url("newestvid11")  # newest first
    assert all("shorts" not in u and "kick.com" not in u for u in urls)
    assert all("youtube.com" in u or "youtu.be" in u for u in urls)
    # per-channel cap
    sel2 = _w.select_youtube_warm_urls(_channels(), per_channel=2)
    assert len(sel2) == 4, sel2  # 2 per channel
    assert len([u for u, c in sel2 if c == "c1"]) == 2
    assert len([u for u, c in sel2 if c == "c2"]) == 2


def test_select_skips_dead_vids():
    """A warm-dead video is never selected (and does not count toward cap)."""
    _mark_dead("newestvid11")
    sel = _w.select_youtube_warm_urls(_channels(), per_channel=5)
    urls = [u for u, _ in sel]
    assert not any("newestvid11" in u for u in urls)
    assert any("secondvid22" in u for u in urls)  # slot backfilled by next recent


def test_select_skip_fresh_snapshots():
    """skip_fresh drops videos whose (vid, 360) snapshot is still warm."""
    from services.preview.warm import _put_session_snapshot

    _put_session_snapshot("freshvid444", 360, {"session_id": "x", "cache_dir": "/tmp/x"})
    sel = _w.select_youtube_warm_urls(_channels(), per_channel=5, skip_fresh=True)
    urls = [u for u, _ in sel]
    assert not any("freshvid444" in u for u in urls)
    assert any("clipvid0001" in u for u in urls)  # clip without snapshot still selected


# ── failure classification ─────────────────────────────────────────────

def test_full_chain_failure_marks_vid_dead_without_global_pause():
    """Per-video exhaustion must dead-skip the vid — never arm the 2h pause."""
    with um.patch(
        "services.preview.session.resolve_stream_info",
        side_effect=RuntimeError("YouTube preview unavailable for this video"),
    ):
        with pytest.raises(RuntimeError):
            _w.warm_youtube_preview_resolve(_vid_url("deadvid1234"), reraise=True)
    assert _w._warm_vid_dead("deadvid1234")
    assert _w._warm_bot_gate_pause_until == 0.0
    assert _w._warm_soft_neg_streak == 0


def test_dead_vid_skips_resolve_entirely():
    """Once dead, warm returns immediately without calling the extract chain."""
    _mark_dead("deadvid1234")
    with um.patch(
        "services.preview.session.resolve_stream_info",
        side_effect=AssertionError("resolve must not run for a dead vid"),
    ) as rsi:
        ok = _w.warm_youtube_preview_resolve(_vid_url("deadvid1234"))
    assert ok is False
    rsi.assert_not_called()


def test_gate_signal_still_arms_global_pause():
    """A genuine bot-gate signal keeps the global pause after the threshold."""
    for _ in range(_w._SOFT_NEG_PAUSE_THRESHOLD):
        with um.patch(
            "services.preview.session.resolve_stream_info",
            side_effect=RuntimeError("Sign in to confirm you are not a bot"),
        ):
            with pytest.raises(RuntimeError):
                _w.warm_youtube_preview_resolve(_vid_url("gatedvid999"), reraise=True)
    assert _w._warm_bot_gate_pause_until > 0.0
    # per-video classification untouched: gate streak does not dead-mark
    assert not _w._warm_vid_dead("gatedvid999")


def test_batch_warm_skips_dead_vid_at_enqueue():
    """kickoff_youtube_batch_warm never submits a warm-dead video."""
    _mark_dead("deadvid1234")
    captured: list = []

    def _capture(fn, *a, **k):
        captured.append((fn, a, k))

    with um.patch("deps.WARM_EXECUTOR.submit", side_effect=_capture):
        _w.kickoff_youtube_batch_warm(_vid_url("deadvid1234"), prefer_height=360)
    assert captured == [], "dead vid must not be enqueued"


def test_batch_warm_skips_run_during_gate_pause():
    """During the global gate pause the warm _run fast-skips (no resolve)."""
    _w._warm_bot_gate_pause_until = time.monotonic() + 3600
    captured: list = []

    def _capture(fn, *a, **k):
        captured.append((fn, a, k))

    with um.patch("deps.WARM_EXECUTOR.submit", side_effect=_capture):
        _w.kickoff_youtube_batch_warm(_vid_url("alivevid999"), prefer_height=360, channel_key="c1")
    assert len(captured) == 1, captured
    fn, args, kwargs = captured[0]
    with um.patch.object(
        _w, "warm_youtube_resolve_only",
        side_effect=AssertionError("resolve must not run during gate pause"),
    ):
        fn(*args, **kwargs)


def test_warm_recent_channels_submits_and_dedups():
    """warm_youtube_recent_channels submits the capped selection."""
    submitted: list[tuple] = []

    def _capture(fn, *a, **k):
        submitted.append((fn, a, k))

    with um.patch("deps.WARM_EXECUTOR.submit", side_effect=_capture):
        n = _w.warm_youtube_recent_channels(_channels(), per_channel=2)
    assert n == 4, submitted
    assert len(submitted) == 4


# ── cache-hit consumption ──────────────────────────────────────────────

def _snapshot(tmp_path: Path, sid: str = "snap0001") -> dict:
    return {
        "session_id": sid,
        "cache_dir": str(tmp_path / sid),
        "master_url": "https://x.example/master.m3u8",
        "entry_url": "https://x.example/v.mp4",
        "platform": "YouTube",
        "http_headers": {},
        "allowed_hosts": {"x.example"},
        "kind": "progressive",
        "preview_audio_url": None,
        "variant_muxed": {360: True},
        "variant_entries": [(360, "https://x.example/v.mp4")],
        "custom_master": None,
        "dash_window_hls": False,
        "preview_audio_fmt": None,
        "preview_video_fmt": None,
        "explore_yt_info": {"duration": 600},
        "vod_duration": 600.0,
        "cached_progressive_path": None,
        "mux_status": "pending",
    }


def test_create_session_reuses_warm_snapshot_without_extract(tmp_path):
    """The warm's session snapshot is consumed: create_session skips the
    extract/mux work entirely (the 'Starting…' wait a snapshot hit avoids)."""
    from services.preview.warm import _put_session_snapshot
    from services.preview_service import create_session

    _put_session_snapshot("warmvid1234", 360, _snapshot(tmp_path))
    with um.patch(
        "services.preview.session.resolve_stream_info",
        side_effect=AssertionError("snapshot hit must not call resolve_stream_info"),
    ) as rsi:
        session = create_session(_vid_url("warmvid1234"), 0, 0, prefer_height=360)
    assert session.session_id == "snap0001"
    assert session.kind == "progressive"
    rsi.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
