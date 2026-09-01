"""Self-check for the livestream popup's fast-clip + live-chat endpoints.

Run from the backend directory with:
    python -m tests.test_live_chat_clip

Covers:
- POST /api/live/clip: honest capability report (never fakes a clip),
  per-platform reasons, validation (bad platform → 400, duration out of
  1..60 → 422).
- GET /api/live/chat/stream: validation (bad platform → 400), the per-viewer
  sink factory (right class + viewer-scoped video_id), and the SSE happy path
  with a patched sink that pushes one row (no network).
"""
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app import app


@pytest.mark.anyio
async def test_fast_clip_reports_capability_honestly():
    """Every supported platform answers 200 with available=False + a reason —
    the UI surfaces the gap; no endpoint ever fabricates a clip id."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for platform in ("twitch", "kick", "youtube"):
            res = await client.post(
                "/api/live/clip",
                json={"platform": platform, "slug": "srdogg", "duration_sec": 30},
            )
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["available"] is False
            assert body["reason"]
            assert isinstance(body["needed"], list) and body["needed"]


@pytest.mark.anyio
async def test_fast_clip_rejects_bad_platform_and_duration():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/api/live/clip",
            json={"platform": "facebook", "slug": "x", "duration_sec": 30},
        )
        assert res.status_code == 400

        for bad in (0, -5, 61, 999):
            res = await client.post(
                "/api/live/clip",
                json={"platform": "twitch", "slug": "srdogg", "duration_sec": bad},
            )
            assert res.status_code == 422, (bad, res.text)

        # Default duration is 30 (within range) — no 422.
        res = await client.post(
            "/api/live/clip",
            json={"platform": "kick", "slug": "srdoglol"},
        )
        assert res.status_code == 200
        assert res.json()["available"] is False


def test_viewer_chat_sink_factory():
    """The per-viewer factory builds the RIGHT sink class per platform with a
    viewer-scoped video_id (never an archive id) and a flush callback."""
    from routers import live as live_router

    pushed: list = []

    twitch = live_router._build_viewer_chat_sink("twitch", "srdogg", pushed.append)
    assert twitch.__class__.__name__ == "TwitchIRCSink"
    assert twitch.login == "srdogg"
    assert twitch.video_id.startswith("viewer-twitch-srdogg-")

    kick = live_router._build_viewer_chat_sink("kick", "srdoglol", pushed.append)
    assert kick.__class__.__name__ == "KickPusherSink"
    assert kick.slug == "srdoglol"
    assert kick.video_id.startswith("viewer-kick-srdoglol-")

    yt = live_router._build_viewer_chat_sink("youtube", "@srdogyt", pushed.append)
    assert yt.__class__.__name__ == "YTLiveSink"
    assert yt.handle == "srdogyt"
    assert yt.video_id.startswith("viewer-youtube-@srdogyt-")

    # Flush callback forwards buffered rows (the SSE queue wiring).
    yt.add_row({"username": "x", "text": "hi"})
    yt.flush()
    assert pushed and pushed[0][0]["text"] == "hi"


@pytest.mark.anyio
async def test_chat_stream_validates_platform(monkeypatch):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/api/live/chat/stream", params={"platform": "bogus", "slug": "x"})
        assert res.status_code == 400
        res = await client.get("/api/live/chat/stream", params={"platform": "twitch", "slug": ""})
        assert res.status_code == 400


class _FakeSink:
    """Patched fanout: start() flushes one row into the queue, unsubscribe()
    records the call (no thread, no network — the SSE body is the only thing
    under test). """

    def __init__(self, push):
        self._push = push
        self.stopped = False

    def start(self):
        self._push([{"username": "viewer", "text": "hello live"}])

    def unsubscribe(self, queue):
        self.stopped = True


class _FakeRequest:
    """Request whose disconnect never fires — the generator is closed by the
    test instead (aclose() runs the finally → fanout.unsubscribe())."""

    async def is_disconnected(self):
        return False


@pytest.mark.anyio
async def test_chat_stream_sse_forwarding():
    """The SSE generator starts the fanout, forwards a flushed row batch as a
    data: frame, and unsubscribes when the connection closes."""
    import asyncio

    from routers import live as live_router

    queue: asyncio.Queue = asyncio.Queue()
    sink = _FakeSink(queue.put_nowait)
    gen = live_router._chat_sse_gen(_FakeRequest(), queue, sink)

    try:
        chunk = await asyncio.wait_for(gen.__anext__(), timeout=5)
    finally:
        await gen.aclose()

    assert sink.stopped
    body = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
    assert body.startswith("data: ")
    row = json.loads(body[len("data: "):].strip())
    assert row["username"] == "viewer"
    assert row["text"] == "hello live"
