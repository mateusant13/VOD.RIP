"""Integration tests for API endpoints using FastAPI TestClient.

Tests real HTTP requests against the running app (no mocks).
"""
import json
import os
import tempfile
import time
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

from app import app
from deps import settings_mgr, download_mgr
from models.schemas import AppSettings
from services.download_manager import DownloadManager


@pytest.fixture(autouse=True)
def _reset_settings():
    """Reset settings to a clean temp state before each test."""
    # Create temp file in same directory as target to avoid cross-drive issues
    original_file = settings_mgr._settings_file
    original_dir = original_file.parent
    original_dir.mkdir(parents=True, exist_ok=True)
    temp_file = original_dir / f"settings_test_{os.getpid()}.json"
    settings_mgr._settings_file = temp_file
    settings_mgr._settings = AppSettings()
    yield
    settings_mgr._settings_file = original_file
    if temp_file.exists():
        temp_file.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _clean_download_manager():
    """Clean download manager before and after each test to avoid polluting UI."""
    # Clean before
    download_mgr.cancel_all()
    time.sleep(0.1)
    state = download_mgr.get_active_and_history()
    for d in state["queue"]:
        download_mgr.discard_from_queue(d.download_id)
    for d in state["history"]:
        download_mgr.remove_history(d.download_id)
    for d in state["recent"]:
        download_mgr.remove_history(d.download_id)
    yield
    # Clean after
    download_mgr.cancel_all()
    time.sleep(0.1)
    state = download_mgr.get_active_and_history()
    for d in state["queue"]:
        download_mgr.discard_from_queue(d.download_id)
    for d in state["history"]:
        download_mgr.remove_history(d.download_id)
    for d in state["recent"]:
        download_mgr.remove_history(d.download_id)


