"""
Archive routes — read/write the local SQLite store (chat, transcripts, video
index, dedupe, job queue). Consumers: ingestion adapters (YouTube/Twitch/Kick)
and the search UI.
"""
import asyncio
import logging
import re
import sqlite3
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from services import archive_db, archive_twitch, queue_policy
from services.archive_scheduler import TRANSCRIBE_PRIORITY_HIGH, _chat_job_guard

logger = logging.getLogger(__name__)

# Hard ceiling on ONE local /api/archive/search call. See the wait_for at the
# call site for the reasoning; the remote sibling uses 25s for a network
# round-trip, the local pass only touches a local SQLite file, so it gets a
# slightly larger budget.
_SEARCH_TIMEOUT_S = 30.0


def _start_frozen_archive_worker() -> None:
    """Start the optional worker after a job is queued in the frozen app."""
    if not getattr(sys, "frozen", False):
        return
    try:
        from services.asr_runtime import start_archive_worker

        start_archive_worker()
    except Exception:
        logger.warning("could not start optional archive worker", exc_info=True)
router = APIRouter(tags=["archive"])


# --- Twitch chat backfill (background) --------------------------------------

# A full 12h VOD has tens of thousands of comments; backfill_chat's default
# max_messages=200 would stop a real VOD at 200 rows (TwitchProbe-verified:
# 211 GQL pages across 6 VODs, zero 429s). Re-runs are incremental and
# idempotent (seed = MAX(messages.offset_sec)), so a big cap only ever
# fetches what is still missing.
_BACKFILL_MAX_MESSAGES = 100_000  # ponytail: platform ceiling — see BACKFILL_MAX_MESSAGES

_backfill_inflight: set[str] = set()  # video_ids currently backfilling
_backfill_lock = threading.Lock()     # guards the set + throttle clock
_last_auto_kick = 0.0                 # monotonic clock of the last auto-kick
# Preview-panel progress (0..1) for in-flight runs — written by the worker
# thread after every stored page, read under the lock by the panel endpoint.
_backfill_progress: dict[str, float] = {}
_AUTO_KICK_MIN_GAP_S = 15.0           # min seconds between auto-kicks
_AUTO_KICK_LIMIT = 2                  # newest chat-less videos per search
# Completion cooldown: a chat-less VOD (comments disabled / purged) would
# otherwise be re-kicked on every search; remember recent attempts so
# auto-kick skips them for a while. Manual backfill is never throttled.
_backfill_attempted_at: dict[str, float] = {}
_BACKFILL_COOLDOWN_S = 600.0
# P2-6: consecutive failed resume attempts per video (interactive lane).
# A permanently-unresumable tail (the API answers the same service error
# at the same offset forever) must not re-kick every _BACKFILL_COOLDOWN_S
# indefinitely — after the limit the panel status goes 'idle' and the
# auto-kicks stop (manual backfill stays available). Reset on any fetch
# that actually made progress or reached a terminal state.
_backfill_failed_resumes: dict[str, int] = {}
_BACKFILL_FAILED_RESUME_LIMIT = 3

# Transcript enrichment (search-v2): same global-throttle + per-video
# cooldown shape as chat backfill, on its OWN clock so the two halves never
# starve each other. NOT gated on worker_live(): a frozen app starts the
# optional worker itself after enqueueing (see _start_frozen_archive_worker
# and _transcribe_candidates).
_last_transcribe_kick = 0.0
_TRANSCRIBE_MIN_GAP_S = 30.0
_transcribe_attempted_at: dict[str, float] = {}
_TRANSCRIBE_COOLDOWN_S = 600.0
_TRANSCRIBE_FAILED_FRESH_S = 3600.0
_TRANSCRIBE_LIMIT = 1
# How far down the ranked candidate list the archive_path stat() probe walks
# before giving up (see _transcribe_candidates).
_TRANSCRIBE_PROBE_CAP = 10
_transcribe_lock = threading.Lock()

# YouTube chat display-name resolution: lazy, throttled, fire-and-forget.
# A USER-filter search warms the first batch of author channel ids; resolved
# names are cached in messages.display_name and matched on later searches.
# Bot-walled (503) resolutions fail inside the worker and retry next run.
_display_name_lock = threading.Lock()
_display_name_last_run: float = 0.0
_DISPLAY_NAME_COOLDOWN_S = 120.0
_DISPLAY_NAME_BATCH = 20


def _maybe_resolve_display_names() -> None:
    """Fire one bounded display-name resolution batch, at most every
    _DISPLAY_NAME_COOLDOWN_S. Never blocks the search response."""
    global _display_name_last_run
    now = time.time()
    with _display_name_lock:
        if now - _display_name_last_run < _DISPLAY_NAME_COOLDOWN_S:
            return
        _display_name_last_run = now
    threading.Thread(
        target=_resolve_display_names_worker,
        daemon=True,
        name="yt-display-names",
    ).start()


def _resolve_display_names_worker() -> None:
    try:
        from services.archive_ytdlp import resolve_youtube_display_names

        n = resolve_youtube_display_names(_DISPLAY_NAME_BATCH)
        if n:
            logger.info("resolved %d youtube chat display name(s)", n)
    except Exception:
        logger.debug("display-name resolution skipped", exc_info=True)


def _set_backfill_progress(video_id: str, progress: float) -> None:
    with _backfill_lock:
        _backfill_progress[video_id] = progress


async def _run_backfill(
    video_id: str, channel: str, seed_offset_sec: Optional[float] = None
) -> None:
    """Background task: run backfill_chat in a worker thread; drop the
    in-flight marker and stamp the completion time on exit."""
    _set_backfill_progress(video_id, 0.0)
    result = None
    try:
        result = await asyncio.to_thread(
            archive_twitch.backfill_chat,
            channel, video_id,
            max_messages=_BACKFILL_MAX_MESSAGES,
            seed_offset_sec=seed_offset_sec,
            progress_cb=lambda p: _set_backfill_progress(video_id, p),
        )
    except Exception:
        logger.exception("chat backfill failed for twitch/%s", video_id)
    finally:
        with _backfill_lock:
            _backfill_inflight.discard(video_id)
            _backfill_attempted_at[video_id] = time.monotonic()
            _backfill_progress.pop(video_id, None)
            if result is None:
                # The resume fetch failed — count it (P2-6); after
                # _BACKFILL_FAILED_RESUME_LIMIT consecutive failures the
                # panel stops polling and the auto-kicks stop.
                _backfill_failed_resumes[video_id] = (
                    _backfill_failed_resumes.get(video_id, 0) + 1
                )
            elif result.get("stopped") in ("end_of_chat", "max_messages", "already"):
                # The tail is resumable after all (or terminal) — clear the
                # failure streak. 'busy'/'queued' leave it unchanged (no
                # fetch actually ran).
                _backfill_failed_resumes[video_id] = 0
        # Bulk chat inserts leave the FTS index fragmented; merge after every
        # completed backfill (manual or auto) so searches stay fast.
        try:
            archive_db.optimize_fts()
        except Exception:
            logger.exception("fts optimize failed after backfill")


