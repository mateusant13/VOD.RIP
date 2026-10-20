"""Resolving a playlist line must not cost more than the click that serves it.

A Twitch DVR playlist for a 29-hour VOD carries ~32k segment lines. The create
path probes that playlist to learn the VOD duration and seeds the rewrite cache,
and every line went through ``urljoin`` + ``urlparse``: **4.0 s of pure CPU**
inside the click (profiled 2026-10-20: 32,324 ``_rewrite_playlist_line`` calls,
2.2 s in ``urlparse``). The server answered that click in 15 s instead of ~1 s.

The fast path resolves the shapes playlists actually use with string operations
and keeps ``urljoin`` for the rest, so these tests pin two things: the fast path
agrees with ``urljoin`` on every shape, and a 32k-line rewrite is fast.
"""
from __future__ import annotations

import time
from urllib.parse import urljoin

import pytest

_BASE_URL = "https://video-weaver.example/vod/1234/480p30/index-dvr.m3u8"

_SHAPES = [
    "0.ts",
    "segment-00042.ts",
    "a/b/c.ts",
    "/absolute/path/1.ts",
    "//other.example/x/2.ts",
    "https://cdn.example/abs/3.ts",
    "http://cdn.example/abs/4.ts",
    "?token=abc",
    "./relative/5.ts",
    "../up/6.ts",
]


@pytest.mark.parametrize("uri", _SHAPES)
def test_fast_path_matches_urljoin(uri):
    from services.preview.session import _resolve_playlist_uri

    base = _BASE_URL.rsplit("/", 1)[0] + "/"
    assert _resolve_playlist_uri(uri, base, _BASE_URL) == urljoin(_BASE_URL, uri)


def _playlist(lines: int) -> str:
    head = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:4",
        "#EXT-X-MEDIA-SEQUENCE:0",
        '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"',
    ]
    body = []
    for i in range(lines):
        body.append("#EXTINF:4.000,")
        body.append(f"{i}.ts")
    head.append("#EXT-X-ENDLIST")
    return "\n".join(head + body) + "\n"


def test_rewriting_a_32k_segment_playlist_is_fast():
    from services.preview.session import _rewrite_playlist

    class _Session:
        session_id = "abc12345"
        resource_map: dict = {}
        allowed_hosts: set = set()

    text = _playlist(32_000)
    session = _Session()

    started = time.perf_counter()
    out = _rewrite_playlist(text, session, _BASE_URL)
    elapsed = time.perf_counter() - started

    assert out.count("/resource?id=") == 32_001  # 32k segments + the key URI
    assert "video-weaver.example" in session.allowed_hosts
    assert elapsed < 1.5, (
        f"rewriting a 32k-line playlist took {elapsed:.2f}s; it used to take 4.0s "
        "and the whole preview click is supposed to finish in ~1s"
    )
