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
from services.preview_service import _manager, PreviewSession, get_session, create_session, proxy_master


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
