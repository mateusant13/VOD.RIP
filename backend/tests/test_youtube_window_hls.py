"""YouTube DASH window HLS helpers — no network."""
from __future__ import annotations

import tempfile
from pathlib import Path

from services.preview_service import (
    WINDOW_HLS_INITIAL_CHUNK_SEC,
    WINDOW_HLS_LONG_VOD_MIN_SEC,
    WINDOW_HLS_MARKER,
    WINDOW_HLS_MUX_CHUNK_LONG_SEC,
    WINDOW_HLS_MUX_CHUNK_SEC,
    WINDOW_HLS_INIT_RESOURCE,
    WINDOW_HLS_SHORT_VOD_MAX_SEC,
    WINDOW_HLS_PLAYLIST_RESOURCE,
    WINDOW_HLS_SEGMENT_RESOURCE_PREFIX,
    WINDOW_HLS_SEGMENT_SEC,
    PreviewSession,
    _build_youtube_window_hls_master,
    _build_youtube_window_hls_media_playlist,
    _init_window_hls_mux_bounds,
    _position_in_window_hls_mux,
    _register_youtube_window_hls_resources,
    _window_hls_dir,
    _window_hls_mux_bounds,
    _window_hls_seek_chunk_sec,
    _window_hls_playlist_complete,
    _window_hls_seg0_ready,
    _window_hls_segment_path,
    preview_mux_ready,
)


def _sess(crop_end: float = 25.0, cache_dir: Path | None = None) -> PreviewSession:
    return PreviewSession(
        session_id="t",
        vod_url="https://www.youtube.com/watch?v=x",
        platform="YouTube",
        master_url="",
        entry_url="https://example.com/v",
        cache_dir=cache_dir or Path(tempfile.mkdtemp(prefix="window_hls_test_")),
        crop_start=0,
        crop_end=crop_end,
        dash_window_hls=True,
    )


def test_window_hls_short_vod_muxes_full_crop():
    s = _sess(crop_end=30.0)
    lo, hi = _window_hls_mux_bounds(s)
    assert lo == 0.0
    assert hi == 30.0


def test_window_hls_mux_bounds_caps_large_crop():
    s = _sess(crop_end=1158.0)
    lo, hi = _window_hls_mux_bounds(s)
    assert lo == 0.0
    assert hi == WINDOW_HLS_INITIAL_CHUNK_SEC
    slo, shi = _window_hls_mux_bounds(s, around_sec=868.5)
    assert slo < 868.5 < shi
    assert shi - slo <= WINDOW_HLS_MUX_CHUNK_SEC + 0.01
    # Seek remux should put the target near the start of the chunk so the first
    # buffered segment starts close to the requested time (keyframe-aligned).
    assert 0 < 868.5 - slo <= WINDOW_HLS_SEGMENT_SEC + 0.01


def test_window_hls_long_vod_wider_seek_chunk():
    s = _sess(crop_end=7200.0)
    s.vod_duration = WINDOW_HLS_LONG_VOD_MIN_SEC + 60
    assert _window_hls_seek_chunk_sec(s) == WINDOW_HLS_MUX_CHUNK_LONG_SEC
    slo, shi = _window_hls_mux_bounds(s, around_sec=3600.0)
    assert shi - slo <= WINDOW_HLS_MUX_CHUNK_LONG_SEC + 0.01


def test_window_hls_position_in_active_mux_window():
    s = _sess(crop_end=1158.0)
    _init_window_hls_mux_bounds(s)
    assert s.window_hls_mux_start == 0.0
    assert s.window_hls_mux_end == WINDOW_HLS_INITIAL_CHUNK_SEC
    assert _position_in_window_hls_mux(s, WINDOW_HLS_INITIAL_CHUNK_SEC / 2)
    assert not _position_in_window_hls_mux(s, 868.5)
    s.window_hls_mux_start, s.window_hls_mux_end = _window_hls_mux_bounds(s, around_sec=868.5)
    assert _position_in_window_hls_mux(s, 868.5)
    assert not _position_in_window_hls_mux(s, 30.0)


def test_window_hls_master_points_at_playlist_resource():
    s = _sess()
    master = _build_youtube_window_hls_master(s)
    assert master.startswith("#EXTM3U")
    assert WINDOW_HLS_PLAYLIST_RESOURCE in master
    assert f"resource?id={WINDOW_HLS_PLAYLIST_RESOURCE}" in master


def test_window_hls_seg_paths_and_readiness():
    s = _sess()
    assert _window_hls_dir(s).name == "window_hls"
    assert _window_hls_segment_path(s, 0).name == "seg_000.ts"
    assert _window_hls_segment_path(s, 7).name == "seg_007.ts"
    assert not _window_hls_seg0_ready(s)
    seg0 = _window_hls_segment_path(s, 0)
    seg0.parent.mkdir(parents=True, exist_ok=True)
    seg0.write_bytes(b"\x47" * 60_000)
    assert _window_hls_seg0_ready(s)