def _kick_backfill(
    video_id: str,
    channel: str,
    seed_offset_sec: Optional[float] = None,
    *,
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> str:
    """Start a background Twitch chat backfill; returns the status word.

    ``loop`` is supplied by the preview panel when this synchronous function
    runs in PANEL_EXECUTOR. The DB guards stay in that worker while the
    coroutine is submitted safely back to the request loop.

    'queued' — task started now; 'running' — already in flight;
    'already' — chat rows exist or a chat job (queued/running/done marker)
    already covers the video, nothing to do; 'failed' — could not start."""
    if not (channel or "").strip():
        return "failed"
    with _backfill_lock:
        if video_id in _backfill_inflight:
            return "running"
    # Shared scheduler guard: has_chat / queued / running / done (the done
    # row on a chat-less VOD is the terminal no-chat marker). retry_fresh_
    # failed=True: an explicit kick (manual endpoint, ingest, preview) is
    # the user asking NOW — a recently-failed row is retried, not ignored.
    if _chat_job_guard("twitch", video_id, retry_fresh_failed=True):
        return "already"
    try:
        with _backfill_lock:
            _backfill_inflight.add(video_id)
        coroutine = _run_backfill(
            video_id, channel, seed_offset_sec=seed_offset_sec
        )
        if loop is None:
            asyncio.get_running_loop().create_task(coroutine)
        else:
            asyncio.run_coroutine_threadsafe(coroutine, loop)
        return "queued"
    except Exception:
        logger.exception("could not start chat backfill for twitch/%s", video_id)
        with _backfill_lock:
            _backfill_inflight.discard(video_id)
        # If task submission failed before ownership transferred to a loop,
        # close the coroutine so the failed request does not leak it.
        try:
            coroutine.close()
        except (NameError, UnboundLocalError):
            pass
        return "failed"

def kick_preview_backfill(
    platform: str,
    video_id: str,
    offset_sec: Optional[float] = None,
    *,
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> str:
    """Throttled single-video Twitch chat backfill on preview open.

    All validation, status, and archive reads are synchronous so callers can
    place this function in PANEL_EXECUTOR. When ``loop`` is provided, only
    coroutine submission crosses back to that event loop.

    Mirrors _maybe_auto_backfill's gates on ONE video: numeric (non-watchdog)
    id, an archived row with a channel, and the same shared auto-kick
    throttle + per-video cooldown clocks, so preview and search kicks share
    one budget. *offset_sec* (the client playhead) seeds the sweep so
    near-playhead chat arrives first (see backfill_chat). Returns the
    _kick_backfill status word
    ('queued'/'running'/'already'/'failed'), or '' when no kick applies
    (wrong platform, synthetic id, unknown video, throttled, or cooldown)."""
    global _last_auto_kick
    if (platform or "").strip().lower() != "twitch":
        return ""
    if not video_id or not re.fullmatch(r"[0-9]+", video_id):
        return ""
    row = archive_db.query(
        "SELECT channel FROM videos WHERE platform='twitch' AND video_id=?",
        (video_id,),
    )
    if not row or not (row[0]["channel"] or "").strip():
        return ""
    # A chat job already covers the video (queued/running, or the done
    # no-chat marker) — the kick would be a no-op, so don't consume the
    # shared throttle or spawn a pointless task. retry_fresh_failed=True:
    # opening the preview IS the user asking for chat now.
    if _chat_job_guard("twitch", video_id, retry_fresh_failed=True):
        return ""
    now = time.monotonic()
    with _backfill_lock:
        if now - _last_auto_kick < _AUTO_KICK_MIN_GAP_S:
            return ""
        if now - _backfill_attempted_at.get(video_id, 0.0) < _BACKFILL_COOLDOWN_S:
            return ""
        if _backfill_failed_resumes.get(video_id, 0) >= _BACKFILL_FAILED_RESUME_LIMIT:
            return ""  # P2-6: N failed resumes — no more auto-kicks
    status = _kick_backfill(
        video_id,
        row[0]["channel"],
        seed_offset_sec=offset_sec,
        loop=loop,
    )
    if status == "queued":
        with _backfill_lock:
            _last_auto_kick = now
    return status


def preview_backfill_status(platform: str, video_id: str) -> tuple[str, float]:
    """('idle' | 'running' | 'done', progress 0..1) for the preview-panel
    envelope.

    'running' also covers "kick owed": the archive could still grow — no
    rows yet, or stored rows that do NOT reach the video's end (a partial
    capture: a backfill that died mid-sweep, ran while the broadcast was
    still live, or a watchdog capture of only the watched window) — so the
    next panel poll will kick an incremental resume (the shared 15 s
    throttle + per-video 600 s cooldown bound the kick rate). 'idle' =
    nothing will come (unknown/synthetic video, or the terminal no-chat
    marker: a done job proved the API has nothing — comments disabled /
    purged) — the panel stops polling. 'done' = stored chat covers the
    whole video; the panel fetches once more."""
    if (platform or "").strip().lower() != "twitch":
        return "idle", 0.0
    if not video_id or not re.fullmatch(r"[0-9]+", video_id):
        return "idle", 0.0
    with _backfill_lock:
        if video_id in _backfill_inflight:
            return "running", _backfill_progress.get(video_id, 0.0)
    if archive_db.chat_covered("twitch", video_id):
        return "done", 1.0
    latest = archive_db.latest_job("twitch", video_id, kind="chat")
    if latest and latest["status"] in ("queued", "running"):
        return "running", 0.0  # a worker owns the fetch — panel stays bounded
    if latest and latest["status"] == "done" and not archive_db.has_chat("twitch", video_id):
        # Terminal no-chat marker: the backfill already proved the API has
        # nothing (comments disabled / purged) — 'idle' so the panel stops
        # polling and serves the full (empty) timeline, never re-kicking.
        return "idle", 0.0
    row = archive_db.query(
        "SELECT channel FROM videos WHERE platform='twitch' AND video_id=?",
        (video_id,),
    )
    if not row or not (row[0]["channel"] or "").strip():
        return "idle", 0.0
    # Kick owed: no rows yet, or rows short of the video's end. Unlike the
    # pre-coverage build, a recent failed attempt is NOT terminal 'idle' —
    # it keeps the panel polling and the kick's own throttle + cooldown
    # clocks bound the retry rate, so a partial capture self-heals once the
    # API recovers instead of freezing on the head window forever.
    # P2-6: EXCEPT after _BACKFILL_FAILED_RESUME_LIMIT consecutive failed
    # resumes on the same tail — the API is not recovering; stop the loop
    # (idle = the panel stops polling, auto-kicks stop).
    if _backfill_failed_resumes.get(video_id, 0) >= _BACKFILL_FAILED_RESUME_LIMIT:
        return "idle", 0.0
    return "running", 0.0


def _tokenize(text: str) -> list[str]:
    """Lowercase, non-alphanumeric split (matches the search tokenizer)."""
    return [t for t in re.split(r"[^0-9a-z]+", (text or "").lower()) if t]


def _title_relevance(q: str, title: str) -> int:
    """Count of query tokens present in the title, fuzzy-tolerant.

    A q token counts when it equals a title token or sits within a cheap
    Levenshtein distance (≤ max(1, len//5)) of one — "twitch" matches
    "twitc", "gaming" matches "gamin". Reuses archive_db._levenshtein."""
    q_tokens = _tokenize(q)
    title_tokens = _tokenize(title)
    if not q_tokens or not title_tokens:
        return 0
    score = 0
    for qt in q_tokens:
        for tt in title_tokens:
            if tt == qt:
                score += 1
                break
            if archive_db._levenshtein(qt, tt, max(1, len(qt) // 5)) is not None:
                score += 1
                break
    return score


def _maybe_auto_backfill(
    *, platform: Optional[str], channel: Optional[str], source: str,
    q: str = "", video_id: Optional[str] = None,
) -> list[dict]:
    """Chat half of _maybe_enrich: lazily kick chat backfill for chat-less
    Twitch VODs in scope.

    Candidates are ranked by title-token relevance to q (ties → newest
    started_at first) and the top _AUTO_KICK_LIMIT are kicked. Throttled to
    one burst per _AUTO_KICK_MIN_GAP_S; in-flight, recently-attempted and
    job-covered videos are skipped (queued/running, or the done no-chat
    marker — _chat_job_guard, the scheduler's dedupe). Returns the kicked
    rows (video_id/channel/title) so the search response can show an honest
    'Indexing…' line; the pre-v2 caller contract (return None, chat-only)
    is preserved for tests.

    With a video_id the scope is that single video only (a video-scoped
    search must never kick backfills for unrelated archive-wide VODs — the
    popup's 'Indexing N videos…' line would lie about what's being
    indexed). Non-Twitch videos resolve to no candidates."""
    global _last_auto_kick
    source_set = {s for s in source.split(",") if s}
    if "both" in source_set or not source_set:
        source_set = {"chat", "transcript", "video"}
    if "chat" not in source_set:
        return []
    if platform and "twitch" not in [
        p.strip().lower() for p in platform.split(",") if p.strip()
    ]:
        return []
    now = time.monotonic()
    with _backfill_lock:
        if now - _last_auto_kick < _AUTO_KICK_MIN_GAP_S:
            return []
    if video_id:
        rows = list(archive_db.query(
            "SELECT v.video_id, v.channel, v.title, v.started_at FROM videos v "
            "WHERE v.platform='twitch' AND v.video_id=? "
            "AND v.video_id GLOB '[0-9]*'"
            " AND NOT EXISTS (SELECT 1 FROM messages m "
            "  WHERE m.platform='twitch' AND m.video_id=v.video_id)",
            (video_id,),
        ))
    else:
        sql = (
            "SELECT v.video_id, v.channel, v.title, v.started_at FROM videos v "
            "WHERE v.platform='twitch' AND NOT EXISTS ("
            "  SELECT 1 FROM messages m WHERE m.platform='twitch' AND m.video_id=v.video_id)"
            # Watchdog rows are synthetic ('twitch-live-<channel>-<ts>'): backfill
            # needs a numeric VOD id (same gate as the manual endpoint).
            " AND v.video_id GLOB '[0-9]*'"
        )
        params: list[Any] = []
        if channel:
            slugs = [c.strip() for c in channel.split(",") if c.strip()]
            if slugs:
                sql += " AND lower(v.channel) IN (" + ",".join("?" * len(slugs)) + ")"
                params.extend(s.lower() for s in slugs)
        # Relevance is computed in Python (SQL can't score titles); the cap only
        # bounds the scan, never the final ranking.
        sql += " ORDER BY v.started_at DESC LIMIT 100"
        rows = list(archive_db.query(sql, params))
        rows.sort(key=lambda r: r["started_at"] or "", reverse=True)
        rows.sort(key=lambda r: -_title_relevance(q, r["title"] or ""))  # stable: keeps newest-first
        rows = rows[:_AUTO_KICK_LIMIT]
    kicked: list[dict] = []
    for r in rows:
        vid = r["video_id"]
        if not (r["channel"] or "").strip():
            continue  # nothing to backfill against without a channel
        # Scheduler guard: a chat job already covers the video (queued /
        # running, or the done no-chat marker on a chat-less VOD) — not a
        # kick candidate; a fresh failure is skipped too (same anti-hammer
        # policy as the scheduler — explicit preview/manual kicks retry).
        if _chat_job_guard("twitch", vid):
            continue
        with _backfill_lock:
            if vid in _backfill_inflight:
                continue
            if now - _backfill_attempted_at.get(vid, 0.0) < _BACKFILL_COOLDOWN_S:
                continue
            if _backfill_failed_resumes.get(vid, 0) >= _BACKFILL_FAILED_RESUME_LIMIT:
                continue  # P2-6: N failed resumes — no more auto-kicks
            _backfill_inflight.add(vid)
            kicked.append(r)
        asyncio.get_running_loop().create_task(_run_backfill(vid, r["channel"] or ""))
    if kicked:
        with _backfill_lock:
            _last_auto_kick = now
    return kicked


def _transcribe_candidates(
    *, platform: Optional[str], channel: Optional[str], q: str
) -> list[dict]:
    """YouTube 'ready' videos in scope without transcripts, ranked by title
    relevance then duration (shortest first — fast feedback), top-1.

    Gates: global 30s throttle (own clock), per-video 600s cooldown, file
    still on disk, not covered by captions, no queued/running transcribe
    job, and latest job not failed-within-1h. A frozen app starts the optional
    worker immediately after enqueueing the job.
    """
    if platform:
        plats = {p.strip().lower() for p in platform.split(",") if p.strip()}
        if plats and not plats.intersection({"youtube", "twitch", "kick"}):
            return []
    now = time.monotonic()
    with _transcribe_lock:
        if now - _last_transcribe_kick < _TRANSCRIBE_MIN_GAP_S:
            return []
    sql = (
        "SELECT v.platform, v.video_id, v.channel, v.title, v.duration_sec, v.archive_path "
        "FROM videos v WHERE v.platform IN ('youtube','twitch','kick') AND v.status='ready' "
        "AND v.archive_path IS NOT NULL AND v.archive_path != '' "
        "AND NOT EXISTS (SELECT 1 FROM transcripts t "
        "  WHERE t.platform=v.platform AND t.video_id=v.video_id)"
    )
    params: list[Any] = []
    if channel:
        slugs = [c.strip() for c in channel.split(",") if c.strip()]
        if slugs:
            sql += " AND lower(v.channel) IN (" + ",".join("?" * len(slugs)) + ")"
            params.extend(s.lower() for s in slugs)
    # Shortest-first SQL scan; final rank re-sorts by relevance in Python.
    sql += " ORDER BY v.duration_sec ASC LIMIT 50"
    rows = list(archive_db.query(sql, params))
    # Batch the per-candidate probes (were 2 queries EACH — an N+1 of up to
    # 100 SELECTs per search when the worker is live): transcript coverage
    # and the latest transcribe job, both read-only filters with identical
    # per-video semantics. Job ids are unique per video
    # ("transcribe-<platform>-<vid>"), so per-video "latest" is well-defined.
    vids = [r["video_id"] for r in rows]
    try:
        from deps import settings_mgr  # lazy: mirrors captions_cover

        subtitles_first = bool(getattr(settings_mgr.get(), "yt_subtitles_first", True))
    except Exception:
        subtitles_first = True
    covered: set[str] = set()
    latest_by_vid: dict[str, dict] = {}
    if vids:
        if subtitles_first:
            covered = {
                r["video_id"] for r in archive_db.query(
                    "SELECT DISTINCT video_id FROM transcripts "
                    "WHERE platform IN ('youtube','twitch','kick') AND video_id IN ("
                    + ",".join("?" * len(vids)) + ")",
                    vids,
                )
            }
        for r in archive_db.query(
            "SELECT * FROM archive_jobs WHERE platform IN ('youtube','twitch','kick') "
            "AND video_id IN (" + ",".join("?" * len(vids)) + ") AND kind='transcribe'",
            vids,
        ):
            cur = latest_by_vid.get(r["video_id"])
            if cur is None or r["created_at"] > cur["created_at"]:
                latest_by_vid[r["video_id"]] = dict(r)
    fresh_cutoff = datetime.now(timezone.utc) - timedelta(seconds=_TRANSCRIBE_FAILED_FRESH_S)
    out: list[dict] = []
    for r in rows:
        vid = r["video_id"]
        if now - _transcribe_attempted_at.get(vid, 0.0) < _TRANSCRIBE_COOLDOWN_S:
            continue
        if vid in covered:
            continue
        latest = latest_by_vid.get(vid)
        if latest and latest["status"] in ("queued", "running"):
            continue
        if latest and latest["status"] == "failed":
            try:
                fresh = datetime.fromisoformat(latest["updated_at"]) > fresh_cutoff
            except (TypeError, ValueError):
                fresh = True  # unparseable timestamp — treat as fresh failure
            if fresh:
                continue  # failed < 1h ago — do not re-enqueue forever
        out.append(r)
    out.sort(key=lambda r: r["duration_sec"] or 0.0)  # stable: duration tiebreak
    out.sort(key=lambda r: -_title_relevance(q, r["title"] or ""))  # relevance first
    # The disk probe runs LAST and bounded. archive_path lives on a network/
    # spinning-disk volume, so Path().is_file() is a real stat() per row —
    # up to 50 of them in the request path to pick the ONE row this function
    # returns. Probe down the ranked list only until _TRANSCRIBE_LIMIT
    # candidates survive; a vanished file still falls through to the
    # next-best one, so the outcome is unchanged and the stat count is capped
    # by _TRANSCRIBE_PROBE_CAP instead of the SQL LIMIT.
    picked: list[dict] = []
    for r in out[:_TRANSCRIBE_PROBE_CAP]:
        if not (r["archive_path"] or "").strip() or not Path(r["archive_path"]).is_file():
            continue  # file gone — whisper would fail immediately
        picked.append(r)
        if len(picked) >= _TRANSCRIBE_LIMIT:
            break
    return picked


def _maybe_enrich(
    *, platform: Optional[str], channel: Optional[str], source: str, q: str,
    video_id: Optional[str] = None,
) -> list[dict]:
    """Targeted background enrichment for the search scope.

    Chat half: kick chat backfill for chat-less Twitch VODs (existing
    behavior + title-relevance ordering; single-video scope when the search
    is video-scoped). Transcript half: enqueue ONE transcribe job for the
    best eligible YouTube video. Runs inline (a few indexed SELECTs, at most
    one INSERT, one create_task) — never awaited, never blocks the search
    response. Returns what was actually kicked as the response's
    'enriching' list ({platform, video_id, kind, channel, title}); empty
    when idle."""
    enriching: list[dict] = []
    source_set = {s for s in source.split(",") if s}
    if "both" in source_set or not source_set:
        source_set = {"chat", "transcript", "video"}
    if "chat" in source_set:
        for r in _maybe_auto_backfill(
            platform=platform, channel=channel, source=source, q=q,
            video_id=video_id,
        ):
            enriching.append({
                "platform": "twitch",
                "video_id": r["video_id"],
                "kind": "chat",
                "channel": r["channel"] or "",
                "title": r["title"] or "",
            })
    if "transcript" in source_set:
        for r in _transcribe_candidates(platform=platform, channel=channel, q=q):
            job_id = f"transcribe-{r['platform']}-{r['video_id']}"
            try:
                # Transcript searches are the user actively asking for
                # whisper work -> top priority (the worker's ORDER BY
                # priority DESC picks these before the scheduler's
                # background queue).
                archive_db.enqueue_job(
                    job_id, "transcribe", r["platform"], r["video_id"],
                    priority=TRANSCRIBE_PRIORITY_HIGH,
                )
            except sqlite3.IntegrityError:
                # Already queued (scheduler or an earlier search) — bump it
                # to the front so this search's transcript still jumps the
                # queue.
                archive_db.execute(
                    "UPDATE archive_jobs SET priority = ? "
                    "WHERE id = ? AND status = 'queued'",
                    (TRANSCRIBE_PRIORITY_HIGH, job_id),
                )
                continue
            with _transcribe_lock:
                _last_transcribe_kick = time.monotonic()
                _transcribe_attempted_at[r["video_id"]] = time.monotonic()
            enriching.append({
                "platform": r["platform"],
                "video_id": r["video_id"],
                "kind": "transcribe",
                "channel": r["channel"] or "",
                "title": r["title"] or "",
            })
    return enriching


def _require_platform(platform: str) -> str:
    p = (platform or "").strip().lower()
    if p not in archive_db.PLATFORMS:
        raise HTTPException(status_code=400, detail=f"platform must be one of {archive_db.PLATFORMS}")
    return p


def _is_iso_date(value: str) -> bool:
    """True for a real calendar date in strict YYYY-MM-DD (2026-02-30 is
    rejected). The regex gate matters: Python 3.11's date.fromisoformat()
    also accepts 'YYYYMMDD', which SQLite's date() silently turns into NULL
    and would make searches return 0 hits instead of a 400."""
    import re
    from datetime import date

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
        return True
    except ValueError:
        return False


@router.get("/api/archive/videos")
async def archive_videos(platform: str | None = None, channel: str | None = None):
    videos = archive_db.list_videos(platform, channel)
    # Age-gated videos show WHY they have no captions and what would fix it.
    # The marker alone (captions_unavailable_at) is indistinguishable from an
    # ordinary "no captions" verdict, which is what made the age-gate loop
    # invisible to the user — the stored kind is what separates them, and it
    # lives on the row, so this still answers after a restart. `code` is the
    # stable contract; `reason` is the sentence derived from it.
    parked = _age_parked_map()
    if parked:
        for v in videos:
            code = parked.get(str(v.get("video_id") or ""))
            if code:
                v["captions_parked_reason_code"] = code
                v["captions_parked_reason"] = _age_gate_park_text(code)
    return {"videos": videos}


@router.post("/api/archive/videos")
async def archive_videos_upsert(video: dict):
    try:
        archive_db.upsert_video(video)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"missing required field: {exc}") from exc
    if (video.get("status") or "") == "ready":
        archive_db.maybe_enqueue_transcribe(
            str(video.get("platform") or ""),
            str(video.get("video_id") or ""),
            archive_path=video.get("archive_path"),
        )
    return {"ok": True}


@router.post("/api/archive/messages")
async def archive_messages(platform: str, video_id: str, body: Any = Body(...)):
    _require_platform(platform)
    if not video_id:
        raise HTTPException(status_code=400, detail="video_id required")
    # Accept both raw array (legacy) and {messages: [...]} (documented contract).
    messages = body.get("messages") if isinstance(body, dict) and "messages" in body else body
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="body must be a list or {messages: [...]}")
    count = archive_db.insert_messages(platform, video_id, messages)
    return {"ok": True, "inserted": count}


@router.post("/api/archive/transcripts")
async def archive_transcripts(platform: str, video_id: str, body: Any = Body(...)):
    _require_platform(platform)
    if not video_id:
        raise HTTPException(status_code=400, detail="video_id required")
    segments = body.get("segments") if isinstance(body, dict) and "segments" in body else body
    if not isinstance(segments, list):
        raise HTTPException(status_code=400, detail="body must be a list or {segments: [...]}")
    count = archive_db.insert_transcript(platform, video_id, segments)
    return {"ok": True, "inserted": count}


@router.get("/api/archive/search")
async def archive_search(
    q: str = Query(""),
    platform: str | None = None,
    channel: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    kind: str | None = None,
    source: str = "both",
    video_id: str | None = None,
    lang: str | None = None,
    username: str | None = None,
    # Ceiling matches the UI's documented literal-search cap (300 rows, a
    # full scroll of variety) with headroom for a narrower, hand-made call.
    # It used to be 100000, which made archive_db.fetch = limit*3 = 300k rows
    # PER TABLE per pass (a full bm25 rank + sort) and serialised the whole
    # thing as JSON on every keystroke.
    limit: int = Query(20, ge=1, le=1000),
    hint: bool = Query(True),
    semantic: bool = Query(False),
    # Defaults mirror archive_db.search(): an omitted mode is 'broad' (fuzzy
    # expansion), NOT the old exact-phrase default.
    mode: str = Query("broad"),
):
    # Semantic (embedding) search is expensive per candidate — its cap stays
    # tight. Literal-word queries may page through every match ("infinite
    # results"): the frontend asks for a large limit and renders incrementally.
    if not isinstance(mode, str):
        mode = "broad"
    mode = (mode or "broad").strip().lower()
    if mode not in ("exact", "broad", "semantic"):
        mode = "broad"
    if mode == "semantic":
        semantic = True
    if semantic:
        limit = min(limit, 100)
    # platform/kind accept comma-separated lists ("twitch,kick").
    for p in (platform or "").split(","):
        if p.strip():
            _require_platform(p)
    for label, value in (("date_from", date_from), ("date_to", date_to)):
        if value and not _is_iso_date(value):
            raise HTTPException(status_code=400, detail=f"{label} must be YYYY-MM-DD")
    _KIND_OK = set(archive_db.KINDS) | {"video"}
    bad_kinds = [
        k for k in (k.strip().lower() for k in (kind or "").split(","))
        if k and k not in _KIND_OK
    ]
    if bad_kinds:
        raise HTTPException(status_code=400, detail=f"kind must be one of {archive_db.KINDS}")
    # source restricts the content kinds searched: 'both' (default) = all,
    # or a comma-joined subset of chat/transcript/video ("video,transcript"
    # — the FE's multi-select source chips). 'video' = local video-title
    # matches only. channel accepts comma-separated slugs ("a,b" → IN
    # match) but never empty segments.
    source = (source or "both").strip().lower()
    source_tokens = [s for s in source.split(",") if s]
    if not source_tokens:
        source_tokens = ["both"]
    bad_sources = [s for s in source_tokens if s not in ("both", "chat", "transcript", "video")]
    if bad_sources:
        raise HTTPException(
            status_code=400,
            detail="source must be one of both, chat, transcript, video (or a comma-joined subset)",
        )
    # Normalize: 'both' (or any mixture containing it) means everything.
    if "both" in source_tokens:
        source = "both"
    if channel and any(not s.strip() for s in channel.split(",")):
        raise HTTPException(status_code=400, detail="channel must be non-empty slugs")
    # username narrows to one or more chat authors — comma-separated
    # ("a,b" → OR set, '@' tolerated per token — YouTube stores the
    # @handle; Twitch/Kick store the displayed name). The chat-source
    # coercion happens inside archive_db.search(). With an empty q the
    # search becomes a pure author-history query, so at least one of
    # q/username must be present.
    username = (username or "").strip()
    un_tokens = [t.lstrip("@") for t in username.split(",") if t.strip()]
    if username and any(len(t) > 120 for t in un_tokens):
        raise HTTPException(status_code=400, detail="username too long")
    if not un_tokens:
        username = ""
    if not q.strip() and not username:
        raise HTTPException(status_code=400, detail="q or username required")
    if len(q) > 500:
        # Bound the fuzzy-expansion work: the FE never sends queries this
        # long, and an unbounded q (pasted novels, fuzzers) multiplies the
        # per-token vocab scans, the FTS5 phrase/AND MATCH sizes, and the
        # title pass (O(q_tokens × videos × title_tokens)).
        raise HTTPException(status_code=400, detail="q too long (max 500 characters)")
    # channel_hint: search() understands a leading channel-slug token (see
    # archive_db.search) and reports the matched slug through the out-param.
    # hint=False (UI dismissed the chip) disables the whole implicit-scope
    # pass — pass no out-param so search() never applies it.
    hint_box: list[str] = []
    # Bounded like the remote path below (wait_for, not a bare to_thread): a
    # pathological query — a 1-char token that expands to a huge fuzzy tier,
    # a broad-mode chain of passes over a multi-million-row archive, a DB
    # page thrashing under a concurrent transcribe — otherwise parks the
    # request forever and the panel's spinner never resolves. 30s is well
    # past any honest local search (the slowest measured hit on the real
    # archive was ~2.2s cold) and past the deepest sweep's own budget, so it
    # only fires when the pass is genuinely stuck. The thread is NOT
    # cancellable, but the response is abandoned and the search cache it
    # would have published is keyed to a generation that a later write retires.
    try:
        hits = await asyncio.wait_for(
            asyncio.to_thread(
                archive_db.search,
                q,
                platform=platform or None,
                channel=channel or None,
                date_from=date_from,
                date_to=date_to,
                kind=kind or None,
                source=source,
                video_id=video_id or None,
                lang=lang or None,
                limit=limit,
                semantic=semantic,
                mode=mode,
                _channel_hint_out=hint_box if hint else None,
                username=username or None,
            ),
            timeout=_SEARCH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        # Graceful, same style as the remote path: the FE surfaces `error` as
        # a retryable banner, never a 500.
        logger.warning("archive search timed out after %ss (q=%r mode=%s)", _SEARCH_TIMEOUT_S, q[:80], mode)
        return {
            "hits": [],
            "enriching": [],
            "error": "Search took too long — narrow it with a channel, date or kind filter.",
        }
    channel_hint = hint_box[0] if hint_box else None
    # Targeted enrichment: lazily kick chat backfill / enqueue transcribe
    # jobs for videos in scope. Runs inline but only fires background tasks;
    # the 'enriching' list reports what was actually kicked. The explicit
    # channel param wins over the hint for scoping.
    enriching: list[dict] = []
    try:
        from deps import settings_mgr  # lazy: keeps routers.archive import-light

        smart_enrich = bool(getattr(settings_mgr.get(), "archive_smart_enrich", True))
    except Exception:
        smart_enrich = True
    if smart_enrich and q.strip():
        # Empty q = author-history mode: never let enrichment kick a
        # transcribe/backfill job as a side effect of a pure username query.
        enriching = _maybe_enrich(
            platform=platform or None,
            channel=channel or channel_hint,
            source=source,
            q=q,
            video_id=video_id or None,
        )
    if enriching:
        threading.Thread(
            target=_start_frozen_archive_worker,
            daemon=True,
            name="archive-worker-kick",
        ).start()
    resp: dict[str, Any] = {"hits": hits, "enriching": enriching}
    if channel_hint:
        resp["channel_hint"] = channel_hint
    if username:
        _maybe_resolve_display_names()
    return resp


@router.get("/api/archive/search/remote")
async def archive_search_remote(
    q: str = Query(..., min_length=1),
    channel: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=50),
):
    """Channel-scoped YouTube title search (remote fallback).

    The local archive only indexes the newest ~100 uploads per saved channel
    (the panel fetch cap), so old series are unreachable locally. This runs
    the channel's own YouTube search tab via yt-dlp and returns flat hits in
    the archive-search hit shape (kind='youtube'). Fetch failures return
    [] + error (never a 500) — the UI surfaces it as a note.

    Gating contract lives in the FE (ArchiveSearchPopup): it only calls this
    endpoint for unfiltered queries on a saved channel with a YouTube handle;
    the backend intentionally does not re-check those conditions."""
    from deps import settings_mgr  # lazy: keeps routers.archive import-light

    handle: Optional[str] = None
    try:
        saved = settings_mgr.get().saved_channels or []
    except Exception:
        saved = []
    target = channel.strip().lower()
    for entry in saved:
        if not isinstance(entry, dict):
            continue
        slugs = [str(entry.get(k) or "").strip() for k in ("kickSlug", "twitchSlug", "youtubeSlug")]
        if any(s.lower() == target for s in slugs if s):
            handle = str(entry.get("youtubeSlug") or "").strip() or None
            break
    if not handle:
        return {"hits": [], "error": f"'{channel}' has no YouTube handle — set one on this channel's card (pencil / Edit button → channel links)"}
    from services.youtube_service import search_channel_videos_sync
    from deps import INFO_EXECUTOR

    try:
        items = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                INFO_EXECUTOR, search_channel_videos_sync, handle, q, max(1, min(int(limit), 50))
            ),
            timeout=25,
        )
    except asyncio.TimeoutError:
        return {"hits": [], "error": "YouTube search timed out — try again"}
    except Exception as exc:
        logger.debug("remote search failed: %s", exc)
        return {"hits": [], "error": "YouTube search failed"}
    hits = [
        {
            "kind": "youtube",
            "platform": "youtube",
            "video_id": v["id"],
            "offset_sec": 0,
            "text": v["title"],
            "score": 1.0,
            "lang": None,
            "channel": v.get("channel") or handle,
            "title": v["title"],
            "date": v.get("created_at"),
            "video_kind": "vod",
            "duration_sec": v.get("duration"),
            "duration_string": v.get("duration_string"),
            "thumbnail_url": v.get("thumbnail_url"),
        }
        for v in items
    ]
    return {"hits": hits, "error": None}


