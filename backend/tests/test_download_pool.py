"""Download pool tests — I/O pool creation, YouTube/Twitch+Kick prefetch,
and chunk-size contract.

All network/download functions are monkeypatched: no real yt-dlp, ffmpeg, or
HLS fetches.  Tests verify the pool plumbing only.
"""

from __future__ import annotations

import sys
from concurrent.futures import Future
from pathlib import Path

import pytest

from services import archive_transcribe as at  # noqa: E402
from services import archive_ytdlp  # noqa: E402

# Skip pool tests if the download pool hasn't been implemented yet.
_has_pool = hasattr(at, "_get_download_pool")
_skip_no_pool = pytest.mark.skipif(not _has_pool, reason="download pool not implemented yet")


# ---------------------------------------------------------------------------
# 1. Pool creation
# ---------------------------------------------------------------------------

@_skip_no_pool
def test_get_download_pool_creates_with_four_workers():
    """_get_download_pool() returns a ThreadPoolExecutor with _DOWNLOAD_POOL_SIZE=4."""
    pool = at._get_download_pool()
    assert pool._max_workers in (4, 8), (
        f"download pool must have 4 (legacy) or 8 (governor keep-pool), got {pool._max_workers}"
    )


@_skip_no_pool
def test_get_download_pool_is_singleton():
    """Calling _get_download_pool() twice returns the same instance."""
    pool1 = at._get_download_pool()
    pool2 = at._get_download_pool()
    assert pool1 is pool2, "pool must be a module-level singleton"


# ---------------------------------------------------------------------------
# 2. YouTube prefetch
# ---------------------------------------------------------------------------

@_skip_no_pool
def test_prefetch_youtube_audio_stashes_path(monkeypatch):
    """_prefetch_youtube_audio() calls download_bestaudio and stashes the path."""
    fake_path = Path("/tmp/fake-youtube.webm")

    def _fake_download(video_id, outdir, **kw):
        return fake_path

    # download_bestaudio is imported inside the function from archive_ytdlp
    monkeypatch.setattr(archive_ytdlp, "download_bestaudio", _fake_download)

    stash: dict = {}
    result = at._prefetch_youtube_audio("test123", stash)
    assert result == fake_path, f"expected {fake_path}, got {result}"
    assert stash.get("wav") == fake_path, "wav must be stashed for transcribe"


@_skip_no_pool
def test_prefetch_youtube_audio_stashes_dir(monkeypatch):
    """_prefetch_youtube_audio() stashes the temp dir for cleanup."""
    fake_path = Path("/tmp/fake-youtube.webm")

    def _fake_download(video_id, outdir, **kw):
        return fake_path

    monkeypatch.setattr(archive_ytdlp, "download_bestaudio", _fake_download)

    stash: dict = {}
    at._prefetch_youtube_audio("test456", stash)
    assert "dir" in stash, "temp dir must be stashed"
    assert isinstance(stash["dir"], Path)


# ---------------------------------------------------------------------------
# 3. Twitch/Kick prefetch
# ---------------------------------------------------------------------------

@_skip_no_pool
def test_prefetch_twitch_kick_audio_routes_twitch(monkeypatch):
    """_prefetch_twitch_kick_audio() resolves channel from DB and calls _fetch_remote_audio_wav."""
    called_with: dict = {}

    def _fake_fetch(platform, video_id, channel, out_wav):
        called_with["platform"] = platform
        called_with["video_id"] = video_id
        called_with["channel"] = channel
        out_wav.write_bytes(b"RIFFfake")
        return out_wav

    monkeypatch.setattr(at, "_fetch_remote_audio_wav", _fake_fetch)

    # Stub DB query to return a channel name
    monkeypatch.setattr(at.archive_db, "query", lambda sql, params=(): [{"channel": "shroud"}])

    stash: dict = {}
    result = at._prefetch_twitch_kick_audio("twitch", "tw-456", stash)
    assert called_with["platform"] == "twitch"
    assert called_with["video_id"] == "tw-456"
    assert isinstance(result, Path)
    assert "wav" in stash, "wav must be stashed for transcribe"


@_skip_no_pool
def test_prefetch_twitch_kick_audio_routes_kick(monkeypatch):
    """_prefetch_twitch_kick_audio() works for Kick platform too."""
    called_with: dict = {}

    def _fake_fetch(platform, video_id, channel, out_wav):
        called_with["platform"] = platform
        return out_wav

    monkeypatch.setattr(at, "_fetch_remote_audio_wav", _fake_fetch)
    monkeypatch.setattr(at.archive_db, "query", lambda sql, params=(): [{"channel": "xqc"}])

    stash: dict = {}
    at._prefetch_twitch_kick_audio("kick", "kick-789", stash)
    assert called_with["platform"] == "kick"


# ---------------------------------------------------------------------------
# 4. Prefetch integration: pool.submit returns Future
# ---------------------------------------------------------------------------

@_skip_no_pool
def test_download_pool_submit_returns_future(monkeypatch):
    """Submitting to the download pool returns a Future."""
    fake_path = Path("/tmp/pool-test.webm")

    def _fake_download(video_id, outdir, **kw):
        return fake_path

    monkeypatch.setattr(archive_ytdlp, "download_bestaudio", _fake_download)

    pool = at._get_download_pool()
    future = pool.submit(at._prefetch_youtube_audio, "pool-test", {})
    try:
        assert isinstance(future, Future)
        result = future.result(timeout=5)
        assert result == fake_path
    finally:
        future.cancel()


# ---------------------------------------------------------------------------
# 5. Chunk-size contract
# ---------------------------------------------------------------------------

def test_max_chunk_sec_is_60():
    """_MAX_CHUNK_SEC is 60.0 — parakeet TDT v3 halves chunk overhead."""
    assert at._MAX_CHUNK_SEC == 60.0, (
        f"_MAX_CHUNK_SEC must be 60.0, got {at._MAX_CHUNK_SEC}"
    )
