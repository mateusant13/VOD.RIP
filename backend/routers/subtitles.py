"""Live YouTube subtitles for URL-only previews (no archive row).

The preview chat panel shows captions for videos opened from a bare URL —
99% YouTube. Resolution is caption-first: one InnerTube ANDROID player-client
call (the same token-free path the manager's `yt-transcript` tool uses) asks
for the caption tracklist directly, and the winning track is served straight
from its timedtext URL. Only when that fast path yields nothing usable does
the router fall back to the historical full yt-dlp video-info resolve — which
paid a complete format-ladder extraction before touching a single caption.
`VODRIP_CAPTION_FIRST=0` disables the fast path.

Manual subtitles are preferred over auto-generated for each requested
language (pt > en > es family preference, mirroring archive_ytdlp's
_CAPTION_LANG_PREF); the best available track is served as preview-panel
transcript rows ({offset_sec, text}) so the panel's existing Subtitles tab
renders them unchanged.

Track bodies are fetched with the same 429 retry/backoff + format fallback
(vtt -> json3 -> srv3) the archive ingest uses. It deliberately does NOT use
yt-dlp's ``_write_subtitles``: that downloads every regional/merged track
matching the requested language families (each a separate, rate-limit-prone
request — a single HTTP 429 killed the whole call) and adds file I/O for
nothing.

Results are cached in a small process-lifetime LRU keyed by video id —
negative results are cached too, so a caption-less video is not re-fetched
on every tab switch. Concurrent requests for the same video share one
in-flight fetch (single-flight) instead of each spawning a full extraction.
Never touches the archive DB.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import OrderedDict
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

# Same policy as archive_ytdlp: payload fallback order (VTT carries word
# timestamps; the timedtext API rate-limits VTT (HTTP 429) while json3/srv3
# still serve) and 429 retry/backoff.
_CAPTION_FMTS = ("vtt", "json3", "srv3")
_CAPTION_RETRIES = 2
_CAPTION_BACKOFF_S = 1.0

# Caption-first fast path: wall-clock budgets for the InnerTube player call
# and the timedtext track fetch. Both are far below the yt-dlp resolve they
# replace, so a hung fast path must not make the endpoint slower than the
# fallback it short-circuits.
_CAPTION_FIRST = "VODRIP_CAPTION_FIRST"
_CAPTION_FIRST_PLAYER_TIMEOUT_S = 8.0
_CAPTION_FIRST_HTTP_TIMEOUT_S = 15.0
_CAPTION_FIRST_UA = (
    "com.google.android.youtube/21.26.364 (Linux; U; Android 11) gzip"
)

_CACHE_MAX = 64
_MISS = object()

# In-flight single-flight: video_id -> (done event, result holder). The
# holder stores the payload, or the exception the fetcher raised (waiters
# re-raise it instead of duplicating the fetch).
_INFLIGHT_TIMEOUT_S = 180.0


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
_inflight_lock = threading.Lock()
_inflight: dict[str, tuple[threading.Event, dict[str, object]]] = {}


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


def _subtitles_opts() -> dict:
    """Mirror archive_ytdlp._yt_opts extraction side: skip_download + the
    player clients that serve caption metadata. No caption-writing flags —
    tracks are fetched straight from their timedtext URLs (_fetch_track)."""
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "extractor_args": {"youtube": {"player_client": ["android", "web_safari"]}},
        **_ytdlp_engine_opts(),
    }


def _caption_family_pref() -> tuple[str, ...]:
    """Caption family ranking for this machine's user.

    An explicit ``asr_language`` setting wins (pt/es/en — the user picked
    it, keep it first); 'auto'/unset falls back to the UI language family
    (pt-BR → pt first, es → es first). Repo-wide default: pt > en > es.
    """
    fam = ""
    try:
        from deps import settings_mgr  # lazy: avoids import cycles

        s = settings_mgr.get()
        explicit = getattr(s, "asr_language", "auto") or "auto"
        if explicit != "auto" and explicit.split("-")[0] in _SUBTITLE_FAMILY_PREF:
            fam = explicit.split("-")[0]
        else:
            ui = getattr(s, "ui_language", "") or ""
            fam = {"pt-BR": "pt", "es": "es", "en": "en"}.get(ui, "")
    except Exception:
        fam = ""
    if fam:
        rest = tuple(f for f in _SUBTITLE_FAMILY_PREF if f != fam)
        return (fam, *rest)
    return _SUBTITLE_FAMILY_PREF


def _track_key(lang: str, manual_langs: set[str]) -> tuple[int, int, int]:
    """Sort key: language-family preference, exact pref code, manual first."""
    family = lang.split("-")[0]
    fam_pref = _caption_family_pref()
    fam_idx = fam_pref.index(family) if family in fam_pref else len(fam_pref)
    exact = 0 if lang in _SUBTITLE_LANG_PREF else 1
    manual = 0 if lang in manual_langs else 1
    return (fam_idx, exact, manual)


def _candidate_tracks(info: dict) -> list[tuple[str, bool, str]]:
    """Caption tracks of the requested families, ranked best-first.

    Manual tracks (``info['subtitles']``) and auto tracks
    (``info['automatic_captions']``) are merged; ranking mirrors
    ``_track_key``: family preference (pt > en > es), then exact pref code,
    then manual over auto. Merged/translated codes ('en-de-DE', 'aa-pt-BR')
    and out-of-family ASR junk ('aa', 'ab') are skipped — they are
    translation blobs with no search value.

    Returns [(lang, is_manual, url), ...].
    """
    manual = {str(k).lower(): v for k, v in (info.get("subtitles") or {}).items()}
    auto = {str(k).lower(): v for k, v in (info.get("automatic_captions") or {}).items()}
    out: list[tuple[str, bool, str]] = []
    for lang in set(manual) | set(auto):
        if lang.count("-") >= 2:
            continue  # merged/translated track (e.g. 'en-de-DE')
        if lang.split("-")[0] not in _SUBTITLE_FAMILY_PREF:
            continue  # ASR junk codes ('aa', 'ab', ...)
        entries = manual.get(lang) or auto.get(lang)
        url = next((e.get("url") for e in entries if e.get("url")), None)
        if not url:
            continue
        out.append((lang, lang in manual, url))
    out.sort(key=lambda t: _track_key(t[0], set(manual)))
    return out


def _fetch_track(ydl, lang: str, entries: list[dict]) -> tuple[str, str, str] | None:
    """Download one caption track: format fallback + 429 retry.

    Mirrors archive_ytdlp._fetch_caption: each format (vtt -> json3 ->
    srv3) gets up to _CAPTION_RETRIES attempts with a 1s backoff on HTTP
    429; any failure falls through to the next format. Returns
    (lang, fmt, payload) or None when nothing served.
    """
    for fmt in _CAPTION_FMTS:
        url = next(
            (e.get("url") for e in entries if e.get("ext") == fmt and e.get("url")),
            None,
        )
        if not url:
            continue
        for attempt in range(_CAPTION_RETRIES):
            try:
                data = ydl.urlopen(url).read().decode("utf-8", "replace")
                return lang, fmt, data
            except Exception as exc:
                if getattr(exc, "code", None) == 429 and attempt < _CAPTION_RETRIES - 1:
                    time.sleep(_CAPTION_BACKOFF_S)
                    continue
                logger.warning("subtitle track %s (%s) failed: %s", fmt, lang, exc)
                break
    return None


def _payload_from_info(url: str, info: dict, opener) -> dict | None:
    """Walk the ranked candidates of an info dict and serve the first track
    that fetches. `opener` needs only urlopen(url).read() (a YoutubeDL or the
    caption-first HTTP adapter). Returns the response payload dict, or None
    when no candidate served — the caller decides what "nothing found" means.
    """
    merged = {
        str(k).lower(): v
        for k, v in {
            **(info.get("subtitles") or {}),
            **(info.get("automatic_captions") or {}),
        }.items()
    }
    for lang, is_manual, _url in _candidate_tracks(info):
        got = _fetch_track(opener, lang, merged.get(lang) or [])
        if got is None:
            continue
        fmt, data = got[1], got[2]
        segments = _parse_vtt(data) if fmt == "vtt" else _parse_caption(fmt, data)
        return {
            "url": url,
            "lang": lang,
            "source": "manual" if is_manual else "auto",
            "has_subtitles": True,
            "rows": [{"offset_sec": seg["start_sec"], "text": seg["text"]} for seg in segments],
        }
    return None


class _CaptionHttpError(Exception):
    """Non-200 timedtext fetch. `.code` mirrors yt-dlp's DownloadError so
    _fetch_track's 429 retry/backoff sees the same contract."""

    def __init__(self, code: int, url: str) -> None:
        super().__init__(f"HTTP {code}: {url}")
        self.code = code


