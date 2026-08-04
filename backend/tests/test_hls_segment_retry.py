"""Per-segment HLS retry: transient failures retried, user-cancel not.

Deterministic: patches requests.get, no network.
"""

import threading

import pytest

from services import ytdlp_hls
from services.ytdlp_ffmpeg import CancelledError, PausedError


class _FakeResp:
    def __init__(self, status=200, body=b"x" * 2048):
        self._status = status
        self._body = body
        self.closed = False

    @property
    def status_code(self):
        return self._status

    def raise_for_status(self):
        if self._status >= 400:
            raise ytdlp_hls.requests.HTTPError(f"HTTP {self._status}")

    def iter_content(self, chunk_size):
        yield self._body

    def close(self):
        self.closed = True


def _patch_requests(monkeypatch, outcomes):
    """outcomes: list of _FakeResp | Exception; last entry repeats."""
    calls = {"n": 0}

    def get(url, **kwargs):
        i = min(calls["n"], len(outcomes) - 1)
        calls["n"] += 1
        out = outcomes[i]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(ytdlp_hls.requests, "get", get)
    return calls


def _run_segment(tmp_path, monkeypatch, outcomes):
    calls = _patch_requests(monkeypatch, outcomes)
    path = ytdlp_hls._download_one_segment(
        7, {"url": "https://cdn.invalid/seg7.ts"}, {},
        str(tmp_path), None, None,
    )
    return calls, path


def test_segment_retries_transient_500_then_succeeds(tmp_path, monkeypatch):
    calls, path = _run_segment(
        tmp_path, monkeypatch,
        [_FakeResp(status=500), _FakeResp(status=200)],
    )
    assert calls["n"] == 2
    assert path.endswith("00007.ts")
    assert open(path, "rb").read() == b"x" * 2048


def test_segment_gives_up_after_max_attempts(tmp_path, monkeypatch):
    calls = _patch_requests(monkeypatch, [_FakeResp(status=503)])
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        ytdlp_hls._download_one_segment(
            0, {"url": "https://cdn.invalid/s.ts"}, {}, str(tmp_path), None, None,
        )
    assert calls["n"] == ytdlp_hls._SEGMENT_RETRIES


def test_cancel_is_not_retried(tmp_path, monkeypatch):
    calls = _patch_requests(monkeypatch, [CancelledError("stop")])
    with pytest.raises(CancelledError):
        ytdlp_hls._download_one_segment(
            0, {"url": "https://cdn.invalid/s.ts"}, {}, str(tmp_path), None, None,
        )
    assert calls["n"] == 1


def test_pause_is_not_retried(tmp_path, monkeypatch):
    calls = _patch_requests(monkeypatch, [PausedError("pause")])
    with pytest.raises(PausedError):
        ytdlp_hls._download_one_segment(
            0, {"url": "https://cdn.invalid/s.ts"}, {}, str(tmp_path), None, None,
        )
    assert calls["n"] == 1
