"""Tests for the Twitch clip editor-open + history endpoints.

Semi-automatic flow: POST /api/twitch/clip records the editor-open into
<data_dir>/twitch_clips.json and (optionally) opens the URL in the default
browser. Tests use open_browser=false and an isolated VODRIP_DATA_DIR so the
real user data is never touched and nothing is opened.
"""
import json
import os
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

from app import app


@pytest.fixture()
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path))
    return tmp_path


async def _post(client, body):
    return await client.post("/api/twitch/clip", json=body)


async def _history(client):
    res = await client.get("/api/twitch/clips/history")
    assert res.status_code == 200
    return res.json()


@pytest.mark.anyio
async def test_vod_clip_builds_url_and_records_history(_isolated_data_dir):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _post(client, {
            "broadcaster_login": "surtepi",
            "vod_id": "2536167775",
            "offset_sec": 434,
            "duration_sec": 30,
            "open_browser": False,
        })
        assert res.status_code == 200
        body = res.json()
        assert body["url"] == (
            "https://clips.twitch.tv/create?vodID=2536167775"
            "&broadcasterLogin=surtepi&offsetSeconds=434"
        )
        rows = await _history(client)
        assert len(rows) == 1
        assert rows[0]["channel"] == "surtepi"
        assert rows[0]["status"] == "editor_opened"
        # persisted on disk, newest first
        stored = json.loads(
            (_isolated_data_dir / "twitch_clips.json").read_text("utf-8")
        )
        assert stored[0]["id"] == rows[0]["id"]


@pytest.mark.anyio
async def test_live_clip_omits_vod_params(_isolated_data_dir):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _post(client, {"broadcaster_login": "surtepi", "open_browser": False})
        assert res.status_code == 200
        assert res.json()["url"] == (
            "https://clips.twitch.tv/create?broadcasterLogin=surtepi"
        )


@pytest.mark.anyio
async def test_duration_out_of_range_rejected(_isolated_data_dir):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for bad in (4, 0, 61):
            res = await _post(client, {
                "broadcaster_login": "surtepi",
                "vod_id": "2536167775",
                "offset_sec": 434,
                "duration_sec": bad,
                "open_browser": False,
            })
            assert res.status_code == 422
            assert "duration_sec must be 5..60" in res.json()["detail"]
        rows = await _history(client)
        assert rows == []


@pytest.mark.anyio
async def test_duration_at_min_boundary_accepted(_isolated_data_dir):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _post(client, {
            "broadcaster_login": "surtepi",
            "vod_id": "2536167775",
            "offset_sec": 434,
            "duration_sec": 5,
            "open_browser": False,
        })
        assert res.status_code == 200


@pytest.mark.anyio
async def test_bad_login_rejected(_isolated_data_dir):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _post(client, {"broadcaster_login": "not a login!", "open_browser": False})
        assert res.status_code == 422
        rows = await _history(client)
        assert rows == []
