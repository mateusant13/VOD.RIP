"""Self-check for the upstream stall watchdog in services.preview.hls.

Run from the backend directory with:
    python -m pytest tests/test_upstream_stall.py -q -p no:cacheprovider

Network is stubbed with fake streaming responses — the wall-clock deadline,
the blocked-read watchdog, and the requests fallback are exercised
deterministically (no live upstream).
"""
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import services.preview.hls as hls_mod
from services.preview import session as session_mod

# Public IP literal: _validate_proxy_url() resolves the host and must see a
# public address — getaddrinfo returns literals directly, so this is
# deterministic and works offline.
PLAYLIST_URL = "http://93.184.216.34/media.m3u8"
SEGMENT_URL = "http://93.184.216.34/seg-1.ts"
BODY = (
    b"#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
    b"#EXTINF:6.000,\nhttp://93.184.216.34/seg-1.ts\n"
)


class _FakeResp:
    """Minimal stand-in for a curl_cffi/requests streaming response."""

    def __init__(self, chunks, stall: bool = False):
        self.headers = {"Content-Type": "application/vnd.apple.mpegurl"}
        self.status_code = 200
        self._chunks = list(chunks)
        self._stall = stall
        self._unblock = threading.Event()
        self.closed = False
        self.iter_calls = 0

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=None):
        self.iter_calls += 1
        if self._stall:
            # Simulate a 0 B/s upstream: block until close() unblocks us,
            # the way libcurl's low-speed abort would.
            self._unblock.wait(30)
            return
        for chunk in self._chunks:
            yield chunk

    def close(self):
        self.closed = True
        self._unblock.set()


def _mk_session(sid: str, **over) -> session_mod.PreviewSession:
    kwargs = dict(
        session_id=sid,
        vod_url=PLAYLIST_URL,
        master_url=PLAYLIST_URL,
        entry_url=PLAYLIST_URL,
        platform="Twitch",
        http_headers={},
        allowed_hosts={"93.184.216.34"},
        cache_dir=Path("/tmp/preview-test-" + sid),
        kind="hls",
        crop_start=0.0,
        crop_end=3600.0,
    )
    kwargs.update(over)
    return session_mod.PreviewSession(**kwargs)


def _force_requests_fallback(monkeypatch) -> None:
    # Make `from curl_cffi import requests` raise ImportError so
    # _http_get_bytes takes the `import requests` fallback path. The None
    # sys.modules halt only triggers once the parent attribute is removed
    # (importlib skips submodule loading when the parent already has it).
    import curl_cffi

    monkeypatch.setitem(sys.modules, "curl_cffi.requests", None)
    monkeypatch.delattr(curl_cffi, "requests", raising=False)


def _wait_closed(resp: _FakeResp, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not resp.closed and time.monotonic() < deadline:
        time.sleep(0.01)


def test_stalled_read_aborts_at_budget(monkeypatch) -> None:
    """A 0 B/s stalled playlist fetch raises RuntimeError at the budget."""
    monkeypatch.setattr(hls_mod, "_UPSTREAM_PLAYLIST_DEADLINE_SEC", 1.0)
    resp = _FakeResp([], stall=True)
    captured: dict = {}

    def fake_get(url, **kw):
        captured.update(kw)
        return resp

    with patch("curl_cffi.requests.get", fake_get):
        start = time.monotonic()
        with pytest.raises(RuntimeError, match="stalled/timed out"):
            hls_mod._http_get_bytes(_mk_session("s1"), PLAYLIST_URL)
        elapsed = time.monotonic() - start

    assert elapsed < 5.0, f"stall aborted too slowly: {elapsed:.1f}s"
    assert captured["timeout"] == (
        session_mod._UPSTREAM_CONNECT_TIMEOUT_SEC,
        hls_mod._UPSTREAM_READ_TIMEOUT_SEC,
    ), "per-read timeout must be the module constant (was hardcoded 90/60)"
    _wait_closed(resp)
    assert resp.closed, "aborted response must be closed by the cleanup thread"


def test_stalled_segment_uses_segment_budget(monkeypatch) -> None:
    """Segments (proxy_segment path) abort at the 15s-class budget, not the playlist one."""
    monkeypatch.setattr(hls_mod, "_UPSTREAM_SEGMENT_DEADLINE_SEC", 0.5)
    monkeypatch.setattr(hls_mod, "_UPSTREAM_PLAYLIST_DEADLINE_SEC", 30.0)
    resp = _FakeResp([], stall=True)

    def fake_get(url, **kw):
        return resp

    with patch("curl_cffi.requests.get", fake_get):
        start = time.monotonic()
        with pytest.raises(RuntimeError, match="stalled/timed out"):
            hls_mod._http_get_bytes(_mk_session("s2"), SEGMENT_URL)
        elapsed = time.monotonic() - start

    assert elapsed < 5.0, f"segment stall must use the segment budget: {elapsed:.1f}s"


def test_fast_response_still_succeeds(monkeypatch) -> None:
    """A healthy fast response is untouched by the deadline machinery."""
    monkeypatch.setattr(hls_mod, "_UPSTREAM_PLAYLIST_DEADLINE_SEC", 1.0)
    resp = _FakeResp([BODY, b"#EXTINF:6.000,\nhttp://93.184.216.34/seg-2.ts\n"])

    def fake_get(url, **kw):
        return resp

    with patch("curl_cffi.requests.get", fake_get):
        start = time.monotonic()
        data, ctype, headers, status = hls_mod._http_get_bytes(
            _mk_session("s3"), PLAYLIST_URL
        )
        elapsed = time.monotonic() - start

    assert elapsed < 1.0
    assert data == BODY + b"#EXTINF:6.000,\nhttp://93.184.216.34/seg-2.ts\n"
    assert status == 200
    assert ctype == "application/vnd.apple.mpegurl"
    assert headers["Content-Length"] == str(len(data))
    assert resp.closed, "normal drain must close the response"


def test_requests_fallback_stalled_aborts_at_budget(monkeypatch) -> None:
    """The requests fallback path aborts stalled reads identically."""
    monkeypatch.setattr(hls_mod, "_UPSTREAM_PLAYLIST_DEADLINE_SEC", 1.0)
    _force_requests_fallback(monkeypatch)
    resp = _FakeResp([], stall=True)
    captured: dict = {}

    def fake_get(url, **kw):
        captured.update(kw)
        return resp

    with patch("requests.get", fake_get):
        start = time.monotonic()
        with pytest.raises(RuntimeError, match="stalled/timed out"):
            hls_mod._http_get_bytes(_mk_session("s4"), PLAYLIST_URL)
        elapsed = time.monotonic() - start

    assert elapsed < 5.0, f"fallback stall aborted too slowly: {elapsed:.1f}s"
    assert captured["timeout"] == (
        session_mod._UPSTREAM_CONNECT_TIMEOUT_SEC,
        hls_mod._UPSTREAM_READ_TIMEOUT_SEC,
    )
    _wait_closed(resp)
    assert resp.closed


def test_requests_fallback_fast_succeeds(monkeypatch) -> None:
    """The requests fallback path returns healthy bodies unchanged."""
    monkeypatch.setattr(hls_mod, "_UPSTREAM_PLAYLIST_DEADLINE_SEC", 1.0)
    _force_requests_fallback(monkeypatch)
    resp = _FakeResp([BODY])

    def fake_get(url, **kw):
        return resp

    with patch("requests.get", fake_get):
        data, _ctype, _headers, status = hls_mod._http_get_bytes(
            _mk_session("s5"), PLAYLIST_URL
        )

    assert data == BODY
    assert status == 200
    assert resp.closed
