"""Uvicorn / API port lifecycle — start, stop, and release port 7897 on app exit."""

from __future__ import annotations

import logging
import os
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


def _pids_listening_on_port(port: int) -> list[int]:
    pids: list[int] = []
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=_NO_WINDOW,
            )
            for line in result.stdout.splitlines():
                if "LISTENING" not in line:
                    continue
                cols = line.split()
                if len(cols) < 5:
                    continue
                local_addr = cols[1]
                if not local_addr.endswith(f":{port}"):
                    continue
                pid = cols[-1]
                if pid.isdigit():
                    pids.append(int(pid))
        except Exception as exc:
            _logger.debug("netstat for port %s: %s", port, exc)
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


def _kill_pid(port: int, pid: int) -> None:
    if pid == os.getpid():
        return
    image = _process_image_name(pid) or "unknown"
    try:
        if os.name == "nt":
            _logger.info("Stopping port %s listener pid %s (%s)", port, pid, image)
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                creationflags=_NO_WINDOW,
            )
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


def release_api_port(port: int, *, skip_pid: Optional[int] = None, timeout: float = 6.0) -> None:
    """Free *port* for a new VOD.RIP / dev API instance (graceful first, then safe kill)."""
    listeners = [p for p in _pids_listening_on_port(port) if skip_pid is None or p != skip_pid]
    if not listeners:
        return

    _logger.info("Port %s busy — asking existing VOD.RIP API to exit", port)
    if _request_graceful_shutdown(port):
        if _wait_for_port_free(port, skip_pid=skip_pid, timeout=min(4.0, timeout)):
            _logger.info("Port %s released after graceful shutdown", port)
            return

    remaining = [p for p in _pids_listening_on_port(port) if skip_pid is None or p != skip_pid]
    if not remaining:
        return

    _logger.info("Releasing port %s — stopping listener(s): %s", port, remaining)
    for pid in remaining:
        _kill_pid(port, pid)

    if not _wait_for_port_free(port, skip_pid=skip_pid, timeout=timeout):
        still = [p for p in _pids_listening_on_port(port) if skip_pid is None or p != skip_pid]
        if still:
            _logger.warning("Port %s still busy after shutdown attempt: %s", port, still)


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
