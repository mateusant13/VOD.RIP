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


# --- expiry ----------------------------------------------------------------

def test_expired_rows_purged_and_hidden_on_read(bridge_db):
    from services import cookie_store as cs
    from services.cookie_store import counts, list_cookies, purge_expired, status

    _seed(bridge_db, "youtube", "SID", "live-sid")
    accepted, dropped = bridge_db.upsert_cookies([{
        "name": "APISID",
        "domain": ".youtube.com",
        "path": "/",
        "secure": True,
        "value": "stale",
        "expirationDate": time.time() - 60,  # already expired
    }])
    assert accepted == 1 and dropped == 0

    # Throttle the lazy purge so this test asserts the SQL-level read filter
    # on its own: reads must never serve the expired row.
    cs._last_purge_mono = time.monotonic()
    assert counts()["youtube"] == 1
    assert [c["name"] for c in list_cookies("youtube")] == ["SID"]
    st = status()["youtube"]
    assert st["count"] == 1 and st["expiredCount"] == 1
    assert st["lastGrabAt"], "lastGrabAt must reflect the newest live row"

    # physical purge catches up and the aggregate reports zero expired
    cs._last_purge_mono = 0.0
    assert purge_expired() == 1
    st = status()["youtube"]
    assert st["count"] == 1 and st["expiredCount"] == 0


def test_status_shape_per_platform(bridge_db):
    from services.cookie_store import status

    _seed(bridge_db, "youtube", "SID", "s")
    _seed(bridge_db, "kick", "auth_token", "t")
    st = status()
    assert set(st) == {"youtube", "kick"}
    assert st["youtube"] == {"count": 1, "expiredCount": 0, "lastGrabAt": st["youtube"]["lastGrabAt"]}
    assert st["kick"]["count"] == 1
    assert "twitch" not in st, "platforms without rows must be absent"


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

def test_youtube_session_from_values_uses_bridge_cookies(bridge_db, bridge_settings):
    """InnerTube session: bridge cookies beat the anonymous cold visit; an
    empty store leaves the anonymous path untouched (the regression bar)."""
    from services import youtube_session as ys

    _seed(bridge_db, "youtube", "SID", "bridge-sid", http_only=True)

    def _boom(*a, **k):
        raise AssertionError("anonymous bootstrap must not run when bridge has cookies")

    with patch.object(ys, "bootstrap_anonymous_session", side_effect=_boom):
        sess = ys.youtube_session_from_values(visitor_data="vd-test", auto_auth=False)
    assert sess.cookie_header == "SID=bridge-sid"
    assert sess.cookie_file and Path(sess.cookie_file).name == "youtube.txt"
    assert sess.anonymous is False, "bridge cookies are a real session, not anonymous"

    # empty store → anonymous bootstrap unchanged
    bridge_db.clear()
    with patch.object(
        ys, "bootstrap_anonymous_session",
        return_value=("anon-vd", "YSC=anon-cookie", None, None),
    ):
        sess = ys.youtube_session_from_values(visitor_data="vd-test", auto_auth=False)
    assert sess.cookie_header == "YSC=anon-cookie"
    assert sess.anonymous is True


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

"""Cookie bridge router tests — kill switch, extension endpoints, pairing.

Real HTTP via ASGI transport (no mocks); temp settings + a synthetic crx/pem
so the real %APPDATA% state and cookie DB are never touched.
"""
import base64
import hashlib
import io
import json
import os
import struct
import zipfile
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

from app import app
from deps import settings_mgr
from models.schemas import AppSettings

ALPHABET = "abcdefghijklmnop"


def _pem_for(der: bytes) -> str:
    b64 = base64.b64encode(der).decode("ascii")
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n"


