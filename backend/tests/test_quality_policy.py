"""Quality policy — anonymous YouTube previews stay 360p-only (backend half).

Covers: the anonymity determination helper, the create_session clamp,
the set_session_prefer_height / refresh clamps, live-session anonymity
marking, and the response surfacing. All resolve/mux paths are stubbed —
no network.
"""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.preview.session import (
    _manager,
    _youtube_preview_is_anonymous,
    create_live_session,
    create_session,
    get_session,
    refresh_youtube_preview_session,
    set_session_prefer_height,
)
from services.preview_service import PreviewSession

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _anon_settings():
    return SimpleNamespace(
        youtube_cookies_file="",
        youtube_cookies_browser="",
        youtube_po_token="",
        youtube_tokens_file="",
    )


def _fake_session(platform="YouTube", anonymous=False, sid="poltest"):
    session = PreviewSession(
        session_id=sid,
        vod_url=YOUTUBE_URL,
        master_url="https://cdn.example.com/master.m3u8",
        entry_url="https://cdn.example.com/1080.m3u8",
        platform=platform,
        cache_dir=Path(tempfile.mkdtemp(prefix="vodrip-pol-")),
    )
    session.anonymous = anonymous
    session.variant_entries = [(1080, "https://cdn.example.com/1080.m3u8")]
    session.kind = "hls"
    with _manager._lock:
        _manager._sessions[sid] = session
    return session


def _canned_resolve():
    variants = [
        {"url": "https://cdn.example.com/360.mp4", "height": 360, "acodec": "mp4a"},
        {"url": "https://cdn.example.com/720.mp4", "height": 720, "acodec": "mp4a"},
        {"url": "https://cdn.example.com/1080.mp4", "height": 1080, "acodec": "mp4a"},
    ]
    return (
        "https://cdn.example.com/360.mp4",
        {"Cookie": "VISITOR=pol-test"},
        "YouTube",
        variants,
        "progressive",
        {"duration": 300.0, "http_headers": {"Cookie": "VISITOR=pol-test"}},
    )


def _create_policy_session(prefer_height: int, settings: SimpleNamespace):
    with patch("deps.settings_mgr") as mgr, patch(
        "services.youtube_auth.find_fresh_cookie_cache", return_value=None
    ), patch(
        "services.preview.session._get_session_snapshot", return_value=None
    ), patch(
        "services.preview.session._resolve_and_cache_youtube_snapshot", return_value=None
    ), patch(
        "services.preview.session.resolve_stream_info", return_value=_canned_resolve()
    ), patch(
        "services.preview.session._youtube_entry_needs_mux", return_value=False
    ):
        mgr.get.return_value = settings
        return create_session(YOUTUBE_URL, 0, 0, prefer_height=prefer_height)


# ── anonymity determination ──

def test_anonymous_default_when_no_user_auth():
    with patch("deps.settings_mgr") as mgr, patch(
        "services.youtube_auth.find_fresh_cookie_cache", return_value=None
    ):
        mgr.get.return_value = _anon_settings()
        assert _youtube_preview_is_anonymous(None) is True


def test_oauth_token_means_user_auth():
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = _anon_settings()
        assert _youtube_preview_is_anonymous("oauth-token") is False


def test_manual_cookie_file_means_user_auth(tmp_path):
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n")
    with patch("deps.settings_mgr") as mgr, patch(
        "services.youtube_auth.find_fresh_cookie_cache", return_value=None
    ):
        mgr.get.return_value = SimpleNamespace(
            youtube_cookies_file=str(cookie),
            youtube_cookies_browser="",
            youtube_po_token="",
            youtube_tokens_file="",
        )
        assert _youtube_preview_is_anonymous(None) is False


def test_browser_cookies_setting_means_user_auth():
    with patch("deps.settings_mgr") as mgr, patch(
        "services.youtube_auth.find_fresh_cookie_cache", return_value=None
    ):
        mgr.get.return_value = SimpleNamespace(
            youtube_cookies_file="",
            youtube_cookies_browser="chrome",
            youtube_po_token="",
            youtube_tokens_file="",
        )
        assert _youtube_preview_is_anonymous(None) is False


def test_cached_browser_export_means_user_auth():
    with patch("deps.settings_mgr") as mgr, patch(
        "services.youtube_auth.find_fresh_cookie_cache",
        return_value="/appdata/VOD.RIP/youtube_cookies_chrome.txt",
    ):
        mgr.get.return_value = _anon_settings()
        assert _youtube_preview_is_anonymous(None) is False


def test_anon_jar_stays_anonymous():
    with patch("deps.settings_mgr") as mgr, patch(
        "services.youtube_auth.find_fresh_cookie_cache",
        return_value="/tmp/yt_anon_abc123",
    ):
        mgr.get.return_value = _anon_settings()
        assert _youtube_preview_is_anonymous(None) is True


# ── create_session clamp ──

def test_create_session_clamps_anonymous_youtube_to_360():
    session = _create_policy_session(720, _anon_settings())
    try:
        assert session.anonymous is True
        assert session.prefer_height == 360
        # The served entry is the 360p variant, not the requested 720p.
        assert "360.mp4" in session.entry_url
        assert session.variant_entries == [(360, "https://cdn.example.com/360.mp4"),
                                           (720, "https://cdn.example.com/720.mp4"),
                                           (1080, "https://cdn.example.com/1080.mp4")]
    finally:
        _manager.delete_session(session.session_id)


