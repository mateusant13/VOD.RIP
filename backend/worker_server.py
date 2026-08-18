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


# --- resource watchdog (RAM cap enforcement) --------------------------------
# Monitors the child archive_transcribe process RSS every 60s. If RSS
# exceeds the cap for 3 consecutive checks, kill the child with taskkill.
# The cap is read from the same env knob the child uses (VODRIP_TRANSCRIBE_RSS_CAP_MB).
_WATCHDOG_INTERVAL_S = 60.0
_WATCHDOG_KILL_THRESHOLD = 3  # consecutive over-cap checks before kill
_RSS_CAP_ENV = "VODRIP_TRANSCRIBE_RSS_CAP_MB"


def _rss_cap_bytes() -> int:
    """Hard RSS ceiling (bytes) — mirrors archive_transcribe._rss_cap_bytes."""
    env_val = os.environ.get(_RSS_CAP_ENV, "").strip()
    if env_val:
        try:
            return int(float(env_val)) * 1024 * 1024
        except (ValueError, OverflowError):
            pass
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys * 0.4)
        else:
            total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
            return int(total * 0.4)
    except Exception:
        pass
    return 0


def _process_rss_bytes(pid: int) -> int:
    """RSS of an arbitrary process by PID (bytes, 0 = unknown).

    Windows: ``psapi.GetProcessMemoryInfo`` on an opened handle (accurate,
    fixed cost, locale-independent). Falls back to ``tasklist`` only if the
    handle open / probe fails (e.g. elevated child). POSIX: ``/proc/[pid]/statm``.
    """
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, int(pid))
            if h:
                try:
                    class _PMC(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS_EX
                        _fields_ = [  # noqa: RUF012
                            ("cb", wintypes.DWORD),
                            ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t),
                            ("PrivateUsage", ctypes.c_size_t),
                        ]

                    pmc = _PMC()
                    pmc.cb = ctypes.sizeof(_PMC)
                    fn = ctypes.windll.psapi.GetProcessMemoryInfo
                    fn.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
                    fn.restype = wintypes.BOOL
                    if fn(h, ctypes.byref(pmc), pmc.cb):
                        return int(max(pmc.WorkingSetSize, pmc.PrivateUsage))
                finally:
                    ctypes.windll.kernel32.CloseHandle(h)
            import subprocess as _sp

            out = _sp.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                text=True,
                timeout=5,
                stderr=_sp.DEVNULL,
            )
            for line in out.strip().splitlines():
                parts = line.split(",")
                if len(parts) >= 5:
                    mem_str = parts[4].strip().strip('"')
                    num_str = mem_str[:-1].replace(".", "").replace(",", "")
                    if mem_str.endswith("K"):
                        return int(num_str) * 1024
                    elif mem_str.endswith("M"):
                        return int(float(num_str)) * 1024 * 1024
                    elif mem_str.endswith("G"):
                        return int(float(num_str)) * 1024 * 1024 * 1024
        else:
            with open(f"/proc/{pid}/statm") as f:
                pages = int(f.read().split()[1])
                return pages * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        pass
    return 0

def _resource_watchdog(
    logf, proc: subprocess.Popen, stop_event: threading.Event
) -> None:
    """Daemon thread: poll child RSS every 60s; kill after 3 consecutive over-cap."""
    cap = _rss_cap_bytes()
    if cap <= 0:
        return  # no cap configured — watchdog is a no-op
    consecutive_over = 0
    while not stop_event.wait(_WATCHDOG_INTERVAL_S):
        if proc.poll() is not None:
            return  # child already exited
        rss = _process_rss_bytes(proc.pid)
        if rss <= 0:
            consecutive_over = 0  # can't measure — reset counter
            continue
        if rss > cap:
            consecutive_over += 1
            _log(logf, f"watchdog: child RSS {rss / 1024**2:.0f} MB exceeds cap "
                 f"{cap / 1024**2:.0f} MB ({consecutive_over}/{_WATCHDOG_KILL_THRESHOLD})")
            if consecutive_over >= _WATCHDOG_KILL_THRESHOLD:
                _log(logf, f"watchdog: killing child pid {proc.pid} — RSS over cap "
                     f"for {_WATCHDOG_KILL_THRESHOLD} consecutive checks")
                try:
                    if os.name == "nt":
                        os.system(f"taskkill /PID {proc.pid} /F /T")
                    else:
                        os.kill(proc.pid, 9)
                except Exception:
                    _log(logf, "watchdog: kill failed")
                return
        else:
            consecutive_over = 0  # back under cap — reset counter


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

            # Resource watchdog: monitor child RSS and kill if over cap
            # for 3 consecutive 60s checks.
            watchdog_stop = threading.Event()
            watchdog = threading.Thread(
                target=_resource_watchdog,
                args=(logf, proc, watchdog_stop),
                daemon=True,
                name="resource-watchdog",
            )
            watchdog.start()

            try:
                rc = proc.wait()
            except KeyboardInterrupt:
                watchdog_stop.set()
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

            watchdog_stop.set()
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