@router.get("/api/archive/videos/{platform}/{video_id}/chat")
async def archive_chat_window(
    platform: str,
    video_id: str,
    offset: float = 0.0,
    half: float = 30.0,
    limit: int = Query(archive_db.CHAT_FROM_OFFSET_LIMIT, ge=1, le=50_000),
    offsets: str | None = None,
):
    """Chat for the video's whole canonical dedupe group, merged by offset.

    half > 0 → the classic ±half nearby window per member, merged; truncated
    reports any member hitting its per-member row cap. half <= 0 → "from
    offset onward" per member, merged by
    offset_sec (platform order breaks equal-offset ties) and sliced to
    `limit` rows — truncated reports the cut. Pagination is a per-platform
    keyset: the response's `next_offsets` carries each member's last
    delivered offset_sec, and the next request echoes them back as
    `offsets` ("platform:sec,platform:sec"); members absent from the map
    resume from the global `offset`. platform/video_id stay on every row so
    the client can filter per platform. Single-platform groups behave
    exactly like the pre-group endpoint (one member, offsets map has one
    entry)."""
    _require_platform(platform)
    members = archive_db.chat_group_members(platform, video_id)
    platforms = [m["platform"] for m in members]
    # Per-member resume offsets ("twitch:100.5,kick:20"); unknown platforms
    # are dropped, malformed segments ignored — absent members use `offset`.
    resume: dict[str, float] = {}
    if offsets:
        for seg in offsets.split(","):
            seg = seg.strip()
            if ":" not in seg:
                continue
            p, _, raw = seg.partition(":")
            if p in resume or p not in platforms:
                continue
            try:
                resume[p] = float(raw)
            except ValueError:
                continue
    order = {p: i for i, p in enumerate(platforms)}
    if half is not None and half > 0:
        window: list[dict] = []
        truncated = False
        for m in members:
            msgs, cut = archive_db.chat_window(m["platform"], m["video_id"], offset, half, limit)
            window.extend(msgs)
            truncated = truncated or cut
        window.sort(key=lambda r: (r["offset_sec"], order[r["platform"]]))
        return {"messages": window, "truncated": truncated, "platforms": platforms, "next_offsets": {}}
    cap = max(1, int(limit))
    fetched: list[dict] = []
    truncated = False
    for m in members:
        msgs, cut = archive_db.chat_window(
            m["platform"], m["video_id"], resume.get(m["platform"], offset), 0.0, cap,
        )
        fetched.extend(msgs)
        truncated = truncated or cut
    fetched.sort(key=lambda r: (r["offset_sec"], order[r["platform"]]))
    delivered = fetched[:cap]
    truncated = truncated or len(fetched) > cap
    next_offsets: dict[str, float] = {}
    for m in members:
        own = [r for r in delivered if r["platform"] == m["platform"]]
        next_offsets[m["platform"]] = (
            own[-1]["offset_sec"] if own else resume.get(m["platform"], offset)
        )
    return {"messages": delivered, "truncated": truncated, "platforms": platforms, "next_offsets": next_offsets}


@router.post("/api/archive/videos/{platform}/{video_id}/chat/backfill")
async def archive_chat_backfill(platform: str, video_id: str):
    """Queue a background Twitch chat backfill for one archived VOD.

    Twitch-only (Kick/YouTube chats arrive via the queue/worker — Kick has
    no retro API; YouTube chat is enqueued at ingest). Status words:
    'queued' (task started now), 'running' (already in flight),
    'already' (chat rows exist), 'failed' (could not start)."""
    p = _require_platform(platform)
    if p != "twitch":
        raise HTTPException(status_code=400, detail="chat backfill is twitch-only")
    if not video_id or not str(video_id).isdigit():
        raise HTTPException(status_code=400, detail="video_id must be numeric")
    row = archive_db.query(
        "SELECT channel FROM videos WHERE platform=? AND video_id=?", (p, str(video_id))
    )
    if not row:
        raise HTTPException(status_code=404, detail="video not found")
    status = _kick_backfill(str(video_id), row[0]["channel"] or "")
    return {"ok": status != "failed", "status": status}


@router.get("/api/archive/videos/{platform}/{video_id}/transcript")
async def archive_transcript(platform: str, video_id: str):
    _require_platform(platform)
    # Cross-platform fallback: a video with no transcript rows of its own
    # serves its canonical twin's rows (youtube > twitch > kick), so a
    # Twitch VOD with a transcribed YouTube mirror shows its transcript in
    # the player's Transcript tab. source_platform/source_video_id tell the
    # UI where the rows came from (own rows -> the requested ids).
    src_platform, src_video_id = archive_db.transcript_source(platform, video_id) or (
        platform, video_id
    )
    return {
        "segments": archive_db.transcript_for(src_platform, src_video_id),
        "source_platform": src_platform,
        "source_video_id": src_video_id,
    }


@router.get("/api/archive/dedupe")
async def archive_dedupe():
    # content_groups: byte-identical media files (SHA-256) shared by >= 2
    # rows — the content-dedup layer, distinct from canonical_key groups.
    return {"groups": archive_db.dedupe_view(),
            "content_groups": archive_db.content_duplicates()}


