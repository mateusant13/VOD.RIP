#!/usr/bin/env python3
"""CPU-01 exhaust fix: global warm-work cap + yt-dlp extract executor routing.

Behavioral, mock-only tests:
  - The full-mux warm path spawns one RAW daemon thread per URL (no executor
    cap), so it is the perfect probe for the shared WARM_WORK_SEMAPHORE: 12
    URLs must never run more than 8 concurrent extracts.
  - get_video_info (yt-dlp extract) must run on INFO_EXECUTOR, not the
    unbounded asyncio default pool.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

os.environ.setdefault("VODRIP_NO_DAEMONS", "1")
from deps import INFO_EXECUTOR, WARM_WORK_SEMAPHORE  # noqa: E402
from services.preview import warm as _w  # noqa: E402


def _vid_url(vid: str) -> str:
    assert len(vid) == 11, f"test video ids must be 11 chars, got {vid!r}"
    return f"https://www.youtube.com/watch?v={vid}"


@pytest.fixture(autouse=True)
def _clean_warm_state():
    """Reset the process-global warm state so the raw-thread test is isolated."""
    _w._warm_bot_gate_pause_until = 0.0
    _w._warm_soft_neg_streak = 0
    with _w._WARM_DEAD_VIDS_LOCK:
        _w._WARM_DEAD_VIDS.clear()
    with _w._YOUTUBE_WARM_LOCK:
        _w._YOUTUBE_WARM_INFLIGHT.clear()
    with _w._ACTIVE_YOUTUBE_PREVIEW_LOCK:
        _w._ACTIVE_YOUTUBE_PREVIEW_KEY = None
    yield


def test_warm_semaphore_is_shared_bounded_8():
    """The cap exists, is shared (importable from deps), and holds 8 slots."""
    assert isinstance(WARM_WORK_SEMAPHORE, threading.BoundedSemaphore)
    assert WARM_WORK_SEMAPHORE._value == 8
    got = []
    for _ in range(8):
        got.append(WARM_WORK_SEMAPHORE.acquire(blocking=False))
    assert all(got), "8 acquires must succeed"
    assert not WARM_WORK_SEMAPHORE.acquire(blocking=False), "9th must block"
    for _ in range(8):
        WARM_WORK_SEMAPHORE.release()


def test_full_mux_warm_raw_threads_capped_at_8(monkeypatch):
    """12 full-mux warm URLs spawn 12 raw threads; the shared semaphore must
    hold concurrent extract work at exactly 8 (the executor would cap at 3,
    so 8 proves the semaphore, not a pool)."""
    lock = threading.Lock()
    active = 0
    max_active = 0
    finished = 0
    N = 12

    def fake_extract(url, cookies_file=None):
        nonlocal active, max_active, finished
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.35)
        finally:
            with lock:
                active -= 1
                finished += 1
        return False  # stops before resolve_stream_info/MuxJob

    monkeypatch.setattr("services.ytdlp_hls.warm_youtube_extract", fake_extract)

    for i in range(N):
        _w.kickoff_youtube_full_mux_warm(_vid_url(f"cap{i:08d}"))

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        with lock:
            if finished >= N:
                break
        time.sleep(0.05)
    with lock:
        assert finished == N, f"only {finished}/{N} warm threads finished"
        assert max_active == 8, f"expected 8 concurrent warm extracts, saw {max_active}"


def test_get_video_info_runs_extract_on_info_executor(monkeypatch, tmp_path):
    """The yt-dlp extract inside get_video_info must land on INFO_EXECUTOR
    (bounded, shared) — never the unbounded asyncio default pool."""
    import asyncio

    from services import ytdlp_download as _yd

    class _RecordingExecutor:
        def __init__(self, inner):
            self.inner = inner
            self.submits = 0

        def submit(self, fn, *a, **k):
            self.submits += 1
            return self.inner.submit(fn, *a, **k)

    recorder = _RecordingExecutor(INFO_EXECUTOR)
    monkeypatch.setattr("deps.INFO_EXECUTOR", recorder)
    monkeypatch.setattr(_yd, "_get_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(_yd, "guarded_youtube_dl", lambda opts: _FakeYdl())
    monkeypatch.setattr("services.ytdlp_ffmpeg._ytdlp_engine_opts", lambda: {})

    info = asyncio.run(
        _yd.get_video_info("https://www.twitch.tv/videos/2833943352")
    )
    assert recorder.submits == 1, "extract must run through INFO_EXECUTOR"
    assert info.id == "2833943352"
    assert info.platform == "Twitch"


class _FakeYdl:
    """Context-manager ydl stand-in returning a minimal Twitch info dict."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return {
            "id": "2833943352",
            "title": "fake-title",
            "duration": 60.0,
            "duration_string": "1:00",
            "uploader": "fake-channel",
            "channel_id": "fake-channel",
            "thumbnail": None,
            "webpage_url": url,
            "extractor": "twitch",
            "is_live": False,
            "view_count": 0,
            "formats": [],
        }


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
