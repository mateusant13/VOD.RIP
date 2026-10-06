"""Preview ASR subtitles for Twitch/Kick — /api/preview/subtitles/{platform}/{video_id}.

Neither platform publishes caption tracks, so a preview can only show words by
transcribing locally. The design is bounded at every layer because the
alternative is useless: the archive path pulls the WHOLE VOD (a 6h Twitch VOD
is ~350 MB) because transcription genuinely needs all of it, while a preview
only needs its head.

What these pin:
  - ffmpeg's -t goes BEFORE -i, which is what bounds the HLS read. After -i it
    would bound the OUTPUT instead and still stream the entire playlist.
  - A permanent unavailability (VOD deleted / sub-only / geo) is "no
    subtitles", not a 502 — otherwise every tab switch re-pays a doomed fetch.
  - The response is the same shape /api/subtitles already returns, so the
    panel renders ASR rows through the path it already has.
  - Nothing here writes to the archive DB: a preview caption is a read.

No network: ffmpeg, the recognizer and the platform resolvers are all faked.
"""

from __future__ import annotations

import subprocess

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from routers import subtitles as subtitles_router
from services import archive_db, archive_transcribe


# --------------------------------------------------------------------------
# ffmpeg command shape — the load-bearing assertion
# --------------------------------------------------------------------------

def _write_wav_like(cmd, seconds=1.0, sample_rate=16000):
    """Write the ffmpeg OUTPUT path (the last argv element) so the fake
    honours the real contract instead of guessing a filename.

    Produces a genuine 16 kHz mono PCM wav, because _wav_duration_sec reads
    the header through the `wave` module and a hand-rolled blob would make
    that return 0. ``seconds=0`` is a valid header with zero frames — real
    silence, which is what the empty-output guard must reject.
    """
    import struct
    import wave
    from pathlib import Path

    path = Path(cmd[-1])
    if seconds <= 0:
        path.write_bytes(b"\0" * 44)
        return
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\0\0" * int(seconds * sample_rate))


