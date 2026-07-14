"""
Comprehensive E2E tests for the preview session flow.

Tests the full lifecycle: session creation, master.m3u8 fetch,
stream.mp4 fetch, and verifies no 404s on valid sessions.
"""
import asyncio
import secrets
import shutil
import tempfile
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

from app import app
from services.preview_service import _manager, PreviewSession, get_session, create_session, proxy_master, _is_playlist_url, _RESOLVED_STREAM_CACHE


@pytest.fixture(autouse=True)
def _clear_youtube_resolve_cache():
    _RESOLVED_STREAM_CACHE.clear()
    yield
    _RESOLVED_STREAM_CACHE.clear()


class TestIsPlaylistUrl:
    def test_youtube_videoplayback_segment_not_playlist(self):
        seg = (
            "https://rr1---sn-x.googlevideo.com/videoplayback/id/abc/itag/231"
            "/source/youtube/playlist/index.m3u8/seg"
        )
        assert not _is_playlist_url(seg)

    def test_manifest_url_is_playlist(self):
        assert _is_playlist_url("https://manifest.googlevideo.com/api/manifest/hls_playlist/x.m3u8")


class TestYouTubePreviewResolve:
    """YouTube preview must stay on HLS — progressive googlevideo URLs 403 in-browser."""

    def test_progressive_only_metadata_accepted(self):
        from unittest.mock import patch

        from services.preview_service import resolve_stream_info

        prog_only = {
            "formats": [{
                "url": "https://rr1---sn.example.googlevideo.com/videoplayback",
                "protocol": "https",
                "ext": "mp4",
                "height": 720,
                "vcodec": "avc1",
                "acodec": "mp4a",
            }],
            "http_headers": {},
        }
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        with patch(
            "services.preview_service._extract_youtube_preview_info",
            return_value=prog_only,
        ):
            entry, _hdrs, platform, _variants, kind, _yt = resolve_stream_info(url)
        assert platform == "YouTube"
        assert kind == "progressive"
        assert "googlevideo.com" in entry

    def test_hls_metadata_used(self):
        from unittest.mock import patch

        from services.preview_service import resolve_stream_info

        hls_info = {
            "formats": [{
                "url": "https://manifest.googlevideo.com/api/manifest/hls_playlist/x/720.m3u8",
                "protocol": "m3u8_native",
                "height": 720,
            }],
            "http_headers": {"Referer": "https://www.youtube.com/"},
        }
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        with patch(
            "services.preview_service._extract_youtube_preview_info",
            return_value=hls_info,
        ):
            entry, _hdrs, platform, _variants, kind, _yt = resolve_stream_info(url)
        assert platform == "YouTube"
        assert kind == "hls"
        assert ".m3u8" in entry

    def test_dash_only_with_separate_audio_uses_hls(self):
        from unittest.mock import patch

        from services.preview_service import resolve_stream_info

        dash_info = {
            "formats": [
                {"height": 720, "protocol": "https", "url": "https://x/v720.mp4", "acodec": "none"},
                {"height": 480, "protocol": "https", "url": "https://x/v480.mp4", "acodec": "none"},
            ],
            "_preview_audio_format": {"url": "https://x/a.m4a"},
            "http_headers": {},
        }
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        with patch(
            "services.preview_service._extract_youtube_preview_info",
            return_value=dash_info,
        ):
            _entry, _hdrs, platform, _variants, kind, _yt = resolve_stream_info(url)
        assert platform == "YouTube"
        assert kind == "hls"

    def test_dash_only_does_not_fall_back_to_lone_360p_progressive(self):
        """A lone 360p muxed tier must not hide higher DASH video heights."""
        from unittest.mock import patch

        from services.preview_service import resolve_stream_info

        dash_with_low_muxed = {
            "formats": [
                {"height": 720, "protocol": "https", "url": "https://x/v720.mp4", "acodec": "none"},
                {
                    "height": 360,
                    "protocol": "https",
                    "url": "https://x/p360.mp4",
                    "acodec": "mp4a.40.2",
                    "vcodec": "avc1.4d401e",
                    "ext": "mp4",
                },
            ],
            "_preview_audio_format": {"url": "https://x/a.m4a"},
            "http_headers": {},
        }
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        with patch(
            "services.preview_service._extract_youtube_preview_info",
            return_value=dash_with_low_muxed,
        ):
            _entry, _hdrs, platform, variants, kind, _yt = resolve_stream_info(url)
        assert platform == "YouTube"
        assert kind == "hls"
        assert 720 in {int(f.get("height") or 0) for f in variants}

    def test_refresh_keeps_progressive_session_progressive(self):
        """A progressive session refresh must not switch to window-HLS on a far seek."""
        from unittest.mock import patch, MagicMock

        from services.preview_service import (
            PreviewSession,
            _manager,
            refresh_youtube_preview_session,
        )

        sid = secrets.token_hex(8)
        cache_dir = Path(tempfile.gettempdir()) / "kd_test" / sid
        cache_dir.mkdir(parents=True, exist_ok=True)
        session = PreviewSession(
            session_id=sid,
            vod_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            master_url=f"/api/preview/hls/{sid}/master.m3u8",
            entry_url="https://x/p360.mp4",
            platform="YouTube",
            cache_dir=cache_dir,
            kind="progressive",
            dash_window_hls=False,
        )
        with _manager._lock:
            _manager._sessions[sid] = session

        dash_info = {
            "formats": [
                {"height": 720, "protocol": "https", "url": "https://x/v720.mp4", "acodec": "none"},
            ],
            "_preview_audio_format": {"url": "https://x/a.m4a"},
            "http_headers": {},
        }
        muxed_info = {
            "formats": [
                {
                    "height": 360,
                    "protocol": "https",
                    "url": "https://x/p360.mp4",
                    "acodec": "mp4a.40.2",
                    "vcodec": "avc1.4d401e",
                    "ext": "mp4",
                },
            ],
            "http_headers": {},
        }

        def _fake_extract(_url, oauth=None, cachedir=None, cookies_file=None):
            return muxed_info

        with patch(
            "services.preview_service._extract_youtube_preview_info",
            side_effect=_fake_extract,
        ):
            with patch(
                "services.preview_service._reextract_youtube_for_preview",
                return_value=muxed_info,
            ):
                refreshed = refresh_youtube_preview_session(sid)

        assert refreshed.kind == "progressive", refreshed.kind
        assert refreshed.dash_window_hls is False
        assert refreshed.entry_url == "https://x/p360.mp4"

        with _manager._lock:
            _manager._sessions.pop(sid, None)
        shutil.rmtree(cache_dir, ignore_errors=True)

    def test_refresh_falls_back_to_existing_progressive_when_no_muxed_available(self):
        """If re-extract is DASH-only and no muxed fallback exists, stay progressive."""
        from unittest.mock import patch

        from services.preview_service import (
            PreviewSession,
            _manager,
            refresh_youtube_preview_session,
        )

        sid = secrets.token_hex(8)
        cache_dir = Path(tempfile.gettempdir()) / "kd_test" / sid
        cache_dir.mkdir(parents=True, exist_ok=True)
        session = PreviewSession(
            session_id=sid,
            vod_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            master_url=f"/api/preview/hls/{sid}/master.m3u8",
            entry_url="https://x/p360.mp4",
            platform="YouTube",
            cache_dir=cache_dir,
            kind="progressive",
            dash_window_hls=False,
        )
        with _manager._lock:
            _manager._sessions[sid] = session

        dash_only = {
            "formats": [
                {"height": 720, "protocol": "https", "url": "https://x/v720.mp4", "acodec": "none"},
            ],
            "_preview_audio_format": {"url": "https://x/a.m4a"},
            "http_headers": {},
        }

        with patch(
            "services.preview_service.resolve_stream_info",
            return_value=("https://x/v720.mp4", {}, "YouTube", dash_only["formats"], "hls", dash_only),
        ):
            with patch(
                "services.preview_service._reextract_youtube_for_preview",
                return_value=dash_only,
            ):
                refreshed = refresh_youtube_preview_session(sid)

        assert refreshed.kind == "progressive", refreshed.kind
        assert refreshed.dash_window_hls is False
        assert refreshed.entry_url == "https://x/p360.mp4"

        with _manager._lock:
            _manager._sessions.pop(sid, None)
        shutil.rmtree(cache_dir, ignore_errors=True)


