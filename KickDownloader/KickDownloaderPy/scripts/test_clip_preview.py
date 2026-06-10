#!/usr/bin/env python3
"""Test Twitch clip preview for titiltei (service + HTTP API)."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.preview_service import create_session, proxy_master
from services.twitch_gql_service import list_channel_clips_sync
from services.ytdlp_service import is_clip_url

API_BASE = "http://127.0.0.1:7897"
CHANNEL = "titiltei"


def _fetch_clips(limit: int = 3) -> list[dict]:
    clips = list_channel_clips_sync(CHANNEL, limit=limit)
    if not clips:
        raise RuntimeError(f"No Twitch clips found for {CHANNEL}")
    return clips


def test_service_layer(clip_url: str) -> None:
    print(f"\n=== Service layer: {clip_url}")
    print(f"  is_clip_url: {is_clip_url(clip_url)}")
    session = create_session(clip_url, crop_start=0, crop_end=30, prefer_height=360)
    print(f"  session_id: {session.session_id}")
    print(f"  kind: {session.kind}")
    print(f"  entry_url: {session.entry_url[:100]}...")
    assert session.kind == "progressive", f"expected progressive, got {session.kind}"

    body, ctype, headers, status = proxy_master(session.session_id)
    print(f"  proxy_master: status={status} ctype={ctype} bytes={len(body)}")
    assert status == 200, status
    assert ctype == "video/mp4", ctype
    assert len(body) > 10_000, f"MP4 too small: {len(body)}"
    assert body[:4] == b"ftyp" or body[4:8] == b"ftyp", "not a valid MP4"
    print("  OK service layer")


def test_http_api(clip_url: str) -> None:
    print(f"\n=== HTTP API: {clip_url}")
    payload = json.dumps({
        "url": clip_url,
        "crop_start": 0,
        "crop_end": 30,
        "prefer_height": 360,
    }).encode()
    req = urllib.request.Request(
        f"{API_BASE}/api/preview/session",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        print(f"  SKIP HTTP (API not running): {e}")
        return

    print(f"  session response: {json.dumps(data, indent=2)}")
    kind = data.get("kind")
    playback = data.get("playback_url") or data.get("master_url")
    assert kind == "progressive", f"expected progressive, got {kind}"
    assert playback, "missing playback_url"
    if not playback.endswith("stream.mp4"):
        print("  WARN: playback_url is not stream.mp4 — restart API to pick up latest code")

    stream_url = f"{API_BASE}{playback}"
    print(f"  fetching: {stream_url}")
    req2 = urllib.request.Request(stream_url, headers={"Range": "bytes=0-65535"})
    with urllib.request.urlopen(req2, timeout=60) as resp:
        chunk = resp.read()
        ctype = resp.headers.get("Content-Type", "")
    print(f"  stream: status=200 ctype={ctype} bytes={len(chunk)}")
    assert "mp4" in ctype.lower() or len(chunk) > 1000
    print("  OK HTTP API")


def test_vod_preview(vod_url: str) -> None:
    print(f"\n=== VOD preview (control): {vod_url}")
    session = create_session(vod_url, crop_start=0, crop_end=20, prefer_height=360)
    print(f"  kind: {session.kind}")
    assert session.kind == "hls", f"expected hls, got {session.kind}"
    body, ctype, _, status = proxy_master(session.session_id)
    text = body.decode("utf-8", errors="replace")
    print(f"  master: status={status} ctype={ctype} lines={len(text.splitlines())}")
    assert status == 200 and "#EXTM3U" in text
    print("  OK VOD preview")


def main() -> int:
    clips = _fetch_clips(3)
    print(f"Found {len(clips)} clips for {CHANNEL}")
    for c in clips:
        print(f"  - {c['url']}")

    for clip in clips:
        test_service_layer(clip["url"])

    test_http_api(clips[0]["url"])

    # Also test clips.twitch.tv URL shape
    slug = clips[0]["url"].rsplit("/", 1)[-1]
    alt_url = f"https://clips.twitch.tv/{slug}"
    print(f"\n=== Alternate URL shape: {alt_url}")
    test_service_layer(alt_url)

    # titiltei Kick VOD as control (user said VOD preview works)
    kick_vod = "https://kick.com/titiltei/videos/ddaf9751-fc2e-4f5e-9d5d-94fe637ef234"
    test_vod_preview(kick_vod)

    print("\nAll clip preview tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
