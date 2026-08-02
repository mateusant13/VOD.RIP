"""Cookie bridge M3 wiring — bridge cookies into yt-dlp / Kick / Twitch consumers.

Isolated cookie DB via VODRIP_COOKIE_DB (the store's own override knob).
The settings flag is read with getattr(default True), so tests drive it
with a SimpleNamespace stub — works before and after M2 adds the real
``cookie_bridge_enabled`` field to AppSettings.
"""

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from services import cookie_store


@pytest.fixture
def bridge_db(monkeypatch, tmp_path):
    """Point the cookie store at a throwaway SQLite file (never the real DB)."""
    import services.cookie_bridge as cb

    db = tmp_path / "bridge-cookies.db"
    monkeypatch.setenv("VODRIP_COOKIE_DB", str(db))
    cookie_store._conn = None
    cookie_store._schema_ready = False
    cb._export_state.clear()
    yield cookie_store
    cookie_store.clear()
    cookie_store._conn = None
    cookie_store._schema_ready = False
    cb._export_state.clear()


@pytest.fixture
def bridge_settings(monkeypatch):
    """deps.settings_mgr stub — flag absent by default (= enabled, pre-M2)."""
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace()
        yield mgr


def _seed(cs, platform, name, value, *, domain=None, http_only=False):
    dom = domain or {
        "youtube": ".youtube.com",
        "kick": ".kick.com",
        "twitch": ".twitch.tv",
    }[platform]
    accepted, dropped = cs.upsert_cookies([{
        "name": name,
        "domain": dom,
        "path": "/",
        "secure": True,
        "httpOnly": http_only,
        "value": value,
        "expirationDate": 1900000000,
    }])
    assert accepted == 1 and dropped == 0, "seeded cookie must be accepted"


def _bridge_text(bridge_settings, path):
    assert path and Path(path).is_file()
    return Path(path).read_text(encoding="utf-8")


# --- gate logic -------------------------------------------------------------

def test_flag_defaults_and_disable():
    from services.cookie_bridge import bridge_enabled

    assert bridge_enabled(SimpleNamespace()) is True, "missing flag = enabled"
    assert bridge_enabled(SimpleNamespace(cookie_bridge_enabled=True)) is True
    assert bridge_enabled(SimpleNamespace(cookie_bridge_enabled=False)) is False


# --- cookiefile export ------------------------------------------------------

def test_resolve_cookiefile_writes_and_caches(bridge_db, bridge_settings, tmp_path):
    from services.cookie_bridge import resolve_cookiefile

    _seed(bridge_db, "youtube", "SID", "youtube-sid-value", http_only=True)
    p = resolve_cookiefile("youtube")
    text = _bridge_text(bridge_settings, p)
    assert text.lstrip().startswith("# Netscape HTTP Cookie File")
    assert "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t" in text, "httpOnly prefix + flags"
    assert "\tSID\tyoutube-sid-value" in text, "Netscape line must carry the SID"

    # hot path: cached export — no rewrite within the TTL window
    mtime1 = Path(p).stat().st_mtime
    time.sleep(0.02)
    assert resolve_cookiefile("youtube") == p
    assert Path(p).stat().st_mtime == mtime1, "same count + fresh file must not rewrite"

    # count change → immediate refresh
    _seed(bridge_db, "youtube", "APISID", "apisid-value")
    assert "APISID\tapisid-value" in _bridge_text(bridge_settings, resolve_cookiefile("youtube"))


def test_resolve_cookiefile_empty_store_returns_none(bridge_db, bridge_settings):
    from services.cookie_bridge import cookie_dict, cookie_header, resolve_cookiefile

    assert resolve_cookiefile("youtube") is None
    assert cookie_dict("kick") is None
    assert cookie_header("twitch") is None


def test_disable_flag_skips_bridge(bridge_db, bridge_settings):
    from services.cookie_bridge import cookie_dict, resolve_cookiefile

    bridge_settings.get.return_value = SimpleNamespace(cookie_bridge_enabled=False)
    _seed(bridge_db, "youtube", "SID", "v")
    _seed(bridge_db, "kick", "auth_token", "t")
    assert resolve_cookiefile("youtube") is None, "disabled flag must skip the export"
    assert cookie_dict("kick") is None, "disabled flag must skip live cookies"


# --- YouTube / yt-dlp -------------------------------------------------------

def test_resolve_ytdlp_cookiefile_chain(bridge_db, bridge_settings, tmp_path):
    from services.youtube_session import YouTubeSession, resolve_ytdlp_cookiefile

    _seed(bridge_db, "youtube", "SID", "bridge-sid")
    manual = tmp_path / "manual-cookies.txt"
    manual.write_text("manual", encoding="utf-8")
    anon = tmp_path / "yt_anon_temp.txt"
    anon.write_text("anon", encoding="utf-8")

    # manual settings file > bridge (both explicit and session.cookie_file)
    assert resolve_ytdlp_cookiefile(YouTubeSession(), explicit=str(manual)) == str(manual)
    assert resolve_ytdlp_cookiefile(YouTubeSession(cookie_file=str(manual))) == str(manual)

    # bridge > anonymous temp jar
    p = resolve_ytdlp_cookiefile(YouTubeSession(cookie_file=str(anon)))
    assert "bridge-sid" in _bridge_text(bridge_settings, p)

    # browser mode is a manual override — never bridged
    assert resolve_ytdlp_cookiefile(
        YouTubeSession(cookies_from_browser="edge", cookie_file=str(manual))
    ) is None

    # empty store → anonymous jar survives unchanged
    bridge_db.clear()
    assert resolve_ytdlp_cookiefile(YouTubeSession(cookie_file=str(anon))) == str(anon)
    assert resolve_ytdlp_cookiefile(YouTubeSession()) is None


