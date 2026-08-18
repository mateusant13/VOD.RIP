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
import collections
import json
import threading

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

# Same variants but swapped order — fallback must pick the LOWEST bandwidth.
MASTER_NO_AUDIO_SWAPPED = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080
video-1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720
video-720.m3u8
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

    # Loop tests exercise the polling/transcribe path, not the engine warm-up
    # (a real _parakeet_model() load must never run in unit tests) — the
    # warm-up has its own dedicated test below. The channel-language evidence
    # and translation pre-warm are likewise stubbed (a real archive-DB read
    # would create the live archive file in unit tests); the translation
    # path itself has its own tests in test_caption_translate.py.
    monkeypatch.setattr(live_captions, "_warm_asr", lambda: True)
    monkeypatch.setattr(live_captions, "_warm_translate", lambda evidence, target_family=None: None)
    monkeypatch.setattr(live_captions, "_resolve_evidence", lambda platform, channel: None)
    # _maybe_translate must never touch the real SLID/NLLB models in unit
    # tests (they are present on dev hosts — the detect_language call would
    # run real inference on the fake audio buffer).
    monkeypatch.setattr(
        live_captions, "_maybe_translate",
        lambda captioner, text, audio, lang=None: (text, False),
    )
    monkeypatch.setattr(live_captions, "_resolve_live_master", pipeline.resolve_master)
    monkeypatch.setattr(live_captions, "_fetch", pipeline.fetch)
    monkeypatch.setattr(live_captions, "_decode_audio_bytes", pipeline.decode)
    monkeypatch.setattr(live_captions, "_transcribe_window", lambda audio, dur: (pipeline.transcribe_window(audio, dur), None))
    # Mock the ASR thread prewarm so it doesn't load real models
    from services import archive_transcribe as _at
    monkeypatch.setattr(_at, "prewarm_parakeet", lambda: True)
    # Pin caption_low_latency=False so tests use _FLUSH_FAIL_LIMIT=3
    from models.schemas import AppSettings as _AppSettings
    _fake_settings = _AppSettings(caption_low_latency=False)
    import deps as _deps
    monkeypatch.setattr(_deps, "settings_mgr", type("M", (), {"get": staticmethod(lambda: _fake_settings)})())
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
        # no PDT anchor -> no wall-clock latency measurement (key omitted)
        assert "latency_ms" not in block1

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
async def test_release_stops_worker_mid_ingest_without_transcribing(monkeypatch):
    """Stream-switch teardown: a release while the worker is blocked fetching
    a segment must NOT decode/transcribe it afterwards — the old session
    stops promptly (no zombie ASR on the old stream), the window never rolls
    post-release, and the registry entry is removed."""
    import threading as _threading

    fetch_entered = _threading.Event()
    released = _threading.Event()

    class _BlockingPipeline(_FakePipeline):
        def __init__(self):
            super().__init__(
                [PLAYLIST_2, PLAYLIST_3, PLAYLIST_4], ["window-1", "window-2"]
            )
            self.transcribe_calls = 0

        def fetch(self, url, headers):
            if url.endswith("seg2.ts"):
                fetch_entered.set()
                released.wait(timeout=5.0)  # hold the ingest until release()
            return super().fetch(url, headers)

        def decode(self, data):
            return np.zeros(16000, dtype=np.float32)  # 1s of audio

        def transcribe_window(self, audio, duration):
            self.transcribe_calls += 1
            return "window-1"

    pipeline = _BlockingPipeline()
    live_captions = _install_pipeline(monkeypatch, pipeline)

    loop = asyncio.get_running_loop()
    captioner = live_captions.LiveCaptioner(
        "twitch", "srdogg", loop, window_sec=1.5, poll_sec=0.02,
    )
    captioner.acquire()
    try:
        # The worker ingested seg1 (1s buffered, one short of the 1.5s
        # window) and is now blocked mid-ingest on seg2's fetch.
        assert fetch_entered.wait(timeout=5.0)
        assert pipeline.segment_fetches == ["https://edge/seg1.ts"]
        captioner.release()  # stream switched / popup closed
        released.set()
        th = captioner._thread
        assert th is not None
        th.join(timeout=3.0)
        assert not th.is_alive()
        # The in-flight segment was fetched but dropped: no ASR pass on the
        # old stream, no caption emitted after the release.
        assert "https://edge/seg2.ts" in pipeline.segment_fetches
        assert pipeline.transcribe_calls == 0
        assert captioner.events.empty()
    finally:
        released.set()
        captioner.release()
        th = captioner._thread
        if th is not None:
            th.join(timeout=3.0)
    assert (captioner.platform, captioner.channel) not in live_captions._CAPTIONERS


