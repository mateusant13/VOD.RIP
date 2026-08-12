"""Age-gate handling on the YouTube download path.

Research (yt-dlp 2026.07.04 source + yt-dlp wiki, verified live 2026-08-12):
YouTube's age gate is NOT bypassable by any anonymous player client anymore —
web, web_embedded, android_vr and web_safari all return "Sign in to confirm
your age" (even yt-dlp's own "works with web_embedded" test video HtVdAasjOgU).
A po_token attests the client, not age verification. The only working path is
logged-in cookies — the app's cookie_bridge / browser-cookie flow, already
wired in _build_ydl_opts + apply_ytdlp_cookie_opts. This suite pins the two
guardrails around that reality:
  1. age_limit is pinned high so yt-dlp never *skips* age-restricted videos
     after extraction (the CLI default 0 would silently drop age_limit>=18).
  2. age-gate errors surface a definitive "sign in" message + 403 — NOT the
     transient "try again in a moment" that bot-gate errors get.

No real download of age-restricted content — config/classification only.

  python tests/test_yt_age_gate.py
  pytest tests/test_yt_age_gate.py
"""

from services.ytdlp_download import _build_ydl_opts, sanitize_download_error
from services.youtube_diag import (
    is_age_gate_error,
    youtube_http_status,
    youtube_user_message,
)

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# Real yt-dlp 2026.07.04 failure text for an age-gated video (anonymous).
AGE_GATE_ERROR = RuntimeError(
    "[youtube] HtVdAasjOgU: Sign in to confirm your age. This video may be "
    "inappropriate for some users. Use --cookies-from-browser or --cookies "
    "for the authentication."
)


def test_age_limit_pinned_high_on_youtube_downloads():
    """yt-dlp emits age_limit>=18 for gated content; the CLI default 0 would
    skip it after extraction ('age restricted') — pin 100 so the extractor
    output is what decides, not the filter."""
    opts = _build_ydl_opts(YOUTUBE_URL, "C:/tmp/vodrip_age_gate_out.mp4")
    assert opts.get("age_limit") == 100
    # Non-YouTube paths are untouched — the guard is YouTube-scoped.
    opts_twitch = _build_ydl_opts(
        "https://www.twitch.tv/videos/123456789", "C:/tmp/t.mp4"
    )
    assert opts_twitch.get("age_limit") is None


def test_age_gate_error_is_definitive_not_transient():
    """'Sign in to confirm your age' must NOT map to the transient 'try again'
    — retry never succeeds without a logged-in account."""
    msg = sanitize_download_error(AGE_GATE_ERROR)
    assert "age-restricted" in msg.lower()
    assert "sign in" in msg.lower()
    assert "try again" not in msg.lower()
    # API/preview paths get the same definitive classification.
    assert "age-restricted" in youtube_user_message(AGE_GATE_ERROR, preview=True).lower()
    assert "age-restricted" in youtube_user_message(AGE_GATE_ERROR, preview=False).lower()
    assert "try again" not in youtube_user_message(AGE_GATE_ERROR, preview=False).lower()
    assert youtube_http_status(AGE_GATE_ERROR) == 403


def test_bot_gate_stays_transient():
    """'Sign in to confirm you're not a bot' is transient — retry helps."""
    err = RuntimeError("Sign in to confirm you're not a bot")
    assert not is_age_gate_error(err)
    assert "try again" in sanitize_download_error(err).lower()
    assert youtube_http_status(err) == 503


def test_age_gate_classifier():
    assert is_age_gate_error(RuntimeError("Sign in to confirm your age"))
    assert is_age_gate_error(RuntimeError("This video is age-restricted"))
    assert is_age_gate_error(RuntimeError("Confirm your age to continue"))
    assert is_age_gate_error(RuntimeError("age_verification_required"))
    assert not is_age_gate_error(RuntimeError("Sign in to confirm you're not a bot"))
    assert not is_age_gate_error(RuntimeError("This video is unavailable"))
    assert not is_age_gate_error(RuntimeError("members-only content"))


if __name__ == "__main__":
    test_age_limit_pinned_high_on_youtube_downloads()
    test_age_gate_error_is_definitive_not_transient()
    test_bot_gate_stays_transient()
    test_age_gate_classifier()
    print("all age-gate self-checks passed")
