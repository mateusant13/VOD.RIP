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
  3. YouTube — ingest metadata + auto-captions (subtitles) for every saved
               vod/clip URL, bounded per pass and with a 1h retry backoff
               behind the bot wall; chat history is enqueued as a kind='chat'
               job (the archive worker fetches it, never inline).
  4. Chat backfill — two legs that ENQUEUE kind='chat' jobs for the archive
               worker: Twitch (the only platform with a retro chat API) for
               every chat-less saved-channel VOD, oldest first; YouTube
               chat-only live-chat replay re-fetch for chat-less streams
               whose historical ingest crash archived captions but zero
               chat. Each fetch is incremental (seeds from the deepest
               stored offset); Twitch backfills self-cap at 2 concurrent
               inside backfill_chat (per-IP GQL 429 limiter, measured).
  5. Transcribe queue — top up ASR jobs at TRANSCRIBE_PRIORITY_LOW for
               Twitch/Kick VODs without transcripts (ready rows with a local
               file AND metadata-only rows — Twitch ingest is metadata-only,
               so the audio is downloaded at transcribe time) and for
               YouTube videos whose captions are permanently unavailable
               (captions_unavailable_at marker). Stale-failed jobs (older
               than FAILED_JOB_FRESH_S) are requeued IN PLACE first, in a
               pass OUTSIDE the duration window, so a long VOD's job is
               never starved past the LIMIT-50 candidate slice; ready rows
               whose archive file was evicted are enqueued (the worker
               downloads at job time) instead of skipped forever. A
               transcript-source search re-enqueues at
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
from typing import Any, Optional

from services import archive_db

logger = logging.getLogger(__name__)

PASS_INTERVAL_SEC = 180.0
TWITCH_INGEST_LIMIT = 100            # GQL page cap for list_channel_videos
KICK_INGEST_LIMIT = 50
YOUTUBE_INGEST_PER_PASS = 3          # yt-dlp extracts are slow + bot-gated
TRANSCRIBE_QUEUE_PER_PASS = 2
BACKFILL_MAX_MESSAGES = 100_000      # chat-backfill ceiling (worker + search kick)
# ponytail: 100k rows is the platform ceiling for one sweep (a 12 h dense
# VOD ~= 60-100k rows). The sweep is incremental (seeds from MAX(offset_sec)),
# so a capped run self-heals on the next resume instead of losing chat;
# upgrade path: persist a per-video cursor (offset_sec) with the job row if
# a single VOD ever exceeds the cap.
TRANSCRIBE_PRIORITY_LOW = 0
TRANSCRIBE_PRIORITY_HIGH = 100       # transcript-source search jump-the-queue
YOUTUBE_RETRY_BACKOFF_S = 3600.0     # bot-wall retry delay per video
FAILED_JOB_FRESH_S = 3600.0          # don't re-run a job failed < 1h ago
# Re-fetch window for the per-pass Twitch GQL channel walk: a channel whose
# last successful ingest is fresher than this is skipped for the pass (the
# channel_snapshots table — the same table routers/channels.py consults, same
# contract: touch after a successful non-empty fetch, skip while fresh).
TWITCH_CHANNEL_FRESH_SEC = 600.0     # matches deps.TWITCH_CHANNEL_FRESH_SEC
# Persistent no-captions cooldown: a video stamped captions_unavailable_at is
# not re-extracted for this long (survives restarts; the in-memory 1h
# _yt_attempted_at backoff remains the fast-path within a process).
CAPTIONS_UNAVAILABLE_FRESH_S = 86400.0

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _background() -> bool:
    """Quiet (autostart) mode: slower pass cadence, smaller per-pass
    budgets — the machine belongs to whoever logged in, not the archive."""
    from services.autostart import background_mode

    return background_mode()


def _yt_ingest_budget() -> int:
    """yt-dlp extracts per pass: 1 in background (bot-gated + slow), else 3."""
    return 1 if _background() else YOUTUBE_INGEST_PER_PASS