@router.get("/api/archive/rate-budget")
async def archive_rate_budget():
    """Read-only view of the adaptive rate governor (no mutations, no reset).

    Shows the learned per-platform ceiling, the AUTO/USER token pools under
    it, the hot-path counters, and the last throttle decisions — enough to
    watch the governor learn (ceiling drops on a 429, creeps back on clean
    windows) without touching anything.
    """
    from services import rate_budget

    return rate_budget.status()


@router.post("/api/archive/aliases")
async def archive_aliases(platform: str, video_id: str, canonical_key: str, note: str = ""):
    _require_platform(platform)
    if not canonical_key.strip():
        raise HTTPException(status_code=400, detail="canonical_key required")
    archive_db.set_alias(platform, video_id, canonical_key.strip(), note)
    return {"ok": True}


# --- rate-limit history (read-only) ----------------------------------------
# The gates (yt_gate / kick_gate) freeze per-process and forget on restart.
# These two endpoints expose the DURABLE record so a later policy lane — or
# a human — can see when each platform limits us and whether the work that
# tripped it was background ('auto') or user-initiated ('user'). Read-only
# by design: this lane adds no way to arm or clear a gate from the API.


@router.get("/api/archive/rate-limits/recent")
async def archive_rate_limits_recent(
    platform: Optional[str] = None,
    since_hours: int = Query(24, ge=1, le=24 * 30),
    limit: int = Query(200, ge=1, le=2000),
):
    """Recent rate-limit / bot-gate events, newest first."""
    if platform is not None:
        _require_platform(platform)
    events = archive_db.recent_rate_limits(
        platform=platform, since_hours=since_hours, limit=limit,
    )
    return {"events": events, "count": len(events), "since_hours": since_hours}


@router.get("/api/archive/rate-limits/summary")
async def archive_rate_limits_summary(
    since_hours: int = Query(24, ge=1, le=24 * 30),
    platform: Optional[str] = None,
):
    """How often we get limited, per platform, split auto vs user work."""
    if platform is not None:
        _require_platform(platform)
    return archive_db.rate_limit_summary(
        since_hours=since_hours, platform=platform,
    )


@router.post("/api/archive/jobs/clear")
async def archive_jobs_clear():
    n = archive_db.clear_finished_jobs()
    return {"ok": True, "cleared": n}


@router.get("/api/archive/jobs")
async def archive_jobs(limit: int = Query(50, ge=1, le=500)):
    jobs = archive_db.list_jobs(limit)
    if jobs:
        # Progress UI (QueueTab polls this every 3s): enrich each row with
        # the video's display title — the jobs table stores only ids, and
        # the videos row may be absent (cleaned or never indexed). One
        # batched lookup; a per-row N+1 on a poll would be silly. Display
        # title prefers the WS-4 original (non-auto-translated) copy, the
        # same rule search hits use.
        pairs = [(j["platform"], j["video_id"]) for j in jobs]
        placeholders = ", ".join("(?, ?)" for _ in pairs)
        titles = {
            (r["platform"], r["video_id"]): r["title"]
            for r in archive_db.query(
                "SELECT platform, video_id, "
                "COALESCE(NULLIF(original_title, ''), title) AS title "
                f"FROM videos WHERE (platform, video_id) IN ({placeholders})",
                [v for p in pairs for v in p],
            )
        }
        for j in jobs:
            j["title"] = titles.get((j["platform"], j["video_id"]), "")
    return {"jobs": jobs}


@router.post("/api/archive/jobs")
async def archive_jobs_enqueue(job: dict):
    job_id = str(job.get("id") or "").strip()
    kind = str(job.get("kind") or "").strip()
    platform = str(job.get("platform") or "").strip()
    video_id = str(job.get("video_id") or "").strip()
    if not (job_id and kind and video_id):
        raise HTTPException(status_code=400, detail="id, kind and video_id required")
    _require_platform(platform)
    # Optional priority (the caller may ask for a tier explicitly); omitted
    # or unparseable keeps the historical default of 0 rather than 400ing a
    # client that never knew the field existed.
    try:
        priority = int(job.get("priority", 0))
    except (TypeError, ValueError):
        priority = 0
    try:
        archive_db.enqueue_job(job_id, kind, platform, video_id, priority=priority)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail=f"job {job_id} already exists") from None
    threading.Thread(
        target=_start_frozen_archive_worker,
        daemon=True,
        name="archive-worker-kick",
    ).start()
    return {"ok": True, "id": job_id}


# --- user control over the queue -----------------------------------------
# The asymmetry this closes: per-DOWNLOAD pause/resume/cancel existed
# (download_manager), while a TRANSCRIPTION had no controls at all — the
# only job endpoints were list / enqueue / clear. A user watching a
# 13-hour VOD had no way to say "not that one" or "do this one next".
#
# Every action is refused with 409 when the job is not in a state it can act
# on (running, done, failed). A RUNNING job is deliberately NOT preemptable:
# the executor is mid-decode and killing it would throw away the in-flight
# chunk, so pause/cancel/prioritise only apply to queued and paused rows.
def _job_or_409(job_id: str) -> dict:
    job = archive_db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return job


def _applied_or_409(job_id: str, applied: bool, action: str) -> dict:
    if applied:
        return {"ok": True, "id": job_id, "status": action}
    job = _job_or_409(job_id)
    raise HTTPException(
        status_code=409,
        detail=(
            f"cannot {action} job {job_id}: it is {job['status']} "
            "(running jobs are never preempted)"
        ),
    )


@router.post("/api/archive/jobs/{job_id}/pause")
async def archive_job_pause(job_id: str):
    return _applied_or_409(job_id, archive_db.pause_job(job_id), "paused")


@router.post("/api/archive/jobs/{job_id}/resume")
async def archive_job_resume(job_id: str):
    return _applied_or_409(job_id, archive_db.resume_job(job_id), "resumed")


@router.post("/api/archive/jobs/{job_id}/cancel")
async def archive_job_cancel(job_id: str):
    return _applied_or_409(job_id, archive_db.cancel_job(job_id), "cancelled")


@router.post("/api/archive/jobs/{job_id}/priority")
async def archive_job_priority(job_id: str, body: dict):
    """Re-prioritise a queued/paused job.

    Accepts a raw integer (0/100/200/300 — queue_policy's named tiers) or a
    tier NAME, so a client can say what it MEANS ('focus', 'preview',
    'search', 'background') instead of hardcoding a magic number. Unknown
    names fall back to the raw value, then to 400 if neither parses."""
    tier = str(body.get("tier") or "").strip().lower()
    tiers = {
        "background": queue_policy.PRIORITY_BACKGROUND,
        "search": queue_policy.PRIORITY_SEARCH,
        "preview": queue_policy.PRIORITY_PREVIEW,
        "focus": queue_policy.PRIORITY_FOCUS,
    }
    if tier in tiers:
        priority = tiers[tier]
    else:
        raw = body.get("priority", tier or None)
        try:
            priority = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="priority must be an integer or a tier name "
                       "(background/search/preview/focus)",
            ) from None
    return _applied_or_409(
        job_id, archive_db.set_job_priority(job_id, priority), "reprioritised"
    )


@router.post("/api/archive/focus")
async def archive_focus_stamp(body: dict):
    """Record that the user is interacting with an item right now.

    The worker consults this BEFORE claiming a transcribe job: while a focus
    is live, the focused VOD gets the transcribe queue and the others wait
    (chat/events keep draining). The record expires on its own after
    queue_policy.FOCUS_TTL_S, so a client that never sends a release — a
    closed tab, a crashed renderer — cannot wedge the queue.

    An empty body (or a missing video_id) RELEASES the focus, which is the
    natural 'the user navigated away' signal."""
    platform = str(body.get("platform") or "").strip().lower()
    video_id = str(body.get("video_id") or "").strip()
    if not video_id or platform not in archive_db.PLATFORMS:
        archive_db.clear_user_focus()
        return {"ok": True, "focus": None}
    archive_db.set_user_focus(platform, video_id)
    # Bump the focused item's job to the focus tier so it also wins the
    # priority ordering against older queued work. Never touches a running
    # job (set_job_priority refuses) — a running job is already past the
    # claim decision, which is the no-preemption invariant.
    try:
        archive_db.set_job_priority(
            f"transcribe-{platform}-{video_id}", queue_policy.PRIORITY_FOCUS
        )
    except Exception:
        logger.debug("focus priority bump skipped", exc_info=True)
    return {"ok": True, "focus": {"platform": platform, "video_id": video_id}}



from pydantic import BaseModel


class ChatExportRequest(BaseModel):
    platform: str
    video_id: str
    start_sec: float | None = None
    end_sec: float | None = None
    full: bool = True


@router.post("/api/archive/chat/export")
async def export_chat(body: ChatExportRequest):
    from services.download_sidecars import write_chat_sidecar
    from utils import download_kind_dir
    from deps import settings_mgr

    _require_platform(body.platform)
    opts = settings_mgr.get()
    dest_dir = download_kind_dir(opts, "chat")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{body.platform}-{body.video_id}.chat.txt"
    start = None if body.full else body.start_sec
    end = None if body.full else body.end_sec
    path = write_chat_sidecar(
        str(dest), body.platform, body.video_id, start_sec=start, end_sec=end,
    )
    if not path:
        raise HTTPException(status_code=404, detail="No chat history for this video")
    return {"path": path}


# --- Deep channel-transcript search (background sweep jobs) -----------------

# Searches the TRANSCRIPTS of every video of a channel (uploads + shorts +
# streams), not just titles. It is deliberately a job, not a request: a full
# sweep walks the channel tabs, skips videos already covered by the transcript
# cache, and fetches only what is missing — bounded concurrency (2) and
# yt-dlp pacing (1.5s) per the repo's YouTube bot-gate discipline. Fetched
# transcripts are written through to the same archive_db.transcripts cache the
# rest of the app uses (and mirrored into the subtitles LRU), so a second
# query on the same channel never re-downloads captions.

_DEEP_JOB_CAP = 50
_DEEP_TAB_LIMIT = 1000  # per-tab enumeration ask (== playlist ceiling)
# Hard cap on the DEDUPED merged enumeration across all three tabs. The
# sweep pages each tab past the first window (see _deep_enumerate) so a
# channel with >1000 uploads searchable deep is walked exhaustively up to
# this bound; truncation is reported honestly when the merge exceeds it.
_DEEP_ENUM_MAX = 5000
# Aggregate result bounds: _deep_match_chat/_deep_match_titles each stop at
# their per-source cap (chat / titles independently), and the job's combined
# `results` list stops adding beyond the total cap. Without these a sweep
# could emit one hit per matching message over up to _DEEP_ENUM_MAX videos
# with NO bound — a hot chat-heavy channel would accumulate an unbounded
# results list that the ~2s status poll then copies + JSON-serves every poll.
_DEEP_SOURCE_RESULT_CAP = 2000
_DEEP_TOTAL_RESULT_CAP = 10000
_DEEP_SNIPPET_PAD = 120
_DEEP_FETCH_CONCURRENCY = 2
_DEEP_MIN_GAP_S = 1.5  # mirrors archive_ytdlp._ORIGINAL_MIN_GAP_S
_DEEP_SQL_CHUNK = 500
_DEEP_RUNNING_CAP = 2  # concurrent sweeps across ALL channels (bot-gate discipline)
# Max concurrent channel caption-ingest pumps (channel-add + scheduler).
_DEEP_CAPTION_PUMP_CAP = 2
# Caption-ingest persistent cursor: keep the per-channel cursor fresh every
# N videos so a crash/restart loses at most that many caption fetches.
_DEEP_CURSOR_EVERY = 20
# Scope refresh TTL for the caption-ingest cursor store: the scheduler pass
# re-enumerates a channel's tabs at most once per interval, so a long
# backlog never re-crawls all three tabs on every 180s pass.
_DEEP_ENUM_CACHE_TTL_S = 180.0

_deep_jobs: dict[str, dict] = {}
_deep_jobs_lock = threading.Lock()
# Lazy deep_jobs-table ensure (per resolved DB path so a test that rebinds
# VODRIP_ARCHIVE_DB still creates the table on its fresh scratch DB).
_deep_jobs_tables_ok: set[str] = set()
# Per-channel in-memory enumerate cache (monotonic ts, items, truncated).
_deep_enumerate_cache: dict[str, tuple[float, list[dict], bool]] = {}
# Per-channel caption-ingest locks: settings channel-add and the scheduler
# pass may race on the same channel's cursor — serialize the sweep pump.
_caption_ingest_locks: dict[str, threading.Lock] = {}
_caption_ingest_locks_guard = threading.Lock()
# Fetch pacing is GLOBAL, not per-job: two sweeps from two clients must
# still start yt-dlp calls >=_DEEP_MIN_GAP_S apart. Closure-local pace
# would let each job blast YouTube independently.
_deep_pace = {"last": 0.0}
_deep_pace_lock = threading.Lock()
# Global count of concurrently-RUNNING channel caption-ingest pumps
# (channel-add + scheduler). Bounded so a burst of channel-adds can't blast
# the bot gate; the per-channel pump lock already prevents same-channel
# races, this caps cross-channel parallelism on top.
_caption_pump_active = 0
_caption_pump_active_lock = threading.Lock()

def _deaccent(text: str) -> str:
    """casefold + strip combining marks (á→a, ç→c) for accent-insensitive match."""
    import unicodedata

    return "".join(
        c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c)
    )


def _deaccent_map(text: str) -> tuple[str, list[int]]:
    """Like ``_deaccent`` but also returns an offset map: index in the
    deaccented string -> index in the ORIGINAL ``text``.

    NFKD casefold is not always length-preserving (ß→ss), so a match found
    over the deaccented string cannot be blindly re-sliced on the original —
    the offsets must be carried back through this map."""
    import unicodedata

    out: list[str] = []
    map_: list[int] = []
    for i, c in enumerate(text):
        for d in unicodedata.normalize("NFKD", c.casefold()):
            if not unicodedata.combining(d):
                out.append(d)
                map_.append(i)
    return "".join(out), map_


def _deep_snippet(text: str, start: int, end: int) -> str:
    """±_DEEP_SNIPPET_PAD window around the match, ellipsised at each cut."""
    lo = max(0, start - _DEEP_SNIPPET_PAD)
    hi = min(len(text), end + _DEEP_SNIPPET_PAD)
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    return prefix + text[lo:hi].strip() + suffix


