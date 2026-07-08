"""Guards against yt-dlp PO plugins spawning headless Chrome."""

from services.ytdlp_guard import (
    assert_ytdlp_safe,
    guarded_youtube_dl,
    sanitize_ytdlp_opts,
)


def test_ytdlp_plugins_blocked_at_import():
    assert_ytdlp_safe()


def test_sanitize_strips_fetch_pot_auto():
    out = sanitize_ytdlp_opts({
        "extractor_args": {"youtube": {"fetch_pot": ["auto"], "player_client": ["ios"]}},
    })
    assert out["extractor_args"]["youtube"]["fetch_pot"] == ["never"]


def test_guarded_youtube_dl_channel_has_separate_lock():
    from services.ytdlp_guard import YTDLP_CHANNEL_LOCK, YTDLP_EXTRACT_LOCK
    import threading

    assert isinstance(YTDLP_CHANNEL_LOCK, type(threading.Lock()))
    assert YTDLP_CHANNEL_LOCK is not YTDLP_EXTRACT_LOCK
