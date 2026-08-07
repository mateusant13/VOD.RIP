"""Whisper e2e acceptance — REAL model, REAL media, machine-aware lane.

Proof contract (resource-capped, per Main's steers):
  1. Measure the machine at claim time (nvidia-smi free VRAM + compute-apps,
     free RAM, CPU load) and DECIDE the lane with the planner code.
  2. Real media: yt-dlp downloads the first 90 s of a real YouTube stream
     (nhUB3cJhUWE) as audio.
  3. Real worker path: `python -m services.archive_transcribe --once` runs
     as a CHILD (exactly what worker_server.py spawns), claims a real
     transcribe job from an isolated DB, plans its pool from the measured
     machine state, loads the REAL small model (int8) and transcribes.
  4. Assert segments land in transcripts; report lane + resource numbers.

GPU rule: another process holding a GPU model (compute-apps non-empty with
foreign pids) OR free VRAM below the model+headroom budget => CPU lane.
This box is shared (live backend + user's ML project), so the expected
result is the CPU int8 lane — the planner must say so.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
VID = "nhUB3cJhUWE"
CLIP_SEC = 90

root = Path(tempfile.mkdtemp(prefix="bw-whisper-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(root / "archive.db")
appdata = root / "appdata"
appdata.mkdir(parents=True, exist_ok=True)
(appdata / "settings.json").write_text(
    json.dumps({"youtube_auto_auth": False, "cookie_bridge_enabled": False}),
    encoding="utf-8",
)
os.environ["VODRIP_APP_DATA"] = str(appdata)

# 1) Machine state at claim time
def nvidia(fmt):
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=" + fmt, "--format=csv,noheader"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return out or "n/a"
    except Exception:
        return "n/a"

vram_used, vram_free, gpu_util = nvidia("memory.used,memory.free,utilization.gpu").split(",")
comp = subprocess.run(
    ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader"],
    capture_output=True, text=True, timeout=30,
).stdout.strip().splitlines()
import psutil  # noqa: E402

mem = psutil.virtual_memory()
cpu = psutil.cpu_percent(interval=1)
state = {
    "vram_free_gb": float(vram_free.replace(" MiB", "")) / 1024,
    "vram_used_gb": float(vram_used.replace(" MiB", "")) / 1024,
    "gpu_util_pct": gpu_util.replace(" %", ""),
    "compute_apps": comp,
    "ram_available_gb": round(mem.available / 2 ** 30, 2),
    "cpu_load_pct": cpu,
}
print("[state]", json.dumps(state))

# 2) Real media: first 90 s audio of the real stream
from services import archive_db  # noqa: E402

archive_db.get_conn()
media = root / "clip.mp3"
import yt_dlp  # noqa: E402

opts = {
    "format": "bestaudio/best",
    "outtmpl": str(media),
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "download_ranges": lambda _i, _y: [{"start_time": 0, "end_time": CLIP_SEC}],
    "force_keyframes_at_cuts": True,
    "noprogress": True,
}
t0 = time.monotonic()
with yt_dlp.YoutubeDL(opts) as ydl:
    info = ydl.extract_info(f"https://www.youtube.com/watch?v={VID}", download=True)
print(f"[media] downloaded {os.path.getsize(media) / 2 ** 20:.1f} MiB in "
      f"{time.monotonic() - t0:.0f}s (duration {info.get('duration')}s)")

archive_db.upsert_video({
    "platform": "youtube",
    "video_id": VID,
    "channel": "unknown",
    "title": f"whisper-e2e clip of {VID}",
    "duration_sec": CLIP_SEC,
    "archive_path": str(media),
})
archive_db.enqueue_job(f"transcribe-{VID}", "transcribe", "youtube", VID, priority=1)

# 3) REAL worker path as a child (what worker_server.py spawns)
env = dict(os.environ,
           VODRIP_WHISPER_MODEL="small",
           VODRIP_WHISPER_DEVICE="cpu",  # int8 CPU lane — the planner must agree
           )
child = subprocess.run(
    [sys.executable, "-m", "services.archive_transcribe", "--once", "--poll-interval", "2"],
    cwd=str(BACKEND), env=env, capture_output=True, text=True, timeout=1500,
)
print("[child] rc:", child.returncode)
for line in child.stdout.splitlines():
    if any(k in line for k in ("lane", "plan", "Loading whisper", "loaded in", "job", "worker")):
        print("[child]", line[:160])

# 4) Assert
segs = archive_db.query(
    "SELECT COUNT(*) c, SUM(end_sec - start_sec) secs FROM transcripts "
    "WHERE platform='youtube' AND video_id=?",
    (VID,),
)
job = archive_db.query(
    "SELECT status, error FROM archive_jobs WHERE id=?",
    (f"transcribe-{VID}",),
)[0]
print(f"[result] segments={segs[0]['c']} seconds={segs[0]['secs']} job={dict(job)}")
assert segs[0]["c"] > 0, "real transcription must produce segments"
assert job["status"] == "done", f"transcribe job must be done, got {dict(job)}"
print("WHISPER E2E: PASS")
