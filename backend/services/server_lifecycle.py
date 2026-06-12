"""Uvicorn / API port lifecycle — start, stop, and release port 7897 on app exit."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from typing import Any, Optional

_logger = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

_shutdown_event = threading.Event()
_uvicorn_server: Any = None
_server_lock = threading.Lock()

def register_uvicorn_server(server: Any) -> None:
    global _uvicorn_server
    with _server_lock:
        _uvicorn_server = server


def should_stop_supervisor() -> bool:
    return _shutdown_event.is_set()


_WIN_LISTEN_MARKERS = (
    "LISTENING", "OUVINDO", "ABH", "ÉCOUTE", "ESCUCHA", "IN ASCOLTO", "LISTEN",
)


def _local_endpoint_port(addr: str) -> Optional[int]:
    if addr.startswith("["):
        m = re.search(r"]:(\d+)$", addr)
    else:
        m = re.search(r":(\d+)$", addr)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _pids_listening_on_port_windows(port: int) -> list[int]:
    pids: list[int] = []
    try:
        ps = (
            f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
            f"-ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess)"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=6,
            creationflags=_NO_WINDOW,
        )
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
        if pids:
            return list(dict.fromkeys(pids))
    except Exception as exc:
        _logger.debug("Get-NetTCPConnection for port %s: %s", port, exc)

    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_NO_WINDOW,
        )
        for line in (result.stdout or "").splitlines():
            upper = line.upper()
            if not any(marker in upper for marker in _WIN_LISTEN_MARKERS):
                continue
            cols = line.split()
            if len(cols) < 5:
                continue
            if _local_endpoint_port(cols[1]) != port:
                continue
            pid = cols[-1]
            if pid.isdigit():
                pids.append(int(pid))
    except Exception as exc:
        _logger.debug("netstat for port %s: %s", port, exc)
    return list(dict.fromkeys(pids))


def _pids_listening_on_port(port: int) -> list[int]:
    pids: list[int] = []
    if os.name == "nt":
        return _pids_listening_on_port_windows(port)
    else:
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            for part in result.stdout.split():
                if part.strip().isdigit():
                    pids.append(int(part.strip()))
        except Exception as exc:
            _logger.debug("lsof for port %s: %s", port, exc)
    return list(dict.fromkeys(pids))


def _process_image_name(pid: int) -> Optional[str]:
    if os.name != "nt":
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "comm="],
                capture_output=True,
                text=True,
                timeout=3,
            )
            name = result.stdout.strip()
            return name or None
        except Exception:
            return None
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=_NO_WINDOW,
        )
        line = result.stdout.strip()
        if not line or line.upper().startswith("INFO:"):
            return None
        return line.split(",")[0].strip().strip('"') or None
    except Exception:
        return None


def _request_graceful_shutdown(port: int) -> bool:
    try:
        import requests

        info = requests.get(f"http://127.0.0.1:{port}/api/info", timeout=1.5)
        if info.status_code != 200 or info.json().get("name") != "VOD.RIP":
            return False
        response = requests.post(f"http://127.0.0.1:{port}/api/exit", timeout=2)
        return response.status_code == 200
    except Exception as exc:
        _logger.debug("graceful shutdown on port %s: %s", port, exc)
        return False


def _wait_for_port_free(port: int, *, skip_pid: Optional[int], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = [p for p in _pids_listening_on_port(port) if skip_pid is None or p != skip_pid]
        if not remaining:
            return True
        time.sleep(0.15)
    return not [p for p in _pids_listening_on_port(port) if skip_pid is None or p != skip_pid]


def _process_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return exit_code.value == STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate_process_windows(pid: int) -> bool:
    try:
        import ctypes

        PROCESS_TERMINATE = 0x0001
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            if not ctypes.windll.kernel32.TerminateProcess(handle, 1):
                return False
            ctypes.windll.kernel32.WaitForSingleObject(handle, 3000)
            return not _process_alive(pid)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception as exc:
        _logger.debug("TerminateProcess pid %s: %s", pid, exc)
        return False


def _kill_pid_windows(port: int, pid: int, image: str) -> bool:
    _logger.info("Stopping port %s listener pid %s (%s)", port, pid, image)
    for args in (
        ["taskkill", "/F", "/PID", str(pid)],
        ["taskkill", "/F", "/T", "/PID", str(pid)],
    ):
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=4,
                creationflags=_NO_WINDOW,
            )
            if result.returncode == 0 or not _process_alive(pid):
                return True
            err = (result.stderr or result.stdout or "").strip()
            if err:
                _logger.debug("%s: %s", " ".join(args), err)
        except subprocess.TimeoutExpired:
            _logger.debug("%s timed out", " ".join(args))
        if not _process_alive(pid):
            return True

    try:
        subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue",
            ],
            capture_output=True,
            text=True,
            timeout=4,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        _logger.debug("Stop-Process pid %s timed out", pid)

    if not _process_alive(pid):
        return True
    return _terminate_process_windows(pid)


def _kill_pid(port: int, pid: int) -> bool:
    """Force-stop *pid*. Returns True if the process is gone afterward."""
    if pid == os.getpid():
        return True
    image = _process_image_name(pid) or "unknown"
    try:
        if os.name == "nt":
            if not _kill_pid_windows(port, pid, image):
                _logger.warning(
                    "Could not stop pid %s (%s) on port %s",
                    pid,
                    image,
                    port,
                )
                return False
        else:
            os.kill(pid, 15)
            time.sleep(0.25)
            try:
                os.kill(pid, 0)
                os.kill(pid, 9)
            except OSError:
                pass
    except Exception as exc:
        _logger.debug("kill pid %s: %s", pid, exc)
    time.sleep(0.2)
    return not _process_alive(pid)


def release_api_port(port: int, *, skip_pid: Optional[int] = None, timeout: float = 10.0) -> None:
    """Free *port* for a new VOD.RIP / dev API instance (graceful first, then force kill)."""
    deadline = time.monotonic() + timeout

    def active_listeners() -> list[int]:
        return [p for p in _pids_listening_on_port(port) if skip_pid is None or p != skip_pid]

    listeners = active_listeners()
    if not listeners:
        return

    _logger.info("Port %s busy — asking existing VOD.RIP API to exit", port)
    if _request_graceful_shutdown(port):
        if _wait_for_port_free(port, skip_pid=skip_pid, timeout=min(4.0, timeout)):
            _logger.info("Port %s released after graceful shutdown", port)
            return

    while time.monotonic() < deadline:
        remaining = active_listeners()
        if not remaining:
            return

        _logger.info("Releasing port %s — stopping listener(s): %s", port, remaining)
        for pid in remaining:
            if not _kill_pid(port, pid) and _process_alive(pid):
                _logger.warning(
                    "Could not stop pid %s (%s) on port %s",
                    pid,
                    _process_image_name(pid) or "unknown",
                    port,
                )

        if _wait_for_port_free(port, skip_pid=skip_pid, timeout=1.5):
            _logger.info("Port %s released", port)
            return

    still = active_listeners()
    if still:
        _logger.error("Port %s still busy after shutdown attempt: %s", port, still)


def stop_api_server(port: Optional[int] = None, timeout: float = 4.0) -> None:
    """Signal uvicorn to exit and ensure *port* is no longer listening."""
    _shutdown_event.set()
    with _server_lock:
        server = _uvicorn_server
    if server is not None:
        try:
            server.should_exit = True
        except Exception as exc:
            _logger.debug("uvicorn should_exit: %s", exc)

    if port is None:
        return

    if _wait_for_port_free(port, skip_pid=os.getpid(), timeout=timeout):
        return

    listeners = [p for p in _pids_listening_on_port(port) if p != os.getpid()]
    if listeners:
        _logger.info("Force-releasing port %s (pids: %s)", port, listeners)
        for pid in listeners:
            _kill_pid(port, pid)
