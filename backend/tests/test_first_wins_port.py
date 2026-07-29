"""Self-check: first-wins single-instance — a second run.py must NOT kill a
healthy API on the port, it must exit 0 instead.

The bug: hub/watchdog auto-restarts of `python backend/run.py` found :7897
busy (owned by a dev-all session) and POSTed /api/exit via release_api_port,
murdering the healthy dev API seconds after it bound. Now run.py probes
/api/info first and yields when a healthy VOD.RIP API owns the port.

Run against a LIVE dev API:  python backend/tests/test_first_wins_port.py
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.server_lifecycle import vodrip_api_healthy  # noqa: E402

PORT = 7897
BACKEND = os.path.join(os.path.dirname(__file__), "..")


def main() -> int:
    # 1. Live API answers the identity probe; dead port fails fast.
    t = time.monotonic()
    assert vodrip_api_healthy(PORT), f"no healthy VOD.RIP API on :{PORT} — start one first"
    print(f"OK: vodrip_api_healthy({PORT}) True ({time.monotonic() - t:.2f}s)")

    t = time.monotonic()
    assert not vodrip_api_healthy(7991), "dead port reported healthy"
    elapsed = time.monotonic() - t
    assert elapsed < 2.0, f"dead-port probe too slow: {elapsed:.2f}s"
    print(f"OK: vodrip_api_healthy(7991) False fast ({elapsed:.2f}s)")

    # 2. A second run.py against the healthy API must exit 0, not kill it.
    proc = subprocess.run(
        [sys.executable, "run.py"],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"second instance exit={proc.returncode}\n{out}"
    assert "already running" in out, f"missing first-wins message:\n{out}"
    print(f"OK: second run.py exited 0 ({proc.returncode}) with first-wins message")

    # 3. The original API survived.
    assert vodrip_api_healthy(PORT), "API was killed by the second instance"
    print(f"OK: API on :{PORT} still alive after second instance ran")

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