def _deep_match_segments(raw_query: str, segments: list[tuple[float, str]]) -> list[dict]:
    """EVERY occurrence of the query in (start_sec, text) segments.

    LITERAL substring match over the deaccented/casefolded text (never
    regex — pt-BR queries like 'R$ 10' or '$100' must match literally, and
    no path may run a user-supplied pattern with the GIL held). Match
    offsets are mapped back to ORIGINAL text indices so the snippet keeps
    the accent characters; ts is the segment start in seconds. Returns one
    hit per occurrence (a caption may contain the query multiple times) —
    there is no per-video result cap: a deep query must surface every match
    it is asked about."""
    q = _deaccent(raw_query.strip())
    if not q:
        return []
    out: list[dict] = []
    for start_sec, text in segments:
        deaccented, offmap = _deaccent_map(text)
        idx = deaccented.find(q)
        while idx >= 0:
            # Map the match span back to original-text indices (the map is
            # monotone non-decreasing; end-offset is the char AFTER the
            # match).
            orig_start = offmap[idx]
            orig_end = offmap[idx + len(q) - 1] + 1
            out.append(
                {"ts": int(start_sec), "snippet": _deep_snippet(text, orig_start, orig_end)}
            )
            # Advance past this match so overlapping repeats (e.g. "aaa" in
            # "aaaa") still count each occurrence.
            idx = deaccented.find(q, idx + len(q))
    return out


def _deep_match_chat(raw_query: str, video_ids: list[str]) -> tuple[list[dict], bool]:
    """Query matches against archived CHAT messages of the swept videos.

    Plain chunked SELECT over the messages table (owned/compatible with the
    archive_db API — we call the public query() only, never its internals).
    Returns (hits, capped): one hit {"video_id", "ts", "snippet",
    "source": "chat"} per message whose text contains the query as a
    deaccented substring (a message counts as a single hit; its whole text
    is the snippet). Matching STOPS at _DEEP_SOURCE_RESULT_CAP, with
    `capped` True when the bound was reached — the caller surfaces it as
    truncated_results so a hot chat-heavy channel can't accumulate an
    unbounded hit list.
    """
    q = _deaccent(raw_query.strip())
    if not q or not video_ids:
        return [], False
    hits: list[dict] = []
    for i in range(0, len(video_ids), _DEEP_SQL_CHUNK):
        chunk = video_ids[i : i + _DEEP_SQL_CHUNK]
        ph = ",".join("?" * len(chunk))
        for r in archive_db.query(
            "SELECT video_id, offset_sec, text FROM messages "
            f"WHERE platform='youtube' AND video_id IN ({ph})",
            chunk,
        ):
            if len(hits) >= _DEEP_SOURCE_RESULT_CAP:
                return hits, True
            text = str(r["text"] or "")
            if q in _deaccent(text):
                hits.append({
                    "video_id": str(r["video_id"] or ""),
                    "ts": int(float(r["offset_sec"] or 0.0)),
                    "snippet": _deep_snippet(text, 0, len(text)),
                    "source": "chat",
                })
    return hits, len(hits) >= _DEEP_SOURCE_RESULT_CAP


def _deep_match_titles(raw_query: str, video_ids: list[str]) -> tuple[list[dict], bool]:
    """Query matches against video TITLES of the swept videos.

    Matches the deaccented concatenation of `title` and `original_title`
    (covers non-YT-dlp ingest that stored the raw uploader title separately).
    Returns (hits, capped): one hit {"video_id", "ts": 0, "snippet",
    "source": "title"} per matching video. First-placed because it neither
    needs a fetch nor a transcript — it always answers from the videos
    table. Matching STOPS at _DEEP_SOURCE_RESULT_CAP (a huge channel whose
    every title matches must not grow an unbounded list).
    """
    q = _deaccent(raw_query.strip())
    if not q or not video_ids:
        return [], False
    hits: list[dict] = []
    for i in range(0, len(video_ids), _DEEP_SQL_CHUNK):
        chunk = video_ids[i : i + _DEEP_SQL_CHUNK]
        ph = ",".join("?" * len(chunk))
        for r in archive_db.query(
            "SELECT video_id, title, original_title FROM videos "
            f"WHERE platform='youtube' AND video_id IN ({ph})",
            chunk,
        ):
            if len(hits) >= _DEEP_SOURCE_RESULT_CAP:
                return hits, True
            title = f"{str(r['title'] or '')} {str(r['original_title'] or '')}".strip()
            if title and q in _deaccent(title):
                hits.append({
                    "video_id": str(r["video_id"] or ""),
                    "ts": 0,
                    "snippet": _deep_snippet(title, 0, len(title)),
                    "source": "title",
                })
    return hits, len(hits) >= _DEEP_SOURCE_RESULT_CAP


def _deep_enumerate(handle: str) -> tuple[list[dict], bool, int]:
    """All channel videos (uploads+shorts+streams), newest-first, deduped.

    Seam for tests. Returns (items, truncated, enumerated_total): *items* is
    the deduped newest-first list (capped at _DEEP_ENUM_MAX), *truncated* is
    True when any tab failed, any window saturated its bound, or the merged
    set exceeded the hard cap (a partial sweep must never claim full
    coverage), and *enumerated_total* is the deduped merged COUNT BEFORE the
    hard-cap cut — consumers use it to report how far the crawl really got.

    Pagination: each tab is paged in _DEEP_TAB_LIMIT windows via the
    guarded flat extract's `start` offset (playlist_items window starting at
    `start+1`). A saturated window (raw crawl == the window bound) MAY have
    more pages behind it, so the walk pages forward; it STOPS when a window
    reports `saturated == False` (the tab is genuinely exhausted — a fully
    covered by the pages walked so far). Because a first saturated window is
    resolved by a later covered window, truncation is NOT set merely for
    seeing saturation — it is set only when the walk cannot cover the whole
    tab: a window errored, or the merged crawl hit the hard _DEEP_ENUM_MAX
    cap while a tab still reported saturation (the while-else below).

    The saturation asked for is the RAW crawl bound (list_order >=
    playlistend), not the show-more `has_more`: this sweep asks at the
    1000-row ceiling, where has_more is force-False by design (a deeper ask
    could never serve new rows) and would otherwise hide truncation.

    Governor: every window is admitted against the learned YouTube budget by
    the chokepoint itself (list_channel_videos_sync, source="auto"), which is
    where the single token per window is drawn. A refused window stops the
    WHOLE enumeration rather than the current tab: the pool is platform-wide,
    so continuing would pay the bounded wait once per remaining tab/window and
    stall the scheduler that drives this pass. A stop is reported as truncated
    — the next pass re-crawls from a fresh cursor.
    """
    from services import youtube_service
    from services.youtube_service import list_channel_videos_sync

    # The governor's refusal class, borrowed from the chokepoint that owns the
    # gate. Resolved here (not imported directly) so this file keeps working
    # if the governor module is unavailable.
    refusal = youtube_service._governor_refusal()
    governor_stop = False
    merged: dict[str, dict] = {}
    truncated = False
    for tab in ("videos", "shorts", "streams"):
        offset = 0
        while offset < _DEEP_ENUM_MAX:
            try:
                rows, _has_more, saturated = list_channel_videos_sync(
                    handle,
                    _DEEP_TAB_LIMIT,
                    start=offset,
                    playlist=tab,
                    enrich=False,
                    return_has_more=True,
                    return_crawl_saturation=True,
                    # This is the sweep's own background crawl, so it opts into
                    # the AUTO pool: it may pace a bounded wait rather than
                    # fail fast. A USER caller (the channel panel) does not.
                    source="auto",
                )
            except refusal as exc:
                # Budget exhausted — stop the WHOLE enumeration, not just this
                # tab. The pool is platform-wide, so the remaining tabs and
                # windows would each pay the bounded wait again: three tabs x
                # N windows is exactly the 20x-stall a batch loop must not do.
                # Coverage is honestly partial and the next pass re-crawls.
                #
                # No acquire() here on purpose: the ONE token for this window
                # was already drawn inside list_channel_videos_sync. This is
                # loop control only — gating the operation again from the
                # caller would double-charge the same logical walk.
                logger.info("deep enumerate stopped by the rate governor: %s", exc)
                truncated = True
                governor_stop = True
                break
            except Exception as exc:
                logger.debug("deep enumerate tab %s window %s failed: %s", tab, offset, exc)
                # A window that errored yielded NOTHING — the result set is
                # silently incomplete; report it as truncated (honest
                # partial) and stop paging this tab.
                truncated = True
                break
            for v in rows:
                vid = str(v.get("id") or "").strip()
                if vid and vid not in merged:
                    merged[vid] = v
            if len(merged) >= _DEEP_ENUM_MAX:
                # Hard cap — the tab (or cross-tab merge) has more we did
                # not crawl. Honest partial.
                truncated = True
                break
            if not bool(saturated):
                break  # tab exhausted: fully covered by pages walked
            offset += _DEEP_TAB_LIMIT
        else:
            # while exited at the offset cap while the tab NEVER reported
            # non-saturated — deeper windows exist beyond the cap we did
            # not crawl. Truncated.
            truncated = True
        if governor_stop:
            break
    items = list(merged.values())

    def _ts(v: dict) -> float:
        raw = str(v.get("created_at") or "")
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return 0.0

    items.sort(key=_ts, reverse=True)
    enumerated_total = len(items)
    return items, truncated, enumerated_total


def _deep_fetch_transcript(video_id: str) -> dict:
    """One caption fetch for the sweep; returns the subtitles payload.

    Seam for tests. Raises on transport/bot-gate failure (the runner
    classifies); an empty ``rows``/has_subtitles=False is a NO-CAPTIONS
    verdict, not an error."""
    from routers.subtitles import _fetch_subtitles, _subtitle_langs_default

    langs = [x.strip() for x in _subtitle_langs_default().split(",") if x.strip()]
    return _fetch_subtitles(f"https://www.youtube.com/watch?v={video_id}", langs)


def _deep_store_transcript(video_id: str, payload: dict) -> list[tuple[float, str]]:
    """Write-through the fetched captions into the shared transcript cache.

    Same table/lang semantics as the ingest path (insert_transcript), plus
    the subtitles LRU mirror (set for empty verdicts too) — the durable
    cache is the DB. Returns the (start_sec, text) segments for matching."""
    rows = [r for r in (payload.get("rows") or []) if (r.get("text") or "").strip()]
    segments: list[tuple[float, str]] = []
    # Mirror into the subtitles LRU BEFORE the no-captions early return, so
    # an empty verdict (rows==[], has_subtitles=False) is cached server-side
    # too — otherwise /subtitles re-fetches a video the sweep already probed
    # (the DB holds no row to short-circuit on). Best-effort; the DB is the
    # cache of record for real transcripts.
    try:
        from routers.subtitles import _MISS, _subs_cache

        if _subs_cache.get(video_id) is _MISS:
            _subs_cache.put(video_id, payload)
    except Exception:
        pass
    if not rows:
        return segments
    try:
        archive_db.insert_transcript(
            "youtube",
            video_id,
            [
                {
                    "seg_idx": i,
                    "start_sec": float(r.get("offset_sec") or 0.0),
                    "end_sec": float(
                        rows[i + 1].get("offset_sec")
                        if i + 1 < len(rows)
                        else (float(r.get("offset_sec") or 0.0) + 2.0)
                    ),
                    "text": str(r.get("text") or ""),
                }
                for i, r in enumerate(rows)
            ],
            lang=payload.get("lang"),
        )
    except Exception as exc:
        logger.debug("deep transcript cache write failed for %s: %s", video_id, exc)
    return [(float(r.get("offset_sec") or 0.0), str(r.get("text") or "")) for r in rows]


def _deep_covered_ids(video_ids: list[str]) -> tuple[set[str], set[str]]:
    """Batched pre-skip probe: (has_transcript, fresh no-captions marker).

    Two IN-chunk SELECTs total — never per-video existence checks (an
    800-video sweep would issue 800 round-trips otherwise)."""
    covered: set[str] = set()
    marked: set[str] = set()
    now = datetime.now(timezone.utc)
    for i in range(0, len(video_ids), _DEEP_SQL_CHUNK):
        chunk = video_ids[i : i + _DEEP_SQL_CHUNK]
        ph = ",".join("?" * len(chunk))
        for r in archive_db.query(
            f"SELECT DISTINCT video_id FROM transcripts WHERE platform='youtube' AND video_id IN ({ph})",
            chunk,
        ):
            covered.add(str(r["video_id"]))
        for r in archive_db.query(
            "SELECT video_id, captions_unavailable_at FROM videos "
            f"WHERE platform='youtube' AND video_id IN ({ph}) AND captions_unavailable_at IS NOT NULL",
            chunk,
        ):
            try:
                if now - datetime.fromisoformat(str(r["captions_unavailable_at"])) < timedelta(
                    seconds=_deep_marker_fresh_s()
                ):
                    marked.add(str(r["video_id"]))
            except (TypeError, ValueError):
                pass  # unparseable stamp — treat as absent (retry once)
    return covered, marked


def _deep_marker_fresh_s() -> float:
    from services.archive_scheduler import CAPTIONS_UNAVAILABLE_FRESH_S

    return float(CAPTIONS_UNAVAILABLE_FRESH_S)


def _deep_seed_video_rows(handle: str, videos: list[dict]) -> None:
    """Ensure every swept video has a videos row so the no-captions marker
    (UPDATE-only) can stick on it and hits carry title/date. Only missing
    ids are upserted — archive fields are never touched either way."""
    ids = [str(v.get("id") or "") for v in videos if v.get("id")]
    existing: set[str] = set()
    for i in range(0, len(ids), _DEEP_SQL_CHUNK):
        chunk = ids[i : i + _DEEP_SQL_CHUNK]
        ph = ",".join("?" * len(chunk))
        for r in archive_db.query(
            f"SELECT video_id FROM videos WHERE platform='youtube' AND video_id IN ({ph})",
            chunk,
        ):
            existing.add(str(r["video_id"]))
    for v in videos:
        vid = str(v.get("id") or "")
        if not vid or vid in existing:
            continue
        kind = {"video": "vod", "short": "short", "stream": "stream"}.get(
            str(v.get("content_kind") or ""), "vod"
        )
        try:
            archive_db.upsert_channel_video({
                "platform": "youtube",
                "video_id": vid,
                "channel": str(v.get("channel") or handle),
                "title": str(v.get("title") or ""),
                "kind": kind,
                "started_at": v.get("created_at"),
                "duration_sec": v.get("duration"),
                "duration_string": v.get("duration_string"),
                "views": v.get("views"),
                "thumbnail_url": v.get("thumbnail_url"),
            })
        except Exception as exc:
            logger.debug("deep seed row failed for %s: %s", vid, exc)


