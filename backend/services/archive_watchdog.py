"""Archive chat watchdog — captures live chat into the local archive.

A daemon thread polls saved channels every ``POLL_INTERVAL_SEC`` against the
SAME live-status source the live router uses (``routers.live`` — no
re-implemented Twitch/Kick/YouTube checks). When a channel goes live it
starts the matching chat sink (twitch_irc / kick_pusher / yt_live); when the
stream ends (or the sink disconnects) it stops the sink and closes the video
row with ended_at/duration.

Each capture creates a ``videos`` row (status 'known' — chat captured, VOD
ingest is a separate job) whose ``canonical_key`` follows the shared
cross-platform dedupe contract (each sink module carries the identical
``_canonical_key`` helper).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, List, Optional

from services.chat_sinks import kick_pusher, twitch_irc, yt_live
from services.chat_sinks.base import ChatSink

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 30.0
FLUSH_INTERVAL_SEC = 5.0
FLUSH_MAX_ROWS = 100
RESTART_COOLDOWN_SEC = 60.0

_PLATFORMS = ("twitch", "kick", "youtube")
_SLUG_KEY = {"twitch": "twitchSlug", "kick": "kickSlug", "youtube": "youtubeSlug"}
_SINK_MODULES = {"twitch": twitch_irc, "kick": kick_pusher, "youtube": yt_live}


class Capture:
    __slots__ = ("channel_id", "platform", "video_id", "channel", "title",
                 "started_at_ts", "canonical_key", "sink")

    def __init__(self, channel_id: str, platform: str, video_id: str, channel: str,
                 title: str, started_at_ts: float, canonical_key: str, sink: ChatSink):
        self.channel_id = channel_id
        self.platform = platform
        self.video_id = video_id
        self.channel = channel
        self.title = title
        self.started_at_ts = started_at_ts
        self.canonical_key = canonical_key
        self.sink = sink


_lock = threading.Lock()
_captures: dict[str, Capture] = {}     # key: f"{channel_id}:{platform}"
_last_start: dict[str, float] = {}     # key → monotonic() of last sink start
_stop = threading.Event()
_thread: Optional[threading.Thread] = None


# ---------------------------------------------------------------------------
# Default data sources (overridable in tests)
# ---------------------------------------------------------------------------

def _default_channels() -> list:
    from deps import settings_mgr

    return settings_mgr.get().saved_channels or []


def _poll_live(channel: dict) -> list:
    """Live entries for one saved channel — the live router's own poller.

    ``routers.live._fetch_channel_live_payload`` reads kick_live_info /
    twitch_live_info / youtube_live_info (the same source as
    /api/channels/{id}/live), so the watchdog never re-implements platform
    checks. Entries are normalized to lowercase platform keys.
    """
    from routers.live import _fetch_channel_live_payload

    payload = _fetch_channel_live_payload(channel)
    out: list = []
    for ent in payload.get("live") or []:
        plat = (ent.get("platform") or "").lower()
        if plat not in _PLATFORMS:
            continue
        out.append({
            "platform": plat,
            "title": ent.get("title") or "",
            "url": ent.get("url") or "",
            "started_at": ent.get("started_at"),
            # Real YouTube videoId when extraction found one — stored as the
            # capture's video_id so archive rows link to the actual video.
            "videoId": ent.get("videoId"),
        })
    return out


def _default_sink_factory(platform: str, channel: dict, entry: dict,
                          video_id: str, start_ms: float) -> ChatSink:
    from services.chat_sinks import SINKS

    cls = SINKS[platform]
    slug = _slug_for(channel, platform)
    kwargs = dict(video_id=video_id, channel=slug,
                  title=entry.get("title") or "", stream_start_ts=start_ms)
    if platform == "twitch":
        return cls(login=slug, **kwargs)
    if platform == "kick":
        return cls(slug=slug, **kwargs)
    return cls(handle=slug, **kwargs)


# ---------------------------------------------------------------------------
# Watchdog lifecycle
# ---------------------------------------------------------------------------

def start_archive_watchdog(*, poll: Optional[Callable] = None,
                           sink_factory: Optional[Callable] = None,
                           channels_provider: Optional[Callable] = None,
                           poll_interval: float = POLL_INTERVAL_SEC,
                           restart_cooldown: float = RESTART_COOLDOWN_SEC,
                           ) -> threading.Thread:
    """Start the watchdog daemon thread (idempotent). Injectable poll /
    sink_factory / channels_provider for tests."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        _stop.clear()
        _captures.clear()
        _last_start.clear()
        t = threading.Thread(
            target=_run_loop,
            args=(channels_provider or _default_channels,
                  poll or _poll_live,
                  sink_factory or _default_sink_factory,
                  float(poll_interval), float(restart_cooldown)),
            daemon=True, name="archive-watchdog",
        )
        _thread = t
        t.start()
        return t


def stop_archive_watchdog(timeout: float = 10.0) -> None:
    """Stop the watchdog and every running sink (flushing remaining rows)."""
    global _thread
    _stop.set()
    with _lock:
        caps = list(_captures.values())
        _captures.clear()
    for cap in caps:
        _stop_capture(cap, reason="watchdog shutdown")
    t, _thread = _thread, None
    if t is not None:
        t.join(timeout=timeout)


def active_captures() -> List[Capture]:
    with _lock:
        return list(_captures.values())


# ---------------------------------------------------------------------------
# Poll loop + reconcile
# ---------------------------------------------------------------------------