def _transcribe_budget() -> int:
    """Transcribe enqueues per pass: 1 in background, else 2."""
    return 1 if _background() else TRANSCRIBE_QUEUE_PER_PASS


def _pass_interval() -> float:
    """Seconds between scheduler passes: 6 min in background, else 3 min."""
    return 360.0 if _background() else PASS_INTERVAL_SEC


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


def _order_channels(channels: list, prio: set[tuple[str, str]]) -> list:
    """Stable priority-first ordering of the saved-channel list.

    Channels inside their top-priority window (just added, or the user
    viewing their page — see archive_db.mark_channel_priority) sort ahead
    of the older backlog; everything else keeps its saved order. Applied
    once per pass, so the YouTube per-pass budget and the backfill legs
    both consume priority channels first. Pure — the caller supplies the
    priority set, which keeps the self-check DB-free."""
    if not prio:
        return channels

    def _is_prio(ch: dict) -> bool:
        for platform, key in (
            ("twitch", ch.get("twitchSlug")),
            ("kick", ch.get("kickSlug")),
            ("youtube", ch.get("youtubeSlug")),
        ):
            slug = (key or "").strip().lower()
            if slug and (platform, slug) in prio:
                return True
        return False

    return sorted(channels, key=lambda ch: not _is_prio(ch))


def _ordered_channels(channels: list) -> list:
    """_order_channels fed with the live priority set from the DB."""
    return _order_channels(channels, archive_db.priority_channel_keys())


def _ingest_twitch(channel: dict) -> None:
    if not _platform_enabled(channel, "twitch"):
        return
    slug = (channel.get("twitchSlug") or "").strip().lower()
    if not slug:
        return
    # Snapshot gate (channels.py contract): a channel fetched within the
    # freshness window is skipped for this pass — the scheduler runs every
    # 180s, so without the gate every saved channel pays a GQL round-trip
    # per pass even though Twitch VOD metadata changes slowly.
    age = archive_db.channel_snapshot_age_sec("twitch", slug)
    if age is not None and age < TWITCH_CHANNEL_FRESH_SEC:
        return
    try:
        from services.archive_twitch import ingest_channel_vods

        rows = ingest_channel_vods(slug, limit=TWITCH_INGEST_LIMIT)
        if rows:
            # Same contract as channels.py: touch only after a successful
            # NON-EMPTY fetch, so a dead/empty channel is retried on the
            # next pass instead of being parked by an empty snapshot.
            archive_db.touch_channel_snapshot("twitch", slug)
        logger.info("scheduler twitch ingest %s: %d VOD(s) upserted", slug, len(rows))
    except Exception as exc:  # noqa: BLE001 — GQL 429 / dead channel
        logger.info("scheduler twitch ingest %s failed: %s", slug, exc)


