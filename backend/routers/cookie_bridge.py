
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
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Optional

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
            zip_start = raw.find(b"PK\x03\x04", 12)
            if zip_start > 0:
                with zipfile.ZipFile(io.BytesIO(raw[zip_start:])) as zf:
                    manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                version = manifest.get("version") or ""
        except Exception:
            version = ""
        _ext_version_cache[key] = version
    return _ext_version_cache[key]


def _ext_src_dir() -> Path:
    """Unpacked extension folder for drag-and-drop load (chrome://extensions).

    Named after the extension (not the packaging dir) so the user can
    recognize what they are dragging; lives next to the crx it was
    materialized from.
    """
    return _get_appdata_dir() / "cookie-extension" / "VOD.RIP-cookies"


def _materialize_ext_src() -> Path:
    """Extract the packed crx's zip payload into src/ (idempotent).

    Chrome's "load unpacked" accepts a folder, not a crx, so the settings
    install flow materializes the same bytes as a plain folder next to the
    crx. The zip payload starts at the first PK\\x03\\x04 signature (CRX2/3
    safe, mirrors _ext_version); zip-slip guarded; skips _metadata so the
    unpacked load never ships Chrome's own bookkeeping.
    """
    src = _ext_src_dir()
    manifest = src / "manifest.json"
    crx = _ext_crx_path()
    try:
        if manifest.exists() and crx.exists() and crx.stat().st_mtime <= manifest.stat().st_mtime:
            return src
        raw = crx.read_bytes()
        zip_start = raw.find(b"PK\x03\x04", 12)
        if zip_start <= 0:
            return src
        src.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(raw[zip_start:])) as zf:
            for name in zf.namelist():
                if not name or name.startswith("_metadata/"):
                    continue
                target = src / name
                if not str(target.resolve()).startswith(str(src.resolve())):
                    continue
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(name))
        return src
    except (OSError, KeyError, zipfile.BadZipFile):
        return src


_BROWSER_RELS = {
    "chrome": r"Google\Chrome\Application\chrome.exe",
    "msedge": r"Microsoft\Edge\Application\msedge.exe",
    "brave": r"BraveSoftware\Brave-Browser\Application\brave.exe",
}
_EXT_MANAGER_URLS = {
    "chrome": "chrome://extensions/",
    "msedge": "edge://extensions/",
    "brave": "chrome://extensions/",
}


def _find_browser(name: str) -> Optional[Path]:
    """chromium-family browser path — Program Files, Program Files (x86), then PATH."""
    if os.name == "nt" and name in _BROWSER_RELS:
        for root in (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        ):
            candidate = root / _BROWSER_RELS[name]
            if candidate.is_file():
                return candidate
    return Path(shutil.which(name)) if shutil.which(name) else None


def _focus_existing_extension_tab() -> bool:
    """Bring an already-open chrome://extensions tab forward instead of
    spawning a duplicate.

    Uses UI Automation (scripts/focus_extension_tab.ps1): Chrome exposes its
    tab strip as TabItem elements whose name is the localized page title, and
    TabItem supports SelectionItemPattern, so we can select the tab and raise
    its window without CDP or a remote-debugging port. Tab titles are matched
    per locale (pt-BR Chrome calls the page "Extensões"); an unmatched locale
    or a missing script simply reports False and the caller opens a fresh tab.
    """
    script = Path(__file__).resolve().parent.parent / "scripts" / "focus_extension_tab.ps1"
    if not script.is_file():
        return False
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ],
            capture_output=True,
            timeout=20,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _open_extension_manager() -> dict:
    """Activate the extensions-manager tab: reuse an already-open one, else
    launch a fresh tab in the default chromium-family browser."""
    if _focus_existing_extension_tab():
        return {"launched": True, "browser": None, "url": None, "reused": True}
    for name, url in _EXT_MANAGER_URLS.items():
        path = _find_browser(name)
        if not path:
            continue
        try:
            subprocess.Popen([str(path), url])
            return {"launched": True, "browser": name, "url": url, "reused": False}
        except OSError as exc:
            logger.debug("cookie extension manager launch failed (%s): %s", name, exc)
    return {"launched": False, "browser": None, "url": None, "reused": False}


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


@router.get("/api/session/cookies/extension/source")
async def session_cookies_extension_source():
    """Unpacked extension folder for the drag-and-drop install flow.

    ``extension_dir`` is the folder the user drags onto chrome://extensions
    (dev mode on); ``ready`` is false when no crx is installed yet.
    """
    src = _materialize_ext_src()
    return {
        "extension_dir": str(src),
        "ready": (src / "manifest.json").exists(),
        "version": _ext_version() or None,
    }


@router.post("/api/session/cookies/extension/open")
async def session_cookies_extension_open():
    """Best-effort: open the browser's extensions manager (chrome://extensions)."""
    return _open_extension_manager()


@router.post("/api/session/cookies/extension/reveal")
async def session_cookies_extension_reveal():
    """Open Explorer/finder at the unpacked extension folder."""
    src = _materialize_ext_src()
    if not (src / "manifest.json").exists():
        raise HTTPException(status_code=404, detail="extension source not installed")
    try:
        if os.name == "nt":
            os.startfile(str(src))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(src)])
        else:
            subprocess.Popen(["xdg-open", str(src)])
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not reveal folder: {exc}") from exc
    return {"ok": True, "extension_dir": str(src)}


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
