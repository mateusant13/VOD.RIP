"""Stale-while-revalidate on the extract cache (metadata callers only).

The 6h TTL is a googlevideo signature lifetime, not a metadata staleness bound.
Past it, a metadata-only caller (/api/info/video) used to pay the full InnerTube
race again for a title it could have served from memory. It now gets the cached
resolve immediately while ONE shared background re-extract refreshes it — and
callers that consume stream URLs never take that branch at all.
"""

from __future__ import annotations

import threading
import time

import pytest

from services import ytdlp_hls as h


WATCH = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

GOOD = {
    "id": "dQw4w9WgXcQ",
    "title": "Cached Title",
    "duration": 212,
    "formats": [
        {
            "format_id": "18",
            "url": "https://example.invalid/720.mp4",
            "protocol": "https",
            "height": 720,
            "ext": "mp4",
            "vcodec": "avc1.4d401f",
            "acodec": "mp4a.40.2",
        },
        {
            "format_id": "137",
            "url": "https://example.invalid/1080.mp4",
            "protocol": "https",
            "height": 1080,
            "ext": "mp4",
            "vcodec": "avc1.4d401f",
            "acodec": "none",
        },
    ],
}


@pytest.fixture(autouse=True)
def _clean_cache():
    h._EXTRACT_INFO_CACHE.clear()
    h._EXTRACT_INFLIGHT.clear()
    h._EXTRACT_FATAL_CACHE.clear()
    h._EXTRACT_NEG_CACHE.clear()
    h._EXTRACT_SWR_INFLIGHT.clear()
    yield
    h._EXTRACT_INFO_CACHE.clear()
    h._EXTRACT_INFLIGHT.clear()
    h._EXTRACT_FATAL_CACHE.clear()
    h._EXTRACT_NEG_CACHE.clear()
    h._EXTRACT_SWR_INFLIGHT.clear()


def _store(age_sec: float, info: dict | None = None) -> str:
    """Write a cache entry directly, back-dated by `age_sec`."""
    key = h._extract_cache_key(WATCH, {})
    h._EXTRACT_INFO_CACHE[key] = (time.time() - age_sec, dict(info or GOOD))
    return key


class _FakeExecutor:
    """Records submits and runs them on plain threads (no pool sizing in tests)."""

    def __init__(self, block: threading.Event | None = None):
        self.calls: list[tuple] = []
        self.block = block
        self.threads: list[threading.Thread] = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))

        def run():
            if self.block is not None:
                self.block.wait(timeout=10.0)
            fn(*args)

        t = threading.Thread(target=run, daemon=True)
        self.threads.append(t)
        t.start()
        return t

    def join(self):
        for t in self.threads:
            t.join(timeout=10.0)


def _fresh() -> dict:
    return {**GOOD, "title": "Fresh Title"}


def _opts(**extra):
    opts = {"_youtube_session": None}
    opts.update(extra)
    return opts


def test_fresh_hit_never_schedules_a_refresh(monkeypatch):
    _store(60.0)
    ex = _FakeExecutor()
    monkeypatch.setattr("deps.INFO_EXECUTOR", ex)
    called = []
    monkeypatch.setattr(
        h, "_youtube_extract_with_retries", lambda *_a: called.append(1) or GOOD
    )

    out = h.cached_extract_info(WATCH, _opts(_stale_metadata_ok=True))
    assert out is not None and out["title"] == "Cached Title"
    assert called == [] and ex.calls == []


def test_stale_hit_serves_metadata_and_refreshes_once(monkeypatch):
    """Past TTL, inside the window: return now, re-extract behind the response."""
    _store(h._EXTRACT_CACHE_TTL_SEC + 600)
    block = threading.Event()
    ex = _FakeExecutor(block=block)
    monkeypatch.setattr("deps.INFO_EXECUTOR", ex)
    refreshed = _fresh()
    monkeypatch.setattr(
        h, "_youtube_extract_with_retries", lambda *_a: dict(refreshed)
    )

    started = time.monotonic()
    out = h.cached_extract_info(WATCH, _opts(_stale_metadata_ok=True))
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, "stale serve must not wait on the re-extract"
    assert out["title"] == "Cached Title"
    assert len(ex.calls) == 1

    # The refresh runs WITHOUT the stale opt-in, so it cannot serve itself.
    block.set()
    ex.join()
    assert len(ex.calls) == 1, "exactly one shared refresh per stale window"
    key = h._extract_cache_key(WATCH, {})
    assert h._EXTRACT_INFO_CACHE[key][1]["title"] == "Fresh Title"
    assert key not in h._EXTRACT_SWR_INFLIGHT, "claim must be released"


def test_concurrent_stale_callers_share_one_refresh(monkeypatch):
    _store(h._EXTRACT_CACHE_TTL_SEC + 600)
    block = threading.Event()
    ex = _FakeExecutor(block=block)
    monkeypatch.setattr("deps.INFO_EXECUTOR", ex)
    monkeypatch.setattr(h, "_youtube_extract_with_retries", lambda *_a: dict(GOOD))

    opts = _opts(_stale_metadata_ok=True)
    first = h.cached_extract_info(WATCH, opts)
    second = h.cached_extract_info(WATCH, opts)
    third = h.cached_extract_info(WATCH, opts)

    assert first["title"] == second["title"] == third["title"] == "Cached Title"
    assert len(ex.calls) == 1, f"{len(ex.calls)} refreshes for one stale entry"
    block.set()
    ex.join()


