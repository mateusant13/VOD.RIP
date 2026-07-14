"""Playlist-first preview semantics — no network."""

from __future__ import annotations

from pathlib import Path

from services.preview_service import (
    PreviewSession,
    _build_youtube_window_hls_master,
    _bytes_response_for_range,
    preview_mux_ready,
    preview_playlist_ready,
    preview_segment_buffer_ready,
)


def _window_sess() -> PreviewSession:
    s = PreviewSession(
        session_id="pl",
        vod_url="https://www.youtube.com/watch?v=x",
        platform="YouTube",
        master_url="",
        entry_url="https://example.com/v",
        cache_dir=Path("/tmp"),
        crop_start=0,
        crop_end=30,
        dash_window_hls=True,
    )
    s.custom_master = _build_youtube_window_hls_master(s)
    return s


def test_window_hls_playlist_ready_requires_seg0():
    """dash_window_hls: playlist_ready only when seg0 is on disk (playable)."""
    s = _window_sess()
    assert not preview_playlist_ready(s)
    assert not preview_mux_ready(s)
    assert not preview_segment_buffer_ready(s)
    assert "window-playlist" in (s.custom_master or "")


def test_cached_range_returns_206():
    body, hdrs, status = _bytes_response_for_range(b"0123456789", "bytes=2-5")
    assert status == 206
    assert body == b"2345"
    assert hdrs["Content-Range"] == "bytes 2-5/10"
