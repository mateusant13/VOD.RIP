"""Single gate for all yt-dlp — blocks getpot_wpc (Chrome); allows bgutil PO plugin."""

from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path
from typing import Any, Iterator

from services import ytdlp_env  # noqa: F401

import yt_dlp  # noqa: E402

_YTDLP_LOCK = threading.Lock()
_YTDLP_CHANNEL_LOCK = threading.Lock()
_FORBIDDEN_PLUGIN_MARKERS = ("getpot_wpc", "getpot-wpc")
_BLOCKED_YOUTUBE_KEYS = frozenset()


def _forbidden_plugin_present() -> bool:
    try:
        from yt_dlp.plugins import directories as plugin_dirs

        roots = plugin_dirs()
    except Exception:
        return False
    for root in roots:
        try:
            base = Path(root)
            if not base.is_dir():
                continue
            for entry in base.iterdir():
                name = entry.name.lower()
                if any(marker in name for marker in _FORBIDDEN_PLUGIN_MARKERS):
                    return True
        except OSError:
            continue
    return False


def _pot_auto_enabled() -> bool:
    try:
        from services.youtube_pot_service import pot_service_ping

        return pot_service_ping()
    except Exception:
        return False


def assert_ytdlp_safe() -> None:
    """Fail fast if getpot_wpc PO plugin is installed (spawns headless Chrome)."""
    if _forbidden_plugin_present():
        raise RuntimeError(
            "yt-dlp getpot_wpc plugin must not be installed — it spawns headless Chrome",
        )


def sanitize_ytdlp_opts(opts: dict[str, Any]) -> dict[str, Any]:
    """Strip blocked keys; enable bgutil fetch_pot when the POT server is up."""
    out = dict(opts)
    ext = out.get("extractor_args")
    if not isinstance(ext, dict):
        ext = {}
    else:
        ext = dict(ext)
    yt = dict(ext.get("youtube") or {})
    for key in _BLOCKED_YOUTUBE_KEYS:
        yt.pop(key, None)
    if _pot_auto_enabled():
        yt["fetch_pot"] = ["auto"]
    else:
        yt["fetch_pot"] = ["never"]
    bgutil = dict(ext.get("youtubepot-bgutilhttp") or {})
    if _pot_auto_enabled():
        from services.youtube_pot_service import POT_DEFAULT_BASE

        bgutil.setdefault("base_url", [POT_DEFAULT_BASE])
    ext["youtube"] = yt
    if bgutil:
        ext["youtubepot-bgutilhttp"] = bgutil
    out["extractor_args"] = ext
    return out


@contextlib.contextmanager
def guarded_youtube_dl(opts: dict[str, Any]) -> Iterator[yt_dlp.YoutubeDL]:
    """Only supported way to construct YoutubeDL — one instance at a time."""
    assert_ytdlp_safe()
    safe = sanitize_ytdlp_opts(opts)
    with _YTDLP_LOCK:
        with yt_dlp.YoutubeDL(safe) as ydl:
            yield ydl


@contextlib.contextmanager
def guarded_youtube_dl_channel(opts: dict[str, Any]) -> Iterator[yt_dlp.YoutubeDL]:
    """Flat channel playlists — separate lock so preview segment yt-dlp can't starve lists."""
    assert_ytdlp_safe()
    safe = sanitize_ytdlp_opts(opts)
    with _YTDLP_CHANNEL_LOCK:
        with yt_dlp.YoutubeDL(safe) as ydl:
            yield ydl


YTDLP_EXTRACT_LOCK = _YTDLP_LOCK
YTDLP_CHANNEL_LOCK = _YTDLP_CHANNEL_LOCK

assert_ytdlp_safe()
out_never = sanitize_ytdlp_opts({
    "extractor_args": {"youtube": {"fetch_pot": ["auto"], "player_client": ["ios"]}},
})
assert out_never["extractor_args"]["youtube"]["fetch_pot"] in (["auto"], ["never"])
