"""YouTube DASH window HLS helpers — no network."""
from __future__ import annotations

import tempfile
from pathlib import Path

from services.preview_service import (
    WINDOW_HLS_PLAYLIST_RESOURCE,
    WINDOW_HLS_SEGMENT_RESOURCE_PREFIX,
    WINDOW_HLS_SEGMENT_SEC,
    PreviewSession,
    _build_youtube_window_hls_master,
    _build_youtube_window_hls_media_playlist,
    _register_youtube_window_hls_resources,
    _window_hls_dir,
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


def test_window_hls_media_playlist_from_disk():
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
