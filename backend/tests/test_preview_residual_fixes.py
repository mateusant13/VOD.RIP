from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def test_m3u8_map_is_preserved_for_fmp4_segments():
    from services import ytdlp_hls

    class Response:
        text = "\n".join(
            [
                "#EXTM3U",
                '#EXT-X-MAP:URI="init.mp4"',
                "#EXTINF:4,",
                "chunk-0.m4s",
            ]
        )

        def raise_for_status(self):
            return None

    with patch.object(ytdlp_hls.requests, "get", return_value=Response()):
        segments, info = ytdlp_hls._parse_m3u8(
            "https://cdn.example.test/path/media.m3u8", {}, 720
        )

    assert info["init_url"] == "https://cdn.example.test/path/init.mp4"
    assert segments == [
        {"duration": 4.0, "url": "https://cdn.example.test/path/chunk-0.m4s"}
    ]


def test_twitch_refresh_updates_urls_and_invalidates_rewrites():
    from services.preview import hls
    from services.preview.session import PreviewSession

    session = PreviewSession(
        session_id="a" * 16,
        vod_url="https://www.twitch.tv/videos/123",
        master_url="https://old.example/master.m3u8",
        entry_url="https://old.example/old.m3u8",
        platform="Twitch",
        allowed_hosts={"old.example"},
    )
    hls._playlist_cache(session)["stale"] = (b"old", 0.0)
    fresh = "https://new.example/720.m3u8"
    with patch(
        "services.twitch_gql_service.get_vod_playback_sync",
        return_value=(
            "https://new.example/master.m3u8",
            {"Authorization": "fresh"},
            [{"height": 720, "url": fresh}],
        ),
    ):
        assert hls._twitch_refresh_and_remap(
            session, "https://old.example/old.m3u8"
        ) == fresh

    assert session.master_url == "https://new.example/master.m3u8"
    assert session.entry_url == fresh
    assert session.http_headers == {"Authorization": "fresh"}
    assert session.allowed_hosts == {"new.example"}
    assert session.rewritten_playlists == {}


def test_preview_session_registry_restores_and_resolves(tmp_path, monkeypatch):
    from services.preview import session as preview_session

    monkeypatch.setattr(preview_session, "preview_root", lambda: Path(tmp_path))
    manager = preview_session.PreviewManager()
    restored_id = "b" * 16
    original = preview_session.PreviewSession(
        session_id=restored_id,
        vod_url="https://www.twitch.tv/videos/456",
        master_url="https://old.example/master.m3u8",
        entry_url="https://old.example/720.m3u8",
        platform="Twitch",
        cache_dir=Path(tmp_path) / restored_id,
        kind="hls",
        crop_start=3.0,
        crop_end=9.0,
        prefer_height=720,
    )
    with manager._lock:
        manager._sessions[restored_id] = original
    manager._persist_sessions()

    recovered = preview_session.PreviewManager()
    with patch.object(
        preview_session,
        "resolve_stream_info",
        return_value=(
            "https://new.example/master.m3u8",
            {"Referer": "https://twitch.tv/"},
            "Twitch",
            [{"height": 720, "url": "https://new.example/720.m3u8"}],
            "hls",
            None,
        ),
    ):
        session = recovered.get_session(restored_id)

    assert session is not None
    assert session.needs_recovery is False
    assert session.entry_url == "https://new.example/720.m3u8"
    assert session.crop_start == 3.0 and session.crop_end == 9.0


assert True, "preview residual regression checks loaded"