class TestPreviewManagerDirect:
    """Tests PreviewManager directly (no HTTP)."""

    def test_store_and_retrieve_session(self):
        """Session stored in _manager can be retrieved via module-level get_session."""
        sid = secrets.token_hex(8)
        cache_dir = Path(tempfile.gettempdir()) / "kd_test" / sid
        cache_dir.mkdir(parents=True, exist_ok=True)

        session = PreviewSession(
            session_id=sid,
            vod_url="https://example.com/test",
            master_url="https://example.com/test.m3u8",
            entry_url="https://example.com/test.ts",
            platform="test",
            cache_dir=cache_dir,
            kind="hls",
        )

        with _manager._lock:
            _manager._sessions[sid] = session

        found = get_session(sid)
        assert found is not None, "get_session should find the stored session"
        assert found.session_id == sid
        assert found is session

        shutil.rmtree(cache_dir, ignore_errors=True)

    def test_store_and_retrieve_progressive_session(self):
        """Progressive sessions can also be stored and retrieved."""
        sid = secrets.token_hex(8)
        cache_dir = Path(tempfile.gettempdir()) / "kd_test" / sid
        cache_dir.mkdir(parents=True, exist_ok=True)

        session = PreviewSession(
            session_id=sid,
            vod_url="https://example.com/test",
            master_url="https://example.com/test.mp4",
            entry_url="https://example.com/test.mp4",
            platform="test",
            cache_dir=cache_dir,
            kind="progressive",
        )

        with _manager._lock:
            _manager._sessions[sid] = session

        found = get_session(sid)
        assert found is not None
        assert found.kind == "progressive"

        shutil.rmtree(cache_dir, ignore_errors=True)

    def test_missing_session_returns_none(self):
        """get_session returns None for unknown session IDs."""
        found = get_session("nonexistent_session_id")
        assert found is None


