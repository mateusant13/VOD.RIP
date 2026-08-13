"""Instant previews — generator pass + /api/previews endpoints.

Generator tests inject fake latest-VOD fetchers + a fake range download and
assert the 6s slice is requested and files land in the previews dir; the
failure path skips gracefully. Endpoint tests exercise the real contract:
status shape (incl. empty), media bytes, and Range -> 206 with the exact
slice. All file I/O is pinned to scratch via VODRIP_DATA_DIR (data_dir()
re-reads the env at call time) — no monkeypatching of module internals.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

os.environ["VODRIP_ARCHIVE_DB"] = str(
    Path(tempfile.mkdtemp(prefix="instant-preview-")) / "archive.db")
os.environ["VODRIP_APP_DATA"] = str(Path(tempfile.mkdtemp(prefix="instant-preview-app-")))
os.environ["VODRIP_DATA_DIR"] = str(Path(tempfile.mkdtemp(prefix="instant-preview-data-")))
os.environ["VODRIP_CACHE_DIR"] = str(Path(tempfile.mkdtemp(prefix="instant-preview-cache-")))

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app import app  # noqa: E402
from services import instant_preview, yt_gate  # noqa: E402

_MP4_MAGIC = b"\x00\x00\x00\x18ftypmp42"


def _channel(cid="ch1", twitch="", kick="", youtube=""):
    return {
        "id": cid,
        "displayName": cid,
        "kickSlug": kick,
        "twitchSlug": twitch,
        "youtubeSlug": youtube,
    }


def _twitch_vod(vid="1234567890", title="Latest Stream"):
    return {
        "platform": "twitch", "title": title,
        "vod_url": f"https://www.twitch.tv/videos/{vid}",
        "vod_id": vid, "video_id": None, "duration_sec": 3600,
    }


@pytest.fixture()
def _previews(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path))
    return tmp_path / "previews"


def _fake_download(files: dict):
    def _dl(url, output_path, start_sec, end_sec):
        assert (start_sec, end_sec) == (0.0, instant_preview.PREVIEW_DURATION_SEC)
        files.setdefault("urls", []).append((url, start_sec, end_sec))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(_MP4_MAGIC + b"\x00" * 1024)
    return _dl


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def test_pick_platform_prefers_twitch_then_kick_then_youtube():
    assert instant_preview.pick_platform(_channel(twitch="t1", kick="k1", youtube="y1")) == "twitch"
    assert instant_preview.pick_platform(_channel(kick="k1", youtube="y1")) == "kick"
    assert instant_preview.pick_platform(_channel(youtube="y1")) == "youtube"
    assert instant_preview.pick_platform(_channel(twitch="  ")) is None
    assert instant_preview.pick_platform(_channel()) is None


def test_pass_requests_first_six_seconds_and_writes_files(_previews, monkeypatch):
    calls = []

    def _dl(url, output_path, start_sec, end_sec):
        calls.append((url, start_sec, end_sec))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(_MP4_MAGIC + b"\x00" * 512)

    monkeypatch.setattr(instant_preview, "_fetch_latest_vod", lambda ch, p: _twitch_vod())
    monkeypatch.setattr(instant_preview, "_download_range", _dl)

    instant_preview.run_pass([_channel(twitch="t1", kick="k1")])

    # Twitch wins over Kick, and only the first 6s are requested.
    assert calls == [("https://www.twitch.tv/videos/1234567890", 0.0, 6.0)]
    assert (_previews / "ch1.mp4").is_file()
    sidecar = json.loads((_previews / "ch1.json").read_text("utf-8"))
    assert sidecar["platform"] == "twitch"
    assert sidecar["title"] == "Latest Stream"
    assert sidecar["vod_url"] == "https://www.twitch.tv/videos/1234567890"
    assert sidecar["vod_id"] == "1234567890"
    assert sidecar["video_id"] is None
    assert sidecar["duration_sec"] == 6.0
    assert "generated_at" in sidecar


def test_pass_skips_unchanged_vod(_previews, monkeypatch):
    downloads = []

    def _dl(url, output_path, start_sec, end_sec):
        downloads.append(url)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(_MP4_MAGIC + b"\x00" * 256)

    monkeypatch.setattr(instant_preview, "_fetch_latest_vod", lambda ch, p: _twitch_vod())
    monkeypatch.setattr(instant_preview, "_download_range", _dl)

    instant_preview.run_pass([_channel(twitch="t1")])
    instant_preview.run_pass([_channel(twitch="t1")])
    assert len(downloads) == 1, "same VOD must not re-download on the next pass"


def test_pass_failure_skips_channel_and_continues(_previews, monkeypatch):
    def _fetch(channel, platform):
        if channel["id"] == "bad":
            raise RuntimeError("GQL 429")
        return _twitch_vod(vid="999", title="ok")

    downloads = []

    def _dl(url, output_path, start_sec, end_sec):
        downloads.append(url)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(_MP4_MAGIC + b"\x00" * 128)

    monkeypatch.setattr(instant_preview, "_fetch_latest_vod", _fetch)
    monkeypatch.setattr(instant_preview, "_download_range", _dl)

    instant_preview.run_pass([_channel("bad", twitch="t1"), _channel("good", twitch="t2")])

    assert downloads == ["https://www.twitch.tv/videos/999"]
    assert not (_previews / "bad.mp4").exists(), "failed channel must be skipped silently"
    assert (_previews / "good.mp4").exists()


def test_pass_download_failure_removes_stale_sidecar(_previews, monkeypatch):
    # A preview exists; the refresh for the SAME channel fails mid-download —
    # the stale sidecar must not keep advertising a media_url that 404s.
    (_previews).mkdir(parents=True, exist_ok=True)
    (_previews / "ch1.mp4").write_bytes(_MP4_MAGIC)
    (_previews / "ch1.json").write_text(json.dumps({"platform": "twitch"}), encoding="utf-8")

    monkeypatch.setattr(instant_preview, "_fetch_latest_vod", lambda ch, p: _twitch_vod())

    def _fail(url, output_path, start_sec, end_sec):
        raise RuntimeError("ffmpeg failed")

    monkeypatch.setattr(instant_preview, "_download_range", _fail)
    instant_preview.run_pass([_channel(twitch="t1")])  # must not raise
    assert not (_previews / "ch1.json").exists()


def test_pass_respects_youtube_gate(_previews, monkeypatch):
    yt_hits = []

    def _fetch(channel, platform):
        yt_hits.append(platform)
        return {
            "platform": "youtube", "title": "stream", "vod_url": "https://www.youtube.com/watch?v=ABCDEFGHIJK",
            "vod_id": None, "video_id": "ABCDEFGHIJK", "duration_sec": 7200,
        }

    monkeypatch.setattr(instant_preview, "_fetch_latest_vod", _fetch)
    monkeypatch.setattr(instant_preview, "_download_range", _fake_download({}))

    yt_gate.note_youtube_gate("test arm", freeze_sec=60)
    try:
        instant_preview.run_pass([_channel(youtube="y1")])
        assert yt_hits == [], "YouTube must not be hit while the bot-gate freeze is active"
        assert not (_previews / "ch1.mp4").exists()
    finally:
        yt_gate.clear_youtube_gate()


def test_pass_skips_channel_without_any_slug(_previews, monkeypatch):
    hits = []
    monkeypatch.setattr(instant_preview, "_fetch_latest_vod", lambda ch, p: hits.append(p))
    instant_preview.run_pass([_channel(), _channel(twitch="t1")])
    assert hits == ["twitch"], "no platform fetch may happen for a slug-less channel"


def test_remove_channel_previews_deletes_files(_previews):
    (_previews).mkdir(parents=True, exist_ok=True)
    (_previews / "ch1.mp4").write_bytes(_MP4_MAGIC)
    (_previews / "ch1.json").write_text("{}", encoding="utf-8")
    instant_preview.remove_channel_previews("ch1")
    assert not (_previews / "ch1.mp4").exists()
    assert not (_previews / "ch1.json").exists()
    instant_preview.remove_channel_previews("")  # no-op, never raises


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_previews_status_empty_when_no_files(_previews):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/previews/status")
    assert resp.status_code == 200
    assert resp.json() == {"previews": []}


@pytest.mark.asyncio
async def test_previews_status_shape_and_media_range(_previews):
    (_previews).mkdir(parents=True, exist_ok=True)
    body = bytes(range(256)) * 4  # 1024 bytes, deterministic
    (_previews / "ch1.mp4").write_bytes(body)
    (_previews / "ch1.json").write_text(json.dumps({
        "platform": "kick",
        "title": "VOD da semana",
        "vod_url": "https://kick.com/gaveta/videos/11111111-2222-3333-4444-555555555555",
        "vod_id": "11111111-2222-3333-4444-555555555555",
        "video_id": None,
        "generated_at": "2026-08-13T00:00:00Z",
        "duration_sec": 6.0,
    }), encoding="utf-8")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/previews/status")
    assert resp.status_code == 200
    previews = resp.json()["previews"]
    assert len(previews) == 1
    p = previews[0]
    assert p["channel_id"] == "ch1"
    assert p["platform"] == "kick"
    assert p["title"] == "VOD da semana"
    assert p["vod_url"] == "https://kick.com/gaveta/videos/11111111-2222-3333-4444-555555555555"
    assert p["vod_id"] == "11111111-2222-3333-4444-555555555555"
    assert p["video_id"] is None
    assert p["media_url"] == "/api/previews/ch1/media"
    assert p["generated_at"] == "2026-08-13T00:00:00Z"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        full = await client.get("/api/previews/ch1/media")
    assert full.status_code == 200
    assert full.headers["content-type"].startswith("video/mp4")
    assert full.content == body
    assert full.headers["accept-ranges"] == "bytes"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rng = await client.get("/api/previews/ch1/media", headers={"range": "bytes=100-199"})
    assert rng.status_code == 206
    assert rng.content == body[100:200]
    assert rng.headers["content-range"] == f"bytes 100-199/{len(body)}"
    assert int(rng.headers["content-length"]) == 100

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        open_ended = await client.get("/api/previews/ch1/media", headers={"range": "bytes=1000-"})
    assert open_ended.status_code == 206
    assert open_ended.content == body[1000:]
    assert open_ended.headers["content-range"] == f"bytes 1000-1023/{len(body)}"


@pytest.mark.asyncio
async def test_previews_status_omits_sidecar_without_mp4(_previews):
    (_previews).mkdir(parents=True, exist_ok=True)
    (_previews / "ghost.json").write_text(json.dumps({
        "platform": "twitch", "title": "x", "vod_url": "u", "vod_id": "1",
        "video_id": None, "generated_at": "2026-08-13T00:00:00Z", "duration_sec": 6.0,
    }), encoding="utf-8")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/previews/status")
    assert resp.status_code == 200
    assert resp.json() == {"previews": []}


@pytest.mark.asyncio
async def test_previews_media_404_for_unknown_channel(_previews):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/previews/nope/media")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_settings_update_removes_previews_of_removed_channels(_previews):
    (_previews).mkdir(parents=True, exist_ok=True)
    (_previews / "gone.mp4").write_bytes(_MP4_MAGIC)
    (_previews / "gone.json").write_text("{}", encoding="utf-8")
    (_previews / "kept.mp4").write_bytes(_MP4_MAGIC)
    (_previews / "kept.json").write_text("{}", encoding="utf-8")

    ch = lambda cid: {"id": cid, "displayName": cid, "twitchSlug": cid}  # noqa: E731
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/api/settings", json={"saved_channels": [ch("gone"), ch("kept")]})
        assert first.status_code == 200
        # Removing "gone" from the saved list must delete its preview files.
        second = await client.post("/api/settings", json={"saved_channels": [ch("kept")]})
    assert second.status_code == 200
    assert not (_previews / "gone.mp4").exists()
    assert not (_previews / "gone.json").exists()
    assert (_previews / "kept.mp4").exists(), "saved channel previews must survive"
