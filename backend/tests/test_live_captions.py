"""Self-check for the live-captions pipeline (real-time ASR captions).

Run from the backend directory with:
    python -m pytest tests/test_live_captions.py -q

Covers:
- HLS parsing: audio-only rendition pick from the master (Twitch audio_only
  group + Kick-style generic audio + STREAM-INF fallback) and media-playlist
  segment parsing (EXTINF / PROGRAM-DATE-TIME / ENDLIST).
- The LiveCaptioner loop with mocked playlist/segment fetch + stubbed parakeet
  decode: blocks assemble in order, the window rolls, the seen-set skips
  re-downloads, refcount start/stop, offline event, gate 503.
- The parakeet wiring itself (VAD regions -> _transcribe_batch_parakeet ->
  concatenated text) with the ASR functions stubbed.
- SSE endpoint shape via the existing router-test pattern (ASGITransport for
  validation/gate, direct generator drive for frame forwarding + release).
"""
import asyncio
import json

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from app import app

MASTER_WITH_AUDIO = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio_only",NAME="Audio Only",DEFAULT=YES,AUTOSELECT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=6000000,AVERAGE-BANDWIDTH=5200000,CODECS="avc1.4D401F,mp4a.40.2",RESOLUTION=1920x1080,FRAME-RATE=60.000,AUDIO="audio_only"
video.m3u8
"""

MASTER_NO_AUDIO = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720
video-720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080
video-1080.m3u8
"""