def _run_loop(channels_provider, poll, sink_factory, poll_interval, restart_cooldown) -> None:
    while not _stop.is_set():
        try:
            channels = channels_provider()
        except Exception as exc:
            logger.debug("archive watchdog: channels unavailable: %s", exc)
            channels = []
        for ch in channels:
            if _stop.is_set():
                break
            if not isinstance(ch, dict):
                continue
            try:
                _reconcile(ch, poll, sink_factory, restart_cooldown)
            except Exception as exc:
                logger.debug("archive watchdog: reconcile failed for %s: %s",
                             ch.get("id"), exc)
        _stop.wait(poll_interval)


def _reconcile(channel: dict, poll, sink_factory, restart_cooldown: float) -> None:
    ch_id = str(channel.get("id") or "")
    if not ch_id:
        return
    live = {e["platform"]: e for e in poll(channel)}
    with _lock:
        mine = {k: c for k, c in _captures.items() if c.channel_id == ch_id}
    for key, cap in mine.items():
        plat = key.split(":", 1)[1]
        if plat in live and cap.sink.is_alive():
            continue
        if not cap.sink.is_alive():
            _stop_capture(cap, reason=cap.sink.disconnect_reason or "sink died")
        elif plat not in live:
            _stop_capture(cap, reason="stream ended")
    for plat, entry in live.items():
        key = f"{ch_id}:{plat}"
        with _lock:
            if key in _captures:
                continue
            cooldown_left = _last_start.get(key, 0.0) + restart_cooldown - time.monotonic()
        if cooldown_left > 0:
            logger.debug("archive watchdog: %s restart cooldown %.0fs", key, cooldown_left)
            continue
        _start_capture(channel, plat, entry, sink_factory)


def _start_capture(channel: dict, platform: str, entry: dict, sink_factory) -> None:
    ch_id = str(channel.get("id") or "")
    slug = _slug_for(channel, platform)
    start_ms = _to_epoch_ms(entry.get("started_at")) or (time.time() * 1000.0)
    # Real YouTube videoId when extraction found one (the archive row then
    # links to the actual video); synthetic fallback keeps the stable
    # f"{platform}-live-{slug}-{ms}" key shape when no id could be extracted.
    video_id = entry.get("videoId") or f"{platform}-live-{slug}-{int(start_ms)}"
    title = entry.get("title") or slug
    canonical_key = _SINK_MODULES[platform]._canonical_key(title, entry.get("started_at"))
    sink = sink_factory(platform, channel, entry, video_id, start_ms)
    cap = Capture(ch_id, platform, video_id, slug, title, start_ms, canonical_key, sink)
    key = f"{ch_id}:{platform}"
    with _lock:
        if key in _captures:
            return
        _captures[key] = cap
        _last_start[key] = time.monotonic()
    try:
        from services import archive_db

        archive_db.upsert_video({
            "platform": platform,
            "video_id": video_id,
            "channel": slug,
            "title": title,
            "started_at": _iso_from_epoch_ms(start_ms),
            "status": "known",
            "canonical_key": canonical_key,
        })
    except Exception as exc:
        logger.debug("archive watchdog: video row failed for %s: %s", video_id, exc)
    sink.start()
    logger.info("archive chat capture started: %s %s (%s)", platform, video_id, title)


def _stop_capture(cap: Capture, *, reason: str) -> None:
    key = f"{cap.channel_id}:{cap.platform}"
    with _lock:
        _captures.pop(key, None)
    try:
        cap.sink.stop()
    except Exception as exc:
        logger.debug("archive watchdog: sink stop failed for %s: %s", cap.video_id, exc)
    try:
        from services import archive_db

        archive_db.upsert_video({
            "platform": cap.platform,
            "video_id": cap.video_id,
            "channel": cap.channel,
            "title": cap.title,
            "started_at": _iso_from_epoch_ms(cap.started_at_ts),
            "ended_at": _iso_from_epoch_ms(time.time() * 1000.0),
            "duration_sec": max((time.time() * 1000.0 - cap.started_at_ts) / 1000.0, 0.0),
            "status": "known",
            "canonical_key": cap.canonical_key,
        })
    except Exception as exc:
        logger.debug("archive watchdog: end row failed for %s: %s", cap.video_id, exc)
    logger.info("archive chat capture stopped: %s %s (%s)", cap.platform, cap.video_id, reason)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slug_for(channel: dict, platform: str) -> str:
    slug = (channel.get(_SLUG_KEY[platform]) or "").strip()
    if not slug:
        slug = (channel.get("displayName") or channel.get("name")
                or str(channel.get("id") or "unknown"))
    return slug


def _to_epoch_ms(value) -> Optional[float]:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        return ts if ts > 10_000_000_000 else ts * 1000.0  # epoch s → ms
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


def _iso_from_epoch_ms(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat(timespec="seconds")


# Module self-check: helpers + canonical_key consistency across sink modules.
_EPOCH_MS = _to_epoch_ms("2026-08-01T22:30:00Z")
assert _EPOCH_MS is not None and abs(_EPOCH_MS - 1785623400000.0) < 1.0
assert abs(_to_epoch_ms(1785623400) - 1785623400000.0) < 1.0
assert _to_epoch_ms(None) is None
assert _to_epoch_ms("garbage") is None
assert _iso_from_epoch_ms(1785623400000.0).startswith("2026-08-01T22:30:00")
assert _slug_for({"twitchSlug": " lubumr "}, "twitch") == "lubumr"
assert _slug_for({"kickSlug": "lubu"}, "kick") == "lubu"
assert _slug_for({"youtubeSlug": "@x"}, "youtube") == "@x"
_expected_key = "ultimo-dia-do-mundial|2026-08-01"
assert twitch_irc._canonical_key("Último dia do Mundial!", "2026-08-01T22:30:00Z") == _expected_key
assert kick_pusher._canonical_key("Último dia do Mundial!", "2026-08-01T22:30:00Z") == _expected_key
assert yt_live._canonical_key("Último dia do Mundial!", "2026-08-01T22:30:00Z") == _expected_key
