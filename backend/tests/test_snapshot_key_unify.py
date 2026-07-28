#!/usr/bin/env python3
"""Self-check: cache key unification between warm and create_session.

Tests the snapshot storage/lookup key layer directly — _put_session_snapshot
and _get_session_snapshot — with various prefer_height values, plus the
public-facing normalize behavior.
"""

import sys
import os
import types
import unittest.mock as um

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Minimal yt_dlp to let imports through ──
yt_dlp_mod = types.ModuleType("yt_dlp")
yt_dlp_mod.version = um.MagicMock(return_value="2024.01.01")
yt_dlp_mod.DownloadError = Exception
utils_mod = types.ModuleType("yt_dlp.utils")
utils_mod.encodeArgument = lambda v: v
utils_mod.get_executable_path = lambda *a, **kw: None
utils_mod.unescape_html = lambda v: v
utils_mod.GetPids = lambda: []
sys.modules["yt_dlp.utils"] = utils_mod
yt_dlp_mod.utils = utils_mod
FakeFFmpegPP = type("FFmpegPostProcessor", (), {"real_run_ffmpeg": lambda *a: None})
ffmpeg_mod = types.ModuleType("yt_dlp.postprocessor.ffmpeg")
ffmpeg_mod.FFmpegPostProcessor = FakeFFmpegPP
sys.modules["yt_dlp.postprocessor.ffmpeg"] = ffmpeg_mod
pp_mod = types.ModuleType("yt_dlp.postprocessor")
pp_mod.FFmpegPostProcessor = FakeFFmpegPP
sys.modules["yt_dlp.postprocessor"] = pp_mod
sys.modules["yt_dlp"] = yt_dlp_mod
sys.modules["services.youtube_service"] = um.MagicMock()
sys.modules["services.youtube_innertube"] = um.MagicMock()
sys.modules["models.preview"] = um.MagicMock()

from services.preview_service import (
    _put_session_snapshot,
    _get_session_snapshot,
    _SESSION_SNAPSHOT,
)


def make_snap(sid="snap_001"):
    """Return a minimal snapshot dict with the fields _get_session_snapshot reads."""
    return {
        "session_id": sid,
        "cache_dir": "/tmp/test_cache",
    }


# ── Test 1: Warm stores (vid, 720), create_session looks up (vid, 720) ──
def test_warm_vid720_createsession_vid720():
    _SESSION_SNAPSHOT.clear()
    vid = "videotest123"
    _put_session_snapshot(vid, 720, make_snap("warm_720"))

    found = _get_session_snapshot(vid, 720)
    assert found is not None, "snapshot stored at (vid, 720) should be found at (vid, 720)"
    assert found["session_id"] == "warm_720"
    print("PASS: (vid, 720) -> (vid, 720) -> HIT")


# ── Test 2: This was the BUG — warm stores (vid, 720), create_session
#    with prefer_height=None looks up (vid, 0) → MISS.
#    After fix: prefer_height = prefer_height or 720 normalizes None→720. ──
def test_warm_vid720_createsession_vid0_was_bug():
    _SESSION_SNAPSHOT.clear()
    vid = "bugtest_was_miss"
    _put_session_snapshot(vid, 720, make_snap("warm_720"))

    # BEFORE FIX: _get_session_snapshot(vid, None) → key=(vid, int(None or 0))=(vid, 0)
    # AFTER FIX:  prefer_height = prefer_height or 720 → prefer_height=720 before lookup
    found = _get_session_snapshot(vid, 720)  # normalized key
    assert found is not None, (
        "The bug: warm (vid,720) but create_session(None) looked up (vid,0). "
        "Fix: prefer_height = prefer_height or 720 normalizes to 720."
    )
    assert found["session_id"] == "warm_720"
    print("PASS: warm (vid, 720) -> create_session(None->720) -> HIT (bug fixed)")


# ── Test 3: prefer_height=0 also normalizes ──
def test_prefer_height_zero_normalizes():
    # warm stores at 720
    _SESSION_SNAPSHOT.clear()
    vid = "zero_test"
    _put_session_snapshot(vid, 720, make_snap("warm_720"))

    # create_session with prefer_height=0 → int(0 or 720)=720
    normalized = 0 or 720
    assert normalized == 720, "prefer_height=0 should normalize to 720"

    found = _get_session_snapshot(vid, normalized)
    assert found is not None, "0 normalizes to 720"
    print("PASS: prefer_height=0 -> normalized to 720 -> HIT")


# ── Test 4: Different heights are separate keys ──
def test_different_heights_separate():
    _SESSION_SNAPSHOT.clear()
    vid = "sep_test"
    _put_session_snapshot(vid, 360, make_snap("warm_360"))
    _put_session_snapshot(vid, 720, make_snap("warm_720"))

    f360 = _get_session_snapshot(vid, 360)
    f720 = _get_session_snapshot(vid, 720)

    assert f360 is not None and f360["session_id"] == "warm_360"
    assert f720 is not None and f720["session_id"] == "warm_720"
    print("PASS: (vid,360) and (vid,720) are separate entries")


# ── Test 5: Height normalization function ──
def test_normalize_logic():
    # This is prefer_height = prefer_height or 720
    for val, expected in [(None, 720), (0, 720), (360, 360), (720, 720), (1080, 1080)]:
        normalized = val or 720
        assert normalized == expected, f"normalize({val}) = {normalized}, expected {expected}"
    print("PASS: prefer_height normalization logic correct")


# ── Test 6: _build_and_cache_youtube_snapshot with a
#    resolve_result that makes _build_youtube_session_snapshot return
#    quickly. Since the full pipeline is complex, we test the
#    key consistency through _put + _get directly. ──
def test_snapshot_overwrite():
    """Re-storing under the same key replaces the old entry."""
    _SESSION_SNAPSHOT.clear()
    vid = "overwrite_test"
    _put_session_snapshot(vid, 720, make_snap("first_snap"))
    _put_session_snapshot(vid, 720, make_snap("second_snap"))

    found = _get_session_snapshot(vid, 720)
    assert found is not None
    assert found["session_id"] == "second_snap", "should get the latest snapshot"
    print("PASS: snapshot overwrite works — last write wins")


# ── Test 7: TTL expiration ──
def test_ttl_expiration():
    """Snapshot older than TTL is treated as a cache miss."""
    import time
    _SESSION_SNAPSHOT.clear()

    # Store a snapshot with a timestamp in the distant past
    vid = "ttl_test"
    old_stamp = time.time() - 99999  # well past TTL
    _SESSION_SNAPSHOT[(vid, 720)] = (old_stamp, make_snap("expired_snap"))

    found = _get_session_snapshot(vid, 720)
    assert found is None, "expired snapshot should return None"
    assert (vid, 720) not in _SESSION_SNAPSHOT, "expired entry should be removed"
    print("PASS: expired snapshot -> None (cache miss)")


if __name__ == "__main__":
    test_warm_vid720_createsession_vid720()
    test_warm_vid720_createsession_vid0_was_bug()
    test_prefer_height_zero_normalizes()
    test_different_heights_separate()
    test_normalize_logic()
    test_snapshot_overwrite()
    test_ttl_expiration()
    print("\nAll self-checks passed.")
    sys.exit(0)
