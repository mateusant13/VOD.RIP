"""YouTube false-'Video unavailable' 404 fix (batch 3).

Proves, with mocked playability payloads (no network):
- transient gates (bot/consent/sign-in age) classify SOFT and never stamp the
  300s fatal cache;
- the preview chain aborts only on DEFINITIVE fatal verdicts;
- create_session retries once on a soft verdict, respects the yt_gate freeze,
  and does not retry fatal verdicts.
"""
import shutil
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from services.preview_service import _manager
from services.ytdlp_hls import (
    _EXTRACT_FATAL_CACHE,
    _EXTRACT_NEG_CACHE,
    _extract_cache_key,
    _youtube_extract_preview_with_retries,
    cached_extract_info,
)

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _clear_caches() -> None:
    _EXTRACT_FATAL_CACHE.clear()
    _EXTRACT_NEG_CACHE.clear()


# ---------------------------------------------------------------------------
# _youtube_extract_preview_with_retries — abort only on DEFINITIVE fatal
# ---------------------------------------------------------------------------

def _retry_patches(stack: ExitStack, verdict):
    """Shared mocks for the preview-with-retries path (offline)."""
    stack.enter_context(patch("services.ytdlp_hls._youtube_extract_pass", return_value=None))
    stack.enter_context(patch("services.ytdlp_hls.preview_fast_only_mode", return_value=True))
    stack.enter_context(patch("services.ytdlp_hls._youtube_manual_auth_configured", return_value=False))
    stack.enter_context(patch("services.ytdlp_hls._youtube_has_user_auth", return_value=True))
    stack.enter_context(
        patch("services.youtube_innertube.innertube_last_playability", return_value=verdict)
    )


def test_preview_retries_soft_verdict_falls_through_to_fallback():
    """A soft gate verdict ('Video unavailable' from LOGIN_REQUIRED probes) must
    NOT abort the chain with the gate reason — it falls through to the slow
    ladder and ends in the generic transient message."""
    with ExitStack() as stack:
        _retry_patches(stack, ("LOGIN_REQUIRED", "Video unavailable", "retry"))
        with pytest.raises(RuntimeError) as ei:
            _youtube_extract_preview_with_retries(YOUTUBE_URL, {})
    assert str(ei.value) == "YouTube preview unavailable for this video"


def test_preview_retries_fatal_verdict_aborts_with_reason():
    """A definitive fatal verdict aborts immediately with the real reason."""
    with ExitStack() as stack:
        _retry_patches(stack, ("ERROR", "This video is unavailable", "fatal"))
        with pytest.raises(RuntimeError) as ei:
            _youtube_extract_preview_with_retries(YOUTUBE_URL, {})
    assert str(ei.value) == "This video is unavailable"


# ---------------------------------------------------------------------------
# Cache stamping — soft verdicts never enter the 300s fatal cache
# ---------------------------------------------------------------------------

def _cache_patches(stack: ExitStack, chain_exc, verdict):
    stack.enter_context(
        patch("services.ytdlp_hls._youtube_extract_with_retries", side_effect=chain_exc)
    )
    stack.enter_context(
        patch("services.youtube_innertube.innertube_last_playability", return_value=verdict)
    )


def test_soft_verdict_never_stamps_fatal_cache():
    """The historical poison: gate probes report reason 'Video unavailable' AND
    the chain raises a fatal-looking message. The soft verdict must keep it out
    of the 300s fatal cache."""
    _clear_caches()
    key = _extract_cache_key(YOUTUBE_URL, {})
    try:
        with ExitStack() as stack:
            _cache_patches(
                stack,
                RuntimeError("This video is unavailable"),
                ("LOGIN_REQUIRED", "Video unavailable", "retry"),
            )
            with pytest.raises(RuntimeError):
                cached_extract_info(YOUTUBE_URL, {})
        assert key not in _EXTRACT_FATAL_CACHE, (
            "soft verdict must not be stamped into the 300s fatal cache"
        )
    finally:
        _clear_caches()


def test_soft_verdict_uses_short_neg_cache_only():
    """Soft verdict + generic soft-collapse message -> 30s neg cache, never the
    300s fatal cache."""
    _clear_caches()
    key = _extract_cache_key(YOUTUBE_URL, {})
    try:
        with ExitStack() as stack:
            _cache_patches(
                stack,
                RuntimeError("YouTube preview unavailable for this video"),
                ("LOGIN_REQUIRED", "Sign in to confirm you're not a bot", "retry"),
            )
            with pytest.raises(RuntimeError):
                cached_extract_info(YOUTUBE_URL, {})
        assert key not in _EXTRACT_FATAL_CACHE
        assert key in _EXTRACT_NEG_CACHE
    finally:
        _clear_caches()


