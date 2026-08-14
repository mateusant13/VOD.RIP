"""E2E: transcribe the 13.5h twitch VOD into the playground and sweep the
failure queries against the fresh transcript corpus.

Run from backend/:  python -m e2e_twitch_transcribe
Uses VODRIP_ARCHIVE_DB (default %TEMP%/vodrip-search-lab/playground.db).
"""
import os
import re
import sys
import time
from pathlib import Path

LAB = Path(os.environ.get("TEMP", ".")) / "vodrip-search-lab"
os.environ.setdefault("VODRIP_ARCHIVE_DB", str(LAB / "playground.db"))
os.environ.setdefault("VODRIP_WHISPER_DEVICE", "cuda")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from services import archive_db, archive_transcribe  # noqa: E402

PLATFORM = "twitch"
VIDEO_ID = "2831080813"
AUDIO = LAB / "2831080813.m4a"

# --- 1. register the archived video --------------------------------------
archive_db.upsert_video(
    {
        "platform": PLATFORM,
        "video_id": VIDEO_ID,
        "channel": "titiltei",
        "title": "E2E 13.5h VOD (search v3)",
        "kind": "video",
        "archive_path": str(AUDIO),
        "canonical_key": f"titiltei-{VIDEO_ID}",
        "status": "ready",
    }
)
n0 = archive_db.query("SELECT COUNT(*) AS n FROM transcripts")[0]["n"]
print(f"[e2e] video registered; transcripts before: {n0}", flush=True)

# --- 2. transcribe (resume-aware product path) ----------------------------
t0 = time.monotonic()
stats = archive_transcribe.transcribe_video(
    PLATFORM, VIDEO_ID, language="pt",
    progress_cb=lambda done, total, c, ct: (
        print(
            f"[e2e] speech {done:.0f}/{total:.0f}s chunk {c}/{ct} "
            f"({time.monotonic() - t0:.0f}s wall)",
            flush=True,
        )
        if c % 25 == 0
        else None
    ),
)
print(f"[e2e] transcribe stats: {stats}", flush=True)

# --- 3. sweep the failure queries -----------------------------------------
def found_variant(hits, variant):
    pat = re.compile(r"(?<![a-zà-ú0-9])" + re.escape(variant) + r"(?![a-zà-ú0-9])")
    return any(pat.search((h.get("text") or "").lower()) for h in hits)

cases = [
    ("katarina", "catarina"), ("shaco", "chaco"), ("kalista", "calista"),
    ("seraphine", "serafine"), ("orianna", "oriana"), ("nautilus", "nutilos"),
    ("shen", "suen"), ("nasus", "nasço"), ("nasus", "nasho"),
    ("sejuani", "sejuane"), ("ambessa", "ambeça"), ("sylas", "silas"),
    ("aurora", "aunara"), ("vayne", "veine"), ("garen", "garin"),
    ("darius", "darus"), ("talon", "tayon"), ("yasuo", "e aço"),
    ("yasuo", "iaso"), ("nasço", "nasus"), ("e aço", "yasuo"),
]
ok = 0
for q, variant in cases:
    hits = archive_db.search(q, limit=30)
    got = found_variant(hits, variant)
    ok += got
    print(f"{'OK ' if got else 'MISS'} q={q!r:12s} -> {variant!r:8s} hits={len(hits)}", flush=True)
n1 = archive_db.query("SELECT COUNT(*) AS n FROM transcripts")[0]["n"]
print(f"[e2e] sweep: {ok}/{len(cases)}  transcripts {n0} -> {n1}", flush=True)
