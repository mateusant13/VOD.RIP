"""Self-check for enrichment_cache.py.

Exercises: fresh hit, stale miss, TTL boundary, persist/reload, _enriched signal.
Usage: python backend/tests/test_enrichment_cache_self.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.enrichment_cache import (
    TTL_AVAIL,
    TTL_META,
    _cache,
    _cache_path,
    _get_cache_path,
    _loaded,
    _save,
    apply_to_row,
    get,
    set,
    set_availability,
)


def _reset():
    _cache.clear()
    _loaded = True
    p = _cache_path
    if p and os.path.exists(p):
        os.remove(p)


def test_fresh_hit():
    _reset()
    set("vid1", {"created_at": "2025-01-01", "views": 1000})
    entry = get("vid1")
    assert entry is not None, "fresh set -> get should return entry"
    assert entry.get("views") == 1000
    assert entry.get("created_at") == "2025-01-01"
    assert "cached_at" in entry


def test_stale_miss():
    _reset()
    set("vid2", {"created_at": "2025-01-01"})
    entry = get("vid2")
    assert entry is not None
    entry["cached_at"] = time.time() - TTL_META - 100
    _save()

    row = {"id": "vid2"}
    applied = apply_to_row(row)
    assert not applied or "created_at" not in row


def test_ttl_boundary_meta():
    _reset()
    set("vid3", {"created_at": "2025-06-01", "duration": 300})
    entry = get("vid3")
    entry["cached_at"] = time.time() - TTL_META + 10
    _save()

    row = {"id": "vid3"}
    applied = apply_to_row(row)
    assert applied, "should apply when 10s before expiry"
    assert row.get("created_at") == "2025-06-01"
    assert row.get("duration") == 300

    _reset()
    set("vid4", {"created_at": "2025-06-01"})
    entry = get("vid4")
    entry["cached_at"] = time.time() - TTL_META - 10
    _save()

    row2 = {"id": "vid4"}
    apply_to_row(row2)
    assert "created_at" not in row2, "should NOT apply when 10s past TTL"


def test_availability_ttl():
    _reset()
    set_availability("vid5", "subscriber_only")
    entry = get("vid5")
    assert entry.get("availability") == "subscriber_only"
    assert "avail_cached_at" in entry

    row = {"id": "vid5"}
    applied = apply_to_row(row)
    assert applied
    assert row.get("availability") == "subscriber_only"
    assert row.get("_availability_checked") is True

    _reset()
    set_availability("vid6", "subscriber_only")
    entry = get("vid6")
    entry["avail_cached_at"] = time.time() - TTL_AVAIL - 100
    _save()

    row2 = {"id": "vid6"}
    apply_to_row(row2)
    assert not row2.get("_availability_checked"), "stale availability should NOT apply"


def test_persist_reload():
    _reset()
    set("vid_persist", {"created_at": "2025-07-01", "views": 500})
    path = _get_cache_path()
    assert os.path.exists(path), f"cache file should exist at {path}"

    _cache.clear()
    _loaded = False
    row = {"id": "vid_persist"}
    applied = apply_to_row(row)
    assert applied, "should load from disk"
    assert row.get("created_at") == "2025-07-01"
    assert row.get("views") == 500


def test_enriched_signal():
    """apply_to_row sets _enriched when metadata keys exist (even if values are None)"""
    _reset()
    # Simulate a metadata result where InnerTube returned no views/duration
    set("vid_enr", {"created_at": None, "views": None, "duration": None, "availability": ""})
    row = {"id": "vid_enr"}
    applied = apply_to_row(row)
    assert row.get("_enriched") is True, "_enriched should be set when metadata keys exist"
    assert applied, "should return True"


def test_enriched_not_set_for_avail_only():
    """apply_to_row does NOT set _enriched when only availability is cached"""
    _reset()
    set_availability("vid_avail_only", "subscriber_only")
    row = {"id": "vid_avail_only"}
    applied = apply_to_row(row)
    assert row.get("_enriched") is None, "_enriched should NOT be set for availability-only"
    assert row.get("_availability_checked") is True, "availability should still be applied"
    assert applied


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  PASS  {name}")
    print("All self-checks passed.")
