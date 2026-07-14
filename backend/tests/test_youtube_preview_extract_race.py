"""Preview extract races fallbacks instead of long sequential chains."""

from unittest.mock import patch

from services.ytdlp_hls import (
    _PREVIEW_EXTRACT_MAX_WALL_SEC,
    _PREVIEW_EXTRACT_RACE_SEC,
    _youtube_extract_preview_race,
    _youtube_info_playable,
)


def test_preview_race_returns_first_playable():
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    opts = {"_youtube_session": None, "socket_timeout": 3}
    playable = {
        "formats": [{"url": "https://x/v.mp4", "protocol": "https", "height": 720}],
    }

    def slow_inn():
        import time
        time.sleep(0.3)
        return playable

    def fast_bare():
        return playable

    with patch("services.ytdlp_hls._try_innertube_info", side_effect=slow_inn):
        with patch("services.ytdlp_hls._extract_hls_info_quiet") as ydl:
            ydl.side_effect = [None, playable]
            t0 = __import__("time").monotonic()
            info = _youtube_extract_preview_race(url, opts)
            elapsed = __import__("time").monotonic() - t0

    assert info is not None
    assert _youtube_info_playable(info)
    assert elapsed < _PREVIEW_EXTRACT_RACE_SEC
    assert ydl.call_count >= 1


def test_preview_race_budget_constants():
    assert _PREVIEW_EXTRACT_RACE_SEC <= _PREVIEW_EXTRACT_MAX_WALL_SEC