# --- deep_jobs persistence table --------------------------------------------
#
# One table backs BOTH consumers of the same monotonic caption work — deep
# channel-search sweeps (kind='deep') and the channel-add/scheduler caption
# ingest (kind='caption'). A cursor = index into the deduped newest-first
# enumerated video list: the index of the next video to process. Persisting
# it lets a restart resume mid-sweep instead of re-crawling every caption.
# The table lives OUTSIDE archive_db.SCHEMA (that file is SearchFix's) — we
# create it additively/idempotently through the app's schema-ready EXECUTE
# path, so it needs no schema migration hook.
_DEEP_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS deep_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'deep',
    handle TEXT NOT NULL,
    handle_norm TEXT NOT NULL,
    query TEXT NOT NULL,
    status TEXT NOT NULL,
    scanned INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    cursor INTEGER,
    truncated INTEGER NOT NULL DEFAULT 0,
    no_transcript INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    age_parked INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _ensure_deep_jobs_table() -> None:
    """Lazy-create deep_jobs on the resolved DB path. Keyed per DB path so a
    test that rebinds VODRIP_ARCHIVE_DB to a fresh scratch DB still gets the
    table (an unconditional module-global cache would skip it).

    age_parked is added ADDITIVELY for a deep_jobs table created before this
    column existed (CREATE TABLE IF NOT EXISTS is a no-op on it): a sweep
    parked its videos into the video rows regardless, so the count has to land
    somewhere durable too, or a recovered job reports 0 — the exact hole this
    column closes. Pre-existing rows default to 0 (that run's per-run count was
    never persisted; the per-video parks themselves are intact and reported
    through /api/archive/videos)."""
    path = str(archive_db._db_path())
    if path in _deep_jobs_tables_ok:
        return
    archive_db.execute(_DEEP_JOBS_DDL)
    try:
        cols = {str(r["name"]) for r in archive_db.query("PRAGMA table_info(deep_jobs)")}
        if "age_parked" not in cols:
            archive_db.execute(
                "ALTER TABLE deep_jobs ADD COLUMN age_parked INTEGER NOT NULL DEFAULT 0"
            )
    except sqlite3.Error as exc:
        logger.debug("deep_jobs age_parked migration failed: %s", exc)
    _deep_jobs_tables_ok.add(path)


