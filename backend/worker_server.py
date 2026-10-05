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
from typing import Optional

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
            # restype MUST be HANDLE: ctypes defaults it to c_int, which
            # truncates a 64-bit handle to 32 bits and silently probes
            # garbage (or a reused handle) instead of the real process.
            _open = ctypes.windll.kernel32.OpenProcess
            _open.restype = wintypes.HANDLE
            _open.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            h = _open(PROCESS_QUERY_LIMITED, False, int(pid))
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


# --- stall watchdog (GIL-held decode deadlock) ----------------------------
# WHY EXTERNAL, AND WHY HERE
# The parakeet decode can hard-deadlock inside the native sherpa/onnxruntime
# call: all chunk lanes stuck in decode_stream on the shared per-device
# recognizer, MainThread parked in _wait_for_tstate_lock on an unbounded
# fut.result(). The wedged call HOLDS THE GIL, so nothing inside the child
# can observe, time out, or recover from it — an in-process heartbeat thread
# stops ticking, no timeout fires, and KeyboardInterrupt is unreachable.
# Measured on the wedged child: 0.000 s of CPU across 12 s sampling windows
# (blocked, not spinning) while its job row sat 'running' with a heartbeat
# frozen for hours. The ONLY thing that can bound it lives in a different
# process — this supervisor, which already parents the child and already runs
# the RSS watchdog above.
#
# DETECTING A STALL, NOT A SLOW VIDEO
# The signals are OBSERVABLE PROGRESS, never "this job is taking long":
#   * 'progress marks' — timestamps the child itself writes: every completed
#     60 s chunk stamps archive_jobs.heartbeat (throttled to one UPDATE per
#     2 s), every yt-dlp byte-progress event does the same, plus the
#     worker's own worker_heartbeats row. A 13-hour VOD is fine because its
#     marks keep moving; a wedge moves none of them, because writing them
#     needs the very GIL the wedge holds.
#   * child CPU time — consumed while decoding, committing, or unpacking.
#
# A stall needs BOTH marks frozen for the full bound AND the child burning no
# CPU across that same window. The conjunction is the whole trick: CPU
# consumption counts AS progress, so the clock restarts whenever the child is
# busy. A throttled worker is slower, never motionless — even at the worst
# wall multiplier seen in the field (13.6x) it still accumulates ~1.5 s of
# CPU per poll window, 6x the floor below — so throttling can never trip it.
# Only a genuinely dead-stuck process reaches zero.
#
# THE BOUND (derived, not round)
# Measured 2026-10-04 on this box through the production _load_parakeet path
# with real pt-BR/en speech, chunks at the production _MAX_CHUNK_SEC = 60,
# single lane num_threads=2, at the then-current wall_multiplier of 3.12:
#     60 s chunk wall: min 8.906  median 9.493  max 10.484  (5.72x realtime)
# The healthy inter-heartbeat gap is one chunk, so the worst healthy gap at
# the worst throttle is 10.484 * (13.6/3.12) = 45.7 s. Times a 3x safety
# factor for descheduling, segment-dense commits and contention with other
# projects: STALL_BOUND_S below. It also sits deliberately ABOVE the
# in-process download stall watchdog (STALL_WATCHDOG_SEC = 90 s), so a
# genuinely stalled download always gets first refusal and fails the job
# cleanly; this watchdog firing during a fetch means the in-process one could
# not act either, i.e. the child is wedged.
_MEASURED_CHUNK_WALL_S = 10.484       # real speech, 60 s chunk, worst of 6
_BASELINE_WALL_MULTIPLIER = 3.12      # watcher multiplier at measurement time
_WORST_WALL_MULTIPLIER = 13.6         # worst observed under heavy throttle
_STALL_SAFETY_FACTOR = 3.0
STALL_BOUND_S = round(
    _MEASURED_CHUNK_WALL_S
    * (_WORST_WALL_MULTIPLIER / _BASELINE_WALL_MULTIPLIER)
    * _STALL_SAFETY_FACTOR,
    1,
)  # == 137.1 s
_STALL_POLL_S = 15.0
# END-TO-END bound from "decode wedges" to "job is back in the retry queue".
# Every term is a constant, so the recovery is a hard bound, not a nominal
# one: one poll interval to notice + the bound itself + the kill/verify
# budget (30 s taskkill wait + 15 s TerminateProcess wait) + the retry
# UPDATE. Under the default bound that is <= 137.1 + 15 + 45 + ~1 ~= 198 s.
STALL_RECOVERY_BOUND_S = (
    _STALL_POLL_S + STALL_BOUND_S + 30.0 + 15.0 + 1.0
)
# CPU seconds that must be exceeded in one poll window to count as "working".
# The measured wedge is exactly 0.000; a 13.6x-throttled worker clears ~1.5.
_STALL_CPU_FLOOR_S = 0.25
_STILL_ACTIVE = 259  # Windows: GetExitCodeProcess value for a live process
_STALL_BOUND_ENV = "VODRIP_ASR_STALL_BOUND_S"

