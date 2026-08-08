
"""Cookie bridge routes — pairing + ingest from the local browser extension.

POST /api/session/cookies  {token, cookies:[...]}  — pairing happens on the
    first successful POST (any token becomes the paired token); later calls
    must present the same token or get 403. Returns 403 while the bridge is
    disabled (cookie_bridge_enabled setting).
GET  /api/session/cookies/pull?platform=  — Netscape cookies.txt (text/plain)
    with only the keep-listed cookie names for that platform.
GET  /api/session/cookies/status — {paired, enabled, platforms:{platform:{count, lastGrabAt, expiredCount}},
    youtube_gate_active, youtube_gate_remaining_sec} (the any-tab bot-gate banner polls these)
GET  /api/session/cookies/token  — the paired token (Settings diagnostics).
POST /api/session/cookies/enable|disable — kill switch (consent toggle).
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
import threading
import time
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from deps import settings_mgr
from services import cookie_store
from services.settings import _get_appdata_dir
from services.yt_gate import gate_remaining_sec

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


# Signpost the user drops alongside the extension folder in the parent dir —
# "Show folder" opens the PARENT (cookie-extension/) so the user sees both the
# folder to drag and a note marking it.
_DRAG_NOTE_NAME = "drag this folder above.txt"
_DRAG_NOTE_TEXT = (
    "VOD.RIP-cookies is the folder to drag onto chrome://extensions "
    "(Developer mode ON).\n"
    "This note is just a reminder - drag the folder, not the note.\n"
)


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
    notes = src.parent / _DRAG_NOTE_NAME
    try:
        src.parent.mkdir(parents=True, exist_ok=True)
        if not notes.exists():
            notes.write_text(_DRAG_NOTE_TEXT, encoding="utf-8")
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


def _drive_extension_tab() -> Optional[tuple[str, str]]:
    """Open the extensions manager in a NEW tab of a RUNNING browser.

    Runs scripts/open_extension_new_tab.ps1, which picks a default-profile
    Chromium window (never another profile or incognito), brings it to the
    foreground WITHOUT changing a visible window's state (no restore, no
    ALT-tap), opens a NEW tab (Ctrl+T), and types the URL into the omnibox
    (Ctrl+L -> URL -> Enter) — no clipboard round-trip. A command-line launch
    cannot be used while the browser runs: chrome:// URLs are dropped by the
    process-singleton handoff and leave a stray blank tab (http(s) forward
    fine, chrome:// die). Keystrokes land in a brand-new tab, so the user's
    active tab is never touched.

    The ps1 is the authority on process existence: exit 1 is only emitted
    when NO Chromium browser process is running at all, so the caller may
    spawn a fresh instance. Any running process (even one without a visible
    window) yields exit 2 — bare-spawning then would feed the URL into the
    singleton, which drops it.

    Returns (browser_name, url) when the new tab was driven, ("none", None)
    when no browser is running at all (caller may spawn a fresh instance), or
    ("blocked", None) when a browser runs but its window could not be driven.
    """
    script = Path(__file__).resolve().parent.parent / "scripts" / "open_extension_new_tab.ps1"
    if not script.is_file():
        # Without the driver script we cannot know whether a browser process
        # is alive — refuse to spawn blind rather than risk the stray window.
        return ("blocked", None)
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
            timeout=25,
        )
    except (OSError, subprocess.SubprocessError):
        return ("blocked", None)
    browser = (proc.stdout or b"").decode("utf-8", "replace").strip().lower()
    if proc.returncode == 0 and browser in _EXT_MANAGER_URLS:
        return (browser, _EXT_MANAGER_URLS[browser])
    if proc.returncode == 1:
        return ("none", None)
    return ("blocked", None)


def _open_extension_manager() -> dict:
    """Open the browser's extensions manager in a NEW tab, always.

    When a Chromium browser is running, drive it with keystrokes (new tab
    first, then navigate — the active tab is never touched); when the ps1
    confirms NO browser process is running, spawn a fresh instance with the
    URL, which opens directly because there is no process singleton to drop
    it. A bare spawn is never attempted while a browser process may be
    alive — the ps1 reports exit 2 (blocked) for every process-without-a-
    driveable-window case, and the driver script being missing is treated as
    blocked, not as a license to spawn. This always targets the user's real
    browser exe — the app's own WebView2 cookie store is never involved.
    """
    driven = _drive_extension_tab()
    if driven:
        outcome, payload = driven
        if outcome == "blocked":
            return {"launched": False, "browser": None, "url": None, "blocked": True}
        if outcome != "none":
            return {"launched": True, "browser": outcome, "url": payload}
    for name, url in _EXT_MANAGER_URLS.items():
        path = _find_browser(name)
        if not path:
            continue
        try:
            subprocess.Popen([str(path), url])
            return {"launched": True, "browser": name, "url": url}
        except OSError as exc:
            logger.debug("cookie extension manager launch failed (%s): %s", name, exc)
    return {"launched": False, "browser": None, "url": None}


# --- one-click auto-install (productized CookieInstallWorker flow) ----------
# The proven mechanism: kill the browser, relaunch it with
# --remote-debugging-port + an explicit --user-data-dir (Chrome 136+ ignores
# the debug flag on the default profile without it), drive chrome://extensions
# over CDP (real Input.dispatchMouseEvent clicks — synthetic .click() never
# opens the native folder dialog), then drive the #32770 dialog 100% by
# Win32 (WM_SETTEXT the "Pasta:" edit, BM_CLICK "Selecionar pasta"). The
# whole automation lives in scripts/cookie_auto_install.ps1 (BCL only —
# ClientWebSocket for CDP, Add-Type P/Invoke for the dialog); the route here
# only spawns it in a background thread and mirrors its state in
# _AUTO_INSTALL_STATE. Windows-only in practice (the ps1 is the driver);
# on other platforms the route reports a clear error instead of failing blind.

# {state: idle|running|done|error, installed, extension_id, error,
#  started_at, finished_at} — mutated under _AUTO_INSTALL_LOCK.
_AUTO_INSTALL_LOCK = threading.Lock()
_AUTO_INSTALL_STATE = {
    "state": "idle",
    "installed": False,
    "extension_id": "",
    "error": None,
    "started_at": None,
    "finished_at": None,
}

def _ext_src_override() -> Optional[Path]:
    """Env override for the unpacked extension folder (mirrors VODRIP_EXT_CRX)."""
    override = os.environ.get("VODRIP_EXT_SRC", "").strip()
    return Path(override) if override else None


def _auto_install_script() -> Path:
    return Path(__file__).resolve().parent.parent / "scripts" / "cookie_auto_install.ps1"


def _run_auto_install_script(browser: str, extension_dir: Path, port: int = 9222) -> dict:
    """Run scripts/cookie_auto_install.ps1; parse the trailing JSON result line.

    The script prints human progress to stderr and exactly one JSON object as
    its final stdout line. Best-effort: any failure yields an error dict, never
    an exception (the caller reports a clean error string).
    """
    script = _auto_install_script()
    if not script.is_file():
        return {"ok": False, "installed": False, "error": "auto-install driver missing"}
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
                "-ExtensionDir",
                str(extension_dir),
                "-Browser",
                browser,
                "-DebugPort",
                str(port),
            ],
            capture_output=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "installed": False, "error": f"install driver failed: {exc}"}
    out = (proc.stdout or b"").decode("utf-8", "replace")
    result: Optional[dict] = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                result = json.loads(line)
            except ValueError:
                continue
    if result is None:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        tail = err[-1] if err else f"exit {proc.returncode}"
        return {"ok": False, "installed": False, "error": tail[-300:]}
    if not isinstance(result.get("ok"), bool):
        return {"ok": False, "installed": False, "error": "malformed install driver result"}
    return result


def _auto_install_worker(browser: str, extension_dir: Path) -> None:
    """Background thread body — runs the ps1 and folds its result into state."""
    try:
        res = _run_auto_install_script(browser, extension_dir)
    except Exception as exc:  # never let the thread die silently
        logger.exception("cookie auto-install crashed")
        res = {"ok": False, "installed": False, "error": f"install crashed: {exc}"}
    with _AUTO_INSTALL_LOCK:
        _AUTO_INSTALL_STATE["state"] = "done" if res.get("ok") else "error"
        _AUTO_INSTALL_STATE["installed"] = bool(res.get("installed"))
        _AUTO_INSTALL_STATE["extension_id"] = str(res.get("extension_id") or _ext_id() or "")
        _AUTO_INSTALL_STATE["error"] = res.get("error")
        _AUTO_INSTALL_STATE["finished_at"] = time.time()
        logger.info("cookie auto-install finished: %s", _AUTO_INSTALL_STATE["state"])


def _start_auto_install(browser: str, extension_dir: Path) -> bool:
    """Mark running + spawn the worker. Lock only guards state transitions —
    the worker holds no lock while the ps1 runs, so a second POST can detect
    'running' instead of blocking behind the install."""
    with _AUTO_INSTALL_LOCK:
        if _AUTO_INSTALL_STATE["state"] == "running":
            return False
        _AUTO_INSTALL_STATE.update(
            state="running", installed=False, extension_id="", error=None,
            started_at=time.time(), finished_at=None,
        )
    threading.Thread(
        target=_auto_install_worker,
        args=(browser, extension_dir),
        daemon=True,
        name="cookie-auto-install",
    ).start()
    return True


@router.get("/api/session/cookies/extension/extension.crx")
async def session_cookies_extension_crx():
    crx = _ext_crx_path()
    if not crx.exists():
        raise HTTPException(
            status_code=404,
            detail="cookie extension crx not installed — restart the app to refresh the drag-drop folder in Settings",
        )
    return FileResponse(crx, media_type="application/x-chrome-extension")


@router.get("/api/session/cookies/extension/id")
async def session_cookies_extension_id():
    ext_id = _ext_id()
    if not ext_id:
        raise HTTPException(
            status_code=404,
            detail="cookie extension key not installed — restart the app to refresh the drag-drop folder in Settings",
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
    """Open chrome://extensions in a NEW tab of the user's browser (never the active tab)."""
    return _open_extension_manager()


@router.post("/api/session/cookies/extension/reveal")
async def session_cookies_extension_reveal():
    """Open Explorer/finder at the cookie-extension folder — the user sees the
    VOD.RIP-cookies folder to drag PLUS the 'drag this folder above' note."""
    src = _materialize_ext_src()
    if not (src / "manifest.json").exists():
        raise HTTPException(status_code=404, detail="extension source not installed")
    parent = src.parent
    try:
        if os.name == "nt":
            os.startfile(str(parent))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(parent)])
        else:
            subprocess.Popen(["xdg-open", str(parent)])
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not reveal folder: {exc}") from exc
    return {"ok": True, "extension_dir": str(parent)}