def test_preflight_mux_adoption():
    import tempfile
    from services.preview_service import (
        _preflight_mux_dir,
        _try_adopt_preflight_mux,
        _window_hls_dir,
    )

    vid = "dQw4w9WgXcQ"
    pre = _preflight_mux_dir(vid, 720)
    pre.mkdir(parents=True, exist_ok=True)
    (pre / "seg_000.ts").write_bytes(b"\x47" * 60_000)
    cache = Path(tempfile.mkdtemp(prefix="preflight_adopt_"))
    s = PreviewSession(
        session_id="pf",
        vod_url=f"https://www.youtube.com/watch?v={vid}",
        platform="YouTube",
        master_url="",
        entry_url="https://example.com/v",
        cache_dir=cache,
        crop_start=0,
        crop_end=120,
        dash_window_hls=True,
        prefer_height=720,
    )
    s.window_hls_mux_start = 0.0
    s.window_hls_mux_end = WINDOW_HLS_INITIAL_CHUNK_SEC
    assert _try_adopt_preflight_mux(s)
    assert (_window_hls_dir(s) / "seg_000.ts").is_file()


    s = _sess(crop_end=20.0)
    out_dir = _window_hls_dir(s)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "seg_000.ts").write_bytes(b"\x47" * 60_000)
    (out_dir / "seg_001.ts").write_bytes(b"\x47" * 50_000)
    (out_dir / "window.m3u8").write_text(
        "#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-TARGETDURATION:5\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-PLAYLIST-TYPE:VOD\n"
        f"#EXTINF:{WINDOW_HLS_SEGMENT_SEC:.3f},\nseg_000.ts\n"
        f"#EXTINF:{WINDOW_HLS_SEGMENT_SEC:.3f},\nseg_001.ts\n#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    body = _build_youtube_window_hls_media_playlist(s)
    text = body.decode("utf-8")
    assert text.startswith("#EXTM3U")
    assert f"{WINDOW_HLS_SEGMENT_RESOURCE_PREFIX}000" in text
    assert f"{WINDOW_HLS_SEGMENT_RESOURCE_PREFIX}001" in text
    assert "#EXT-X-ENDLIST" in text
    assert preview_mux_ready(s)
    assert _window_hls_playlist_complete(s)
    _register_youtube_window_hls_resources(s)
    assert s.resource_map[WINDOW_HLS_PLAYLIST_RESOURCE]
    assert f"{WINDOW_HLS_SEGMENT_RESOURCE_PREFIX}000" in s.resource_map


def test_fmp4_llhls_playlist_emits_cmaf_tags():
    """When USE_FMP4 is on, the media playlist is LL-HLS (v9) fMP4."""
    s = _sess(crop_end=20.0)
    out_dir = _window_hls_dir(s)
    out_dir.mkdir(parents=True, exist_ok=True)
    # fMP4 init + two CMAF segments on disk.
    (out_dir / "init.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")
    (out_dir / "seg_000.m4s").write_bytes(b"\x00\x00\x00\x18moof")
    (out_dir / "seg_001.m4s").write_bytes(b"\x00\x00\x00\x18moof")
    # Mark the mux complete so the playlist carries EXT-X-ENDLIST.
    (out_dir / "window.m3u8").write_text(
        "#EXTM3U\n#EXT-X-ENDLIST\n", encoding="utf-8"
    )
    body = _build_youtube_window_hls_media_playlist(s)
    text = body.decode("utf-8")
    assert "#EXT-X-VERSION:9" in text
    assert "#EXT-X-MAP:URI=" in text and "init.mp4" in text
    assert "#EXT-X-PART-INF:PART-TARGET=" in text
    assert "#EXT-X-SERVER-CONTROL:" in text
    assert "#EXT-X-PART:" in text
    assert "#EXT-X-PRELOAD-HINT:" in text
    assert f"{WINDOW_HLS_SEGMENT_RESOURCE_PREFIX}000" in text
    assert "#EXT-X-ENDLIST" in text


def test_fmp4_resource_registration_maps_init_and_m4s():
    """Resource map registers window-init and .m4s segment entries under fMP4."""
    s = _sess(crop_end=20.0)
    out_dir = _window_hls_dir(s)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "init.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")
    (out_dir / "seg_000.m4s").write_bytes(b"\x00\x00\x00\x18moof")
    (out_dir / "seg_001.m4s").write_bytes(b"\x00\x00\x00\x18moof")
    _register_youtube_window_hls_resources(s)
    assert s.resource_map[WINDOW_HLS_INIT_RESOURCE] == f"{WINDOW_HLS_MARKER}init.mp4"
    rid0 = f"{WINDOW_HLS_SEGMENT_RESOURCE_PREFIX}000"
    rid1 = f"{WINDOW_HLS_SEGMENT_RESOURCE_PREFIX}001"
    assert s.resource_map[rid0] == f"{WINDOW_HLS_MARKER}seg_000.m4s"
    assert s.resource_map[rid1] == f"{WINDOW_HLS_MARKER}seg_001.m4s"

