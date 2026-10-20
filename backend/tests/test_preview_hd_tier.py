"""Dual-tier window HLS: instant small tier + engaged (1080) tier.

The owner requirement these tests protect: the preview opens small and instant,
and reaching 1080p when the panel is enlarged must not stall. A stall is
structural, not cosmetic - it happens when changing quality means re-muxing,
which is exactly what a single-tier session forced. So the contract is:

  * the master lists BOTH tiers, small first (hls.js starts on index 0);
  * the engaged tier covers the SAME window bounds, so every segment boundary
    matches and hls.js can switch level mid-playback;
  * a not-yet-muxed engaged tier degrades to the small playlist instead of an
    empty one (an empty playlist is a hard player error);
  * the resource ids of the two tiers never collide.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from services.preview import session as S


def _session(tmp_path, heights=(480, 1080), crop_end=25.0):
    sess = S.PreviewSession(
        session_id="hdtest01",
        vod_url="https://www.youtube.com/watch?v=x",
        platform="YouTube",
        master_url="",
        entry_url="https://example.com/v",
        cache_dir=tmp_path,
        crop_start=0,
        crop_end=crop_end,
    )
    sess.dash_window_hls = True
    sess.variant_entries = [(h, f"https://cdn.example/{h}.mp4") for h in heights]
    sess.preview_audio_url = "https://cdn.example/a.m4a"
    return sess


def _write_segments(root: Path, count: int, ext: str = ".ts"):
    root.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (root / f"seg_{i:03d}{ext}").write_bytes(b"\x47" * 188)


def _extinf(playlist: str):
    return re.findall(r"#EXTINF:([0-9.]+),", playlist)


def test_master_lists_both_tiers_small_first(tmp_path):
    sess = _session(tmp_path)

    master = S._build_youtube_window_hls_master(sess)

    assert master.count("#EXT-X-STREAM-INF") == 2
    small_at = master.index(f"RESOLUTION={int(480 * 16 / 9)}x480")
    hd_at = master.index("RESOLUTION=1920x1080")
    assert small_at < hd_at, "hls.js attaches on index 0 - the small tier must be first"
    assert f"id={S.WINDOW_HLS_PLAYLIST_RESOURCE}" in master
    assert f"id={S.WINDOW_HLS_HD_PLAYLIST_RESOURCE}" in master


def test_master_keeps_bandwidth_ordering_sane(tmp_path):
    """HD must not advertise less bandwidth than the small tier, or ABR will
    treat it as nearly free and pick it on a small panel."""
    sess = _session(tmp_path)
    master = S._build_youtube_window_hls_master(sess)

    bands = [int(b) for b in re.findall(r"BANDWIDTH=(\d+)", master)]

    assert bands == sorted(bands)
    assert bands[1] > bands[0]


def test_no_engaged_tier_when_the_ladder_has_nothing_above_small(tmp_path):
    sess = _session(tmp_path, heights=(360, 480))

    master = S._build_youtube_window_hls_master(sess)

    assert master.count("#EXT-X-STREAM-INF") == 1
    assert S.WINDOW_HLS_HD_PLAYLIST_RESOURCE not in master


def test_engaged_tier_caps_at_1080(tmp_path):
    sess = _session(tmp_path, heights=(480, 1080, 1440, 2160))

    assert S._window_hls_hd_height(sess) == 1080


def test_engaged_tier_falls_back_to_the_best_available(tmp_path):
    sess = _session(tmp_path, heights=(480, 720))

    assert S._window_hls_hd_height(sess) == 720


def test_both_playlists_have_identical_segment_boundaries(tmp_path):
    """The alignment that makes the switch gapless: same count, same EXTINF."""
    sess = _session(tmp_path)
    _write_segments(S._window_hls_dir(sess), 4)
    _write_segments(S._window_hls_hd_dir(sess), 4)

    small = S._build_youtube_window_hls_media_playlist(sess).decode()
    hd = S._build_youtube_window_hls_media_playlist(sess, hd=True).decode()

    assert _extinf(small) == _extinf(hd)
    assert len(_extinf(small)) == 4


def test_tier_resource_ids_never_collide(tmp_path):
    sess = _session(tmp_path)
    _write_segments(S._window_hls_dir(sess), 2)
    _write_segments(S._window_hls_hd_dir(sess), 2)

    S._register_youtube_window_hls_resources(sess)
    S._register_youtube_window_hls_hd_resources(sess)

    small = [r for r in sess.resource_map if r.startswith(S.WINDOW_HLS_SEGMENT_RESOURCE_PREFIX)]
    hd = [r for r in sess.resource_map if r.startswith(S.WINDOW_HLS_HD_SEGMENT_RESOURCE_PREFIX)]
    assert small and hd
    assert not set(small) & set(hd)
    assert sess.resource_map[S.WINDOW_HLS_HD_PLAYLIST_RESOURCE] != sess.resource_map[
        S.WINDOW_HLS_PLAYLIST_RESOURCE
    ]


def test_hd_playlist_points_at_hd_resources_not_the_small_ones(tmp_path):
    sess = _session(tmp_path)
    _write_segments(S._window_hls_dir(sess), 2)
    _write_segments(S._window_hls_hd_dir(sess), 2)

    hd = S._build_youtube_window_hls_media_playlist(sess, hd=True).decode()

    assert "window-hd-seg-000" in hd
    assert "window-seg-000" not in hd


def test_hd_playlist_is_empty_but_valid_before_the_mux_runs(tmp_path):
    """The proxy falls back to the small playlist, so this body is only a
    safety net - but it must still be parseable, never a truncated file."""
    sess = _session(tmp_path)

    body = S._build_youtube_window_hls_media_playlist(sess, hd=True).decode()

    assert body.startswith("#EXTM3U")
    assert "#EXT-X-TARGETDURATION" in body
    assert not _extinf(body)


def test_hd_dir_is_distinct_from_the_small_tier(tmp_path):
    sess = _session(tmp_path)

    assert S._window_hls_hd_dir(sess) != S._window_hls_dir(sess)
    assert S._window_hls_hd_dir(sess).name == "window_hls_hd"
    assert S._window_hls_dir(sess).name == "window_hls"


def test_hd_ready_is_false_until_a_segment_exists(tmp_path):
    sess = _session(tmp_path)

    assert S._window_hls_hd_ready(sess) is False

    _write_segments(S._window_hls_hd_dir(sess), 1)

    assert S._window_hls_hd_ready(sess) is True