def _ingest_kick(channel: dict) -> None:
    if not _platform_enabled(channel, "kick"):
        return
    # Kick Cloudflare/rate-limit freeze: skip the API walk until it lifts
    # (kick_api_service fails fast while frozen — this just avoids the
    # per-pass futile hits).
    from services.kick_gate import kick_gate_active

    if kick_gate_active():
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
    caption fetch; a captions-covered row is left alone. A video whose
    persisted no-captions marker (videos.captions_unavailable_at) is fresh
    is ALSO left alone — the last ingest proved there is nothing to fetch,
    and re-extracting every pass/boot (the in-memory 1h backoff dies with
    the process) is exactly the hammering the marker exists to stop. A
    successful caption ingest clears the marker, making the video a
    candidate again."""
    rows = archive_db.query(
        "SELECT captions_unavailable_at FROM videos "
        "WHERE platform='youtube' AND video_id=?",
        (video_id,),
    )
    if not rows:
        return False
    marker = rows[0]["captions_unavailable_at"]
    if marker:
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(marker) < timedelta(
                seconds=CAPTIONS_UNAVAILABLE_FRESH_S
            ):
                return True  # known captionless — skip while the marker is fresh
        except (TypeError, ValueError):
            pass  # unparseable stamp — fall through to the caption checks
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
        queued = _enqueue_chat_job("youtube", video_id)
        logger.info(
            "scheduler yt ingest %s: %d caption segment(s), chat job %s",
            video_id,
            report.get("transcript_segments", 0),
            "queued" if queued else "already covered",
        )
    except Exception as exc:  # noqa: BLE001 — bot wall / dead video
        logger.info("scheduler yt ingest %s failed (retry in 1h): %s", video_id, exc)
    finally:
        with _yt_lock:
            _yt_inflight.discard(video_id)


def _ingest_youtube(channel: dict) -> None:
    if not _platform_enabled(channel, "youtube"):
        return
    # Bot-gate freeze: no extract attempts until it lifts — every attempt
    # fails fast behind the wall (re-arming the freeze) and piles another
    # job row into the panel. Mirrors the instant-preview scheduler's gate
    # skip; the 1h in-memory _yt_attempted_at backoff remains the fast path
    # once the gate clears.
    from services.yt_gate import youtube_gate_active

    if youtube_gate_active():
        return
    yt_budget = _yt_ingest_budget()
    with _yt_lock:
        if len(_yt_inflight) >= yt_budget:
            return  # budget full — a later pass picks the rest
    urls = list(channel.get("vodVideos") or []) + list(channel.get("clipVideos") or [])
    if not urls:
        return
    spawned = 0
    for item in urls:
        if spawned >= yt_budget:
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


def _chat_job_guard(platform: str, video_id: str, *, retry_fresh_failed: bool = False) -> bool:
    """True when a chat backfill job already covers the video — the single
    producer-side dedupe predicate (search auto-kick, preview kick, ingest
    kick and the scheduler all consult it).

    Covered means: stored chat reaches the video's end (chat_covered — full
    capture, or a duration-less capture there is nothing to measure), a
    'chat' job is queued/running, or — unless *retry_fresh_failed* (the
    interactive lanes, where the user asked NOW and the per-video cooldowns
    already bound the hammering) — a job failed within FAILED_JOB_FRESH_S.
    A 'done' job on a chat-less video is the terminal no-chat marker
    (comments disabled / purged), and a 'done' job on a PARTIAL capture
    (rows exist but the newest sits far below the video's end) is terminal
    for the scheduler — but NOT for the user-facing lanes
    (retry_fresh_failed=True): opening the chat / searching is the user
    asking for the full history NOW, and the resume run is incremental
    (seeds from MAX(offset_sec)), so it only re-fetches the missing tail
    and self-heals a mid-sweep failure or a pre-publication empty run."""
    if archive_db.chat_covered(platform, video_id):
        return True
    latest = archive_db.latest_job(platform, video_id, kind="chat")
    if latest and latest["status"] in ("queued", "running"):
        return True
    if latest and latest["status"] == "done":
        # Terminal markers: no-chat (done job, no rows) is permanent; a
        # partial capture with a done job stays covered unless the user
        # asks now (retry_fresh_failed=True) — the one bounded resume path.
        if not archive_db.has_chat(platform, video_id) or not retry_fresh_failed:
            return True
    if latest and latest["status"] == "failed" and not retry_fresh_failed:
        try:
            fresh = datetime.fromisoformat(latest["updated_at"]) > (
                datetime.now(timezone.utc) - timedelta(seconds=FAILED_JOB_FRESH_S)
            )
        except (TypeError, ValueError):
            fresh = True  # unparseable — treat as fresh failure
        if fresh:
            return True  # failed < 1h ago — don't hammer
    return False


def _enqueue_chat_job(
    platform: str, video_id: str, *,
    job_id: Optional[str] = None,
    retry_fresh_failed: bool = False,
) -> bool:
    """Queue one chat-history backfill job for the archive worker.

    Dedupe = _chat_job_guard (has_chat / queued / running / done /
    fresh-failed unless *retry_fresh_failed*). *job_id* overrides the
    default stable id 'chat-<platform>-<video_id>'; the Twitch interactive
    kick lane keeps its own 'tw-backfill-<vid>' prefix so it never collides
    with legacy time-based rows' ids. The stable id's PK backstops a
    producer race (IntegrityError -> already queued). Returns True when a
    job was enqueued."""
    if _chat_job_guard(platform, video_id, retry_fresh_failed=retry_fresh_failed):
        return False
    job_id = job_id or f"chat-{platform}-{video_id}"
    try:
        archive_db.enqueue_job(job_id, "chat", platform, video_id, priority=0)
        return True
    except sqlite3.IntegrityError:
        # The row already exists (a producer race, or a stale row the guard
        # passed). Re-queue it IN PLACE so the stable job id never orphans a
        # retry. With retry_fresh_failed=True a 'done' row can only be a
        # PARTIAL capture the user asked to resume (the guard proved the
        # stored chat is short of the video's end, so the no-chat marker is
        # excluded by construction) — requeue it like a failed row.
        allowed = ("failed", "done") if retry_fresh_failed else ("failed",)
        marks = ",".join("?" * len(allowed))
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = archive_db.execute(
            "UPDATE archive_jobs SET status='queued', error=NULL, progress=0, "
            "attempts=0, next_retry_at=NULL, updated_at=?, heartbeat=? "
            f"WHERE id=? AND status IN ({marks})",
            (now_iso, now_iso, job_id, *allowed),
        )
        return cur.rowcount == 1


def _backfill_twitch_chat(channels: list) -> None:
    slugs = {str(c.get("twitchSlug") or "").strip().lower() for c in channels}
    slugs.discard("")
    if not slugs:
        return
    # Candidates = chat-less twitch VODs of saved channels. The queue is
    # the inflight tracker now: _enqueue_chat_job dedupes queued/running/
    # done/fresh-failed, and the archive worker (detached, supervised)
    # does the fetching with a per-IP concurrency cap inside backfill_chat.
    # A failed job is a candidate again after FAILED_JOB_FRESH_S (the
    # dedupe gates it): backfill_chat is incremental (seeds from the
    # deepest stored offset), so a mid-fetch 'service error' leaves partial
    # chat that the re-run completes instead of skipping forever.
    ph = ",".join("?" * len(slugs))
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = list(
        archive_db.query(
            """SELECT v.video_id, v.channel, v.started_at FROM videos v
               LEFT JOIN channel_priorities cp
                 ON cp.platform='twitch' AND cp.channel_key=lower(v.channel)
                  AND cp.priority_until > ?
               WHERE v.platform='twitch'
                 AND v.video_id GLOB '[0-9]*'
                 AND lower(v.channel) IN (%s)
                 AND NOT EXISTS (SELECT 1 FROM video_aliases a
                                 WHERE a.platform='twitch' AND a.video_id=v.video_id)
                 AND NOT EXISTS (SELECT 1 FROM messages m
                                 WHERE m.platform='twitch' AND m.video_id=v.video_id)
               ORDER BY (cp.priority_until IS NOT NULL) DESC, v.started_at ASC"""
            % ph,
            (now_iso,) + tuple(slugs),
        )
    )
    enqueued = 0
    for r in rows:
        if _enqueue_chat_job("twitch", r["video_id"]):
            enqueued += 1
    if enqueued:
        logger.info("scheduler twitch chat backfill: %d video(s) queued", enqueued)


def _backfill_youtube_chat() -> None:
    """Retro chat backfill for chat-less YouTube streams (leg 4b) — producer.

    The historical live-chat ingest crash (authorNameTextColor as a raw
    packed-ARGB int) archived captions but ZERO chat rows for streams; the
    covered-skip in _ingest_youtube then froze them forever (chat-less
    streams with captions never re-ingest). This leg enqueues kind='chat'
    jobs for exactly those videos: kind='stream' (was_live), no chat rows.
    The archive worker refetches live-chat replay — chat-only, no caption
    re-fetch (they exist already; re-fetching adds YouTube API pressure).
    'none' chat (stream with replay disabled) records a done job so it is
    not retried forever. Dedupe lives in _enqueue_chat_job (queued/running/
    done/fresh-failed skipped).
    """
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = list(
        archive_db.query(
            """SELECT videos.video_id FROM videos
               LEFT JOIN channel_priorities cp
                 ON cp.platform='youtube' AND cp.channel_key=lower(videos.channel)
                  AND cp.priority_until > ?
               WHERE videos.platform='youtube'
                 AND videos.kind='stream'
                 AND videos.video_id NOT LIKE 'youtube-live-%'
                 AND NOT EXISTS (SELECT 1 FROM messages m
                                 WHERE m.platform='youtube' AND m.video_id=videos.video_id)
               ORDER BY (cp.priority_until IS NOT NULL) DESC, videos.started_at ASC""",
            (now_iso,),
        )
    )
    enqueued = 0
    for r in rows:
        if _enqueue_chat_job("youtube", r["video_id"]):
            enqueued += 1
    if enqueued:
        logger.info("scheduler yt chat backfill: %d video(s) queued", enqueued)


# WS-4 original-title sweep: the manual channel-sync router backfills only
# its own channel at limit=20, so scheduled passes left every pre-WS-4 row
# English-translated forever. One daemon sweep thread at a time — the
# backfill's global fetch throttle serializes anyway — restarted each pass
# until every saved YouTube channel is drained.
ORIG_TITLES_SWEEP_LIMIT = 100
_orig_titles_lock = threading.Lock()
_orig_titles_thread: Optional[threading.Thread] = None


def _backfill_original_titles(channels: list) -> None:
    global _orig_titles_thread
    with _orig_titles_lock:
        if _orig_titles_thread is not None and _orig_titles_thread.is_alive():
            return  # previous sweep still running — this pass skips
        _orig_titles_thread = threading.Thread(
            target=_orig_titles_worker, args=(channels,), daemon=True,
            name="orig-titles-sweep",
        )
        _orig_titles_thread.start()


def _orig_titles_worker(channels: list) -> None:
    from services.archive_ytdlp import backfill_original_titles

    slugs = {(ch.get("youtubeSlug") or "").strip().lower() for ch in channels}
    slugs.discard("")
    # Rows archived from a search or under a renamed slug have no saved
    # channel entry (e.g. 'Lu Bu' vs slug 'lubumr') — sweep every DB
    # channel too; lower() dedupes the case variants ('gaveta'/'Gaveta').
    for r in archive_db.query(
        "SELECT DISTINCT lower(channel) AS slug FROM videos WHERE platform='youtube'"
    ):
        if r["slug"]:
            slugs.add(r["slug"])
    for slug in slugs:
        try:
            backfill_original_titles(slug, limit=ORIG_TITLES_SWEEP_LIMIT)
        except Exception as exc:  # noqa: BLE001 — one bad channel never blocks the rest
            logger.debug("orig-title sweep failed for %s: %s", slug, exc)


def _transcribe_video_candidate(platform: str, video_id: str) -> bool:
    """True when the video can still accept a transcribe job.

    Shared by the stale-failed requeue pass and the fresh window: the video
    row must exist, carry no transcript rows yet, and hold no terminal
    verdict (music / blocked — a DRM-dead or instrumental video is never
    re-run)."""
    rows = archive_db.query(
        """SELECT 1 FROM videos
           WHERE platform=? AND video_id=?
             AND NOT EXISTS (SELECT 1 FROM transcripts t
                             WHERE t.platform=videos.platform
                               AND t.video_id=videos.video_id)""",
        (platform, video_id),
    )
    if not rows:
        return False
    return (archive_db.video_transcript_kind(platform, video_id) or "") not in (
        "music", "blocked",
    )


def _requeue_failed_transcribe_job(
    job_id: str, now_iso: str
) -> bool:
    """Requeue one 'failed' transcribe job IN PLACE (stable job id — the
    worker never claims 'failed' rows, so this is the only way back into
    the queue). Returns True when the row was flipped; a raced enqueue
    (row already queued by a search) leaves it untouched.

    P1-1: a job that exhausted max_attempts is NEVER requeued — the
    worker's 3-attempt cap marked it failed as a final verdict, and
    resurrecting it would restart the same doomed cycle (a permanently
    failing remote VOD re-downloading ~350 MB of audio every hour)."""
    row = archive_db.query(
        "SELECT attempts, max_attempts FROM archive_jobs WHERE id = ?", (job_id,)
    )
    if row and int(row[0]["attempts"] or 0) >= int(row[0]["max_attempts"] or 3):
        logger.debug(
            "scheduler skipped failed transcribe job %s — attempts exhausted",
            job_id,
        )
        return False
    cur = archive_db.execute(
        "UPDATE archive_jobs SET status='queued', error=NULL, progress=0, "
        "updated_at=?, heartbeat=? WHERE id=? AND status='failed'",
        (now_iso, now_iso, job_id),
    )
    return cur.rowcount == 1


def _enqueue_transcriptions() -> None:
    """Top up transcribe jobs — Twitch/Kick (ready with a local file, or any
    row whose audio can be fetched at transcribe time — see
    archive_transcribe._transcribe_remote_twitch_kick) and YouTube
    captionless rows (captions permanently unavailable). One per-pass budget
    shared by two passes:

      1. Stale-failed requeue — 'failed' transcribe jobs older than
         FAILED_JOB_FRESH_S are requeued IN PLACE first, OUTSIDE the
         duration window. A long VOD's job ranks beyond the LIMIT-50 window
         and would starve forever otherwise (FIX C). Capped at half the
         per-pass budget (P1-1) so fresh candidates always get a share,
         and jobs whose attempts are exhausted are never resurrected.
      2. Fresh window — duration-ASC candidates (shortest first), skipping
         videos with queued/running/fresh-failed work.
    """
    now_utc = datetime.now(timezone.utc)
    fresh_cutoff = now_utc - timedelta(seconds=FAILED_JOB_FRESH_S)
    enqueued = 0
    budget = _transcribe_budget()
    # P1-1: pass 1 may use at most HALF the budget (0 in background mode,
    # where the whole budget is 1) — fresh candidates always get a share
    # instead of starving behind a wall of stale-failed requeues.
    pass1_cap = budget // 2
    now_iso = now_utc.isoformat(timespec="seconds")

    # Pass 1 — stale-failed requeue (window-independent). Jobs whose
    # attempts are exhausted are skipped by _requeue_failed_transcribe_job
    # (P1-1 zombie guard) — a permanently-failing VOD never cycles here.
    # Exhaust-quiet: LIMIT 100 + single per-pass debug summary (no per-job
    # INFO spam when the DB holds a large historical failure backlog).
    _pass1_exhausted = 0
    _pass1_non_candidate = 0
    for r in archive_db.query(
        """SELECT id, platform, video_id FROM archive_jobs
           WHERE kind='transcribe' AND status='failed' AND updated_at < ?
             AND attempts < max_attempts
           ORDER BY updated_at ASC LIMIT 100""",
        (fresh_cutoff.isoformat(timespec="seconds"),),
    ):
        if enqueued >= pass1_cap:
            break
        if not _transcribe_video_candidate(r["platform"], r["video_id"]):
            _pass1_non_candidate += 1
            continue  # transcribed meanwhile / terminal verdict — leave failed
        if _requeue_failed_transcribe_job(r["id"], now_iso):
            enqueued += 1
            logger.info(
                "scheduler requeued stale failed transcribe job %s (%s/%s)",
                r["id"], r["platform"], r["video_id"],
            )
        else:
            _pass1_exhausted += 1
    if _pass1_exhausted or _pass1_non_candidate:
        logger.info(
            "scheduler transcribe pass-1 skipped %d exhausted, %d non-candidate (cap %d, enqueued %d)",
            _pass1_exhausted, _pass1_non_candidate, pass1_cap, enqueued,
        )

    if enqueued >= budget:
        return

    # BOOT-02: do not auto-create transcribe work on an idle queue. Pass 1
    # still resurrects stale failures (attempt-capped). Fresh candidates
    # only enqueue when the user/search already put transcribe work in
    # flight — otherwise every 180s we'd start dozens of yt-dlp+ffmpeg jobs.
    inflight = list(
        archive_db.query(
            """SELECT 1 FROM archive_jobs
               WHERE kind='transcribe' AND status IN ('queued','running')
               LIMIT 1"""
        )
    )
    if not inflight:
        logger.debug("scheduler skip transcribe pass-2 (idle queue)")
        return

    # Pass 2 — fresh candidates. FIX A: twitch/kick rows are candidates
    # WITHOUT a local archive file (the worker downloads the audio at job
    # time), so the archive_path predicate is gone; a ready row whose file
    # was evicted/relocated is enqueued instead of skipped forever (FIX B).
    rows = list(
        archive_db.query(
            """SELECT platform, video_id, channel, title, duration_sec, archive_path
               FROM videos
               WHERE platform IN ('youtube','twitch','kick')
                 AND (status='ready' OR platform='youtube'
                      OR archive_path IS NULL OR archive_path = '')
                 AND NOT EXISTS (SELECT 1 FROM transcripts t
                                 WHERE t.platform=videos.platform
                                   AND t.video_id=videos.video_id)
               ORDER BY duration_sec ASC LIMIT 50"""
        )
    )
    for r in rows:
        if enqueued >= budget:
            break
        vid = r["video_id"]
        plat = r["platform"]
        latest = archive_db.latest_job(plat, vid, kind="transcribe")
        job_id = f"transcribe-{plat}-{vid}"
        # Terminal verdicts (music / blocked) are never re-run — for
        # youtube (captionless-ASR verdicts) AND twitch/kick (the remote
        # route marks deleted/sub-only VODs 'blocked').
        kind = archive_db.video_transcript_kind(plat, vid) or ""
        if kind in ("music", "blocked"):
            continue
        if plat == "youtube":
            # Captions-first policy: create a transcribe job ONLY when the
            # caption question is settled AND there is nothing that already
            # serves as the transcript — captions_unavailable_at set
            # (permanent unavailability -> ASR candidate) with no transcript
            # rows (the SQL's NOT EXISTS above) and no terminal verdict.
            # Never create while captions are still pending (no marker: the
            # ingest leg is extracting/retrying — the worker requeues any
            # kicked job with 'waiting for caption decision'). The audio is
            # downloaded at transcribe time (no local archive_path).
            if latest is None and archive_db.captions_unavailable_at(plat, vid) is None:
                continue
        if latest:
            if latest["status"] in ("queued", "running"):
                continue
            if latest["status"] == "failed":
                try:
                    if datetime.fromisoformat(latest["updated_at"]) > fresh_cutoff:
                        continue
                except (TypeError, ValueError):
                    continue  # unparseable — treat as fresh failure
                # Stale failed row inside the window (pass 1 may have spent
                # the budget on older jobs first) — requeue IN PLACE, same
                # stable-job-id contract as pass 1.
                if _requeue_failed_transcribe_job(job_id, now_iso):
                    enqueued += 1
                continue
        try:
            archive_db.enqueue_job(
                job_id, "transcribe", plat, vid, priority=TRANSCRIBE_PRIORITY_LOW
            )
        except sqlite3.IntegrityError:
            continue  # already queued by a search — nothing new
        enqueued += 1


def _run_pass() -> None:
    channels = _channels()
    if not channels:
        return
    # Lazy housekeeping: drop expired priority windows so the table never
    # grows and the next read of the live set is exact.
    try:
        archive_db.expire_channel_priorities()
    except Exception:  # noqa: BLE001 — pruning must never break a pass
        logger.debug("channel priority prune failed", exc_info=True)
    channels = _ordered_channels(channels)
    for ch in channels:
        _ingest_twitch(ch)
        _ingest_kick(ch)
        _ingest_youtube(ch)
    # Instant previews (first 6s of each channel's newest VOD, one platform
    # per channel) — kicked on a background thread so a download never blocks
    # this pass; a channel add/edit (kick_scheduler_pass) also refreshes it.
    try:
        from services.instant_preview import refresh_async

        refresh_async(channels)
    except Exception:  # noqa: BLE001 — the kick must never break a pass
        logger.debug("instant preview kick failed", exc_info=True)
    # Instant-preview PREFETCH — first ~8s of the 5 newest VODs per
    # (channel, platform), served by proxy_segment/proxy_playlist. Also a
    # background worker (one at a time): fetches are bounded but must never
    # block the pass. A channel add/edit (kick_scheduler_pass) refreshes it.
    try:
        from services.prefetch_cache import kick_prefetch_pass

        kick_prefetch_pass(channels)
    except Exception:  # noqa: BLE001 — the kick must never break a pass
        logger.debug("prefetch kick failed", exc_info=True)
    _backfill_twitch_chat(channels)
    _backfill_youtube_chat()
    _backfill_original_titles(channels)
    _enqueue_transcriptions()


def _loop(*, interval: Optional[float] = None, delay: float = 0.0) -> None:
    # None -> the background-aware default (6 min quiet / 3 min interactive);
    # an explicit interval (tests) wins over both.
    cadence = _pass_interval() if interval is None else interval
    # Boot grace so the first seconds stay uncontended; kick_scheduler_pass()
    # still wakes the daemon immediately regardless.
    if delay and _stop.wait(delay):
        return
    while not _stop.is_set():
        try:
            _run_pass()
        except Exception:  # noqa: BLE001 — a pass must never kill the daemon
            logger.exception("archive scheduler pass failed")
        if _stop.is_set():
            break
        _wake.wait(cadence)  # returns immediately on kick_scheduler_pass()
        _wake.clear()


# --- lifecycle --------------------------------------------------------------

def start_archive_scheduler(*, interval: Optional[float] = None, delay: float = 0.0) -> threading.Thread:
    """Start the scheduler daemon thread (idempotent). The first pass runs
    after `delay` seconds (boot grace) or immediately when delay=0 (current
    behavior); afterwards one pass per interval (None -> background-aware
    default: 6 min quiet / 3 min interactive) or on kick."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        _stop.clear()
        _wake.clear()
        _thread = threading.Thread(
            target=_loop, kwargs={"interval": interval, "delay": delay}, daemon=True,
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
# Top-priority ordering: a channel inside its priority window leads the
# pass; expired/unrelated priorities leave the saved order untouched.
assert _order_channels(
    [
        {"id": "backlog-1", "twitchSlug": "oldchan"},
        {"id": "hot", "twitchSlug": "newchan"},
        {"id": "backlog-2", "twitchSlug": "zoldchan"},
    ],
    {("twitch", "newchan")},
)[0]["id"] == "hot", "priority channel must lead the pass"
assert [c["id"] for c in _order_channels(
    [
        {"id": "backlog-1", "twitchSlug": "oldchan"},
        {"id": "hot", "twitchSlug": "newchan", "kickSlug": "newchan"},
        {"id": "backlog-2", "twitchSlug": "zoldchan"},
    ],
    {("kick", "newchan")},
)] == ["hot", "backlog-1", "backlog-2"], "kick priority must reorder the list"
assert [c["id"] for c in _order_channels(
    [{"id": "a", "kickSlug": "x"}, {"id": "b", "kickSlug": "y"}],
    {("twitch", "x")},  # expired/mismatched platform — no longer priority
)] == ["a", "b"], "expired or platform-mismatched priority must not reorder"