def test_fatal_verdict_stamps_fatal_cache():
    """Definitive fatal verdicts still get the 300s fatal cache (fail fast on
    re-click of a dead video)."""
    _clear_caches()
    key = _extract_cache_key(YOUTUBE_URL, {})
    try:
        with ExitStack() as stack:
            _cache_patches(
                stack,
                RuntimeError("YouTube preview unavailable for this video"),
                ("ERROR", "This video is unavailable", "fatal"),
            )
            with pytest.raises(RuntimeError):
                cached_extract_info(YOUTUBE_URL, {})
        assert key in _EXTRACT_FATAL_CACHE
    finally:
        _clear_caches()


# ---------------------------------------------------------------------------
# create_session retry — retry once on soft, respect the gate freeze
# ---------------------------------------------------------------------------

_SESSION_PLATFORM = "Twitch"  # keeps the post-loop build YouTube-free (offline)


def _session_result():
    return (
        "https://example.com/master.m3u8",
        {},
        _SESSION_PLATFORM,
        [],
        "hls",
        {"duration": 60},
    )


def _session_patches(stack: ExitStack, *, gate_active=False):
    rsi = stack.enter_context(
        patch(
            "services.preview.session.resolve_stream_info",
            side_effect=[RuntimeError("extract boom"), _session_result()],
        )
    )
    stack.enter_context(
        patch("services.preview.session._resolve_and_cache_youtube_snapshot", return_value=None)
    )
    stack.enter_context(patch("services.preview.session.time.sleep"))
    stack.enter_context(patch("services.preview.session._youtube_preview_is_anonymous", return_value=False))
    stack.enter_context(
        patch("services.preview.session._finalize_youtube_session", side_effect=lambda s, c: s)
    )
    stack.enter_context(patch("services.preview.session._resolve_youtube_preview_audio", return_value=None))
    stack.enter_context(
        patch("services.preview.session._resolve_preview_entry", side_effect=lambda s, e, h: e)
    )
    stack.enter_context(patch("services.preview.session._apply_growing_vod_duration"))
    stack.enter_context(patch("services.yt_gate.youtube_gate_active", return_value=gate_active))
    return rsi


def _drop_session(session) -> None:
    with _manager._lock:
        _manager._sessions.pop(session.session_id, None)
    shutil.rmtree(session.cache_dir, ignore_errors=True)


def test_create_session_does_not_retry_soft_verdict_in_request():
    """Gap 1: a soft InnerTube verdict must NOT sleep+retry inside the click —
    the resolve raises on the first failure (503 + Retry-After at the router);
    the 30s neg-cache + frontend bounded retries cover transience."""
    with ExitStack() as stack:
        rsi = _session_patches(stack)
        stack.enter_context(
            patch(
                "services.youtube_innertube.innertube_last_playability",
                return_value=("LOGIN_REQUIRED", "Video unavailable", "retry"),
            )
        )
        sleep = stack.enter_context(
            patch("services.preview.session.time.sleep")
        )
        with pytest.raises(RuntimeError):
            _manager.create_session(YOUTUBE_URL, prefer_height=360)
    assert rsi.call_count == 1, "soft verdict must fail fast — no in-request retry"
    sleep.assert_not_called()


def test_create_session_does_not_retry_fatal_verdict():
    """A definitive fatal verdict must NOT retry — fail fast with the reason."""
    with ExitStack() as stack:
        rsi = _session_patches(stack)
        stack.enter_context(
            patch(
                "services.youtube_innertube.innertube_last_playability",
                return_value=("ERROR", "This video is unavailable", "fatal"),
            )
        )
        with pytest.raises(RuntimeError):
            _manager.create_session(YOUTUBE_URL, prefer_height=360)
    assert rsi.call_count == 1


def test_create_session_skips_retry_during_gate_freeze():
    """While the process-wide yt_gate freeze is armed, even a soft-neg error
    must not retry — the IP is gated, retrying just hammers it."""
    with ExitStack() as stack:
        rsi = _session_patches(stack, gate_active=True)
        with pytest.raises(RuntimeError):
            _manager.create_session(YOUTUBE_URL, prefer_height=360)
    assert rsi.call_count == 1