class _CaptionResp:
    """urllib-response-shaped wrapper so _fetch_track can stay agnostic."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class _CaptionOpener:
    """Adapter: exposes the yt-dlp `.urlopen(url).read()` contract over
    _caption_http_get so the shared track walk serves both paths."""

    def urlopen(self, url: str) -> _CaptionResp:
        return _caption_http_get(url)


def _caption_http_get(url: str) -> _CaptionResp:
    """GET one timedtext URL through the shared InnerTube session with the
    ANDROID UA — the client whose tracklist produced the URL, so it serves
    without a PO token (verified against the yt-transcript tool path)."""
    from services.youtube_innertube import _http_for  # lazy: circular-import guard

    resp = _http_for(None).get(
        url,
        headers={"User-Agent": _CAPTION_FIRST_UA},
        timeout=_CAPTION_FIRST_HTTP_TIMEOUT_S,
    )
    if resp.status_code != 200:
        raise _CaptionHttpError(resp.status_code, url)
    return _CaptionResp(resp.content)


def _caption_track_url(base_url: str, fmt: str) -> str:
    """timedtext URL for one format — mirrors yt-dlp's process_language
    (`fmt=<fmt>&xosf=`; xosf=1 corrupts text positions, yt-dlp #13654)."""
    if base_url.startswith("//"):
        base_url = f"https:{base_url}"
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}fmt={fmt}&xosf="