def _make_crx_and_pem(tmp: Path) -> tuple[Path, Path, str]:
    """Synthetic CRX3 (junk header + zip with manifest.json) + matching pem."""
    manifest = {"name": "bridge-test", "version": "9.9.9"}
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
    # header: Cr24 + version 3 + 4-byte header len; any header bytes work —
    # the router locates the zip via the PK\x03\x04 signature, like real crx2/3.
    header = b"Cr24" + struct.pack("<II", 3, 4) + b"junk"
    crx = tmp / "extension.crx"
    crx.write_bytes(header + zip_buf.getvalue())
    der = bytes.fromhex(
        "3059301306072a8648ce3d020106082a8648ce3d030107034200"
        + "00"
        + "11" * 32
    )
    pem = tmp / "extension.pem"
    pem.write_text(_pem_for(der), encoding="utf-8")
    digest = hashlib.sha256(der).digest()[:16]
    ext_id = "".join(ALPHABET[b >> 4] + ALPHABET[b & 0xF] for b in digest)
    return crx, pem, ext_id


_RSA_N = bytes.fromhex("00" + "ab" * 32)
_RSA_E = b"\x01\x00\x01"


def _rsa_spki_der() -> bytes:
    """SPKI DER for the tiny RSA key used by the PKCS#8 tests."""
    from routers.cookie_bridge import _der_len

    def tlv(tag: int, body: bytes) -> bytes:
        return bytes([tag]) + _der_len(len(body)) + body

    rsa_inner = tlv(0x02, _RSA_N) + tlv(0x02, _RSA_E)
    oid = bytes.fromhex("2a864886f70d010101")
    algo = tlv(0x30, tlv(0x06, oid) + tlv(0x05, b""))
    return tlv(0x30, algo + tlv(0x03, b"\x00" + tlv(0x30, rsa_inner)))


def _rsa_pkcs8_pem() -> str:
    """PKCS#8 "BEGIN PRIVATE KEY" pem (the format chrome --pack-extension
    actually writes) wrapping the same RSA key as _rsa_spki_der()."""
    from routers.cookie_bridge import _der_len

    def tlv(tag: int, body: bytes) -> bytes:
        return bytes([tag]) + _der_len(len(body)) + body

    oid = bytes.fromhex("2a864886f70d010101")
    algo = tlv(0x30, tlv(0x06, oid) + tlv(0x05, b""))
    pkcs1 = tlv(0x30, tlv(0x02, b"\x00") + tlv(0x02, _RSA_N) + tlv(0x02, _RSA_E))
    pkcs8 = tlv(0x30, tlv(0x02, b"\x00") + algo + tlv(0x04, pkcs1))
    b64 = base64.b64encode(pkcs8).decode("ascii")
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return "-----BEGIN PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END PRIVATE KEY-----\n"


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    """Temp settings file + temp cookie DB + temp crx/pem for every test."""
    from routers import cookie_bridge as cb

    original_file = settings_mgr._settings_file
    temp_file = original_file.parent / f"settings_test_{os.getpid()}.json"
    settings_mgr._settings_file = temp_file
    settings_mgr._settings = AppSettings()
    monkeypatch.setenv("VODRIP_COOKIE_DB", str(tmp_path / "cookies.db"))
    monkeypatch.setenv("VODRIP_EXT_CRX", str(tmp_path / "extension.crx"))
    # cookie_store caches its connection; point it at the fresh temp DB.
    import services.cookie_store as cookie_store_mod
    cookie_store_mod._conn = None
    cookie_store_mod._schema_ready = False
    _make_crx_and_pem(tmp_path)
    with cb._AUTO_INSTALL_LOCK:
        cb._AUTO_INSTALL_STATE.update(
            state="idle", installed=False, extension_id="", error=None,
            started_at=None, finished_at=None,
        )
    yield tmp_path
    settings_mgr._settings_file = original_file
    if temp_file.exists():
        temp_file.unlink(missing_ok=True)


