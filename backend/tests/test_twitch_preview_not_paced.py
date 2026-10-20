"""A Twitch preview click must never be paced as background work.

``rate_budget.acquire`` splits callers into an AUTO pool (scheduled/background
work, which may sleep up to ``MAX_AUTO_WAIT_S`` = 30 s when the learned budget
is dry) and a USER pool (never waits). The Twitch GQL client defaulted every
caller to AUTO, and the preview-session resolve inherited that default.

Measured 2026-10-20 on the live app: ``POST /api/preview/session`` for a Twitch
VOD took **49-57 s** with the archive scheduler running, while the identical
resolve is **0.6-1.4 s** with no pacing - and the second identical click, once
the queue had drained, answered in **3.1 s**. The click was not slow; it was
sleeping in the background queue.

``create_session`` now defaults to ``origin="user"`` and threads it down to the
GQL layer, matching what the Kick resolver already did
(``resolve_kick_stream_api(url, origin="user")``).
"""
from __future__ import annotations

import inspect

import pytest


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


_MASTER = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720\n"
    "https://video-weaver.example/720p/index-dvr.m3u8\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=852x480\n"
    "https://video-weaver.example/480p/index-dvr.m3u8\n"
).encode()


def test_get_vod_playback_sync_passes_the_origin_to_gql(monkeypatch):
    from services import twitch_gql_service as gql

    seen: list[str] = []

    def _fake_persisted(operation_name, sha256_hash, variables, *, source="auto"):
        seen.append(source)
        return {
            "videoPlaybackAccessToken": {"signature": "sig", "value": "tok"},
        }

    monkeypatch.setattr(gql, "_gql_persisted", _fake_persisted)
    monkeypatch.setattr(
        gql._GQL_OPENER, "open", lambda *a, **k: _FakeResponse(_MASTER)
    )

    gql.get_vod_playback_sync("https://www.twitch.tv/videos/1234", origin="user")
    assert seen == ["user"], (
        "a user-origin resolve must reach GQL as 'user' or it sleeps in the AUTO "
        f"queue (saw {seen})"
    )


def test_get_vod_playback_sync_defaults_to_auto(monkeypatch):
    """Background warms must stay in the paced pool."""
    from services import twitch_gql_service as gql

    seen: list[str] = []

    def _fake_persisted(operation_name, sha256_hash, variables, *, source="auto"):
        seen.append(source)
        return {"videoPlaybackAccessToken": {"signature": "s", "value": "t"}}

    monkeypatch.setattr(gql, "_gql_persisted", _fake_persisted)
    monkeypatch.setattr(
        gql._GQL_OPENER, "open", lambda *a, **k: _FakeResponse(_MASTER)
    )

    gql.get_vod_playback_sync("https://www.twitch.tv/videos/1234")
    assert seen == ["auto"]


def test_resolve_stream_info_threads_origin_into_the_twitch_vod_resolve(monkeypatch):
    from services import twitch_gql_service as gql
    from services.preview import session as preview_session

    seen: list[str] = []

    def _fake_vod(url_or_id, *, origin="auto"):
        seen.append(origin)
        return (
            "https://usher.ttvnw.net/vod/v2/1.m3u8",
            {},
            [{"url": "https://x/1.m3u8", "height": 480}],
        )

    monkeypatch.setattr(gql, "get_vod_playback_sync", _fake_vod)

    preview_session.resolve_stream_info(
        "https://www.twitch.tv/videos/999000111", prefer_height=480, origin="user"
    )
    assert seen == ["user"]


def test_create_session_defaults_to_user_origin():
    from services.preview.session import PreviewManager

    sig = inspect.signature(PreviewManager.create_session)
    assert sig.parameters["origin"].default == "user", (
        "create_session is only ever reached by a user waiting on a click; "
        "defaulting to 'auto' puts that click in the paced background queue"
    )


def test_the_router_does_not_override_the_user_origin():
    from routers import preview as routes

    source = inspect.getsource(routes.preview_create_session)
    assert 'origin="auto"' not in source
    assert "origin=" not in source or 'origin="user"' in source
