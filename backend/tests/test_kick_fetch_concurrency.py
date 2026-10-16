"""Kick list+clue run concurrently, not serially.

Measured 2026-10-16, live, unloaded:
  kick list_channel_videos_sync    1537 ms  -> 25 items
  kick get_channel_language_sync   1473 ms  -> None

The route awaited them serially (~3.0 s for a clue that came back empty).
The clue needs only the slug, never the list - same shape YouTube runs
concurrently. Two 0.4 s sleeps must cost ~0.4 s wall, not ~0.8 s.

RED ARM: await the two Kick service calls serially again and this test fails
with wall time near twice the bound.
"""
import time
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client():
    from app import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_kick_list_and_clue_run_concurrently(client, monkeypatch):
    from routers import channels as ch
    def slow_list(url, limit):
        time.sleep(0.4)
        return [{"id": "k%d" % i, "title": "t"} for i in range(3)]
    def slow_clue(slug):
        time.sleep(0.4)
        return None
    monkeypatch.setattr(ch, "kick_list_channel_videos_sync", slow_list)
    monkeypatch.setattr(ch, "kick_get_channel_language_sync", slow_clue)
    url = "/api/channel/videos?url=kick-concurrency-probe&limit=25&page=1&days=0&platforms=Kick&content=vods&kick_slug=kick-concurrency-probe&force=1"
    t0 = time.perf_counter()
    r = await client.get(url)
    wall = time.perf_counter() - t0
    assert r.status_code == 200, r.text[:200]
    assert len(r.json().get("videos") or []) == 3
    assert wall < 0.65, "Kick list+clue took %.2fs - serial again" % wall