def _deep_jobs_put(row: dict) -> None:
    """Upsert one deep_jobs row (deep-store cursor/resume state)."""
    _ensure_deep_jobs_table()
    archive_db.execute(
        """INSERT INTO deep_jobs
             (id, kind, handle, handle_norm, query, status, scanned, total,
              cursor, truncated, no_transcript, error, age_parked, started_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             kind=excluded.kind, handle=excluded.handle,
             handle_norm=excluded.handle_norm, query=excluded.query,
             status=excluded.status, scanned=excluded.scanned,
             total=excluded.total, cursor=excluded.cursor,
             truncated=excluded.truncated, no_transcript=excluded.no_transcript,
             error=excluded.error, age_parked=excluded.age_parked,
             updated_at=excluded.updated_at""",
        (
            row.get("id", ""), row.get("kind", "deep"),
            row.get("handle", ""), row.get("handle_norm", ""),
            row.get("query", ""), row.get("status", "running"),
            int(row.get("scanned", 0)), int(row.get("total", 0)),
            row.get("cursor"), int(row.get("truncated", 0)),
            int(row.get("no_transcript", 0)), row.get("error"),
            int(row.get("age_parked", 0)),
            row.get("started_at", datetime.now(timezone.utc).isoformat(timespec="seconds")),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )


def _deep_jobs_caption_get(handle_norm: str) -> Optional[dict]:
    """Current caption-ingest cursor row for a channel (latest by updated_at),
    or None — the channel has no captured cursor yet (enumerate fresh)."""
    _ensure_deep_jobs_table()
    rows = archive_db.query(
        """SELECT id, handle, handle_norm, query, status, scanned, total, cursor,
                  truncated, no_transcript, error, started_at, updated_at
           FROM deep_jobs WHERE kind='caption' AND handle_norm=?
           ORDER BY updated_at DESC LIMIT 1""",
        (handle_norm,),
    )
    return dict(rows[0]) if rows else None


def _deep_caption_lock(handle_norm: str) -> threading.Lock:
    """Per-channel pump lock (settings channel-add may race the scheduler
    pass on the same channel's cursor)."""
    with _caption_ingest_locks_guard:
        return _caption_ingest_locks.setdefault(handle_norm, threading.Lock())


# --- caption ingest (channel-add / scheduler) ------------------------------

def _deep_caption_created_key(v: dict) -> tuple[int, str]:
    """Sort key for the caption pump's OLDEST-FIRST walk: the ISO created_at
    (lexicographically comparable when present), missing mapped to the
    smallest sentinel so an unknown-date video is treated as OLDEST and
    processed first. Stable sorting keeps the enumerate's within-tab order
    for equal dates."""
    created = str(v.get("created_at") or "")
    return (0 if not created else 1, created)


def _run_channel_caption_ingest(
    handle: str, *, budget: Optional[int] = None
) -> dict:
    """Pump one channel's caption ingest forward from its persistent cursor.

    Scope = the FULL enumerated uploads+shorts+streams list (a fresh cursor
    enumerates all three tabs once; an existing cursor reuses the cached
    enumerate). Each pass processes up to *budget* videos that are not
    covered (_youtube_covered), respecting the global pace/gate, advancing
    the deep_jobs cursor so the next pass (or a post-restart resume)
    continues exactly where this one stopped. Shorts are NOT skipped a
    priori — they may carry captions worth archiving.

    Runs on a worker thread (never the event loop). Returns a stats dict.
    """
    norm = _deep_handle_norm(handle)
    lock = _deep_caption_lock(norm)
    with lock:
        from services import yt_gate

        now = time.monotonic()
        cached = _deep_enumerate_cache.get(norm)
        if cached is None or now - cached[0] >= _DEEP_ENUM_CACHE_TTL_S:
            try:
                raw_videos, truncated, _enumerated_total = _deep_enumerate(handle)
            except Exception as exc:
                logger.debug("caption ingest enumerate failed for %s: %s", handle, exc)
                return {"scanned": 0, "no_transcript": 0, "error": str(exc)[:200]}
            # OLDEST-FIRST for the pump (the raw _deep_enumerate is
            # newest-first, suited to the deep-search sweep that pre-receives
            # its whole id set — but this pump's persistent cursor is a
            # monotonic resume index that REQUIRES append-only ordering: a
            # newest-first sequence PREPENDS new uploads, which would land
            # them BELOW a stale cursor forever (they'd never be captioned),
            # and a cursor==len would no-op the pump permanently. Sorting
            # oldest-first (append-only) means every new upload lands at the
            # TAIL, so a resume with cursor < len always reaches it. Stable;
            # a missing created_at is treated as OLDEST (processed first).
            videos = sorted(raw_videos, key=_deep_caption_created_key)
            _deep_enumerate_cache[norm] = (now, videos, bool(truncated))
        else:
            videos, truncated = cached[1], cached[2]

        # One-time migration note: cursors persisted before the oldest-first
        # flip were indexes INTO the raw newest-first sequence, so after the
        # flip their positions shift once. That re-scan is cheap because the
        # covered-set probe (_deep_covered_ids) and the no-captions marker
        # short-circuit videos that already have transcripts/markers — no
        # remote fetch happens for already-processed videos, only the local
        # existence probe.

        if not videos:
            return {"scanned": 0, "no_transcript": 0, "truncated": False}

        row = _deep_jobs_caption_get(norm)
        cursor = int(row.get("cursor") or 0) if row else 0
        cursor = max(0, min(cursor, len(videos)))

        _deep_seed_video_rows(handle, videos)
        ids = [str(v.get("id") or "") for v in videos]
        # Same un-park as the deep sweep: an age-gated video is credential-
        # bound, so a signed-in session makes it a caption candidate again.
        _unpark_age_gated_if_authenticated()
        covered, _marked = _deep_covered_ids(
            ids[cursor:] if cursor < len(ids) else []
        )

        from services.archive_scheduler import _youtube_covered

        budget = _yt_default_budget() if budget is None else int(budget)
        spawned_budget = max(0, budget)
        processed = 0
        no_transcript = 0
        idx = cursor
        while idx < len(videos) and processed < spawned_budget:
            # Bot-gate freeze: park (do NOT burn budget, do NOT advance the
            # cursor) until the freeze lifts — a gated pump would otherwise
            # fail every fetch fast behind the wall and either pile markers
            # or walk the cursor past videos it never actually captioned.
            # The scheduler picks the channel up again once the gate clears.
            if yt_gate.youtube_gate_active():
                break
            v = videos[idx]
            vid = str(v.get("id") or "")
            if vid and vid not in covered:
                if not _youtube_covered(vid):
                    ok = _paced_caption_fetch(vid, handle)
                    if not ok and yt_gate.youtube_gate_active():
                        # A gate-classified fetch failure parks the pump and,
                        # crucially, does NOT advance idx/processed — the
                        # cursor stays on this unprocessed video so a later
                        # pass (or the scheduler, once the gate clears)
                        # retries it instead of permanently skipping it.
                        break
                    processed += 1
                    if not ok:
                        no_transcript += 1
                else:
                    covered.add(vid)
            idx += 1
            if (idx - cursor) % _DEEP_CURSOR_EVERY == 0:
                _deep_jobs_put({
                    "id": f"caption-{norm}", "kind": "caption",
                    "handle": handle, "handle_norm": norm, "query": "",
                    "status": "running", "scanned": idx,
                    "total": len(videos), "cursor": idx,
                    "truncated": int(bool(truncated)),
                    "no_transcript": no_transcript, "error": None,
                })
        _deep_jobs_put({
            "id": f"caption-{norm}", "kind": "caption",
            "handle": handle, "handle_norm": norm, "query": "",
            "status": "running", "scanned": idx, "total": len(videos),
            "cursor": idx, "truncated": int(bool(truncated)),
            "no_transcript": no_transcript, "error": None,
        })
        return {
            "scanned": idx - cursor,
            "processed": processed,
            "no_transcript": no_transcript,
            "truncated": bool(truncated),
            "cursor": idx,
        }


def _yt_default_budget() -> int:
    from services.archive_scheduler import _yt_ingest_budget

    return int(_yt_ingest_budget())


# --- age-gated caption park (reversible) -------------------------------------
#
# An age-gated video is a TERMINAL no-captions verdict for an anonymous
# request: YouTube answers "Sign in to confirm your age" and no amount of
# retrying substitutes for credentials (no anonymous player client passes it
# anymore — see youtube_diag.is_age_gate_error). It must NOT be confused with
# the IP-level bot gate: the refusal text contains the gate's "sign in to
# confirm" marker, so yt_gate.classify_youtube_gate_error claims it and it
# lands in the branch that deliberately writes NO per-video marker ("that is
# IP state, not a verdict about the video"). Correct for a bot wall, fatal
# here: the same video was re-attempted on an endless ~30-minute loop — 33
# identical rate-limit events in 24h, all naming one video.
#
# Parked the way the transcribe path already parks an age-gated JOB
# (archive_ytdlp.ingest_video -> AGE_GATE_JOB_MARKER -> archive_db.update_job
# terminal check -> archive_scheduler._requeue_failed_transcribe_job): terminal
# NOW, reversible LATER. The marker is the existing videos.captions_unavailable_at
# stamp this sweep already honours — NOT transcript_kind='blocked', which is
# the irreversible ASR verdict. It is cleared by any successful caption
# ingest, and _unpark_age_gated_if_authenticated() clears it the moment an
# authenticated YouTube session exists (the same predicate the sibling fix and
# the cookie-bridge `youtube_authenticated` signal use).
#
# The park is read from the video row, never from a process-lifetime set: an
# earlier version kept {video_id: reason} in a module dict, so the UI rendered
# the parked state until the app restarted and then silently forgot it (and
# reported age_parked 0 for every DB-recovered job). The marker row is the only
# state; the sentence the UI shows is derived from the stored code below.


def _age_gate_park_code() -> str:
    """WHICH age-gate park this is, as a persisted code.

    Deliberately the same two states the age gate already reports elsewhere
    (youtube_diag.age_gate_actionable_message on the download/transcribe job
    error, and the cookie-bridge `youtube_authenticated` signal): no session
    configured vs a session that was rejected. They need different user
    actions, and the caption sweep is the path that actually hit the gate, so
    the classification has to name the remedy here too.

    A CODE, not a sentence: this value is what lands in
    videos.captions_unavailable_kind, and the sentence the UI renders is
    derived from it at read time (_age_gate_park_text). A client can then
    branch on the state without matching English prose."""
    try:
        from services.youtube_session import youtube_session_configured

        configured = bool(youtube_session_configured())
    except Exception:
        # A failed probe must never claim "you are signed in".
        configured = False
    if not configured:
        return archive_db.CAPTIONS_PARK_AGE_GATE_NO_SESSION
    return archive_db.CAPTIONS_PARK_AGE_GATE_SESSION_REJECTED


def _age_gate_park_text(code: str) -> str:
    """The user-facing reason for a persisted park code.

    Wording is caption-specific — the download path's "cannot be
    downloaded/watched" verb is wrong for a video that simply has no captions.
    Derived from the STORED code, never from a fresh probe: the park records
    what was true when the video was gated, and the text must match that code
    or the UI would describe a state the row does not hold."""
    if code == archive_db.CAPTIONS_PARK_AGE_GATE_SESSION_REJECTED:
        return (
            "Age-restricted video — the configured YouTube session was rejected "
            "(YouTube rotates account cookies while a YouTube tab is open), so no "
            "captions could be read. Sign in again from a private window via "
            "Settings > Cookie Bridge, then re-run the caption sweep."
        )
    return (
        "Age-restricted video — YouTube serves no captions to an "
        "anonymous request, and no signed-in YouTube session is "
        "configured. Open Settings > Cookie Bridge, sign in to YouTube, "
        "then re-run the caption sweep."
    )


def _park_age_gated(video_id: str) -> str:
    """Park one age-gated video: per-video marker + the user-facing reason.

    The marker (captions_unavailable_at) plus its age-gate classification is
    what terminates the retry AND what makes the park durable: the sweep's
    covered/marked probe (_deep_covered_ids) and the scheduler's
    _youtube_covered both pre-skip a fresh marker, so the video is attempted
    once and then left alone — and the UI can still say WHY after a restart,
    because the state it reads is the row, not a process-lifetime set. Returns
    the reason for the caller to surface."""
    code = _age_gate_park_code()
    vid = str(video_id or "")
    try:
        archive_db.mark_captions_unavailable("youtube", vid, kind=code)
    except Exception:
        logger.debug("age-gate park marker failed for %s", vid, exc_info=True)
    logger.info("youtube %s age-gated — parked (no captions without a signed-in session)", vid)
    return _age_gate_park_text(code)


def _age_parked_map(*, fresh_only: bool = True) -> dict[str, str]:
    """{video_id: park code} straight from the video rows.

    The park is PERSISTED, so this is the same set before and after a restart
    and for a job recovered from the database. fresh_only honours the same
    cooldown the sweep does: a stamp older than the no-captions freshness
    window is a re-attempt candidate, not a park, so it must not be reported to
    the user as one (it would claim a sign-in is still pending when the next
    sweep is already retrying the video on its own)."""
    try:
        rows = archive_db.age_gate_parked_videos(
            "youtube",
            fresh_seconds=_deep_marker_fresh_s() if fresh_only else None,
        )
    except Exception:
        logger.debug("age-parked read failed", exc_info=True)
        return {}
    return {r["video_id"]: r["kind"] for r in rows}


def _unpark_age_gated_if_authenticated() -> int:
    """Release age-gated caption parks once a signed-in YouTube session exists.

    The mirror of archive_scheduler._requeue_failed_transcribe_job: an age
    gate is credential-bound, so it resolves the moment an authenticated
    session appears. Clearing the marker makes the video a caption-sweep
    candidate again — that is the difference between "sign in and it works"
    and a video that never processes. Returns how many were released.

    Reads the PERSISTED park list, not an in-process set: the release has to
    work for a park written before the restart, or the promise the park text
    makes ("sign in and re-run the sweep") would be a lie across exactly the
    restart the park now survives."""
    try:
        from services.youtube_session import youtube_session_configured

        if not youtube_session_configured():
            return 0
    except Exception:
        return 0  # probe failed — stay parked rather than hammer
    released = 0
    for row in archive_db.age_gate_parked_videos("youtube"):
        vid = row["video_id"]
        try:
            archive_db.clear_captions_unavailable("youtube", vid)
        except Exception:
            logger.debug("age-gate un-park failed for %s", vid, exc_info=True)
            continue
        released += 1
    if released:
        logger.info(
            "released %d age-gated caption park(s) — authenticated YouTube session present",
            released,
        )
    return released


def _age_parked_snapshot() -> dict[str, str]:
    """{video_id: reason} for the parked age gates, for the API surface.

    Derived from the persisted marker rows, so it survives a restart: the old
    process-lifetime set made the UI forget the park (and the count) the moment
    the app restarted. Reason text is derived from the stored code."""
    return {
        vid: _age_gate_park_text(code) for vid, code in _age_parked_map().items()
    }


def _paced_caption_fetch(video_id: str, handle: str) -> bool:
    """One paced caption fetch+store for the channel caption ingest.

    Returns True when a transcript (>=1 segment) was stored, False on a
    no-captions verdict or failure (the failure stamps the negative marker
    so it is not re-fetched for a day). Bot-gate classification mirrors the
    deep-search worker: transport errors are stamped; IP-gate errors park
    and are NOT stamped as video verdicts; an age gate is parked as a
    reversible per-video verdict (see the note above _park_age_gated)."""
    from services import yt_gate
    from services.youtube_diag import is_age_gate_error

    with _deep_pace_lock:
        now = time.monotonic()
        start_at = max(now, _deep_pace["last"] + _DEEP_MIN_GAP_S)
        _deep_pace["last"] = start_at
    while time.monotonic() < start_at:
        time.sleep(0.1)
    try:
        payload = _deep_fetch_transcript(video_id)
    except Exception as exc:
        # Age gate BEFORE the gate classifier: its text contains the bot
        # gate's "sign in to confirm" marker, so the classifier would claim
        # it and this video would be re-attempted forever.
        if is_age_gate_error(exc):
            _park_age_gated(video_id)
            return False
        if yt_gate.classify_youtube_gate_error(exc):
            yt_gate.note_youtube_gate(
                str(exc)[:200], surface="captions", origin="auto",
            )
            return False
        try:
            archive_db.mark_captions_unavailable("youtube", video_id)
        except Exception:
            pass
        return False
    segments = _deep_store_transcript(video_id, payload)
    if segments:
        try:
            archive_db.clear_captions_unavailable("youtube", video_id)
        except Exception:
            pass
        return True
    try:
        archive_db.mark_captions_unavailable("youtube", video_id)
    except Exception:
        pass
    return False


def _start_caption_ingest_channel(handle: str) -> None:
    """Fire-and-forget a channel's caption-ingest pump (channel-add path)."""
    global _caption_pump_active
    if not handle or not str(handle).strip():
        return
    # Bot-gate freeze: skip — the scheduler picks this channel up again once
    # the gate clears (mirror archive_scheduler._ingest_youtube). Starting a
    # pump now would fail every fetch fast behind the wall.
    from services.yt_gate import youtube_gate_active

    if youtube_gate_active():
        return
    # Cross-channel pump cap: skip when _DEEP_CAPTION_PUMP_CAP pumps are
    # already running (the per-channel pump lock still prevents races on the
    # SAME channel; this bounds parallel channels so a burst of channel-adds
    # can't blast the gate). The saved-caption sweep is drained in later
    # passes by the scheduler.
    with _caption_pump_active_lock:
        if _caption_pump_active >= _DEEP_CAPTION_PUMP_CAP:
            return
        _caption_pump_active += 1

    def _pump() -> None:
        try:
            _run_channel_caption_ingest(str(handle).strip())
        finally:
            with _caption_pump_active_lock:
                global _caption_pump_active
                _caption_pump_active = max(0, _caption_pump_active - 1)

    threading.Thread(
        target=_pump,
        daemon=True, name=f"caption-ingest-{_deep_handle_norm(str(handle))[:12]}",
    ).start()


def _deep_set(job: dict, **fields: Any) -> None:
    with _deep_jobs_lock:
        job.update(fields)
        if fields.get("status") not in (None, "running"):
            # Every terminal transition unparks: a paused-then-cancelled
            # (or done/errored) job must never serve stale paused=true.
            ev = job.get("paused")
            if ev is not None:
                ev.clear()


def _run_deep_job(job_id: str, handle: str, query: str) -> None:
    """Sweep thread body: enumerate -> batch pre-skip -> cached matches ->
    paced 2-way caption fetches -> write-through -> match. Per-video errors
    skip+count; only a total enumeration failure errors the job."""
    from services import yt_gate
    from services.youtube_diag import is_age_gate_error

    with _deep_jobs_lock:
        job = _deep_jobs.get(job_id)
    if job is None:
        return
    cancel: threading.Event = job["cancel"]
    results: list[dict] = []
    counters = {"scanned": 0, "no_transcript": 0, "age_parked": 0}
    counters_lock = threading.Lock()
    # Persisted cursor: how far _scanned_ (so a post-restart resume, or a
    # status poll that finds no in-memory job, recovers a truthful position).
    # Throttled to _DEEP_CURSOR_EVERY rows so a long backlog never writes the
    # table on every single video.
    _last_persisted_scanned = 0

    def _bump(scanned: int = 0, missing: int = 0) -> None:
        """Counters are touched by both fetch workers — `+=` on a shared dict
        is not atomic, so the update is serialised."""
        with counters_lock:
            counters["scanned"] += scanned
            counters["no_transcript"] += missing

    def _bump_age_parked() -> None:
        """Videos parked this sweep because YouTube age-gated them. Counted
        separately from no_transcript so the UI can say WHY they have no
        captions (credentials, not "nothing to archive")."""
        with counters_lock:
            counters["age_parked"] += 1

    def _persist() -> None:
        """Best-effort deep_jobs upsert for THIS sweep (id = job_id). Called
        throttled from the scan loops and unconditionally at every terminal
        transition and after enumerate, so a crash/restart loses at most a
        _DEEP_CURSOR_EVERY window of cursor progress (transcripts themselves
        are already persisted via _deep_store_transcript)."""
        try:
            with _deep_jobs_lock:
                _scanned, _no_transcript = counters["scanned"], counters["no_transcript"]
                _age_parked_n = counters["age_parked"]
                _total = job.get("total", 0)
                _trunc = int(bool(job.get("truncated", False)))
                _status = job.get("status", "running")
                _err = job.get("error")
            _deep_jobs_put({
                "id": job_id, "kind": "deep",
                "handle": handle, "handle_norm": _deep_handle_norm(handle),
                "query": query, "status": _status,
                "scanned": _scanned, "total": _total, "cursor": _scanned,
                "truncated": _trunc, "no_transcript": _no_transcript,
                # Persisted next to its sibling per-run counters: the parks
                # themselves are on the video rows, but a per-RUN count is not
                # reconstructible from them (they outlive the run, are shared
                # with other sweeps, and are cleared on release) — so it is
                # stored where no_transcript already lives, not in a
                # process-lifetime set.
                "age_parked": _age_parked_n,
                "error": _err,
            })
        except Exception as exc:
            logger.debug("deep persist failed %s: %s", job_id, exc)

    def _flush() -> None:
        nonlocal _last_persisted_scanned
        with counters_lock, _deep_jobs_lock:
            job["scanned"] = counters["scanned"]
            job["no_transcript"] = counters["no_transcript"]
            job["age_parked"] = counters["age_parked"]
            job["truncated_results"] = _truncated_results
            job["results"] = list(results)
        # Throttle the persisted cursor to _DEEP_CURSOR_EVERY scanned rows;
        # terminal transitions call _persist() directly (unthrottled) right
        # before they flip job status.
        if job["scanned"] - _last_persisted_scanned >= _DEEP_CURSOR_EVERY:
            _last_persisted_scanned = job["scanned"]
            _persist()

    paused: threading.Event = job["paused"]

    def _wait_pause() -> bool:
        """Park while the sweep is paused. Cancel still wins: the loop
        re-checks cancel so a paused sweep stays cancellable."""
        while paused.is_set() and not cancel.is_set():
            time.sleep(0.2)
        return not cancel.is_set()

    def _wait_gate() -> bool:
        """Park while the IP-level bot gate is frozen. False = cancelled."""
        while yt_gate.youtube_gate_active() and not cancel.is_set():
            time.sleep(2.0)
        return not cancel.is_set()

    def _wait_pace() -> bool:
        """Serialise fetch STARTS at >=_DEEP_MIN_GAP_S apart across ALL
        sweeps (2 workers may overlap in flight, but YouTube never sees
        back-to-back starts — even from a second concurrent job)."""
        deadline = 0.0
        with _deep_pace_lock:
            now = time.monotonic()
            start_at = max(now, _deep_pace["last"] + _DEEP_MIN_GAP_S)
            _deep_pace["last"] = start_at
            deadline = start_at
        while time.monotonic() < deadline:
            if cancel.is_set():
                return False
            time.sleep(0.1)
        return not cancel.is_set()

    def _add_result(v: dict, m: dict) -> None:
        hit = {
            "id": str(v.get("id") or ""),
            "title": str(v.get("title") or ""),
            "url": str(v.get("url") or f"https://www.youtube.com/watch?v={v.get('id')}"),
            "date": (str(v.get("created_at") or "")[:10] or None),
            "ts": m["ts"],
            "snippet": m["snippet"],
            "source": m.get("source", "transcript"),
        }
        kind = str(v.get("content_kind") or "").strip()
        if kind:
            # Same vocabulary as the videos.kind rows the unified search
            # ships (vod, not video) — the FE kind chips compose over it.
            hit["video_kind"] = {"video": "vod"}.get(kind, kind)
        if len(results) >= _DEEP_TOTAL_RESULT_CAP:
            _set_truncated_results(True)
            return
        results.append(hit)

    def _add_source_hits(videos_by_id: dict[str, dict], source: tuple[list[dict], bool]) -> None:
        """Chat/title hits carry only video_id; resolve them to the swept
        video dict (fallback: an empty video stub) so _add_result can ship
        a uniform hit shape. `source` is the matcher's (hits, capped) pair;
        a capped source flags the job's results as truncated."""
        hits, capped = source
        if capped:
            _set_truncated_results(True)
        for m in hits:
            if len(results) >= _DEEP_TOTAL_RESULT_CAP:
                _set_truncated_results(True)
                return
            vid = m.get("video_id") or ""
            v = videos_by_id.get(vid) or {"id": vid, "title": None, "url": None, "created_at": None}
            _add_result(v, m)

    _truncated_results = False

    def _set_truncated_results(flag: bool) -> None:
        nonlocal _truncated_results
        if flag:
            _truncated_results = True

    try:
        videos, truncated, enumerated_total = _deep_enumerate(handle)
        if not _wait_pause():
            _deep_set(job, status="cancelled")
            _persist()
            return
        if not videos:
            _deep_set(job, status="error", error="channel enumeration returned no videos")
            _persist()
            return
        with _deep_jobs_lock:
            job["total"] = len(videos)
            job["truncated"] = bool(truncated)
            job["enumerated_total"] = enumerated_total
        _deep_seed_video_rows(handle, videos)
        _persist()  # record total/enumerated_total up front
        # Release any age-gated park before the covered/marked probe, so a
        # video parked while the user was signed out becomes a candidate again
        # the moment they sign in (the probe reads the marker this clears).
        _unpark_age_gated_if_authenticated()
        ids = [str(v.get("id") or "") for v in videos]
        videos_by_id = {
            str(v.get("id") or ""): v
            for v in videos
            if str(v.get("id") or "")
        }
        covered, marked = _deep_covered_ids(ids)

        # Pass 0 — titles + chat. Run ONCE over the FULL swept id set (not
        # just the uncached tail): a query present only in a title or in a
        # chat message must hit even when the video's transcript is already
        # cached or entirely absent. Both are pure DB reads (no fetch, no
        # transcript) so they bound the query's recall without cost.
        if not cancel.is_set() and _wait_pause():
            _add_source_hits(videos_by_id, _deep_match_titles(query, ids))
            _add_source_hits(videos_by_id, _deep_match_chat(query, ids))
            _flush()

        # Pass 1 — cached transcripts: match straight from the DB, zero
        # network. Marker-fresh videos are pre-skipped and counted.
        #
        # Restart-resume is handled by transcript persistence + the covered
        # probe below: a prefix the prior run already stored is `covered`
        # here, so pass 1 re-matches its cached segments (cheap DB read, the
        # ephemeral hits are rebuilt fresh) and pass 2 never re-fetches it —
        # only genuinely-uncovered videos hit the network. The persisted
        # `resume_cursor` is what status recovery reports (see
        # archive_search_deep_status).
        to_fetch: list[dict] = []
        for v in videos:
            if cancel.is_set() or not _wait_pause():
                break
            vid = str(v.get("id") or "")
            if not vid:
                continue
            if vid in marked:
                _bump(1, 1)
                continue
            if vid not in covered:
                to_fetch.append(v)
                continue
            segments = [
                (float(r.get("start_sec") or 0.0), str(r.get("text") or ""))
                for r in archive_db.transcript_for("youtube", vid)
            ]
            _bump(1, 0 if segments else 1)
            for m in _deep_match_segments(query, segments):
                _add_result(v, m)
        _flush()

        # Pass 2 — the uncached tail: paced, 2-concurrent caption fetches.
        def _handle_video(v: dict) -> None:
            vid = str(v.get("id") or "")
            if cancel.is_set() or not vid:
                return
            if not _wait_pause():
                return
            if not _wait_gate() or not _wait_pace():
                return
            try:
                payload = _deep_fetch_transcript(vid)
            except Exception as exc:
                # Age gate FIRST, and terminal. Its text contains the bot
                # gate's "sign in to confirm" marker, so classify_youtube_gate_error
                # claims it and the branch below (correctly, for an IP bot wall)
                # writes NO per-video marker — which re-attempted this one video
                # every ~30 min for 24h straight. Retrying an age-gated video
                # never succeeds without cookies, so park it reversibly and do
                # not spend a second attempt or a pace slot on it.
                if is_age_gate_error(exc):
                    _park_age_gated(vid)
                    _bump_age_parked()
                    _bump(1, 1)
                    _flush()
                    return
                if yt_gate.classify_youtube_gate_error(exc):
                    yt_gate.note_youtube_gate(
                        str(exc)[:200], surface="captions", origin="auto",
                    )
                    # The IP is gated — not this video. Do NOT stamp the
                    # marker; park until the freeze lifts, then retry once.
                    # (A genuine bot wall only: the age gate, which IS a
                    # per-video verdict, returned above.)
                    # A paused sweep must not burn a pace slot here either.
                    if _wait_pause() and _wait_gate() and _wait_pace():
                        try:
                            payload = _deep_fetch_transcript(vid)
                        except Exception as exc2:
                            logger.debug("deep fetch retry failed %s: %s", vid, exc2)
                            if yt_gate.classify_youtube_gate_error(exc2):
                                # STILL gated — this is IP state, not a
                                # verdict about the video. Do not poison it
                                # with a 24h marker; leave it for a later
                                # sweep (the freeze is recorded instead).
                                yt_gate.note_youtube_gate(
                                    str(exc2)[:200], surface="captions", origin="auto",
                                )
                                _bump(1, 0)
                                _flush()
                                return
                            _bump(1, 1)
                            archive_db.mark_captions_unavailable("youtube", vid)
                            _flush()
                            return
                    else:
                        return
                else:
                    logger.debug("deep fetch failed %s: %s", vid, exc)
                    # Failed fetches get the same negative marker as
                    # no-captions verdicts — a re-sweep must pre-skip them
                    # (marker expiry re-tests them after a day).
                    _bump(1, 1)
                    archive_db.mark_captions_unavailable("youtube", vid)
                    _flush()
                    return
            segments = _deep_store_transcript(vid, payload)
            _bump(1, 0 if segments else 1)
            _flush()
            if segments:
                archive_db.clear_captions_unavailable("youtube", vid)
            else:
                archive_db.mark_captions_unavailable("youtube", vid)
            for m in _deep_match_segments(query, segments):
                _add_result(v, m)

        with ThreadPoolExecutor(max_workers=_DEEP_FETCH_CONCURRENCY) as pool:
            futures = [pool.submit(_handle_video, v) for v in to_fetch]
            for f in futures:
                try:
                    f.result()
                except Exception as exc:
                    logger.debug("deep worker video failed: %s", exc)
                if cancel.is_set():
                    break
        _flush()
        if cancel.is_set():
            _deep_set(job, status="cancelled")
        else:
            _deep_set(job, status="done")
        _persist()
    except Exception as exc:
        logger.warning("deep search job %s failed: %s", job_id, exc)
        _flush()
        _deep_set(job, status="error", error=str(exc)[:300])
        _persist()


class DeepSearchRequest(BaseModel):
    channel: str
    query: str


def _deep_prune_locked() -> None:
    """Keep at most _DEEP_JOB_CAP jobs — oldest FINISHED first (never evict
    a running sweep)."""
    if len(_deep_jobs) <= _DEEP_JOB_CAP:
        return
    finished = sorted(
        (jid for jid, j in _deep_jobs.items() if j["status"] != "running"),
        key=lambda jid: _deep_jobs[jid]["started_at"],
    )
    for jid in finished[: max(0, len(_deep_jobs) - _DEEP_JOB_CAP)]:
        _deep_jobs.pop(jid, None)


def _deep_handle_norm(handle: str) -> str:
    """Normalization used to key one-channel-per-run dedupe: case- and
    @-insensitive ('@Whindersson' == 'whindersson')."""
    return str(handle or "").strip().lstrip("@").casefold()


def _deep_running_count_locked() -> int:
    return sum(1 for j in _deep_jobs.values() if j["status"] == "running")


def _deep_join_running_locked(norm: str) -> Optional[str]:
    """job_id of a RUNNING in-process sweep for this normalized channel, or
    None when none is live. Caller holds _deep_jobs_lock."""
    for jid, existing in _deep_jobs.items():
        if existing["status"] == "running" and existing.get("handle_norm") == norm:
            return jid
    return None


def _deep_start_db(norm: str, query: str) -> tuple[int, int, int, int]:
    """DB work for a deep-sweep start, run OFF the event loop (to_thread):
    ensure the table, find a stale 'running' row to resume, and finalize
    stale siblings. Returns (resume_cursor, resume_scanned,
    resume_no_transcript, resume_total) — all 0 when nothing to resume.

    Finalize rule: a crashed sweep must not leave a permanently-'running'
    row that a later start would resurrect forever. When resuming from row
    R, siblings (same handle_norm+query, still 'running', id<>R) become
    'interrupted'; when starting WITHOUT a resume row, every still-'running'
    row for that handle+query becomes 'interrupted' (the fresh job writes its
    own row)."""
    _ensure_deep_jobs_table()
    prev = archive_db.query(
        """SELECT id, scanned, total, cursor, no_transcript
           FROM deep_jobs WHERE kind='deep' AND handle_norm=? AND query=?
             AND status='running' ORDER BY updated_at DESC LIMIT 1""",
        (norm, query),
    )
    prev_id = str(prev[0]["id"]) if prev else ""
    if prev_id:
        archive_db.execute(
            """UPDATE deep_jobs SET status='interrupted'
               WHERE kind='deep' AND handle_norm=? AND query=?
                 AND status='running' AND id<>?""",
            (norm, query, prev_id),
        )
    else:
        archive_db.execute(
            """UPDATE deep_jobs SET status='interrupted'
               WHERE kind='deep' AND handle_norm=? AND query=?
                 AND status='running'""",
            (norm, query),
        )
    if not prev:
        return 0, 0, 0, 0
    return (
        int(prev[0]["cursor"] or 0),
        int(prev[0]["scanned"] or 0),
        int(prev[0]["no_transcript"] or 0),
        int(prev[0]["total"] or 0),
    )


def _deep_status_from_db(job_id: str) -> list:
    """Status-endpoint DB fallback (run off the event loop via to_thread):
    ensure the table + read the row. Kept OUT of _deep_jobs_lock so a poll
    never serializes behind a busy DB."""
    _ensure_deep_jobs_table()
    return archive_db.query(
        """SELECT id, status, scanned, total, cursor, truncated,
                  no_transcript, error, age_parked
           FROM deep_jobs WHERE id=? LIMIT 1""",
        (job_id,),
    )


@router.post("/api/archive/search/deep")
async def archive_search_deep_start(body: DeepSearchRequest):
    """Start a deep transcript sweep for one channel; returns {job_id}."""
    from deps import settings_mgr  # lazy: keeps routers.archive import-light

    channel = str(body.channel or "").strip()
    query = str(body.query or "").strip()
    if not channel:
        raise HTTPException(status_code=400, detail="channel is required")
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="query needs at least 2 characters")

    handle: Optional[str] = None
    try:
        saved = settings_mgr.get().saved_channels or []
    except Exception:
        saved = []
    target = channel.lower()
    for entry in saved:
        if not isinstance(entry, dict):
            continue
        slugs = [str(entry.get(k) or "").strip() for k in ("kickSlug", "twitchSlug", "youtubeSlug")]
        if any(s.lower() == target for s in slugs if s):
            handle = str(entry.get("youtubeSlug") or "").strip() or None
            break
    if not handle:
        # Not a saved channel (or the FE sent a raw handle) — sweep the
        # string as a YouTube handle/@handle directly.
        handle = channel

    norm = _deep_handle_norm(handle)
    job_id = uuid.uuid4().hex
    # One sweep per channel: a second POST for a handle that already has a
    # RUNNING in-process sweep joins it instead of piling a duplicate
    # download (the sweep matches every cached transcript anyway, so the
    # running job will serve this query too). This in-memory check runs
    # BEFORE the DB resume lookup so an already-running sweep never re-reads
    # (and then finalizes) its own persisted row.
    with _deep_jobs_lock:
        joined = _deep_join_running_locked(norm)
        if joined is not None:
            return {"job_id": joined, "joined": True}
        # Global cap: concurrent sweeps across DIFFERENT channels are
        # still bounded (bot-gate discipline; pace is shared anyway).
        if _deep_running_count_locked() >= _DEEP_RUNNING_CAP:
            raise HTTPException(
                status_code=409,
                detail="deep search capacity reached — cancel a running sweep first",
            )
    # Restart-resume: a prior sweep that died mid-run persisted its cursor
    # (kind='deep', same channel+query, still 'running'). Re-hydrate it so
    # the status poll recovers the position and the fresh sweep skips the
    # already-scanned prefix instead of re-fetching/re-scanning it. The
    # transcripts themselves survive restart already; cursor resume avoids
    # even re-reading their cached segments for the covered prefix. DB work
    # runs OFF the event loop (to_thread) — sync sqlite must never run on
    # the loop (the same rule F5 fixed for the _deep_seed path).
    try:
        resume_cursor, resume_scanned, resume_no_transcript, resume_total = (
            await asyncio.to_thread(_deep_start_db, norm, query)
        )
    except Exception as exc:
        logger.debug("deep resume lookup failed: %s", exc)
        resume_cursor = resume_scanned = resume_no_transcript = resume_total = 0

    job = {
        "status": "running",
        "error": None,
        "scanned": resume_scanned,
        "total": resume_total,
        "no_transcript": resume_no_transcript,
        "age_parked": 0,
        "truncated": False,
        "truncated_results": False,
        "results": [],
        "resume_cursor": resume_cursor,
        "paused": threading.Event(),
        "cancel": threading.Event(),
        "started_at": time.monotonic(),
        "handle_norm": norm,
    }
    with _deep_jobs_lock:
        _deep_jobs[job_id] = job
        _deep_prune_locked()
    threading.Thread(
        target=_run_deep_job, args=(job_id, handle, query), daemon=True,
        name=f"deep-search-{job_id[:8]}",
    ).start()
    return {"job_id": job_id}


