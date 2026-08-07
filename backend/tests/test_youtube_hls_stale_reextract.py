"""403 -> fresh-URL re-extract recovery for the YouTube window-HLS mux — no network.

Deterministic: mocks StaleGooglevideoUrl 403s, the session URL refresh, and the
mux itself. Proves:
  * a dead googlevideo URL is detected by a small probe (no deep bisect
    recursion re-fetching the same expired URL at every level);
  * the session re-extracts fresh URLs after a 403 and retries with those ONLY;
  * the same URL is never handed to the mux twice;
  * re-extract cycles are bounded (max 2);
  * partial window files are unlinked before any retry, and a genuinely dead
    URL is never fed to the ffmpeg remote fallback.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from services import ytdlp_ffmpeg, ytdlp_hls
from services.preview import session as sess_mod
from services.preview.session import PreviewSession
from services.ytdlp_ffmpeg import MIN_VALID_OUTPUT_BYTES

A = "https://cdn.invalid/entryA"
A_AUDIO = "https://cdn.invalid/audioA"
B = "https://cdn.invalid/entryB"
B_AUDIO = "https://cdn.invalid/audioB"


def _sess(cache_dir: Path) -> PreviewSession:
    s = PreviewSession(
        session_id="t",
        vod_url="https://www.youtube.com/watch?v=x",
        platform="YouTube",
        master_url="",
        entry_url=A,
        cache_dir=cache_dir,
        crop_start=0,
        crop_end=25.0,
    )
    s.dash_window_hls = True  # dynamic attr — window HLS is not a dataclass field
    s.variant_entries = [(720, A)]
    s.preview_audio_url = A_AUDIO
    return s


def _patch_session_mux(monkeypatch, mux_calls, refresh, fail_urls):
    """Install a fake mux + URL refresh; mux raises StaleGooglevideoUrl for
    every URL in *fail_urls* and writes a minimal valid window otherwise."""
    from services.ytdlp_hls import StaleGooglevideoUrl

    def fake_mux(video_url, audio_url, out_dir, **kwargs):
        mux_calls.append((video_url, audio_url))
        if video_url in fail_urls:
            raise StaleGooglevideoUrl(
                f"googlevideo URL expired HTTP 403 for {video_url[:80]}"
            )
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "seg_000.ts").write_bytes(b"x" * (MIN_VALID_OUTPUT_BYTES + 1))
        return out / "window.m3u8"

    monkeypatch.setattr(ytdlp_hls, "_mux_dash_window_to_hls", fake_mux)
    monkeypatch.setattr(sess_mod, "_refresh_youtube_window_hls_urls", refresh)
    return fake_mux


# ---------------------------------------------------------------- span fetch

class _FakeResp403:
    status_code = 403

    def raise_for_status(self):
        raise ytdlp_hls.requests.HTTPError("HTTP 403")


def _patch_googlevideo_403(monkeypatch, calls):
    import curl_cffi.requests as cffi_requests

    def fake_get(url, **kwargs):
        calls["n"] += 1
        return _FakeResp403()

    monkeypatch.setattr(cffi_requests, "get", fake_get)


def test_stale_span_detected_by_probe_no_deep_recursion(tmp_path, monkeypatch):
    """A dead URL raises after ONE probe — the bisect tree is never walked."""
    calls = {"n": 0}
    _patch_googlevideo_403(monkeypatch, calls)
    with pytest.raises(ytdlp_hls.StaleGooglevideoUrl):
        ytdlp_hls._fetch_googlevideo_span_resilient(
            "https://cdn.invalid/stale", (0, 8 * 1024 * 1024), {}, str(tmp_path / "w"), None,
        )
    # initial 8MB fetch + one 16KB probe; no 5+-deep recursion on the same URL
    assert calls["n"] == 2


def test_stale_span_bisect_depth_capped(tmp_path, monkeypatch):
    """The explicit depth cap stops recursion on the same URL."""
    calls = {"n": 0}
    _patch_googlevideo_403(monkeypatch, calls)
    with pytest.raises(ytdlp_hls.StaleGooglevideoUrl, match="depth exceeded"):
        ytdlp_hls._fetch_googlevideo_span_resilient(
            "https://cdn.invalid/stale", (0, 1024), {}, str(tmp_path / "x"), None,
            _depth=ytdlp_hls._GOOGLEVIDEO_BISECT_MAX_DEPTH + 1,
        )
    assert calls["n"] == 0


# ---------------------------------------------------------------- mux level

def test_mux_stale_url_cleans_partials_and_skips_ffmpeg(tmp_path, monkeypatch):
    """A dead URL re-raises (no ffmpeg remote fallback) and partial window
    files from a previous attempt are unlinked so the retry starts clean."""
    out = tmp_path / "window_hls"
    out.mkdir()
    (out / "_v.mp4").write_bytes(b"partial garbage from previous attempt")
    (out / "_a.m4a").write_bytes(b"partial garbage")

    def fake_fetch(url, start_sec, end_sec, headers, dest, cancel_event=None,
                   prefix_cache=None, **kwargs):
        raise ytdlp_hls.StaleGooglevideoUrl(
            f"googlevideo URL expired HTTP 403 for {url[:80]}"
        )

    remote = {"entered": False}

    def fake_remote_mux(*a, **k):
        remote["entered"] = True
        raise AssertionError("remote fallback entered")

    monkeypatch.setattr(ytdlp_hls, "_fetch_googlevideo_window_local", fake_fetch)
    monkeypatch.setattr(ytdlp_hls, "_run_window_fmp4_mux", fake_remote_mux)
    monkeypatch.setattr(ytdlp_ffmpeg, "_resolve_ffmpeg_exe", lambda: "ffmpeg.exe")
    with pytest.raises(ytdlp_hls.StaleGooglevideoUrl):
        ytdlp_hls._mux_dash_window_to_hls(
            "https://cdn.invalid/v", "https://cdn.invalid/a", str(out), 0.0, 25.0, {},
        )
    assert remote["entered"] is False, "a dead URL must never reach the ffmpeg fallback"
    assert not (out / "_v.mp4").exists(), "partial video must be unlinked"
    assert not (out / "_a.m4a").exists(), "partial audio must be unlinked"


def test_mux_truncated_body_keeps_remote_fallback(tmp_path, monkeypatch):
    """A throttled-but-alive URL (truncated body) still gets the ffmpeg
    remote fallback — only genuinely expired URLs skip it."""
    out = tmp_path / "window_hls"
    out.mkdir()

    def fake_fetch(url, start_sec, end_sec, headers, dest, cancel_event=None,
                   prefix_cache=None, **kwargs):
        raise ytdlp_hls.GooglevideoTruncatedBody(
            f"googlevideo response truncated HTTP 206 for {url[:80]}"
        )

    def fake_remote_mux(*a, **k):
        raise RuntimeError("remote fallback entered")

    monkeypatch.setattr(ytdlp_hls, "_fetch_googlevideo_window_local", fake_fetch)
    monkeypatch.setattr(ytdlp_hls, "_run_window_fmp4_mux", fake_remote_mux)
    monkeypatch.setattr(ytdlp_ffmpeg, "_resolve_ffmpeg_exe", lambda: "ffmpeg.exe")
    with pytest.raises(RuntimeError, match="remote fallback entered"):
        ytdlp_hls._mux_dash_window_to_hls(
            "https://cdn.invalid/v", "https://cdn.invalid/a", str(out), 0.0, 25.0, {},
        )
    assert not (out / "_v.mp4").exists(), "partials are still cleaned pre-fallback"


# ---------------------------------------------------------------- session level

def test_session_reextract_uses_fresh_urls(tmp_path, monkeypatch):
    """403 -> one fresh-URL re-extract -> mux succeeds on the fresh URL only.

    The pre-loop refresh (call #0) is the mux's initial freshness pass; the
    retry refresh (call #1) is the single re-extract cycle after the 403."""
    s = _sess(tmp_path)
    mux_calls: list = []
    refreshed = {"n": 0}

    def refresh(session, prefer_height=720):
        n = refreshed["n"]
        refreshed["n"] += 1
        if n == 0:  # pre-loop freshness pass keeps the session's current set
            u, au = A, A_AUDIO
        else:  # re-extract after the 403 — fresh URLs only
            u, au = B, B_AUDIO
        session.variant_entries = [(720, u)]
        session.entry_url = u
        session.preview_audio_url = au

    _patch_session_mux(monkeypatch, mux_calls, refresh, fail_urls={A})
    assert sess_mod._ensure_youtube_window_hls_mux(s) is True
    assert refreshed["n"] == 2, "pre-loop pass + exactly one retry re-extract"
    assert mux_calls == [(A, A_AUDIO), (B, B_AUDIO)], "fresh URLs only on the retry"


def test_session_never_retries_same_url_twice(tmp_path, monkeypatch):
    """If the re-extract hands back the SAME dead URL, it is never re-muxed;
    retry re-extracts are bounded at 2 and the progressive fallback takes over."""
    s = _sess(tmp_path)
    mux_calls: list = []
    refreshed = {"n": 0}

    def refresh(session, prefer_height=720):
        # pathological: innertube keeps returning the identical expired URL
        refreshed["n"] += 1
        session.variant_entries = [(720, A)]
        session.entry_url = A
        session.preview_audio_url = A_AUDIO

    _patch_session_mux(monkeypatch, mux_calls, refresh, fail_urls={A})
    monkeypatch.setattr(
        sess_mod, "_try_fallback_from_window_hls",
        lambda session, prefer_height=720: True,
    )
    assert sess_mod._ensure_youtube_window_hls_mux(s) is True
    assert mux_calls == [(A, A_AUDIO)], "the dead URL is never attempted twice"
    # 1 pre-loop pass + 2 bounded retry re-extracts (never a third)
    assert refreshed["n"] == 3


def test_session_reextract_bounded_two_cycles(tmp_path, monkeypatch):
    """Even when every fresh URL 403s, retry re-extracts stop at 2 (3 mux
    attempts total) and the loop falls back to progressive."""
    s = _sess(tmp_path)
    mux_calls: list = []
    refreshed = {"n": 0}
    urls = [A, B, "https://cdn.invalid/entryC"]

    def refresh(session, prefer_height=720):
        n = refreshed["n"]
        refreshed["n"] += 1
        u = urls[min(n, len(urls) - 1)]
        session.variant_entries = [(720, u)]
        session.entry_url = u
        session.preview_audio_url = u.replace("entry", "audio")

    _patch_session_mux(monkeypatch, mux_calls, refresh, fail_urls=set(urls))
    monkeypatch.setattr(
        sess_mod, "_try_fallback_from_window_hls",
        lambda session, prefer_height=720: True,
    )
    assert sess_mod._ensure_youtube_window_hls_mux(s) is True
    # 1 pre-loop pass + 2 retry re-extracts — the retry ceiling
    assert refreshed["n"] == 3
    assert len(mux_calls) == 3, "initial + 2 fresh-URL attempts, never more"
    assert len({u for u, _ in mux_calls}) == 3, "every attempt used a distinct URL"