@pytest.mark.anyio
async def test_caption_session_reservation_toggles_with_refcount(monkeypatch):
    """The archive GPU reservation follows the caption session across ALL
    captioners: first acquire declares it, last release clears it, and a
    concurrent second session keeps it active until the last one leaves
    (idempotent releases never clear a live session)."""
    from services import archive_transcribe as at

    live_captions = _install_pipeline(monkeypatch, _FakePipeline([], []))
    # The resolver reports offline so the workers park without network or
    # model loads — only the reservation toggle is under test.
    live_captions._resolve_live_master = lambda platform, channel: None

    loop = asyncio.get_running_loop()
    c1 = live_captions.LiveCaptioner("twitch", "ch1", loop, poll_sec=0.02)
    c2 = live_captions.LiveCaptioner("twitch", "ch2", loop, poll_sec=0.02)
    assert at.caption_session_active() is False
    try:
        c1.acquire()
        assert at.caption_session_active() is True
        c2.acquire()
        assert at.caption_session_active() is True
        c1.release()
        assert at.caption_session_active() is True  # c2 still live
        c2.release()
        assert at.caption_session_active() is False
        # idempotent release must not clear a live session
        c1.acquire()
        c2.acquire()
        c1.release()
        c1.release()  # double release — c2 keeps the session live
        assert at.caption_session_active() is True
        c2.release()
        assert at.caption_session_active() is False
    finally:
        c1.release()
        c2.release()
        for c in (c1, c2):
            if c._thread is not None:
                c._thread.join(timeout=3.0)
    assert (c1.platform, c1.channel) not in live_captions._CAPTIONERS
    assert (c2.platform, c2.channel) not in live_captions._CAPTIONERS


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


@pytest.mark.anyio
async def test_captioner_drops_stale_backlog_and_resyncs_to_live_edge(monkeypatch):
    """After a freeze/buffer the poll returns the whole gap; transcribing it
    segment-by-segment would keep captions behind the live edge. With
    max_backlog_sec=5 and a 15x1s gap, only the newest 5s may reach the
    transcriber, the timeline advances across the dropped head, and the
    first caption anchors AFTER the gap (resynced to live edge)."""
    gap = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n" + "".join(
        f"#EXTINF:1.0,\nseg{i}.ts\n" for i in range(1, 16)
    )
    pipeline = _FakePipeline([gap], ["tail-window-1", "tail-window-2"])
    live_captions = _install_pipeline(monkeypatch, pipeline)

    loop = asyncio.get_running_loop()
    captioner = live_captions.LiveCaptioner(
        "twitch", "srdogg", loop, window_sec=1.5, poll_sec=0.02,
        max_backlog_sec=5.0,
    )
    captioner.acquire()
    try:
        ev1, block1 = await _wait_event(captioner.events)
        ev2, block2 = await _wait_event(captioner.events)
        assert ev1 == "caption" and ev2 == "caption"
        # The events are queued by the flush; the worker still ingests the
        # remaining tail segments after the last flush (and a segment's fetch
        # lands before its stream_sec increment) — settle on the timeline.
        deadline = loop.time() + 5.0
        while captioner._stream_sec < 15.0 and loop.time() < deadline:
            await asyncio.sleep(0.01)
        # Only the newest 5 of the 15 seconds may be fetched/transcribed.
        assert sorted(pipeline.segment_fetches) == [
            f"https://edge/seg{i}.ts" for i in range(11, 16)
        ]
        # First caption starts after the dropped gap — at the live edge.
        assert block1["start"] == pytest.approx(10.0)
        assert block2["start"] == pytest.approx(12.0)
        # Timeline accounts for the dropped audio (10s dropped + 5s ingested).
        assert captioner._stream_sec == pytest.approx(15.0)
    finally:
        captioner.release()
        captioner._thread.join(timeout=3.0)
    assert (captioner.platform, captioner.channel) not in live_captions._CAPTIONERS