@router.get("/api/archive/search/deep/{job_id}")
async def archive_search_deep_status(job_id: str):
    """Progress + results for a deep sweep (FE polls this every ~2s)."""
    with _deep_jobs_lock:
        job = _deep_jobs.get(job_id)
        if job is None:
            job = None  # fall through to the DB recovery below
        else:
            snapshot = {
                "status": job["status"],
                "paused": job["status"] == "running" and job["paused"].is_set(),
                "scanned": job["scanned"],
                "total": job["total"],
                "no_transcript": job["no_transcript"],
                # Videos YouTube age-gated (parked: no captions without a
                # signed-in session) — the why behind some of no_transcript,
                # with the remedy in /api/archive/videos.
                "age_parked": int(job.get("age_parked") or 0),
                "truncated": job["truncated"],
                "truncated_results": bool(job.get("truncated_results")),
                "results": list(job["results"]),
                "error": job["error"],
                "resumed": bool(job.get("resume_cursor")),
            }
    if job is not None:
        return snapshot
    # No in-memory job: the DB fallback (restart/prune recovery). The read
    # runs OFF the event loop via to_thread (sync sqlite must not run on the
    # loop) AND is taken OUT of _deep_jobs_lock so a poll never serializes
    # behind a busy-DB 10s spin — the in-memory lookup above already
    # released the lock before any sqlite work.
    try:
        rows = await asyncio.to_thread(_deep_status_from_db, job_id)
    except Exception:
        rows = []
    if not rows:
        raise HTTPException(status_code=404, detail="unknown deep search job")
    r = rows[0]
    # Hit results are ephemeral match output (not independently
    # persisted); a DB-recovered job reports its position + status
    # truthfully with an empty results list — the client re-runs the
    # query (transcripts persist) if it wants the hits again.
    return {
        "status": r["status"],
        "paused": False,
        "scanned": int(r["scanned"] or 0),
        "total": int(r["total"] or 0),
        "no_transcript": int(r["no_transcript"] or 0),
        # Persisted beside no_transcript (see _persist), so a job recovered
        # from the DB reports what the run actually parked. This used to be a
        # hardcoded 0 with a comment claiming the number was unknowable: it is
        # knowable, and the UI showed a live sweep's parked state and then
        # forgot it across the restart.
        "age_parked": int(r["age_parked"] or 0),
        "truncated": bool(int(r["truncated"] or 0)),
        "truncated_results": False,
        "results": [],
        "error": r["error"],
        "resumed": True,
    }


@router.post("/api/archive/search/deep/{job_id}/cancel")
async def archive_search_deep_cancel(job_id: str):
    """Ask a running sweep to stop; terminal jobs answer ok too (no-op)."""
    with _deep_jobs_lock:
        job = _deep_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown deep search job")
        job["cancel"].set()
    return {"ok": True}


@router.post("/api/archive/search/deep/{job_id}/pause")
async def archive_search_deep_pause(job_id: str):
    """Park a running sweep (workers sleep at the per-video boundaries, no
    new REMOTE fetches start — pace/gate reservations are never burned).
    Unknown job → 404; a finished job has nothing to park → 409."""
    with _deep_jobs_lock:
        job = _deep_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown deep search job")
        if job["status"] != "running":
            raise HTTPException(
                status_code=409,
                detail=f"deep search job is {job['status']} — nothing to pause",
            )
        job["paused"].set()
    return {"ok": True}


@router.post("/api/archive/search/deep/{job_id}/resume")
async def archive_search_deep_resume(job_id: str):
    """Unpark a paused sweep. Unknown job → 404; a finished job has
    nothing to resume → 409."""
    with _deep_jobs_lock:
        job = _deep_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown deep search job")
        if job["status"] != "running":
            raise HTTPException(
                status_code=409,
                detail=f"deep search job is {job['status']} — nothing to resume",
            )
        job["paused"].clear()
    return {"ok": True}
