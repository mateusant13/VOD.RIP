"""Real-time live captions — a refcounted per-(platform, channel) captioner.

The livestream popup's CC overlay needs a rolling transcription of the live
audio. One ``LiveCaptioner`` per (platform, channel) polls the channel's live
HLS media playlist (audio-only rendition — a fraction of the bandwidth),
downloads NEW segments (a seen-set skips re-downloads), decodes them to mono
16 kHz PCM via ffmpeg, buffers a ~2s caption window, VAD-splits the window
and sends the float32 window to the optional ASR runtime over loopback. The
runtime owns VAD/model loading and downloads missing model files on first use.
translated by ``caption_translate`` (NLLB ct2 int8 + SLID gating) — every
failure degrades to the raw ASR text. The whole loop runs in its own worker
thread (SSE / HTTP stay responsive); caption blocks are pushed into a bounded
asyncio queue via ``call_soon_threadsafe`` — the same per-connection queue
pattern as the live chat SSE.

Lifecycle: refcounted by active SSE subscribers — the first ``acquire()``
starts the worker thread, the last ``release()`` stops it. ASR requests use
the loopback runtime client, keeping native model imports and memory in the
separate worker process.

Failures: transient (playlist fetch error, decode error) back off and retry,
keeping the loop alive; a channel confirmed offline (resolver returns None
``_OFFLINE_STRIKES`` times in a row, or the playlist carries ENDLIST) emits
an ``offline`` event and stops. The SSE generator turns that into the last
frame; the frontend hides the overlay. The worker NEVER raises into the
generator.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import re
import subprocess as sp
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Optional
from services.http_fingerprint import BROWSER_USER_AGENT
from services.ytdlp_ffmpeg import _resolve_ffmpeg_exe
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

SUPPORTED_PLATFORMS = ("twitch", "kick", "youtube")

# --- error ring + file log (Phase D) ----------------------------------------
_ERROR_RING_MAX = 50
_ERROR_RING: "collections.deque[dict]" = collections.deque(maxlen=_ERROR_RING_MAX)
_ERROR_RING_LOCK = threading.Lock()

def _error_log_path() -> Path:
    try:
        from services.settings import _get_appdata_dir
        return _get_appdata_dir() / "logs" / "live-captions.log"
    except Exception:
        return Path("logs") / "live-captions.log"

def _record_error(kind: str, message: str, platform: str = "", channel: str = "") -> None:
    entry = {"ts": time.time(), "kind": kind, "message": str(message)[:500], "platform": platform, "channel": channel}
    with _ERROR_RING_LOCK:
        _ERROR_RING.append(entry)
    try:
        pp = _error_log_path()
        pp.parent.mkdir(parents=True, exist_ok=True)
        try:
            if pp.exists() and pp.stat().st_size > 5 * 1024 * 1024:
                bak = pp.with_suffix(".1.log")
                try:
                    bak.unlink(missing_ok=True)
                except Exception:
                    pass
                pp.rename(bak)
        except Exception:
            pass
        ts = datetime.fromtimestamp(entry["ts"], tz=timezone.utc).isoformat()
        msg = entry["message"]
        line = f"{ts} [{kind}] {platform}/{channel} {msg}\n"
        with pp.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
def get_error_ring(limit: int = 50) -> list[dict]:
    with _ERROR_RING_LOCK:
        items = list(_ERROR_RING)[-max(1, min(limit, _ERROR_RING_MAX)):]
    return items

def _clear_error_ring_for_tests() -> None:
    with _ERROR_RING_LOCK:
        _ERROR_RING.clear()

# --- env knobs (all optional) ------------------------------------------------
WINDOW_SEC_ENV = "VODRIP_CAPTION_WINDOW_SEC"
POLL_SEC_ENV = "VODRIP_CAPTION_POLL_SEC"
MAX_BACKLOG_SEC_ENV = "VODRIP_CAPTION_MAX_BACKLOG_SEC"
LOW_LATENCY_ENV = "VODRIP_CAPTION_LOW_LATENCY"
# Target AV alignment when a captions session is active: keep video
# ~0.8-1.0s behind the live edge so the transcript's parakeet trail
# (~0.3-1s on a 2s window) lands ≤1.0s behind speech. The predictor is
# comment-only (no new config): the frontend honors it by riding
# liveSyncDurationCount 2 (≈4s) vs 3 (≈6s) only while captions are ON
# and the buffer is healthy (>4s), and the backend honors it by
# flushing captions as soon as the VAD window completes with no extra
# queue delay (call_soon_threadsafe put_nowait in _emit — see _asr_worker).
CAPTION_TARGET_ALIGN_SEC = 0.9  # ponytail: comment constant; upgrade path is env knob if per-site tuning needed
# NOTE: this value documents the *ASR pipeline* internal latency target
# (audio-in → caption-out). The *end-to-end* behind-speech latency is
# higher: HLS live edge count 2 ≈ 4s + parakeet 0.3–1s ≈ 4.3–5s total.
# count 3 ≈ 6s + parakeet ≈ 6.3–7s. Adjust liveSyncDurationCount
# (frontend) to trade latency for stability; this constant tracks only
# the backend pipeline target.
# ~2s of speech is a usable caption block (~2-4 words per second of speech);
# a 1s window would yield 2-3 words — too short to read.
#
# INTENTIONAL DELAY: the transcript needs the audio it describes, so a
# caption for window [t0, t1] is emitted right after t1 plus the transcribe
# latency — never before the window's audio is complete. Parakeet (CPU
# int8) on a 2s window transcribes in ~0.3-1s, so the caption text lands
# ~0.5s after the words it transcribes; the player's own 0.8-1.0s
# behind-live nudge (liveSyncDurationCount 2 when captions ON) then
# puts it ~1.0s behind the live edge. The 2s window (was 3s) makes each
# 2s Twitch segment flush alone — the old 3s window waited for a second
# segment to fill, adding a full segment of pipeline latency. The
# frontend anchors every block to the VIDEO clock (frag PDT), so the
# visible lag is just this transcribe trail, self-adaptive per machine
# (stalls pause the overlay automatically).
WINDOW_SEC = 2.0
WINDOW_SEC_REDUCED = 1.0  # low-latency mode: flush every ~1s instead of ~2s
POLL_SEC = 1.0  # media-playlist poll cadence (~1 segment ahead of the player)
# Cap on untranscribed stale audio: after the live freezes/buffers, one poll
# returns the whole gap; transcribing it segment-by-segment keeps captions
# stuck on audio the player already live-synced past. Dropping the stale
# head (see LiveCaptioner._trim_stale_head) bounds the transcript's lag —
# the realtime-transcript contract. ~10s covers a normal hiccup and still
# feels live; the player's own live-sync seek skips the same audio.
_MAX_BACKLOG_SEC = 10.0
_FLUSH_FAIL_LIMIT = 3  # consecutive ASR flush failures -> offline (never a silent dead stream)
_FLUSH_FAIL_LIMIT_LOW_LATENCY = 5  # smaller windows = more empty/short transcriptions, tolerate more
_MASTER_TTL_SEC = 300.0  # re-resolve the live master (fresh usher tokens)
_MASTER_403_TTL_SEC = 60.0  # ...or sooner after a 403 (expired token)
_OFFLINE_STRIKES = 3  # consecutive offline resolutions -> offline event + stop
_TRANSIENT_STRIKE_LIMIT = 6  # consecutive generic errors (404, network) -> offline + stop
_BACKOFF_INITIAL_SEC = 1.0
_BACKOFF_MAX_SEC = 15.0
_QUEUE_MAX = 8  # bounded subscriber queue — slow consumers drop blocks
_TRANSLATE_QUEUE_MAX = 8  # bounded FIFO translate queue — drop-oldest on overflow
_HTTP_TIMEOUT = 10.0

class _Offline(RuntimeError):
    """Channel confirmed offline or the stream ended."""


# --- HLS parsing (master audio rendition + media playlist) -------------------

_ATTR_RE = re.compile(r'([A-Z0-9-]+)="([^"]*)"')
_PDT_RE = re.compile(r"#EXT-X-PROGRAM-DATE-TIME:\s*(.+)")
_EXTINF_RE = re.compile(r"#EXTINF:\s*([\d.]+)")


def _parse_master_audio_url(master_text: str, master_url: str) -> Optional[str]:
    """Pick the audio-only rendition URI from an HLS master playlist.

    Twitch usher masters (allow_audio_only=true) carry
    ``#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio_only",NAME="Audio Only",...
    URI="..."``; Kick masters use a GROUP-ID/NAME without the audio_only
    marker. Prefer an audio_only-ish group/name, else any TYPE=AUDIO line;
    fall back to the first ``#EXT-X-STREAM-INF`` variant when the master has
    no audio rendition. Relative URIs resolve against the master URL.
    """
    best: Optional[tuple[int, str]] = None
    for line in master_text.splitlines():
        if not line.startswith("#EXT-X-MEDIA:TYPE=AUDIO"):
            continue
        attrs = dict(_ATTR_RE.findall(line))
        uri = attrs.get("URI", "")
        if not uri:
            continue
        group = (attrs.get("GROUP-ID") or "").lower()
        name = (attrs.get("NAME") or "").lower()
        score = 2 if ("audio_only" in group or "audio only" in name) else 1
        if best is None or score > best[0]:
            best = (score, uri)
    if best is not None:
        return urljoin(master_url, best[1])
    # Audio-only rendition absent — fall back to the LOWEST BANDWIDTH
    # STREAM-INF variant (least likely to cause buffering on the CDN).
    _bw_re = re.compile(r"BANDWIDTH=(\d+)")
    lowest_bw: Optional[tuple[int, str]] = None
    for block in master_text.split("#EXT-X-STREAM-INF")[1:]:
        bw = 0
        uri = ""
        for bline in block.splitlines():
            bstripped = bline.strip()
            if not bstripped:
                continue
            m = _bw_re.search(bstripped)
            if m:
                bw = int(m.group(1))
            elif not bstripped.startswith("#"):
                uri = bstripped
                break
        if uri:
            if lowest_bw is None or bw < lowest_bw[0]:
                lowest_bw = (bw, uri)
    if lowest_bw is not None:
        return urljoin(master_url, lowest_bw[1])
    return None


def _parse_iso_epoch(value: str) -> float:
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _parse_media_playlist(text: str, base_url: str) -> tuple[list[dict], bool]:
    """Parse a live media playlist.

    Returns ``(segments, is_live)``: segments are ``{uri, dur, pdt}`` dicts
    in playlist order (pdt = epoch seconds of the segment start, or None),
    and ``is_live`` is False when the playlist carries ``#EXT-X-ENDLIST``
    (the stream ended). LL-HLS ``#EXT-X-PART`` lines and map headers are
    ignored — only COMPLETE ``#EXTINF`` segments are transcribed.
    """
    segments: list[dict] = []
    is_live = True
    cur_dur = 0.0
    cur_pdt: Optional[float] = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-ENDLIST"):
            is_live = False
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME"):
            m = _PDT_RE.match(line)
            if m:
                try:
                    cur_pdt = _parse_iso_epoch(m.group(1))
                except ValueError:
                    cur_pdt = None
        elif line.startswith("#EXTINF"):
            m = _EXTINF_RE.search(line)
            if m:
                cur_dur = float(m.group(1))
        elif line.startswith("#"):
            continue
        elif line and cur_dur > 0:
            segments.append({"uri": urljoin(base_url, line), "dur": cur_dur, "pdt": cur_pdt})
            cur_dur = 0.0
            cur_pdt = None
    return segments, is_live


# --- fetch / decode ----------------------------------------------------------

def _fetch(url: str, headers: dict) -> bytes:
    """GET *url* with the resolver's headers (+ a browser UA). Raises on any
    non-2xx — the worker treats that as a transient failure."""
    import requests

    hdrs = dict(headers or {})
    # Same quirk the preview proxy handles (services/preview/session.py
    # _request_headers): Twitch edge CDNs (*.ttvnw.net) 403 an EMPTY body for
    # any request carrying an Origin header — the usher master fetch needs
    # Origin, the CDN playlist/segment fetches must not send it.
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    # Twitch edge CDNs (*.ttvnw.net) 403 an EMPTY body for any request
    # carrying an Origin header — but the Usher API (usher.ttvnw.net)
    # REQUIRES Origin.  Strip Origin only for CDN hosts, keep it for usher.
    if (host == "ttvnw.net" or host.endswith(".ttvnw.net")) and host != "usher.ttvnw.net":
        hdrs = {k: v for k, v in hdrs.items() if k.lower() != "origin"}
    hdrs.setdefault("User-Agent", BROWSER_USER_AGENT)
    resp = requests.get(url, headers=hdrs, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def _decode_audio_bytes(data: bytes) -> "Any":
    """Decode one HLS segment (TS / fMP4 / ADTS AAC) to mono 16 kHz float32.

    The decoder returns 16 kHz mono float32 samples and feeds raw segment
    bytes through ffmpeg's stdin — no temp files, one subprocess per segment.
    """
    import numpy as np

    cmd = [
        _resolve_ffmpeg_exe(), "-v", "error", "-threads", "1",
        "-i", "pipe:0", "-threads", "1",
        "-f", "f32le", "-ac", "1", "-ar", "16000", "-",
    ]
    proc = sp.run(cmd, input=data, capture_output=True, timeout=60)
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace")[-300:]
        raise RuntimeError(f"ffmpeg segment decode failed: {stderr}")
    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    return samples.copy()  # writable copy for the runtime request


def _resolve_live_master(platform: str, channel: str) -> Optional[dict]:
    """Resolve the live master playlist — reuses the SAME resolver the
    ``/api/live/{platform}`` endpoint uses (services.live_capture), so auth /
    token / ad-rotation logic stays in one place. Twitch uses
    ``twitch_live_info`` (GQL + quick usher probe, ~0.5–1.5s) — the same
    fast path as ``/api/live`` — not the full vaft ad-rotation probe
    (1 GQL + usher + media per player type). Returns ``{url, headers}`` or
    None when the channel is offline / unreachable."""
    if platform == "youtube":
        try:
            from services.yt_gate import youtube_gate_active
            if youtube_gate_active():
                logger.debug("live captions youtube/%s gated — skipping resolve", channel)
                _record_error("hls", "youtube gate active — cooldown", platform, channel)
                return None
        except Exception:
            pass
        try:
            from services.live_capture import youtube_live_info
            info = youtube_live_info(channel)
            if not info or not info.get("url"):
                if info and info.get("reason"):
                    _record_error("hls", info["reason"], platform, channel)
                return None
            return {"url": info["url"], "headers": info.get("headers") or {}}
        except Exception as exc:
            _record_error("hls", f"youtube resolve failed: {exc}", platform, channel)
            return None
    from services.live_capture import kick_live_info, twitch_live_info

    if platform == "twitch":
        info = twitch_live_info(channel)
        if not info or not info.get("url"):
            if info and info.get("reason"):
                _record_error("hls", info["reason"], platform, channel)
            return None
        return {"url": info["url"], "headers": info.get("headers") or {}}

    info = kick_live_info(channel)
    if not info or not info.get("url"):
        return None
    return {"url": info["url"], "headers": info.get("headers") or {}}


def _transcribe_window(audio: "Any", duration: float) -> tuple[str, Optional[str]]:
    """Transcribe one decoded float32/16 kHz window through the ASR worker.

    The base process deliberately does not import the native ASR stack. The
    optional worker owns VAD/model loading and downloads missing model files on
    this first real caption request.
    """
    del duration  # the worker receives the complete fixed-rate audio window
    from services.asr_runtime import transcribe_window

    return transcribe_window(audio.tobytes())


def _warm_asr() -> bool:
    """Start the optional ASR worker without loading the model."""
    try:
        from services.asr_runtime import ensure_server

        ensure_server()
        return True
    except Exception as exc:
        logger.debug(
            "live captions ASR pre-warm failed (first flush loads lazily): %s", exc
        )
        return False


def captions_available(platform: str) -> tuple[bool, str]:
    """Report whether the optional ASR runtime can serve captions.

    The model itself is intentionally not required here: it is downloaded by
    the ASR worker on the first actual caption request.
    """
    plat = (platform or "").lower()
    if plat not in SUPPORTED_PLATFORMS:
        return False, "captions support twitch, kick and youtube"
    from services.asr_runtime import runtime_available

    if not runtime_available():
        return False, "speech runtime is not installed — start a caption stream to download it"
    return True, ""


def _resolve_evidence(platform: str, channel: str) -> Optional[str]:
    """Best-effort channel-language family for the caption translation gate."""
    _EVIDENCE_TTL_SEC = 24 * 3600
    now = time.monotonic()
    cache_key = (platform, channel)
    cached = _evidence_cache.get(cache_key)
    if cached and (now - cached[1]) < _EVIDENCE_TTL_SEC:
        return cached[0]
    try:
        from services.channel_language import aggregate_channel_language

        result = aggregate_channel_language(platform, channel).get("language")
        _evidence_cache[cache_key] = (result, now)
        return result
    except Exception:
        return None


_evidence_cache: dict[tuple[str, str], tuple[Optional[str], float]] = {}


def _warm_translate(evidence: Optional[str], target_family: Optional[str] = None) -> None:
    """Pre-load the translator models OFF the worker's critical path.

    Only when translation can ever be needed: evidence unknown (the SLID
    gate may flip) or known-different from the effective target (the app
    language, or the per-session ?lang= override). Loads in a background
    daemon thread (~1 s NLLB ct2-int8 load) so the first captions arrive
    raw and switch to translated once the model is resident — a blocking
    warm would delay the first caption past the "few seconds" contract."""
    try:
        from services import caption_translate as ct
    except Exception:
        return
    if not ct.enabled() or ct.nllb_dir() is None:
        return
    try:
        app = target_family or ct.app_language_family()
        if evidence and evidence == app:
            return  # stream known to be in the effective target — never translates
    except Exception:
        pass
    threading.Thread(
        target=ct.prewarm, daemon=True, name="live-captions-translate-warm"
    ).start()


def _maybe_translate(
    captioner: "LiveCaptioner", text: str, audio: "Any", *, lang: Optional[str] = None
) -> tuple[str, bool]:
    """Language-gated translation of one caption window.

    Sticky session lock: once ``_session_family`` is set, only translate when
    it differs from the target. If not yet locked, collect SLID votes (when
    SLID is available) and do NOT translate — the ASR thread locks via
    ``lock_source_family`` after 3 agreeing detections. Any failure degrades
    to the raw ASR text — captions never block on translation.

    ``lang`` is the ASR-detected language from ``_transcribe_window`` — fed
    into ``lock_source_family`` for immediate evidence when available.
    Returns (caption_text, translated)."""
    try:
        from services import caption_translate as ct

        if not ct.enabled() or ct.nllb_dir() is None:
            return text, False
        evidence = captioner._evidence_family
        # Lock session family from evidence (immediate) or SLID/ASR votes (majority).
        if captioner._session_family is None:
            if evidence is not None:
                locked = ct.lock_source_family(evidence, captioner._lang_votes, asr_lang=lang)
                if locked:
                    captioner._session_family = locked
            elif ct.slid_dir() is not None:
                fam = ct.detect_language(audio)
                if fam:
                    captioner._lang_votes.append(fam)
                locked = ct.lock_source_family(
                    evidence, captioner._lang_votes, asr_lang=lang,
                )
                if locked:
                    captioner._session_family = locked
            elif lang:
                # SLID absent but NLLB present — parakeet asr_lang is the only
                # gate signal; accumulate it so lock_source_family can reach
                # SLID_VOTE_MIN (without this branch translation never started).
                from services.caption_translate import _TARGET_TOKEN

                if lang in _TARGET_TOKEN:
                    captioner._lang_votes.append(lang)
                locked = ct.lock_source_family(
                    evidence, captioner._lang_votes, asr_lang=lang,
                )
                if locked:
                    captioner._session_family = locked
        app = captioner._target_family or ct.app_language_family()
        # Translate iff session family is locked and differs from target
        sf = captioner._session_family
        if sf is None or sf == app:
            return text, False
        out = ct.translate(text, app, source_family=sf)
        return (out, True) if out else (text, False)
    except Exception as exc:
        logger.debug(
            "live captions %s/%s translate skipped: %s",
            captioner.platform, captioner.channel, exc,
        )
        return text, False


# --- captioner registry ------------------------------------------------------

_CAPTIONERS: dict[tuple[str, str], "LiveCaptioner"] = {}
_REGISTRY_LOCK = threading.Lock()
_MAX_CONCURRENT_CAPTIONERS = 10  # ponytail: env knob if needed; prevents anon resource exhaustion



def get_captioner(
    platform: str, channel: str, loop: asyncio.AbstractEventLoop
) -> Optional["LiveCaptioner"]:
    """The shared captioner for (platform, channel), created on first use.

    Returns None when the concurrent-captioner cap is hit (router should
    return 429 to prevent anon resource exhaustion)."""
    key = (platform, channel)
    with _REGISTRY_LOCK:
        c = _CAPTIONERS.get(key)
        if c is None:
            if len(_CAPTIONERS) >= _MAX_CONCURRENT_CAPTIONERS:
                return None
            c = LiveCaptioner(platform, channel, loop)
            _CAPTIONERS[key] = c
        return c


def _unregister(captioner: "LiveCaptioner") -> None:
    key = (captioner.platform, captioner.channel)
    with _REGISTRY_LOCK:
        if _CAPTIONERS.get(key) is captioner:
            _CAPTIONERS.pop(key, None)




# --- the captioner -----------------------------------------------------------

class LiveCaptioner:
    """One worker per (platform, channel), refcounted by SSE subscribers."""

    def __init__(
        self,
        platform: str,
        channel: str,
        loop: asyncio.AbstractEventLoop,
        *,
        window_sec: Optional[float] = None,
        poll_sec: Optional[float] = None,
        max_backlog_sec: Optional[float] = None,
    ):
        self.platform = platform
        self.channel = channel
        self.loop = loop
        # Every SSE subscriber gets its OWN bounded queue (asyncio.Queue
        # created on the running loop at subscribe time). The worker fan-outs
        # every caption/offline event to each queue so one slow or dropped
        # viewer NEVER steals caption blocks from another (the old single
        # shared `events` queue). _subscriber_lock guards the set against
        # concurrent subscribe/unsubscribe from the asyncio loop.
        self._subscribers: "set[asyncio.Queue]" = set()
        self._subscriber_lock = threading.Lock()
        # Read from settings first, fall back to env var for backward compat.
        try:
            from deps import settings_mgr
            _low_lat = settings_mgr.get().caption_low_latency
        except Exception:
            _low_lat = False
        if not _low_lat:
            _low_lat = (os.environ.get(LOW_LATENCY_ENV, "0") or "0").strip() == "1"
        self._low_latency = bool(_low_lat)
        _default_window = WINDOW_SEC_REDUCED if self._low_latency else WINDOW_SEC
        self.window_sec = window_sec if window_sec is not None else _env_float(WINDOW_SEC_ENV, _default_window)
        self.poll_sec = poll_sec if poll_sec is not None else _env_float(POLL_SEC_ENV, POLL_SEC)
        self.max_backlog_sec = (
            max_backlog_sec
            if max_backlog_sec is not None
            else _env_float(MAX_BACKLOG_SEC_ENV, _MAX_BACKLOG_SEC)
        )
        # ponytail: low-latency uses smaller windows which produce more
        # empty/short transcriptions; raise the failure threshold so the
        # captioner doesn't go offline on transient short windows.
        self._flush_fail_limit = _FLUSH_FAIL_LIMIT_LOW_LATENCY if self._low_latency else _FLUSH_FAIL_LIMIT
        self._life_lock = threading.Lock()
        self._refcount = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # ASR thread (separate from fetch/decode): processes windows from a
        # FIFO queue so fetch/decode is never blocked by CUDA inference.
        self._asr_thread: Optional[threading.Thread] = None
        self._asr_stop = threading.Event()
        self._asr_window_ready = threading.Event()
        self._asr_queue: deque = deque(maxlen=3)  # FIFO, max 2-3 windows, drop oldest
        self._asr_lock = threading.Lock()  # protects _asr_queue popleft/append
        # Translate thread: a SINGLE dedicated FIFO worker off the ASR
        # critical path. _maybe_translate can spend ~0.9s on NLLB CPU per
        # window; running it inline in _asr_worker would block the next ASR
        # window AND the SSE emit. One ordered worker preserves caption
        # order + the latency target (see #3). Drop-oldest on overflow so a
        # saturated NLLB never accumulates stale windows.
        self._translate_thread: Optional[threading.Thread] = None
        self._translate_stop = threading.Event()
        self._translate_window_ready = threading.Event()
        self._translate_queue: deque = deque(maxlen=_TRANSLATE_QUEUE_MAX)
        self._translate_lock = threading.Lock()  # protects _translate_queue popleft/append
        self._buffer_lock = threading.Lock()
        # Worker-owned state (touched only by the worker thread).
        self._seen: set[str] = set()
        self._buffer: Any = None  # np float32 16 kHz samples of the window
        self._buffer_sec = 0.0
        self._origin: Optional[float] = None  # epoch of stream time 0 (PDT-derived)
        self._stream_sec = 0.0  # cumulative duration of ingested segments
        self._flush_failures = 0  # consecutive ASR flush failures (-> offline)
        self._master: Optional[dict] = None
        self._master_at = 0.0
        self._media_url: Optional[str] = None
        self._offline_strikes = 0
        self._transient_strikes = 0
        # Translation state (worker-owned): channel-language evidence resolved
        # once at start; SLID detections for the last 5 speech windows feed
        # the majority gate when evidence is unknown (caption_translate).
        # _session_family: sticky language lock — once locked (3 agreeing
        # detections), IGNORE later SLID flips for the session.
        self._evidence_family: Optional[str] = None
        self._lang_votes: Deque[str] = deque(maxlen=5)
        self._session_family: Optional[str] = None
        self._target_family: Optional[str] = None
        # Per-session translate-target override (?lang= on the SSE) — the
        # LAST explicit selection wins for all subscribers of the shared
        # captioner; None = follow the app language at flush time. ponytail:
        # a per-connection target would need per-viewer translation; the
        # shared captioner emits one stream, so one target applies.

    # --- lifecycle -------------------------------------------------------

    def acquire(self, lang: Optional[str] = None) -> None:
        """Refcount++ — starts the worker thread on the first subscriber.

        ``lang`` (pt | en | es) overrides the caption translate-target for
        the session; None on a fresh session resets to the app-language
        The worker starts on the first subscriber and stops when the last
        subscriber releases it."""
        with self._life_lock:
            self._refcount += 1
            if self._refcount == 1:
                th = self._thread
                if th is not None and th.is_alive():
                    # The last release set _stop; wait for the old thread so a
                    # restart never runs two polling loops (bounded — the
                    # worker checks _stop between every step).
                    th.join(timeout=2.0)
                # Fresh pipeline on restart: the seen-set / buffer / master of
                # a previous run must not leak into the new one.
                self._seen = set()
                self._buffer = None
                self._buffer_sec = 0.0
                self._origin = None
                self._stream_sec = 0.0
                self._flush_failures = 0
                self._master = None
                self._master_at = 0.0
                self._media_url = None
                self._offline_strikes = 0
                self._transient_strikes = 0
                self._evidence_family = None
                self._lang_votes = deque(maxlen=5)
                self._session_family = None  # sticky language lock: reset on session start
                self._asr_queue.clear()
                self._asr_stop.clear()
                with self._translate_lock:
                    self._translate_queue.clear()
                self._translate_stop.clear()
                self._translate_window_ready.clear()
                self._target_family = lang  # fresh session: explicit selection or app-language default
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._run,
                    daemon=True,
                    name=f"live-captions-{self.platform}-{self.channel}",
                )
                self._thread.start()
            elif lang is not None:
                # Active session: LAST explicit ?lang= wins for all subscribers.
                self._target_family = lang
                _warm_translate(self._evidence_family, lang)
    def release(self) -> None:
        """Refcount-- — stops the worker thread when the last subscriber
        leaves. Idempotent-safe: a second release on an already-released
        captioner is a no-op."""
        with self._life_lock:
            if self._refcount == 0:
                return  # already released — nothing to stop or un-reserve
            self._refcount -= 1
            if self._refcount == 0:
                self._stop.set()
                self._asr_stop.set()
                self._asr_window_ready.set()  # unblock ASR thread so it sees stop
                self._translate_stop.set()
                self._translate_window_ready.set()  # unblock translate worker

    # --- ASR thread --------------------------------------------------------

    def _start_asr_thread(self) -> None:
        """Start the dedicated ASR thread that dequeues windows and transcribes."""
        self._asr_stop.clear()
        self._asr_thread = threading.Thread(
            target=self._asr_worker, daemon=True,
            name=f"live-captions-asr-{self.platform}-{self.channel}",
        )
        self._asr_thread.start()

    def _stop_asr_thread(self) -> None:
        """Stop the ASR thread (called on release or crash)."""
        self._asr_stop.set()
        self._asr_window_ready.set()  # unblock wait
        th = self._asr_thread
        if th is not None:
            th.join(timeout=3.0)
        self._asr_thread = None

    # --- translate thread --------------------------------------------------

    def _start_translate_thread(self) -> None:
        """Start the single dedicated FIFO translate worker."""
        self._translate_stop.clear()
        self._translate_thread = threading.Thread(
            target=self._translate_worker, daemon=True,
            name=f"live-captions-xlate-{self.platform}-{self.channel}",
        )
        self._translate_thread.start()

    def _stop_translate_thread(self) -> None:
        """Stop the translate worker (called on release or crash)."""
        self._translate_stop.set()
        self._translate_window_ready.set()  # unblock wait
        th = self._translate_thread
        if th is not None:
            th.join(timeout=3.0)
        self._translate_thread = None

    def _enqueue_translate(
        self, text, audio, buffer_sec, win_start_off, origin, lang
    ) -> None:
        """Push a transcribed window onto the ordered translate queue.

        start/end are computed HERE (at ingest/offload time) so the
        translate worker's scheduling jitter never shifts captions on the
        timeline — same invariant the ASR queue already keeps.
        """
        start = win_start_off + (origin or 0.0)
        end = start + buffer_sec
        with self._translate_lock:
            if len(self._translate_queue) >= _TRANSLATE_QUEUE_MAX:
                dropped = self._translate_queue.popleft()
                logger.warning(
                    "live captions %s/%s translate queue full — dropped oldest window (%s)",
                    self.platform, self.channel, dropped[0] if dropped else "?",
                )
            self._translate_queue.append(
                (text, audio, round(start, 3), round(end, 3), origin, lang)
            )
        self._translate_window_ready.set()

    def _translate_get_item(self):
        """Block until a window is queued or stop is set."""
        while not self._translate_stop.is_set():
            with self._translate_lock:
                if self._translate_queue:
                    return self._translate_queue.popleft()
            self._translate_window_ready.clear()
            self._translate_window_ready.wait(timeout=0.5)
        return None

    def _translate_worker(self) -> None:
        """Single FIFO translate worker (off the ASR critical path).

        Dequeues windows in order and runs _maybe_translate on each, so
        NLLB CPU work is serialized on ONE thread and caption ordering is
        preserved. The ASR thread now only transcribes and enqueues — it
        never blocks on translation. _maybe_translate never raises (it
        returns (text, False) on error), but we guard anyway so a worker
        bug can't silently kill caption delivery.
        """
        while not self._translate_stop.is_set():
            item = self._translate_get_item()
            if item is None or self._translate_stop.is_set():
                return
            text, audio, start, end, origin, lang = item
            if self._translate_stop.is_set():
                return
            try:
                text, translated = _maybe_translate(self, text, audio, lang=lang)
                if not text:
                    continue
                logger.info(
                    "live captions %s/%s translated %.3f-%.3fs via %s: %s",
                    self.platform, self.channel, start, end,
                    "NLLB" if translated else "pass-through", text[:80],
                )
                payload = {
                    "text": text,
                    "start": start,
                    "end": end,
                }
                if translated:
                    payload["translated"] = True
                if origin is not None:
                    payload["latency_ms"] = round((time.time() - end) * 1000)
            except Exception:
                logger.exception(
                    "live captions %s/%s translate worker error — dropping window",
                    self.platform, self.channel,
                )
                continue
            self._emit("caption", payload)

    def _asr_get_window(self) -> Optional[tuple[Any, float]]:
        """Block until a window is available or stop is set. Returns
        (audio, buffer_sec) or None on shutdown."""
        while not self._asr_stop.is_set():
            with self._asr_lock:
                if self._asr_queue:
                    return self._asr_queue.popleft()
            self._asr_window_ready.clear()
            self._asr_window_ready.wait(timeout=0.5)
        return None

    def _asr_worker(self) -> None:
        """ASR thread: dequeue windows, wait for warm, transcribe, emit.

        Window timing (start/end) is computed at INGEST time and passed in
        the queue, so the ASR thread's scheduling jitter never shifts
        captions on the timeline."""
        while not self._asr_stop.is_set():
            item = self._asr_get_window()
            if item is None or self._asr_stop.is_set():
                return
            audio, buffer_sec, win_start_off, origin = item
            if audio is None or self._asr_stop.is_set():
                return
            try:
                text, lang = _transcribe_window(audio, buffer_sec)
            except Exception as exc:
                _record_error("asr", str(exc), self.platform, self.channel)
                self._flush_failures += 1
                logger.warning(
                    "live captions %s/%s ASR flush failed (%d/%d): %s",
                    self.platform, self.channel, self._flush_failures,
                    self._flush_fail_limit, exc,
                )
                if self._flush_failures >= self._flush_fail_limit:
                    logger.error(
                        "live captions %s/%s disabled — %d consecutive ASR failures: %s",
                        self.platform, self.channel, self._flush_failures, exc,
                    )
                    self._emit("offline", {"reason": f"asr failure: {exc}"})
                    self._stop.set()
                continue
            self._flush_failures = 0
            if not text:
                continue
            # Offload translation to the single dedicated FIFO translate
            # worker so NLLB CPU (~0.9s/window) never blocks the next ASR
            # window or the SSE emit. _maybe_translate is no longer called
            # on the ASR critical path — order + latency preserved by the
            # ordered translate queue (see _translate_worker).
            self._enqueue_translate(text, audio, buffer_sec, win_start_off, origin, lang)

    # --- worker --------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._evidence_family = _resolve_evidence(self.platform, self.channel)
            # Lock immediately from channel evidence if known
            if self._evidence_family:
                from services import caption_translate as _ct
                if self._evidence_family in getattr(_ct, '_TARGET_TOKEN', {}):
                    self._session_family = self._evidence_family
            # Warm ASR in background (never blocks HLS poll start)
            threading.Thread(
                target=_warm_asr, daemon=True,
                name=f"live-captions-warm-{self.platform}-{self.channel}",
            ).start()
            _warm_translate(self._evidence_family, self._target_family)
            self._start_asr_thread()
            self._start_translate_thread()
            self._run_loop()
        except Exception:
            logger.exception(
                "live captions worker crashed for %s/%s", self.platform, self.channel
            )
            self._emit("offline", {})
        finally:
            self._stop_asr_thread()
            self._stop_translate_thread()
            _unregister(self)

    def _run_loop(self) -> None:
        backoff = _BACKOFF_INITIAL_SEC
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                segments = self._poll_segments()
            except _Offline:
                self._offline_strikes += 1
                if self._offline_strikes >= _OFFLINE_STRIKES:
                    logger.info(
                        "live captions %s/%s offline (confirmed)",
                        self.platform, self.channel,
                    )
                    self._emit("offline", {})
                    return
                # Recheck at a poll-scaled cadence — the resolver call itself
                # is the expensive part, so a short fixed pause per strike.
                self._stop.wait(max(self.poll_sec * 3, 0.1))
                continue
            except Exception as exc:
                _record_error("hls", str(exc), self.platform, self.channel)
                # Transient: playlist fetch error / expired token / decode —
                # back off and retry, keeping the loop (and the SSE) alive.
                # After _TRANSIENT_STRIKE_LIMIT consecutive failures the
                # channel is treated as unreachable (token revoked, CDN
                # broken, channel genuinely down) — emit offline so the
                # frontend hides the CC cluster instead of showing keepalives
                # forever.
                self._transient_strikes += 1
                logger.debug(
                    "live captions %s/%s transient error (%d/%d): %s",
                    self.platform, self.channel,
                    self._transient_strikes, _TRANSIENT_STRIKE_LIMIT, exc,
                )
                if self._transient_strikes >= _TRANSIENT_STRIKE_LIMIT:
                    logger.info(
                        "live captions %s/%s offline (transient limit reached)",
                        self.platform, self.channel,
                    )
                    self._emit("offline", {})
                    return
                backoff = min(backoff * 2, _BACKOFF_MAX_SEC)
                self._stop.wait(backoff)
                continue
            backoff = _BACKOFF_INITIAL_SEC
            self._offline_strikes = 0
            self._transient_strikes = 0
            segments = self._trim_stale_head(segments)
            for seg in segments:
                if self._stop.is_set():
                    return
                try:
                    self._ingest(seg)
                except Exception as exc:
                    # One bad segment must not kill the stream — skip it
                    # (a brief caption gap) and keep polling.
                    logger.debug(
                        "live captions %s/%s segment skipped: %s",
                        self.platform, self.channel, exc,
                    )
                    continue
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.05, self.poll_sec - elapsed))

    def _poll_segments(self) -> list[dict]:
        """Resolve/refresh the master as needed, fetch the media playlist,
        and return NEW segments (not in the seen-set). Raises _Offline when
        the channel is offline or the stream ended."""
        now = time.monotonic()
        if self._master is None or (now - self._master_at) >= _MASTER_TTL_SEC:
            self._master = self._resolve_master()
            self._master_at = now
            self._media_url = None
        master = self._master
        try:
            master_text = _fetch(master["url"], master["headers"]).decode("utf-8", "replace")
        except Exception as exc:
            if _is_http_token_error(exc):
                # Expired usher token or offline channel (403/404) —
                # re-resolve sooner than the TTL.
                self._master = None
                self._master_at = 0.0
            raise
        if self._media_url is None:
            media = _parse_master_audio_url(master_text, master["url"])
            if media is None:
                raise RuntimeError("no media rendition in master playlist")
            self._media_url = media
        try:
            playlist = _fetch(self._media_url, master["headers"]).decode("utf-8", "replace")
        except Exception as exc:
            if _is_http_token_error(exc):
                self._master = None
                self._master_at = 0.0
            raise
        segments, is_live = _parse_media_playlist(playlist, self._media_url)
        if not is_live:
            raise _Offline("stream ended")
        new = [s for s in segments if s["uri"] not in self._seen]
        for s in new:
            self._seen.add(s["uri"])
        return new

    def _resolve_master(self) -> dict:
        master = _resolve_live_master(self.platform, self.channel)
        if master is None:
            raise _Offline("channel offline")
        logger.info("live captions %s/%s master resolved", self.platform, self.channel)
        return master

    def _trim_stale_head(self, segments: list[dict]) -> list[dict]:
        """Backlog cap — the realtime-transcript guard against falling behind.

        After the live freezes/buffers/loads, one poll returns the whole gap
        (e.g. 60s) PLUS the live edge. Transcribing it segment-by-segment
        would keep captions stuck on audio the player has already live-synced
        past (hls.js maxLiveSyncPlaybackRate + live-sync seek), so the
        transcript stays behind indefinitely. Drop the stale head (including
        any buffered pre-gap window) and keep only the newest
        ``max_backlog_sec`` of audio; ``_stream_sec`` still
        advances across the dropped audio so PDT-anchored caption times stay
        exact (stream time is a timeline, not a transcription log).
        Segment-quantized (whole segments dropped; a tail up to one segment
        over the cap is fine and avoids splitting decoded samples).
        """
        cap = self.max_backlog_sec
        if not cap or len(segments) <= 1:
            return segments
        durs = [s.get("dur") for s in segments]
        if any(d is None for d in durs):
            return segments  # unknown durations — keep every segment
        total = sum(durs)  # type: ignore[arg-type]
        if total <= cap:
            return segments
        drop_sec = total - cap
        dropped = 0.0
        while len(segments) > 1 and dropped + (segments[0].get("dur") or 0.0) <= drop_sec:
            dropped += segments.pop(0)["dur"]  # type: ignore[typeddict-item]
        if dropped > 0:
            self._stream_sec += dropped
            # The buffered window predates the dropped head — it is part of
            # the stale backlog (the player live-synced past it too). Clear it
            # so the first post-stall caption starts at the live edge; keeping
            # it would flush a window mixing pre-gap audio with the live edge
            # and anchor it mid-gap.
            self._buffer = None
            self._buffer_sec = 0.0
            logger.info(
                "live captions %s/%s dropped %.1fs of stale backlog (cap %.1fs) "
                "— resyncing to live edge",
                self.platform, self.channel, dropped, cap,
            )
        return segments

    def _ingest(self, seg: dict) -> None:
        """Download, decode and buffer one segment. When the window fills,
        snapshot the audio + duration for the ASR thread (never block on
        transcription here — fetch/decode stays fast). Checks _stop between
        the blocking steps so a release (stream switch / popup close) ends
        the session promptly: a segment already being fetched is dropped
        after the fetch, never decoded or transcribed."""
        import numpy as np

        master = self._master
        data = _fetch(seg["uri"], master["headers"])
        if self._stop.is_set():
            return  # session ended mid-fetch — discard the segment
        samples = _decode_audio_bytes(data)
        if samples is None or len(samples) == 0:
            return
        if self._stop.is_set():
            return  # session ended mid-decode — don't roll the window after release
        dur = seg.get("dur") or (len(samples) / 16000.0)
        if self._origin is None and seg.get("pdt") is not None:
            # pdt is the segment's wall-clock start; stream time 0 = pdt
            # minus the duration already consumed before this segment.
            self._origin = seg["pdt"] - self._stream_sec
        with self._buffer_lock:
            if self._buffer is None:
                self._buffer = samples
            else:
                self._buffer = np.concatenate([self._buffer, samples])
            self._buffer_sec += dur
            self._stream_sec += dur
            if self._buffer_sec >= self.window_sec:
                # Snapshot for the ASR thread (safe: _buffer_lock held,
                # ASR thread reads only after we clear). Compute start/end
                # timing HERE so ASR scheduling jitter never shifts captions.
                audio = self._buffer
                buffer_sec = self._buffer_sec
                self._buffer = None
                self._buffer_sec = 0.0
                win_start_off = self._stream_sec - buffer_sec
                origin = self._origin
                # Bounded FIFO: drop oldest if queue is full (max 3 windows)
                with self._asr_lock:
                    if len(self._asr_queue) >= 3:
                        dropped = self._asr_queue.popleft()
                        logger.warning(
                            "live captions %s/%s ASR queue full — dropped oldest window (%.1fs)",
                            self.platform, self.channel,
                            dropped[1] if dropped else 0,
                        )
                    self._asr_queue.append((audio, buffer_sec, win_start_off, origin))
                self._asr_window_ready.set()

    def _emit(self, event: str, data: dict) -> None:
        """Fan the (event, data) tuple out to every subscriber queue.

        Runs on a worker thread (ASR or translate); the asyncio loop is
        touched only via call_soon_threadsafe. A queue that is full (slow
        viewer) silently drops this block rather than stalling the worker
        or stealing another viewer's captions — captions are lossy-known,
        keepalive/offline still get through because the window is small.
        """
        with self._subscriber_lock:
            targets = tuple(self._subscribers)
        if not targets:
            return

        def _put(q: asyncio.Queue, ev: str, payload: dict) -> None:
            try:
                q.put_nowait((ev, payload))
            except asyncio.QueueFull:
                pass  # slow subscriber — drop rather than stall the worker

        self.loop.call_soon_threadsafe(
            lambda: [_put(q, event, data) for q in targets]
        )

    # --- per-subscriber subscription ---------------------------------

    def subscribe(self) -> asyncio.Queue:
        """Register a viewer and return its own bounded caption queue.

        The SSE generator drains THIS queue via queue.get(). Every event the
        worker emits is fan-out to every subscribed queue, so one slow or
        disconnected viewer can neither stall the worker nor swallow blocks
        meant for another viewer. The queue is bounded so a viewer that
        stops reading triggers drops, not worker back-pressure.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)  # created on the loop
        with self._subscriber_lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Stop delivering events to this viewer's queue."""
        with self._subscriber_lock:
            self._subscribers.discard(q)

    # --- worker lifecycle -------------------------------------------


# --- helpers -----------------------------------------------------------------

def _is_http_403(exc: BaseException) -> bool:
    resp = getattr(exc, "response", None)
    return resp is not None and getattr(resp, "status_code", None) == 403


def _is_http_token_error(exc: BaseException) -> bool:
    """Usher token errors: 403 (nauth_token_invalid) or 404 (offline channel)."""
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None) if resp is not None else None
    return code in (403, 404)


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.environ.get(name, "") or default))
    except ValueError:
        return default