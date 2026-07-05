"""YouTube cookie + format policy — no Chrome spam on auto-auth."""
from unittest.mock import MagicMock, patch

from services.youtube_session import youtube_session_from_values


def test_auto_auth_does_not_load_browser_cookies():
    with patch("services.youtube_auth.load_best_browser_session") as load:
        youtube_session_from_values(auto_auth=True)
    load.assert_not_called()


def test_manual_browser_loads_once():
    with patch(
        "services.youtube_auth.load_best_browser_session",
        return_value=("chrome", "YSC=x", MagicMock()),
    ) as load:
        s = youtube_session_from_values(cookies_from_browser="chrome", auto_auth=True)
    load.assert_called_once_with("chrome", False)
    assert s.cookies_from_browser == "chrome"


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
