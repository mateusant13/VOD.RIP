"""A truncated mux body is a TRANSPORT: one attempt, then the fallback.

This is the falsification of the review-objection "the new truncated-body
break exits the retry loop but bypasses the intended fallback". Two arms,
both of which can fail:

  * the attempt count is EXACTLY ONE - a re-extracted URL truncated
    identically three times in a row (measured 2026-10-12 on
    manifest.googlevideo.com, "got 1915 expected 1048577"), so a second
    attempt cannot help. If the fail-fast is removed this test goes red.
  * the session-level fallback DOES run after that break - Googlevideo-
    TruncatedBody subclasses StaleGooglevideoUrl (ytdlp_hls.py:64), so the
    post-loop isinstance guard still classifies it. If that guard ever
    narrows, this test goes red.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from services import ytdlp_hls
from services.preview import session as sess_mod
from services.preview.session import PreviewSession

A = "https://cdn.invalid/entryA"
A_AUDIO = "https://cdn.invalid/audioA"


def _sess(cache_dir: Path) -> PreviewSession:
    s = PreviewSession(
        session_id="t633ca1",
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


def test_truncated_body_costs_one_attempt_and_still_falls_back(tmp_path, monkeypatch):
    mux_calls: list = []
    fallback_calls: list = []
    refreshes = {"n": 0}

    def fake_mux(video_url, audio_url, out_dir, **kwargs):
        mux_calls.append(video_url)
        raise ytdlp_hls.GooglevideoTruncatedBody(
            "truncated Range 0-1048576 on every transport for " + video_url[:80]
        )

    def fake_refresh(session, prefer_height=720, **kwargs):
        refreshes["n"] += 1
        return None

    def fake_fallback(session, *a, **k):
        fallback_calls.append(session.session_id)
        return True

    monkeypatch.setattr(ytdlp_hls, "_mux_dash_window_to_hls", fake_mux)
    monkeypatch.setattr(sess_mod, "_refresh_youtube_window_hls_urls", fake_refresh)
    monkeypatch.setattr(sess_mod, "_try_fallback_from_window_hls", fake_fallback)

    with tempfile.TemporaryDirectory() as d:
        s = _sess(Path(d))
        assert sess_mod._ensure_youtube_window_hls_mux(s) is True

    # Arm 1 — the fail-fast: the same URL truncated identically three times
    # measured 2026-10-12, so a re-extract cannot help and must not be paid for.
    assert len(mux_calls) == 1, ("a truncated body is a transport, not a stale URL — "
                              "re-extracting cannot fix it")
    # Arm 2 — the falsification of "the new break bypasses the fallback":
    # GooglevideoTruncatedBody subclasses StaleGooglevideoUrl (ytdlp_hls.py:64),
    # so the post-loop isinstance guard still classifies it and the fallback runs.
    assert fallback_calls == [s.session_id], ("the break must land in the "
                                                 "fallback, not past it")
    # and no re-extract cycle was paid for: the pre-loop freshness pass is the
    # only refresh, exactly as the measurement in the commit message says.
    assert refreshes["n"] == 1, "a truncated body must not buy a re-extract"


def test_expired_still_gets_its_reextract_cycles(tmp_path, monkeypatch):
    """The other half of the law: an EXPIRED url is still a URL-level failure,
    so it keeps the re-extract cycles it always had and the fail-fast above.
    """
    mux_calls: list = []
    refreshes = {"n": 0}
    # each re-extract hands back a DIFFERENT fresh url — the failed_urls guard
    # refuses to retry a URL it already saw fail, which is correct behaviour
    ladder = ["https://cdn.invalid/entryB", "https://cdn.invalid/entryC"]

    def fake_mux(video_url, audio_url, out_dir, **kwargs):
        mux_calls.append(video_url)
        raise ytdlp_hls.StaleGooglevideoUrl(
            "googlevideo URL expired HTTP 403 for " + video_url[:80]
        )

    def refresh(session, prefer_height=720, **kwargs):
        n = refreshes["n"]
        refreshes["n"] += 1
        u = ladder[min(n, len(ladder) - 1)]
        session.variant_entries = [(720, u)]
        session.preview_audio_url = u + "?audio"

    monkeypatch.setattr(ytdlp_hls, "_mux_dash_window_to_hls", fake_mux)
    monkeypatch.setattr(sess_mod, "_refresh_youtube_window_hls_urls", refresh)
    monkeypatch.setattr(sess_mod, "_try_fallback_from_window_hls", lambda s, *a, **k: True)

    with tempfile.TemporaryDirectory() as d:
        s = _sess(Path(d))
        assert sess_mod._ensure_youtube_window_hls_mux(s) is True

    assert refreshes["n"] >= 2, ("an expired URL is a URL-level failure, so it "
                                 "still pays at least one re-extract cycle after "
                                 "the pre-loop freshness pass")
    assert len(mux_calls) >= 2, ("the retry must actually reach the mux with the "
                                 "freshly refreshed URL")