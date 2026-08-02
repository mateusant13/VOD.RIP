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


async def test_extension_update_xml(client, ext_state):
    resp = await client.get("/api/session/cookies/extension/update.xml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/xml")
    body = resp.text
    assert f"appid='{ext_state}'" in body
    assert "codebase='http://test/api/session/cookies/extension/extension.crx'" in body
    assert "version='9.9.9'" in body


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
        "/api/session/cookies/extension/update.xml",
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


async def test_status_shape(client):
    resp = await client.get("/api/session/cookies/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["paired"] is False
    assert body["enabled"] is True
    assert body["platforms"] == {}
