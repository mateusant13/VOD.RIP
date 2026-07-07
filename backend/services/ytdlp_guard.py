"""Single gate for all yt-dlp — blocks PO plugins (headless Chrome) and serializes YoutubeDL."""

from __future__ import annotations

import contextlib
import os
import threading
from typing import Any, Iterator

from services import ytdlp_env  # noqa: F401 — YTDLP_NO_PLUGINS before yt-dlp import

# Force off — setdefault is not enough; a prior import could leave plugins enabled.
os.environ["YTDLP_NO_PLUGINS"] = "1"

import yt_dlp  # noqa: E402

_YTDLP_LOCK = threading.Lock()
_FORBIDDEN_YOUTUBE_KEYS = frozenset({"fetch_pot"})


def assert_ytdlp_safe() -> None:
    """Fail fast at startup if PO plugins could load (getpot_wpc spawns Chrome)."""
    if os.environ.get("YTDLP_NO_PLUGINS") != "1":
        raise RuntimeError("YTDLP_NO_PLUGINS must be 1 — refusing to run with yt-dlp PO plugins")


def sanitize_ytdlp_opts(opts: dict[str, Any]) -> dict[str, Any]:
    """Strip fetch_pot and other keys that trigger headless browser providers."""
    out = dict(opts)
    ext = out.get("extractor_args")
    if not isinstance(ext, dict):
        return out
    ext = dict(ext)
    yt = ext.get("youtube")
    if isinstance(yt, dict):
        yt = {k: v for k, v in yt.items() if k not in _FORBIDDEN_YOUTUBE_KEYS}
        yt["fetch_pot"] = ["never"]
        ext["youtube"] = yt
    out["extractor_args"] = ext
    return out


@contextlib.contextmanager
def guarded_youtube_dl(opts: dict[str, Any]) -> Iterator[yt_dlp.YoutubeDL]:
    """Only supported way to construct YoutubeDL — one instance at a time, no PO plugins."""
    assert_ytdlp_safe()
    safe = sanitize_ytdlp_opts(opts)
    with _YTDLP_LOCK:
        with yt_dlp.YoutubeDL(safe) as ydl:
            yield ydl


# Back-compat alias used by ytdlp_hls tests / comments.
YTDLP_EXTRACT_LOCK = _YTDLP_LOCK

assert_ytdlp_safe()
assert sanitize_ytdlp_opts({
    "extractor_args": {"youtube": {"fetch_pot": ["auto"], "player_client": ["ios"]}},
})["extractor_args"]["youtube"]["fetch_pot"] == ["never"]
