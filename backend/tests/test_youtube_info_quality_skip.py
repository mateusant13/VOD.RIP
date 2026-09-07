"""/api/info/video quality enrichment: skip the InnerTube merge when the cached
extract already exposes a >=720p tier, keep paying it when the ladder is capped.

The merge exists because the fast extract can return only a muxed 360p stream;
when the cached ladder already reaches 720p+ it contributes no quality label
and no size entry (measured: identical on three videos) yet costs ~1.1 s of
InnerTube on every click.
"""
from __future__ import annotations

import asyncio

import pytest

from services import ytdlp_download as yd


def _fmt(height: int, *, itag: str, vcodec: str = "avc1", acodec: str = "none") -> dict:
    return {
        "format_id": itag,
        "ext": "mp4",
        "height": height,
        "vcodec": vcodec,
        "acodec": acodec,
        "tbr": 1000 + height,
        "url": f"https://cdn.example/{itag}.mp4",
        "protocol": "https",
    }


@pytest.fixture
def info_env(monkeypatch):
    """Drive get_video_info with a stubbed cached extract; count InnerTube merges."""
    box: dict = {"formats": [], "enrich_calls": 0, "enrich_result": None}

    def _fake_cached(url, opts):
        return {"id": "dQw4w9WgXcQ", "title": "T", "duration": 213, "formats": list(box["formats"])}

    def _fake_enrich(url, timeout=None, session=None, *, allow_session_refresh=True, preview_fast=False):
        box["enrich_calls"] += 1
        return box["enrich_result"]

    monkeypatch.setattr("services.ytdlp_hls.cached_extract_info", _fake_cached)
    monkeypatch.setattr("services.youtube_innertube.innertube_extract_info", _fake_enrich)
    # WS-4 title path must not reach the network either.
    monkeypatch.setattr("services.youtube_innertube.innertube_original_meta", lambda v: None)
    return box


def _run():
    return asyncio.run(
        yd.get_video_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    )


def test_full_ladder_skips_innertube_merge(info_env):
    info_env["formats"] = [
        _fmt(144, itag="adaptive-160"),
        _fmt(360, itag="adaptive-133"),
        _fmt(720, itag="adaptive-135"),
        _fmt(1080, itag="adaptive-137"),
        _fmt(360, itag="progressive-18", acodec="mp4a"),
    ]
    info_env["enrich_result"] = {"formats": [_fmt(2160, itag="adaptive-401")]}

    info = _run()

    assert info_env["enrich_calls"] == 0, "a >=720p cached ladder must not pay the merge"
    assert "1080p" in info.qualities and "720p" in info.qualities


def test_capped_ladder_still_enriches(info_env):
    """The case the merge was written for: fast extract returned 360p only."""
    info_env["formats"] = [
        _fmt(360, itag="progressive-18", acodec="mp4a"),
        _fmt(144, itag="adaptive-160"),
    ]
    info_env["enrich_result"] = {
        "formats": [_fmt(720, itag="adaptive-135"), _fmt(1080, itag="adaptive-137")]
    }

    info = _run()

    assert info_env["enrich_calls"] == 1
    assert "1080p" in info.qualities and "720p" in info.qualities


def test_audio_only_formats_do_not_count_as_a_video_tier(info_env):
    """A 2160p audio-only entry must not suppress the merge — quality labels
    come from video codecs, so the ladder is still effectively empty."""
    info_env["formats"] = [
        _fmt(360, itag="progressive-18", acodec="mp4a"),
        _fmt(0, itag="audio-141", vcodec="none", acodec="mp4a"),
    ]
    info_env["enrich_result"] = {"formats": [_fmt(1080, itag="adaptive-137")]}

    info = _run()

    assert info_env["enrich_calls"] == 1
    assert "1080p" in info.qualities