# Set by the stall watchdog once it has killed a wedged child AND returned
# its job to the retry path. main() reads it so a stall we diagnosed and
# recovered from does not count toward MAX_CONSECUTIVE_CRASHES: the child
# exiting non-zero is the RECOVERY working, not an unexplained crash, and
# counting it would park the whole queue in the 15 min give-up cooldown
# after three wedged videos. Undiagnosed crashes still count as before.
_STALL_RECOVERED = threading.Event()


def _stall_bound_s() -> float:
    """The derived bound, overridable for the field and for tests.

    The default is STALL_BOUND_S (137.1 s). An operator on a much slower or
    much faster box can retune it without a code change; the derivation in
    the comment above says what a sane value looks like."""
    raw = os.environ.get(_STALL_BOUND_ENV, "").strip()
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except (ValueError, OverflowError):
            pass
    return STALL_BOUND_S


def _process_cpu_seconds(pid: int) -> Optional[float]:
    """Total CPU seconds (user+kernel) consumed by `pid`, or None.

    Windows: GetProcessTimes on an opened handle. POSIX: utime+stime from
    /proc/[pid]/stat. A dead or unopenable pid is None — the caller must
    treat a missing reading as 'no evidence', never as 'wedged'."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class _FT(ctypes.Structure):
                _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

            _open = ctypes.windll.kernel32.OpenProcess
            _open.restype = wintypes.HANDLE
            _open.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            # PROCESS_QUERY_LIMITED | PROCESS_VM_READ
            h = _open(0x1000 | 0x0010, False, int(pid))
            if not h:
                return None
            try:
                c, e, k, u = _FT(), _FT(), _FT(), _FT()
                _gpt = ctypes.windll.kernel32.GetProcessTimes
                _gpt.restype = wintypes.BOOL
                _gpt.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(_FT), ctypes.POINTER(_FT),
                    ctypes.POINTER(_FT), ctypes.POINTER(_FT),
                ]
                if not _gpt(h, ctypes.byref(c), ctypes.byref(e),
                            ctypes.byref(k), ctypes.byref(u)):
                    return None
                ticks = ((k.hi << 32) | k.lo) + ((u.hi << 32) | u.lo)
                return ticks / 1e7  # 100 ns FILETIME units -> seconds
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        with open(f"/proc/{pid}/stat", "rb") as f:
            parts = f.read().rsplit(b")", 1)[1].split()
        return (int(parts[11]) + int(parts[12])) / os.sysconf("SC_CLK_TCK")
    except Exception:
        return None


def _process_exit_code(pid: int) -> Optional[int]:
    """GetExitCodeProcess(pid), or None if the pid cannot be opened.

    OpenProcess SUCCEEDS on a terminated-but-unreaped pid, so a successful
    open proves nothing about liveness — only the exit code does. A live
    process reports STILL_ACTIVE (259); anything else has exited."""
    try:
        if os.name != "nt":
            return None
        import ctypes
        from ctypes import wintypes

        _open = ctypes.windll.kernel32.OpenProcess
        _open.restype = wintypes.HANDLE
        _open.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        h = _open(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED
        if not h:
            return None
        try:
            code = wintypes.DWORD(0)
            _gecp = ctypes.windll.kernel32.GetExitCodeProcess
            _gecp.restype = wintypes.BOOL
            _gecp.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            if not _gecp(h, ctypes.byref(code)):
                return None
            return int(code.value)
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return None


def _process_is_alive(pid: int) -> bool:
    """True only if `pid` is a RUNNING process.

    Decided by exit code against STILL_ACTIVE — never by OpenProcess success
    (a terminated-but-unreaped pid still opens) and never by tasklist (a
    previous lane's kill path reported killed processes as alive because it
    trusted one of those). On POSIX, signal 0 is the equivalent probe."""
    rc = _process_exit_code(pid)
    if rc is not None:
        return rc == _STILL_ACTIVE
    if os.name == "nt":
        return False  # could not prove liveness -> do not claim alive
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except Exception:
        return False


def _progress_marks() -> Optional[str]:
    """A string that changes iff the child made observable progress.

    Reads only the child's own bookkeeping: the heartbeat of every 'running'
    job row (stamped per completed chunk and per yt-dlp progress event) plus
    the worker_heartbeats rows. Read-only.

    Returns None when the marks are UNOBSERVABLE (the DB is unreachable), which
    is deliberately distinct from an empty string. An empty string is a real
    reading: the child has no running job rows and no worker heartbeat yet. An
    unobservable reading is the absence of a reading, and the caller must not
    treat two of those in a row as "the marks are frozen" - that would let a
    database hiccup read as a stalled worker, kill a healthy child, and file
    the kill under a decode deadlock it never had.
    """
    try:
        from services import archive_db

        jobs = archive_db.query(
            "SELECT id, COALESCE(heartbeat, updated_at) AS hb FROM archive_jobs "
            "WHERE status = 'running' ORDER BY id"
        )
        beats = archive_db.query(
            "SELECT tag, at FROM worker_heartbeats ORDER BY tag"
        )
    except Exception:
        return None
    return "|".join(f"{r['id']}={r['hb']}" for r in jobs) + "#" + "|".join(
        f"{r['tag']}={r['at']}" for r in beats
    )


def _stall_state(
    holder: dict,
    now: float,
    *,
    marks: str,
    cpu_seconds: Optional[float],
    bound_s: float = STALL_BOUND_S,
    cpu_floor_s: float = _STALL_CPU_FLOOR_S,
    active: bool = True,
) -> Optional[str]:
    """Pure stall verdict: None = healthy, else the reason string.

    Clock-injected and process-free, exactly like download_manager's
    _stall_state, so the decision is testable without a database or a
    clock. See the module comment for why a stall requires frozen marks AND
    a motionless child."""
    if not active:
        return None
    if holder.get("error"):
        return holder["error"]  # latched: same verdict until acted on
    if not holder.get("armed"):
        holder["armed"] = True
        holder["last_marks"] = marks
        holder["last_progress_wall"] = now
        holder["cpu_baseline"] = cpu_seconds
        return None
    if marks is None:
        # Unobservable, not frozen. Re-arm the clock so the bound is measured
        # from the first reading we can actually see, and keep the CPU baseline
        # fresh. Without this, N consecutive DB failures look like a stall.
        holder["last_marks"] = None
        holder["last_progress_wall"] = now
        holder["cpu_baseline"] = cpu_seconds
        return None
    if holder["last_marks"] is None:
        # First observable reading after a blind patch: re-arm rather than
        # compare against a value we never had.
        holder["last_marks"] = marks
        holder["last_progress_wall"] = now
        holder["cpu_baseline"] = cpu_seconds
        return None
    if marks != holder["last_marks"]:
        holder["last_marks"] = marks
        holder["last_progress_wall"] = now
        holder["cpu_baseline"] = cpu_seconds
        return None
    used = 0.0
    if cpu_seconds is not None and holder["cpu_baseline"] is not None:
        used = max(0.0, cpu_seconds - float(holder["cpu_baseline"]))
    if used > cpu_floor_s:
        # Burning CPU IS progress: decode, segment commit, ffmpeg unpack.
        # Restart the clock so only a child that is busy AND silent past the
        # bound can ever be killed. This is what keeps a throttled worker
        # safe, and it self-heals a busy-then-wedged child.
        holder["last_progress_wall"] = now
        holder["cpu_baseline"] = cpu_seconds
        return None
    if now - float(holder["last_progress_wall"]) < bound_s:
        return None
    held = now - float(holder["last_progress_wall"])
    # State only what was measured. "No job-progress mark moved for Ns while the
    # child consumed Xs of CPU" is the entire finding. A GIL-held decode is the
    # hypothesis this watchdog was built for, but it is NOT established by this
    # evidence: an unreachable database, a dead socket, or a lock elsewhere are
    # all consistent with the same reading, and they have different fixes. A log
    # that names the cause sends the next reader to the wrong subsystem.
    reason = (
        f"ASR worker wedged: no observable progress for {int(held)}s "
        f"while consuming {used:.2f}s CPU "
        f"(cause not established: a GIL-held decode, an unreachable database, "
        f"and a blocked socket all present this same signature)"
    )
    holder["error"] = reason  # latched here, as download_manager does, so a
    return reason               # caller that only re-polls gets one verdict


def _running_transcribe_jobs() -> list[dict]:
    """The 'running' transcribe rows the child owns (read-only)."""
    try:
        from services import archive_db

        return [
            dict(r)
            for r in archive_db.query(
                "SELECT id, kind, platform, video_id, status, attempts, max_attempts "
                "FROM archive_jobs WHERE status = 'running' AND kind = 'transcribe'"
            )
        ]
    except Exception:
        return []


def _mark_stalled_job_failed(reason: str, logf) -> list[str]:
    """Hand the stall-killed job back to the EXISTING retry path.

    Calls archive_db.update_job(status='failed', ...) — the same helper a
    crashed job already uses — so the row is requeued with a next_retry_at
    backoff and attempts+1, and only lands on 'failed' once max_attempts is
    spent. A job left 'running' with no owner is the one outcome that must
    never happen. The reason string is deliberately free of the terminal
    markers update_job matches on ('FileNotFound', 'DownloadError', 'no HLS
    source', 'ASR unsupported', 'ASR unavailable', ...) so a stall is always
    retried, never parked as terminal."""
    marked: list[str] = []
    for row in _running_transcribe_jobs():
        job_id = row.get("id")
        if not job_id:
            continue
        try:
            from services import archive_db

            archive_db.update_job(job_id, status="failed", error=reason[:400])
            after = archive_db.query(
                "SELECT status, attempts, next_retry_at FROM archive_jobs WHERE id = ?",
                (job_id,),
            )
            state = dict(after[0]) if after else {}
            _log(
                logf,
                f"stall: job {job_id} released -> status={state.get('status')} "
                f"attempts {row.get('attempts')}->{state.get('attempts')} "
                f"next_retry_at={state.get('next_retry_at')}",
            )
            marked.append(job_id)
        except Exception as exc:  # noqa: BLE001 - never mask the kill
            _log(logf, f"stall: could not release job {job_id}: {exc}")
    if not marked:
        _log(logf, "stall: no running transcribe job row to release")
    return marked


def _running_job_heartbeats() -> dict[str, str]:
    """COALESCE(heartbeat, updated_at) per 'running' job — the SCHEDULING
    signal, kept separate from the work marks on purpose (read-only)."""
    try:
        from services import archive_db

        return {
            str(r["id"]): str(r["hb"])
            for r in archive_db.query(
                "SELECT id, COALESCE(heartbeat, updated_at) AS hb "
                "FROM archive_jobs WHERE status = 'running'"
            )
        }
    except Exception:
        return {}


# --- job-liveness reaper: a heartbeat is not work -------------------------
# WHY THIS EXISTS
# update_job stamps archive_jobs.heartbeat on EVERY call, progress or not
# (archive_db.update_job). The claim path re-stamps it too, and the download
# watchdog's bare `update_job(job_id)` touch stamps it every 5 min with no
# work at all. So the heartbeat says "a thread was scheduled", never "a
# result was produced". _claim_next_job then decides staleness from that
# column alone (COALESCE(heartbeat, updated_at) < cutoff).
#
# The production shape that leaks: a 'chat' row whose executor wedged. Its
# heartbeat still advances — because the reclaim itself re-stamps it — so it
# never ages out, and the reclaim's CAS flips running->running without
# touching attempts or next_retry_at. attempts stays 0 forever, max_attempts
# is never reached, and the row is relaunched every 2 h for as long as the DB
# lives. The observed job was two months old with a same-day heartbeat.
#
# THE PREDICATE
# A 'running' row is released only when ALL of these hold:
#   1. its WORK marks (progress+status, never the heartbeat — see
#      archive_db.running_job_work_marks) have not changed for the whole
#      bound, AND
#   2. the owning worker has burned less than the CPU floor over that same
#      window, AND
#   3. the row's heartbeat ADVANCED while the work marks were frozen — i.e.
#      something is actively refreshing a job that is producing nothing.
#
# Clause 3 is what makes this catch an immortal row instead of racing the
# existing reclaim for it: the re-stamp that keeps the job alive is the very
# evidence that it is dead. It also means a row nobody touches is left to the
# existing reclaim, which is the correct owner for that case.
#
# WHAT MAKES IT CONSERVATIVE
#   * Both work AND CPU must be flat. This is the same conjunction as
#     _stall_state: burning CPU counts as progress and restarts the clock, so
#     the 7-14% CPU throttle (13.6x wall) cannot trip it — a throttled worker
#     is slower, never motionless, and clears ~1.5 s CPU per window against a
#     0.25 s floor.
#   * The bound is derived from the longest HEALTHY gap between work marks
#     (~300 s: an ASR chunk is 45.7 s worst-case wall, a twitch chat page
#     under 429 backoff is 4-5 min) times a 4.5x safety factor. A working
#     job re-marks orders of magnitude inside that.
#   * Only status='running' rows are ever considered. A queued row is a
#     queue, not a fault: the healthy backlog is untouchable by construction.
#   * The release is a compare-and-set on status='running', so a job that
#     moved between the read and the write is never clobbered.
#   * The release goes through update_job(status='failed'), the EXISTING
#     retry machinery, so the row gets attempts+1 and a next_retry_at
#     backoff and lands terminal at max_attempts. Nothing is left running
#     with no owner, and this reaper cannot invent a second retry vocabulary.
_HEALTHY_MARK_GAP_S = 300.0
_JOB_LIVENESS_SAFETY_FACTOR = 4.5
JOB_LIVENESS_BOUND_S = round(
    _HEALTHY_MARK_GAP_S * _JOB_LIVENESS_SAFETY_FACTOR, 1
)  # == 1350.0 s
JOB_LIVENESS_POLL_S = 60.0
_JOB_LIVENESS_CPU_FLOOR_S = _STALL_CPU_FLOOR_S
_JOB_LIVENESS_BOUND_ENV = "VODRIP_JOB_LIVENESS_BOUND_S"


def _job_liveness_bound_s() -> float:
    """The derived bound, overridable for the field and for tests (same
    contract as _stall_bound_s)."""
    raw = os.environ.get(_JOB_LIVENESS_BOUND_ENV, "").strip()
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except (ValueError, OverflowError):
            pass
    return JOB_LIVENESS_BOUND_S


def _job_liveness_state(
    holder: dict,
    now: float,
    *,
    work: str,
    beat: str,
    cpu_seconds: Optional[float],
    bound_s: float = JOB_LIVENESS_BOUND_S,
    cpu_floor_s: float = _JOB_LIVENESS_CPU_FLOOR_S,
) -> Optional[str]:
    """Pure per-job liveness verdict: None = healthy, else the reason.

    Clock-injected and process-free, exactly like _stall_state, so the
    decision is testable with no database and no clock. `holder` is the
    per-job state dict the reaper keeps between ticks.
    """
    if holder.get("error"):
        return holder["error"]  # latched: same verdict until acted on
    if not holder.get("armed"):
        holder["armed"] = True
        holder["last_work"] = work
        holder["last_beat"] = beat
        holder["work_frozen_since"] = now
        holder["cpu_baseline"] = cpu_seconds
        return None
    if work != holder["last_work"]:
        # The job produced something. This is the ONLY unconditional reset.
        holder["last_work"] = work
        holder["last_beat"] = beat
        holder["work_frozen_since"] = now
        holder["cpu_baseline"] = cpu_seconds
        return None
    used = 0.0
    if cpu_seconds is not None and holder["cpu_baseline"] is not None:
        used = max(0.0, cpu_seconds - float(holder["cpu_baseline"]))
    if used > cpu_floor_s:
        # Burning CPU IS work: a throttled worker looks slow here, never
        # motionless. Restart the clock for every row so the throttle can
        # never be mistaken for a stall.
        holder["work_frozen_since"] = now
        holder["cpu_baseline"] = cpu_seconds
        return None
    if beat == holder["last_beat"]:
        # Silent AND frozen: nobody is even pretending to work on it. Not
        # this reaper's case — the existing stale-window reclaim owns it.
        return None
    if now - float(holder["work_frozen_since"]) < bound_s:
        return None
    # Frozen work past the bound AND a re-stamp landed in that window: a
    # timestamp is being refreshed to keep a job that computes nothing.
    held = now - float(holder["work_frozen_since"])
    reason = (
        f"job alive but not working: no progress for {int(held)}s and "
        f"{used:.2f}s CPU while the heartbeat kept advancing"
    )
    holder["error"] = reason
    return reason


def _reclaim_lifeless_jobs(
    logf, holders: dict, proc, *, bound_s: Optional[float] = None,
) -> list[str]:
    """One reaper tick: release every 'running' row the predicate condemns.

    `holders` is the per-job state dict, persisted across ticks by the
    caller. `proc` is the supervised child, whose CPU time is the
    'is the worker doing anything at all' signal — the same reading the
    stall watchdog takes. Read-only apart from the release itself.
    """
    from services import archive_db

    if bound_s is None:
        bound_s = _job_liveness_bound_s()
    work_marks = archive_db.running_job_work_marks()
    beats = _running_job_heartbeats()
    cpu = _process_cpu_seconds(proc.pid)
    now = time.monotonic()

    # Drop holders for rows that are no longer running so the dict cannot
    # grow without bound across a long-lived supervisor.
    for job_id in list(holders):
        if job_id not in work_marks:
            holders.pop(job_id, None)

    reclaimed: list[str] = []
    for job_id, work in work_marks.items():
        holder = holders.setdefault(job_id, {})
        reason = _job_liveness_state(
            holder, now, work=work, beat=beats.get(job_id, ""),
            cpu_seconds=cpu, bound_s=bound_s,
        )
        if reason is None:
            continue
        holders.pop(job_id, None)
        try:
            # expect_status makes this a compare-and-set: if an executor
            # moved the row since the read above, the update matches nothing
            # and we leave it alone instead of clobbering real work.
            ok = archive_db.update_job(
                job_id, status="failed", error=reason[:400],
                expect_status="running",
            )
        except Exception as exc:  # noqa: BLE001 - never kill the reaper
            _log(logf, f"liveness: could not release job {job_id}: {exc}")
            continue
        if not ok:
            _log(logf, f"liveness: job {job_id} moved on before release — left alone")
            continue
        after = archive_db.query(
            "SELECT status, attempts, next_retry_at FROM archive_jobs WHERE id = ?",
            (job_id,),
        )
        state = dict(after[0]) if after else {}
        _log(
            logf,
            f"liveness: job {job_id} released -> status={state.get('status')} "
            f"attempts={state.get('attempts')} "
            f"next_retry_at={state.get('next_retry_at')}",
        )
        reclaimed.append(job_id)
    return reclaimed


def _job_liveness_reaper(
    logf, proc: subprocess.Popen, stop_event: threading.Event,
    bound_s: Optional[float] = None, poll_s: Optional[float] = None,
) -> None:
    """Daemon thread: release 'running' rows that are alive but not working.

    Runs beside the stall watchdog and never kills anything — the row is
    handed to the existing retry path, and the worker is left to pick it up
    after the backoff. `bound_s` / `poll_s` are injectable only so tests can
    drive the REAL loop at test speed."""
    if bound_s is None:
        bound_s = _job_liveness_bound_s()
    if poll_s is None:
        poll_s = JOB_LIVENESS_POLL_S
    holders: dict = {}
    while not stop_event.wait(poll_s):
        if proc.poll() is not None:
            return  # child exited; nothing left to judge
        try:
            _reclaim_lifeless_jobs(logf, holders, proc, bound_s=bound_s)
        except Exception as exc:  # noqa: BLE001 - a reaper must never die
            _log(logf, f"liveness: tick failed: {exc}")


def _kill_child_tree(proc: subprocess.Popen, logf) -> Optional[int]:
    """Kill the child and its tree, then confirm it is really gone.

    PID-REUSE SAFETY: the tree kill is necessarily PID-addressed (taskkill
    reaches ffmpeg grandchildren), and Windows recycles PIDs freely. So the
    Popen object — which holds a live OS handle to the process we spawned and
    reaps it — is the gate: poll() is re-checked immediately before the kill,
    and a child that has already exited is NEVER killed by PID, because that
    pid may already belong to someone else. Same reason _process_is_alive
    decides on the exit code and not on OpenProcess succeeding.

    Returns the observed exit code, or None if the child is still alive."""
    pid = proc.pid
    if proc.poll() is not None:
        # Already gone (and reaped) — a PID kill here could hit a stranger.
        rc = proc.returncode
        _log(logf, f"stall: child pid {pid} already exited (rc={rc}) — "
                   "not killing by pid")
        return rc
    try:
        if os.name == "nt":
            os.system(f"taskkill /PID {pid} /F /T")
        else:
            os.kill(pid, 9)
    except Exception as exc:  # noqa: BLE001
        _log(logf, f"stall: kill of pid {pid} raised: {exc}")
    try:
        rc = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        _log(logf, f"stall: pid {pid} survived taskkill — proc.kill()")
        try:
            proc.kill()  # handle-based TerminateProcess: no PID lookup
            rc = proc.wait(timeout=15)
        except Exception as exc:  # noqa: BLE001
            _log(logf, f"stall: pid {pid} still alive after proc.kill(): {exc}")
            return None
    if _process_is_alive(pid):
        _log(logf, f"stall: pid {pid} STILL REPORTS ALIVE after kill (rc={rc})")
        return None
    _log(logf, f"stall: child pid {pid} confirmed dead (exit code {rc})")
    return rc


def _stall_watchdog(
    logf, proc: subprocess.Popen, stop_event: threading.Event,
    bound_s: Optional[float] = None, poll_s: Optional[float] = None,
) -> None:
    """Daemon thread: kill a wedged child, hand its job to the retry path.

    `bound_s` / `poll_s` are injectable only so tests can drive the REAL loop
    at test speed; production takes the derived bound and _STALL_POLL_S."""
    if bound_s is None:
        bound_s = _stall_bound_s()
    if poll_s is None:
        poll_s = _STALL_POLL_S
    holder = {
        "armed": False,
        "last_marks": None,
        "last_progress_wall": 0.0,
        "cpu_baseline": None,
        "error": None,
    }
    while not stop_event.wait(poll_s):
        if proc.poll() is not None:
            return  # child already exited on its own
        marks = _progress_marks()
        cpu = _process_cpu_seconds(proc.pid)
        reason = _stall_state(
            holder, time.monotonic(), marks=marks, cpu_seconds=cpu, bound_s=bound_s,
        )
        if reason is None:
            continue
        holder["error"] = reason
        _log(logf, f"stall watchdog: {reason} — killing child pid {proc.pid}")
        rc = _kill_child_tree(proc, logf)
        if rc is None:
            _log(logf, "stall watchdog: kill unconfirmed — leaving job state alone")
            return
        _mark_stalled_job_failed(reason, logf)
        _STALL_RECOVERED.set()
        return


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

            # Stall watchdog: the in-process watchdog CANNOT do this. A
            # decode that hard-deadlocks inside the native call holds the
            # GIL, so the child can neither notice nor time itself out; only
            # this parent can. It kills the wedged child, hands the job back
            # to the existing retry path, and the rc!=0 below then respawns
            # the worker so the next job runs.
            stall_watchdog = threading.Thread(
                target=_stall_watchdog,
                args=(logf, proc, watchdog_stop),
                daemon=True,
                name="stall-watchdog",
            )
            stall_watchdog.start()

            # Job-liveness reaper: the stall watchdog can only judge a child
            # it KILLS, and only for 'transcribe'. A 'chat' row that wedges
            # keeps a fresh heartbeat forever and is relaunched every 2 h
            # with attempts pinned at 0, so it never reaches max_attempts.
            # This thread judges the ROW — work marks + the child's CPU, the
            # same vocabulary the stall watchdog uses — and hands a condemned
            # row to the existing retry path. It never kills anything.
            liveness_reaper = threading.Thread(
                target=_job_liveness_reaper,
                args=(logf, proc, watchdog_stop),
                daemon=True,
                name="job-liveness-reaper",
            )
            liveness_reaper.start()

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

            if _STALL_RECOVERED.is_set():
                # A stall we killed and released is a recovery, not a crash:
                # keep serving the queue instead of walking into the 15 min
                # give-up cooldown. Per-job max_attempts still bounds a
                # video that wedges every time.
                _STALL_RECOVERED.clear()
                _log(logf, f"worker exited rc={rc} after a recovered stall — "
                           "not counting it as a crash; respawning for the "
                           "next job")
                wait = BACKOFF_SECONDS[0]
                _log(logf, f"restarting in {wait}s...")
                time.sleep(wait)
                continue

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