def test_stream_consumer_never_gets_a_stale_entry(monkeypatch):
    """No `_stale_metadata_ok` → the caller is a preview/download path that will
    hand the googlevideo URLs to a player: it must re-extract, not be served."""
    _store(h._EXTRACT_CACHE_TTL_SEC + 600)
    ex = _FakeExecutor()
    monkeypatch.setattr("deps.INFO_EXECUTOR", ex)
    fresh = _fresh()
    monkeypatch.setattr(
        h, "_youtube_extract_with_retries", lambda *_a: dict(fresh)
    )

    out = h.cached_extract_info(WATCH, _opts())
    assert out["title"] == "Fresh Title"
    assert ex.calls == []  # ran inline as the single-flight leader, not via SWR


def test_past_the_window_is_a_hard_miss(monkeypatch):
    """Beyond TTL+24h the metadata is genuinely old — no stale serve."""
    _store(h._EXTRACT_CACHE_TTL_SEC + h._EXTRACT_SWR_WINDOW_SEC + 60)
    ex = _FakeExecutor()
    monkeypatch.setattr("deps.INFO_EXECUTOR", ex)
    monkeypatch.setattr(
        h,
        "_youtube_extract_with_retries",
        lambda *_a: _fresh(),
    )

    out = h.cached_extract_info(WATCH, _opts(_stale_metadata_ok=True))
    assert out["title"] == "Fresh Title"
    assert ex.calls == []


def test_degenerate_bot_wall_entry_is_not_stale_served(monkeypatch):
    """_cache_extract_result back-dates ≤1-format entries to a 60s TTL on
    purpose; SWR must not turn a bot-wall page into a 24h answer."""
    wall = {"id": "dQw4w9WgXcQ", "title": "Wall", "formats": []}
    _store(h._EXTRACT_CACHE_TTL_SEC + 600, wall)
    ex = _FakeExecutor()
    monkeypatch.setattr("deps.INFO_EXECUTOR", ex)
    monkeypatch.setattr(
        h,
        "_youtube_extract_with_retries",
        lambda *_a: _fresh(),
    )

    out = h.cached_extract_info(WATCH, _opts(_stale_metadata_ok=True))
    assert out["title"] == "Fresh Title"


def test_fatal_stamp_beats_a_stale_serve(monkeypatch):
    """A video that hard-failed 3 minutes ago is gone, not "stale metadata"."""
    key = _store(h._EXTRACT_CACHE_TTL_SEC + 600)
    h._EXTRACT_FATAL_CACHE[key] = (time.time(), "This video is unavailable")
    ex = _FakeExecutor()
    monkeypatch.setattr("deps.INFO_EXECUTOR", ex)

    with pytest.raises(RuntimeError, match="unavailable"):
        h.cached_extract_info(WATCH, _opts(_stale_metadata_ok=True))
    assert ex.calls == []


def test_failed_refresh_keeps_the_stale_entry_and_releases_the_claim(monkeypatch):
    _store(h._EXTRACT_CACHE_TTL_SEC + 600)
    ex = _FakeExecutor()
    monkeypatch.setattr("deps.INFO_EXECUTOR", ex)

    def boom(*_a):
        raise RuntimeError("bot gate")

    monkeypatch.setattr(h, "_youtube_extract_with_retries", boom)

    out = h.cached_extract_info(WATCH, _opts(_stale_metadata_ok=True))
    assert out["title"] == "Cached Title"
    ex.join()
    key = h._extract_cache_key(WATCH, {})
    assert key in h._EXTRACT_INFO_CACHE, "a lost refresh must not drop the entry"
    assert key not in h._EXTRACT_SWR_INFLIGHT, "claim must be released for retry"


def test_submit_rejection_releases_the_claim(monkeypatch):
    _store(h._EXTRACT_CACHE_TTL_SEC + 600)

    class Rejecting:
        def submit(self, *_a):
            raise RuntimeError("pool shut down")

    monkeypatch.setattr("deps.INFO_EXECUTOR", Rejecting())
    monkeypatch.setattr(h, "_youtube_extract_with_retries", lambda *_a: dict(GOOD))

    out = h.cached_extract_info(WATCH, _opts(_stale_metadata_ok=True))
    assert out["title"] == "Cached Title"
    key = h._extract_cache_key(WATCH, {})
    assert key not in h._EXTRACT_SWR_INFLIGHT
    # Next caller re-claims rather than being stuck behind a dead claim.
    out2 = h.cached_extract_info(WATCH, _opts(_stale_metadata_ok=True))
    assert out2["title"] == "Cached Title"


def test_swr_window_is_longer_than_the_ttl():
    assert h._EXTRACT_SWR_WINDOW_SEC > h._EXTRACT_CACHE_TTL_SEC
