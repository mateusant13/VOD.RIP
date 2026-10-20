"""A process that lost its listener must die, not linger holding the DB.

Captured 2026-10-20 from this box's dev log, after an accept failure:

    OSError: [WinError 64] The specified network name is no longer available
    ERROR asyncio: Accept failed on a socket
    socket: <asyncio.TransportSocket fd=1992 ... laddr=('127.0.0.1', 7897)>

`uvicorn.run()` never returned, so the process stayed alive at 367 MB with the
archive DB open and nothing listening. The healthy instance then hit
`database is locked` on its first write, its health check failed, and it was
restarted into the same wall - two `run.py` processes, one a zombie.

`_start_listener_watchdog` closes that hole: three consecutive refused
connections after a startup grace period means this process serves nothing, so
it exits and lets the supervisor start a clean one.

No listener is simulated by binding an ephemeral port and closing it: the
operating system will not hand that port to anyone else in the test window.
"""
from __future__ import annotations

import socket
import threading
import time

import run


def _dead_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _live_port() -> tuple[int, socket.socket]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(4)
    return sock.getsockname()[1], sock


def _fast_watchdog(monkeypatch) -> threading.Event:
    exited = threading.Event()

    def _exit(code: int) -> None:  # stands in for os._exit
        exited.set()
        # Stop the watchdog loop: the real os._exit never returns, and a stub
        # that returns would have it strike again forever, racing pytest's
        # capture streams during shutdown.
        raise SystemExit(code)

    monkeypatch.setattr(run, "_WATCHDOG_GRACE_SEC", 0.0)
    monkeypatch.setattr(run, "_WATCHDOG_INTERVAL_SEC", 0.02)
    monkeypatch.setattr(run, "_WATCHDOG_STRIKES", 2)
    monkeypatch.setattr(run.os, "_exit", _exit)
    return exited


def test_it_exits_when_the_listener_disappears(monkeypatch):
    """The zombie case: it answered, then the accept loop died."""
    exited = _fast_watchdog(monkeypatch)
    port, sock = _live_port()

    run._start_listener_watchdog("127.0.0.1", port)
    # Give it a few 20 ms intervals to arm against the live listener.
    time.sleep(0.2)
    assert not exited.is_set(), "a healthy listener must never trigger an exit"
    sock.close()

    assert exited.wait(5.0), (
        "the watchdog did not exit a process that lost its listener - that is "
        "the zombie that wedges the archive DB"
    )


def test_it_never_fires_before_the_listener_has_ever_answered(monkeypatch):
    """A slow cold boot must not be mistaken for a dead process.

    The watchdog arms on the first successful probe on purpose: a boot on this
    box (archive open + WAL checkpoint) outlives any fixed grace period, and
    counting boot seconds as strikes would kill a healthy process and produce a
    restart loop that looks exactly like the bug this guard prevents.
    """
    exited = _fast_watchdog(monkeypatch)

    run._start_listener_watchdog("127.0.0.1", _dead_port())

    assert not exited.wait(0.5)


def test_it_stays_alive_while_the_listener_answers(monkeypatch):
    exited = _fast_watchdog(monkeypatch)
    port, sock = _live_port()
    try:
        run._start_listener_watchdog("127.0.0.1", port)
        assert not exited.wait(0.5), "a healthy listener must never trigger an exit"
    finally:
        sock.close()


def test_the_kill_switch_is_honoured(monkeypatch):
    exited = _fast_watchdog(monkeypatch)
    monkeypatch.setenv("VODRIP_NO_LISTENER_WATCHDOG", "1")

    run._start_listener_watchdog("127.0.0.1", _dead_port())

    assert not exited.wait(0.5)