@router.post("/api/session/cookies/enable")
async def session_cookies_enable():
    s = settings_mgr.get()
    s.cookie_bridge_enabled = True
    settings_mgr.save(s)
    return {"enabled": True}


@router.post("/api/session/cookies/auto-install")
async def session_cookies_auto_install():
    """One-click cookie-extension install (productized proven flow).

    Already paired -> short-circuit without touching the browser. Otherwise
    spawn the automation (scripts/cookie_auto_install.ps1) in a background
    thread and return immediately — progress/result are mirrored by
    GET /api/session/cookies/status -> auto_install while the frontend
    shows a spinner. Never blocks the event loop: the ps1 runs in a thread.
    """
    if _paired_token():
        return {"ok": True, "installed": True, "alreadyInstalled": True, "state": "done"}
    with _AUTO_INSTALL_LOCK:
        if _AUTO_INSTALL_STATE["state"] == "running":
            return {"ok": True, "started": False, "alreadyRunning": True, "state": "running"}
    src = _ext_src_override() or _materialize_ext_src()
    if not (src / "manifest.json").exists():
        return {
            "ok": False,
            "state": "error",
            "error": "cookie extension package not installed — restart the app to refresh it",
        }
    browser = next((n for n in ("chrome", "msedge", "brave") if _find_browser(n)), None)
    if not browser:
        return {"ok": False, "state": "error", "error": "no Chromium browser found"}
    if not _start_auto_install(browser, src):
        return {"ok": True, "started": False, "alreadyRunning": True, "state": "running"}
    return {"ok": True, "started": True, "state": "running"}


