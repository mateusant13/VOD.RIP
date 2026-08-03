"""Self-check for live-stream detection / DVR helpers.

Run from repo root with:
    python -m backend.tests.test_live_capture
or from the backend directory with:
    python -m tests.test_live_capture
"""
from services.live_capture import (
    _TWITCH_MASTER_CACHE,
    _emit_progress,
    _twitch_master_for_player_type,
    probe_twitch_live_master,
)
from routers import live


def test_progress_hook_shape() -> None:
    calls: list[dict] = []

    def _hook(d: dict) -> None:
        calls.append(d)

    _emit_progress(b"size=123kB time=00:01:23.45 bitrate=...", _hook)
    assert len(calls) == 1, "progress hook should be called once for a time= line"
    d = calls[0]
    assert d.get("status") == "downloading"
    assert d.get("percent") == 0
    assert d.get("speed") == "live 83.45s"
    assert d.get("eta_seconds") is None


def test_live_router_imports() -> None:
    # Smoke test that the router module loads without import errors.
    assert hasattr(live, "router")
    assert hasattr(live, "check_live_status")
    assert hasattr(live, "channel_live_status")


# ---------------------------------------------------------------------------
# vaft stream-rotation probe (no network — decision logic only)
# ---------------------------------------------------------------------------


def _fake_builder(login: str, player_type: str):
    return {"url": f"https://usher.ttvnw.net/api/channel/hls/{login}.m3u8?pt={player_type}", "player_type": player_type}


def _install_probe_fakes(monkeypatch, has_ads):
    """Stub the three network touches of probe_twitch_live_master."""
    _TWITCH_MASTER_CACHE.clear()
    monkeypatch.setattr(
        "services.live_capture._twitch_master_for_player_type", _fake_builder
    )
    monkeypatch.setattr(
        "services.live_capture._twitch_pick_media_variant", lambda u: u
    )
    monkeypatch.setattr(
        "services.live_capture._twitch_media_has_ads", has_ads
    )


def test_probe_picks_first_ad_free_player_type(monkeypatch) -> None:
    # vaft order: embed (dirty), popout (clean) -> popout wins, autoplay never probed.
    _install_probe_fakes(monkeypatch, lambda media_url: "popout" not in media_url)
    seen: list[str] = []
    monkeypatch.setattr(
        "services.live_capture._twitch_master_for_player_type",
        lambda login, pt: (seen.append(pt), _fake_builder(login, pt))[1],
    )
    result = probe_twitch_live_master("SomeChannel")
    assert result is not None
    assert result["player_type"] == "popout"
    assert result["ad_free"] is True
    assert "pt=popout" in result["url"]
    assert seen == ["embed", "popout"], "probe should stop at the first clean type"


def test_probe_falls_back_to_embed_when_all_have_ads(monkeypatch) -> None:
    _install_probe_fakes(monkeypatch, lambda media_url: True)
    result = probe_twitch_live_master("monstercat")
    assert result is not None
    assert result["player_type"] == "embed"
    assert result["ad_free"] is False
    assert "pt=embed" in result["url"]


def test_probe_returns_none_when_every_player_type_fails(monkeypatch) -> None:
    _TWITCH_MASTER_CACHE.clear()
    monkeypatch.setattr(
        "services.live_capture._twitch_master_for_player_type", lambda login, pt: None
    )
    assert probe_twitch_live_master("downed") is None


def test_probe_cache_serves_hits_without_network(monkeypatch) -> None:
    _install_probe_fakes(monkeypatch, lambda media_url: False)
    seen: list[str] = []
    monkeypatch.setattr(
        "services.live_capture._twitch_master_for_player_type",
        lambda login, pt: (seen.append(pt), _fake_builder(login, pt))[1],
    )
    first = probe_twitch_live_master("cacheme")
    second = probe_twitch_live_master("cacheme")
    assert first == second
    assert seen == ["embed"], "cache hit must not rebuild/probe (3s live-poll loop)"


def test_probe_skip_cache_forces_refresh(monkeypatch) -> None:
    _install_probe_fakes(monkeypatch, lambda media_url: False)
    seen: list[str] = []
    monkeypatch.setattr(
        "services.live_capture._twitch_master_for_player_type",
        lambda login, pt: (seen.append(pt), _fake_builder(login, pt))[1],
    )
    probe_twitch_live_master("fresh")
    probe_twitch_live_master("fresh", skip_cache=True)
    assert seen == ["embed", "embed"], "rotation must always mint a fresh token"


def test_probe_respects_custom_player_order(monkeypatch) -> None:
    _install_probe_fakes(monkeypatch, lambda media_url: False)
    seen: list[str] = []
    monkeypatch.setattr(
        "services.live_capture._twitch_master_for_player_type",
        lambda login, pt: (seen.append(pt), _fake_builder(login, pt))[1],
    )
    probe_twitch_live_master("ord", player_types=("autoplay",), skip_cache=True)
    assert seen == ["autoplay"]


def test_probe_caches_only_when_not_skipped(monkeypatch) -> None:
    _install_probe_fakes(monkeypatch, lambda media_url: True)
    monkeypatch.setattr(
        "services.live_capture._twitch_master_for_player_type", _fake_builder
    )
    probe_twitch_live_master("cacher", skip_cache=True)
    assert "cacher" not in _TWITCH_MASTER_CACHE
    probe_twitch_live_master("cacher")
    assert "cacher" in _TWITCH_MASTER_CACHE


