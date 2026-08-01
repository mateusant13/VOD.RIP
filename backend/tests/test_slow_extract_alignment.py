"""Slow-path EXTRACT INFO alignment + negative-cache guard (Bug 9).

Pytest collects the pure rule tests; the settings-dependent alignment check
(`check_*`) is exercised by `python tests/test_slow_extract_alignment.py`
(plain-assert self-check, no test framework).

  python tests/test_slow_extract_alignment.py
  pytest tests/test_slow_extract_alignment.py
"""

from services.ytdlp_hls import (
    _EXTRACT_NEG_TTL_SEC,
    _should_neg_cache_youtube,
    _youtube_soft_neg_error,
    youtube_preview_ytdl_opts,
)

YOUTUBE_URL = "https://www.youtube.com/watch?v=1tap3CLaqr8"


def check_preview_opts_aligned_with_fast_path():
    """Slow path shares the fast path's opts: android_vr ladder + POT fetched.

    Needs the real settings environment (YouTubeSession build pulls in deps /
    cookie cache), so it only runs under the plain-python __main__ self-check.
    """
    opts = youtube_preview_ytdl_opts(YOUTUBE_URL)
    yt_args = (opts.get("extractor_args") or {}).get("youtube", {})
    clients = yt_args.get("player_client")
    assert clients == ["android_vr", "android", "web_safari"], (
        f"slow path must use the least bot-gated client ladder, got {clients}"
    )
    # POT must be fetched unless the operator forced fast-only mode (env off by default).
    assert yt_args.get("fetch_pot") != "never", (
        "fetch_pot=never starves the slow path of POT and trips bot-gates"
    )
    assert yt_args.get("fetch_pot") != ["never"]
    assert opts.get("_preview_fast") is True


def test_negative_cache_skipped_when_innertube_says_playable():
    """pb_kind == 'ok' (InnerTube probed the video OK) -> transient collapse, no poison."""
    generic = RuntimeError("YouTube preview unavailable for this video")
    opts: dict = {}
    assert _youtube_soft_neg_error(generic)
    assert not _should_neg_cache_youtube(generic, opts, "ok"), (
        "must NOT negative-cache a video InnerTube just played"
    )


def test_negative_cache_applies_for_unknown_or_bot_gated():
    """Unknown playability or real bot-gate keeps the short backoff."""
    opts: dict = {}
    bot_gate = RuntimeError("Sign in to confirm you're not a bot")
    assert _should_neg_cache_youtube(bot_gate, opts, "retry")
    assert _should_neg_cache_youtube(bot_gate, opts, "")


def test_negative_cache_never_for_warm_light():
    """warm_light failures stay uncached — the click's full chain may still work."""
    bot_gate = RuntimeError("Sign in to confirm you're not a bot")
    assert not _should_neg_cache_youtube(bot_gate, {"_warm_light": True}, "retry")


def test_negative_cache_ttl_bounded():
    """120s poison window -> 30s so healthy videos recover quickly."""
    assert _EXTRACT_NEG_TTL_SEC == 30, _EXTRACT_NEG_TTL_SEC


def test_negative_cache_not_for_non_soft_errors():
    """Network timeouts etc. are not soft-negative markers."""
    net_err = RuntimeError("timed out after 5000 ms")
    assert not _youtube_soft_neg_error(net_err)
    assert not _should_neg_cache_youtube(net_err, {}, "ok")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    check_preview_opts_aligned_with_fast_path()
    print("PASS check_preview_opts_aligned_with_fast_path")
    print("all slow-extract alignment self-checks passed")
