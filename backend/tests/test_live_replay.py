"""Self-check for live-player DVR (REPLAY) + ongoing-VOD helpers.

Run from repo root with:
    python -m backend.tests.test_live_replay
or from the backend directory with:
    python -m tests.test_live_replay

Covers: media-playlist totalduration probing, the growing-VOD crop clamp,
the low_latency master downgrade, the replay ENDLIST snapshot resource, and
the per-session playlist TTL. Network is stubbed — no upstream calls.
"""
import time
from pathlib import Path

import pytest

from services import preview
from services.preview import session as session_mod
from services.preview.hls import proxy_playlist
from services.preview.session import (
    REPLAY_PLAYLIST_RESOURCE,
    _apply_growing_vod_duration,
    _manager,
    _playlist_cache,
    _playlist_total_duration,
    _strip_low_latency_param,
    _twitch_login_from_master,
    _kick_slug_from_master,
    open_replay_hls_proxy,
)

LIVE_PLAYLIST = b"""#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:6
#EXT-X-PART-INF:PART-TARGET=1.0
#EXTINF:6.000,
http://cdn.example.com/seg-1.ts
#EXTINF:6.000,
http://cdn.example.com/seg-2.ts
"""

ARCHIVE_GROWING = b"""#EXTM3U
#EXT-X-TARGETDURATION:6
#EXTINF:6.000,
http://cdn.example.com/arch-1.ts
#EXTINF:6.000,
http://cdn.example.com/arch-2.ts
#EXTINF:6.000,
http://cdn.example.com/arch-3.ts
"""


def _mk_session(sid: str, **over) -> session_mod.PreviewSession:
    kwargs = dict(
        session_id=sid,
        vod_url="http://cdn.example.com/live.m3u8",
        master_url="http://cdn.example.com/live.m3u8",
        entry_url="http://cdn.example.com/media.m3u8",
        platform="Twitch",
        http_headers={},
        allowed_hosts={"cdn.example.com"},
        cache_dir=Path("/tmp/preview-test-" + sid),
        kind="hls",
        crop_start=0.0,
        crop_end=3600.0,
    )
    kwargs.update(over)
    return session_mod.PreviewSession(**kwargs)


def _stub_http_get_bytes(fixture: bytes):
    def _fake(session, url, range_header=None, **_kw):
        return fixture, "application/vnd.apple.mpegurl", {}, 200

    import services.preview.hls as hls_mod

    hls_mod._http_get_bytes = _fake


def test_playlist_total_duration() -> None:
    assert _playlist_total_duration(LIVE_PLAYLIST.decode()) == 12.0
    assert _playlist_total_duration("#EXTM3U\n#EXT-X-ENDLIST\n") == 0.0
    assert _playlist_total_duration("garbage") == 0.0


def test_strip_low_latency_param() -> None:
    url = (
        "https://usher.ttvnw.net/api/channel/hls/monstercat.m3u8?"
        "allow_source=true&low_latency=true&p=42&nauth=x&nauthsig=y"
    )
    out = _strip_low_latency_param(url)
    assert out is not None and "low_latency" not in out
    assert "nauth=x" in out and "nauthsig=y" in out and "p=42" in out
    assert "allow_source=true" in out


def test_slug_extraction() -> None:
    assert _twitch_login_from_master(
        "https://usher.ttvnw.net/api/channel/hls/monstercat.m3u8?p=1"
    ) == "monstercat"
    assert _twitch_login_from_master("https://x/y.m3u8") is None
    assert _kick_slug_from_master("https://kick.com/hls/foo/index.m3u8") == "foo"
    assert _kick_slug_from_master("https://playback.live-video.net/x/y.m3u8") is None