# ---------------------------------------------------------------------------
# Parakeet wiring (ASR functions stubbed)
# ---------------------------------------------------------------------------


def test_transcribe_window_uses_parakeet_path(monkeypatch):
    """VAD speech regions -> _parakeet_model + _transcribe_batch_parakeet ->
    concatenated text + detected lang; empty VAD yields no caption text."""
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
    text, detected_lang = live_captions._transcribe_window(audio, 3.0)
    assert text == "olá pessoal"
    assert detected_lang == "pt"

    monkeypatch.setattr(at, "vad_speech_seconds", lambda a: [])
    text, detected_lang = live_captions._transcribe_window(audio, 3.0)
    assert text == ""
    assert detected_lang is None


def test_warm_asr_preloads_engine_and_vad_once(monkeypatch):
    """The worker pre-warms via prewarm_parakeet (resident pin + model + VAD +
    CUDA EP prime) at start so the first flush is not a 2-6s cold load."""
    from services import archive_transcribe as at
    from services import live_captions

    calls: list[str] = []
    monkeypatch.setattr(at, "prewarm_parakeet", lambda: (calls.append("prewarm"), True)[-1])
    result = live_captions._warm_asr()
    assert result is True
    assert calls == ["prewarm"]


@pytest.mark.anyio
async def test_captioner_pdt_flush_carries_latency_ms(monkeypatch):
    """With a wall-clock origin (playlist PDT), each caption payload carries
    latency_ms — wall ms since the window's audio completed (the frontend
    anchors blocks to the video clock, so this trail is the visible lag)."""
    import time as _time
    from datetime import datetime, timezone

    pdt = datetime.fromtimestamp(
        _time.time() - 30, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    playlist = (
        "#EXTM3U\n#EXT-X-TARGETDURATION:2\n"
        f"#EXT-X-PROGRAM-DATE-TIME:{pdt}\n"
        "#EXTINF:1.0,\nseg1.ts\n"
        "#EXTINF:1.0,\nseg2.ts\n"
    )
    pipeline = _FakePipeline([playlist], ["window-1"])
    live_captions = _install_pipeline(monkeypatch, pipeline)

    loop = asyncio.get_running_loop()
    captioner = live_captions.LiveCaptioner(
        "twitch", "srdogg", loop, window_sec=1.5, poll_sec=0.02,
    )
    captioner.acquire()
    try:
        ev, block = await _wait_event(captioner.events)
        assert ev == "caption"
        assert block["text"] == "window-1"
        # window start anchors to the playlist PDT (wall epoch)
        assert block["start"] == pytest.approx(_time.time() - 30, abs=5.0)
        # latency_ms measures wall ms since the window's audio completed —
        # time.time() and the PDT-derived end must be on the SAME clock base
        # (UTC epoch); a frontend/backend parse mismatch would drift this by
        # the user's timezone offset, not by a few ms. Tolerance covers only
        # the queue hop between the flush and this assertion.
        assert block["latency_ms"] == pytest.approx(
            (_time.time() - block["end"]) * 1000, abs=250.0
        )
        assert block["latency_ms"] >= 0
    finally:
        captioner.release()
        th = captioner._thread
        assert th is not None
        th.join(timeout=3.0)
    assert (captioner.platform, captioner.channel) not in live_captions._CAPTIONERS


@pytest.mark.anyio
async def test_captioner_pdt_with_zone_offset_parses_to_utc_epoch(monkeypatch):
    """A PDT carrying a NON-UTC zone offset must parse to the SAME UTC epoch
    as the equivalent Z value — caption start/end and time.time() (latency_ms)
    share one base, so an offset-aware parse is mandatory (a naive/localtime
    read on either side is the caption-drift suspect)."""
    from datetime import datetime, timezone

    # 2024-01-01T00:00:00Z, written with a +02:00 offset for the same instant.
    playlist = (
        "#EXTM3U\n#EXT-X-TARGETDURATION:2\n"
        "#EXT-X-PROGRAM-DATE-TIME:2024-01-01T02:00:00.000+02:00\n"
        "#EXTINF:1.0,\nseg1.ts\n"
        "#EXTINF:1.0,\nseg2.ts\n"
    )
    pipeline = _FakePipeline([playlist], ["window-1"])
    live_captions = _install_pipeline(monkeypatch, pipeline)

    loop = asyncio.get_running_loop()
    captioner = live_captions.LiveCaptioner(
        "twitch", "srdogg", loop, window_sec=1.5, poll_sec=0.02,
    )
    captioner.acquire()
    try:
        ev, block = await _wait_event(captioner.events)
        assert ev == "caption"
        assert block["text"] == "window-1"
        expected = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
        # The +02:00 tag lands on the UTC epoch, not 2h off.
        assert block["start"] == pytest.approx(expected, abs=0.01)
        assert block["end"] == pytest.approx(expected + 2.0, abs=0.01)
        assert block["latency_ms"] >= 0
    finally:
        captioner.release()
        th = captioner._thread
        assert th is not None
        th.join(timeout=3.0)
    assert (captioner.platform, captioner.channel) not in live_captions._CAPTIONERS


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
        res = await client.get("/api/live/captions/available", params={"platform": "facebook", "channel": "x"})
        assert res.status_code == 400


@pytest.mark.anyio
async def test_captions_stream_503_when_parakeet_gated(monkeypatch):
    """VODRIP_PARAAKEET=0 / missing sherpa -> 503 with the reason; the
    feature gate is patched to enabled so this tests the parakeet gate."""
    from services import live_captions
    from services.feature_registry import is_enabled as _orig_enabled
    monkeypatch.setattr("services.feature_registry.is_enabled", lambda fid: True if fid == "live-captions" else _orig_enabled(fid))
    monkeypatch.setattr(live_captions, "captions_available", lambda plat: (False, "parakeet engine unavailable"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/api/live/captions", params={"platform": "twitch", "channel": "srdogg"})
        assert res.status_code == 503
        assert "parakeet" in res.json()["detail"]


@pytest.mark.anyio
async def test_available_endpoint_shape(monkeypatch):
    from services import live_captions
    from services.feature_registry import is_enabled as _orig_enabled
    monkeypatch.setattr("services.feature_registry.is_enabled", lambda fid: True if fid == "live-captions" else _orig_enabled(fid))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        monkeypatch.setattr(live_captions, "captions_available", lambda plat: (True, ""))
        res = await client.get("/api/live/captions/available", params={"platform": "kick", "channel": "srdoglol"})
        assert res.status_code == 200
        body = res.json()
        assert body["available"] is True
        assert body["reason"] is None
        assert "low_latency" in body  # low-latency subtitle feature

        monkeypatch.setattr(live_captions, "captions_available", lambda plat: (False, "model missing"))
        res = await client.get("/api/live/captions/available", params={"platform": "kick", "channel": "srdoglol"})
        assert res.status_code == 200
        body = res.json()
        assert body["available"] is False
        assert body["reason"] == "model missing"


@pytest.mark.anyio
async def test_captions_stream_lang_param_reaches_captioner(monkeypatch):
    """?lang=es on the SSE request flows into captioner.acquire — the
    per-session translate-target override. Absent lang -> acquire(None)
    (app-language default); a bad lang is rejected 400 before any stream."""
    from routers import live as live_router
    from services import live_captions
    from services.feature_registry import is_enabled as _orig_enabled
    monkeypatch.setattr("services.feature_registry.is_enabled", lambda fid: True if fid == "live-captions" else _orig_enabled(fid))
    monkeypatch.setattr(live_captions, "captions_available", lambda plat: (True, ""))
    acquired: list = []

    class _StubCaptioner:
        events = None

        def acquire(self, lang=None):
            acquired.append(lang)

        def release(self):
            pass

    monkeypatch.setattr(live_captions, "get_captioner", lambda p, c, loop: _StubCaptioner())

    async def _fake_gen(request, captioner):
        return
        yield  # pragma: no cover — keeps this an async generator

    monkeypatch.setattr(live_router, "_captions_sse_gen", _fake_gen)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/api/live/captions", params={"platform": "twitch", "channel": "srdogg", "lang": "es"})
        assert res.status_code == 200
        assert acquired == ["es"]
        res = await client.get("/api/live/captions", params={"platform": "kick", "channel": "srdoglol"})
        assert res.status_code == 200
        assert acquired == ["es", None]
        res = await client.get("/api/live/captions", params={"platform": "twitch", "channel": "srdogg", "lang": "de"})
        assert res.status_code == 400
        assert "lang" in res.json()["detail"]

@pytest.mark.anyio
async def test_captioner_acquire_lang_sets_target_family(monkeypatch):
    """acquire('es') stores the session target on the captioner; a fresh
    pipeline cycle (refcount 0->1) without lang resets to the app default
    (None), so the worker never inherits a stale override."""
    pipeline = _FakePipeline([], [])
    live_captions = _install_pipeline(monkeypatch, pipeline)

    loop = asyncio.get_running_loop()
    captioner = live_captions.LiveCaptioner(
        "twitch", "srdogg", loop, window_sec=1.5, poll_sec=0.02,
    )
    captioner.acquire("es")
    assert captioner._target_family == "es"
    captioner.release()
    captioner._thread.join(timeout=3.0)

    # A later subscriber without lang starts a fresh worker with the default.
    captioner.acquire()
    assert captioner._target_family is None
    captioner.release()
    captioner._thread.join(timeout=3.0)
    assert (captioner.platform, captioner.channel) not in live_captions._CAPTIONERS


@pytest.mark.anyio
async def test_maybe_translate_uses_session_target_family(monkeypatch):
    """_maybe_translate translates into the captioner's ?lang= override
    (per-session selector) instead of the app language when one is set.
    Sticky lock: once locked from evidence, the session family stays fixed."""
    from services import caption_translate as ct
    from services import live_captions

    monkeypatch.setattr(ct, "enabled", lambda: True)
    monkeypatch.setattr(ct, "nllb_dir", lambda: object())  # truthy — models "present"
    monkeypatch.setattr(ct, "slid_dir", lambda: None)
    monkeypatch.setattr(ct, "app_language_family", lambda: "pt")
    monkeypatch.setattr(ct, "translate", lambda text, family, source_family=None: f"[{family}] {text}")

    captioner = live_captions.LiveCaptioner(
        "twitch", "srdogg", asyncio.get_running_loop(),
    )
    captioner._evidence_family = "en"  # stream evidence — different from both targets
    captioner._target_family = "es"  # the user's in-player selection
    out, translated = live_captions._maybe_translate(captioner, "hello", None)
    assert translated is True
    assert out == "[es] hello"
    # Session family locked from evidence
    assert captioner._session_family == "en"

    # Without an override the app language drives the target.
    captioner._target_family = None
    out, translated = live_captions._maybe_translate(captioner, "hello", None)
    assert out == "[pt] hello"


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
    fake.events.put_nowait(("caption", {"text": "olá pessoal", "start": 10.0, "end": 13.0, "latency_ms": 812}))
    fake.events.put_nowait(("offline", {}))

    frames: list[str] = []
    async for frame in live_router._captions_sse_gen(_FakeRequest(), fake):
        frames.append(frame)

    # the full block — text/start/end AND latency_ms — is forwarded verbatim
    assert frames[0] == "event: caption\ndata: " + json.dumps(
        {"text": "olá pessoal", "start": 10.0, "end": 13.0, "latency_ms": 812},
        ensure_ascii=False,
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


# ---------------------------------------------------------------------------
# New: lowest-bandwidth fallback
# ---------------------------------------------------------------------------


def test_parse_master_falls_back_to_lowest_bandwidth():
    """When no audio-only rendition exists, the fallback must pick the
    LOWEST bandwidth STREAM-INF variant — not the first one."""
    from services.live_captions import _parse_master_audio_url

    url = _parse_master_audio_url(MASTER_NO_AUDIO, "https://edge/master.m3u8")
    assert url == "https://edge/video-720.m3u8"

    # Swapped order: lowest is now the second variant — must still pick 720.
    url = _parse_master_audio_url(MASTER_NO_AUDIO_SWAPPED, "https://edge/master.m3u8")
    assert url == "https://edge/video-720.m3u8"


# ---------------------------------------------------------------------------
# New: sticky session language lock
# ---------------------------------------------------------------------------


def test_sticky_session_lock_from_evidence(monkeypatch):
    """Channel evidence locks session_family immediately; later SLID votes
    cannot flip it (sticky lock)."""
    from services import caption_translate as ct
    from services import live_captions

    monkeypatch.setattr(ct, "enabled", lambda: True)
    monkeypatch.setattr(ct, "nllb_dir", lambda: object())
    monkeypatch.setattr(ct, "slid_dir", lambda: object())
    monkeypatch.setattr(ct, "app_language_family", lambda: "pt")
    monkeypatch.setattr(ct, "translate", lambda text, fam, source_family=None: f"X({text})")
    # SLID would say "en" but evidence "pt" wins
    monkeypatch.setattr(ct, "detect_language", lambda audio: "en")

    c = live_captions.LiveCaptioner.__new__(live_captions.LiveCaptioner)
    c.platform = "twitch"
    c.channel = "test"
    c._evidence_family = "en"  # stream is English, app is PT
    c._target_family = None
    c._session_family = None
    c._lang_votes = collections.deque(maxlen=5)

    # Evidence "en" != app "pt" → translate, session locked on "en"
    out, translated = live_captions._maybe_translate(c, "hello", b"audio")
    assert translated is True
    assert c._session_family == "en"  # locked from evidence

    # SLID now says "pt" but session stays "en" (sticky lock)
    monkeypatch.setattr(ct, "detect_language", lambda audio: "pt")
    out, translated = live_captions._maybe_translate(c, "fala", b"audio")
    assert translated is True  # session still "en" != "pt" → translates
    assert c._session_family == "en"  # sticky — did not flip to "pt"


def test_sticky_session_lock_from_slid_votes(monkeypatch):
    """SLID votes accumulate; once 3 agree, session locks and stays."""
    from services import caption_translate as ct
    from services import live_captions

    monkeypatch.setattr(ct, "enabled", lambda: True)
    monkeypatch.setattr(ct, "nllb_dir", lambda: object())
    monkeypatch.setattr(ct, "slid_dir", lambda: object())
    monkeypatch.setattr(ct, "app_language_family", lambda: "pt")
    monkeypatch.setattr(ct, "translate", lambda text, fam, source_family=None: f"X({text})")
    monkeypatch.setattr(ct, "detect_language", lambda audio: "en")

    c = live_captions.LiveCaptioner.__new__(live_captions.LiveCaptioner)
    c.platform = "twitch"
    c.channel = "test"
    c._evidence_family = None
    c._target_family = None
    c._session_family = None
    c._lang_votes = collections.deque(maxlen=5)

    # 1 vote: not enough
    live_captions._maybe_translate(c, "a", b"audio")
    assert c._session_family is None

    # 2 votes: still not enough
    live_captions._maybe_translate(c, "b", b"audio")
    assert c._session_family is None

    # 3 votes → locks
    live_captions._maybe_translate(c, "c", b"audio")
    assert c._session_family == "en"

    # 4th call: SLID says "pt" now — session stays "en" (sticky)
    monkeypatch.setattr(ct, "detect_language", lambda audio: "pt")
    live_captions._maybe_translate(c, "d", b"audio")
    assert c._session_family == "en"


# ---------------------------------------------------------------------------
# New: parallel warm vs HLS poll
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_parallel_warm_vs_hls_poll(monkeypatch):
    """ASR warm runs on a daemon thread so the HLS poll loop starts
    immediately (fetching happens in parallel with warm)."""
    warm_started = threading.Event()
    warm_block = threading.Event()

    def blocking_warm():
        warm_started.set()
        warm_block.wait(timeout=2.0)  # block until test releases

    pipeline = _FakePipeline([PLAYLIST_1, PLAYLIST_2], ["w1"])
    live_captions = _install_pipeline(monkeypatch, pipeline)
    monkeypatch.setattr(live_captions, "_warm_asr", blocking_warm)

    loop = asyncio.get_running_loop()
    captioner = live_captions.LiveCaptioner(
        "twitch", "paratest", loop, window_sec=1.5, poll_sec=0.02,
    )
    captioner.acquire()
    try:
        # The fetch should have happened even though warm is still blocked
        assert warm_started.wait(timeout=2.0), "warm should have started"
        # Wait for the caption event — proves HLS poll ran in parallel
        ev, block = await _wait_event(captioner.events, timeout=5.0)
        assert ev == "caption"
        assert block["text"] == "w1"
    finally:
        warm_block.set()  # release the warm
        captioner.release()
        th = captioner._thread
        if th is not None:
            th.join(timeout=3.0)
