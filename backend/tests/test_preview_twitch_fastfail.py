"""Twitch VOD fast-fail contract — no network.

A sub-only/geo-restricted/removed Twitch VOD must fail fast with the explicit
TwitchVodUnavailable message instead of draining into the slow yt-dlp fallback
(repro: srdogg VOD took 47s before the fix).
"""

from __future__ import annotations

import pytest

import services.twitch_gql_service as tgs
from services.preview.session import resolve_stream_info

_URL = "https://www.twitch.tv/fastfail/videos/12345"


def test_twitch_vod_unavailable_fails_fast_with_clear_message(monkeypatch):
    def boom(url):
        raise tgs.TwitchVodUnavailable(
            "Twitch VOD 12345 has no playable stream — it is sub-only, "
            "geo-restricted, or removed (log in with Twitch cookies and retry)"
        )

    monkeypatch.setattr(tgs, "get_vod_playback_sync", boom)
    # If the fast-fail is broken the yt-dlp fallback runs; make that loud.
    monkeypatch.setattr(
        "services.preview.session._extract_hls_info",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("yt-dlp fallback ran — Twitch fast-fail broken")
        ),
    )

    with pytest.raises(tgs.TwitchVodUnavailable, match="has no playable stream"):
        resolve_stream_info(_URL, prefer_height=360)

import re

from fastapi.testclient import TestClient

from app import app
from routers import preview as preview_router


def test_metadata_without_a_cdn_path_is_terminal(monkeypatch):
    """Twitch ANSWERING with no seekPreviewsURL means no playable source exists.

    A bare RuntimeError there drained into the yt-dlp fallback, which reproduced
    the same verdict 13.5s later as an HTTP 500 (measured 2026-10-11 on VOD
    2883275549: token signature absent, metadata with no CDN path, then
    "[twitch:vod] ... does not exist").
    """
    monkeypatch.setattr(tgs, "_gql_request", lambda *_a, **_k: {"video": {"id": "12345"}})
    with pytest.raises(tgs.TwitchVodUnavailable, match="unavailable"):
        tgs._get_vod_meta_sync("12345")


def test_terminal_message_matches_the_client_permanent_failure_regex(monkeypatch):
    """The client only stops retrying on a message matching its permanent-failure
    regex (src/previewPlayerUtils.ts: /members-only|membership|unavailable|private|removed/i).
    A message that misses it costs 3 POSTs x ~13-19s of spinner for a dead VOD.
    """
    client_fatal = re.compile(r"members-only|membership|unavailable|private|removed", re.I)
    monkeypatch.setattr(tgs, "_gql_request", lambda *_a, **_k: {"video": {}})
    with pytest.raises(tgs.TwitchVodUnavailable) as exc:
        tgs._get_vod_meta_sync("2883275549")
    assert client_fatal.search(str(exc.value)), str(exc.value)


def test_router_maps_a_dead_twitch_vod_to_404(monkeypatch):
    """The user-visible contract: 404 + the explicit message, never a retryable 500."""

    def _dead(*_a, **_k):
        raise tgs.TwitchVodUnavailable(
            "Twitch VOD 2883275549 is unavailable — Twitch reports no playable "
            "source for it (removed, expired, or sub-only)"
        )

    monkeypatch.setattr(preview_router, "create_session", _dead)
    with TestClient(app) as c:
        resp = c.post("/api/preview/session", json={"url": _URL})
    assert resp.status_code == 404, resp.text[:300]
    assert "unavailable" in resp.json()["detail"].lower(), resp.text[:300]

def test_metadata_without_a_cdn_path_is_not_terminal_when_twitch_issued_a_token(
    monkeypatch,
):
    """Reviewer finding (gpt, 2026-10-11): a missing seekPreviewsURL alone is not
    proof that a VOD is unplayable - it is proof only when Twitch ALSO refused the
    playback token. With a token issued (Usher merely failed), the failure must stay
    non-terminal so the caller can still fall back to yt-dlp.
    """
    monkeypatch.setattr(tgs, "_gql_request", lambda *_a, **_k: {"video": {"id": "999"}})
    with pytest.raises(RuntimeError) as exc:
        tgs._get_vod_meta_sync("999", token_refused=False)
    assert not isinstance(exc.value, tgs.TwitchVodUnavailable), type(exc.value)
    assert "seekPreviewsURL" in str(exc.value)


def test_token_refusal_makes_the_same_response_terminal(monkeypatch):
    """The same metadata is terminal when Twitch refused the token. The two branches
    must not be collapsed into one, or one of them is wrong.
    """
    monkeypatch.setattr(tgs, "_gql_request", lambda *_a, **_k: {"video": {"id": "999"}})
    with pytest.raises(tgs.TwitchVodUnavailable):
        tgs._get_vod_meta_sync("999", token_refused=True)


def test_playback_sync_passes_the_measured_token_state(monkeypatch):
    """The call site must pass the token state it measured, not the default: if it
    ever hard-codes the default, an issued token still reads as a refusal and a
    playable VOD is rejected as terminal.
    """
    import urllib.error

    seen = {}

    def _meta(vod_id, token_refused=True):
        seen["vod_id"] = vod_id
        seen["token_refused"] = token_refused
        raise RuntimeError("stop here")

    def _boom(*_a, **_k):
        raise urllib.error.URLError("usher down")

    monkeypatch.setattr(tgs, "_get_vod_meta_sync", _meta)
    monkeypatch.setattr(
        tgs,
        "_gql_persisted",
        lambda *_a, **_k: {"videoPlaybackAccessToken": {"signature": "s", "value": "v"}},
    )
    monkeypatch.setattr(tgs, "twitch_http_headers", lambda: {})
    monkeypatch.setattr(tgs.urllib.request, "urlopen", _boom)
    with pytest.raises(RuntimeError, match="stop here"):
        tgs.get_vod_playback_sync("https://www.twitch.tv/videos/999")
    assert seen.get("token_refused") is False, seen




def test_router_rejects_a_twitch_clip_fast_and_explicitly():
    """A clip is a different Twitch resource, not a VOD id.

    Measured 2026-10-11 against the live API: a clips.twitch.tv URL drained
    into the Twitch VOD chain and answered 500 "Preview failed - try again."
    after 8,034 ms. The contract is a fast explicit 400 naming the supported
    alternative - never a retryable 500, never a hang. Kick clips are
    canonicalized earlier and must keep working.
    """
    with TestClient(app) as c:
        resp = c.post(
            "/api/preview/session",
            json={"url": "https://clips.twitch.tv/AwkwardHelplessSalamanderSwiftRage"},
        )
    assert resp.status_code == 400, resp.text[:300]
    detail = str(resp.json()["detail"]).lower()
    assert "clip" in detail and "not supported" in detail, resp.text[:300]
    assert "videos/" in detail, "the message must name the working alternative"
