"""Zero-UI automatic PO Token via the bgutil HTTP server (Brainicism/bgutil-ytdlp-pot-provider).

The bgutil-ytdlp-pot-provider is a small Node.js HTTP server that mints YouTube
``PO`` tokens on demand. This module owns the *background* side: it ensures the
server is up before the first YouTube request, fetches a token by video id,
and intentionally does **not** shut it down on app exit (the process tree is
reaped with the OS).

Endpoints used (bgutil-ytdlp-pot-provider >= 1.x):

  GET  /ping        -> "pong"  (text/plain)
  POST /get_pot     -> {"content_binding": "<video_id>"}  ->  {"poToken": "..."}

If the server is not running and a prebuilt bundle is present under
``_pot_server_dir()/server/build/main.js`` we spawn it; if Node is missing or
the bundle is missing we silently no-op and the caller falls back to the
existing innertube + cookie path (``youtube_innertube`` / ``youtube_auth``).

The module never raises into callers — every public entry point returns a
plain ``bool`` / ``Optional[str]`` so the youtube services can call it on
every preview/download without try/except boilerplate.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

from services.os_services import _NO_WINDOW, register_child_pid

logger = logging.getLogger(__name__)

# ===================================================================
# Constants
# ===================================================================

POT_DEFAULT_BASE = "http://127.0.0.1:4416"
POT_DEFAULT_PORT = 4416

_POT_PING_PATH = "/ping"
_POT_GET_PATH = "/get_pot"

# 8 s is the upper bound for a cold node cold-start of bgutil on a slow disk.
# We poll instead of blocking so the UI thread stays responsive.
_POT_STARTUP_TIMEOUT_S = 8.0
_POT_PING_INTERVAL_S = 0.25
_POT_PING_TIMEOUT_S = 1.5
_POT_GET_TIMEOUT_S = 5.0

# Field-name fallbacks for /get_pot — bgutil 1.x ships "poToken" but earlier
# 0.x betas used "po_token" and the upstream yt-dlp plugin sometimes echoes
# "pot". The order is part of the contract with ``youtube_service`` —
# do not reorder.
_POT_TOKEN_KEYS = ("poToken", "po_token", "pot")

# Module-level state — guarded by ``_state_lock``.
_state_lock = threading.Lock()
_warm_thread: Optional[threading.Thread] = None
_last_ready: bool = False


# ===================================================================
# Paths
# ===================================================================

def _frozen_exe_dir() -> Optional[Path]:
    """Directory containing the running frozen EXE, or None when not frozen.

    Mirrors the helper in ``ytdlp_ffmpeg._bundled_ffmpeg_dirs``: under a
    PyInstaller one-file bootloader ``sys.executable`` points inside the
    temp extract dir, while under COLLECT (the layout vod-rip.spec
    emits) it points at the EXE on disk. Both put ``runtime/`` next to
    the EXE so this single helper covers both.
    """
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def _frozen_runtime_paths() -> Optional[tuple[Path, Path]]:
    """Pair of ``(node.exe, bgutil main.js)`` for the frozen installer.

    ``scripts/download-node.ps1`` drops a private Node 20 at
    ``build/external/node.exe`` (bundled by vod-rip.spec to
    ``<exe-dir>/runtime/node.exe``), and ``scripts/build-bgutil-bundle.ps1``
    drops a built bgutil server at ``<exe-dir>/runtime/bgutil-pot/``.
    Returns ``(node_exe, main_js)`` when both artefacts exist on disk,
    or ``None`` when either is missing — callers fall back to PATH /
    lazy bootstrap in that case.
    """
    exe_dir = _frozen_exe_dir()
    if exe_dir is None:
        return None
    node_exe = exe_dir / "runtime" / "node.exe"
    main_js = exe_dir / "runtime" / "bgutil-pot" / "server" / "build" / "main.js"
    if node_exe.is_file() and main_js.is_file():
        return node_exe, main_js
    return None


def _pot_server_dir() -> Path:
    """Install path for the optional prebuilt bgutil server bundle.

    On Windows we use ``%LOCALAPPDATA%\\VOD.RIP\\bgutil-pot`` — it persists
    across reinstalls and does not require admin rights. On other platforms
    we fall back to ``$TMPDIR`` / ``$TEMP`` / ``/tmp`` so we never write
    inside the project tree (which may be read-only after install).

    The directory is *not* created here — creation is deferred to the
    spawn step so importing this module is side-effect free.
    """
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "VOD.RIP" / "bgutil-pot"
    tmp = os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp"
    return Path(tmp) / "VOD.RIP" / "bgutil-pot"


def _pot_main_js() -> Optional[Path]:
    """Path to a usable bgutil ``build/main.js`` if one exists on disk.

    Order of preference:

    1. Frozen runtime bundle (``<exe-dir>/runtime/bgutil-pot/...``)
       — the layout vod-rip.spec emits for the Windows installer.
    2. Writable user-level bundle under ``_pot_server_dir()/server/``
       — the dev / first-run path produced by ``_bootstrap_pot_server_bundle``.

    Returns ``None`` when neither exists.
    """
    frozen = _frozen_runtime_paths()
    if frozen is not None:
        return frozen[1]
    candidate = _pot_server_dir() / "server" / "build" / "main.js"
    return candidate if candidate.is_file() else None


# ===================================================================
# HTTP helpers (stdlib urllib — matches twitch_gql_service style)
# ===================================================================

def _http_get(url: str, *, timeout: float) -> Optional[bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    # ponytail: best-effort HTTP — any I/O / HTTP / URL failure returns None
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        logger.debug("POT http GET %s failed: %s", url, exc)
        return None


def _http_post_json(url: str, payload: dict, *, timeout: float) -> Optional[dict]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    # ponytail: best-effort HTTP — any I/O / HTTP / URL failure returns None
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        logger.debug("POT http POST %s failed: %s", url, exc)
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        logger.debug("POT http POST %s non-JSON: %s", url, exc)
        return None
    return parsed if isinstance(parsed, dict) else None


# ===================================================================
# Public API — used by youtube_service / youtube_auth
# ===================================================================

def pot_service_ping(
    base: str = POT_DEFAULT_BASE,
    timeout: float = _POT_PING_TIMEOUT_S,
) -> bool:
    """Return True if the bgutil pot server answers ``GET /ping`` within *timeout*."""
    if not base:
        return False
    url = base.rstrip("/") + _POT_PING_PATH
    data = _http_get(url, timeout=timeout)
    if data is None:
        return False
    # bgutil returns the literal text "pong"; accept any non-empty body so a
    # future minor version with JSON or a different text still works.
    return bool(data.strip())


def fetch_video_po_token(
    video_id: str,
    base: str = POT_DEFAULT_BASE,
    *,
    timeout: float = _POT_GET_TIMEOUT_S,
) -> Optional[str]:
    """Mint a YouTube PO token bound to *video_id* via ``POST /get_pot``.

    Returns the token string on success, ``None`` on any failure (server
    down, timeout, non-JSON, missing field). Never raises.
    """
    vid = (video_id or "").strip()
    if not vid or not base:
        return None
    url = base.rstrip("/") + _POT_GET_PATH
    data = _http_post_json(url, {"content_binding": vid}, timeout=timeout)
    if not data:
        return None
    for key in _POT_TOKEN_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# ===================================================================
# Lifecycle — best-effort background warm
# ===================================================================

def _bootstrap_pot_server_bundle() -> bool:
    """Download bgutil server source and build if Node+npm are available.

    Skipped entirely under a frozen install: the bundle is already
    shipped alongside the EXE (``scripts/build-bgutil-bundle.ps1``), and
    the user-level ``%LOCALAPPDATA%`` location is intentionally
    read-only for the frozen path — PyInstaller installation on Windows
    defaults to ``%LOCALAPPDATA%\\VOD.RIP`` (per installer/installer.iss),
    which the installer does *not* mark writable for self-update. Trying
    to extract a fresh tarball there during normal operation is a
    permission-error footgun.
    """
    if _pot_main_js():
        return True
    if getattr(sys, "frozen", False):
        return False
    node = shutil.which("node")
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not node or not npm:
        return False
    root = _pot_server_dir()
    server_dir = root / "server"
    tag = "1.3.1"
    zip_url = f"https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/{tag}.zip"
    zip_path = root / f"bgutil-{tag}.zip"
    try:
        root.mkdir(parents=True, exist_ok=True)
        if not zip_path.is_file():
            logger.info("POT bootstrap: downloading bgutil %s", tag)
            with urllib.request.urlopen(zip_url, timeout=120) as resp:
                zip_path.write_bytes(resp.read())
        extract_root = root / f"bgutil-ytdlp-pot-provider-{tag}"
        if not extract_root.is_dir():
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(root)
        src_server = extract_root / "server"
        if src_server.is_dir() and not server_dir.is_dir():
            shutil.copytree(src_server, server_dir)
    except Exception as exc:
        logger.debug("POT bootstrap download failed: %s", exc)
        return False
    if not server_dir.is_dir():
        return False
    try:
        if not (server_dir / "build" / "main.js").is_file():
            logger.info("POT bootstrap: npm ci + build (first run may take a minute)")
            subprocess.run(
                [npm, "ci"],
                cwd=str(server_dir),
                check=False,
                timeout=300,
                creationflags=_NO_WINDOW,
            )
            subprocess.run(
                [npm, "exec", "--", "tsc"],
                cwd=str(server_dir),
                check=False,
                timeout=300,
                creationflags=_NO_WINDOW,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("POT bootstrap build failed: %s", exc)
        return False
    return _pot_main_js() is not None


def _spawn_pot_server() -> Optional[subprocess.Popen]:
    """Start the bgutil server in the background. Returns the Popen or None.

    Resolves ``node.exe`` from the frozen runtime bundle (when present)
    before falling back to ``shutil.which("node")``. Same applies to
    the bgutil ``main.js`` — see ``_pot_main_js``.
    """
    main_js = _pot_main_js()
    if main_js is None:
        return None
    frozen = _frozen_runtime_paths()
    if frozen is not None:
        node_exe, frozen_main = frozen
    else:
        node_exe = shutil.which("node")
        frozen_main = None
    if not node_exe:
        return None
    if frozen_main is not None and main_js != frozen_main:
        # Defensive: a user-level bundle shadows the frozen one — keep
        # using the bundled main.js to stay consistent with node_exe.
        main_js = frozen_main

    # main_js = <root>/server/build/main.js -> cwd = <root>/server/ so that
    # ``node build/main.js --port 4416`` resolves the entry the way the
    # bgutil README documents. Applies identically to the frozen layout
    # (``<exe-dir>/runtime/bgutil-pot/server/build/main.js``) — the
    # cwd chosen this way lets node find sibling ``node_modules/`` via
    # its default module resolution walk.
    cwd = main_js.parent.parent

    log_path = _pot_server_dir() / "pot-server.log"
    log_file = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "ab", buffering=0)
    # ponytail: best-effort log open — fall through to DEVNULL
    except OSError as exc:
        logger.debug("POT server log open failed: %s", exc)

    try:
        proc = subprocess.Popen(
            [node_exe, "build/main.js", "--port", str(POT_DEFAULT_PORT)],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log_file if log_file is not None else subprocess.DEVNULL,
            stderr=log_file if log_file is not None else subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
    # ponytail: subprocess spawn must never raise into callers — fall back to None
    except (OSError, ValueError) as exc:
        logger.debug("POT server spawn failed: %s", exc)
        if log_file is not None:
            try:
                log_file.close()
            except OSError:
                pass
        return None

    # register_child_pid is the project's single channel for "kill my
    # children on shutdown". The bgutil server is a child of VOD.RIP and
    # should be reaped with us; trusting the PID via os_services is the
    # correct use here.
    try:
        register_child_pid(proc.pid)
    # ponytail: best-effort — pid tracking is a hint, not a contract
    except Exception as exc:
        logger.debug("POT server pid tracking failed: %s", exc)

    logger.info("POT server spawned pid=%d port=%d", proc.pid, POT_DEFAULT_PORT)
    return proc


def ensure_pot_server_started() -> bool:
    """Make sure the bgutil pot server is reachable on ``POT_DEFAULT_BASE``.

    Behaviour:

    1. If a ping succeeds we are done (return True).
    2. Otherwise, if Node is on ``PATH`` and the prebuilt bundle exists under
       ``_pot_server_dir()/server/build/main.js`` we spawn it and poll
       ``GET /ping`` every 250 ms for up to 8 s.
    3. Returns True on success, False on any failure. Never raises.
    """
    global _last_ready
    with _state_lock:
        if pot_service_ping():
            _last_ready = True
            return True

        if not _pot_main_js():
            _bootstrap_pot_server_bundle()

        if _spawn_pot_server() is None:
            _last_ready = False
            return False

        deadline = time.monotonic() + _POT_STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(_POT_PING_INTERVAL_S)
            if pot_service_ping():
                _last_ready = True
                return True

        logger.debug(
            "POT server did not become ready within %.1fs", _POT_STARTUP_TIMEOUT_S
        )
        _last_ready = False
        return False


def schedule_pot_service_warm() -> Optional[threading.Thread]:
    """Fire-and-forget background warm of the bgutil pot server.

    Spawns a daemon thread that calls ``ensure_pot_server_started()`` once.
    Idempotent: subsequent calls return the existing thread while it is
    alive, or a fresh thread if the previous one has exited. Never raises.
    """
    global _warm_thread
    with _state_lock:
        if _warm_thread is not None and _warm_thread.is_alive():
            return _warm_thread

        def _run() -> None:
            try:
                ok = ensure_pot_server_started()
                logger.info("POT warm complete ready=%s", ok)
            # ponytail: best-effort — daemon must never crash the app
            except Exception as exc:
                logger.debug("POT warm failed: %s", exc)

        t = threading.Thread(target=_run, name="pot-warm", daemon=True)
        t.start()
        _warm_thread = t
        return t


def pot_service_is_ready() -> bool:
    """Snapshot of the last successful warm — used for diagnostics only."""
    with _state_lock:
        return _last_ready


# ponytail: auto-download + npm build of bgutil server runs on first warm when Node is present.


# ===================================================================
# Self-checks — run on import. Cheap, no I/O, no network.
# ===================================================================

assert POT_DEFAULT_BASE == "http://127.0.0.1:4416"
assert POT_DEFAULT_PORT == 4416
assert _POT_STARTUP_TIMEOUT_S > 0
assert _POT_PING_INTERVAL_S > 0
assert _POT_PING_TIMEOUT_S > 0
assert _POT_GET_TIMEOUT_S > 0

# Public surface is stable — youtube_service imports these by name.
assert callable(pot_service_ping)
assert callable(fetch_video_po_token)
assert callable(ensure_pot_server_started)
assert callable(schedule_pot_service_warm)
assert callable(pot_service_is_ready)

# Field-name fallback order is part of the contract — do not reorder.
assert list(_POT_TOKEN_KEYS) == ["poToken", "po_token", "pot"]

# _pot_server_dir() is pure: same env -> same Path.
assert isinstance(_pot_server_dir(), Path)
assert _pot_server_dir() == _pot_server_dir()

# Frozen-runtime helpers are pure functions of ``sys.frozen`` — call them
# once on import so an unexpected signature change fails at startup, not
# at the moment someone tries to spawn the server from a YouTube request.
assert _frozen_exe_dir() is None or isinstance(_frozen_exe_dir(), Path)
frozen_paths = _frozen_runtime_paths()
assert frozen_paths is None or (
    isinstance(frozen_paths, tuple)
    and len(frozen_paths) == 2
    and all(isinstance(p, Path) for p in frozen_paths)
)

# Hit a guaranteed-unreachable address (port 1 is reserved and unbound on
# every modern OS). Must return False and must not raise.
assert pot_service_ping("http://127.0.0.1:1", timeout=0.2) is False

# Empty base / empty video_id must short-circuit to a safe default, not raise.
assert pot_service_ping("", timeout=0.2) is False
assert fetch_video_po_token("") is None
assert fetch_video_po_token("dQw4w9WgXcQ", base="") is None