# ---------------------------------------------------------------------------
# POST /api/preview/live/rotate/{session_id} orchestration (no network)
# ---------------------------------------------------------------------------


def test_rotate_swaps_session_to_next_player_type(monkeypatch) -> None:
    from routers.preview import _rotate_live_twitch_session

    class FakeSession:
        platform = "Twitch"
        master_url = "https://usher.ttvnw.net/api/channel/hls/monstercat.m3u8?nauth=x"
        entry_url = master_url
        allowed_hosts = {"usher.ttvnw.net"}
        twitch_player_type = "embed"

    calls: list = []
    sess = FakeSession()

    def fake_get_session(sid):
        assert sid == "sid123"
        return sess

    def fake_probe(login, player_types=None, skip_cache=False):
        calls.append((login, tuple(player_types or ()), skip_cache))
        return {
            "url": f"https://usher.ttvnw.net/api/channel/hls/{login}.m3u8?pt={player_types[0]}",
            "headers": {"Referer": "https://www.twitch.tv/"},
            "player_type": player_types[0],
            "ad_free": True,
        }

    monkeypatch.setattr("routers.preview.get_session", fake_get_session)
    monkeypatch.setattr("services.live_capture.probe_twitch_live_master", fake_probe)

    out = _rotate_live_twitch_session("sid123", None)
    assert out["ok"] is True
    assert out["player_type"] == "popout"  # next after embed (vaft order)
    assert out["ad_free"] is True
    assert out["master_url"] == "/api/preview/hls/sid123/master.m3u8"
    # Session swapped in place — the same proxied URL now serves the new master.
    assert sess.master_url == out["url"]
    assert sess.twitch_player_type == "popout"
    assert sess.allowed_hosts == {"usher.ttvnw.net"}
    # Rotation mints a fresh token (skip_cache) with the next type first.
    assert calls == [("monstercat", ("popout", "embed", "autoplay"), True)]


def test_rotate_rejects_non_twitch_or_non_usher_sessions(monkeypatch) -> None:
    from routers.preview import _rotate_live_twitch_session

    class FakeYouTube:
        platform = "YouTube"
        master_url = "https://example.com/x.m3u8"

    monkeypatch.setattr("routers.preview.get_session", lambda sid: FakeYouTube())
    try:
        _rotate_live_twitch_session("sid", None)
        raise AssertionError("expected ValueError for non-Twitch session")
    except ValueError:
        pass

    class FakeSynthetic:
        platform = "Twitch"
        master_url = "http://localhost:5173/live/master.m3u8"  # e2e synthetic

    monkeypatch.setattr("routers.preview.get_session", lambda sid: FakeSynthetic())
    try:
        _rotate_live_twitch_session("sid", None)
        raise AssertionError("expected ValueError for non-usher master")
    except ValueError:
        pass

    class FakeMissing:
        pass

    monkeypatch.setattr("routers.preview.get_session", lambda sid: None)
    try:
        _rotate_live_twitch_session("sid", None)
        raise AssertionError("expected ValueError for missing session")
    except ValueError:
        pass


def test_rotate_honors_explicit_player_type(monkeypatch) -> None:
    from routers.preview import _rotate_live_twitch_session

    class FakeSession:
        platform = "Twitch"
        master_url = "https://usher.ttvnw.net/api/channel/hls/xqc.m3u8?nauth=x"
        entry_url = master_url
        allowed_hosts = {"usher.ttvnw.net"}
        twitch_player_type = "popout"

    calls: list = []
    sess = FakeSession()
    monkeypatch.setattr("routers.preview.get_session", lambda sid: sess)

    def fake_probe(login, player_types=None, skip_cache=False):
        calls.append(tuple(player_types or ()))
        return {
            "url": f"https://usher.ttvnw.net/api/channel/hls/{login}.m3u8?pt=a",
            "headers": {},
            "player_type": player_types[0],
            "ad_free": False,
        }

    monkeypatch.setattr("services.live_capture.probe_twitch_live_master", fake_probe)
    out = _rotate_live_twitch_session("sid", "autoplay")
    assert out["player_type"] == "autoplay"
    assert out["ad_free"] is False  # dirty masters still rotate — strip fallback
    assert sess.twitch_player_type == "autoplay"
    assert calls == [("autoplay", "embed", "popout")]


def test_youtube_live_info_returns_video_id(monkeypatch) -> None:
    """youtube_live_info must surface the real videoId it already extracts
    from the /live redirect page — the archive watchdog stores it as
    video_id so rows link to the actual video (mocked fetch: no network)."""
    from services.live_capture import youtube_live_info

    class FakeResp:
        content = b'"videoId": "AbCdEfGhIjK"' + b' "videoId":"ZZZZZZZZZZZ"'

    monkeypatch.setattr("services.live_capture.requests.get",
                        lambda *a, **k: FakeResp())
    monkeypatch.setattr(
        "services.youtube_innertube.innertube_extract_info",
        lambda url, timeout=None: {
            "title": "Test Live",
            "viewer_count": 42,
            "http_headers": {"User-Agent": "x"},
            "formats": [{"protocol": "m3u8", "height": 720,
                          "url": "https://cdn.example.com/live.m3u8"}],
        })
    out = youtube_live_info("@somechannel")
    assert out is not None
    assert out.get("videoId") == "AbCdEfGhIjK"
    assert out["url"] == "https://cdn.example.com/live.m3u8"
    assert out["platform"] == "YouTube"


if __name__ == "__main__":
    test_progress_hook_shape()
    test_live_router_imports()
    print("live_capture self-check OK")
