"""worker_server.py — supervised, detached archive worker.

One command for the VOD.RIP background worker: drains the archive_jobs
queue (transcribe/events/chat) until it is empty, survives app close and
crashes. The app's lifespan spawns this detached at boot when jobs are
pending; a human or CI can also run it directly.

Behavior (dev_server.py supervision pattern, no port, no HTTP):
  1. First-wins guard — a live worker heartbeat (in-process OR another
     detached worker) means the queue already has a consumer; we print and
     exit 0. Never double-loads the whisper model.
  2. Child supervision — `python -m services.archive_transcribe --once`
     runs as a child; any crash (even a C-level hard exit) restarts it
     with backoff 5s/10s/20s, bounded to 3 consecutive failures so a
     broken worker stops looping instead of masking the error.
  3. Exit contract — child rc 0 (queue drained) exits 0 quietly; giving
     up after 3 crashes exits 1 pointing at the log.
  4. Output tee — child output goes to the console AND backend/logs/
     worker.log, so a crash traceback survives.
  5. Graceful stop — Ctrl+C sends CTRL_BREAK_EVENT so the child exits
     (the queue is crash-safe: an interrupted job is reclaimed by the next
     worker after its stale window), then hard-kills if stuck.

Stdlib only.
"""
from __future__ import annotations

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

from rotating_log import open_rotating  # noqa: E402  (DISK-06: 5 MB x 3 rotation)


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


def _singleton_mutex_held() -> bool:
    """True when another worker_server holds the machine-session mutex.

    The heartbeat guard is DB-scoped (VODRIP_ARCHIVE_DB) and tests run on
    scratch DBs, so supervisors from different trees/processes all win it
    and pile up (observed 30+ daemons burning cores). A named mutex is
    session-scoped, survives DB isolation, and the kernel releases it on
    exit/crash. POSIX keeps the heartbeat guard only."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [
            wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW(None, False, "Local\\VOD.RIP.worker-server")
        return kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return False


def main() -> int:
    LOG_DIR.mkdir(exist_ok=True)
    if _singleton_mutex_held():
        logf = open_rotating(LOG_DIR / "worker.log")
        try:
            _log(logf, "another worker supervisor already owns the mutex — exiting")
        finally:
            logf.close()
        return 0
    log_path = LOG_DIR / "worker.log"
    logf = open_rotating(log_path)
    _log(logf, f"VOD.RIP archive worker supervisor starting (log {log_path})")

    # First-wins guard BEFORE spawning: a fresh worker heartbeat means the
    # queue already has a live consumer (in-process or another detached
    # worker) — spawning a second would double-load the whisper model.
    # The child re-checks inside --once (belt and suspenders).
    from services import archive_db  # local: keep boot light

    if archive_db.worker_live(age_s=45):
        _log(logf, "archive worker already running — nothing to do.")
        logf.close()
        return 0

    py = sys.executable
    if getattr(sys, "frozen", False):
        # Frozen EXE cannot run `python -m`; the launcher dispatches
        # --transcribe-once to services.archive_transcribe.run_worker with
        # the same once/poll contract as the dev child below.
        cmd = [py, "--transcribe-once"]
    else:
        cmd = [py, "-m", "services.archive_transcribe", "--once", "--poll-interval", "2"]

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
                _log(logf, "worker stopped")
                return 0

            if rc == 0:
                _log(logf, "worker exited cleanly (queue drained, rc=0) — not restarting")
                return 0

            crashes += 1
            _log(logf, f"worker exited rc={rc} (consecutive crash #{crashes}/{MAX_CONSECUTIVE_CRASHES})")
            if crashes >= MAX_CONSECUTIVE_CRASHES:
                _log(logf, f"giving up after {crashes} consecutive crashes — inspect {log_path}")
                try:
                    from services import archive_db

                    # Cooldown marker: the background daemon skips respawn
                    # for 15 min so a crash-loop worker doesn't spawn a
                    # replacement every minute (the 'python keeps coming
                    # back at 33% CPU' treadmill).
                    archive_db.worker_heartbeat("worker-gave-up")
                except Exception:
                    pass
                return 1
            wait = BACKOFF_SECONDS[crashes - 1]
            _log(logf, f"restarting in {wait}s...")
            time.sleep(wait)
    finally:
        logf.close()


if __name__ == "__main__":
    sys.exit(main())
