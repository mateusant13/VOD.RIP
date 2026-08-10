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
