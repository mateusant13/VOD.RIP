"""WS-7 E2E on a copy of the REAL archive DB (read-only source).

Auto mode: syncs entities from the real saved_channels (14 channels incl.
guiven — added by the user via UI), scans all real titiltei transcripts
(29,167 rows), reports hits per entity with snippets.
Manual mode: registers the phrase 'o guiven é muito ruim' + explicit
aliases, scans, reports.

The real %APPDATA% files are never written: the DB is copied to temp and
VODRIP_ARCHIVE_DB points at the copy; settings are only read.
"""

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

APPDATA_VODRIP = Path(os.environ["APPDATA"]) / "VOD.RIP"
SRC_DB = APPDATA_VODRIP / "archive.db"
assert SRC_DB.exists(), f"real archive.db missing: {SRC_DB}"

tmp = Path(tempfile.mkdtemp(prefix="vodrip-entity-e2e-"))
dst = tmp / "archive.db"
print(f"copying {SRC_DB} ({SRC_DB.stat().st_size/1e6:.1f} MB) -> {dst}")
shutil.copy2(SRC_DB, dst)

os.environ["VODRIP_ARCHIVE_DB"] = str(dst)
# Keep the REAL settings (saved_channels incl. guiven) for auto sync.
os.environ.pop("VODRIP_APP_DATA", None)

# Import AFTER env is set (archive_db binds its connection at import).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from services import archive_db  # noqa: E402
from services.entity_watch import run_scan_once, sync_auto_entities, match_entity_in_text  # noqa: E402

t0 = time.time()
n_sync = sync_auto_entities()
print(f"auto entities synced from {n_sync} saved channels: "
      f"{[e['text'] for e in archive_db.list_watched_entities() if e['kind']=='auto' and e['enabled']]}")

total = archive_db.query("SELECT COUNT(*) AS n FROM transcripts")[0]["n"]
print(f"transcript rows: {total}")
t1 = time.time()
passes = 0
cum_hits = 0
while True:
    stats = run_scan_once()
    passes += 1
    cum_hits += stats["hits"]
    if stats["scanned"] == 0:
        break
print(f"scan: {passes} passes, {cum_hits} hits, full catch-up in {time.time()-t1:.1f}s")

# Second full pass must be near-zero (watermark advanced).
t1 = time.time()
stats2 = run_scan_once()
print(f"scan pass after catch-up: {stats2} in {time.time()-t1:.2f}s")

hits = archive_db.list_entity_hits(limit=500)
print(f"\nhit rows: {len(hits)}")
by_entity: dict[str, list] = {}
for h in hits:
    by_entity.setdefault(h["entity_text"], []).append(h)

for ent_text, hs in sorted(by_entity.items()):
    print(f"\n=== {ent_text} ({len(hs)} hits) ===")
    for h in hs[:6]:
        print(f"  [{h['platform']}/{h['video_id']} @{h['offset_sec']:.0f}s"
              + (f" via {h['variant']!r}" if h["variant"] else "")
              + f"] {h['snippet'][:110]}")

# Manual mode: phrase + explicit aliases, scan a second copy to keep the
# auto-mode watermark intact.
dst2 = tmp / "archive-manual.db"
shutil.copy2(SRC_DB, dst2)
os.environ["VODRIP_ARCHIVE_DB"] = str(dst2)
# Fresh import is not needed: archive_db reopens on the path change.
t1 = time.time()
eid = archive_db.upsert_watched_entity(
    "o guiven é muito ruim", kind="manual",
    aliases=["o guiven é mt ruim", "guiven é muito ruim"],
)
print(f"\nmanual entity id={eid}; scanning full corpus again "
      f"({time.time()-t1:.1f}s to prep)")
t1 = time.time()
while True:
    stats = run_scan_once()
    if stats["scanned"] == 0:
        break
print(f"manual scan: {stats} (final pass) in {time.time()-t1:.1f}s total")
mhits = archive_db.list_entity_hits(entity_id=eid, limit=20)
print(f"manual phrase hits: {len(mhits)}")
for h in mhits[:10]:
    print(f"  [{h['platform']}/{h['video_id']} @{h['offset_sec']:.0f}s"
          + (f" via {h['variant']!r}" if h["variant"] else "")
          + f"] {h['snippet'][:110]}")

# Matcher spot-checks on real titiltei content (srdogg ASR variants).
samples = archive_db.query(
    "SELECT text FROM transcripts WHERE text LIKE '%senhor%' OR text LIKE '%senior%' LIMIT 20"
)
print(f"\nsenhor/senior raw occurrences: {len(samples)}")
for row in samples[:5]:
    print(f"  {row['text'][:120]!r}")

print(f"\ntotal elapsed: {time.time()-t0:.1f}s; temp artifacts in {tmp}")
