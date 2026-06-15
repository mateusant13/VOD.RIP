"""Detect / focus a running VOD.RIP instance and enforce process-level singleton.

Two layers of single-instance protection:

1. **File lock** (hard, process-level) — held for the lifetime of the
   first process. A second ``VOD-RIP.exe`` cannot acquire it, so it
   exits immediately without ever starting uvicorn or the WebView
   window. Works during the slow cold start of the first process when
   the HTTP API isn't yet reachable.
2. **HTTP poll** (soft, network-level) — once the first instance's API
   is up, a second launch defers to it and asks it to focus the window.
   Backs up the file lock for the focus-the-existing-window UX.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

APP_NAME_PREFIX = "VOD.RIP"

# How long a second instance waits for the first one's HTTP API to
# become reachable before giving up. Bounded so the user never sees a
# hanging splash if the first process is wedged.
_ACTIVATE_POLL_TOTAL_SEC = 6.0
_ACTIVATE_POLL_STEP_SEC = 0.4


def is_vodrip_api_name(name: str) -> bool:
    return (name or "").startswith(APP_NAME_PREFIX)


def _api_base(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def is_vodrip_running(port: int) -> bool:
    try:
        import requests

        resp = requests.get(f"{_api_base(port)}/api/info", timeout=1.5)
        if resp.status_code != 200:
            return False
        return is_vodrip_api_name(resp.json().get("name", ""))
    except Exception:
        return False


def _lock_path() -> Path:
    """Return the path to the singleton lock file (per-user)."""
    from services.settings import _get_appdata_dir

    return _get_appdata_dir() / "vodrip.singleton.lock"


def acquire_process_lock() -> object | None:
    """Try to acquire the per-user singleton lock.

    Returns a token object on success (the caller must keep it alive for
    the lifetime of the process) or ``None`` if another VOD.RIP is
    already running for this user.

    Windows: ``msvcrt.locking`` over an open file. POSIX: ``fcntl.flock``.
    Falls back to a "no protection" sentinel if neither is available.
    """
    path = _lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Open in append mode so the OS keeps the same inode alive
        # across acquire/release cycles; otherwise Windows can reuse
        # the same name and the lock would be meaningless.
        fh = open(path, "a+b")
    except OSError as exc:
        logger.debug("Could not open singleton lock file %s: %s", path, exc)
        return _NoOpLock()

    if os.name == "nt":
        try:
            import msvcrt

            # LK_NBLCK = 2 — non-blocking exclusive lock. Returns
            # OSError (PermissionError) if another process already holds it.
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return _FileLock(fh)
        except (ImportError, OSError) as exc:
            logger.debug("msvcrt.locking failed: %s", exc)
            try:
                fh.close()
            except OSError:
                pass
            return None

    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _FileLock(fh)
    except (ImportError, OSError) as exc:
        logger.debug("fcntl.flock failed: %s", exc)
        try:
            fh.close()
        except OSError:
            pass
        return None


class _FileLock:
    """Holds the singleton file handle open for the process lifetime."""

    __slots__ = ("_fh",)

    def __init__(self, fh) -> None:  # type: ignore[no-untyped-def]
        self._fh = fh

    def release(self) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                self._fh.close()
            except OSError:
                pass


class _NoOpLock:
    """Returned only if the lock subsystem is unavailable — never blocks."""

    __slots__ = ()

    def release(self) -> None:
        return None


def try_activate_existing(port: int) -> bool:
    """Ask a running VOD.RIP instance to focus its window.

    Polls for up to ``_ACTIVATE_POLL_TOTAL_SEC`` to absorb a slow cold
    start of the first instance — otherwise a quick second click during
    the first boot would launch a duplicate instead of deferring.
    """
    deadline = time.monotonic() + _ACTIVATE_POLL_TOTAL_SEC
    saw_first = False
    while time.monotonic() < deadline:
        if is_vodrip_running(port):
            saw_first = True
            try:
                import requests

                resp = requests.post(f"{_api_base(port)}/api/focus", timeout=2.5)
                if resp.status_code == 200:
                    logger.info("Focused existing VOD.RIP instance on port %s", port)
                    return True
            except Exception as exc:
                logger.debug("Focus existing instance failed: %s", exc)
            # Got a 200-shaped response but focus didn't take — stop polling.
            break
        if saw_first:
            # Server was up and is now down (unlikely) — give up.
            break
        time.sleep(_ACTIVATE_POLL_STEP_SEC)
    return False
