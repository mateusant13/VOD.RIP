"""Real live-captioner e2e — local HLS over real HTTP, real ffmpeg decode,
real poll/trim/ingest/flush loop; ASR text stubbed (deterministic).

Verifies the realtime-transcript contract against a real network path:
  * steady state: captions flow from a live HLS stream (local server),
  * freeze/buffer: the playlist jumps forward by more than the backlog cap;
    the captioner drops the stale head (only the newest max_backlog_sec of
    audio is fetched and transcribed) and the next caption anchors at the
    live edge — the transcript never falls behind (the user's bug: after a
    freeze/buffer/loading the realtime transcript stayed delayed).

Stubbed (documented): ASR text (`_transcribe_window`) and the platform
master resolver (local URL instead of the Twitch usher). Real: HTTP fetch,
HLS master/media parsing, ffmpeg decode of real TS segments, the entire
captioner loop (backoff, seen-set, trim, ingest, flush, emit, queue).

Run directly (isolated process — recommended):
    python tests/test_live_captions_e2e_real.py
Under pytest (opt-in `real` marker — excluded by default addopts):
    python -m pytest tests/test_live_captions_e2e_real.py -m real -s
"""
from __future__ import annotations

import asyncio
import functools
import http.server
import os
import pathlib
import shutil
import subprocess as sp
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import live_captions  # noqa: E402
from services.ytdlp_ffmpeg import _resolve_ffmpeg_exe  # noqa: E402

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="vodrip-livecap-e2e-"))

MASTER = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Audio",URI="media.m3u8"\n'
    "#EXT-X-STREAM-INF:BANDWIDTH=128000\n"
    "video.m3u8\n"
)


def _playlist(first: int, last: int) -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2"]
    for i in range(first, last + 1):
        lines += ["#EXTINF:1.0,", f"seg{i}.ts"]
    return "\n".join(lines) + "\n"


def _publish_playlist(path: pathlib.Path, text: str) -> None:
    """Atomic publish (temp + os.replace) — a mid-write reader would serve a
    truncated playlist and skew the seen-set/trim."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _make_segment(path: pathlib.Path) -> None:
    """One 1s AAC-in-TS segment from a 440 Hz sine (real audio for ffmpeg)."""
    sp.run(
        [
            _resolve_ffmpeg_exe(), "-y", "-v", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:a", "aac", "-b:a", "48k", "-f", "mpegts", str(path),
        ],
        check=True, capture_output=True, timeout=60,
    )


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        pass


async def _run() -> None:
    for i in range(1, 16):
        _make_segment(_TMP / f"seg{i}.ts")
    (_TMP / "master.m3u8").write_text(MASTER, encoding="utf-8")
    _publish_playlist(_TMP / "media.m3u8", _playlist(1, 3))

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(_QuietHandler, directory=str(_TMP)),
    )
    base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    segment_fetches: list[str] = []
    real_fetch = live_captions._fetch

    def recording_fetch(url, headers):
        if url.rsplit(".", 1)[-1] == "ts":
            segment_fetches.append(url.rsplit("/", 1)[-1])
        return real_fetch(url, headers)

    try:
        live_captions._fetch = recording_fetch
        # e2e drives the LOOP, not the engine — never load the real model.
        live_captions._warm_asr = lambda: None
        # nor the real translator/SLID/archive-DB reads (own e2e exists)
        live_captions._warm_translate = lambda evidence, target=None: None
        live_captions._resolve_evidence = lambda platform, channel: None
        live_captions._maybe_translate = lambda captioner, text, audio: (text, False)
        live_captions._resolve_live_master = lambda platform, channel: {
            "url": f"{base}/master.m3u8",
            "headers": {},
        }
        text_n = {"n": 0}

        def fake_transcribe(audio, duration):
            text_n["n"] += 1
            return f"text-{text_n['n']}"

        live_captions._transcribe_window = fake_transcribe

        loop = asyncio.get_running_loop()
        captioner = live_captions.LiveCaptioner(
            "twitch", "e2e", loop, window_sec=1.5, poll_sec=0.05,
            max_backlog_sec=5.0,
        )
        captioner.acquire()
        queue = captioner.subscribe()
        try:
            async def next_event(timeout=20.0):
                ev, data = await asyncio.wait_for(queue.get(), timeout)
                assert ev == "caption", f"expected caption, got {ev}: {data}"
                return data

            # Steady state: seg1+seg2 fill the 1.5s window.
            b1 = await next_event()
            assert b1["start"] == 0.0, b1
            assert b1["text"] == "text-1", b1

            # Freeze/buffer: the playlist jumps to 15 segments (the gap is
            # seg4..seg10, 7s > cap 5). The captioner must drop the stale
            # head and transcribe only the newest 5s (seg11..seg15).
            _publish_playlist(_TMP / "media.m3u8", _playlist(1, 15))

            b2 = await next_event()
            b3 = await next_event()
            # Resynced to the live edge: the first post-stall window starts
            # at 10.0 (3s ingested + 7s dropped), NOT at the gap head (3.0).
            assert b2["start"] == 10.0, b2
            assert b3["start"] == 12.0, b3
            assert b2["text"] == "text-2" and b3["text"] == "text-3", (b2, b3)
        finally:
            captioner.release()
            th = captioner._thread
            if th is not None:
                th.join(timeout=3.0)
    finally:
        server.shutdown()
        server.server_close()

    # The gap head (seg4..seg10) must never have been fetched/transcribed.
    fetched = sorted(segment_fetches)
    assert fetched == [
        "seg1.ts", "seg11.ts", "seg12.ts", "seg13.ts",
        "seg14.ts", "seg15.ts", "seg2.ts", "seg3.ts",
    ], fetched

    shutil.rmtree(_TMP, ignore_errors=True)
    print("live captions e2e OK — steady captions, stale backlog dropped, "
          "transcript resynced to live edge after the jump.")


if __name__ == "__main__":
    import logging

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(_run())


# --- pytest wrapper (opt-in `real` marker) -----------------------------------

def test_live_captions_e2e_real():
    try:
        _resolve_ffmpeg_exe()
    except Exception as exc:
        pytest.skip(f"ffmpeg unavailable: {exc}")
    asyncio.run(_run())


test_live_captions_e2e_real = pytest.mark.real(test_live_captions_e2e_real)
