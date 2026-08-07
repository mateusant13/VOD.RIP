"""A4 acceptance: app auto-spawn + detached worker survives backend exit.

Boots the worktree dev_server on a free port with a fresh isolated DB
containing one pending chat job; asserts the detached supervisor spawns and
the in-process worker is NOT started (watchdog grace); kills the backend
tree mid-job; asserts the detached worker survives and drains the queue.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
PORT = 7905

db = Path(tempfile.mkdtemp(prefix="bw-a4-")) / "archive.db"
appdata = Path(tempfile.mkdtemp(prefix="bw-a4-appdata-"))
# Settings isolation: with the LIVE appdata the boot scheduler + warm +
# live-chat watchdog enqueue ~500 channel jobs and never drain; a fresh
# appdata (defaults, zero saved channels) keeps the queue to OUR one job.
env = dict(os.environ, VODRIP_ARCHIVE_DB=str(db), VODRIP_APP_DATA=str(appdata))

# A REAL youtube stream with live-chat replay: the worker's chat job must
# actually extract + download chat, not fail on a synthetic id.
VID = "nhUB3cJhUWE"

subprocess.run(
    [sys.executable, "-c",
     "from services import archive_db\n"
     "archive_db.get_conn()\n"
     f"archive_db.enqueue_job('chat-youtube-{VID}', 'chat', 'youtube', '{VID}')\n"],
    cwd=str(BACKEND), env=env, check=True,
)

srv_log = Path(tempfile.mkdtemp(prefix="bw-a4-")) / "dev.log"
srv = subprocess.Popen(
    [sys.executable, "dev_server.py", "--port", str(PORT)],
    cwd=str(BACKEND), env=env,
    stdout=open(srv_log, "w"), stderr=subprocess.STDOUT,
)

def grep_log(pattern, tail=0):
    text = srv_log.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if tail:
        lines = lines[-tail:]
    return [l for l in lines if pattern in l]

try:
    # 1) Boot: detached spawn, in-process SKIPPED, no premature takeover
    for _ in range(120):
        if grep_log("Archive worker: detached supervisor spawned"):
            break
        time.sleep(0.5)
    spawn_lines = grep_log("Archive worker: detached supervisor spawned")
    assert spawn_lines, "detached supervisor never spawned"
    time.sleep(10)  # give the watchdog its early polls (bug regression window)
    takeover = grep_log("Detached worker exited")
    assert not takeover, f"watchdog double-started worker: {takeover}"

    # 2) Detached supervisor + child alive
    ps = subprocess.run(
        ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine"],
        capture_output=True, text=True,
    ).stdout
    sup = [l for l in ps.splitlines() if "worker_server.py" in l]
    assert sup, "detached supervisor not in process list"

    # 2b) Two-lane smoke: an interactive HTTP request completes instantly
    # while the detached worker is active, and the app-activity heartbeat
    # lands in the DB (the worker's pacing reads it to back off).
    import urllib.request

    t0 = time.monotonic()
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=10) as resp:
        body = resp.read()
    interactive_elapsed = time.monotonic() - t0
    assert resp.status == 200 and body, "interactive request must answer"
    assert interactive_elapsed < 5.0, f"interactive lane must stay fast, got {interactive_elapsed:.2f}s"
    print(f"PASS: interactive HTTP {resp.status} in {interactive_elapsed:.2f}s while worker active")
    activity_ok = False
    for _ in range(30):
        r = subprocess.run(
            [sys.executable, "-c",
             "from services import archive_db\n"
             "archive_db.get_conn()\n"
             "print(bool(archive_db.worker_live(age_s=90, tag='app-activity')))\n"],
            cwd=str(BACKEND), env=env, capture_output=True, text=True,
        )
        if r.stdout.strip() == "True":
            activity_ok = True
            break
        time.sleep(1)
    assert activity_ok, "app-activity heartbeat never stamped (two-lane pacing signal missing)"

    # 3) Kill the backend tree mid-job; detached worker must survive
    srv_ps = [l for l in ps.splitlines() if f"dev_server.py --port {PORT}" in l]
    assert srv_ps, "dev_server not found"
    srv_pid = int(srv_ps[0].split()[-1])
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(srv_pid)],
                   capture_output=True, text=True)
    time.sleep(2)
    ps2 = subprocess.run(
        ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine"],
        capture_output=True, text=True,
    ).stdout
    assert any("worker_server.py" in l for l in ps2.splitlines()), \
        "detached supervisor died with the backend"
    print("PASS: detached supervisor survived backend exit")

    # 4) Queue drains; detached worker exits on its own
    ok = False
    for _ in range(300):
        r = subprocess.run(
            [sys.executable, "-c",
             "from services import archive_db\n"
             "c = archive_db.get_conn()\n"
             f"row = c.execute(\"select status from archive_jobs where id='chat-youtube-{VID}'\").fetchone()\n"
             "print(row[0] if row else 'MISSING')\n"
             "print(archive_db.has_pending_jobs())"],
            cwd=str(BACKEND), env=env, capture_output=True, text=True,
        )
        # A transient sqlite lock (worker mid-write) makes the probe fail
        # with empty stdout — treat as not-yet-done and retry.
        lines = r.stdout.splitlines()
        status, pending = (lines[0], lines[1]) if len(lines) >= 2 else ("", "")
        if status == "done" and pending == "False":
            ok = True
            break
        time.sleep(2)
    assert ok, "job never drained to done"
    print("PASS: job done, queue empty")

    for _ in range(120):
        ps3 = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine"],
            capture_output=True, text=True,
        ).stdout
        if not any("worker_server.py" in l for l in ps3.splitlines()):
            print("PASS: detached supervisor exited after drain")
            break
        time.sleep(1)
    else:
        raise AssertionError("detached supervisor still running after drain")
finally:
    # Kill the WHOLE dev_server tree: srv.kill() alone leaves run.py orphaned
    # and holding the port, which poisons the next run.
    ps = subprocess.run(
        ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine"],
        capture_output=True, text=True,
    ).stdout
    for l in ps.splitlines():
        if f"dev_server.py --port {PORT}" in l:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", l.split()[-1]],
                               capture_output=True)
            except Exception:
                pass
    if srv.poll() is None:
        srv.kill()
    ps = subprocess.run(
        ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine"],
        capture_output=True, text=True,
    ).stdout
    for l in ps.splitlines():
        if "worker_server.py" in l:
            try:
                subprocess.run(["taskkill", "/F", "/PID", l.split()[-1]],
                               capture_output=True)
            except Exception:
                pass
print("A4 ACCEPTANCE: ALL PASS")
