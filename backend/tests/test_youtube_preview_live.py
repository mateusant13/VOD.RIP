"""Live YouTube preview smoke — hits real InnerTube + stream proxy (one URL at a time)."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app import app

SHORTS = [
    "https://www.youtube.com/shorts/IbkQI11-NZk",
    "https://www.youtube.com/shorts/t_Or3Oz5LX8",
    "https://www.youtube.com/shorts/KkzZw5ebY0A",
]
WATCH = "https://www.youtube.com/watch?v=4kyvGbRpV7M"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [*SHORTS, WATCH])
async def test_youtube_preview_session_and_stream(url: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60.0) as client:
        create = await client.post(
            "/api/preview/session",
            json={"url": url, "crop_start": 0, "crop_end": 60, "prefer_height": 720},
        )
        if create.status_code == 500:
            detail = (create.json().get("detail") or "").lower()
            if "unavailable" in detail or "try again" in detail:
                pytest.skip(f"YouTube extract blocked: {create.json().get('detail')}")
        assert create.status_code == 200, create.text
        body = create.json()
        sid = body["session_id"]
        kind = body.get("kind")
        playback = body.get("playback_url") or ""
        assert sid and playback, body

        if kind == "progressive":
            stream = await client.get(
                f"/api/preview/hls/{sid}/stream.mp4",
                headers={"Range": "bytes=0-4095"},
            )
        else:
            stream = await client.get(f"/api/preview/hls/{sid}/master.m3u8")

        assert stream.status_code in (200, 206), (
            f"{url} kind={kind} stream={stream.status_code} detail={stream.text[:500]}"
        )
        assert len(stream.content) > 0, f"empty body for {url}"

        qual = await client.post(
            f"/api/preview/session/{sid}/quality",
            json={"prefer_height": 480},
        )
        assert qual.status_code == 200, qual.text

        if kind == "progressive":
            stream2 = await client.get(
                f"/api/preview/hls/{sid}/stream.mp4",
                headers={"Range": "bytes=0-4095"},
            )
        else:
            stream2 = await client.get(
                f"/api/preview/hls/{sid}/master.m3u8",
                params={"prefer_height": 480},
            )
        assert stream2.status_code in (200, 206), (
            f"after quality {url} status={stream2.status_code} {stream2.text[:500]}"
        )

        await client.delete(f"/api/preview/session/{sid}")