@pytest.fixture
async def client():
    """Create an AsyncClient for testing the API."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestSettingsAPI:
    """Tests for /api/settings endpoints."""

    @pytest.mark.asyncio
    async def test_get_settings(self, client):
        resp = await client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "download_threads" in data
        assert "download_folder" in data
        assert "quality" in data

    @pytest.mark.asyncio
    async def test_update_settings(self, client):
        resp = await client.post("/api/settings", json={"quality": "720p"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["quality"] == "720p"

    @pytest.mark.asyncio
    async def test_update_settings_download_threads(self, client):
        resp = await client.post("/api/settings", json={"download_threads": 8})
        assert resp.status_code == 200
        data = resp.json()
        assert data["download_threads"] == 8

    @pytest.mark.asyncio
    async def test_update_settings_invalid_threads(self, client):
        resp = await client.post("/api/settings", json={"download_threads": 100})
        assert resp.status_code == 200
        data = resp.json()
        assert data["download_threads"] == 16  # clamped to max


class TestInfoAPI:
    """Tests for /api/info/video and /api/info/clip endpoints."""

    @pytest.mark.asyncio
    async def test_info_video_invalid_url(self, client):
        resp = await client.get("/api/info/video?id=not-a-url")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_info_clip_invalid_url(self, client):
        resp = await client.get("/api/info/clip?id=not-a-url")
        assert resp.status_code == 404


class TestChannelsAPI:
    """Tests for /api/channel/videos and /api/channel/clips endpoints."""

    @pytest.mark.asyncio
    async def test_channel_videos_missing_params(self, client):
        resp = await client.get("/api/channel/videos")
        assert resp.status_code == 422  # FastAPI validation error

    @pytest.mark.asyncio
    async def test_channel_clips_missing_params(self, client):
        resp = await client.get("/api/channel/clips")
        assert resp.status_code == 400


class TestPreviewAPI:
    """Tests for /api/preview/* endpoints."""

    @pytest.mark.asyncio
    async def test_preview_session_invalid_range(self, client):
        resp = await client.post(
            "/api/preview/session",
            json={"url": "https://kick.com/test/clip/abc", "crop_start": 10, "crop_end": 5},
        )
        assert resp.status_code == 400
        assert "End must be after start" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_preview_session_missing_url(self, client):
        resp = await client.post(
            "/api/preview/session",
            json={"crop_start": 0, "crop_end": 10},
        )
        assert resp.status_code == 422  # FastAPI validation error


class TestSystemAPI:
    """Tests for /api/system/* endpoints."""

    @pytest.mark.asyncio
    async def test_server_info(self, client):
        resp = await client.get("/api/info")
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert "name" in data

    @pytest.mark.asyncio
    async def test_app_version(self, client):
        resp = await client.get("/api/app/version")
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data

    @pytest.mark.asyncio
    async def test_ytdlp_status(self, client):
        resp = await client.get("/api/ytdlp/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "available" in data
        assert data["available"] is True


class TestDownloadAPI:
    """Tests for /api/download/* endpoints."""

    @pytest.mark.asyncio
    async def test_list_downloads_empty(self, client):
        resp = await client.get("/api/downloads")
        assert resp.status_code == 200
        data = resp.json()
        assert "queue" in data
        assert "history" in data
        assert "recent" in data

    @pytest.mark.asyncio
    async def test_download_video_invalid_url(self, client):
        # Invalid URL is rejected upfront with 400
        resp = await client.post(
            "/api/download/video",
            json={"url": "not-a-url", "quality": "source"},
        )
        assert resp.status_code == 400
        assert "http" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_download_clip_invalid_url(self, client):
        # Invalid URL is rejected upfront with 400
        resp = await client.post(
            "/api/download/clip",
            json={"url": "not-a-url", "quality": "source"},
        )
        assert resp.status_code == 400
        assert "http" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_download_video_missing_url(self, client):
        resp = await client.post("/api/download/video", json={})
        assert resp.status_code == 422  # FastAPI validation error

    @pytest.mark.asyncio
    async def test_cancel_nonexistent(self, client):
        resp = await client.post("/api/download/dl_nonexistent/cancel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cancelled"] is False

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, client):
        resp = await client.get("/api/download/dl_nonexistent")
        assert resp.status_code == 404


class TestDownloadManagerRaceConditions:
    """Test potential race conditions in download manager."""

    def test_start_multiple_downloads_rapidly(self):
        """Test that starting multiple downloads rapidly doesn't mix up URLs."""
        mgr = DownloadManager(max_workers=2)
        ids = []
        for i in range(5):
            dl_id = mgr.start_download(
                url=f"https://twitch.tv/videos/{1_000_000 + i}",
                output_file=rf"C:\tmp\{i}.mp4",
            )
            ids.append(dl_id)

        state = mgr.get_active_and_history()
        queue = state["queue"]
        # Verify each download has the correct URL
        for dl_id, expected_url in zip(
            ids,
            [f"https://twitch.tv/videos/{1_000_000 + i}" for i in range(5)],
        ):
            found = [d for d in queue if d.download_id == dl_id]
            assert len(found) == 1
            assert found[0].url == expected_url

    def test_cancel_specific_download(self):
        """Test that cancelling one download doesn't affect others."""
        mgr = DownloadManager(max_workers=2)
        id1 = mgr.start_download(
            url="https://twitch.tv/videos/1_000_001",
            output_file=r"C:\tmp\1.mp4",
        )
        id2 = mgr.start_download(
            url="https://twitch.tv/videos/1_000_002",
            output_file=r"C:\tmp\2.mp4",
        )

        # Cancel only the first
        result = mgr.cancel(id1)
        assert result is True

        state = mgr.get_active_and_history()
        queue = state["queue"]
        # id1 should be gone, id2 should remain
        assert not any(d.download_id == id1 for d in queue)
        assert any(d.download_id == id2 for d in queue)

    def test_download_state_isolation(self):
        """Test that each download's state is isolated."""
        mgr = DownloadManager(max_workers=2)
        id1 = mgr.start_download(
            url="https://twitch.tv/videos/1_000_001",
            output_file=r"C:\tmp\1.mp4",
            title="Video 1",
            channel="Channel 1",
        )
        id2 = mgr.start_download(
            url="https://twitch.tv/videos/1_000_002",
            output_file=r"C:\tmp\2.mp4",
            title="Video 2",
            channel="Channel 2",
        )

        s1 = mgr.get(id1)
        s2 = mgr.get(id2)

        assert s1.title == "Video 1"
        assert s1.channel == "Channel 1"
        assert s2.title == "Video 2"
        assert s2.channel == "Channel 2"
        assert s1.url != s2.url


if __name__ == "__main__":
    pytest.main([__file__, "-v"])