"""Prove preview is playable — MP4 ftyp + multi-range proxy, not just session 200."""
from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient

from app import app

# titiltei short — user regression case
URL = "https://www.youtube.com/shorts/KkzZw5ebY0A"
# fallback when primary is geo/age blocked
FALLBACK = "https://www.youtube.com/shorts/t_Or3Oz5LX8"

_PLAYBACK_RANGES = (
    "bytes=0-8191",
    "bytes=524288-526335",
    "bytes=8388608-8396799",
)

# The window-HLS mux runs asynchronously after session creation; give it room
# to land the first segment batch (mirrors test_youtube_dash_segments_real.py).
_HLS_MUX_POLL_SEC = 45.0


def _is_mp4_head(chunk: bytes) -> bool:
    return bool(chunk) and b"ftyp" in chunk[:32]


def _create_session(client: TestClient, url: str) -> dict:
    from services.ytdlp_hls import _EXTRACT_INFO_CACHE

    _EXTRACT_INFO_CACHE.clear()
    r = client.post(
        "/api/preview/session",
        json={"url": url, "crop_start": 0, "crop_end": 60, "prefer_height": 720},
    )
    if r.status_code == 500:
        detail = (r.json().get("detail") or "").lower()
        if "unavailable" in detail or "try again" in detail:
            pytest.skip(f"YouTube extract blocked: {r.json().get('detail')}")
    assert r.status_code == 200, r.text
    return r.json()


def _stream_path(body: dict) -> tuple[str, str]:
    kind = body.get("kind") or "hls"
    sid = body["session_id"]
    if kind == "progressive":
        return kind, f"/api/preview/hls/{sid}/stream.mp4"
    return kind, f"/api/preview/hls/{sid}/master.m3u8"


def _pick_segment_url(text: str) -> str | None:
    """First proxied SEGMENT uri in a media playlist.

    Prefers window-HLS ``window-seg-NNN`` resources; falls back to any
    proxied resource that is not the media playlist itself or the fMP4 init
    segment (``#EXT-X-MAP`` URIs sit inside tag lines, so bare ``/api/``
    lines in a media playlist are segments or keys).
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("/api/preview/") and "window-seg-" in stripped:
            return stripped
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("/api/preview/"):
            continue
        if "window-playlist" in stripped or "init.mp4" in stripped:
            continue
        return stripped
    return None


def _prove_hls_playback(client: TestClient, master_path: str) -> None:
    """Master → media playlist → segment. The window mux is async, so poll
    the media playlist until a segment reference appears (98-byte minimal
    playlist = mux still landing), then fetch the segment like <video> would.
    """
    master = client.get(master_path)
    assert master.status_code == 200, master.text[:300]
    text = master.text
    assert text.lstrip().startswith("#EXTM3U"), "HLS master must be a playlist"
    first_resource = next(
        (
            ln.strip()
            for ln in text.splitlines()
            if ln.strip().startswith("/api/preview/")
        ),
        None,
    )
    assert first_resource, "HLS master must reference proxy resources"

    seg_url: str | None = None
    deadline = time.monotonic() + _HLS_MUX_POLL_SEC
    while time.monotonic() < deadline:
        media = client.get(first_resource)
        assert media.status_code == 200, media.text[:200]
        body = media.content
        if not body.lstrip().startswith(b"#EXTM3U"):
            # Single-level master: the referenced resource IS the media segment.
            seg_url = first_resource
            break
        seg_url = _pick_segment_url(media.text)
        if seg_url:
            break
        time.sleep(0.5)
    assert seg_url, "media playlist never referenced a segment (window mux failed)"

    seg_resp = client.get(seg_url, headers={"Range": "bytes=0-8191"})
    assert seg_resp.status_code in (200, 206), seg_resp.text[:200]
    assert len(seg_resp.content) > 256, "HLS segment body empty"
    assert not seg_resp.content.lstrip().startswith(b"#EXTM3U"), (
        "segment response is a playlist, not media bytes"
    )


def _prove_progressive_playback(client: TestClient, sid: str, path: str) -> None:
    total = 0
    for rng in _PLAYBACK_RANGES:
        resp = client.get(path, headers={"Range": rng})
        assert resp.status_code in (200, 206), (
            f"range {rng} returned {resp.status_code}: {resp.text[:200]}"
        )
        chunk = resp.content
        assert len(chunk) > 0, f"empty body for {rng}"
        total += len(chunk)
        if rng.startswith("bytes=0-"):
            assert _is_mp4_head(chunk), f"not MP4: head={chunk[:16]!r}"
    assert total > 16_384, "playback proof needs >16KB across ranges"

    refresh = client.post(f"/api/preview/session/{sid}/refresh", json={})
    assert refresh.status_code == 200, refresh.text
    again = client.get(path, headers={"Range": "bytes=0-8191"})
    assert again.status_code in (200, 206)
    assert _is_mp4_head(again.content), "post-refresh stream must still be MP4"


def test_youtube_preview_playback_proof():
    """Session + stream bytes must be valid MP4/HLS — simulates <video> range requests."""
    from services.youtube_session import invalidate_anonymous_session

    invalidate_anonymous_session()
    errors: list[str] = []
    with TestClient(app) as client:
        for url in (URL, FALLBACK):
            try:
                body = _create_session(client, url)
            except AssertionError as exc:
                errors.append(f"{url}: session create blocked: {exc}")
                continue
            sid = body["session_id"]
            kind, path = _stream_path(body)
            try:
                if kind == "hls":
                    _prove_hls_playback(client, path)
                else:
                    _prove_progressive_playback(client, sid, path)
                return
            except AssertionError as exc:
                errors.append(f"{url}: {exc}")
            finally:
                client.delete(f"/api/preview/session/{sid}")
    if all("session create blocked" in err for err in errors):
        pytest.skip("YouTube extract blocked for all probe URLs")
    pytest.fail("\n".join(errors))
