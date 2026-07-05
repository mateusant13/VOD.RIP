"""YouTube auth — browser cookies only, no WPC."""
from unittest.mock import MagicMock, patch

from services.youtube_auth import (
    auth_status,
    pot_minting_enabled,
    resolve_youtube_browser,
    strengthen_youtube_session,
)


def test_resolve_youtube_browser_manual():
    assert resolve_youtube_browser("chrome", False) == "chrome"


def test_pot_minting_disabled():
    assert pot_minting_enabled() is False
    assert auth_status(True)["pot_auto_available"] is False


def test_strengthen_reads_browser_cookies():
    from services.youtube_session import YouTubeSession

    session = YouTubeSession(cookies_from_browser="edge")
    with patch(
        "services.youtube_auth.browser_cookie_session",
        return_value=("YSC=x", MagicMock()),
    ):
        strong = strengthen_youtube_session(session, "vid", auto_auth=True)
    assert strong.cookie_header == "YSC=x"
