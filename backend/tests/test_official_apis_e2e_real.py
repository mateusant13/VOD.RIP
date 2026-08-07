"""E2E (real network, isolated DB) — official-API hybrid (issue #4).

Credential probe (READ-ONLY on the real %APPDATA%):
- Twitch: bridge-cookies/twitch.txt auth-token line, else the
  twitch_helix_token field of the real settings.json.

When a real token exists the matching real API is exercised. On this
machine neither exists, so the suite proves:
  * the full wiring with a mock token (settings -> helix service -> ingest
    -> archive DB) against a stubbed HTTP layer, and
  * the real public Twitch GQL path (the no-token route) against the live
    API with the archive DB isolated (VODRIP_ARCHIVE_DB -> scratch).

Run from backend/: python -m pytest tests/test_official_apis_e2e_real.py -v
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest

from deps import settings_mgr
from models.schemas import AppSettings
from services import archive_db
from services import twitch_helix_service as ths
from services.archive_twitch import ingest_channel_vods


# --- real-credential probe (read-only) -------------------------------------

def _real_appdata() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "VOD.RIP"


def _real_twitch_token() -> str | None:
    txt = _real_appdata() / "bridge-cookies" / "twitch.txt"
    if txt.is_file():
        try:
            m = re.search(r"auth-token\s+(\S+)", txt.read_text(encoding="utf-8", errors="replace"))
            if m:
                return m.group(1)
        except OSError:
            pass
    settings_path = _real_appdata() / "settings.json"
    if settings_path.is_file():
        try:
            tok = json.loads(settings_path.read_text(encoding="utf-8")).get("twitch_helix_token") or ""
            if tok.strip():
                return tok.strip()
        except (OSError, ValueError):
            pass
    return None


_REAL_TWITCH_TOKEN = _real_twitch_token()


@pytest.fixture(autouse=True)
def _reset_settings(tmp_path):
    """Redirect the shared settings manager to scratch (no real %APPDATA%
    writes; mirrors test_archive_yt_captions._reset_settings)."""
    original_file = settings_mgr._settings_file
    original_dir = settings_mgr._settings_dir
    scratch_dir = tmp_path / "VOD.RIP"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    settings_mgr._settings_dir = scratch_dir
    settings_mgr._settings_file = scratch_dir / "settings.json"
    settings_mgr._settings = AppSettings()
    yield
    settings_mgr._settings_file = original_file
    settings_mgr._settings_dir = original_dir


# --- Twitch: helix-first routing -------------------------------------------

HELIX_USERS = {"data": [{"id": "54321", "login": "cellbit", "display_name": "Cellbit"}]}
HELIX_VIDEOS = {"data": [
    {"id": "e2e-helix-1", "user_id": "54321", "user_login": "cellbit",
     "user_name": "Cellbit", "title": "E2E Helix VOD One",
     "created_at": "2026-08-01T00:00:00Z", "url": "https://www.twitch.tv/videos/e2e-helix-1",
     "thumbnail_url": "https://cdn.example/thumb-%{width}x%{height}.jpg",
     "viewable": "public", "view_count": 10, "language": "pt", "type": "archive",
     "duration": "2h15m00s", "game_id": "509658", "game_name": "Just Chatting"},
    {"id": "e2e-helix-2", "user_id": "54321", "user_login": "cellbit",
     "user_name": "Cellbit", "title": "E2E Helix VOD Two",
     "created_at": "2026-08-02T00:00:00Z", "url": "https://www.twitch.tv/videos/e2e-helix-2",
     "thumbnail_url": "https://cdn.example/thumb2-%{width}x%{height}.jpg",
     "viewable": "public", "view_count": 3, "language": "en", "type": "archive",
     "duration": "0h30m00s", "game_id": "509658", "game_name": "Just Chatting"},
]}


def test_mock_token_full_wiring_to_archive_db(monkeypatch):
    """Mock token: settings -> helix service -> ingest -> archive DB, with a
    stubbed HTTP layer. Proves the complete official path without a key."""
    s = AppSettings()
    s.twitch_helix_token = "mock-token"
    s.twitch_helix_token_updated_at = time.time()
    settings_mgr.save(s)

    calls = []

    def _fake_helix(path, params):
        calls.append(path)
        return HELIX_USERS if path == "/users" else HELIX_VIDEOS

    monkeypatch.setattr(ths, "_helix_get", _fake_helix)

    results = ingest_channel_vods("cellbit", limit=2)
    assert calls == ["/users", "/videos"], "helix must serve the listing"
    assert len(results) == 2
    assert results[0]["video_id"] == "e2e-helix-1"
    assert results[1]["video_id"] == "e2e-helix-2"
    assert archive_db.video_channel("twitch", "e2e-helix-1") == "cellbit"
    assert archive_db.video_channel("twitch", "e2e-helix-2") == "cellbit"
    row = archive_db.list_videos("twitch", "cellbit")
    by_id = {r["video_id"]: r for r in row}
    assert by_id["e2e-helix-1"]["duration_sec"] == 2 * 3600 + 15 * 60
    assert by_id["e2e-helix-1"]["title"] == "E2E Helix VOD One"
    assert by_id["e2e-helix-2"]["duration_sec"] == 30 * 60


def test_mock_token_helix_failure_falls_back_real_gql():
    """Invalid token -> helix raises -> the REAL public GQL path must still
    produce rows (silent fallback end to end, live network)."""
    s = AppSettings()
    s.twitch_helix_token = "definitely-invalid-token"
    s.twitch_helix_token_updated_at = time.time()
    settings_mgr.save(s)

    results = []
    for channel in ("gaules", "loud", "cellbit", "xqc", "shroud"):
        results = ingest_channel_vods(channel, limit=1)
        if results:
            break
    assert results, "GQL fallback produced no rows for any probe channel"
    row = results[0]
    assert row["video_id"]
    assert archive_db.video_channel("twitch", row["video_id"]) == row["channel"]


def test_real_gql_no_token_path():
    """No token -> real public Twitch GQL listing -> archive DB (live)."""
    assert settings_mgr.get().twitch_helix_token == "", "no-token path requires empty settings"
    results = []
    for channel in ("gaules", "loud", "cellbit", "xqc", "shroud"):
        results = ingest_channel_vods(channel, limit=1)
        if results:
            break
    assert results, "live GQL returned no VODs for any probe channel"
    row = results[0]
    assert row["video_id"]
    assert row["title"]
    assert archive_db.video_channel("twitch", row["video_id"]) == row["channel"]


@pytest.mark.skipif(
    _REAL_TWITCH_TOKEN is None,
    reason="no real Twitch token on this machine (bridge-cookies/twitch.txt + "
    "settings.json empty) — mock-token e2e above covers the helix path",
)
def test_real_token_helix_ingest(monkeypatch):
    """Real token (from the cookie bridge): live Helix listing -> archive DB.
    A counting spy around _helix_get proves the official API actually ran
    (a bad token would silently fall back to GQL and zero the counter)."""
    s = AppSettings()
    s.twitch_helix_token = _REAL_TWITCH_TOKEN
    s.twitch_helix_token_updated_at = time.time()
    settings_mgr.save(s)

    real_get = ths._helix_get
    count = {"n": 0}

    def _spy(path, params):
        count["n"] += 1
        return real_get(path, params)

    monkeypatch.setattr(ths, "_helix_get", _spy)
    results = ingest_channel_vods("gaules", limit=2)
    assert results, "real-helix ingest produced no rows"
    assert count["n"] >= 2, "helix was not actually used (fell back to GQL?)"
    assert archive_db.video_channel("twitch", results[0]["video_id"]) is not None
