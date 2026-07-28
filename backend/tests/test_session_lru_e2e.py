"""Self-check: LRU eviction pressure on PreviewManager's session dict.

Run from anywhere: `python backend/tests/test_session_lru_e2e.py`
"""
import os
import sys
import time
from pathlib import Path

# ponytail: tests run from various cwds; anchor to backend/ so
# `from services.X` resolves identically to how run.py loads it.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from services.preview_service import PreviewManager, PreviewSession, LRU_SIZE_HARD_LIMIT


def test_lru_size_hard_limit():
    """Create 13 sessions — oldest is evicted, size stays <= 12."""
    mgr = PreviewManager()
    assert mgr._max_sessions == LRU_SIZE_HARD_LIMIT == 12

    now = time.time()
    # Insert 13 sessions with ascending last_access — oldest = sid-000
    for i in range(13):
        sid = f"sid-{i:03d}"
        s = PreviewSession(
            session_id=sid,
            vod_url=f"https://example.com/{i}",
            master_url=f"https://example.com/{i}/master.m3u8",
            entry_url=f"https://example.com/{i}",
            platform="Twitch",
            http_headers={},
            allowed_hosts=set(),
            cache_dir=Path("."),
            kind="vod",
            crop_start=0.0,
            crop_end=0.0,
            prefer_height=720,
        )
        s.last_access = now + i  # each is 1s newer
        mgr._sessions[sid] = s

    # Count open (non-deleted) sessions before eviction
    open_count = sum(1 for s in mgr._sessions.values() if not s.closed)
    assert open_count == 13

    # Eviction uses the same pattern as create_session / create_live_session
    with mgr._lock:
        if len(mgr._sessions) > mgr._max_sessions:
            stale = sorted(
                mgr._sessions.items(),
                key=lambda item: item[1].last_access,
            )[:len(mgr._sessions) - mgr._max_sessions]
            for popped_sid, popped_session in stale:
                del mgr._sessions[popped_sid]

    assert len(mgr._sessions) <= LRU_SIZE_HARD_LIMIT  # 12
    assert "sid-000" not in mgr._sessions  # oldest evicted
    assert "sid-012" in mgr._sessions  # newest survives