@pytest.fixture
def ext_state(tmp_path):
    pem = (tmp_path / "extension.pem").read_text(encoding="utf-8")
    b64 = "".join(
        pem.split("-----BEGIN PUBLIC KEY-----", 1)[-1]
        .split("-----END PUBLIC KEY-----", 1)[0]
        .split()
    )
    der = base64.b64decode(b64)
    digest = hashlib.sha256(der).digest()[:16]
    return "".join(ALPHABET[b >> 4] + ALPHABET[b & 0xF] for b in digest)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_extension_id_from_pkcs8_pem(client, tmp_path):
    """Real chrome --pack-extension keys are PKCS#8 private keys — the id
    endpoint must derive the same id as from the SPKI public key."""
    expected = "".join(
        ALPHABET[b >> 4] + ALPHABET[b & 0xF]
        for b in hashlib.sha256(_rsa_spki_der()).digest()[:16]
    )
    (tmp_path / "extension.pem").write_text(_rsa_pkcs8_pem(), encoding="utf-8")
    resp = await client.get("/api/session/cookies/extension/id")
    assert resp.status_code == 200
    assert resp.json()["extension_id"] == expected


async def test_extension_crx_served(client):
    resp = await client.get("/api/session/cookies/extension/extension.crx")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-chrome-extension")
    assert resp.content[:4] == b"Cr24"


async def test_extension_id(client, ext_state):
    resp = await client.get("/api/session/cookies/extension/id")
    assert resp.status_code == 200
    assert resp.json() == {"extension_id": ext_state}


async def test_extension_endpoints_404_without_artifacts(client, tmp_path):
    (tmp_path / "extension.crx").unlink()
    (tmp_path / "extension.pem").unlink()
    for path in (
        "/api/session/cookies/extension/extension.crx",
        "/api/session/cookies/extension/id",
    ):
        resp = await client.get(path)
        assert resp.status_code == 404


async def test_kill_switch_blocks_ingest(client):
    # enabled by default → pairing POST accepted
    resp = await client.post("/api/session/cookies", json={
        "token": "tok-1", "cookies": [{
            "name": "auth_token", "value": "v", "domain": "kick.com",
            "path": "/", "secure": True, "httpOnly": True,
        }],
    })
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1

    resp = await client.post("/api/session/cookies/disable")
    assert resp.json() == {"enabled": False}

    status = await client.get("/api/session/cookies/status")
    assert status.json()["enabled"] is False

    # ingest now refused even with the correct token
    resp = await client.post("/api/session/cookies", json={
        "token": "tok-1", "cookies": [],
    })
    assert resp.status_code == 403

    resp = await client.post("/api/session/cookies/enable")
    assert resp.json() == {"enabled": True}
    resp = await client.post("/api/session/cookies", json={
        "token": "tok-1", "cookies": [],
    })
    assert resp.status_code == 200


async def test_settings_roundtrip_flag(client):
    resp = await client.post("/api/settings", json={"cookie_bridge_enabled": False})
    assert resp.status_code == 200
    assert resp.json()["cookie_bridge_enabled"] is False
    resp = await client.get("/api/settings")
    assert resp.json()["cookie_bridge_enabled"] is False


async def test_settings_roundtrip_auto_install_flag(client):
    """auto_install_extension round-trips through GET/POST /api/settings."""
    resp = await client.get("/api/settings")
    assert resp.json()["auto_install_extension"] is True, "default is ON"
    resp = await client.post("/api/settings", json={"auto_install_extension": False})
    assert resp.status_code == 200
    assert resp.json()["auto_install_extension"] is False
    resp = await client.get("/api/settings")
    assert resp.json()["auto_install_extension"] is False


async def test_auto_install_short_circuits_when_paired(client, monkeypatch):
    """Already-paired -> {alreadyInstalled:true} and NO automation thread."""
    from routers import cookie_bridge as cb

    resp = await client.post("/api/session/cookies", json={
        "token": "tok-1", "cookies": [],
    })
    assert resp.status_code == 200

    captured: list = []
    monkeypatch.setattr("threading.Thread", _capture_thread(captured))
    resp = await client.post("/api/session/cookies/auto-install")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["alreadyInstalled"] is True
    assert body["installed"] is True
    assert captured == [], "no thread may spawn when already paired"

    status = (await client.get("/api/session/cookies/status")).json()
    assert status["auto_install"]["installed"] is True


