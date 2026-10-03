"""Archive queue policy — WHAT gets transcribed, in WHICH order, HOW MANY at once.

One home for the archive queue's decision rules, so the scheduler (which
CREATES work) and the worker (which CONSUMES work) can never drift. Every
knob resolves settings -> env -> default, in that order, so a launcher can
override a user setting without editing the DB and a user setting can
override a stale shell export.

The four rules that used to live inline in two 5k-line modules:

  1. auto_transcribe_enabled()  — may the scheduler CREATE fresh transcribe
     work on its own? (the "BOOT-02" idle gate, now a setting)
  2. latest_per_channel_candidates() — WHICH videos are candidates: the
     newest N per channel, recency-ordered (was: 50 shortest globally,
     which starved every recent VOD of a large channel).
  3. transcript_route_verdict() — captions-first routing, ONE function
     consulted by both the scheduler (before enqueue) and the worker
     (before running), with a force-transcribe override.
  4. transcribe_job_concurrency() — how many VODs transcribe AT ONCE
     (default 1: one VOD at a time, with every lane cooperating on that
     single VOD — see archive_transcribe._transcribe_chunks_hybrid).

Priority tiers are named here instead of being magic numbers spread across
call sites (200 was previously discoverable only from a test file).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# --- priority tiers ------------------------------------------------------
# Ordered by how strongly the user asked for the work. Kept as named
# constants so a call site reads as INTENT ("the user opened this") rather
# than as a number nobody can justify.
PRIORITY_BACKGROUND = 0     # scheduler pass-2 backlog top-up
PRIORITY_SEARCH = 100       # transcript-source search jump-the-queue
PRIORITY_PREVIEW = 200      # the user opened/previewed this video
PRIORITY_FOCUS = 300        # the user is interacting with this item RIGHT NOW

# --- env overrides (launcher tier) ---------------------------------------
ENV_AUTO_TRANSCRIBE = "VODRIP_ARCHIVE_AUTO_TRANSCRIBE"
ENV_LATEST_PER_CHANNEL = "VODRIP_TRANSCRIBE_LATEST_PER_CHANNEL"
ENV_JOB_CONCURRENCY = "VODRIP_TRANSCRIBE_JOB_CONCURRENCY"
ENV_FOCUS_PAUSE = "VODRIP_TRANSCRIBE_FOCUS_PAUSE"
ENV_FORCE_TRANSCRIBE = "VODRIP_ARCHIVE_FORCE_TRANSCRIBE"

# --- defaults ------------------------------------------------------------
DEFAULT_LATEST_PER_CHANNEL = 5   # "the latest 5" per channel
DEFAULT_JOB_CONCURRENCY = 1      # one VOD at a time (lanes cooperate on it)
# Hard guard on the candidate pool, so a 400-channel archive cannot pull
# 2000 rows into memory every 3 minutes. Recency-ordered, so the newest
# work across ALL channels always survives the cut.
DEFAULT_CANDIDATE_POOL = 200
# A focus record older than this is ignored (and lazily deleted) — a user
# who closes the app mid-VOD must never wedge the queue forever.
FOCUS_TTL_S = 300.0

# Verdicts returned by transcript_route_verdict().
VERDICT_RUN_ASR = "run-asr"
VERDICT_SKIP_CAPTIONS = "skip-captions"
VERDICT_WAIT_CAPTION = "wait-caption"
VERDICT_MUSIC = "music"
VERDICT_BLOCKED = "blocked"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _settings():
    """The live settings model, or None when unavailable.

    Lazy: archive_transcribe is opt-in and does not import deps at module
    scope, so a missing settings manager must degrade to defaults, never
    raise into a worker thread."""
    try:
        from deps import settings_mgr  # lazy by design

        return settings_mgr.get()
    except Exception:
        return None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    return max(minimum, val)


def _setting(name: str, default):
    """settings.<name> when present, else the default.

    getattr-guarded so an OLD settings.json (or a test double built with
    SimpleNamespace) falls back to the default instead of raising."""
    settings = _settings()
    if settings is None:
        return default
    val = getattr(settings, name, None)
    return default if val is None else val


# --- 1. may the scheduler create fresh work? -----------------------------
def auto_transcribe_enabled() -> bool:
    """Fresh transcribe enqueue on an idle queue (default ON).

    The BOOT-02 guard returned early unless transcribe work was ALREADY in
    flight, which meant a freshly added channel was ingested and then sat
    there: nothing to enqueue from, so the user had to open or search a
    video by hand. The guard existed to stop a boot-time yt-dlp+ffmpeg
    storm, which is a BUDGET problem, not a permission problem — the
    per-pass budget and the priority ordering already bound the storm, so
    the gate is now a user-visible setting that defaults to the behaviour
    that actually does the job."""
    return _env_bool(
        ENV_AUTO_TRANSCRIBE, bool(_setting("archive_auto_transcribe", True))
    )


# --- 2. which videos are candidates? -------------------------------------
def latest_per_channel() -> int:
    """Newest N videos per channel considered for transcription."""
    return _env_int(
        ENV_LATEST_PER_CHANNEL,
        int(_setting("archive_transcribe_latest_per_channel", DEFAULT_LATEST_PER_CHANNEL)),
        minimum=0,
    )


def latest_per_channel_candidates(limit_per_channel: Optional[int] = None) -> list[dict]:
    """Newest N videos per channel, recency-ordered, without a transcript.

    Replaces `ORDER BY duration_sec ASC LIMIT 50`: shortest-first across
    ALL channels meant the 50 shortest items in a large archive crowded
    out every recent VOD — the exact opposite of "transcribe the latest 5
    per channel".

    Recency key is COALESCE(started_at, created_at) DESC (both UTC ISO
    from the ingest leg; a NULL started_at falls back to the row's own
    creation time). duration_sec ASC is the tiebreak ONLY, so equal-timestamp
    rows keep the old quick-win order without ever beating a fresher row.
    """
    from services import archive_db  # lazy: avoids an import cycle

    per_channel = latest_per_channel() if limit_per_channel is None else int(limit_per_channel)
    if per_channel <= 0:
        return []
    pool = _env_int(
        "VODRIP_TRANSCRIBE_CANDIDATE_POOL",
        int(_setting("archive_transcribe_candidate_pool", DEFAULT_CANDIDATE_POOL)),
        minimum=1,
    )
    rows = archive_db.query(
        """SELECT platform, video_id, channel, title, duration_sec, archive_path, started_at
             FROM (
               SELECT v.platform, v.video_id, v.channel, v.title, v.duration_sec,
                      v.archive_path, v.started_at, v.created_at,
                      ROW_NUMBER() OVER (
                        PARTITION BY v.platform, lower(v.channel)
                        ORDER BY COALESCE(v.started_at, v.created_at) DESC, v.created_at DESC
                      ) AS rn
                 FROM videos v
                WHERE v.platform IN ('youtube','twitch','kick')
                  AND (v.status='ready' OR v.platform='youtube'
                       OR v.archive_path IS NULL OR v.archive_path = '')
                  AND NOT EXISTS (SELECT 1 FROM transcripts t
                                  WHERE t.platform=v.platform AND t.video_id=v.video_id)
             )
            WHERE rn <= ?
            ORDER BY COALESCE(started_at, created_at) DESC, duration_sec ASC
            LIMIT ?""",
        (per_channel, pool),
    )
    return list(rows)


# --- 3. captions-first routing (single source) ---------------------------
def transcript_route_verdict(
    platform: str,
    video_id: str,
    *,
    subtitles_first: Optional[bool] = None,
    force_transcribe: bool = False,
) -> str:
    """The ONE captions-first verdict, consulted by scheduler and worker.

    Decision matrix (captions-first, settings.yt_subtitles_first, default
    True):
      'skip-captions' — captions-first ON and transcript rows exist: the
          captions ARE the transcript — resolve done, never ASR.
      'music'         — terminal VAD verdict (speech fraction below
          VODRIP_MUSIC_SPEECH_FRAC): instrumental — done, never ASR.
      'blocked'       — terminal download verdict (DRM/age-gated/deleted/
          private): the audio can never be fetched — done, never ASR.
      'wait-caption'  — no captions AND no captions_unavailable_at marker:
          the caption question is undetermined (the ingest leg is still
          extracting) — requeue, never run ASR.
      'run-asr'       — captions_unavailable_at set (permanent
          unavailability) OR subtitles_first OFF.

    force_transcribe=True bypasses ONLY the caption question ('wait-caption'
    and 'skip-captions' become 'run-asr'); it never overrides the terminal
    'music'/'blocked' verdicts, which are physical facts about the media —
    a forced ASR run on a music-only or DRM'd video produces nothing and
    burns the machine's time. Non-YouTube platforms are always 'run-asr'
    (Twitch and Kick ship no caption track to prefer).
    """
    from services import archive_db  # lazy: avoids an import cycle

    if platform != "youtube":
        return VERDICT_RUN_ASR
    kind = archive_db.video_transcript_kind(platform, video_id) or ""
    if kind == "music":
        return VERDICT_MUSIC
    if kind == "blocked":
        return VERDICT_BLOCKED
    if force_transcribe:
        return VERDICT_RUN_ASR
    if subtitles_first is None:
        subtitles_first = bool(_setting("yt_subtitles_first", True))
    has_rows = bool(archive_db.transcript_for(platform, video_id))
    if has_rows and subtitles_first:
        return VERDICT_SKIP_CAPTIONS
    if not has_rows and archive_db.captions_unavailable_at(platform, video_id) is None:
        return VERDICT_WAIT_CAPTION
    return VERDICT_RUN_ASR


def force_transcribe_enabled() -> bool:
    """Force ASR even while the caption question is still open (default OFF).

    The escape hatch for a video whose captions never arrive: the
    captions-first matrix would otherwise hold the job in 'wait-caption'
    forever if the ingest leg died without stamping an unavailability
    verdict. Bypasses the caption question only — see
    transcript_route_verdict for what it deliberately does NOT override."""
    return _env_bool(
        ENV_FORCE_TRANSCRIBE, bool(_setting("archive_force_transcribe", False))
    )


# --- 4. how many VODs at once? ------------------------------------------
def transcribe_job_concurrency() -> int:
    """Max VODs transcribing concurrently. Default 1 (one at a time).

    1 means "one VOD at a time, GPU and CPU cooperating on that single
    VOD": the worker claims ONE job and the existing intra-VOD hybrid
    (archive_transcribe._transcribe_chunks_hybrid) fans that VOD's chunks
    across every lane in the pool. 0 = unlimited = the legacy behaviour
    (each lane claims a DIFFERENT VOD)."""
    return _env_int(
        ENV_JOB_CONCURRENCY,
        int(_setting("archive_transcribe_concurrency", DEFAULT_JOB_CONCURRENCY)),
        minimum=0,
    )


# --- 5. user interaction -> focus ---------------------------------------
def focus_pauses_queue() -> bool:
    """While the user is on item X, transcribe claims are limited to X."""
    return _env_bool(
        ENV_FOCUS_PAUSE, bool(_setting("archive_focus_pauses_queue", True))
    )


def active_focus(max_age_s: float = FOCUS_TTL_S) -> Optional[tuple[str, str]]:
    """The (platform, video_id) the user is interacting with, or None.

    Expired rows are deleted on read so a stale focus cannot wedge the
    queue, and a crash mid-write leaves at most one expired row behind."""
    from services import archive_db  # lazy: avoids an import cycle

    if not focus_pauses_queue():
        return None
    try:
        rows = archive_db.query(
            "SELECT platform, video_id, focused_at FROM user_focus "
            "ORDER BY focused_at DESC LIMIT 5"
        )
    except Exception:
        return None
    cutoff = (_now() - timedelta(seconds=max_age_s)).isoformat(timespec="seconds")
    for row in rows:
        if (row["focused_at"] or "") >= cutoff:
            return row["platform"], row["video_id"]
    # Everything is expired — clean up so the table cannot grow unbounded.
    try:
        archive_db.execute("DELETE FROM user_focus WHERE focused_at < ?", (cutoff,))
    except Exception:
        logger.debug("focus cleanup failed", exc_info=True)
    return None