def _caption_first_info(video_id: str) -> dict | None:
    """InnerTube ANDROID player response -> yt-dlp-shaped info dict.

    Only the caption containers are built (the walk below reads nothing
    else). Language codes and the manual/auto split mirror yt-dlp's
    get_lang_code + `kind == 'asr'` rule so _candidate_tracks ranks the
    fast-path tracks exactly like the fallback's. Returns None when the
    player response carries no usable captionTracks.
    """
    from services import youtube_innertube as it  # lazy: circular-import guard

    # _player_request returns data even on a playability failure — caption
    # tracks can be present while formats are gated — so the FailureKind is
    # deliberately ignored; only captionTracks decides.
    data, _status, _kind = it._player_request(
        video_id, it._PROFILE_BY_NAME["ANDROID"], _CAPTION_FIRST_PLAYER_TIMEOUT_S
    )
    renderer = ((data or {}).get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}
    subs: dict[str, list[dict]] = {}
    auto: dict[str, list[dict]] = {}
    for track in renderer.get("captionTracks") or []:
        base_url = track.get("baseUrl") or ""
        if not base_url:
            continue
        is_asr = track.get("kind") == "asr"
        lang = (
            str(track.get("vssId") or "").lstrip(".").replace(".", "-")
            or str(track.get("languageCode") or "")
        )
        if is_asr and lang.startswith("a-"):
            lang = lang[2:]
        if not lang:
            continue
        entries = auto.setdefault(lang, []) if is_asr else subs.setdefault(lang, [])
        for fmt in _CAPTION_FMTS:
            entries.append({"ext": fmt, "url": _caption_track_url(base_url, fmt)})
    if not subs and not auto:
        return None
    return {"subtitles": subs, "automatic_captions": auto}


def _caption_first(url: str) -> dict | None:
    """Caption-first fast path: tracklist via one InnerTube call, then the
    shared ranked walk. Returns the payload dict on a hit; None (→ caller
    runs the yt-dlp fallback unchanged) when disabled, no video id, no
    captionTracks, or no candidate served. Any exception degrades to None —
    the fast path must never turn a working fallback into a 502."""
    if os.environ.get(_CAPTION_FIRST, "1").strip() == "0":
        return None
    video_id = _video_id(url)
    if not video_id:
        return None
    try:
        info = _caption_first_info(video_id)
        if info is None:
            return None
        return _payload_from_info(url, info, _CaptionOpener())
    except Exception as exc:  # noqa: BLE001 — degrade to the yt-dlp fallback
        logger.info("caption-first path failed for %s, using yt-dlp: %s", video_id, exc)
        return None


