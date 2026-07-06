"""Anonymous parallel YouTube extract policy."""
import time
from unittest.mock import patch

from services.ytdlp_hls import _youtube_extract_parallel, _youtube_manual_auth_configured


def test_manual_auth_false_by_default():
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value.youtube_cookies_file = ""
        mgr.get.return_value.youtube_cookies_browser = ""
        mgr.get.return_value.youtube_po_token = ""
        mgr.get.return_value.youtube_tokens_file = ""
        assert _youtube_manual_auth_configured() is False


def test_parallel_picks_first_playable():
    good = {"formats": [{"url": "https://x/v.mp4", "protocol": "https", "height": 720}]}

    def fast_inn(*_a, **_k):
        return good

    def slow_ydl(*_a, **_k):
        import time
        time.sleep(2)
        return good

    with patch("services.ytdlp_hls._try_innertube_info_retry", side_effect=fast_inn):
        with patch("services.ytdlp_hls._extract_hls_info_quiet", side_effect=slow_ydl):
            t0 = time.monotonic()
            info = _youtube_extract_parallel("https://www.youtube.com/watch?v=abc123def45", {}, None, "abc123def45")
            elapsed = time.monotonic() - t0
    assert info is good
    assert elapsed < 1.0, f"parallel should not wait for slow yt-dlp, took {elapsed:.2f}s"
