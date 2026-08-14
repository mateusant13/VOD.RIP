"""Real-time live captions — a refcounted per-(platform, channel) captioner.

The livestream popup's CC overlay needs a rolling transcription of the live
audio. One ``LiveCaptioner`` per (platform, channel) polls the channel's live
HLS media playlist (audio-only rendition — a fraction of the bandwidth),
downloads NEW segments (a seen-set skips re-downloads), decodes them to mono
16 kHz PCM via ffmpeg, buffers a ~2s caption window, VAD-splits the window
and transcribes the speech with the parakeet engine — reusing
``archive_transcribe``'s recognizer (``_parakeet_model`` +
``_transcribe_batch_parakeet``) so the model cache/download logic is shared
with the archive worker. When the app language differs from the stream's
language the caption text is translated by ``caption_translate`` (NLLB ct2
int8 + SLID gating) — every failure degrades to the raw ASR text. The whole
loop runs in its own worker thread (SSE / HTTP stay responsive); caption
blocks are pushed into a bounded asyncio queue via ``call_soon_threadsafe``
— the same per-connection queue pattern as the live chat SSE.

Lifecycle: refcounted by active SSE subscribers — the first ``acquire()``
starts the worker thread, the last ``release()`` stops it. Transcription
imports ``archive_transcribe`` LAZILY inside the worker thread, so network
I/O never holds the model lock and the archive transcribe worker can run
concurrently (VAD serializes on its own lock; parakeet inference is lock-free
per recognizer owner).

Failures: transient (playlist fetch error, decode error) back off and retry,
keeping the loop alive; a channel confirmed offline (resolver returns None
``_OFFLINE_STRIKES`` times in a row, or the playlist carries ENDLIST) emits
an ``offline`` event and stops. The SSE generator turns that into the last
frame; the frontend hides the overlay. The worker NEVER raises into the
generator.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess as sp
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Optional
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

SUPPORTED_PLATFORMS = ("twitch", "kick")

# --- env knobs (all optional) ------------------------------------------------
WINDOW_SEC_ENV = "VODRIP_CAPTION_WINDOW_SEC"
POLL_SEC_ENV = "VODRIP_CAPTION_POLL_SEC"
MAX_BACKLOG_SEC_ENV = "VODRIP_CAPTION_MAX_BACKLOG_SEC"
# ~2s of speech is a usable caption block (~2-4 words per second of speech);
# a 1s window would yield 2-3 words — too short to read.
#
# INTENTIONAL DELAY: the transcript needs the audio it describes, so a
# caption for window [t0, t1] is emitted right after t1 plus the transcribe
# latency — never before the window's audio is complete. Parakeet (CPU
# int8) on a 2s window transcribes in ~0.3-1s, so the caption text lands
# ~0.5s after the words it transcribes; the player's own sub-second
# behind-live (liveSyncDurationCount 0) then puts it ~1-2s behind the live
# edge. The 2s window (was 3s) makes each 2s Twitch segment flush alone —
# the old 3s window waited for a second segment to fill, adding a full
# segment of pipeline latency. The frontend anchors every block to the
# VIDEO clock (frag PDT), so the visible lag is just this transcribe trail,
# self-adaptive per machine (stalls pause the overlay automatically).
WINDOW_SEC = 2.0
POLL_SEC = 1.0  # media-playlist poll cadence (~1 segment ahead of the player)
# Cap on untranscribed stale audio: after the live freezes/buffers, one poll
# returns the whole gap; transcribing it segment-by-segment keeps captions
# stuck on audio the player already live-synced past. Dropping the stale
# head (see LiveCaptioner._trim_stale_head) bounds the transcript's lag —
# the realtime-transcript contract. ~10s covers a normal hiccup and still
# feels live; the player's own live-sync seek skips the same audio.
_MAX_BACKLOG_SEC = 10.0
_FLUSH_FAIL_LIMIT = 3  # consecutive ASR flush failures -> offline (never a silent dead stream)
_MASTER_TTL_SEC = 300.0  # re-resolve the live master (fresh usher tokens)
_MASTER_403_TTL_SEC = 60.0  # ...or sooner after a 403 (expired token)
_OFFLINE_STRIKES = 3  # consecutive offline resolutions -> offline event + stop
_BACKOFF_INITIAL_SEC = 1.0
_BACKOFF_MAX_SEC = 15.0
_QUEUE_MAX = 8  # bounded subscriber queue — slow consumers drop blocks
_HTTP_TIMEOUT = 10.0
_SEGMENT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


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
    for block in master_text.split("#EXT-X-STREAM-INF")[1:]:
        for line in block.splitlines()[1:]:
            stripped = line.strip()
            if stripped:
                return urljoin(master_url, stripped)
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
    if host == "ttvnw.net" or host.endswith(".ttvnw.net"):
        hdrs = {k: v for k, v in hdrs.items() if k.lower() != "origin"}
    hdrs.setdefault("User-Agent", _SEGMENT_UA)
    resp = requests.get(url, headers=hdrs, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def _decode_audio_bytes(data: bytes) -> "Any":
    """Decode one HLS segment (TS / fMP4 / ADTS AAC) to mono 16 kHz float32.

    Same output contract as ``archive_transcribe.decode_audio`` (16k mono
    float32 numpy array) but feeds the raw bytes through ffmpeg's stdin — no
    temp files, one subprocess per segment (~100ms).
    """
    import numpy as np

    from services.ytdlp_ffmpeg import _resolve_ffmpeg_exe

    cmd = [
        _resolve_ffmpeg_exe(), "-v", "error", "-i", "pipe:0",
        "-f", "f32le", "-ac", "1", "-ar", "16000", "-",
    ]
    proc = sp.run(cmd, input=data, capture_output=True, timeout=60)
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace")[-300:]
        raise RuntimeError(f"ffmpeg segment decode failed: {stderr}")
    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    return samples.copy()  # writable copy, mirrors archive_transcribe.decode_audio


def _resolve_live_master(platform: str, channel: str) -> Optional[dict]:
    """Resolve the live master playlist — reuses the SAME resolver the
    ``/api/live/{platform}`` endpoint uses (services.live_capture), so auth /
    token / ad-rotation logic stays in one place. Twitch goes through the
    fast GQL+usher ``probe_twitch_live_master`` first (~1-3s, per-channel 60s
    cache) instead of the yt-dlp page extract (3-15s) — the captioner must
    not sit on the popup's open path. Returns ``{url, headers}`` or None
    when the channel is offline / unreachable."""
    from services.live_capture import kick_live_info, probe_twitch_live_master

    if platform == "twitch":
        probed = probe_twitch_live_master(channel)
        if probed and probed.get("url"):
            return {"url": probed["url"], "headers": probed.get("headers") or {}}
        # ponytail: the usher probe failed (GQL token / usher unreachable) —
        # fall back to the yt-dlp page extract, which routes around it at a
        # 3-15s cost. Upgrade path: fix the probe path, then drop yt-dlp
        # from this resolver entirely.
        from services.live_capture import twitch_live_info

        info = twitch_live_info(channel)
        if not info or not info.get("url"):
            return None
        return {"url": info["url"], "headers": info.get("headers") or {}}

    info = kick_live_info(channel)
    if not info or not info.get("url"):
        return None
    return {"url": info["url"], "headers": info.get("headers") or {}}


def _transcribe_window(audio: "Any", duration: float) -> str:
    """VAD-split one decoded window and transcribe the speech with parakeet.

    archive_transcribe is imported HERE, inside the worker thread (never on
    the asyncio loop): the import is heavy (torch/numpy) and the model lock
    must not be held during network I/O. Empty return = no speech (dead air /
    music) — the caller emits no caption block.
    """
    from services import archive_transcribe as at

    speech = at.vad_speech_seconds(audio)
    if not speech:
        return ""
    rec = at._parakeet_model()
    results = at._transcribe_batch_parakeet(rec, audio, speech, None)
    texts: list[str] = []
    for items, _lang in results:
        for item in items:
            text = (item.get("text") or "").strip()
            if text:
                texts.append(text)
    return " ".join(texts)


def _warm_asr() -> None:
    """Pre-load the parakeet engine + Silero VAD once per worker start so the
    FIRST flush is not a 2-6s cold model load. Runs in the captioner's worker
    thread (never on the SSE request path). Failure is non-fatal — the first
    flush retries the same calls through the normal path, and the flush
    failure counter still surfaces a persistently broken engine."""
    try:
        from services import archive_transcribe as at

        at._parakeet_model()
        at._get_vad()
    except Exception as exc:
        logger.debug(
            "live captions ASR pre-warm failed (first flush loads lazily): %s", exc
        )


def _resolve_evidence(platform: str, channel: str) -> Optional[str]:
    """Best-effort channel-language family (platform clue / transcript tally).

    None when unknown or the archive DB is unavailable — the per-window SLID
    gate then decides (see caption_translate). Never raises."""
    try:
        from services.channel_language import aggregate_channel_language

        return aggregate_channel_language(platform, channel).get("language")
    except Exception:
        return None


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
    captioner: "LiveCaptioner", text: str, audio: "Any"
) -> tuple[str, bool]:
    """Language-gated best-effort translation of one caption window.

    Target: the per-session ?lang= override (``captioner._target_family``)
    when set, else the app language. Gate: channel-language evidence first;
    without it, the SLID majority over recent speech windows (see
    caption_translate.needs_translation — the source-vs-target comparison
    makes an explicit target behave exactly like the app family). Any
    failure degrades to the raw ASR text — captions never block on
    translation. Returns (caption_text, translated)."""
    try:
        from services import caption_translate as ct

        if not ct.enabled() or ct.nllb_dir() is None:
            return text, False
        evidence = captioner._evidence_family
        if evidence is None and ct.slid_dir() is not None:
            fam = ct.detect_language(audio)
            if fam:
                captioner._lang_votes.append(fam)
        app = captioner._target_family or ct.app_language_family()
        if not ct.needs_translation(evidence, app, captioner._lang_votes):
            return text, False
        out = ct.translate(text, app)
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


def get_captioner(
    platform: str, channel: str, loop: asyncio.AbstractEventLoop
) -> "LiveCaptioner":
    """The shared captioner for (platform, channel), created on first use."""
    key = (platform, channel)
    with _REGISTRY_LOCK:
        c = _CAPTIONERS.get(key)
        if c is None:
            c = LiveCaptioner(platform, channel, loop)
            _CAPTIONERS[key] = c
        return c


def _unregister(captioner: "LiveCaptioner") -> None:
    key = (captioner.platform, captioner.channel)
    with _REGISTRY_LOCK:
        if _CAPTIONERS.get(key) is captioner:
            _CAPTIONERS.pop(key, None)


def captions_available(platform: str) -> tuple[bool, str]:
    """(available, reason) gate — captions need the parakeet MODEL files
    present locally (a live captioner must not trigger a multi-GB download
    mid-stream).

    Deliberately checks ONLY the model dir (stdlib file probe) and never
    imports sherpa_onnx in the API process: the +cuda wheel's onnxruntime
    DLL import can crash the HTTP listener natively on boxes where CUDA is
    broken (frozen-bundle repro: the first /available request killed the
    server). Engine importability is probed lazily in the worker thread at
    transcribe time; repeated flush failures surface as an 'offline' event
    instead of a silent keepalive stream. Runs off-loop via
    asyncio.to_thread."""
    plat = (platform or "").lower()
    if plat not in SUPPORTED_PLATFORMS:
        return False, "captions support twitch and kick only"
    try:
        from services import archive_transcribe as at
    except Exception as exc:  # pragma: no cover - env-specific
        return False, f"transcription engine unavailable: {exc}"
    if at._parakeet_resolve_dir() is None:
        return False, "parakeet model not downloaded yet — run a transcription job to fetch it"
    return True, ""


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
        # (event, data) tuples — consumed by the SSE generator. Bounded so a
        # slow subscriber drops blocks instead of stalling the worker.
        self.events: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self.window_sec = window_sec if window_sec is not None else _env_float(WINDOW_SEC_ENV, WINDOW_SEC)
        self.poll_sec = poll_sec if poll_sec is not None else _env_float(POLL_SEC_ENV, POLL_SEC)
        self.max_backlog_sec = (
            max_backlog_sec
            if max_backlog_sec is not None
            else _env_float(MAX_BACKLOG_SEC_ENV, _MAX_BACKLOG_SEC)
        )
        self._life_lock = threading.Lock()
        self._refcount = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
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
        # Translation state (worker-owned): channel-language evidence resolved
        # once at start; SLID detections for the last 5 speech windows feed
        # the majority gate when evidence is unknown (caption_translate).
        self._evidence_family: Optional[str] = None
        self._lang_votes: Deque[str] = deque(maxlen=5)
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
        default and None on an active session keeps the current target.
        The worker's session-active toggle stays keyed on the 0->1 refcount
        transition regardless of the lang arg."""
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
                self._evidence_family = None
                self._lang_votes = deque(maxlen=5)
                self._target_family = lang  # fresh session: explicit selection or app-language default
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._run,
                    daemon=True,
                    name=f"live-captions-{self.platform}-{self.channel}",
                )
                self._thread.start()
    def release(self) -> None:
        """Refcount-- — stops the worker thread when the last subscriber
        leaves. Idempotent-safe (refcount never goes negative)."""
        with self._life_lock:
            self._refcount = max(0, self._refcount - 1)
            if self._refcount == 0:
                self._stop.set()

    # --- worker ----------------------------------------------------------

    def _run(self) -> None:
        try:
            self._evidence_family = _resolve_evidence(self.platform, self.channel)
            _warm_asr()
            _warm_translate(self._evidence_family, self._target_family)
            self._run_loop()
        except Exception:
            logger.exception(
                "live captions worker crashed for %s/%s", self.platform, self.channel
            )
            self._emit("offline", {})
        finally:
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
                # Transient: playlist fetch error / expired token / decode —
                # back off and retry, keeping the loop (and the SSE) alive.
                logger.debug(
                    "live captions %s/%s transient error: %s",
                    self.platform, self.channel, exc,
                )
                backoff = min(backoff * 2, _BACKOFF_MAX_SEC)
                self._stop.wait(backoff)
                continue
            backoff = _BACKOFF_INITIAL_SEC
            self._offline_strikes = 0
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
            if _is_http_403(exc):
                # Expired usher token — re-resolve sooner than the TTL.
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
            if _is_http_403(exc):
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
        """Download, decode and buffer one segment; flush a caption when the
        window fills."""
        import numpy as np

        master = self._master
        data = _fetch(seg["uri"], master["headers"])
        samples = _decode_audio_bytes(data)
        if samples is None or len(samples) == 0:
            return
        dur = seg.get("dur") or (len(samples) / 16000.0)
        if self._origin is None and seg.get("pdt") is not None:
            # pdt is the segment's wall-clock start; stream time 0 = pdt
            # minus the duration already consumed before this segment.
            self._origin = seg["pdt"] - self._stream_sec
        if self._buffer is None:
            self._buffer = samples
        else:
            self._buffer = np.concatenate([self._buffer, samples])
        self._buffer_sec += dur
        self._stream_sec += dur
        if self._buffer_sec >= self.window_sec:
            self._flush()

    def _flush(self) -> None:
        """Transcribe the buffered window and emit one caption block.

        The window rolls either way: dead air / no speech emits nothing (a
        silent stretch must not accumulate forever), speech emits one block
        with the window's absolute stream time (PDT-anchored when the
        playlist carries PROGRAM-DATE-TIME, else stream-relative seconds)."""
        audio = self._buffer
        buffer_sec = self._buffer_sec
        win_start_off = self._stream_sec - buffer_sec
        self._buffer = None
        self._buffer_sec = 0.0
        try:
            text = _transcribe_window(audio, buffer_sec)
        except Exception as exc:
            # ASR failure (model load hiccup, VAD error) — drop the window,
            # keep rolling; but a persistently broken engine must not leave
            # the SSE alive with keepalives and NO captions forever: after
            # _FLUSH_FAIL_LIMIT consecutive failures, surface it as an
            # offline event so the frontend hides the overlay (never a
            # silent dead stream).
            self._flush_failures += 1
            logger.warning(
                "live captions %s/%s flush failed (%d/%d): %s",
                self.platform, self.channel, self._flush_failures,
                _FLUSH_FAIL_LIMIT, exc,
            )
            if self._flush_failures >= _FLUSH_FAIL_LIMIT:
                logger.error(
                    "live captions %s/%s disabled — %d consecutive ASR failures: %s",
                    self.platform, self.channel, self._flush_failures, exc,
                )
                self._emit("offline", {"reason": f"asr failure: {exc}"})
                self._stop.set()
            return
        self._flush_failures = 0
        if not text:
            return
        text, translated = _maybe_translate(self, text, audio)
        if not text:
            return
        logger.info(
            "live captions %s/%s transcribed %.1fs window via parakeet%s: %s",
            self.platform, self.channel, buffer_sec,
            " + translate" if translated else "", text[:80],
        )
        start = win_start_off + (self._origin or 0.0)
        end = start + buffer_sec
        payload = {
            "text": text,
            "start": round(start, 3),
            "end": round(end, 3),
        }
        if translated:
            payload["translated"] = True
        if self._origin is not None:
            # Wall-clock pipeline latency: ms since the window's audio
            # completed. The frontend anchors captions to the VIDEO clock,
            # so the visible lag is just this trail. Stream-relative times
            # (no PDT anchor) cannot measure wall latency — key omitted.
            payload["latency_ms"] = round((time.time() - end) * 1000)
        self._emit("caption", payload)

    def _emit(self, event: str, data: dict) -> None:
        def _put(q: asyncio.Queue, ev: str, payload: dict) -> None:
            try:
                q.put_nowait((ev, payload))
            except asyncio.QueueFull:
                pass  # slow subscriber — drop rather than stall the worker

        self.loop.call_soon_threadsafe(_put, self.events, event, data)


# --- helpers -----------------------------------------------------------------

def _is_http_403(exc: BaseException) -> bool:
    resp = getattr(exc, "response", None)
    return resp is not None and getattr(resp, "status_code", None) == 403


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.environ.get(name, "") or default))
    except ValueError:
        return default