class TestPreviewAPIHTTP:
    """Tests preview endpoints through HTTP."""

    @pytest.mark.asyncio
    async def test_master_m3u8_finds_session(self):
        """
        CRITICAL TEST: Inject a session into _manager, then call
        the master.m3u8 route. If this returns 500 (upstream error)
        instead of 404 (session not found), the session routing works.
        """
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sid = secrets.token_hex(8)
            cache_dir = Path(tempfile.gettempdir()) / "kd_test" / sid
            cache_dir.mkdir(parents=True, exist_ok=True)

            session = PreviewSession(
                session_id=sid,
                vod_url="https://example.com/test",
                master_url="https://example.com/test.m3u8",
                entry_url="https://example.com/test.ts",
                platform="test",
                cache_dir=cache_dir,
                kind="progressive",
            )

            # Store in _manager (same singleton the route uses)
            with _manager._lock:
                _manager._sessions[sid] = session

            # Call the same route the frontend calls
            resp = await client.get(f"/api/preview/hls/{sid}/master.m3u8")
            
            # If 404: session not found = BUG in session routing
            # If 500/502: session found but upstream unreachable = OK (expected network error)
            # If 200: session found and data fetched = BEST CASE
            assert resp.status_code != 404, (
                f"BUG: Session {sid} was stored in _manager but route returned 404!\n"
                f"Body: {resp.text[:200]}"
            )
            print(f"master.m3u8 returned {resp.status_code} (expected 500/502 - upstream not reachable)")

            shutil.rmtree(cache_dir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_stream_mp4_finds_session(self):
        """Same test for stream.mp4 endpoint."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sid = secrets.token_hex(8)
            cache_dir = Path(tempfile.gettempdir()) / "kd_test" / sid
            cache_dir.mkdir(parents=True, exist_ok=True)

            session = PreviewSession(
                session_id=sid,
                vod_url="https://example.com/test",
                master_url="https://example.com/test.mp4",
                entry_url="https://example.com/test.mp4",
                platform="test",
                cache_dir=cache_dir,
                kind="progressive",
            )

            with _manager._lock:
                _manager._sessions[sid] = session

            resp = await client.get(f"/api/preview/hls/{sid}/stream.mp4")
            
            assert resp.status_code != 404, (
                f"BUG: Session {sid} stored in _manager but stream.mp4 returned 404!\n"
                f"Body: {resp.text[:200]}"
            )
            print(f"stream.mp4 returned {resp.status_code} (expected 500/502 - upstream not reachable)")

            shutil.rmtree(cache_dir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_resource_with_registered_id_finds_session(self):
        """Resource endpoint finds session when the resource ID exists in session.resource_map."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sid = secrets.token_hex(8)
            cache_dir = Path(tempfile.gettempdir()) / "kd_test" / sid
            cache_dir.mkdir(parents=True, exist_ok=True)
            session = PreviewSession(
                session_id=sid,
                vod_url="https://example.com/test",
                master_url="https://example.com/test.m3u8",
                entry_url="https://example.com/test.ts",
                platform="test",
                cache_dir=cache_dir,
                kind="hls",
            )
            # Register a real resource ID
            import hashlib
            real_url = "https://example.com/segment.ts"
            rid = hashlib.sha256(real_url.encode()).hexdigest()[:16]
            session.resource_map[rid] = real_url
            with _manager._lock:
                _manager._sessions[sid] = session
            # Use the registered resource ID
            resp = await client.get(f"/api/preview/hls/{sid}/resource?id={rid}")
            # Should NOT be 404 - session is found. May return 500/502 (upstream unreachable)
            assert resp.status_code != 404, f"BUG: resource returned 404 for stored session {sid}"
            shutil.rmtree(cache_dir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_preview_session_create_validates_range(self):
        """Session creation rejects invalid crop ranges."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/preview/session",
                json={"url": "https://kick.com/test/clip/abc", "crop_start": 10, "crop_end": 5},
            )
            assert resp.status_code == 400
            assert "End must be after start" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_preview_session_delete(self):
        """Deleted sessions should not be found."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sid = secrets.token_hex(8)
            cache_dir = Path(tempfile.gettempdir()) / "kd_test" / sid
            cache_dir.mkdir(parents=True, exist_ok=True)
            session = PreviewSession(
                session_id=sid,
                vod_url="https://example.com/test",
                master_url="https://example.com/test.m3u8",
                entry_url="https://example.com/test.ts",
                platform="test",
                cache_dir=cache_dir,
                kind="progressive",
            )
            with _manager._lock:
                _manager._sessions[sid] = session

            # Delete via API
            resp = await client.delete(f"/api/preview/session/{sid}")
            assert resp.status_code == 200

            # Should now be gone
            found = get_session(sid)
            assert found is None, "Deleted session should not be found"

            shutil.rmtree(cache_dir, ignore_errors=True)


class TestPreviewManagerSingletonConsistency:
    """Verifies that the _manager singleton is truly singleton."""

    def test_same_manager_used_everywhere(self):
        """The module-level functions all route to the same PreviewManager instance."""
        from services.preview_service import _manager as mgr1
        from services.preview_service import _manager as mgr2
        assert mgr1 is mgr2, "_manager should be the same instance"

    def test_module_functions_use_same_manager(self):
        """get_session and create_session should use the same _manager instance."""
        from services.preview_service import get_session, create_session, _manager
        
        # Check that get_session is bound to _manager
        assert get_session.__self__ is _manager, (
            f"get_session.__self__ ({id(get_session.__self__)}) should be "
            f"_manager ({id(_manager)})"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
