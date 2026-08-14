"""Self-check: a Twitch live master fetch that 403s (expired usher token) is
re-probed once with a fresh GQL token and the session's upstream URL swapped,
instead of failing straight into the hls.js fatal-error UI. Media-playlist
403s (per-resource issue) are NOT retried.

Run: `python backend/tests/test_twitch_master_403_retry_e2e.py`
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from pathlib import Path
import services.preview.session as _session_mod
from services.preview.session import PreviewSession, StalePreviewUrls

_MASTER = "https://usher.ttvnw.net/api/channel/hls/gaules.m3u8?token=EXPIRED"
_NEW_MASTER = "https://usher.ttvnw.net/api/channel/hls/gaules.m3u8?token=FRESH"


class _FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code
        self.headers = {}
        self.content = b""
        self.closed = False

    def close(self):
        self.closed = True

    def iter_content(self, *a, **k):
        return iter([])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _make_session(master_url=_MASTER):
    return PreviewSession(
        session_id="twitch-403-retry",
        vod_url=master_url,
        master_url=master_url,
        entry_url=master_url,
        platform="Twitch",
        http_headers={},
        allowed_hosts={"usher.ttvnw.net"},
        cache_dir=Path("."),
        kind="hls",
        crop_start=0.0,
        crop_end=0.0,
        prefer_height=720,
    )


_MISS = object()  # probe returns nothing (token refresh unavailable)


def _run(calls, probe_result=_MISS):
    """Patch the upstream GET to serve `calls` (list of status codes, one per
    call) and probe_twitch_live_master to serve `probe_result` (default: the
    fresh master; pass _MISS'ing sentinel for a probe miss). Returns
    (result_status, session, probe_hits, close_hit)."""
    import importlib
    try:
        cffi = importlib.import_module("curl_cffi.requests")
        _get_orig = cffi.get
    except ImportError:
        cffi = None
        import requests as _req
        _get_orig = _req.get

    probe_orig = None
    import services.live_capture as _lc
    probe_orig = _lc.probe_twitch_live_master

    # _validate_proxy_url does a live DNS lookup — skip it so the self-check
    # never depends on the network.
    _validate_orig = _session_mod._validate_proxy_url
    _session_mod._validate_proxy_url = lambda url: True

    def _fake_get(url, **kwargs):
        status = calls.pop(0)
        return _FakeResp(status)

    probe_hits = []
    def _fake_probe(login, player_types=None, skip_cache=False):
        probe_hits.append((login, skip_cache))
        if probe_result is _MISS:
            return {"url": _NEW_MASTER, "headers": {}, "player_type": "vaft", "ad_free": True}
        return probe_result

    cffi.get = _fake_get if cffi else None
    _lc.probe_twitch_live_master = _fake_probe
    try:
        session = _make_session()
        resp = _session_mod._open_upstream_stream(session, session.master_url)
        return resp.status_code, session, probe_hits, getattr(resp, "closed", False)
    finally:
        if cffi:
            cffi.get = _get_orig
        _lc.probe_twitch_live_master = probe_orig
        _session_mod._validate_proxy_url = _validate_orig


# -- Case 1: master 403 -> re-probe once -> fresh master fetched (200) --
status, session, hits, _ = _run([403, 200])
assert status == 200, f"Expected 200 after retry, got {status}"
assert len(hits) == 1 and hits[0] == ("gaules", True), f"Expected one skip_cache probe, got {hits}"
assert session.master_url == _NEW_MASTER, f"Session master not swapped: {session.master_url}"
print("OK master 403 -> re-probe + swap")

# -- Case 2: probe finds nothing -> original error surfaces --
try:
    _run([403], probe_result=None)
    raise AssertionError("expected StalePreviewUrls")
except StalePreviewUrls:
    pass
print("OK probe miss -> StalePreviewUrls")

print("\nAll Twitch master 403 retry checks pass")
