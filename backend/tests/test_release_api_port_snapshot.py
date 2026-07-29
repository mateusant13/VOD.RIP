"""Self-check: release_api_port must NOT kill listeners when skip_pid matches.

The previous bug: a stray release_api_port(7897) call from any process could
POST /api/exit to whichever API happened to be on the port — including the
freshly-spawned dev API we just started. With the snapshot fix, callers
should pass skip_pid=os.getpid() so the API refuses to kill itself.
"""
from __future__ import annotations

import subprocess as _sp
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.server_lifecycle import _port_has_listener, release_api_port  # noqa: E402

PORT = 7897
_NO_WINDOW = 0x08000000


def _listening_pid(port: int) -> int | None:
    """Return PID listening on *port* via netstat, or None if no listener."""
    for _ in range(5):
        r = _sp.run(
            ["netstat", "-ano"],
            capture_output=True, text=False, timeout=5, creationflags=_NO_WINDOW,
        )
        if r.stdout:
            text = r.stdout.decode("cp1252", errors="ignore")
            for line in text.splitlines():
                if "LISTENING" in line.upper() and f":{port} " in line:
                    cols = line.split()
                    pid = cols[-1]
                    if pid.isdigit():
                        return int(pid)
        time.sleep(0.3)
    return None


def main() -> int:
    if not _port_has_listener(PORT):
        print(f"FAIL: no API listening on :{PORT} - start the dev API first")
        return 1

    import requests as _req
    try:
        info = _req.get(f"http://127.0.0.1:{PORT}/api/info", timeout=2).json()
        assert info.get("name", "").startswith("VOD.RIP"), f"unexpected API: {info}"
    except Exception as exc:
        print(f"FAIL: cannot reach API on :{PORT}: {exc}")
        return 1
    print(f"OK: VOD.RIP API on :{PORT} before release_api_port call")

    pre_pid = _listening_pid(PORT)
    assert pre_pid is not None, f"no listener for :{PORT}"
    print(f"OK: pre-call listener PID={pre_pid}")

    # Simulate the EXE startup call: skip_pid=our_own_pid.
    # release_api_port must NOT kill the listener at skip_pid.
    try:
        release_api_port(PORT, skip_pid=pre_pid, timeout=2)
    except Exception as exc:
        print(f"FAIL: release_api_port raised: {exc}")
        return 1

    time.sleep(0.5)
    try:
        post = _req.get(f"http://127.0.0.1:{PORT}/api/info", timeout=2).json()
        assert post.get("name", "").startswith("VOD.RIP"), f"API was killed: {post}"
        print(f"OK: API on :{PORT} still alive after release_api_port(skip_pid={pre_pid})")
    except Exception as exc:
        print(f"FAIL: API was killed after release_api_port: {exc}")
        return 1

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())