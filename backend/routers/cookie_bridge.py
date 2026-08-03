
"""Cookie bridge routes — pairing + ingest from the local browser extension.

POST /api/session/cookies  {token, cookies:[...]}  — pairing happens on the
    first successful POST (any token becomes the paired token); later calls
    must present the same token or get 403. Returns 403 while the bridge is
    disabled (cookie_bridge_enabled setting).
GET  /api/session/cookies/pull?platform=  — Netscape cookies.txt (text/plain)
    with only the keep-listed cookie names for that platform.
GET  /api/session/cookies/status — {paired, enabled, platforms:{platform:{count, lastGrabAt, expiredCount}}}
GET  /api/session/cookies/token  — the paired token (Settings diagnostics).
POST /api/session/cookies/enable|disable — kill switch (consent toggle).
GET  /api/session/cookies/extension/update.xml  — policy-install manifest.
GET  /api/session/cookies/extension/extension.crx — the packed extension.
GET  /api/session/cookies/extension/id — extension id from the packed key.
"""

import base64
import hashlib
import io
import json
import logging
import os
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response

from deps import settings_mgr
from services import cookie_store
from services.settings import _get_appdata_dir

logger = logging.getLogger(__name__)
router = APIRouter(tags=["cookie-bridge"])


def _require_platform(platform: str) -> str:
    p = (platform or "").strip().lower()
    if p not in cookie_store.PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail=f"platform must be one of {cookie_store.PLATFORMS}",
        )
    return p


def _paired_token() -> str:
    return (settings_mgr.get().cookie_bridge_token or "").strip()


def _ext_crx_path() -> Path:
    override = os.environ.get("VODRIP_EXT_CRX", "").strip()
    if override:
        return Path(override)
    return _get_appdata_dir() / "cookie-extension" / "extension.crx"


def _ext_pem_path() -> Path:
    return _ext_crx_path().with_suffix(".pem")


_EXT_ID_ALPHABET = "abcdefghijklmnop"

# --- minimal DER helpers (stdlib only; crypto libs are not a declared dep) ---
# Enough to convert the chrome --pack-extension key (PKCS#8 "BEGIN PRIVATE KEY")
# or an SPKI "BEGIN PUBLIC KEY" into the SPKI DER the extension id is hashed
# over. Lengths use the definite form with long-form support (2048-bit RSA
# SPKI needs it).


def _der_read(data: bytes, start: int) -> tuple[int, bytes, int]:
    """(tag, content, next_offset) for one DER TLV."""
    tag = data[start]
    ln = data[start + 1]
    off = start + 2
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(data[off:off + n], "big")
        off += n
    return tag, data[off:off + ln], off + ln


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _der_tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(body)) + body


_RSA_OID = bytes.fromhex("2a864886f70d010101")


def _pkcs8_to_spki(pkcs8: bytes) -> bytes:
    """Public-key SPKI DER for the RSA key inside a PKCS#8 private key."""
    _, outer, _ = _der_read(pkcs8, 0)        # SEQUENCE
    _, _, off = _der_read(outer, 0)          # INTEGER version
    _, _, off = _der_read(outer, off)        # AlgorithmIdentifier
    _, rsa_body, _ = _der_read(outer, off)   # OCTET STRING { RSAPrivateKey }
    _, seq_body, _ = _der_read(rsa_body, 0)  # SEQUENCE { version, n, e, ... }
    _, _, off = _der_read(seq_body, 0)       # INTEGER version
    _, n, off = _der_read(seq_body, off)     # INTEGER modulus
    _, e, _ = _der_read(seq_body, off)       # INTEGER public exponent
    spki_inner = _der_tlv(0x02, n) + _der_tlv(0x02, e)
    algo = _der_tlv(0x30, _der_tlv(0x06, _RSA_OID) + _der_tlv(0x05, b""))
    return _der_tlv(0x30, algo + _der_tlv(0x03, b"\x00" + _der_tlv(0x30, spki_inner)))


def _ext_id() -> str:
    """Extension id from the packed crx key: sha256(SPKI DER)[:16] -> a-p.

    Accepts either the SPKI pem chrome writes for public keys or the PKCS#8
    private key it actually writes ("BEGIN PRIVATE KEY") — the public half is
    derived with stdlib DER parsing.
    """
    try:
        pem = _ext_pem_path().read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = [ln for ln in pem.splitlines() if ln and not ln.startswith("-----")]
    try:
        der = base64.b64decode("".join(lines))
    except Exception:
        return ""
    try:
        _, body, _ = _der_read(der, 0)
        first, _, _ = _der_read(body, 0)
        if first == 0x02:  # INTEGER → PKCS#8 private key
            der = _pkcs8_to_spki(der)
        elif first != 0x30:  # neither SPKI nor PKCS#8
            return ""
    except Exception:
        return ""
    digest = hashlib.sha256(der).digest()[:16]
    return "".join(
        _EXT_ID_ALPHABET[b >> 4] + _EXT_ID_ALPHABET[b & 0xF] for b in digest
    )


