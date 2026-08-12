"""Kick resilience: retry/backoff + Cloudflare gate + download retry.

All mocked — curl_cffi.requests.get is patched per test and the archive
downloader is patched in _ingest_one tests. Real Kick/Cloudflare behavior
is NOT verified here (no live network); these tests pin the retry/cooldown/
requeue logic against fabricated responses.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from services import archive_db, kick_gate
from services.kick_models import KickVideo


class _Resp:
    """curl_cffi response stand-in: status_code + json + raise_for_status."""

    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _kick_gate_reset():
    """Fresh gate state per test — never leak a freeze across tests."""
    kick_gate.clear_kick_gate()
    yield
    kick_gate.clear_kick_gate()


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """Fresh archive DB per test; module connection rebound to tmp path."""
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(tmp_path / "archive.db"))
    archive_db._conn = None
    archive_db._schema_ready = False


# --- _get_json: retry/backoff --------------------------------------------

def test_get_json_429_retries_then_succeeds(monkeypatch):
    from services import kick_api_service as k

    calls = {"n": 0}

    def fake_get(url, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return _Resp(429)
        return _Resp(200, {"ok": True})

    monkeypatch.setattr("time.sleep", lambda _s: None)
    monkeypatch.setattr("curl_cffi.requests.get", fake_get)
    body = k._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips")
    assert body == {"ok": True}
    assert calls["n"] == 3, "429 must be retried, not failed"
    assert not kick_gate.kick_gate_active(), "a recovered request resets the gate streak"


def test_get_json_transport_timeout_retries_then_succeeds(monkeypatch):
    from services import kick_api_service as k

    calls = {"n": 0}

    def fake_get(url, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("curl error: Operation timed out")
        return _Resp(200, {"ok": True})

    monkeypatch.setattr("time.sleep", lambda _s: None)
    monkeypatch.setattr("curl_cffi.requests.get", fake_get)
    body = k._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips")
    assert body == {"ok": True}
    assert calls["n"] == 3, "transport timeouts are transient and must be retried"


def test_get_json_5xx_backoff_then_terminal(monkeypatch):
    from services import kick_api_service as k

    calls = {"n": 0}

    def fake_get(url, **kwargs):
        calls["n"] += 1
        return _Resp(500)

    monkeypatch.setattr("time.sleep", lambda _s: None)
    monkeypatch.setattr("curl_cffi.requests.get", fake_get)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        k._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips")
    assert calls["n"] == k._BACKOFF_MAX_ATTEMPTS == 8, (
        "5xx must exhaust the backoff loop before raising terminal"
    )
    assert not kick_gate.kick_gate_active(), "5xx is server-side, not a Cloudflare gate signal"


def test_get_json_404_still_terminal_value_error(monkeypatch):
    from services import kick_api_service as k

    monkeypatch.setattr("curl_cffi.requests.get", lambda *a, **k: _Resp(404))
    with pytest.raises(ValueError, match="not found"):
        k._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips")


# --- _get_json: 403 → Cloudflare gate ------------------------------------

def test_get_json_403_classifies_then_freezes(monkeypatch):
    from services import kick_api_service as k

    calls = {"n": 0}

    def fake_get(url, **kwargs):
        calls["n"] += 1
        return _Resp(403)

    monkeypatch.setattr(kick_gate, "_SHORT_COOLDOWN_SEC", 0.0)  # next 403 arrives immediately
    monkeypatch.setattr("curl_cffi.requests.get", fake_get)
    for _ in range(kick_gate._GATE_TRIP_COUNT):
        with pytest.raises(k.KickGateError, match="blocked"):
            k._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips")
    assert kick_gate.kick_gate_active(), "N consecutive 403s must arm the long freeze"
    # Frozen → fail fast with a clear error and NO network call.
    with pytest.raises(k.KickGateError, match="frozen"):
        k._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips")
    assert calls["n"] == kick_gate._GATE_TRIP_COUNT, "frozen requests must not hit the network"


def test_get_json_frozen_fails_fast_no_network(monkeypatch):
    from services import kick_api_service as k

    calls = {"n": 0}

    def fake_get(url, **kwargs):
        calls["n"] += 1
        return _Resp(200, {"ok": True})

    monkeypatch.setattr("curl_cffi.requests.get", fake_get)
    kick_gate._until = time.monotonic() + 3600.0  # freeze for an hour
    with pytest.raises(k.KickGateError, match="frozen"):
        k._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips")
    assert calls["n"] == 0, "a frozen gate must never touch the network"


def test_gate_freeze_expiry(monkeypatch):
    from services import kick_api_service as k

    calls = {"n": 0}

    def fake_get(url, **kwargs):
        calls["n"] += 1
        if calls["n"] <= kick_gate._GATE_TRIP_COUNT:
            return _Resp(403)
        return _Resp(200, {"ok": True})

    monkeypatch.setattr(kick_gate, "_SHORT_COOLDOWN_SEC", 0.0)
    monkeypatch.setattr("curl_cffi.requests.get", fake_get)
    for _ in range(kick_gate._GATE_TRIP_COUNT):
        with pytest.raises(k.KickGateError):
            k._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips")
    assert kick_gate.kick_gate_active()
    kick_gate._until = time.monotonic() - 1.0  # freeze expired
    assert not kick_gate.kick_gate_active()
    body = k._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips")
    assert body == {"ok": True}, "requests resume once the freeze lifts"
    assert calls["n"] == kick_gate._GATE_TRIP_COUNT + 1


def test_get_json_429_exhausted_counts_toward_gate(monkeypatch):
    from services import kick_api_service as k

    calls = {"n": 0}

    def fake_get(url, **kwargs):
        calls["n"] += 1
        return _Resp(429)

    monkeypatch.setattr("time.sleep", lambda _s: None)
    monkeypatch.setattr("curl_cffi.requests.get", fake_get)
    with pytest.raises(k.KickRateLimitError, match="429"):
        k._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips")
    assert calls["n"] == k._BACKOFF_MAX_ATTEMPTS == 8
    assert kick_gate.kick_gate_active(), "an exhausted 429 run is a gate event (short cooldown)"


# --- classifier -----------------------------------------------------------

def test_transient_classifier():
    transient = [
        "KickGateError: Kick requests frozen for 900s (Cloudflare/rate-limit cooldown)",
        "KickGateError: Kick request blocked (Cloudflare/403): /api/v2/channels/x",
        "KickRateLimitError: Kick rate-limited (429) after 8 attempts: /api/v1/video/x",
        "HTTP 429 Too Many Requests",
        "RuntimeError: curl error: Operation timed out",
        "ConnectionError: connection reset by peer",
        "timed out after 300s (cancelled)",
    ]
    for text in transient:
        assert kick_gate.classify_transient_kick_error(text), text
    terminal = [
        "RuntimeError: Kick API returned no HLS source for this VOD",
        "simulated failure",
        "HTTP 500 Internal Server Error",
        "ffmpeg exited with code 1",
        "RuntimeError: Unexpected Kick videos API response",
    ]
    for text in terminal:
        assert not kick_gate.classify_transient_kick_error(text), text


# --- archive_kick: download retry / requeue -------------------------------

def _kick_video(video_id: str = "vid-r", title: str = "Retry Me") -> KickVideo:
    return KickVideo(id=video_id, title=title, created_at="2026-08-01T10:00:00Z", duration=60.0)


def test_download_retries_once_then_succeeds(scratch_db, tmp_path, monkeypatch):
    """Transient failure on attempt 1 → one retry after the pause → success."""
    from services import archive_kick

    archive_dir = tmp_path / "vods"
    archive_dir.mkdir()
    calls = {"n": 0}

    def _flaky_download(url, out_path, budget_sec, quality):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": False, "error": "timed out after 300s (cancelled)"}
        Path(out_path).write_bytes(b"hello-kick")
        return {"ok": True}

    monkeypatch.setattr(archive_kick, "_download_with_budget", _flaky_download)
    monkeypatch.setattr(archive_kick, "_RETRY_DELAY_SEC", 0.0)

    r = archive_kick._ingest_one(_kick_video(), "ch", download=True,
                                 max_download_sec=30.0, quality="720",
                                 archive_dir=str(archive_dir))
    assert calls["n"] == 2, "transient failure must trigger exactly one retry"
    assert r["action"] == "downloaded" and r["status"] == "ready"
    assert archive_db.latest_job("kick", "vid-r")["status"] == "done"


def test_download_transient_twice_requeues_not_fails(scratch_db, tmp_path, monkeypatch):
    """Transient on both attempts → requeued (job 'queued', video stays 'known')."""
    from services import archive_kick

    archive_dir = tmp_path / "vods"
    archive_dir.mkdir()
    calls = {"n": 0}

    def _always_transient(url, out_path, budget_sec, quality):
        calls["n"] += 1
        return {"ok": False, "error": "KickGateError: Kick requests frozen for 900s (Cloudflare/rate-limit cooldown)"}

    monkeypatch.setattr(archive_kick, "_download_with_budget", _always_transient)
    monkeypatch.setattr(archive_kick, "_RETRY_DELAY_SEC", 0.0)

    r = archive_kick._ingest_one(_kick_video(), "ch", download=True,
                                 max_download_sec=30.0, quality="720",
                                 archive_dir=str(archive_dir))
    assert calls["n"] == 2, "retry happened once; transient is not terminal"
    assert r["action"] == "requeued" and r["status"] == "known"
    job = archive_db.latest_job("kick", "vid-r")
    assert job["status"] == "queued" and "requeued" in job["error"]
    row = archive_db.query("SELECT status FROM videos WHERE video_id='vid-r'")[0]
    assert row["status"] == "known", "transient must never mark the video 'failed'"


def test_download_gated_skips_retry_and_requeues(scratch_db, tmp_path, monkeypatch):
    """While the gate is frozen, a download fails fast — no pointless retry."""
    from services import archive_kick

    archive_dir = tmp_path / "vods"
    archive_dir.mkdir()
    calls = {"n": 0}

    def _gated(url, out_path, budget_sec, quality):
        calls["n"] += 1
        return {"ok": False, "error": "KickGateError: Kick requests frozen for 3600s (Cloudflare/rate-limit cooldown)"}

    monkeypatch.setattr(archive_kick, "_download_with_budget", _gated)
    kick_gate._until = time.monotonic() + 3600.0

    r = archive_kick._ingest_one(_kick_video(), "ch", download=True,
                                 max_download_sec=30.0, quality="720",
                                 archive_dir=str(archive_dir))
    assert calls["n"] == 1, "frozen gate → no retry, requeue immediately"
    assert r["action"] == "requeued" and r["status"] == "known"
    assert archive_db.latest_job("kick", "vid-r")["status"] == "queued"


def test_download_terminal_error_fails(scratch_db, tmp_path, monkeypatch):
    """Terminal errors (no HLS source, ffmpeg) keep the 'failed' contract."""
    from services import archive_kick

    archive_dir = tmp_path / "vods"
    archive_dir.mkdir()
    calls = {"n": 0}

    def _terminal(url, out_path, budget_sec, quality):
        calls["n"] += 1
        return {"ok": False, "error": "RuntimeError: Kick API returned no HLS source for this VOD"}

    monkeypatch.setattr(archive_kick, "_download_with_budget", _terminal)
    monkeypatch.setattr(archive_kick, "_RETRY_DELAY_SEC", 0.0)

    r = archive_kick._ingest_one(_kick_video(), "ch", download=True,
                                 max_download_sec=30.0, quality="720",
                                 archive_dir=str(archive_dir))
    assert calls["n"] == 1, "no retry on a terminal error"
    assert r["action"] == "failed" and r["status"] == "failed"
    assert archive_db.latest_job("kick", "vid-r")["status"] == "failed"