def test_create_session_allows_1080_with_cookies(tmp_path):
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n")
    session = _create_policy_session(
        1080,
        SimpleNamespace(
            youtube_cookies_file=str(cookie),
            youtube_cookies_browser="",
            youtube_po_token="",
            youtube_tokens_file="",
        ),
    )
    try:
        assert session.anonymous is False
        assert session.prefer_height == 1080
        # canned resolve returns 360.mp4 as raw_entry regardless of prefer_height
        assert "360.mp4" in session.entry_url
    finally:
        _manager.delete_session(session.session_id)


def test_create_session_defaults_anonymous_to_360():
    """Default create_session (no prefer_height) stays 360p for anonymous."""
    session = _create_policy_session(0, _anon_settings())
    try:
        assert session.prefer_height == 360
    finally:
        _manager.delete_session(session.session_id)


def test_create_session_non_youtube_unaffected():
    with patch("services.kick_api_service.resolve_kick_stream_api") as kick, patch(
        "services.preview.session._get_session_snapshot", return_value=None
    ), patch(
        "services.preview.session._resolve_and_cache_youtube_snapshot", return_value=None
    ), patch(
        "services.preview.session._resolve_preview_entry",
        return_value="https://cdn.kick.com/live.m3u8",
    ):
        kick.return_value = SimpleNamespace(
            m3u8_url="https://cdn.kick.com/live.m3u8",
            url="https://kick.com/channel",
        )
        session = create_session("https://kick.com/channel/videos/123", 0, 0, prefer_height=720)
        try:
            assert session.anonymous is False
            assert session.prefer_height == 720
        finally:
            _manager.delete_session(session.session_id)


# ── set_session_prefer_height / refresh clamps ──

def test_set_prefer_height_clamps_anonymous_youtube():
    _fake_session(platform="YouTube", anonymous=True)
    with patch("services.preview.session._refresh_youtube_preview_urls") as refresh:
        session = set_session_prefer_height("poltest", 1080)
        assert session.prefer_height == 360
        refresh.assert_called_once()
        assert refresh.call_args.kwargs["prefer_height"] == 360
    _manager.delete_session("poltest")


def test_set_prefer_height_allows_1080_with_cookies():
    _fake_session(platform="YouTube", anonymous=False)
    with patch("services.preview.session._refresh_youtube_preview_urls") as refresh:
        session = set_session_prefer_height("poltest", 1080)
        assert session.prefer_height == 1080
        refresh.assert_called_once()
        assert refresh.call_args.kwargs["prefer_height"] == 1080
    _manager.delete_session("poltest")


def test_set_prefer_height_non_youtube_unaffected():
    _fake_session(platform="Twitch", anonymous=True)
    session = set_session_prefer_height("poltest", 1080)
    assert session.prefer_height == 1080
    assert session.entry_url == "https://cdn.example.com/1080.m3u8"
    _manager.delete_session("poltest")


def test_refresh_clamps_anonymous_youtube():
    _fake_session(platform="YouTube", anonymous=True)
    with patch("services.preview.session._refresh_youtube_preview_urls") as refresh:
        refresh_youtube_preview_session("poltest", prefer_height=1080)
        refresh.assert_called_once()
        assert refresh.call_args.kwargs["prefer_height"] == 360
    _manager.delete_session("poltest")


# ── live sessions carry the flag ──

def test_create_live_session_marks_youtube_anonymous():
    with patch("services.preview.hls.proxy_playlist"), patch(
        "services.preview.session._resolve_preview_entry",
        return_value="https://cdn.example.com/media.m3u8",
    ), patch("deps.settings_mgr") as mgr, patch(
        "services.youtube_auth.find_fresh_cookie_cache", return_value=None
    ):
        mgr.get.return_value = _anon_settings()
        session = create_live_session("https://manifest.example.com/live.m3u8", {}, "youtube")
        try:
            assert session.is_live is True
            assert session.anonymous is True
        finally:
            _manager.delete_session(session.session_id)


def test_create_live_session_twitch_not_anonymous():
    with patch("services.preview.hls.proxy_playlist"), patch(
        "services.preview.session._resolve_preview_entry",
        return_value="https://cdn.example.com/media.m3u8",
    ):
        session = create_live_session("https://manifest.example.com/live.m3u8", {}, "twitch")
        try:
            assert session.anonymous is False
        finally:
            _manager.delete_session(session.session_id)


# ── response carries the flag ──

def test_preview_session_response_carries_anonymous():
    from routers.preview import _preview_session_response

    session = _fake_session(platform="YouTube", anonymous=True, sid="polresp")
    try:
        resp = _preview_session_response(session)
        assert resp.anonymous is True
        assert resp.is_live is False
    finally:
        _manager.delete_session("polresp")


def test_preview_session_response_defaults_false():
    from routers.preview import _preview_session_response

    session = _fake_session(platform="Twitch", anonymous=False, sid="polresp2")
    try:
        resp = _preview_session_response(session)
        assert resp.anonymous is False
    finally:
        _manager.delete_session("polresp2")


# ── self-check ──

def test_policy_self_check():
    from models.schemas import PreviewSessionResponse

    assert "anonymous" in PreviewSessionResponse.model_fields
    leaked = get_session("poltest")
    assert leaked is None or leaked.closed  # deleted sessions never linger active