def test_slice_bounds_the_hls_read_with_t_before_i(monkeypatch, tmp_path):
    """-t BEFORE -i bounds the INPUT.

    Placed after -i it would bound the output write instead, and ffmpeg would
    still read the whole HLS playlist first — the exact cost this function
    exists to avoid.
    """
    seen = {}

    def fake_run(cmd, capture_output=False, timeout=None):
        seen["cmd"] = list(cmd)
        _write_wav_like(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(archive_transcribe, "_resolve_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(archive_transcribe.sp, "run", fake_run)
    monkeypatch.setattr(
        archive_transcribe, "_resolve_remote_playback",
        lambda p, v, c: ("https://cdn.example/master.m3u8", {}),
    )

    wav = tmp_path / "out.wav"
    archive_transcribe._fetch_remote_audio_slice(
        "twitch", "2892722496", "somechannel", wav, 300.0)

    cmd = seen["cmd"]
    i_t = cmd.index("-t")
    i_i = cmd.index("-i")
    assert i_t < i_i, "-t must precede -i to bound the HLS read, not the write"
    assert cmd[i_t + 1] == "300.000"
    # Still audio-only 16 kHz mono — same contract as the whole-VOD fetch.
    assert "-vn" in cmd and "16000" in cmd and "1" in cmd


def test_slice_raises_on_empty_output(monkeypatch, tmp_path):
    """A header-only file (no frames) is silence, not audio; fail loudly."""
    def fake_run(cmd, capture_output=False, timeout=None):
        _write_wav_like(cmd, seconds=0.0)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(archive_transcribe, "_resolve_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(archive_transcribe.sp, "run", fake_run)
    monkeypatch.setattr(
        archive_transcribe, "_resolve_remote_playback",
        lambda p, v, c: ("https://cdn.example/master.m3u8", {}),
    )
    with pytest.raises(RuntimeError, match="no audio slice"):
        archive_transcribe._fetch_remote_audio_slice(
            "twitch", "v", "c", tmp_path / "out.wav", 300.0)


def test_slice_raises_on_ffmpeg_failure(monkeypatch, tmp_path):
    def fake_run(cmd, capture_output=False, timeout=None):
        _write_wav_like(cmd)
        return subprocess.CompletedProcess(cmd, 1, b"", b"403 forbidden")

    monkeypatch.setattr(archive_transcribe, "_resolve_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(archive_transcribe.sp, "run", fake_run)
    monkeypatch.setattr(
        archive_transcribe, "_resolve_remote_playback",
        lambda p, v, c: ("https://cdn.example/master.m3u8", {}),
    )
    with pytest.raises(RuntimeError, match="slice failed"):
        archive_transcribe._fetch_remote_audio_slice(
            "twitch", "v", "c", tmp_path / "out.wav", 300.0)


def test_whole_vod_fetch_still_works_through_the_shared_resolver(monkeypatch, tmp_path):
    """The refactor moved playback resolution into _resolve_remote_playback.
    The whole-VOD path must keep working — and must NOT gain a -t."""
    seen = {}

    def fake_run(cmd, capture_output=False, timeout=None):
        seen["cmd"] = list(cmd)
        _write_wav_like(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(archive_transcribe, "_resolve_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(archive_transcribe.sp, "run", fake_run)
    monkeypatch.setattr(
        archive_transcribe, "_resolve_remote_playback",
        lambda p, v, c: ("https://cdn.example/master.m3u8", {"referer": "x"}),
    )
    archive_transcribe._fetch_remote_audio_wav(
        "twitch", "v", "c", tmp_path / "audio.wav")
    assert "-t" not in seen["cmd"], "the archive path must stay unbounded"


# --------------------------------------------------------------------------
# recognizer plumbing
# --------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, text, tokens, timestamps):
        self.text = text
        self.tokens = tokens
        self.timestamps = timestamps
        self.ys_log_probs = None


class _FakeStream:
    def __init__(self, rec):
        self._rec = rec
        self.result = None

    def accept_waveform(self, sr, data):
        self._rec.accepted.append((sr, len(data)))


class _FakeRec:
    """Emits one non-empty result per chunk, so a decode can never be a no-op."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.accepted = []

    def create_stream(self):
        s = _FakeStream(self)
        return s

    def decode_stream(self, stream):
        idx = len(self.accepted) - 1
        text = self.texts[idx] if idx < len(self.texts) else ""
        stream.result = _FakeResult(text, ["a", "b"], [0.0, 0.5])


def test_parakeet_segments_use_absolute_offsets(monkeypatch):
    rec = _FakeRec(["primeira", "segunda"])
    monkeypatch.setattr(archive_transcribe, "_parakeet_model", lambda: rec)
    monkeypatch.setattr(archive_transcribe, "_MAX_CHUNK_SEC", 60.0)

    # 150 s at 16 kHz = 3 chunks of 60/60/30.
    audio = [0.0] * (150 * 16000)
    segs = archive_transcribe.parakeet_segments_from_array(audio, 16000)
    assert [s["text"] for s in segs] == ["primeira", "segunda"]
    assert segs[0]["start_sec"] == 0.0
    # Second chunk starts at 60 s in absolute video time, not chunk time.
    assert segs[1]["start_sec"] == pytest.approx(60.0)
    assert segs[1]["end_sec"] > segs[1]["start_sec"]
    # One accept_waveform per chunk.
    assert [n for _, n in rec.accepted] == [60 * 16000, 60 * 16000, 30 * 16000]


def test_parakeet_skips_silent_chunks_without_inventing_rows(monkeypatch):
    """parakeet does not hallucinate on silence; an empty result must yield no
    segment rather than a blank caption row in the panel."""
    rec = _FakeRec(["", ""])
    monkeypatch.setattr(archive_transcribe, "_parakeet_model", lambda: rec)
    monkeypatch.setattr(archive_transcribe, "_MAX_CHUNK_SEC", 60.0)
    audio = [0.0] * (120 * 16000)
    assert archive_transcribe.parakeet_segments_from_array(audio, 16000) == []


def test_parakeet_max_sec_bounds_the_decode(monkeypatch):
    rec = _FakeRec(["one", "two", "three"])
    monkeypatch.setattr(archive_transcribe, "_parakeet_model", lambda: rec)
    monkeypatch.setattr(archive_transcribe, "_MAX_CHUNK_SEC", 60.0)
    audio = [0.0] * (300 * 16000)
    segs = archive_transcribe.parakeet_segments_from_array(audio, 16000, max_sec=125.0)
    # 125 s -> 60 + 60 + 5 (the 5 s tail is below the 0.5 s guard? no, above)
    assert len(rec.accepted) == 3
    assert segs[-1]["start_sec"] == pytest.approx(120.0)


# --------------------------------------------------------------------------
# router
# --------------------------------------------------------------------------

@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with subtitles_router._subs_cache._lock:
        subtitles_router._subs_cache._data.clear()
    monkeypatch.setenv("VODRIP_CAPTION_FIRST", "0")


def _seed(platform="twitch", video_id="2892722496", channel="chan"):
    archive_db.execute(
        "INSERT OR REPLACE INTO videos (platform, video_id, channel, title) "
        "VALUES (?,?,?,?)", (platform, video_id, channel, "t"))


def _patch_asr(monkeypatch, segments, *, fetch=None):
    monkeypatch.setattr(
        subtitles_router, "_fetch_preview_asr",
        fetch or (lambda *a, **k: {
            "url": "", "lang": None, "source": "asr",
            "has_subtitles": bool(segments),
            "rows": [{"offset_sec": s["start_sec"], "text": s["text"]} for s in segments],
            "covered_sec": 300.0, "partial": True,
        }))


@pytest.mark.asyncio
async def test_youtube_is_rejected_with_a_pointer_to_the_other_endpoint(client):
    """YouTube has real captions; sending it down the ASR path would cost a
    model load to reproduce what /api/subtitles returns in ~1 s."""
    r = await client.get("/api/preview/subtitles/youtube/abc123")
    assert r.status_code == 400
    assert "/api/subtitles" in r.json()["detail"]


@pytest.mark.asyncio
async def test_unknown_platform_is_rejected(client):
    r = await client.get("/api/preview/subtitles/tiktok/abc123")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_unknown_video_is_404(client, monkeypatch):
    r = await client.get("/api/preview/subtitles/twitch/does-not-exist")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_rows_are_served_and_marked_partial(client, monkeypatch):
    _patch_asr(monkeypatch, [{"start_sec": 0.0, "text": "ola"},
                             {"start_sec": 60.0, "text": "mundo"}])
    r = await client.get("/api/preview/subtitles/twitch/2892722496")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "asr"
    assert body["has_subtitles"] is True
    assert [x["text"] for x in body["rows"]] == ["ola", "mundo"]
    # partial MUST be true: the head is never the whole VOD, and a caller that
    # reads it as "this is all of it" is exactly the wrong conclusion.
    assert body["partial"] is True
    assert body["covered_sec"] == 300.0


@pytest.mark.asyncio
async def test_second_call_is_served_from_cache(client, monkeypatch):
    calls = []

    def counting(*a, **k):
        calls.append(a)
        return {"url": "", "lang": None, "source": "asr", "has_subtitles": False,
                "rows": [], "covered_sec": 300.0, "partial": True}

    _patch_asr(monkeypatch, [], fetch=counting)
    await client.get("/api/preview/subtitles/twitch/2892722496")
    await client.get("/api/preview/subtitles/twitch/2892722496")
    assert len(calls) == 1, "a cached head must not re-run ASR"


@pytest.mark.asyncio
async def test_platform_is_part_of_the_cache_key(client, monkeypatch):
    """The same numeric id on two platforms must never share a cache entry."""
    _patch_asr(monkeypatch, [{"start_sec": 0.0, "text": "x"}])
    await client.get("/api/preview/subtitles/twitch/2892722496")
    await client.get("/api/preview/subtitles/kick/2892722496")
    keys = list(subtitles_router._subs_cache._data.keys())
    assert any(k.startswith("asr:twitch:") for k in keys)
    assert any(k.startswith("asr:kick:") for k in keys)


@pytest.mark.asyncio
async def test_head_sec_is_bucketed_so_a_slider_cannot_fill_the_cache(client, monkeypatch):
    """head_sec 300 and 301 must share one entry, or a slider turns the LRU
    into a write amplifier and every entry is a fresh 7 s ASR."""
    calls = []

    def counting(*a, **k):
        calls.append(k.get("head_bucket", a[-1] if a else None))
        return {"url": "", "lang": None, "source": "asr", "has_subtitles": False,
                "rows": [], "covered_sec": 300.0, "partial": True}

    _patch_asr(monkeypatch, [], fetch=counting)
    await client.get("/api/preview/subtitles/twitch/2892722496?head_sec=300")
    await client.get("/api/preview/subtitles/twitch/2892722496?head_sec=301")
    assert len(calls) == 1, "300 and 301 must bucket to the same 30 s slot"


@pytest.mark.asyncio
async def test_head_sec_is_clamped_by_the_query_validator(client, monkeypatch):
    _patch_asr(monkeypatch, [])
    assert (await client.get(
        "/api/preview/subtitles/twitch/2892722496?head_sec=5")).status_code == 422
    assert (await client.get(
        "/api/preview/subtitles/twitch/2892722496?head_sec=99999")).status_code == 422