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


def _kill_pids(pids: list[int], *, skip_pid: Optional[int] = None) -> None:
    for pid in pids:
        if skip_pid is not None and pid == skip_pid:
            continue
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True,
                    timeout=3,
                    creationflags=_NO_WINDOW,
                )
            else:
                os.kill(pid, 9)
        except Exception as exc:
            _logger.debug("kill pid %s: %s", pid, exc)


def release_api_port(port: int, *, skip_pid: Optional[int] = None, timeout: float = 6.0) -> None:
    """Kill any process listening on *port* so a new API instance can bind."""
    listeners = [p for p in _pids_listening_on_port(port) if skip_pid is None or p != skip_pid]
    if not listeners:
        return
    _logger.info("Releasing port %s — stopping listener(s): %s", port, listeners)
    _kill_pids(listeners, skip_pid=skip_pid)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = [p for p in _pids_listening_on_port(port) if skip_pid is None or p != skip_pid]
        if not remaining:
            return
        time.sleep(0.15)

    remaining = [p for p in _pids_listening_on_port(port) if skip_pid is None or p != skip_pid]
    if remaining:
        _logger.warning("Port %s still busy — force kill: %s", port, remaining)
        _kill_pids(remaining, skip_pid=skip_pid)


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

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        listeners = _pids_listening_on_port(port)
        if not listeners:
            return
        time.sleep(0.15)

    listeners = _pids_listening_on_port(port)
    if listeners:
        _logger.info("Force-releasing port %s (pids: %s)", port, listeners)
        _kill_pids(listeners, skip_pid=os.getpid())