def _fetch_subtitles(url: str, langs: list[str]) -> dict:
    """Fetch the best available caption track for one YouTube URL.

    Caption-first: the InnerTube fast path (see _caption_first) resolves the
    tracklist without a full video-info extract. On a miss the historical
    yt-dlp path runs unchanged — it is what still defines the 502 semantics.
    The ranked candidate list (see _candidate_tracks) is walked until a
    track serves; ``has_subtitles`` is False when the video has none of the
    requested languages.
    """
    payload = _caption_first(url)
    if payload is not None:
        return payload
    try:
        with guarded_youtube_dl(_subtitles_opts()) as ydl:
            info = ydl.extract_info(url, download=False) or {}
            got = _payload_from_info(url, info, ydl)
            if got is not None:
                return got
            return {"url": url, "lang": None, "source": None, "has_subtitles": False, "rows": []}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("subtitles fetch failed for %s: %s", url, exc)
        raise HTTPException(status_code=502, detail=f"Could not fetch subtitles: {exc}")


def _subtitle_langs_default() -> str:
    """Default ``langs`` list for /api/subtitles: the UI language family
    first (pt-BR → pt, es → es, en → en), then the other families in the
    repo-wide order — so captions follow the UI language unless the caller
    passes an explicit preference list."""
    fam = ""
    try:
        from deps import settings_mgr  # lazy: avoids import cycles

        ui = getattr(settings_mgr.get(), "ui_language", "") or ""
        fam = {"pt-BR": "pt", "es": "es", "en": "en"}.get(ui, "")
    except Exception:
        fam = ""
    if fam:
        return ",".join([fam, *[f for f in _SUBTITLE_FAMILY_PREF if f != fam]])
    return _SUBTITLE_LANGS_DEFAULT


@router.get("/api/subtitles")
def get_subtitles(
    url: str = Query(...),
    langs: str | None = Query(None, description="comma-separated language preference list; default follows the UI language"),
) -> dict:
    """Captions for a URL-only YouTube video as preview-panel transcript rows.

    ``langs`` is a comma-separated language preference list; when omitted
    it follows the UI language (pt first for pt-BR, es first for Spanish,
    en otherwise — see _subtitle_langs_default). The best available track
    among the requested families is returned — manual subtitles preferred
    over auto-generated, and the family ranking mirrors the UI/explicit
    caption language (see _caption_family_pref).

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
    lang_list = [lang.strip() for lang in (langs or _subtitle_langs_default()).split(",") if lang.strip()]
    if not lang_list:
        lang_list = ["en", "pt", "es"]

    # Single-flight: concurrent requests for the same video (tab re-open,
    # retry pressed while the first fetch is still running) share one
    # extraction instead of each spawning a full yt-dlp fetch behind the
    # global yt-dlp lock.
    with _inflight_lock:
        entry = _inflight.get(video_id)
        if entry is None:
            done = threading.Event()
            holder: dict[str, object] = {"payload": _MISS}
            _inflight[video_id] = (done, holder)
            fetcher = True
        else:
            done, holder = entry
            fetcher = False
    if fetcher:
        try:
            payload = _fetch_subtitles(url, lang_list)
            holder["payload"] = payload
        except Exception as exc:  # noqa: BLE001 — waiters re-raise it
            holder["payload"] = exc
            raise
        finally:
            done.set()
            with _inflight_lock:
                _inflight.pop(video_id, None)
    else:
        if not done.wait(_INFLIGHT_TIMEOUT_S):
            # Fetcher hung past the wait — fetch anyway (best-effort dedupe).
            logger.warning("subtitles in-flight wait timed out for %s", video_id)
            payload = _fetch_subtitles(url, lang_list)
        else:
            got = holder["payload"]
            if isinstance(got, BaseException):
                raise got
            payload = got
    _subs_cache.put(video_id, payload)
    return payload