_ext_version_cache: dict[tuple[str, int], str] = {}


def _ext_version() -> str:
    """Extension version from the packed crx's embedded manifest (cached by mtime)."""
    crx = _ext_crx_path()
    try:
        st = crx.stat()
    except OSError:
        return ""
    key = (str(crx), st.st_mtime_ns)
    if key not in _ext_version_cache:
        version = ""
        try:
            raw = crx.read_bytes()
            # CRX2/CRX3 both embed a zip; its offset varies with the header,
            # so locate the local-file signature instead of parsing the header.
            zip_start = raw.find(b"PK\x03\x04", 16)
            if zip_start > 0:
                with zipfile.ZipFile(io.BytesIO(raw[zip_start:])) as zf:
                    manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                version = manifest.get("version") or ""
        except Exception:
            version = ""
        _ext_version_cache[key] = version
    return _ext_version_cache[key]


@router.get("/api/session/cookies/extension/update.xml")
async def session_cookies_extension_update_xml(request: Request):
    """Policy-install manifest consumed by Chrome/Edge ExtensionInstallForcelist."""
    ext_id = _ext_id()
    version = _ext_version()
    if not ext_id or not version:
        raise HTTPException(
            status_code=404,
            detail="cookie extension not installed — run scripts/install-cookie-bridge-policy.ps1",
        )
    codebase = (
        str(request.base_url).rstrip("/")
        + "/api/session/cookies/extension/extension.crx"
    )
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>\n"
        f"  <app appid='{ext_id}'>\n"
        f"    <updatecheck codebase='{codebase}' version='{version}' />\n"
        "  </app>\n"
        "</gupdate>\n"
    )
    return Response(content=xml, media_type="text/xml")


@router.get("/api/session/cookies/extension/extension.crx")
async def session_cookies_extension_crx():
    crx = _ext_crx_path()
    if not crx.exists():
        raise HTTPException(
            status_code=404,
            detail="cookie extension crx not installed — run scripts/install-cookie-bridge-policy.ps1",
        )
    return FileResponse(crx, media_type="application/x-chrome-extension")


@router.get("/api/session/cookies/extension/id")
async def session_cookies_extension_id():
    ext_id = _ext_id()
    if not ext_id:
        raise HTTPException(
            status_code=404,
            detail="cookie extension key not installed — run scripts/install-cookie-bridge-policy.ps1",
        )
    return {"extension_id": ext_id}


@router.post("/api/session/cookies/enable")
async def session_cookies_enable():
    s = settings_mgr.get()
    s.cookie_bridge_enabled = True
    settings_mgr.save(s)
    return {"enabled": True}


@router.post("/api/session/cookies/disable")
async def session_cookies_disable():
    s = settings_mgr.get()
    s.cookie_bridge_enabled = False
    settings_mgr.save(s)
    return {"enabled": False}


@router.post("/api/session/cookies")
async def session_cookies_ingest(body: dict):
    current = settings_mgr.get()
    if not current.cookie_bridge_enabled:
        raise HTTPException(status_code=403, detail="cookie bridge disabled")
    token = (body.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=422, detail="token required")
    paired = (current.cookie_bridge_token or "").strip()
    if not paired:
        # Pairing = first successful POST: any token becomes the paired one.
        s = settings_mgr.get()
        s.cookie_bridge_token = token
        settings_mgr.save(s)
        logger.info("cookie bridge paired (token set)")
    elif token != paired:
        raise HTTPException(status_code=403, detail="invalid cookie bridge token")
    cookies = body.get("cookies")
    if not isinstance(cookies, list):
        raise HTTPException(status_code=422, detail="cookies must be a list")
    accepted, dropped = cookie_store.upsert_cookies(cookies)
    return {"ok": True, "accepted": accepted, "dropped": dropped}


@router.get("/api/session/cookies/pull")
async def session_cookies_pull(platform: str):
    p = _require_platform(platform)
    return PlainTextResponse(
        cookie_store.pull_netscape(p),
        media_type="text/plain",
    )


@router.get("/api/session/cookies/status")
async def session_cookies_status():
    return {
        "paired": bool(_paired_token()),
        "enabled": bool(settings_mgr.get().cookie_bridge_enabled),
        "platforms": cookie_store.status(),
    }


@router.get("/api/session/cookies/token")
async def session_cookies_token():
    return {"token": _paired_token()}
