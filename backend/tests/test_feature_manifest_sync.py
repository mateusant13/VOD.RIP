"""Cross-check: backend/services/feature_registry.py MANIFEST == src/lib/featureManifest.ts FEATURE_MANIFEST.

Guards the dual-manifest drift risk flagged in review — a one-line edit in either
file must break this test if ids/costs/defaults diverge. No codegen yet;
this assertion is the cheap single-source enforcement.
"""
import json
import re
from pathlib import Path

from services.feature_registry import MANIFEST


_REPO_ROOT = Path(__file__).resolve().parents[2]
_TS_MANIFEST = _REPO_ROOT / "src" / "lib" / "featureManifest.ts"


def _parse_ts_manifest() -> list[dict]:
    text = _TS_MANIFEST.read_text(encoding="utf-8")
    # match { id: '...', cost: '...', defaultEnabled: true/false, description: '...' }
    pat = re.compile(
        r"\{\s*id:\s*'([^']+)'\s*,\s*cost:\s*'([^']+)'\s*,\s*defaultEnabled:\s*(true|false)\s*,\s*description:\s*'((?:[^'\\]|\\.)*)'",
        re.DOTALL,
    )
    rows: list[dict] = []
    for m in pat.finditer(text):
        rows.append(
            {
                "id": m.group(1),
                "cost": m.group(2),
                "defaultEnabled": m.group(3) == "true",
                "description": m.group(4),
            }
        )
    return rows


def test_manifest_in_sync():
    ts_rows = _parse_ts_manifest()
    assert ts_rows, f"failed to parse {_TS_MANIFEST}"
    assert len(ts_rows) == len(MANIFEST), f"manifest length diverges: py={len(MANIFEST)} ts={len(ts_rows)}"
    for py, ts in zip(MANIFEST, ts_rows):
        assert py["id"] == ts["id"], f"id diverges: py={py['id']} ts={ts['id']}"
        assert py["cost"] == ts["cost"], f"cost diverges for {py['id']}: py={py['cost']} ts={ts['cost']}"
        assert bool(py["defaultEnabled"]) == ts["defaultEnabled"], f"defaultEnabled diverges for {py['id']}: py={py['defaultEnabled']} ts={ts['defaultEnabled']}"
