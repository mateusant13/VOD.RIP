"""YouTube cookie + format policy — manual auth only on hot path."""
from unittest.mock import MagicMock, patch

from services.youtube_session import apply_ytdlp_cookie_opts, youtube_session_from_values
from services.youtube_session import YouTubeSession


def test_auto_auth_tries_browser_before_anonymous():
    with patch(
        "services.youtube_auth.load_best_browser_session",
        return_value=(None, None, None),
    ) as load:
        s = youtube_session_from_values(auto_auth=True)
    load.assert_called_once_with(None, True)
    assert s.anonymous is True


def test_manual_browser_loads_once():
    with patch(
        "services.youtube_auth.load_best_browser_session",
        return_value=("chrome", "YSC=x", MagicMock()),
    ) as load:
        s = youtube_session_from_values(cookies_from_browser="chrome", auto_auth=True)
    load.assert_called_once_with("chrome", False)
    assert s.cookies_from_browser == "chrome"


def test_apply_ytdlp_cookie_opts_skips_auto_browser():
    opts: dict = {}
    session = YouTubeSession()
    apply_ytdlp_cookie_opts(opts, session, auto_auth=True)
    assert "cookiesfrombrowser" not in opts


def test_apply_ytdlp_cookie_opts_explicit_browser():
    opts: dict = {}
    session = YouTubeSession(cookies_from_browser="edge")
    apply_ytdlp_cookie_opts(opts, session, auto_auth=True)
    assert opts["cookiesfrombrowser"] == ("edge",)


def test_find_media_format_progressive_fallback():
    from services.ytdlp_hls import _find_media_format

    info = {
        "formats": [
            {
                "url": "https://cdn.example/v.mp4",
                "protocol": "https",
                "height": 720,
                "vcodec": "avc1",
                "acodec": "mp4a",
            },
        ],
    }
    fmt = _find_media_format(info)
    assert fmt["height"] == 720