async def test_auto_install_spawns_background_install(client, monkeypatch, tmp_path):
    """Not paired -> returns started:true and spawns the worker thread."""
    from routers import cookie_bridge as cb

    src = tmp_path / "ext-src"
    src.mkdir()
    (src / "manifest.json").write_text('{"name": "x"}', encoding="utf-8")
    monkeypatch.setattr(cb, "_materialize_ext_src", lambda: src)
    monkeypatch.setattr(cb, "_find_browser", lambda name: Path("C:/chrome.exe") if name == "chrome" else None)

    captured: list = []
    monkeypatch.setattr("threading.Thread", _capture_thread(captured))
    resp = await client.post("/api/session/cookies/auto-install")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["started"] is True
    assert body["state"] == "running"
    assert len(captured) == 1, "exactly one worker thread must spawn"
    name, kwargs = captured[0]
    assert name == "cookie-auto-install"
    assert kwargs["target"] is cb._auto_install_worker
    assert kwargs["args"] == ("chrome", src)
    assert kwargs["daemon"] is True

    status = (await client.get("/api/session/cookies/status")).json()
    assert status["auto_install"]["state"] == "running"


async def test_auto_install_no_browser_error(client, monkeypatch, tmp_path):
    """No Chromium found -> clear error, nothing spawned."""
    from routers import cookie_bridge as cb

    src = tmp_path / "ext-src"
    src.mkdir()
    (src / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cb, "_materialize_ext_src", lambda: src)
    monkeypatch.setattr(cb, "_find_browser", lambda name: None)

    captured: list = []
    monkeypatch.setattr("threading.Thread", _capture_thread(captured))
    resp = await client.post("/api/session/cookies/auto-install")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "browser" in (body["error"] or "")
    assert captured == []


async def test_auto_install_missing_extension_package(client, monkeypatch, tmp_path):
    """No manifest in the materialized folder -> error before spawning."""
    from routers import cookie_bridge as cb

    empty = tmp_path / "empty-src"
    empty.mkdir()
    monkeypatch.setattr(cb, "_materialize_ext_src", lambda: empty)
    captured: list = []
    monkeypatch.setattr("threading.Thread", _capture_thread(captured))
    resp = await client.post("/api/session/cookies/auto-install")
    body = resp.json()
    assert body["ok"] is False
    assert "package" in (body["error"] or "")
    assert captured == []


def test_auto_install_worker_folds_result_into_state(monkeypatch):
    """Worker maps a successful script result to done + installed."""
    from routers import cookie_bridge as cb

    monkeypatch.setattr(
        cb, "_run_auto_install_script",
        lambda browser, ext_dir: {"ok": True, "installed": True, "extension_id": "abc123"},
    )
    cb._auto_install_worker("chrome", Path("C:/ext"))
    with cb._AUTO_INSTALL_LOCK:
        assert cb._AUTO_INSTALL_STATE["state"] == "done"
        assert cb._AUTO_INSTALL_STATE["installed"] is True
        assert cb._AUTO_INSTALL_STATE["extension_id"] == "abc123"
        assert cb._AUTO_INSTALL_STATE["error"] is None
        assert cb._AUTO_INSTALL_STATE["finished_at"] is not None


def test_auto_install_worker_reports_failure(monkeypatch):
    """Worker maps a failing script result to error state."""
    from routers import cookie_bridge as cb

    monkeypatch.setattr(
        cb, "_run_auto_install_script",
        lambda browser, ext_dir: {"ok": False, "installed": False, "error": "dialog timeout"},
    )
    cb._auto_install_worker("chrome", Path("C:/ext"))
    with cb._AUTO_INSTALL_LOCK:
        assert cb._AUTO_INSTALL_STATE["state"] == "error"
        assert cb._AUTO_INSTALL_STATE["installed"] is False
        assert cb._AUTO_INSTALL_STATE["error"] == "dialog timeout"


