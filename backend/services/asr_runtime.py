"""ASR runtime management — a heavy speech-recognition runtime the base app
splits out and installs on demand.

The base install stays light: the runtime binary is NOT shipped. It lands as a
verified payload under the AI-models folder (``services.disk_hygiene.whisper_cache_dir``),
the same root that hosts parakeet/embed/translate weights, so a user who points
Settings > Disk "AI Models Folder" at a fast drive gets both. **Downloads happen
only in ``ensure_runtime()``** — never at import, never at app startup.

Delivery is a single zip described by a JSON manifest fetched from the official
release URL (or ``VODRIP_ASR_RUNTIME_MANIFEST_URL`` for a private mirror). The
manifest must name the in-archive ``executable``, an ``archive_sha256``, and a
per-file ``files`` sha256 map, so nothing is invented: malformed or incomplete
metadata is a clear error. Extraction is atomic and path-traversal-safe, so a
partial or malicious install never appears as the live runtime dir.

Exported contract: ``runtime_status() -> dict``,
``ensure_runtime(progress=None) -> Path``, ``runtime_available() -> bool``,
``runtime_executable() -> Path``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Official release manifest; deployments can override it for a private mirror.
_DEFAULT_MANIFEST_URL = (
    "https://github.com/mateusant13/VOD.RIP/releases/latest/download/"
    "VOD-RIP-ASR-manifest.json"
)
MANIFEST_URL_ENV = "VODRIP_ASR_RUNTIME_MANIFEST_URL"
# Runtime payload root override (tests/portable installs pin scratch dirs).
RUNTIME_DIR_ENV = "VODRIP_ASR_RUNTIME_DIR"
# Env-guarded module self-check: VODRIP_ASR_RUNTIME_SELFCHECK=1 runs it at import.
SELFCHECK_ENV = "VODRIP_ASR_RUNTIME_SELFCHECK"

# Subdir under the AI-models root for the runtime payload.
_SUBDIR = "asr-runtime"
# Pre-install placeholder for the executable path. The manifest's ``executable``
# field is authoritative once installed; this constant only makes
# runtime_executable() deterministic before the first ensure_runtime().
_DEFAULT_EXECUTABLE = "VOD-RIP-ASR.exe"
_MARKER = ".runtime.json"
_CHUNK = 1024 * 1024
_HTTP_TIMEOUT = 30.0  # socket timeout — a stuck download fails instead of hanging
# Safety cap on total uncompressed bytes from one archive (guards a zip bomb).
# ponytail: a runtime is < a few GB; the cap just needs to be generous, upgrade
# path is manifest-declared per-file sizes enforced before extracting.
_MAX_EXTRACTED_BYTES = 16 * 1024 ** 3
_install_lock = threading.Lock()
_archive_worker_lock = threading.Lock()
_archive_worker_process: Optional[subprocess.Popen] = None

def runtime_dir() -> Path:
    """Root for the ASR runtime payload (support resolver, not API contract).

    Precedence: ``VODRIP_ASR_RUNTIME_DIR`` env -> the AI-models folder
    (``whisper_cache_dir()/asr-runtime``). Pure resolver — never creates the dir
    and never touches the network.
    """
    override = os.environ.get(RUNTIME_DIR_ENV, "").strip()
    if override:
        return Path(override)
    # Lazy: keeps this module import-light; whisper_cache_dir probes drives.
    from services.disk_hygiene import whisper_cache_dir

    return whisper_cache_dir() / _SUBDIR


def runtime_executable() -> Path:
    """The runtime's entry executable path.

    Deterministic even before install: a completed install's marker ``executable``
    wins; otherwise the default name is returned under ``runtime_dir()``. The path
    may not exist yet — pair with ``runtime_available()``.
    """
    d = runtime_dir()
    rel = _DEFAULT_EXECUTABLE
    marker = _read_marker(d)
    if marker is not None:
        rel = marker["executable"]
    return d / rel


def runtime_available() -> bool:
    """True when a fully verified runtime install is present (no network)."""
    return _is_complete(runtime_dir())


def runtime_status() -> dict:
    """Best-effort snapshot of the runtime install (no network, no download).

    Keys: ``installed`` (bool), ``version`` (str|None), ``executable`` (str|None),
    ``dir`` (str), ``env_override`` (bool).
    """
    d = runtime_dir()
    marker = _read_marker(d)
    installed = False
    version = None
    executable = None
    if marker is not None:
        version = marker.get("version")
        exe_rel = marker.get("executable")
        if exe_rel:
            executable = str(d / exe_rel)
            installed = (d / exe_rel).is_file()
    return {
        "installed": installed,
        "version": version,
        "executable": executable,
        "dir": str(d),
        "env_override": bool(os.environ.get(RUNTIME_DIR_ENV, "").strip()),
    }


def ensure_runtime(
    progress: Optional[Callable[[float, str], None]] = None,
) -> Path:
    """Make the ASR runtime available locally, downloading on first call.

    Idempotent: a completed install is detected and returned without network.
    A missing manifest URL, malformed manifest, checksum mismatch, or failed swap
    raises (a partial install never appears as ``runtime_dir()``). ``progress``,
    when given, receives ``(fraction 0..1, short message)`` during manifest fetch /
    download / extraction. This is the ONLY call that ever downloads.
    """
    # Fast path — no lock, no network for the already-installed case.
    if _is_complete(runtime_dir()):
        return runtime_dir()

    with _install_lock:
        # Re-check under the lock: another thread may have just installed.
        if _is_complete(runtime_dir()):
            return runtime_dir()

        manifest_url = (
            os.environ.get(MANIFEST_URL_ENV, "").strip() or _DEFAULT_MANIFEST_URL
        )
        manifest = _fetch_json(manifest_url, progress)
        version = manifest.get("version")
        executable = manifest.get("executable")
        archive_url = manifest.get("archive_url")
        archive_sha = manifest.get("archive_sha256")
        raw_files = manifest.get("files")
        if not isinstance(version, str) or not version:
            raise RuntimeError("ASR runtime manifest is missing a 'version' string")
        if not isinstance(executable, str) or not executable:
            raise RuntimeError("ASR runtime manifest is missing an 'executable' string")
        if not isinstance(archive_url, str) or not (
            archive_url.startswith("http://") or archive_url.startswith("https://")
        ):
            raise RuntimeError(
                "ASR runtime manifest is missing an 'archive_url' (absolute http/https)"
            )
        if not isinstance(archive_sha, str) or not archive_sha:
            raise RuntimeError("ASR runtime manifest is missing 'archive_sha256'")
        if not isinstance(raw_files, dict) or not raw_files:
            raise RuntimeError("ASR runtime manifest is missing a 'files' checksum map")

        files = _normalize_files_map(raw_files)
        exe_rel = _safe_member(executable)
        if exe_rel not in files:
            raise RuntimeError(
                f"ASR runtime manifest 'executable' {executable!r} is not in 'files'"
            )

        d = runtime_dir()
        parent = d.parent
        parent.mkdir(parents=True, exist_ok=True)
        staging = parent / f".{d.name}.staging.{secrets.token_hex(6)}"
        archive_path = parent / f".{d.name}.archive.{secrets.token_hex(6)}"
        try:
            _download(archive_url, archive_path, progress)
            _verify_shas(archive_path, archive_sha)
            staging.mkdir(parents=True)
            _extract_zip_safe(archive_path, staging, files, progress)
            _write_marker(staging, version, exe_rel)
            _atomic_swap(staging, d, parent)
            if progress:
                progress(1.0, "install complete")
            return d
        finally:
            # Never leave download/staging debris behind, even on failure.
            for leftover in (archive_path, staging):
                _rmtree_quiet(leftover)


# --- optional worker process -----------------------------------------------

_server_lock = threading.RLock()
_server_process: Optional[subprocess.Popen] = None
_server_port: Optional[int] = None


def _free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _server_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=1.0
        ) as response:
            return response.status == 200
    except Exception:
        return False


def ensure_server() -> int:
    """Install and start the loopback ASR server on first ASR use."""
    global _server_process, _server_port
    ensure_runtime()
    with _server_lock:
        if (
            _server_process is not None
            and _server_process.poll() is None
            and _server_port is not None
            and _server_ready(_server_port)
        ):
            return _server_port
        if _server_process is not None:
            try:
                _server_process.terminate()
            except OSError:
                pass
        port = _free_loopback_port()
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        _server_process = subprocess.Popen(
            [str(runtime_executable()), "--serve", "--port", str(port)],
            cwd=str(runtime_dir()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        _server_port = port
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if _server_process.poll() is not None:
                raise RuntimeError("ASR runtime server exited during startup")
            if _server_ready(port):
                return port
            time.sleep(0.1)
        raise RuntimeError("ASR runtime server did not become ready")


def transcribe_window(audio: bytes) -> tuple[str, Optional[str]]:
    """Transcribe a float32/16 kHz audio window through the ASR worker."""
    port = ensure_server()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/transcribe",
        data=audio,
        method="POST",
        headers={"Content-Type": "application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120.0) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"ASR runtime request failed: {exc}") from exc
    if not isinstance(body, dict) or "error" in body:
        raise RuntimeError(str(body.get("error", "invalid ASR response")))
    return str(body.get("text", "")), body.get("lang")


def start_archive_worker() -> int:
    """Start one detached archive worker from the optional runtime."""
    global _archive_worker_process
    with _archive_worker_lock:
        if _archive_worker_process is not None:
            if _archive_worker_process.poll() is None:
                return int(_archive_worker_process.pid)
            _archive_worker_process = None
        ensure_runtime()
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        _archive_worker_process = subprocess.Popen(
            [str(runtime_executable()), "--archive-worker"],
            cwd=str(runtime_dir()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        return int(_archive_worker_process.pid)

def stop_server() -> None:
    global _server_process, _server_port
    with _server_lock:
        process, _server_process = _server_process, None
        _server_port = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass


# --- install-state helpers -------------------------------------------------

def _is_complete(d: Path) -> bool:
    marker = _read_marker(d)
    if marker is None:
        return False
    return (d / marker["executable"]).is_file()


def _read_marker(d: Path) -> Optional[dict]:
    m = d / _MARKER
    if not m.is_file():
        return None
    try:
        data = json.loads(m.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("executable"):
        return data
    return None


def _write_marker(d: Path, version: str, executable: str) -> None:
    (d / _MARKER).write_text(
        json.dumps({"version": version, "executable": executable}),
        encoding="utf-8",
    )


# --- network / integrity ---------------------------------------------------

def _fetch_json(url: str, progress: Optional[Callable[[float, str], None]]) -> dict:
    with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as resp:
        payload = resp.read()
    if progress:
        progress(1.0, "manifest fetched")
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("ASR runtime manifest is not a JSON object")
    return data


def _download(url: str, dest: Path, progress: Optional[Callable[[float, str], None]]) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "VOD.RIP ASR runtime/1.0"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = resp.read(_CHUNK)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            if progress and total:
                progress(min(1.0, got / total), f"downloaded {got}/{total} bytes")
    if progress:
        progress(1.0, "download complete")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_shas(path: Path, expected: str) -> None:
    actual = _sha256(path)
    if actual.lower() != expected.lower():
        raise ValueError(
            f"ASR runtime archive checksum mismatch: expected {expected}, got {actual}"
        )


# --- archive extraction (path-traversal-safe, per-file verified) ------------

def _safe_member(name: str) -> str:
    """Validate one archive member name; return the clean relative posix path.

    Rejects absolute paths, drive/colon prefixes, and any ``..`` component (zip
    slip). Raising is the only safe response — never coerce traversal away.
    """
    clean = name.replace("\\", "/")
    if clean.startswith("/") or clean.startswith("//"):
        raise ValueError(f"unsafe absolute path in archive: {name!r}")
    parts = []
    for part in clean.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError(f"path traversal in archive: {name!r}")
        parts.append(part)
    if not parts:
        raise ValueError(f"empty archive path: {name!r}")
    if ":" in parts[0]:
        raise ValueError(f"unsafe archive path (drive/colon): {name!r}")
    return "/".join(parts)


def _safe_dest(target_root: Path, name: str) -> Path:
    rel = _safe_member(name)
    dest = target_root.joinpath(*rel.split("/"))
    # Belt-and-suspenders: the resolved dest stays under the resolved root.
    if not dest.resolve().is_relative_to(target_root.resolve()):
        raise ValueError(f"archive member escapes extraction root: {name!r}")
    return dest


def _normalize_files_map(files: dict) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw_key, value in files.items():
        rel = _safe_member(str(raw_key))
        if not isinstance(value, str) or not value:
            raise ValueError(f"checksum for {raw_key!r} is not a hex string")
        out[rel] = value
    return out


def _extract_zip_safe(
    zip_path: Path,
    target: Path,
    files: Dict[str, str],
    progress: Optional[Callable[[float, str], None]],
) -> None:
    """Extract every member, hashing as we write and refusing unverified files.

    Directory entries get created but are not part of the declared ``files`` map;
    every *file* must be declared with its sha256 (extra archive files and bytes
    that do not match their checksum both fail). A full extract that leaves any
    declared file unverified also fails (incomplete install).
    """
    verified: set = set()
    total_uncompressed = 0
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        for idx, member in enumerate(infos):
            rel = _safe_member(member.filename)
            if member.is_dir():
                _safe_dest(target, member.filename).mkdir(parents=True, exist_ok=True)
                continue
            total_uncompressed += member.file_size
            if total_uncompressed > _MAX_EXTRACTED_BYTES:
                raise ValueError("archive expands beyond the safety cap")
            expected = files.get(rel)
            if expected is None:
                raise ValueError(f"archive contains unlisted file: {member.filename!r}")
            dest = _safe_dest(target, member.filename)
            dest.parent.mkdir(parents=True, exist_ok=True)
            h = hashlib.sha256()
            with zf.open(member) as src, open(dest, "wb") as out:
                for chunk in iter(lambda: src.read(_CHUNK), b""):
                    out.write(chunk)
                    h.update(chunk)
            if h.hexdigest().lower() != expected.lower():
                raise ValueError(f"checksum mismatch for {rel!r}")
            verified.add(rel)
            if progress:
                progress((idx + 1) / len(infos), f"extracted {rel}")
    missing = set(files) - verified
    if missing:
        raise ValueError(f"archive missing declared files: {sorted(missing)}")


def _atomic_swap(staging: Path, dest: Path, parent: Path) -> None:
    """Move a fully extracted/verified staging dir into place atomically.

    ``dest`` is only ever the staging payload or the previous complete install;
    a crash between the two renames leaves either the old install intact at
    ``dest`` or nothing at ``dest`` (never a partial).
    """
    backup = parent / f".{dest.name}.old.{secrets.token_hex(6)}"
    had_old = dest.exists()
    if had_old:
        os.replace(dest, backup)
    try:
        os.replace(staging, dest)
    except OSError:
        # Restore the previous install before re-raising so dest stays valid.
        if had_old and backup.exists():
            try:
                os.replace(backup, dest)
            except OSError:
                pass
        raise
    if had_old and backup.exists():
        _rmtree_quiet(backup)


def _rmtree_quiet(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        try:
            path.unlink()
        except OSError:
            pass


# --- module self-check (env-guarded; creates scratch temp dirs at import) ---

def _selfcheck() -> None:
    """Focused offline check: traversal rejection, checksum verify, atomic swap.

    Never touches the network — the manifest/archive are synthesized in a scratch
    temp dir and the only ``ensure_runtime()`` call is given an invalid URL.
    """
    import tempfile

    scratch = Path(tempfile.mkdtemp(prefix="vodrip-asr-runtime-selfcheck-"))
    saved = {key: os.environ.get(key) for key in (RUNTIME_DIR_ENV, MANIFEST_URL_ENV)}
    try:
        rt_dir = scratch / "rt"
        os.environ[RUNTIME_DIR_ENV] = str(rt_dir)
        os.environ[MANIFEST_URL_ENV] = "not-a-url"

        # 1. An invalid manifest URL fails before any download.
        try:
            ensure_runtime()
        except Exception as exc:
            assert "url" in str(exc).lower() or "unknown url" in str(exc).lower(), str(exc)
        else:
            raise AssertionError("ensure_runtime() should reject an invalid manifest URL")

        # 2. Zip-slip / absolute paths are rejected.
        for bad in ("../evil.exe", "C:/evil.exe", "/abs.exe", "a/../../evil.exe"):
            try:
                _safe_member(bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"traversal not rejected: {bad!r}")

        # 3. Good zip extracts, checksums verify, swap is atomic, install complete.
        payload = b"#!fake-whisper"
        exe_rel = "bin/whisper.exe"
        files = {exe_rel: hashlib.sha256(payload).hexdigest()}
        good_zip = scratch / "good.zip"
        with zipfile.ZipFile(good_zip, "w") as zf:
            zf.writestr(exe_rel, payload)
        _verify_shas(good_zip, _sha256(good_zip))  # archive hash passes

        staging = scratch / ".rt.staging.test"
        staging.mkdir(parents=True)
        _extract_zip_safe(good_zip, staging, files, None)
        assert (staging / exe_rel).read_bytes() == payload
        _write_marker(staging, "1.0.0", exe_rel)
        _atomic_swap(staging, rt_dir, scratch)
        assert runtime_available(), "runtime should be available after atomic swap"
        assert runtime_status()["installed"] is True
        assert runtime_status()["version"] == "1.0.0"
        assert runtime_executable() == rt_dir / exe_rel

        # 4. Wrong archive hash is rejected before extraction.
        bad_zip = scratch / "bad.zip"
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr(exe_rel, payload)
        try:
            _verify_shas(bad_zip, "0" * 64)
        except ValueError:
            pass
        else:
            raise AssertionError("_verify_shas should reject a wrong archive hash")

        # 5. A tampered payload fails its declared checksum.
        tampered_zip = scratch / "tampered.zip"
        with zipfile.ZipFile(tampered_zip, "w") as zf:
            zf.writestr(exe_rel, b"#!tampered")
        tmp = scratch / "tampered-stage"
        tmp.mkdir(parents=True)
        try:
            _extract_zip_safe(tampered_zip, tmp, files, None)
        except ValueError:
            pass
        else:
            raise AssertionError("_extract_zip_safe should reject a tampered payload")

        print("asr_runtime self-check OK")
    finally:
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        shutil.rmtree(scratch, ignore_errors=True)


if os.environ.get(SELFCHECK_ENV) == "1":
    _selfcheck()
