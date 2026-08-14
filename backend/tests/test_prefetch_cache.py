"""Instant-preview prefetch — selection math, cache-key/path logic, and the
proxy serve path (prefetched prefix served; beyond-prefix falls through).

Env vars mirror test_instant_preview.py — everything lands in scratch dirs.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ["VODRIP_ARCHIVE_DB"] = str(
    Path(tempfile.mkdtemp(prefix="prefetch-")) / "archive.db")
os.environ["VODRIP_APP_DATA"] = str(Path(tempfile.mkdtemp(prefix="prefetch-app-")))
os.environ["VODRIP_DATA_DIR"] = str(Path(tempfile.mkdtemp(prefix="prefetch-data-")))
os.environ["VODRIP_CACHE_DIR"] = str(Path(tempfile.mkdtemp(prefix="prefetch-cache-")))

import pytest  # noqa: E402

from services import prefetch_cache as pc  # noqa: E402
from services import preview_service  # noqa: E402
from services.preview import hls as hls_mod  # noqa: E402
from services.preview.session import PreviewSession  # noqa: E402

_TW = "https://www.twitch.tv/videos/1234567890"
_PL_URL = "https://cdn.example.com/vod/720p30/index-dvr.m3u8"


def _session(sid="pfx-test", vod_url=_TW, platform="Twitch", cache_dir="."):
    s = PreviewSession(
        session_id=sid,
        vod_url=vod_url,
        master_url="https://cdn.example.com/master.m3u8",
        entry_url=_PL_URL,
        platform=platform,
        http_headers={},
        allowed_hosts={"cdn.example.com"},
        cache_dir=Path(cache_dir),
        kind="hls",
        crop_start=0.0,
        crop_end=8.0,
        prefer_height=720,
    )
    preview_service._manager._sessions[sid] = s
    return s


@pytest.fixture(autouse=True)
def _cleanup_sessions():
    yield
    for sid in [k for k in preview_service._manager._sessions]:
        preview_service._manager._sessions.pop(sid, None)


def _register(session):
    preview_service._manager._sessions[session.session_id] = session
    return session


# ---------------------------------------------------------------------------
# Cache-key / path logic
# ---------------------------------------------------------------------------

def test_platform_video_id_shapes():
    s = _session(vod_url="https://www.twitch.tv/videos/2845537985")
    assert pc._platform_video_id(s) == ("twitch", "2845537985")
    s = _session(vod_url="https://www.twitch.tv/xqc/videos/2845537985")
    assert pc._platform_video_id(s) == ("twitch", "2845537985")
    s = _session(
        vod_url="https://kick.com/xqc/videos/dedcf0c6-1c74-4767-8049-9319f77fab6c",
        platform="Kick",
    )
    assert pc._platform_video_id(s) == ("kick", "dedcf0c6-1c74-4767-8049-9319f77fab6c")
    s = _session(
        vod_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", platform="YouTube"
    )
    assert pc._platform_video_id(s) == ("youtube", "dQw4w9WgXcQ")
    s = _session(vod_url="https://www.twitch.tv/clip/Slug")
    assert pc._platform_video_id(s) is None  # clip — not a VOD
    s = _session(vod_url="")
    assert pc._platform_video_id(s) is None  # live session — no vod_url
    s = _session(vod_url=_TW, platform="Kick")
    assert pc._platform_video_id(s) is None  # platform/URL mismatch


def test_segment_path_is_per_video_and_per_url(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path))
    p1 = pc._segment_path("twitch", "111", "https://cdn/x/0.ts")
    p2 = pc._segment_path("twitch", "111", "https://cdn/x/1.ts")
    p3 = pc._segment_path("twitch", "222", "https://cdn/x/0.ts")
    assert p1 != p2 and p1 != p3
    assert p1.parent == pc._video_dir("twitch", "111")
    assert p1.name.startswith("seg_") and p1.suffix == ".bin"
    # same inputs → same path (stable key)
    assert p1 == pc._segment_path("twitch", "111", "https://cdn/x/0.ts")


def test_lookup_returns_bytes_on_hit_none_on_miss(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path))
    seg_url = "https://cdn.example.com/vod/720p30/0.ts"
    payload = b"\x00SEGDATA\xff" * 100
    pc._video_dir("twitch", "1234567890").mkdir(parents=True)
    pc._segment_path("twitch", "1234567890", seg_url).write_bytes(payload)

    s = _session()
    assert pc.lookup_prefetched_segment(s, seg_url) == payload
    # unknown segment URL → miss (falls through upstream)
    assert pc.lookup_prefetched_segment(s, "https://cdn.example.com/vod/720p30/9.ts") is None
    # playlists never served as segments
    assert pc.lookup_prefetched_segment(s, _PL_URL) is None
    # different video id → miss
    s2 = _session(vod_url="https://www.twitch.tv/videos/999999")
    assert pc.lookup_prefetched_segment(s2, seg_url) is None


# ---------------------------------------------------------------------------
# Selection math + eviction
# ---------------------------------------------------------------------------

def _channels(*twitch_slugs):
    return [
        {"id": f"c{i}", "twitchSlug": slug}
        for i, slug in enumerate(twitch_slugs)
    ]


def test_pass_selects_top5_and_evicts_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path))
    # 7 archived VODs for the channel; only the 5 newest may be prefetched.
    rows = [{"video_id": f"10000{i}"} for i in range(1, 8)]  # 100001..100007
    calls: list[str] = []

    def _fake_query(sql, params=()):
        # emulate the real SQL's `ORDER BY started_at DESC ... LIMIT ?` (rows are
        # inserted oldest-first) — the pass must only ever see the top-5, newest
        # first; anything older is the evictor's job
        limit = params[-1] if params else None
        newest = list(reversed(rows))
        return newest[:limit] if limit else newest

    def _fake_prefetch(platform, video_id, channel):
        calls.append(video_id)
        pc._touch_manifest(platform, video_id, channel, fetched=True)  # like the real leg
        return True

    monkeypatch.setattr(pc.archive_db, "query", _fake_query)
    monkeypatch.setattr(pc, "_prefetch_video", _fake_prefetch)
    monkeypatch.setattr(pc, "PREFETCH_PER_PASS", 10)

    # Pre-existing stale dir for an OLD video that fell off the top-5, plus a
    # 6th-place dir that must survive while in top-5.
    pc._video_dir("twitch", "100001").mkdir(parents=True)  # oldest
    pc._video_dir("twitch", "100006").mkdir(parents=True)  # 6th-newest

    stats = pc.run_prefetch_pass(_channels("xqc"))
    assert stats["fetched"] == 5
    assert stats["evicted"] >= 1
    assert calls == [f"10000{i}" for i in range(7, 2, -1)]  # top-5, newest-first
    # oldest (100001) evicted, 6th (100006) kept
    assert not (tmp_path / "prefetch/twitch/100001").exists()
    assert (tmp_path / "prefetch/twitch/100006").exists()

    # A NEWER VOD appears → the old 5th-place dir is dropped, new one fetched.
    rows.append({"video_id": "100008"})  # rows ascend oldest→newest
    calls.clear()
    stats = pc.run_prefetch_pass(_channels("xqc"))
    assert "100008" in calls
    assert stats["evicted"] >= 1
    # The old 5th-place (100003) fell off the top-5 and its prefetch is dropped;
    # 100007 is still top-5 and must survive.
    assert not (tmp_path / "prefetch/twitch/100003").exists(), (
        "out-of-top-5 prefetch must be dropped when a newer VOD appears"
    )
    assert (tmp_path / "prefetch/twitch/100007").exists()


def test_pass_keeps_top5_of_every_saved_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path))
    channel_rows = {
        "xqc": [{"video_id": f"a{i}"} for i in range(1, 6)],
        "loltyler1": [{"video_id": f"b{i}"} for i in range(1, 6)],
    }
    fetched: list[str] = []

    def _fake_query(sql, params=()):
        limit = params[-1] if params else None
        rows = list(channel_rows.get(params[1], []))
        return rows[:limit] if limit else rows

    def _fake_prefetch(platform, video_id, channel):
        fetched.append(video_id)
        return True

    monkeypatch.setattr(pc.archive_db, "query", _fake_query)
    monkeypatch.setattr(pc, "_prefetch_video", _fake_prefetch)
    monkeypatch.setattr(pc, "PREFETCH_PER_PASS", 10)
    # Stale dir belonging to neither channel's top-5 — must be evicted.
    pc._video_dir("twitch", "orphan").mkdir(parents=True)

    stats = pc.run_prefetch_pass(_channels("xqc", "loltyler1"))
    assert stats["fetched"] == 10
    assert sorted(fetched) == sorted([f"a{i}" for i in range(1, 6)] + [f"b{i}" for i in range(1, 6)])
    assert not (tmp_path / "prefetch/twitch/orphan").exists()


def test_video_due_freshness_and_failure_backoff(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path))
    d = pc._video_dir("twitch", "v1")
    d.mkdir(parents=True)
    (d / "manifest.json").write_text('{"fetched_at": %r}' % __import__("time").time())
    assert pc._video_due("twitch", "v1") is False  # fresh — skip

    (d / "manifest.json").write_text('{"fetched_at": %r}' % (__import__("time").time() - 7200))
    assert pc._video_due("twitch", "v1") is True  # stale — refetch

    (d / "manifest.json").write_text('{"failed_at": %r}' % __import__("time").time())
    assert pc._video_due("twitch", "v1") is False  # failed recently — backoff

    (d / "manifest.json").write_text('{"failed_at": %r}' % (__import__("time").time() - 7200))
    assert pc._video_due("twitch", "v1") is True

    assert pc._video_due("twitch", "nope") is True  # never tried


# ---------------------------------------------------------------------------
# Fetch leg — prefix selection + storage
# ---------------------------------------------------------------------------

_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    '#EXT-X-MAP:URI="init.mp4"\n'
    + "".join(f"#EXTINF:2.0,\n{i}.ts\n" for i in range(8))
    + "#EXT-X-ENDLIST\n"
)


def test_prefetch_hls_leg_stores_prefix_and_playlist(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path))
    seg_bytes = {f"https://cdn.example.com/vod/720p30/{n}.ts": f"SEG{n}".encode() * 50 for n in range(8)}
    seg_bytes["https://cdn.example.com/vod/720p30/init.mp4"] = b"INITBYTES"
    fetched: list[str] = []

    def _fake_get(session, url, range_header=None, **_kw):
        fetched.append(url)
        if url == _PL_URL:
            return _PLAYLIST.encode(), "application/vnd.apple.mpegurl", {}, 200
        body = seg_bytes.get(url)
        if body is None:
            raise RuntimeError(f"unexpected fetch {url}")
        return body, "video/mp2t", {}, 200

    monkeypatch.setattr(pc, "_http_get_bytes", _fake_get)
    s = _session(cache_dir=str(tmp_path / "sess"))
    ok = pc._prefetch_hls_leg(s, "twitch", "1234567890", {"id": "c1"})
    assert ok is True

    # 8s prefix at 2s segments = segments 0..3 (4 cover exactly 8s) + the init.
    assert fetched[0] == _PL_URL
    assert "https://cdn.example.com/vod/720p30/0.ts" in fetched
    assert "https://cdn.example.com/vod/720p30/3.ts" in fetched
    assert "https://cdn.example.com/vod/720p30/4.ts" not in fetched, (
        "beyond-prefix segments must not be fetched"
    )
    # Raw bytes stored + playable through the serve path.
    assert pc.lookup_prefetched_segment(s, "https://cdn.example.com/vod/720p30/0.ts") == seg_bytes["https://cdn.example.com/vod/720p30/0.ts"]
    assert pc.lookup_prefetched_segment(s, "https://cdn.example.com/vod/720p30/4.ts") is None
    assert pc.lookup_prefetched_segment(s, "https://cdn.example.com/vod/720p30/init.mp4") == b"INITBYTES"
    # Complete playlist stored.
    assert pc._playlist_path("twitch", "1234567890", _PL_URL).is_file()


def test_prefetch_hls_leg_growing_vod_skips_playlist(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path))
    growing = _PLAYLIST.replace("#EXT-X-ENDLIST\n", "")  # in-progress broadcast

    def _fake_get(session, url, range_header=None, **_kw):
        if url == _PL_URL:
            return growing.encode(), "application/vnd.apple.mpegurl", {}, 200
        return b"SEG", "video/mp2t", {}, 200

    monkeypatch.setattr(pc, "_http_get_bytes", _fake_get)
    s = _session(cache_dir=str(tmp_path / "sess"))
    assert pc._prefetch_hls_leg(s, "twitch", "1234567890", {"id": "c1"}) is True
    # Segments cached, but the playlist is NOT (it keeps changing) → the
    # playlist serve path must miss and fall through upstream.
    assert pc.lookup_prefetched_segment(s, "https://cdn.example.com/vod/720p30/0.ts") == b"SEG"
    assert not pc._playlist_path("twitch", "1234567890", _PL_URL).exists()


# ---------------------------------------------------------------------------
# Proxy serve path — prefetched prefix served, beyond-prefix falls through
# ---------------------------------------------------------------------------

def _make_fetch_counter():
    state = {"calls": 0}

    def _fake_get(session, url, range_header=None, **_kw):
        state["calls"] += 1
        return b"UPSTREAM-BYTES", "video/mp2t", {"Content-Length": "13"}, 200

    return state, _fake_get


def test_proxy_segment_serves_prefetched_segment_without_upstream(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path))
    seg_url = "https://cdn.example.com/vod/720p30/0.ts"
    pc._video_dir("twitch", "1234567890").mkdir(parents=True)
    payload = b"PREFETCHED-SEGMENT-BYTES"
    pc._segment_path("twitch", "1234567890", seg_url).write_bytes(payload)

    state, fake = _make_fetch_counter()
    monkeypatch.setattr(hls_mod, "_http_get_bytes", fake)
    _register(_session(cache_dir=str(tmp_path / "sess")))
    body, ctype, headers, status = hls_mod.proxy_segment("pfx-test", seg_url)
    assert body == payload
    assert state["calls"] == 0, "prefetched segment must not hit upstream"
    # Range request on the cached segment → 206 slice.
    body2, _ct, hdrs2, status2 = hls_mod.proxy_segment("pfx-test", seg_url, "bytes=0-3")
    assert status2 == 206 and body2 == payload[:4]
    assert hdrs2.get("Content-Range") == f"bytes 0-3/{len(payload)}"


def test_proxy_segment_falls_through_beyond_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path))
    # Only segment 0 is prefetched; segment 5 (past the 8s prefix) must go
    # upstream — and unknown videos must never touch the prefetch cache.
    pc._video_dir("twitch", "1234567890").mkdir(parents=True)
    pc._segment_path("twitch", "1234567890", "https://cdn.example.com/vod/720p30/0.ts").write_bytes(b"X" * 64)

    state, fake = _make_fetch_counter()
    monkeypatch.setattr(hls_mod, "_http_get_bytes", fake)
    _register(_session(cache_dir=str(tmp_path / "sess")))
    body, _ct, _h, status = hls_mod.proxy_segment("pfx-test", "https://cdn.example.com/vod/720p30/5.ts")
    assert body == b"UPSTREAM-BYTES"
    assert status == 200
    assert state["calls"] == 1, "beyond-prefix segment must fall through to upstream"

    # Non-VOD session (live) — no prefetch lookup, straight upstream.
    state2, fake2 = _make_fetch_counter()
    monkeypatch.setattr(hls_mod, "_http_get_bytes", fake2)
    _register(_session(sid="live-test", vod_url="", platform="Twitch", cache_dir=str(tmp_path / "sess2")))
    body2, _ct, _h2, _st = hls_mod.proxy_segment("live-test", "https://cdn.example.com/vod/720p30/0.ts")
    assert body2 == b"UPSTREAM-BYTES"
    assert state2["calls"] == 1


def test_proxy_playlist_serves_prefetched_playlist(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path))
    pc._write_playlist("twitch", "1234567890", _PL_URL, _PLAYLIST)

    state, fake = _make_fetch_counter()
    monkeypatch.setattr(hls_mod, "_http_get_bytes", fake)
    _register(_session(cache_dir=str(tmp_path / "sess")))
    body, ctype, _h, status = hls_mod.proxy_playlist("pfx-test", _PL_URL)
    assert status == 200
    assert ctype == "application/vnd.apple.mpegurl"
    assert body.lstrip().startswith(b"#EXTM3U")
    # Rewritten to this session's proxy resource URLs.
    assert b"/api/preview/hls/pfx-test/resource?id=" in body
    assert state["calls"] == 0, "prefetched playlist must not hit upstream"
    # Second request serves from the session playlist cache (same bytes).
    body2, _ct, _h2, _st2 = hls_mod.proxy_playlist("pfx-test", _PL_URL)
    assert body2 == body


def test_proxy_playlist_falls_through_for_unknown_url(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path))
    # _fetch_and_rewrite_playlist_streaming (session.py) reads the upstream via
    # _open_upstream_stream + iter_content, NOT hls._http_get_bytes — patch the
    # module where the call actually resolves.
    import services.preview.session as _session_mod

    class _FakeResp:
        status_code = 200

        def __init__(self, body):
            self._body = body
            self.headers = {
                "Content-Length": str(len(body)),
                "Content-Type": "application/vnd.apple.mpegurl",
            }

        def iter_content(self, chunk_size=65536):
            for i in range(0, len(self._body), chunk_size):
                yield self._body[i : i + chunk_size]

        def close(self):
            pass

    calls = {"n": 0}
    original = _session_mod._open_upstream_stream

    def _fake_open(session, url, range_header=None, **_kw):
        calls["n"] += 1
        return _FakeResp(b"#EXTM3U\n#EXT-X-ENDLIST\n")

    _session_mod._open_upstream_stream = _fake_open
    try:
        _register(_session(cache_dir=str(tmp_path / "sess")))
        body, _ct, _h, status = hls_mod.proxy_playlist("pfx-test", _PL_URL)
        assert body == b"#EXTM3U\n#EXT-X-ENDLIST\n"
        assert status == 200
        assert calls["n"] == 1, "unknown playlist must fall through to upstream"
    finally:
        _session_mod._open_upstream_stream = original
