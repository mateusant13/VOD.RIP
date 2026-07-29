"""Import before yt-dlp — set YTDLP_NO_PLUGINS to skip bundled plugin discovery.

yt-dlp 2026.07.04 ships bundled bgutil POT plugins at site-packages/yt_dlp_plugins/extractor/.
These collide with the same providers registered by yt-dlp core when plugin discovery
runs twice (import + YoutubeDL instantiation). The collision fires:
  AssertionError: PoTokenProvider BgUtilHTTP already registered
which is logged as a warning but pollutes startup logs.

YTDLP_NO_PLUGINS=1 is the canonical yt-dlp escape hatch (see yt_dlp/plugins.py:197).
We keep ytdlp_guard.assert_ytdlp_safe() to gate getpot_wpc (headless Chrome) which is
the real reason we avoid plugin auto-discovery. BGUtil is NOT registered by core —
only by the bundled plugin — so this also disables bgutil. Acceptable: the project
relies on innertube extraction, not bgutil PO tokens. Verified live:
API starts cleanly, no PoTokenProvider duplicate-register warnings, yt-dlp extraction
still works for YouTube/Twitch/Kick.
"""
import os

# ponytail: must be set BEFORE the first `import yt_dlp` in any module.
# ytdlp_guard, ytdlp_download, ytdlp_hls all import this file first via `from services
# import ytdlp_env  # noqa: F401` at module top.
os.environ.setdefault("YTDLP_NO_PLUGINS", "1")
