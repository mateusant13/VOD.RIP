"""Proactive archive scheduler — keeps every saved channel's history indexed.

Contrast with the search-driven lazy enrichment in routers/archive.py
(_maybe_enrich): this daemon works continuously from boot AND on channel
add, for EVERY saved channel, without waiting for a search to trigger it.

Each pass (immediately at startup, then every PASS_INTERVAL_SEC, plus on
kick_scheduler_pass()):
  1. Twitch  — ingest the latest VOD metadata (GQL, cheap, dedupe-aware).
  2. Kick    — ingest the latest VOD metadata. Kick has NO retro chat API
               (live capture only — see chat_sinks/kick_pusher docstring),
               so this leg is metadata + whatever live capture already
               collected; there is no chat to backfill.
  3. YouTube — ingest metadata + auto-captions (subtitles) + best-effort
               live-chat replay for every saved vod/clip URL, bounded per
               pass and with a 1h retry backoff behind the bot wall.
  4. Chat backfill — two legs: Twitch (the only platform with a retro chat
               API) fills chat for every chat-less saved-channel VOD, oldest
               first, <= TWITCH_BACKFILL_MAX_INFLIGHT concurrent; YouTube
               retro-fetches live-chat replay for chat-less streams whose
               historical ingest crash archived captions but zero chat
               (chat-only, no caption re-fetch). Each run is incremental
               (seeds from the deepest stored offset) and completes at the
               tail of Twitch's replay window.
  5. Transcribe queue — top up whisper jobs at TRANSCRIBE_PRIORITY_LOW for
               downloaded YouTube files with no captions/transcripts.
               A transcript-source search re-enqueues at
               TRANSCRIBE_PRIORITY_HIGH (routers/archive.py) and the
               worker's ORDER BY priority DESC makes those jump the queue.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from services import archive_db

logger = logging.getLogger(__name__)

PASS_INTERVAL_SEC = 180.0
TWITCH_INGEST_LIMIT = 100            # GQL page cap for list_channel_videos
KICK_INGEST_LIMIT = 50
YOUTUBE_INGEST_PER_PASS = 3          # yt-dlp extracts are slow + bot-gated
TWITCH_BACKFILL_MAX_INFLIGHT = 2     # GQL 429-backoff is per-IP: 4 parallel
                                     # page fetches tripped the limiter and
                                     # collapsed throughput vs 2 (measured)
TRANSCRIBE_QUEUE_PER_PASS = 2
BACKFILL_MAX_MESSAGES = 100_000      # same ceiling as the search kick
TRANSCRIBE_PRIORITY_LOW = 0
TRANSCRIBE_PRIORITY_HIGH = 100       # transcript-source search jump-the-queue
YOUTUBE_RETRY_BACKOFF_S = 3600.0     # bot-wall retry delay per video
FAILED_JOB_FRESH_S = 3600.0          # don't re-run a job failed < 1h ago
STALE_JOB_MIN = 30                   # queued/running older than this = dead executor

_VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")


def _video_id_from_url(url: str) -> str:
    m = re.search(r"[?&]v=([\w-]{6,})", url)
    if not m:
        m = re.search(r"youtu\.be/([\w-]{6,})", url)
    return m.group(1) if m else ""


# --- daemon state -----------------------------------------------------------

_stop = threading.Event()
_wake = threading.Event()
_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_backfill_lock = threading.Lock()
_backfill_inflight: set[str] = set()
_yt_lock = threading.Lock()
_yt_inflight: set[str] = set()  # video_ids currently being ingested
_yt_attempted_at: dict[str, float] = {}  # video_id -> monotonic() last attempt


# --- pass legs --------------------------------------------------------------

def _channels() -> list:
    from deps import settings_mgr

    return settings_mgr.get().saved_channels or []


def _platform_enabled(channel: dict, platform: str) -> bool:
    slug_key = {"twitch": "twitchSlug", "kick": "kickSlug", "youtube": "youtubeSlug"}[platform]
    if not (channel.get(slug_key) or "").strip():
        return False
    flag = {
        "twitch": "channel_twitch_enabled",
        "kick": "channel_kick_enabled",
        "youtube": "channel_youtube_enabled",
    }[platform]
    try:
        from deps import settings_mgr

        return bool(getattr(settings_mgr.get(), flag, True))
    except Exception:
        return True


def _ingest_twitch(channel: dict) -> None:
    if not _platform_enabled(channel, "twitch"):
        return
    slug = (channel.get("twitchSlug") or "").strip().lower()
    if not slug:
        return
    try:
        from services.archive_twitch import ingest_channel_vods

        rows = ingest_channel_vods(slug, limit=TWITCH_INGEST_LIMIT)
        logger.info("scheduler twitch ingest %s: %d VOD(s) upserted", slug, len(rows))
    except Exception as exc:  # noqa: BLE001 — GQL 429 / dead channel
        logger.info("scheduler twitch ingest %s failed: %s", slug, exc)


def _ingest_kick(channel: dict) -> None:
    if not _platform_enabled(channel, "kick"):
        return
    slug = (channel.get("kickSlug") or "").strip().lower()
    if not slug:
        return
    try:
        from services.archive_kick import ingest_channel

        rows = ingest_channel(slug, limit=KICK_INGEST_LIMIT, download=False)
        logger.info("scheduler kick ingest %s: %d VOD(s) upserted", slug, len(rows))
    except Exception as exc:  # noqa: BLE001 — Kick API 429 / dead channel
        logger.info("scheduler kick ingest %s failed: %s", slug, exc)


def _youtube_covered(video_id: str) -> bool:
    """True when the video is already ingested WITH captions/transcripts.

    A bare row (no captions, no transcripts) is re-ingested to retry the
    caption fetch; a captions-covered row is left alone."""
    rows = archive_db.query(
        "SELECT 1 FROM videos WHERE platform='youtube' AND video_id=?", (video_id,)
    )
    if not rows:
        return False
    if archive_db.captions_cover("youtube", video_id):
        return True
    return bool(
        archive_db.query(
            "SELECT 1 FROM transcripts WHERE platform='youtube' AND video_id=? LIMIT 1",
            (video_id,),
        )
    )


def _entry_url(item: Any) -> str:
    """Saved-channel vod/clip list entries are dicts ({'id','url',...}) or
    plain URL strings — normalize to the URL (fallback: the bare id)."""
    if isinstance(item, dict):
        return str(item.get("url") or item.get("id") or "")
    return str(item or "")


def _ingest_one_youtube(video_id: str) -> None:
    try:
        from services.archive_ytdlp import ingest_video

        report = ingest_video(video_id)
        logger.info(
            "scheduler yt ingest %s: %d caption segment(s), %d chat message(s)",
            video_id,
            report.get("transcript_segments", 0),
            report.get("chat_messages", 0),
        )
    except Exception as exc:  # noqa: BLE001 — bot wall / dead video
        logger.info("scheduler yt ingest %s failed (retry in 1h): %s", video_id, exc)
    finally:
        with _yt_lock:
            _yt_inflight.discard(video_id)


def _ingest_youtube(channel: dict) -> None:
    if not _platform_enabled(channel, "youtube"):
        return
    with _yt_lock:
        if len(_yt_inflight) >= YOUTUBE_INGEST_PER_PASS:
            return  # budget full — a later pass picks the rest
    urls = list(channel.get("vodVideos") or []) + list(channel.get("clipVideos") or [])
    if not urls:
        return
    spawned = 0
    for item in urls:
        if spawned >= YOUTUBE_INGEST_PER_PASS:
            break
        url = _entry_url(item)
        if not url or "/shorts/" in url:
            continue  # shorts have no captions/chat to archive
        vid = _video_id_from_url(url)
        if not _VIDEO_ID_RE.fullmatch(vid):
            continue
        if _youtube_covered(vid):
            continue
        now = time.monotonic()
        if now - _yt_attempted_at.get(vid, 0.0) < YOUTUBE_RETRY_BACKOFF_S:
            continue
        with _yt_lock:
            if vid in _yt_inflight:
                continue
            _yt_inflight.add(vid)
        _yt_attempted_at[vid] = now
        threading.Thread(
            target=_ingest_one_youtube, args=(vid,), daemon=True
        ).start()
        spawned += 1


def _backfill_one(video_id: str, channel: str) -> None:
    try:
        from services.archive_twitch import backfill_chat

        result = backfill_chat(channel, video_id, max_messages=BACKFILL_MAX_MESSAGES)
        logger.info(
            "scheduler twitch backfill %s: %d message(s) in %d page(s) (%s)",
            video_id,
            result.get("inserted", 0),
            result.get("pages", 0),
            result.get("stopped"),
        )
    except Exception:  # noqa: BLE001
        logger.info("scheduler twitch backfill %s failed", video_id, exc_info=True)
    finally:
        with _backfill_lock:
            _backfill_inflight.discard(video_id)
        # Bulk chat inserts fragment the FTS b-tree; merge so searches
        # stay fast (same as the router does after every backfill).
        try:
            archive_db.optimize_fts()
        except Exception:  # noqa: BLE001
            logger.debug("fts optimize failed after backfill", exc_info=True)


def _backfill_twitch_chat(channels: list) -> None:
    slugs = {str(c.get("twitchSlug") or "").strip().lower() for c in channels}
    slugs.discard("")
    if not slugs:
        return
    with _backfill_lock:
        free = TWITCH_BACKFILL_MAX_INFLIGHT - len(_backfill_inflight)
    if free <= 0:
        return
    # Candidates = twitch VODs without a completed backfill. Videos with a
    # failed job are candidates again after FAILED_JOB_FRESH_S (the loop
    # below gates it): backfill_chat is incremental (seeds from the deepest
    # stored offset), so a mid-fetch 'service error' leaves partial chat
    # that the re-run completes instead of skipping forever.
    ph = ",".join("?" * len(slugs))
    rows = list(
        archive_db.query(
            """SELECT v.video_id, v.channel, v.started_at FROM videos v
               WHERE v.platform='twitch'
                 AND v.video_id GLOB '[0-9]*'
                 AND lower(v.channel) IN (%s)
                 AND NOT EXISTS (SELECT 1 FROM video_aliases a
                                 WHERE a.platform='twitch' AND a.video_id=v.video_id)
                 AND NOT EXISTS (SELECT 1 FROM archive_jobs j
                                 WHERE j.kind='chat_backfill' AND j.platform='twitch'
                                   AND j.video_id=v.video_id AND j.status='done')
               ORDER BY v.started_at ASC"""
            % ph,
            tuple(slugs),
        )
    )
    now_utc = datetime.now(timezone.utc)
    fresh_cutoff = now_utc - timedelta(seconds=FAILED_JOB_FRESH_S)
    stale_cutoff = now_utc - timedelta(minutes=STALE_JOB_MIN)
    spawned = 0
    for r in rows:
        if spawned >= free:
            break
        vid = r["video_id"]
        latest = archive_db.latest_job("twitch", vid, kind="chat_backfill")
        if latest:
            if latest["status"] in ("queued", "running"):
                # Fresh = an executor is on it; stale = dead executor, re-run.
                try:
                    if datetime.fromisoformat(latest["updated_at"]) > stale_cutoff:
                        continue
                except (TypeError, ValueError):
                    continue  # unparseable — assume in flight
            elif latest["status"] == "failed":
                try:
                    if datetime.fromisoformat(latest["updated_at"]) > fresh_cutoff:
                        continue  # failed < 1h ago — don't hammer
                except (TypeError, ValueError):
                    continue  # unparseable — treat as fresh failure
        with _backfill_lock:
            if vid in _backfill_inflight:
                continue
            _backfill_inflight.add(vid)
        threading.Thread(
            target=_backfill_one, args=(vid, r["channel"]), daemon=True
        ).start()
        spawned += 1


def _backfill_one_youtube(video_id: str) -> None:
    job_id = f"yt-chat-backfill-{video_id}-{int(time.time())}"
    try:
        from services.archive_ytdlp import backfill_live_chat

        archive_db.enqueue_job(job_id, "chat_backfill", "youtube", video_id, priority=0)
        archive_db.update_job(job_id, status="running")
        result = backfill_live_chat(video_id)
        archive_db.update_job(job_id, status="done", progress=1.0)
        logger.info(
            "scheduler yt chat backfill %s: %d message(s) (%s)",
            video_id, result.get("chat_messages", 0), result.get("chat"),
        )
    except Exception as exc:  # noqa: BLE001 — bot wall / dead video
        logger.info("scheduler yt chat backfill %s failed: %s", video_id, exc)
        try:
            archive_db.update_job(job_id, status="failed", error=str(exc)[:500])
        except Exception:  # noqa: BLE001 — bookkeeping must not mask the real error
            logger.debug("job status update failed for %s", video_id, exc_info=True)
    finally:
        with _backfill_lock:
            _backfill_inflight.discard(video_id)
        # Bulk chat inserts fragment the FTS b-tree; merge so searches
        # stay fast (same as the router does after every backfill).
        try:
            archive_db.optimize_fts()
        except Exception:  # noqa: BLE001
            logger.debug("fts optimize failed after backfill", exc_info=True)


def _backfill_youtube_chat() -> None:
    """Retro chat backfill for chat-less YouTube streams (leg 4b).

    The historical live-chat ingest crash (authorNameTextColor as a raw
    packed-ARGB int) archived captions but ZERO chat rows for streams; the
    covered-skip in _ingest_youtube then froze them forever (chat-less
    streams with captions never re-ingest). This leg refetches live-chat
    replay for exactly those videos: kind='stream' (was_live), no chat
    rows, no completed backfill job. Chat-only (no caption re-fetch) to
    avoid extra YouTube API pressure. 'none' chat (stream with replay
    disabled) records a done job so it is not retried forever.
    """
    with _backfill_lock:
        free = TWITCH_BACKFILL_MAX_INFLIGHT - len(_backfill_inflight)
    if free <= 0:
        return
    rows = list(
        archive_db.query(
            """SELECT video_id FROM videos
               WHERE platform='youtube'
                 AND kind='stream'
                 AND video_id NOT LIKE 'youtube-live-%'
                 AND NOT EXISTS (SELECT 1 FROM messages m
                                 WHERE m.platform='youtube' AND m.video_id=videos.video_id)
                 AND NOT EXISTS (SELECT 1 FROM archive_jobs j
                                 WHERE j.kind='chat_backfill' AND j.platform='youtube'
                                   AND j.video_id=videos.video_id AND j.status='done')
               ORDER BY started_at ASC"""
        )
    )
    now_utc = datetime.now(timezone.utc)
    fresh_cutoff = now_utc - timedelta(seconds=FAILED_JOB_FRESH_S)
    stale_cutoff = now_utc - timedelta(minutes=STALE_JOB_MIN)
    spawned = 0
    for r in rows:
        if spawned >= free:
            break
        vid = r["video_id"]
        latest = archive_db.latest_job("youtube", vid, kind="chat_backfill")
        if latest:
            if latest["status"] in ("queued", "running"):
                # Fresh = an executor is on it; stale = dead executor, re-run.
                try:
                    if datetime.fromisoformat(latest["updated_at"]) > stale_cutoff:
                        continue
                except (TypeError, ValueError):
                    continue  # unparseable — assume in flight
            elif latest["status"] == "failed":
                try:
                    if datetime.fromisoformat(latest["updated_at"]) > fresh_cutoff:
                        continue  # failed < 1h ago — don't hammer
                except (TypeError, ValueError):
                    continue  # unparseable — treat as fresh failure
        with _backfill_lock:
            if vid in _backfill_inflight:
                continue
            _backfill_inflight.add(vid)
        threading.Thread(
            target=_backfill_one_youtube, args=(vid,), daemon=True
        ).start()
        spawned += 1


def _enqueue_transcriptions() -> None:
    rows = list(
        archive_db.query(
            """SELECT platform, video_id, channel, title, duration_sec, archive_path
               FROM videos
               WHERE platform='youtube' AND status='ready'
                 AND archive_path IS NOT NULL AND archive_path != ''
                 AND NOT EXISTS (SELECT 1 FROM transcripts t
                                 WHERE t.platform=videos.platform
                                   AND t.video_id=videos.video_id)
               ORDER BY duration_sec ASC LIMIT 50"""
        )
    )
    now_utc = datetime.now(timezone.utc)
    fresh_cutoff = now_utc - timedelta(seconds=FAILED_JOB_FRESH_S)
    enqueued = 0
    for r in rows:
        if enqueued >= TRANSCRIBE_QUEUE_PER_PASS:
            break
        vid = r["video_id"]
        if not (r["archive_path"] or "").strip() or not Path(r["archive_path"]).is_file():
            continue  # file evicted — whisper would fail immediately
        if archive_db.captions_cover("youtube", vid):
            continue  # yt_subtitles_first: whisper skipped anyway
        latest = archive_db.latest_job("youtube", vid, kind="transcribe")
        if latest:
            if latest["status"] in ("queued", "running"):
                continue
            if latest["status"] == "failed":
                try:
                    if datetime.fromisoformat(latest["updated_at"]) > fresh_cutoff:
                        continue
                except (TypeError, ValueError):
                    continue  # unparseable — treat as fresh failure
        job_id = f"transcribe-youtube-{vid}"
        try:
            archive_db.enqueue_job(
                job_id, "transcribe", "youtube", vid, priority=TRANSCRIBE_PRIORITY_LOW
            )
        except sqlite3.IntegrityError:
            continue  # already queued by a search — nothing new
        enqueued += 1


def _run_pass() -> None:
    channels = _channels()
    if not channels:
        return
    for ch in channels:
        _ingest_twitch(ch)
        _ingest_kick(ch)
        _ingest_youtube(ch)
    _backfill_twitch_chat(channels)
    _backfill_youtube_chat()
    _enqueue_transcriptions()


def _loop(*, interval: float = PASS_INTERVAL_SEC) -> None:
    while not _stop.is_set():
        try:
            _run_pass()
        except Exception:  # noqa: BLE001 — a pass must never kill the daemon
            logger.exception("archive scheduler pass failed")
        if _stop.is_set():
            break
        _wake.wait(interval)  # returns immediately on kick_scheduler_pass()
        _wake.clear()


# --- lifecycle --------------------------------------------------------------

def start_archive_scheduler(*, interval: float = PASS_INTERVAL_SEC) -> threading.Thread:
    """Start the scheduler daemon thread (idempotent). The first pass runs
    immediately; afterwards one pass per *interval* (or on kick)."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        _stop.clear()
        _wake.clear()
        _thread = threading.Thread(
            target=_loop, kwargs={"interval": interval}, daemon=True,
            name="archive-scheduler",
        )
        _thread.start()
        return _thread


def stop_archive_scheduler(timeout: float = 10.0) -> None:
    """Stop the daemon. In-flight backfill threads finish on their own."""
    global _thread
    _stop.set()
    _wake.set()
    t, _thread = _thread, None
    if t is not None:
        t.join(timeout=timeout)


def kick_scheduler_pass() -> None:
    """Wake the daemon so a fresh pass starts now (e.g. right after a
    channel is added). No-op when the daemon is not running."""
    _wake.set()


# --- self-check (pure logic; no network, no DB) -----------------------------

assert _video_id_from_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1") == "dQw4w9WgXcQ"
assert _video_id_from_url("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
assert _VIDEO_ID_RE.fullmatch("dQw4w9WgXcQ")
assert not _VIDEO_ID_RE.fullmatch("dQw4w9WgX")