def test_auto_install_worker_survives_crash(monkeypatch):
    """An exception inside the script runner never kills the thread."""
    from routers import cookie_bridge as cb

    def boom(browser, ext_dir):
        raise RuntimeError("boom")

    monkeypatch.setattr(cb, "_run_auto_install_script", boom)
    cb._auto_install_worker("chrome", Path("C:/ext"))
    with cb._AUTO_INSTALL_LOCK:
        assert cb._AUTO_INSTALL_STATE["state"] == "error"
        assert "boom" in (cb._AUTO_INSTALL_STATE["error"] or "")


def _capture_thread(captured: list):
    """threading.Thread stand-in that records (name, kwargs) without running."""

    def fake_thread(*args, **kwargs):
        captured.append((kwargs.pop("name", None), kwargs))
        class _Fake:
            def start(self):
                pass
            daemon = kwargs.get("daemon", False)
        return _Fake()

    return fake_thread


async def test_status_shape(client):
    resp = await client.get("/api/session/cookies/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["paired"] is False
    assert body["enabled"] is True
    assert body["platforms"] == {}
    assert body["youtube_gate_active"] is False
    assert body["youtube_gate_remaining_sec"] == 0


async def test_status_reports_youtube_gate(client):
    from services import yt_gate

    yt_gate.note_youtube_gate("test arm", freeze_sec=60)
    try:
        body = (await client.get("/api/session/cookies/status")).json()
        assert body["youtube_gate_active"] is True
        assert 0 < body["youtube_gate_remaining_sec"] <= 60
    finally:
        yt_gate.clear_youtube_gate()


async def test_status_shape_with_stored_cookies(client):
    resp = await client.post("/api/session/cookies", json={
        "token": "tok-1", "cookies": [{
            "name": "auth_token", "value": "v", "domain": "kick.com",
            "path": "/", "secure": True, "httpOnly": True,
        }],
    })
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1
    body = (await client.get("/api/session/cookies/status")).json()
    assert body["paired"] is True
    assert set(body["platforms"]) == {"kick"}
    kick = body["platforms"]["kick"]
    assert kick["count"] == 1
    assert kick["expiredCount"] == 0
    assert kick["lastGrabAt"], "lastGrabAt must be present after a grab"


def test_real_appdata_db_untouched():
    """Merged-suite guard: the real %APPDATA%/VOD.RIP/archive.db (also the
    cookie store's real file — same path when VODRIP_COOKIE_DB is unset)
    must never be written by the suite.

    Byte-hash comparison against the conftest-import snapshot only holds
    while no external app instance is live (a running app checkpoints its
    WAL into the file concurrently), so when a live WAL is detected we fall
    back to marker rows that only this suite ever writes — real VOD ids
    never look like them."""
    import hashlib
    import sqlite3

    from conftest import REAL_APPDATA_DB_SHA256

    real = Path(os.environ.get("APPDATA", "")) / "VOD.RIP" / "archive.db"
    wal = real.with_name("archive.db-wal")
    try:
        live_writer = wal.exists() and wal.stat().st_size > 0
    except OSError:
        live_writer = True
    if not live_writer:
        try:
            now = hashlib.sha256(real.read_bytes()).hexdigest()
        except OSError:
            now = None
        assert now == REAL_APPDATA_DB_SHA256, (
            "real %APPDATA%/VOD.RIP/archive.db changed during the run — "
            "import-time self-checks or a test leaked onto user data"
        )
    try:
        con = sqlite3.connect(f"file:{real.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return  # real DB absent/unreadable — nothing leaked onto it
    try:
        with con:
            hits = con.execute(
                "SELECT COUNT(*) FROM videos WHERE video_id IN "
                "('__archive_selfcheck__','filter-video-1','legacy-vid','orphan-vid') "
                "OR video_id LIKE 'filter-%' OR video_id LIKE 'kind-%'"
            ).fetchone()[0]
            msg_hits = con.execute(
                "SELECT COUNT(*) FROM messages WHERE video_id IN "
                "('__archive_selfcheck__','orphan-vid') OR video_id LIKE 'filter-%'"
            ).fetchone()[0]
    finally:
        con.close()
    assert hits == 0 and msg_hits == 0, (
        "test-only marker rows found in the real archive.db — the suite leaked"
    )