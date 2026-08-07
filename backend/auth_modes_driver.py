"""Both-auth-modes acceptance for the archive worker's YouTube auth tower.

Run once per mode (isolated subprocess + env so module caches never leak):

    python auth_modes_driver.py cookies [video_id]
    python auth_modes_driver.py anon    [video_id]

cookies mode: keeps the machine's REAL cookie sources (bridge export +
cookie cache) and proves the worker path ingests chat through them.
anon mode:   isolates VODRIP_APP_DATA (no cookie cache, no bridge store)
             and pins settings youtube_auto_auth=false +
             cookie_bridge_enabled=false so the anonymous bootstrap
             (2h TTL cold visit) is the ONLY credential source.

Both modes run the actual worker chat-job path (_process_chat_job ->
backfill_live_chat) against a real YouTube stream with live-chat replay,
into an isolated VODRIP_ARCHIVE_DB, and assert chat rows land + the job
completes 'done'. Exit 0 = pass.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
MODE = sys.argv[1] if len(sys.argv) > 1 else "cookies"
VIDEO_ID = sys.argv[2] if len(sys.argv) > 2 else "nhUB3cJhUWE"

if MODE not in ("cookies", "anon"):
    print("usage: python auth_modes_driver.py cookies|anon [video_id]")
    sys.exit(2)

root = Path(tempfile.mkdtemp(prefix=f"bw-auth-{MODE}-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(root / "archive.db")
if MODE == "anon":
    # Isolated appdata: no cookie cache, no bridge store, empty settings.
    appdata = root / "appdata"
    appdata.mkdir(parents=True, exist_ok=True)
    (appdata / "settings.json").write_text(
        json.dumps({"youtube_auto_auth": False, "cookie_bridge_enabled": False}),
        encoding="utf-8",
    )
    os.environ["VODRIP_APP_DATA"] = str(appdata)

from services import archive_db  # noqa: E402

archive_db.get_conn()

# 1) Mode proof: which credential source did the auth tower pick?
from services.youtube_session import youtube_session_from_settings  # noqa: E402

session = youtube_session_from_settings(video_id=VIDEO_ID)
cf = session.cookie_file or ""
anon_jar = Path(cf).name.startswith("yt_anon_") if cf else False
mode_proof = {
    "anonymous": session.anonymous,
    "cookies_from_browser": session.cookies_from_browser,
    "cookie_file": cf,
    "has_cookie_header": bool(session.cookie_header),
    "has_visitor_data": bool(session.visitor_data),
    "anon_jar": anon_jar,
}
print(f"[{MODE}] session proof: {mode_proof}")

if MODE == "cookies":
    assert mode_proof["has_cookie_header"], "cookies mode must assemble a real cookie source"
    assert not mode_proof["anon_jar"], "cookies mode must not fall back to the anonymous jar"
else:
    assert mode_proof["anonymous"], "anon mode must use the anonymous bootstrap"
    assert mode_proof["anon_jar"] or not mode_proof["has_cookie_header"], (
        "anon mode must carry no real cookie header"
    )

# 2) Real worker chat-job path (gate check -> pacing -> backfill_live_chat).
job_id = f"chat-youtube-{VIDEO_ID}-{int(time.time())}"
archive_db.enqueue_job(job_id, "chat", "youtube", VIDEO_ID, priority=0)

from services.archive_transcribe import _process_chat_job  # noqa: E402

t0 = time.monotonic()
out = _process_chat_job(job_id, "youtube", VIDEO_ID)
elapsed = time.monotonic() - t0

rows = archive_db.query(
    "SELECT COUNT(*) c FROM messages WHERE platform='youtube' AND video_id=?",
    (VIDEO_ID,),
)
job = archive_db.query(
    "SELECT status, error FROM archive_jobs WHERE id=?", (job_id,)
)[0]
print(f"[{MODE}] worker out: {out}")
print(f"[{MODE}] chat rows: {rows[0]['c']}  job: {dict(job)}  ({elapsed:.1f}s)")

assert rows[0]["c"] > 0, f"chat rows must land in {MODE} mode"
assert job["status"] == "done", f"job must complete done in {MODE} mode, got {dict(job)}"
print(f"AUTH-MODES {MODE.upper()}: PASS")
