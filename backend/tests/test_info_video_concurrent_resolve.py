"""/api/info/video resolve scheduling.

SOTA-02 contract: on a capped ladder the extract and the InnerTube
enrichment overlap (barrier-proved); on a >=720p cached ladder the
enrichment is never submitted at all (UX skip guard).
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from services import ytdlp_download as yd
from services import youtube_innertube as it


WATCH = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _formats(prefix: str, heights: list[int]) -> list[dict]:
    return [
        {
            "format_id": f"{prefix}{h}",
            "url": f"https://example.invalid/{prefix}{h}.mp4",
            "protocol": "https",
            "height": h,
            "ext": "mp4",
            "vcodec": "avc1.4d401f",
            "acodec": "mp4a.40.2",
            "tbr": 1000 + h,
        }
        for h in heights
    ]


class _Side:
    """One fake resolve: blocks on a shared barrier, then returns."""

    def __init__(self, barrier: threading.Barrier, info: dict) -> None:
        self.barrier = barrier
        self.info = info
        self.calls = 0

    def __call__(self, *_args, **_kwargs) -> dict:
        self.calls += 1
        # A serial implementation deadlocks here instead of passing: only one
        # side is ever running, so the barrier can never reach its party count.
        self.barrier.wait(timeout=10.0)
        return self.info


@pytest.fixture
def _scratch(monkeypatch, tmp_path):
    monkeypatch.setattr(yd, "_get_cache_dir", lambda: tmp_path)
    # WS-4's original-title preference would otherwise hit the network.
    monkeypatch.setattr(it, "innertube_original_meta", lambda *a, **k: None)
    return tmp_path


def test_capped_ladder_enriches_in_one_pass(_scratch, monkeypatch):
    """Capped ladder (360 only): the InnerTube enrichment runs after the
    extract, in the same request, and both ladders land in the merge.
    Order is asserted; zero InnerTube calls is the full-ladder test below."""
    calls: list[str] = []

    def extract(*_a, **_k):
        calls.append("extract")
        return {
            "id": "dQw4w9WgXcQ",
            "title": "First",
            "duration": 212,
            "uploader": "u",
            "formats": _formats("e", [360]),
        }

    def enrich(*_a, **_k):
        calls.append("enrich")
        return {"id": "dQw4w9WgXcQ", "duration": 212, "formats": _formats("r", [720, 1080])}

    monkeypatch.setattr("services.ytdlp_hls.cached_extract_info", extract)
    monkeypatch.setattr(it, "innertube_extract_info", enrich)

    info = asyncio.run(yd.get_video_info(WATCH))

    assert calls == ["extract", "enrich"]
    # Both ladders survive the merge (360p from the extract, 720/1080 from rich).
    assert info.qualities == ["1080p", "720p", "360p"]

def test_full_ladder_never_submits_the_enrichment(_scratch, monkeypatch):
    """UX skip guard: a cached >=720p ladder pays zero InnerTube — the
    enrichment task is not even submitted, so a slow InnerTube cannot add
    latency to warm clicks."""
    started = threading.Event()

    def slow_enrich(*_a, **_k):
        started.set()
        time.sleep(30)
        return {}

    monkeypatch.setattr(
        "services.ytdlp_hls.cached_extract_info",
        lambda *_a, **_k: {
            "id": "dQw4w9WgXcQ",
            "title": "First",
            "duration": 212,
            "formats": _formats("e", [720, 1080]),
        },
    )
    monkeypatch.setattr(it, "innertube_extract_info", slow_enrich)

    started_at = time.monotonic()
    info = asyncio.run(yd.get_video_info(WATCH))

    assert not started.is_set(), "enrichment must not be submitted for full ladders"
    assert info.qualities == ["1080p", "720p"]


def test_enrichment_failure_does_not_lose_the_request(_scratch, monkeypatch):
    """Quality tiers are best-effort; the extract result must still land.
    Capped ladder (360 only) so the enrichment IS submitted and its raise
    is swallowed by _enrich's own guard."""
    monkeypatch.setattr(
        "services.ytdlp_hls.cached_extract_info",
        lambda *_a, **_k: {
            "id": "dQw4w9WgXcQ",
            "title": "First",
            "duration": 212,
            "formats": _formats("e", [360]),
        },
    )

    def boom(*_a, **_k):
        raise RuntimeError("innertube exhausted")

    monkeypatch.setattr(it, "innertube_extract_info", boom)

    info = asyncio.run(yd.get_video_info(WATCH))
    assert info.id == "dQw4w9WgXcQ"
    assert info.qualities == ["360p"]


