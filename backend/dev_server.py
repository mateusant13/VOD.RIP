"""dev_server.py — supervised, single-instance dev API server.

One command for every context that needs the API up: the repo dev loop
(launch.bat / npm), worktree agents, and supervised background jobs.

Why this exists (the failure it fixes):
  - Two launchers bound :7897 at once. On Windows SO_REUSEADDR lets the
    second bind steal the port and the first instance dies with exit 1 and
    NO traceback (observed with a guarded-less ``python app.py`` instance).
  - A supervised bare uvicorn lost the crash traceback entirely — nothing
    to diagnose.

Behavior:
  1. First-wins guard — a healthy VOD.RIP API on the port wins; we print
     and exit 0. Never double-binds (guard_api_port, same one run.py/app.py
     use).
  2. Child supervision — run.py runs as a child; any crash (even a
     C-level hard exit) restarts it with backoff 5s/10s/20s, bounded to 3
     consecutive failures so a broken boot stops looping instead of masking
     the error.
  3. Output tee — child output goes to the console AND backend/logs/
     server-<port>.log, so the traceback survives the crash.
  4. ``--port`` for worktrees — each worktree runs its own instance on a
     free port with the same guard + log + supervision.
  5. Graceful stop — Ctrl+C sends CTRL_BREAK_EVENT so the child runs its
     shutdown hooks (download/ffmpeg cleanup), then hard-kills if stuck.

Stdlib only. The child (run.py) applies the same guard again plus its
TIME_WAIT bind retry — belt and suspenders.
"""
from __future__ import annotations

import argparse
import locale
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
LOG_DIR = BACKEND_DIR / "logs"
BACKOFF_SECONDS = (5, 10, 20)
MAX_CONSECUTIVE_CRASHES = len(BACKOFF_SECONDS)


def _log(logf, msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        logf.write(line + "\n")
    except OSError:
        pass


def _pump(stream, logf) -> None:
    """Drain the child's stdout until EOF (daemon thread per spawn)."""
    for line in iter(stream.readline, ""):
        _log(logf, line.rstrip("\r\n"))
    stream.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="VOD.RIP supervised dev API server")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "7897")))
    args = ap.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"server-{args.port}.log"
    logf = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)

    # First-wins pre-check BEFORE spawning the child (fast path; the child
    # re-checks inside run.py — a racing healthy instance can still win).
    from services.server_lifecycle import vodrip_api_healthy  # local: keep boot light

    if vodrip_api_healthy(args.port):
        _log(logf, f"VOD.RIP API already running on :{args.port} — nothing to do.")
        logf.close()
        return 0

    py = sys.executable
    cmd = [py, str(BACKEND_DIR / "run.py"), "--port", str(args.port)]
    _log(logf, f"VOD.RIP dev server supervisor starting (url http://localhost:{args.port}, log {log_path})")

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    enc = locale.getpreferredencoding(False) or "utf-8"
    crashes = 0
    try:
        while True:
            _log(logf, f"spawn: {' '.join(cmd)}")
            proc = subprocess.Popen(
                cmd,
                cwd=str(BACKEND_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding=enc,
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
            threading.Thread(target=_pump, args=(proc.stdout, logf), daemon=True).start()
            _log(logf, f"child pid {proc.pid}")

            try:
                rc = proc.wait()
            except KeyboardInterrupt:
                _log(logf, "Ctrl+C received — stopping child gracefully")
                if os.name == "nt":
                    try:
                        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
                    except (OSError, ValueError):
                        proc.terminate()
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                _log(logf, "server stopped")
                return 0

            if rc == 0:
                _log(logf, "server exited cleanly (rc=0) — not restarting")
                return 0

            crashes += 1
            _log(logf, f"server exited rc={rc} (consecutive crash #{crashes}/{MAX_CONSECUTIVE_CRASHES})")
            if crashes >= MAX_CONSECUTIVE_CRASHES:
                _log(logf, f"giving up after {crashes} consecutive crashes — inspect {log_path}")
                return 1
            wait = BACKOFF_SECONDS[crashes - 1]
            _log(logf, f"restarting in {wait}s...")
            time.sleep(wait)
    finally:
        logf.close()


if __name__ == "__main__":
    sys.exit(main())
