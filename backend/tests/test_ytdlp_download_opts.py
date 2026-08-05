"""Full-download yt-dlp opts must mirror the preview client ladder.

The 4:13:17 VOD zZeycndzX24 crawled at ~0 B/s (fragment 438-444, ~7.5KB audio
fragments) with the download profile's old ["android", "web"] player_client
while the android_vr preview worked (canplay 1801ms). Downloads now share
YOUTUBE_LEAST_GATED_PLAYER_CLIENTS with the preview; this pins the ladder so a
regression back to the throttled android/web list fails.

  python tests/test_ytdlp_download_opts.py
  pytest tests/test_ytdlp_download_opts.py
"""

from services.ytdlp_download import _build_ydl_opts
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
    assert opts.get("fragment_retries") == 10


def test_download_opts_skip_youtube_branch_for_other_platforms():
    """Non-YouTube URLs must not get YouTube extractor_args at all."""
    opts = _build_ydl_opts("https://www.twitch.tv/videos/123456789", "C:/tmp/t.mp4")
    yt_args = (opts.get("extractor_args") or {}).get("youtube", {})
    assert not yt_args.get("player_client")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all download-opts self-checks passed")