def test_extract_failure_still_raises(_scratch, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("Sign in to confirm you're not a bot")

    monkeypatch.setattr("services.ytdlp_hls.cached_extract_info", boom)
    monkeypatch.setattr(it, "innertube_extract_info", lambda *_a, **_k: {})
    with pytest.raises(RuntimeError, match="not a bot"):
        asyncio.run(yd.get_video_info(WATCH))


def _short_video_case(monkeypatch, *, info: dict, rich: dict | None):
    """Run get_video_info with a fixed row-probe spy; return (info, probe_calls)."""
    calls: list[float] = []
    monkeypatch.setattr(
        "services.ytdlp_hls.cached_extract_info", lambda *_a, **_k: dict(info)
    )
    monkeypatch.setattr(
        it, "innertube_extract_info", lambda *_a, **_k: (dict(rich) if rich else None)
    )

    def probe(*_a, **_k):
        calls.append(1)
        return {"duration": 44}

    monkeypatch.setattr(it, "innertube_video_row_metadata", probe)
    result = asyncio.run(yd.get_video_info(WATCH))
    return result, calls


def test_merged_player_response_skips_the_row_reprobe(_scratch, monkeypatch):
    """<90s used to cost a third serial resolve. The merged response already
    carries microformat dates, so the row probe is pure waste when present."""
    result, calls = _short_video_case(
        monkeypatch,
        info={
            "id": "dQw4w9WgXcQ",
            "title": "Short",
            "duration": 42,
            "formats": _formats("e", [360]),
        },
        rich={
            "duration": 44,
            "upload_date": "20260708",
            "formats": _formats("r", [720]),
        },
    )
    assert calls == [], "row metadata re-probe must not run when microformat is known"
    # The rich duration still wins over the under-reported fast extract.
    assert result.duration == 44


def test_row_reprobe_still_runs_when_nothing_knows_the_date(_scratch, monkeypatch):
    result, calls = _short_video_case(
        monkeypatch,
        info={
            "id": "dQw4w9WgXcQ",
            "title": "Short",
            "duration": 42,
            "formats": _formats("e", [360]),
        },
        rich={"duration": 0, "formats": []},
    )
    assert len(calls) == 1
    assert result.duration == 44


def test_long_videos_never_reprobe_the_row(_scratch, monkeypatch):
    _, calls = _short_video_case(
        monkeypatch,
        info={
            "id": "dQw4w9WgXcQ",
            "title": "Long",
            "duration": 3600,
            "formats": _formats("e", [360]),
        },
        rich=None,
    )
    assert calls == []


def test_non_youtube_submits_exactly_one_executor_job(monkeypatch, tmp_path):
    """The concurrency fan-out is YouTube-only — a Twitch click must not pay an
    extra (useless) executor submit."""
    from deps import INFO_EXECUTOR

    class Recorder:
        def __init__(self, inner):
            self.inner = inner
            self.submits = 0

        def submit(self, fn, *a, **k):
            self.submits += 1
            return self.inner.submit(fn, *a, **k)

    rec = Recorder(INFO_EXECUTOR)
    monkeypatch.setattr("deps.INFO_EXECUTOR", rec)
    monkeypatch.setattr(yd, "_get_cache_dir", lambda: tmp_path)

    class FakeYdl:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            return {
                "id": "2833943352",
                "title": "T",
                "duration": 100,
                "platform": "twitch",
                "formats": _formats("t", [720]),
            }

    monkeypatch.setattr(yd, "guarded_youtube_dl", lambda opts: FakeYdl())
    monkeypatch.setattr("services.ytdlp_ffmpeg._ytdlp_engine_opts", lambda: {})

    info = asyncio.run(yd.get_video_info("https://www.twitch.tv/videos/2833943352"))
    assert rec.submits == 1
    assert info.platform == "Twitch"