def test_apply_growing_vod_duration() -> None:
    _stub_http_get_bytes(ARCHIVE_GROWING)
    sess = _mk_session("g1", crop_end=3600.0, vod_duration=0.0)
    _apply_growing_vod_duration(sess)
    assert sess.growing_vod is True
    assert sess.vod_duration == 18.0
    # 3600 placeholder crop_end replaced with the probed length
    assert sess.crop_end == 18.0

    # Completed VOD (ENDLIST present): duration still probed, flag False.
    _stub_http_get_bytes(ARCHIVE_GROWING + b"#EXT-X-ENDLIST\n")
    sess2 = _mk_session("g2", crop_end=3600.0)
    _apply_growing_vod_duration(sess2)
    assert sess2.growing_vod is False
    assert sess2.vod_duration == 18.0

    # Real client crop_end (< 3600) is preserved.
    _stub_http_get_bytes(ARCHIVE_GROWING)
    sess3 = _mk_session("g3", crop_end=750.0)
    _apply_growing_vod_duration(sess3)
    assert sess3.vod_duration == 18.0
    assert sess3.crop_end == 750.0

    # YouTube sessions are never probed (their metadata path is authoritative).
    _stub_http_get_bytes(ARCHIVE_GROWING)
    sess4 = _mk_session("g4", platform="YouTube")
    _apply_growing_vod_duration(sess4)
    assert sess4.growing_vod is False and sess4.vod_duration == 0.0


def test_replay_snapshot_endpoint() -> None:
    _stub_http_get_bytes(ARCHIVE_GROWING)
    sess = _mk_session("r1", archive_entry_url="http://cdn.example.com/arch-media.m3u8")
    sess.resource_map[REPLAY_PLAYLIST_RESOURCE] = "replay-hls:playlist"
    _manager._sessions["r1"] = sess
    try:
        gen, ctype, _hdrs, status, _cleanup = open_replay_hls_proxy("r1", REPLAY_PLAYLIST_RESOURCE)
        body = b"".join(gen())
        assert status == 200 and ctype == "application/vnd.apple.mpegurl"
        assert body.rstrip().endswith(b"#EXT-X-ENDLIST")
        # segment URIs rewritten through the session proxy (never upstream CDN)
        assert b"cdn.example.com/arch" not in body
        assert b"/api/preview/hls/r1/resource?id=" in body
        assert b"#EXTINF:6.000" in body
        assert sess.archive_duration == 18.0
        # re-snapshot picks up growth (4th segment added upstream)
        _stub_http_get_bytes(ARCHIVE_GROWING + b"#EXTINF:6.000,\nhttp://cdn.example.com/arch-4.ts\n")
        gen2, _c2, _h2, _s2, _cl2 = open_replay_hls_proxy("r1", REPLAY_PLAYLIST_RESOURCE)
        body2 = b"".join(gen2())
        assert body2.count(b"#EXTINF") == 4
        assert sess.archive_duration == 24.0
    finally:
        _manager._sessions.pop("r1", None)


def test_per_session_playlist_ttl() -> None:
    import services.preview.hls as hls_mod

    calls: list[str] = []

    def _fake_fetch(session, upstream_url):
        calls.append(upstream_url)
        return b"#EXTM3U\n#EXTINF:6.0,\nhttp://cdn.example.com/s.ts\n", 200

    real_fetch = hls_mod._fetch_and_rewrite_playlist_streaming
    hls_mod._fetch_and_rewrite_playlist_streaming = _fake_fetch
    try:
        # Live session: media playlist cache (1s old) is STALE at ttl 0.5s.
        live = _mk_session("t1", playlist_ttl_sec=0.5)
        cache = _playlist_cache(live)
        cache["http://cdn.example.com/media.m3u8"] = (b"#EXTM3U\n", time.time() - 1.0)
        _manager._sessions["t1"] = live
        proxy_playlist("t1", "http://cdn.example.com/media.m3u8")
        assert calls == ["http://cdn.example.com/media.m3u8"], "0.5s TTL must refetch a 1s-old media playlist"

        # Live session master keeps the longer default TTL (2s) — cache HIT.
        calls.clear()
        cache["http://cdn.example.com/live.m3u8"] = (b"#EXTM3U\n", time.time() - 1.0)
        proxy_playlist("t1", "http://cdn.example.com/live.m3u8")
        assert calls == [], "master must keep the default 2s TTL"

        # VOD session (no override): media playlist uses default TTL — cache HIT.
        calls.clear()
        vod = _mk_session("t2")
        vcache = _playlist_cache(vod)
        vcache["http://cdn.example.com/media.m3u8"] = (b"#EXTM3U\n", time.time() - 1.0)
        _manager._sessions["t2"] = vod
        proxy_playlist("t2", "http://cdn.example.com/media.m3u8")
        assert calls == [], "VOD sessions keep the default 2s TTL"
    finally:
        hls_mod._fetch_and_rewrite_playlist_streaming = real_fetch
        _manager._sessions.pop("t1", None)
        _manager._sessions.pop("t2", None)