@router.get("/api/session/cookies/status")
async def session_cookies_status():
    gate_sec = gate_remaining_sec()
    with _AUTO_INSTALL_LOCK:
        ai = dict(_AUTO_INSTALL_STATE)
    return {
        "paired": bool(_paired_token()),
        "enabled": bool(settings_mgr.get().cookie_bridge_enabled),
        "platforms": cookie_store.status(),
        # YouTube bot-gate cooldown state — read-only mirror of yt_gate; the
        # any-tab banner polls these. Gating/freeze logic stays in yt_gate.
        "youtube_gate_active": gate_sec > 0,
        "youtube_gate_remaining_sec": gate_sec,
        # One-click auto-install mirror — state: idle|running|done|error.
        "auto_install": {
            "state": ai["state"],
            "installed": bool(ai["installed"]) or bool(_paired_token()),
            "extension_id": ai["extension_id"] or _ext_id() or None,
            "error": ai["error"],
            "started_at": ai["started_at"],
            "finished_at": ai["finished_at"],
        },
    }


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
    gate_sec = gate_remaining_sec()
    return {
        "paired": bool(_paired_token()),
        "enabled": bool(settings_mgr.get().cookie_bridge_enabled),
        "platforms": cookie_store.status(),
        # YouTube bot-gate cooldown state — read-only mirror of yt_gate; the
        # any-tab banner polls these. Gating/freeze logic stays in yt_gate.
        "youtube_gate_active": gate_sec > 0,
        "youtube_gate_remaining_sec": gate_sec,
    }


@router.get("/api/session/cookies/token")
async def session_cookies_token():
    return {"token": _paired_token()}