def test_apply_ytdlp_cookie_opts_uses_bridge(bridge_db, bridge_settings):
    from services.youtube_session import YouTubeSession, apply_ytdlp_cookie_opts

    _seed(bridge_db, "youtube", "SID", "bridge-sid")
    opts: dict = {}
    apply_ytdlp_cookie_opts(opts, YouTubeSession(), auto_auth=False)
    assert "bridge-sid" in _bridge_text(bridge_settings, opts.get("cookiefile"))
    assert "cookiesfrombrowser" not in opts

    bridge_settings.get.return_value = SimpleNamespace(cookie_bridge_enabled=False)
    opts2: dict = {}
    apply_ytdlp_cookie_opts(opts2, YouTubeSession(), auto_auth=False)
    assert "cookiefile" not in opts2, "disabled flag must leave opts untouched"


def test_find_fresh_cookie_cache_falls_back_to_bridge(bridge_db, bridge_settings):
    from services.youtube_auth import find_fresh_cookie_cache

    # appdata (conftest tmp) holds no youtube_cookies_*.txt → bridge export
    _seed(bridge_db, "youtube", "SID", "bridge-sid")
    p = find_fresh_cookie_cache()
    assert p is not None and Path(p).name == "youtube.txt"
    assert "bridge-sid" in Path(p).read_text(encoding="utf-8")

    bridge_db.clear()
    assert find_fresh_cookie_cache() is None


# --- Kick -------------------------------------------------------------------

def test_kick_requests_carry_bridge_cookies(bridge_db, bridge_settings):
    from services import kick_api_service

    _seed(bridge_db, "kick", "auth_token", "kick-token-val", http_only=True)
    _seed(bridge_db, "kick", "g_session", "g-session-val")
    captured: dict = {}

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeResp()

    with patch("curl_cffi.requests.get", side_effect=fake_get):
        body = kick_api_service._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips")
    assert body == {"ok": True}
    assert captured["url"] == "https://kick.com/api/v2/channels/xyz"
    assert captured["kwargs"]["cookies"] == {
        "auth_token": "kick-token-val",
        "g_session": "g-session-val",
    }
    assert captured["kwargs"]["headers"]["origin"] == "https://kick.com", "headers merge, not clobber"

    # disabled flag → no cookies sent at all
    bridge_settings.get.return_value = SimpleNamespace(cookie_bridge_enabled=False)
    with patch("curl_cffi.requests.get", side_effect=fake_get):
        kick_api_service._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips")
    assert captured["kwargs"]["cookies"] is None


# --- Twitch -----------------------------------------------------------------

def test_twitch_gql_requests_carry_bridge_cookies(bridge_db, bridge_settings):
    from services import twitch_gql_service as tgs

    _seed(bridge_db, "twitch", "auth-token", "twitch-token-val")
    _seed(bridge_db, "twitch", "sp", "sp-device-val")
    seen: dict = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"data": {}}'

    def fake_urlopen(req, timeout=20):
        seen["headers"] = dict(req.headers)
        return FakeResp()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        tgs._gql_request(tgs.VIDEO_INFO_QUERY, {"id": "12345"})
    assert seen["headers"]["Cookie"] == "auth-token=twitch-token-val; sp=sp-device-val"
    assert seen["headers"]["Client-id"] == tgs.TWITCH_GQL_CLIENT_ID  # urllib title-cases header names

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        tgs._gql_persisted("PlaybackAccessToken", tgs.VOD_PLAYBACK_TOKEN_HASH, {"id": "1"})
    assert seen["headers"]["Cookie"] == "auth-token=twitch-token-val; sp=sp-device-val"

    # disabled flag → no Cookie header
    bridge_settings.get.return_value = SimpleNamespace(cookie_bridge_enabled=False)
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        tgs._gql_request(tgs.VIDEO_INFO_QUERY, {"id": "12345"})
    assert "Cookie" not in seen["headers"]


# --- PO token (bgutil has no cookie support — documented) -------------------

def test_pot_fetch_sends_no_cookies(bridge_db, bridge_settings):
    """bgutil 1.3.1 /get_pot accepts only content_binding — anonymous minting."""
    from services import youtube_pot_service as pot

    seen: dict = {}

    def fake_post_json(url, payload, *, timeout):
        seen["payload"] = payload
        return {"poToken": "tok-123"}

    with patch.object(pot, "_http_post_json", side_effect=fake_post_json):
        assert pot.fetch_video_po_token("dQw4w9WgXcQ") == "tok-123"
    assert seen["payload"] == {"content_binding": "dQw4w9WgXcQ"}
    assert "cookies" not in seen["payload"], "no cookie field until bgutil supports it"
