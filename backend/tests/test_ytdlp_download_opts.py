"""Full-download yt-dlp opts must mirror the preview client ladder.

The 4:13:17 VOD zZeycndzX24 crawled at ~0 B/s (fragment 438-444, ~7.5KB audio
fragments) with the download profile's old ["android", "web"] player_client
while the android_vr preview worked (canplay 1801ms). Downloads now share
YOUTUBE_LEAST_GATED_PLAYER_CLIENTS with the preview; this pins the ladder so a
regression back to the throttled android/web list fails.

The same symptom (fragments stuck at 0.00B/s for 5+ minutes) also occurs on a
blackholed CDN: each attempt burns the 20s socket timeout and the retry storm
(inner HttpFD retries × outer fragment_retries = 10×10) multiplies it into
minutes of dead air. The transport guards below pin the bounded retry budget,
the per-attempt socket timeout, and the stall guard (no bytes for N seconds →
abort with a clear error).

  python tests/test_ytdlp_download_opts.py
  pytest tests/test_ytdlp_download_opts.py
"""

from services.ytdlp_download import (
    STALL_GUARD_NO_PROGRESS_SEC,
    _build_ydl_opts,
    _make_stall_guard_hook,
    _stall_guard_state,
)
from services.ytdlp_ffmpeg import DownloadTimeoutError
from services.ytdlp_hls import YOUTUBE_LEAST_GATED_PLAYER_CLIENTS

YOUTUBE_URL = "https://www.youtube.com/watch?v=zZeycndzX24"


def test_download_opts_use_preview_client_ladder():
    """Full downloads must use the same least bot-gated clients as preview."""
    opts = _build_ydl_opts(YOUTUBE_URL, "C:/tmp/vodrip_test_out.mp4")
    yt_args = (opts.get("extractor_args") or {}).get("youtube", {})
    assert yt_args.get("player_client") == YOUTUBE_LEAST_GATED_PLAYER_CLIENTS, (
        "download must use the preview ladder (android_vr first), got "
        f"{yt_args.get('player_client')}"
    )
    # Transport hardening must survive the client change (regression guard).
    assert opts.get("concurrent_fragment_downloads") == 8
    assert opts.get("fragment_retries") == 3


def test_download_opts_skip_youtube_branch_for_other_platforms():
    """Non-YouTube URLs must not get YouTube extractor_args at all."""
    opts = _build_ydl_opts("https://www.twitch.tv/videos/123456789", "C:/tmp/t.mp4")
    yt_args = (opts.get("extractor_args") or {}).get("youtube", {})
    assert not yt_args.get("player_client")


def test_download_opts_include_stall_guards():
    """Every download carries the bounded retry budget, the per-attempt
    socket timeout, and the stall-guard config."""
    opts = _build_ydl_opts("https://www.twitch.tv/videos/123456789", "C:/tmp/t.mp4")
    # Per-attempt read timeout: a dead socket aborts in 20s, not forever.
    assert opts.get("socket_timeout") == 20
    # Bounded retry budget: inner HttpFD attempts (``retries``) × outer
    # fragment attempts (``fragment_retries``) × socket_timeout must stay
    # in the low-minutes class; the old 10×10×20s was a ~40min worst-case
    # dead window per fragment. Extraction retries are governed by
    # ``extractor_retries`` (separate param), so nothing is lost here.
    assert 0 < opts.get("retries", 0) <= 3
    assert 0 < opts.get("fragment_retries", 0) <= 3
    guard = opts.get("_vodrip_stall_guard") or {}
    assert guard.get("no_progress_abort_sec") == STALL_GUARD_NO_PROGRESS_SEC


def test_stall_guard_state_aborts_after_no_progress():
    """No bytes for the threshold → stall error; byte growth keeps it alive;
    unarmed (still extracting) and finished downloads never trip."""
    base = {"no_progress_abort_sec": STALL_GUARD_NO_PROGRESS_SEC}
    holder = dict(base, armed=True, last_move_wall=0.0)
    assert _stall_guard_state(holder, STALL_GUARD_NO_PROGRESS_SEC - 1) is None
    msg = _stall_guard_state(holder, STALL_GUARD_NO_PROGRESS_SEC)
    assert msg and "0 B/s" in msg
    # Latched: a second check returns the same message.
    assert _stall_guard_state(holder, STALL_GUARD_NO_PROGRESS_SEC + 5) == msg
    assert _stall_guard_state(dict(base, armed=False), 10 ** 6) is None
    assert _stall_guard_state(dict(base, done=True), 10 ** 6) is None


def test_stall_guard_hook_tracks_bytes_and_raises_on_abort():
    """The wrapped hook arms on byte growth (0-byte retry events do not
    reset the clock) and raises DownloadTimeoutError once the guard aborts,
    so ``ydl.download()`` unwinds with the stall error instead of hanging."""
    holder = {"no_progress_abort_sec": STALL_GUARD_NO_PROGRESS_SEC}
    seen: list = []
    hook = _make_stall_guard_hook(holder, seen.append)
    hook({"status": "downloading", "downloaded_bytes": 2048})
    assert holder["armed"] and holder["last_bytes"] == 2048
    assert len(seen) == 1
    hook({"status": "downloading", "downloaded_bytes": 0})  # retry event
    assert holder["last_bytes"] == 2048  # 0-byte events must not reset
    hook({"status": "finished"})
    assert holder["done"]
    holder["abort"] = True
    holder["error"] = "download stalled (0 B/s)"
    try:
        hook({"status": "downloading", "downloaded_bytes": 2048})
    except DownloadTimeoutError as exc:
        assert "0 B/s" in str(exc)
    else:
        raise AssertionError("stalled download must raise DownloadTimeoutError")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all download-opts self-checks passed")


def test_temp_folder_is_not_output_home():
    opts = _build_ydl_opts(
        "https://www.twitch.tv/videos/123", "D:/Videos/clip.mp4", temp_folder="C:/Temp/VOD.RIP",
    )
    paths = opts.get("paths") or {}
    assert paths.get("temp") == "C:/Temp/VOD.RIP"
    assert paths.get("home") != "C:/Temp/VOD.RIP"
