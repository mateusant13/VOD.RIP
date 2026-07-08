"""YouTube audio-only trim must not require muxed progressive video."""
from __future__ import annotations

from unittest.mock import patch


def test_resolve_youtube_audio_format_prefers_tagged():
    from services.ytdlp_hls import _resolve_youtube_audio_format

    info = {
        "_preview_audio_format": {"url": "https://x/tagged.m4a", "acodec": "mp4a", "vcodec": "none"},
        "formats": [
            {"url": "https://x/other.m4a", "protocol": "https", "acodec": "mp4a", "vcodec": "none", "abr": 999},
        ],
    }
    assert _resolve_youtube_audio_format(info)["url"] == "https://x/tagged.m4a"


def test_download_hls_clip_audio_only_dash_uses_audio_url():
    from services.ytdlp_hls import _download_hls_clip

    info = {
        "http_headers": {},
        "formats": [
            {
                "url": "https://x/video.mp4",
                "protocol": "https",
                "height": 720,
                "vcodec": "avc1",
                "acodec": "none",
            },
        ],
        "_preview_audio_format": {
            "url": "https://x/audio.m4a",
            "protocol": "https",
            "acodec": "mp4a",
            "vcodec": "none",
        },
    }
    with patch("services.ytdlp_hls.cached_extract_info", return_value=info), patch(
        "services.ytdlp_hls.youtube_preview_ytdl_opts",
        return_value={"cachedir": "/tmp", "_youtube_session": object()},
    ), patch(
        "services.ytdlp_hls._download_progressive_clip",
    ) as prog, patch("services.ytdlp_hls._find_media_format") as find_fmt:
        _download_hls_clip(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "/tmp/out.mp3",
            0.0,
            108.0,
            {"cachedir": "/tmp"},
            audio_only=True,
        )
        prog.assert_called_once()
        assert prog.call_args[0][0] == "https://x/audio.m4a"
        assert prog.call_args[1]["audio_only"] is True
        find_fmt.assert_not_called()
