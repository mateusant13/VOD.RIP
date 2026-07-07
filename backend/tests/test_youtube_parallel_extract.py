"""Anonymous YouTube extract fallback order."""
from unittest.mock import MagicMock, patch

from services.ytdlp_hls import (
    _youtube_extract_pass,
    _youtube_has_user_auth,
    _youtube_manual_auth_configured,
)


def test_manual_auth_false_by_default():
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value.youtube_cookies_file = ""
        mgr.get.return_value.youtube_cookies_browser = ""
        mgr.get.return_value.youtube_po_token = ""
        mgr.get.return_value.youtube_tokens_file = ""
        assert _youtube_manual_auth_configured() is False


def test_anonymous_cookie_jar_is_not_user_auth():
    session = MagicMock(anonymous=True)
    opts = {"cookiefile": "/tmp/anon.txt", "_youtube_session": session}
    with patch("services.ytdlp_hls._youtube_cookie_path", return_value=True):
        with patch("services.ytdlp_hls._youtube_manual_auth_configured", return_value=False):
            assert _youtube_has_user_auth(opts) is False


def test_anonymous_parallel_races_innertube_and_ytdlp():
    good = {"formats": [{"url": "https://x/v.mp4", "protocol": "https", "height": 720}]}
    calls: list[str] = []

    def ydl(url, opts):
        calls.append("ydl")
        return None

    def inn(*_a, **_k):
        calls.append("inn")
        return good

    session = MagicMock(anonymous=True)
    opts = {"_youtube_session": session}
    with patch("services.ytdlp_hls._youtube_has_user_auth", return_value=False):
        with patch("services.ytdlp_hls._extract_hls_info_quiet", side_effect=ydl):
            with patch("services.ytdlp_hls._try_innertube_info_retry", side_effect=inn):
                info = _youtube_extract_pass("https://www.youtube.com/watch?v=abc123def45", opts)
    assert info is good
    assert "inn" in calls
