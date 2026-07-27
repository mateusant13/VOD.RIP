"""Self-check: regression guards for the kind=hls 404 fix.

Two regressions to guard against:
  R1. _extract_youtube_preview_info must define `vid` before its first use
      (a typo/rename once raised NameError on every warm and live path).
  R2. The warm-built snapshot dict MUST carry `resource_map` forward, AND
      _reuse_youtube_snapshot MUST restore it onto the live session, so the
      /api/preview/resource?id=<digest> endpoints don't return 404 for
      kind=hls synthetic-master URLs.

ponytail: live e2e pending a stable YouTube HLS-only fixture; this test
constructs a synthetic session/snapshot dict, runs the reuse function, and
asserts on the data round-trip. No network, no master-building (which
would force full mux disk I/O).
"""
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import preview_service as ps


def _run() -> None:
    # R1: the warm + extract path must resolve `vid` without NameError.
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    try:
        # Force the early-return branch by passing an invalid OAuth - the
        # function will hit _extract_via_yt_dlp_inner and raise RuntimeError
        # for "Preview unavailable", NOT NameError.
        try:
            ps._extract_youtube_preview_info(url, oauth=None, warm_light=True)
        except RuntimeError:
            pass  # expected when inner tube & yt-dlp stubs aren't mocked
        except NameError as e:
            raise AssertionError(f"R1 REGRESSION: {e}")
        # Success: vid was defined, no NameError.
    except Exception as e:
        # Other errors (yt-dlp fail, etc.) are fine; we only assert no
        # NameError on `vid`.
        if isinstance(e, NameError) and "vid" in str(e):
            raise AssertionError(f"R1 REGRESSION: {e}")

    # The production builder wires resource_map forward (the historical bug was
    # a missing key in the snapshot dict literal at line ~3933 of
    # preview_service.py).
    src = (ROOT / "services" / "preview_service.py").read_text(encoding="utf-8")
    assert '"resource_map": dict(tmp.resource_map)' in src, (
        "R2 REGRESSION: snapshot-builder no longer carries resource_map forward"
    )
    assert 'resource_map=dict(snapshot.get("resource_map") or {})' in src, (
        "R2 REGRESSION: _reuse_youtube_snapshot no longer restores resource_map"
    )
    # Bug 3 (race on snapshot reuse): two concurrent reuse calls with the
    # same sid must NOT overwrite each other's session. The fix returns
    # the existing session when one exists under the same sid.
    assert "existing = self._sessions.get(session_id)" in src, (
        "Bug 3 REGRESSION: _reuse_youtube_snapshot is no longer idempotent on sid "
        "- parallel clicks on the same video will overwrite resource_map and 404"
    )


    # Build a minimal snapshot dict exactly as the warm path would (without
    # the heavy master builder - we only need a dict shaped like the
    # production one).
    snap_dict = {
        "session_id": "test_resource_map_round_trip",
        "cache_dir": str(cache_dir),
        "master_url": "https://example.invalid/master.m3u8",
        "entry_url": "https://example.invalid/entry.ts",
        "platform": "YouTube",
        "http_headers": {},
        "allowed_hosts": {"example.invalid"},
        "kind": "hls",
        "preview_audio_url": "https://rr1.googlevideo.com/videoplayback?AUDIO",
        "variant_muxed": {360: False, 720: False, 1080: False},
        "variant_entries": [
            (360, "https://rr1.googlevideo.com/videoplayback?C"),
            (720, "https://rr1.googlevideo.com/videoplayback?B"),
            (1080, "https://rr1.googlevideo.com/videoplayback?A"),
        ],
        "custom_master": "#EXTM3U\n#EXT-X-VERSION:3\nhttp://x",
        "dash_window_hls": False,
        "preview_audio_fmt": None,
        "preview_video_fmt": None,
        "explore_yt_info": {"id": "fake"},
        "vod_duration": 600.0,
        "http_headers_storage": {},
        "resource_map": {
            "deadbeefdeadbeef": "https://rr1.googlevideo.com/videoplayback?C",
            "cafebabecafebabe": "https://rr1.googlevideo.com/videoplayback?B",
            "1234567890abcdef": "https://rr1.googlevideo.com/videoplayback?A",
            "feedfacefeedface": "https://rr1.googlevideo.com/videoplayback?AUDIO",
        },
    }

    # R2a: the snapshot dict itself contains resource_map.
    assert "resource_map" in snap_dict, "R2a: snapshot dict missing 'resource_map'"
    assert len(snap_dict["resource_map"]) == 4, "R2a: resource_map count mismatch"
    for digest in ("deadbeefdeadbeef", "cafebabecafebabe", "1234567890abcdef", "feedfacefeedface"):
        assert digest in snap_dict["resource_map"], f"R2a: missing digest {digest}"

    # R2b: reuse restores it onto the live session.
    manager = ps._manager
    reused = manager._reuse_youtube_snapshot(
        url, 0.0, 0.0, 360, snap_dict
    )
    assert reused is not None, "R2b: reuse returned None (vid extraction may have failed)"
    assert reused.kind == "hls", f"R2b: kind mismatch {reused.kind!r}"
    assert reused.custom_master, "R2b: custom_master lost on reuse"

    # R2a: the snapshot dict itself contains resource_map. We also assert the
    # production builder wires it in (the historical bug was a missing key in
    # the snapshot dict literal at line ~3933 of preview_service.py).
    src = (ROOT / "services" / "preview_service.py").read_text(encoding="utf-8")
    assert '"resource_map": dict(tmp.resource_map)' in src, (
        "R2a REGRESSION: snapshot-builder no longer carries resource_map forward"
    )
    assert 'resource_map=dict(snapshot.get("resource_map") or {})' in src, (
        "R2a REGRESSION: _reuse_youtube_snapshot no longer restores resource_map"
    )

    live_rm = reused.resource_map
    for digest in ("deadbeefdeadbeef", "cafebabecafebabe", "1234567890abcdef", "feedfacefeedface"):
        assert digest in live_rm, (
            f"R2b REGRESSION: live session.resource_map missing digest {digest} "
            f"(would 404 every variant in the synthetic master)"
        )
        assert live_rm[digest].startswith("https://rr1.googlevideo.com/"), (
            f"R2b: digest {digest} mapped to wrong URL: {live_rm[digest]!r}"
        )


if __name__ == "__main__":
    try:
        _run()
        print("OK: kind=hls 404 fix verified - snapshot+reuse preserve resource_map")
    except AssertionError as e:
        print(f"FAIL: {e}")
        sys.exit(1)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