def test_live_session_archive_fields() -> None:
    # create_live_session wires is_live/playlist_ttl/archive resource map.
    import services.preview.hls as hls_mod

    MASTER = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\nhttp://cdn.example.com/media.m3u8\n"

    def _fake_fetch(session, upstream_url):
        if upstream_url.endswith("master.m3u8"):
            return MASTER, 200
        return LIVE_PLAYLIST, 200

    def _fake_bytes(session, url, range_header=None, **_kw):
        if url.endswith("master.m3u8"):
            return MASTER, "application/vnd.apple.mpegurl", {}, 200
        return LIVE_PLAYLIST, "application/vnd.apple.mpegurl", {}, 200

    import services.twitch_gql_service as gql_mod

    real_gql = gql_mod.get_vod_playback_sync
    gql_mod.get_vod_playback_sync = lambda vod: (
        "http://cdn.example.com/archive-master.m3u8",
        {"Referer": "https://www.twitch.tv/"},
        [],
    )
    real_fetch = hls_mod._fetch_and_rewrite_playlist_streaming
    real_bytes = hls_mod._http_get_bytes
    hls_mod._fetch_and_rewrite_playlist_streaming = _fake_fetch
    hls_mod._http_get_bytes = _fake_bytes
    try:
        sess = preview.create_live_session(
            "http://cdn.example.com/master.m3u8",
            {},
            "Twitch",
            vod_url="",
        )
        assert sess.is_live is True
        assert sess.playlist_ttl_sec == 0.5
        assert sess.entry_url == "http://cdn.example.com/media.m3u8"
        # archive resolve is lazy (off the playback critical path)
        assert sess.archive_url is None and sess.archive_entry_url is None
        assert REPLAY_PLAYLIST_RESOURCE not in sess.resource_map

        sess2 = preview.create_live_session(
            "http://cdn.example.com/master.m3u8",
            {},
            "Twitch",
            vod_url="https://www.twitch.tv/videos/123456789",
        )
        # deferred: no archive until the first replay request resolves it
        assert sess2.archive_url is None and sess2.archive_entry_url is None
        assert preview._manager._ensure_live_archive(sess2) is True
        assert sess2.archive_url == "http://cdn.example.com/archive-master.m3u8"
        assert sess2.archive_entry_url == "http://cdn.example.com/media.m3u8"
        assert sess2.resource_map.get(REPLAY_PLAYLIST_RESOURCE) == "replay-hls:playlist"
        # idempotent; resolve_upstream triggers the same lazy path
        assert preview._manager._ensure_live_archive(sess2) is True
        assert (
            preview.resolve_upstream(sess2.session_id, REPLAY_PLAYLIST_RESOURCE)
            == "replay-hls:playlist"
        )
        # no archive → replay resource 404s (frontend rail stays off)
        with pytest.raises(ValueError):
            preview.resolve_upstream(sess.session_id, REPLAY_PLAYLIST_RESOURCE)
    finally:
        gql_mod.get_vod_playback_sync = real_gql
        hls_mod._fetch_and_rewrite_playlist_streaming = real_fetch
        hls_mod._http_get_bytes = real_bytes


if __name__ == "__main__":
    test_playlist_total_duration()
    test_strip_low_latency_param()
    test_slug_extraction()
    test_apply_growing_vod_duration()
    test_replay_snapshot_endpoint()
    test_per_session_playlist_ttl()
    test_live_session_archive_fields()
    print("test_live_replay self-check OK")
