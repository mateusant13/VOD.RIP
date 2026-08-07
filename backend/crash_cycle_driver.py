"""3-crash give-up acceptance for worker_server.py (take 3).

Fresh DB (no heartbeat rows -> worker_server's first-wins guard passes every
round). Each spawned child is hard-killed DURING BOOT (before its first
heartbeat stamp), so every respawn also passes the guard; after 3 crashes the
supervisor must give up with rc 1. Log assertions happen against worker.log.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
LOG = BACKEND / "logs" / "worker.log"

db = Path(tempfile.mkdtemp(prefix="bw-crash-")) / "archive.db"
env = dict(os.environ, VODRIP_ARCHIVE_DB=str(db))

subprocess.run(
    [sys.executable, "-c",
     "from services import archive_db\n"
     "archive_db.get_conn()\n"
     "archive_db.enqueue_job('chat-youtube-XvJy7vyt-18', 'chat', 'youtube', 'XvJy7vyt-18')\n"],
    cwd=str(BACKEND), env=env, check=True,
)

mark = LOG.stat().st_size
sup = subprocess.Popen(
    [sys.executable, "worker_server.py"], cwd=str(BACKEND), env=env,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
kills = []
last_pid = None
try:
    for i in range(1, 4):
        pid = None
        for _ in range(600):  # up to 60s per round
            with open(LOG, encoding="utf-8", errors="replace") as fh:
                fh.seek(mark)
                tail = fh.read()
            lines = [l for l in tail.splitlines() if "child pid" in l]
            if lines:
                cand = int(lines[-1].strip().split()[-1])
                if cand != last_pid:
                    pid = cand
                    break
            time.sleep(0.1)
        assert pid is not None, f"no NEW child spawned in round {i} (last={last_pid})"
        time.sleep(1.5)  # mid-import: BEFORE the first heartbeat stamp
        r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True)
        print(f"round {i}: killed child {pid} (taskkill rc={r.returncode})")
        kills.append(pid)
        last_pid = pid
        time.sleep(0.3)
    rc = sup.wait(timeout=120)
finally:
    if sup.poll() is None:
        sup.kill()
print("SUPERVISOR-RC:", rc, "| killed pids:", kills)
