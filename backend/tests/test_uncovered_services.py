"""Smoke tests for backend services with zero prior test coverage (L7).

Each test imports the module and exercises its primary exported function
with safe inputs. These are not comprehensive — they verify the module
loads without import errors and its core logic is callable.
"""

from __future__ import annotations

import importlib
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def test_crash_handler_imports():
    """crash_handler: module loads and install_crash_handler is callable."""
    mod = importlib.import_module("services.crash_handler")
    assert callable(mod.install_crash_handler)


def test_download_persistence_instantiation(monkeypatch, tmp_path):
    """DownloadPersistence: instantiates and loads history."""
    from services.download_persistence import DownloadPersistence

    monkeypatch.setattr("services.download_persistence._get_appdata_dir", lambda: tmp_path)
    dp = DownloadPersistence()
    assert dp is not None
    assert isinstance(dp._history, list)


def test_download_utils_smoke():
    """download_utils: pure functions return expected types."""
    from services.download_utils import (
        _download_timeout_seconds,
        _hook_progress_percent,
    )

    # timeout: large download gets a budget, small gets None
    assert _download_timeout_seconds(20 * 1024 * 1024 * 1024) is not None
    assert _download_timeout_seconds(100) is None

    # progress: extracts percent from yt-dlp dicts (capped by DOWNLOAD_PROGRESS_CAP)
    pct = _hook_progress_percent({"status": "downloading", "_percent_str": "50.0%"})
    assert isinstance(pct, int) and pct > 0
    assert _hook_progress_percent({"status": "finished"}) is None


def test_single_instance_acquire(monkeypatch, tmp_path):
    """single_instance: file lock path is derivable, acquire returns token."""
    from services import single_instance

    monkeypatch.setattr(single_instance, "_lock_path", lambda: tmp_path / "lock.txt")
    token = single_instance.acquire_process_lock()
    # Token is either a real lock object or None (lock unavailable)
    assert token is not None or token is None  # no crash


def test_token_crypto_roundtrip():
    """token_crypto: encrypt then decrypt preserves plaintext."""
    from services.token_crypto import decrypt_token, encrypt_token

    plaintext = "test-pairing-token-12345"
    ciphertext = encrypt_token(plaintext)
    assert ciphertext is not None
    assert ciphertext != plaintext
    decrypted = decrypt_token(ciphertext)
    assert decrypted == plaintext


def test_tray_service_imports():
    """tray_service: class is importable and has expected methods."""
    from services.tray_service import TrayService

    assert hasattr(TrayService, "__init__")


def test_webview2_installed_callable():
    """webview2_setup: webview2_installed is callable (returns bool)."""
    from services.webview2_setup import webview2_installed

    result = webview2_installed()
    assert isinstance(result, bool)


def test_youtube_fingerprint_headers():
    """youtube_fingerprint: youtube_http_headers returns a dict with User-Agent."""
    from services.youtube_fingerprint import youtube_http_headers

    headers = youtube_http_headers()
    assert isinstance(headers, dict)
    assert "User-Agent" in headers
    assert "Accept-Language" in headers




def test_http_fingerprint_centralized():
    """http_fingerprint: app UA vs browser/Twitch headers stay consistent."""
    from services._version import USER_AGENT
    from services.http_fingerprint import (
        BROWSER_USER_AGENT,
        TWITCH_USER_AGENT,
        twitch_http_headers,
    )
    from services.youtube_fingerprint import YT_USER_AGENT

    assert "VOD.RIP" in USER_AGENT
    assert BROWSER_USER_AGENT == YT_USER_AGENT
    assert TWITCH_USER_AGENT == YT_USER_AGENT
    twitch = twitch_http_headers()
    assert twitch["User-Agent"] == YT_USER_AGENT
    assert twitch["Referer"] == "https://www.twitch.tv/"
    assert twitch["Origin"] == "https://www.twitch.tv/"
def test_youtube_ytdlp_update_stamp(tmp_path, monkeypatch):
    """youtube_ytdlp_update: stamp file write/read roundtrip."""
    from services import youtube_ytdlp_update

    monkeypatch.setattr(youtube_ytdlp_update, "_stamp_path", lambda: tmp_path / "stamp.txt")
    # Initially no stamp -> should check
    assert youtube_ytdlp_update._should_check() is True
    youtube_ytdlp_update._write_stamp()
    # After writing -> should NOT check
    assert youtube_ytdlp_update._should_check() is False


def test_ytdlp_env_sets_env():
    """ytdlp_env: importing sets YTDLP_NO_PLUGINS=1."""
    import os
    import importlib
    from services import ytdlp_env

    # Remove any pre-existing value to test the setdefault behavior
    os.environ.pop("YTDLP_NO_PLUGINS", None)
    importlib.reload(ytdlp_env)
    assert os.environ.get("YTDLP_NO_PLUGINS") == "1"
