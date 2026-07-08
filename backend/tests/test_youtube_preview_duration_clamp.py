"""YouTube preview must not clamp full VODs to ~20s from fast extract under-report."""
from pathlib import Path
from unittest.mock import patch

from services.preview_service import (
    PreviewSession,
    _boost_youtube_duration_if_underreported,
    _clamp_session_crop_to_vod_duration,
    _resolve_youtube_preview_audio,
)


def test_boost_duration_on_placeholder_crop_end():
    session = PreviewSession(
        session_id="t",
        vod_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        master_url="",
        entry_url="",
        platform="YouTube",
        cache_dir=Path("/tmp"),
        crop_start=0,
        crop_end=7200,
    )
    info = {"duration": 19}
    with patch(
        "services.youtube_innertube.innertube_video_row_metadata",
        return_value={"duration": 212},
    ):
        boosted = _boost_youtube_duration_if_underreported(session, info, 7200.0, 19.0)
    assert boosted == 212.0
    assert info["duration"] == 212


def test_clamp_keeps_client_trim_when_above_extract():
    session = PreviewSession(
        session_id="u",
        vod_url="https://www.youtube.com/watch?v=abc123",
        master_url="",
        entry_url="",
        platform="YouTube",
        cache_dir=Path("/tmp"),
        crop_start=0,
        crop_end=50,
    )
    _clamp_session_crop_to_vod_duration(session, {"duration": 19})
    assert session.crop_end == 50
    assert session.vod_duration == 50


def test_resolve_audio_from_formats_list():
    info = {
        "formats": [
            {"url": "https://x/v.mp4", "protocol": "https", "height": 720, "vcodec": "avc1", "acodec": "none"},
            {"url": "https://x/a.m4a", "protocol": "https", "vcodec": "none", "acodec": "mp4a", "abr": 128},
        ],
    }
    url = _resolve_youtube_preview_audio(info)
    assert url == "https://x/a.m4a"
    assert info["_preview_audio_format"]["url"] == "https://x/a.m4a"
