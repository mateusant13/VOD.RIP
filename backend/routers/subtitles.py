"""Live YouTube subtitles for URL-only previews (no archive row).

The preview chat panel shows captions for videos opened from a bare URL —
99% YouTube. This router fetches those captions with yt-dlp in skip_download
mode (writesubtitles + writeautomaticsub + subtitleslangs), prefers manual
subtitles over auto-generated for each requested language (yt-dlp's own
merge rule: manual tracks win for a duplicate code), then serves the best
available track as preview-panel transcript rows ({offset_sec, text}) so the
panel's existing Subtitles tab renders them unchanged.

Results are cached in a small process-lifetime LRU keyed by video id —
negative results are cached too, so a caption-less video is not re-fetched
on every tab switch. Never touches the archive DB.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query

from services.archive_ytdlp import _parse_caption, _parse_vtt
from services.ytdlp_ffmpeg import _ytdlp_engine_opts
from services.ytdlp_guard import guarded_youtube_dl

logger = logging.getLogger(__name__)

router = APIRouter(tags=["subtitles"])

# Language preference for the returned track, mirroring
# archive_ytdlp._CAPTION_LANG_PREF (pt-first repo convention) with the
# Spanish family appended for the en/pt/es request. Exact pref codes win
# over regional variants (pt > pt-br); manual beats auto at equal rank.
_SUBTITLE_LANG_PREF = ("pt", "pt-br", "en", "en-orig", "es", "es-419", "es-orig")
_SUBTITLE_FAMILY_PREF = ("pt", "en", "es")
_SUBTITLE_LANGS_DEFAULT = "en,pt,es"

_CACHE_MAX = 64
_MISS = object()


class _SubsCache:
    """Small process-lifetime LRU keyed by video id (None = no captions)."""

    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._data: OrderedDict[str, object] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> object:
        with self._lock:
            if key not in self._data:
                return _MISS
            self._data.move_to_end(key)
            return self._data[key]

    def put(self, key: str, value: object) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)


_subs_cache = _SubsCache(_CACHE_MAX)


def _is_youtube_url(url: str) -> bool:
    try:
        host = (urlparse(url.strip()).hostname or "").lower()
    except ValueError:
        return False
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


def _video_id(url: str) -> str:
    """YouTube video id for v= / youtu.be / shorts / embed URLs, or ''."""
    for pattern in (
        r"[?&]v=([\w-]{6,})",
        r"youtu\.be/([\w-]{6,})",
        r"/shorts/([\w-]{6,})",
        r"/embed/([\w-]{6,})",
    ):
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return ""


def _subtitles_opts(outdir: Path, langs: list[str]) -> dict:
    """Mirror archive_ytdlp._yt_opts: skip_download + caption writing.

    ``subtitleslangs`` entries are family regexes (``pt.*``) so regional
    codes (pt-BR, en-US, es-419) are matched, not just the bare code.
    ``nopart`` keeps written files at their final path (the router parses
    them right after ``_write_subtitles`` returns).
    """
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "nopart": True,
        "extractor_args": {"youtube": {"player_client": ["android", "web_safari"]}},
        **_ytdlp_engine_opts(),
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [f"{lang}.*" for lang in langs],
        "outtmpl": str(outdir / "%(id)s.%(ext)s"),
    }


def _track_lang(name: str) -> str:
    """Language code from a written subtitle filename <id>.<lang>.<ext>[.part]."""
    stem = name[:-5] if name.endswith(".part") else name
    parts = stem.split(".")
    return parts[1].lower() if len(parts) >= 3 else ""


def _track_key(track: tuple[str, str], manual_langs: set[str]) -> tuple[int, int, int]:
    """Sort key: language-family preference, exact pref code, manual first."""
    lang, _ext = track
    family = lang.split("-")[0]
    fam_idx = (
        _SUBTITLE_FAMILY_PREF.index(family) if family in _SUBTITLE_FAMILY_PREF else len(_SUBTITLE_FAMILY_PREF)
    )
    exact = 0 if lang in _SUBTITLE_LANG_PREF else 1
    manual = 0 if lang in manual_langs else 1
    return (fam_idx, exact, manual)


def _fetch_subtitles(url: str, langs: list[str]) -> dict:
    """Fetch the best available caption track for one YouTube URL.

    Returns the response payload dict; ``has_subtitles`` is False when the
    video has none of the requested languages.
    """
    outdir = Path(tempfile.mkdtemp(prefix="yt-subs-"))
    try:
        with guarded_youtube_dl(_subtitles_opts(outdir, langs)) as ydl:
            info = ydl.extract_info(url, download=False) or {}
            video_id = str(info.get("id") or _video_id(url))
            base = ydl.prepare_filename(info, "subtitle")
            written = ydl._write_subtitles(info, base) or []
            tracks: list[tuple[str, str, Path]] = []
            seen: set[str] = set()
            for sub, final in written:
                for candidate in (Path(final), Path(sub)):
                    if not candidate.is_file():
                        continue
                    lang = _track_lang(candidate.name)
                    ext = candidate.name.removesuffix(".part").rsplit(".", 1)[-1]
                    if lang and lang not in seen:
                        seen.add(lang)
                        tracks.append((lang, ext, candidate))
                    break
            if not tracks:
                return {"url": url, "lang": None, "source": None, "has_subtitles": False, "rows": []}
            manual_langs = {str(k).lower() for k in (info.get("subtitles") or {})}
            best = min(tracks, key=lambda t: _track_key(t[:2], manual_langs))
            lang, ext, path = best
            data = path.read_text(encoding="utf-8", errors="replace")
            segments = _parse_vtt(data) if ext == "vtt" else _parse_caption(ext, data)
            return {
                "url": url,
                "lang": lang,
                "source": "manual" if lang in manual_langs else "auto",
                "has_subtitles": True,
                "rows": [{"offset_sec": seg["start_sec"], "text": seg["text"]} for seg in segments],
            }
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("subtitles fetch failed for %s: %s", url, exc)
        raise HTTPException(status_code=502, detail=f"Could not fetch subtitles: {exc}")
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


@router.get("/api/subtitles")
def get_subtitles(
    url: str = Query(...),
    langs: str = Query(_SUBTITLE_LANGS_DEFAULT),
) -> dict:
    """Captions for a URL-only YouTube video as preview-panel transcript rows.

    ``langs`` is a comma-separated language preference list (default
    en,pt,es); the best available track among them is returned — manual
    subtitles preferred over auto-generated, pt > en > es within the app's
    caption ordering (mirror of archive_ytdlp._CAPTION_LANG_PREF).

    Response: {url, lang, source: 'manual'|'auto', has_subtitles,
    rows: [{offset_sec, text}]}. ``has_subtitles`` is False when the video
    has none of the requested languages (the panel then shows "No subtitles
    available for this video.").
    """
    url = url.strip()
    if not _is_youtube_url(url):
        raise HTTPException(status_code=400, detail="Only YouTube URLs are supported")
    video_id = _video_id(url)
    if not video_id:
        raise HTTPException(
            status_code=400, detail="Could not extract a YouTube video id from the URL"
        )
    cached = _subs_cache.get(video_id)
    if cached is not _MISS:
        return cached
    lang_list = [lang.strip() for lang in langs.split(",") if lang.strip()]
    if not lang_list:
        lang_list = ["en", "pt", "es"]
    payload = _fetch_subtitles(url, lang_list)
    _subs_cache.put(video_id, payload)
    return payload