PLAYLIST_1 = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:1.0,
seg1.ts
"""

PLAYLIST_2 = PLAYLIST_1 + """#EXTINF:1.0,
seg2.ts
"""

PLAYLIST_3 = PLAYLIST_2 + """#EXTINF:1.0,
seg3.ts
"""

PLAYLIST_4 = PLAYLIST_3 + """#EXTINF:1.0,
seg4.ts
"""


# ---------------------------------------------------------------------------
# HLS parsing
# ---------------------------------------------------------------------------


def test_parse_master_picks_audio_only_rendition():
    from services.live_captions import _parse_master_audio_url

    url = _parse_master_audio_url(MASTER_WITH_AUDIO, "https://edge/master.m3u8")
    assert url == "https://edge/audio.m3u8"


def test_parse_master_falls_back_to_first_variant_without_audio():
    from services.live_captions import _parse_master_audio_url

    url = _parse_master_audio_url(MASTER_NO_AUDIO, "https://edge/master.m3u8")
    assert url == "https://edge/video-720.m3u8"


def test_parse_master_prefers_audio_only_marker_over_generic_audio():
    from services.live_captions import _parse_master_audio_url

    master = (
        "#EXTM3U\n"
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Audio",URI="generic-audio.m3u8"\n'
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio_only",NAME="Audio Only",URI="audio-only.m3u8"\n'
        "#EXT-X-STREAM-INF:BANDWIDTH=6000000\nvideo.m3u8\n"
    )
    url = _parse_master_audio_url(master, "https://edge/master.m3u8")
    assert url == "https://edge/audio-only.m3u8"


def test_parse_media_playlist_segments_with_pdt_and_endlist():
    from services.live_captions import _parse_media_playlist

    text = (
        "#EXTM3U\n"
        "#EXT-X-TARGETDURATION:2\n"
        '#EXT-X-PROGRAM-DATE-TIME:2026-08-13T12:00:00.000Z\n'
        "#EXTINF:2.0,\n"
        "seg1.ts\n"
        "#EXTINF:1.5,\n"
        "https://edge/seg2.ts?token=abc\n"
        "#EXT-X-ENDLIST\n"
    )
    segments, is_live = _parse_media_playlist(text, "https://edge/media.m3u8")
    assert is_live is False
    assert len(segments) == 2
    assert segments[0]["uri"] == "https://edge/seg1.ts"
    assert segments[0]["dur"] == 2.0
    assert segments[0]["pdt"] == pytest.approx(1786622400.0)
    assert segments[1]["uri"] == "https://edge/seg2.ts?token=abc"
    assert segments[1]["dur"] == 1.5


def test_parse_media_playlist_ignores_ll_hls_parts():
    from services.live_captions import _parse_media_playlist

    text = (
        "#EXTM3U\n"
        "#EXT-X-TARGETDURATION:2\n"
        "#EXT-X-PART-INF:PART-TARGET=0.33334\n"
        "#EXTINF:1.0,\n"
        "seg1.ts\n"
        '#EXT-X-PART:DURATION=0.33334,URI="seg1.ts?part=1"\n'
        "#EXT-X-PART:DURATION=0.33334,URI=\"seg1.ts?part=2\"\n"
    )
    segments, is_live = _parse_media_playlist(text, "https://edge/media.m3u8")
    assert len(segments) == 1  # parts are NOT segments
    assert segments[0]["uri"] == "https://edge/seg1.ts"


# ---------------------------------------------------------------------------
# Captioner loop (mocked fetch + stubbed parakeet decode)
# ---------------------------------------------------------------------------


class _FakePipeline:
    """Scripted network + decode + transcribe stubs for the captioner loop."""

    def __init__(self, playlists, window_texts):
        self.playlists = list(playlists)  # media playlist texts, one per poll
        self.window_texts = list(window_texts)  # transcript for each flushed window
        self.segment_fetches: list[str] = []
        self.master_url = "https://edge/master.m3u8"
        self.media_url = "https://edge/audio.m3u8"
        self.offline_after = None  # resolver returns None when set

    def resolve_master(self, platform, channel):
        if self.offline_after is not None:
            return None
        return {"url": self.master_url, "headers": {"Referer": "https://edge/"}}

    def fetch(self, url, headers):
        if url == self.master_url:
            return MASTER_WITH_AUDIO.encode()
        if url == self.media_url:
            # One playlist text per poll; the last one repeats forever.
            poll = getattr(self, "_media_polls", 0)
            self._media_polls = poll + 1
            idx = min(poll, len(self.playlists) - 1)
            return self.playlists[idx].encode()
        self.segment_fetches.append(url)
        return b"fake-ts-bytes"

    def decode(self, data):
        return np.zeros(16000, dtype=np.float32)  # exactly 1s of audio

    def transcribe_window(self, audio, duration):
        return self.window_texts.pop(0) if self.window_texts else ""


def _install_pipeline(monkeypatch, pipeline: _FakePipeline, **kw):
    from services import live_captions

    monkeypatch.setattr(live_captions, "_resolve_live_master", pipeline.resolve_master)
    monkeypatch.setattr(live_captions, "_fetch", pipeline.fetch)
    monkeypatch.setattr(live_captions, "_decode_audio_bytes", pipeline.decode)
    monkeypatch.setattr(live_captions, "_transcribe_window", pipeline.transcribe_window)
    return live_captions


async def _wait_event(queue, timeout=5.0):
    return await asyncio.wait_for(queue.get(), timeout)


@pytest.mark.anyio
async def test_captioner_loop_blocks_in_order_and_seen_set_skips_redownloads(monkeypatch):
    """Two 1s segments fill the 1.5s window -> one caption per roll, in order,
    and the seen-set keeps later polls from re-fetching already-consumed
    segments."""
    pipeline = _FakePipeline([PLAYLIST_1, PLAYLIST_2, PLAYLIST_3, PLAYLIST_4], ["window-1", "window-2"])
    live_captions = _install_pipeline(monkeypatch, pipeline)

    loop = asyncio.get_running_loop()
    captioner = live_captions.LiveCaptioner(
        "twitch", "srdogg", loop, window_sec=1.5, poll_sec=0.02,
    )
    captioner.acquire()
    try:
        ev1, block1 = await _wait_event(captioner.events)
        assert ev1 == "caption"
        assert block1["text"] == "window-1"
        # window start/end are stream-relative (no PDT in the fixture playlists)
        assert block1["start"] == 0.0
        assert block1["end"] == 2.0

        ev2, block2 = await _wait_event(captioner.events)
        assert ev2 == "caption"
        assert block2["text"] == "window-2"
        # monotonic: the second window begins where the first ended
        assert block2["start"] >= block1["end"]
        assert block2["end"] > block2["start"]

        # Let a few more polls run: the seen-set must keep segment fetches at
        # exactly the 4 distinct segments despite repeated playlist polls.
        await asyncio.sleep(0.25)
        assert sorted(pipeline.segment_fetches) == [
            "https://edge/seg1.ts",
            "https://edge/seg2.ts",
            "https://edge/seg3.ts",
            "https://edge/seg4.ts",
        ]
    finally:
        captioner.release()
        th = captioner._thread
        assert th is not None
        th.join(timeout=3.0)
        assert not th.is_alive()
    # the worker removed itself from the registry on exit
    assert (captioner.platform, captioner.channel) not in live_captions._CAPTIONERS


@pytest.mark.anyio
async def test_captioner_offline_event_after_consecutive_strikes(monkeypatch):
    pipeline = _FakePipeline([], [])
    pipeline.offline_after = True  # resolver always reports offline
    live_captions = _install_pipeline(monkeypatch, pipeline)

    loop = asyncio.get_running_loop()
    captioner = live_captions.LiveCaptioner(
        "kick", "srdoglol", loop, window_sec=1.5, poll_sec=0.02,
    )
    captioner.acquire()
    try:
        ev, data = await _wait_event(captioner.events)
        assert ev == "offline"
    finally:
        captioner.release()
        th = captioner._thread
        assert th is not None
        th.join(timeout=3.0)
        assert not th.is_alive()


@pytest.mark.anyio
async def test_captioner_surfaces_repeated_asr_failures_as_offline(monkeypatch):
    """A persistently broken ASR engine must NOT leave the SSE alive with
    keepalives and no captions: after _FLUSH_FAIL_LIMIT consecutive flush
    failures the worker emits offline and stops (the failure is surfaced,
    never silent)."""
    long_playlist = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n" + "".join(
        f"#EXTINF:1.0,\nseg{i}.ts\n" for i in range(1, 9)
    )

    class _FailingPipeline(_FakePipeline):
        def transcribe_window(self, audio, duration):
            raise RuntimeError("simulated parakeet failure")

    pipeline = _FailingPipeline([long_playlist], [])
    live_captions = _install_pipeline(monkeypatch, pipeline)

    loop = asyncio.get_running_loop()
    captioner = live_captions.LiveCaptioner(
        "twitch", "srdogg", loop, window_sec=1.5, poll_sec=0.02,
    )
    captioner.acquire()
    try:
        ev, data = await _wait_event(captioner.events)
        assert ev == "offline"
        assert "asr failure" in (data.get("reason") or "")
    finally:
        captioner.release()
        th = captioner._thread
        assert th is not None
        th.join(timeout=3.0)
        assert not th.is_alive()
    # the worker removed itself from the registry on exit
    assert (captioner.platform, captioner.channel) not in live_captions._CAPTIONERS


@pytest.mark.anyio
async def test_captioner_refcount_starts_and_stops_worker(monkeypatch):
    """First acquire starts the worker; releasing back to zero stops it. A
    second acquire restarts a FRESH worker (registry entry reused)."""
    pipeline = _FakePipeline([PLAYLIST_1, PLAYLIST_2], ["window-1"])
    live_captions = _install_pipeline(monkeypatch, pipeline)

    loop = asyncio.get_running_loop()
    captioner = live_captions.get_captioner("twitch", "srdogg2", loop)
    assert captioner._thread is None
    captioner.window_sec = 1.5
    captioner.poll_sec = 0.02
    captioner.acquire()
    assert captioner._thread is not None and captioner._thread.is_alive()
    captioner.release()
    assert captioner._refcount == 0
    captioner._thread.join(timeout=3.0)
    assert not captioner._thread.is_alive()

    # Second subscriber cycle reuses the same instance with a new thread.
    captioner.acquire()
    assert captioner._thread is not None and captioner._thread.is_alive()
    ev, block = await _wait_event(captioner.events)
    assert ev == "caption"
    captioner.release()
    captioner._thread.join(timeout=3.0)


@pytest.mark.anyio
async def test_captioner_recovers_from_transient_fetch_failures(monkeypatch):
    """A playlist fetch that fails once must not kill the loop — the next poll
    succeeds and captions keep flowing."""
    pipeline = _FakePipeline([PLAYLIST_1, PLAYLIST_2, PLAYLIST_3], ["window-1"])
    live_captions = _install_pipeline(monkeypatch, pipeline)
    real_fetch = pipeline.fetch
    fail_next = {"on": True}

    def flaky_fetch(url, headers):
        if url == pipeline.media_url and fail_next["on"]:
            fail_next["on"] = False
            raise RuntimeError("simulated playlist 503")
        return real_fetch(url, headers)

    monkeypatch.setattr(live_captions, "_fetch", flaky_fetch)
    # keep the transient backoff tiny so the recovery poll happens quickly
    monkeypatch.setattr(live_captions, "_BACKOFF_INITIAL_SEC", 0.02)
    monkeypatch.setattr(live_captions, "_BACKOFF_MAX_SEC", 0.1)

    loop = asyncio.get_running_loop()
    captioner = live_captions.LiveCaptioner(
        "twitch", "srdogg", loop, window_sec=1.5, poll_sec=0.02,
    )
    captioner.acquire()
    try:
        ev, block = await _wait_event(captioner.events)
        assert ev == "caption"
        assert block["text"] == "window-1"
    finally:
        captioner.release()
        captioner._thread.join(timeout=3.0)


# ---------------------------------------------------------------------------
# Parakeet wiring (ASR functions stubbed)
# ---------------------------------------------------------------------------


def test_transcribe_window_uses_parakeet_path(monkeypatch):
    """VAD speech regions -> _parakeet_model + _transcribe_batch_parakeet ->
    concatenated text; empty VAD yields no caption text."""
    from services import archive_transcribe as at
    from services import live_captions

    audio = np.zeros(16000 * 3, dtype=np.float32)
    monkeypatch.setattr(at, "vad_speech_seconds", lambda a: [(0.1, 2.9)])
    monkeypatch.setattr(at, "_parakeet_model", lambda: object())
    monkeypatch.setattr(
        at, "_transcribe_batch_parakeet",
        lambda rec, a, chunks, lang: [
            ([{"text": "olá", "start_sec": 0.1, "end_sec": 1.2},
              {"text": "pessoal", "start_sec": 1.2, "end_sec": 2.0}], "pt"),
            ([], "pt"),  # a silent chunk produces no items
        ],
    )
    assert live_captions._transcribe_window(audio, 3.0) == "olá pessoal"

    monkeypatch.setattr(at, "vad_speech_seconds", lambda a: [])
    assert live_captions._transcribe_window(audio, 3.0) == ""


# ---------------------------------------------------------------------------
# Endpoints (gate 503 + SSE shape)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_captions_stream_validates_platform(monkeypatch):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/api/live/captions", params={"platform": "bogus", "channel": "x"})
        assert res.status_code == 400
        res = await client.get("/api/live/captions", params={"platform": "twitch", "channel": ""})
        assert res.status_code == 400
        res = await client.get("/api/live/captions/available", params={"platform": "youtube", "channel": "x"})
        assert res.status_code == 400


@pytest.mark.anyio
async def test_captions_stream_503_when_parakeet_gated(monkeypatch):
    """VODRIP_PARAAKEET=0 / missing sherpa -> 503 with the reason; the
    frontend never opens the stream then."""
    from services import live_captions

    monkeypatch.setattr(live_captions, "captions_available", lambda plat: (False, "parakeet engine unavailable"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/api/live/captions", params={"platform": "twitch", "channel": "srdogg"})
        assert res.status_code == 503
        assert "parakeet" in res.json()["detail"]


@pytest.mark.anyio
async def test_available_endpoint_shape(monkeypatch):
    from services import live_captions

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        monkeypatch.setattr(live_captions, "captions_available", lambda plat: (True, ""))
        res = await client.get("/api/live/captions/available", params={"platform": "kick", "channel": "srdoglol"})
        assert res.status_code == 200
        assert res.json() == {"available": True, "reason": None}

        monkeypatch.setattr(live_captions, "captions_available", lambda plat: (False, "model missing"))
        res = await client.get("/api/live/captions/available", params={"platform": "kick", "channel": "srdoglol"})
        assert res.status_code == 200
        assert res.json() == {"available": False, "reason": "model missing"}


class _FakeRequest:
    async def is_disconnected(self):
        return False


class _FakeCaptioner:
    """Queue + release stub for the SSE generator (mirrors _FakeSink)."""

    def __init__(self):
        self.events = asyncio.Queue()
        self.released = 0

    def release(self):
        self.released += 1


@pytest.mark.anyio
async def test_captions_sse_gen_forwards_blocks_and_releases():
    """The generator emits caption + offline frames and releases the captioner
    refcount when the connection closes."""
    from routers import live as live_router

    fake = _FakeCaptioner()
    fake.events.put_nowait(("caption", {"text": "olá pessoal", "start": 10.0, "end": 13.0}))
    fake.events.put_nowait(("offline", {}))

    frames: list[str] = []
    async for frame in live_router._captions_sse_gen(_FakeRequest(), fake):
        frames.append(frame)

    assert frames[0] == "event: caption\ndata: " + json.dumps(
        {"text": "olá pessoal", "start": 10.0, "end": 13.0}, ensure_ascii=False
    ) + "\n\n"
    assert frames[1] == "event: offline\ndata: {}\n\n"
    assert fake.released == 1


@pytest.mark.anyio
async def test_captions_sse_gen_releases_on_disconnect():
    """A disconnected client stops the generator and releases the refcount
    (no caption frames pending)."""
    from routers import live as live_router

    fake = _FakeCaptioner()

    class _Disconnected:
        async def is_disconnected(self):
            return True

    frames = [f async for f in live_router._captions_sse_gen(_Disconnected(), fake)]
    assert frames == []
    assert fake.released == 1
