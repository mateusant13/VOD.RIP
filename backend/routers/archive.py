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

from services import archive_db, archive_twitch
from services.archive_scheduler import TRANSCRIBE_PRIORITY_HIGH, _chat_job_guard

logger = logging.getLogger(__name__)


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
# starve each other. Enqueue is gated on worker_live() — see _transcribe_candidates.
_last_transcribe_kick = 0.0
_TRANSCRIBE_MIN_GAP_S = 30.0
_transcribe_attempted_at: dict[str, float] = {}
_TRANSCRIBE_COOLDOWN_S = 600.0
_TRANSCRIBE_FAILED_FRESH_S = 3600.0
_TRANSCRIBE_LIMIT = 1
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
    video_id: str, channel: str, seed_offset_sec: Optional[float] = None
) -> str:
    """Start a background Twitch chat backfill; returns the status word.

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
        asyncio.get_running_loop().create_task(
            _run_backfill(video_id, channel, seed_offset_sec=seed_offset_sec)
        )
        return "queued"
    except Exception:
        logger.exception("could not start chat backfill for twitch/%s", video_id)
        with _backfill_lock:
            _backfill_inflight.discard(video_id)
        return "failed"


def kick_preview_backfill(
    platform: str, video_id: str, offset_sec: Optional[float] = None
) -> str:
    """Throttled single-video Twitch chat backfill on preview open.

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
    status = _kick_backfill(video_id, row[0]["channel"], seed_offset_sec=offset_sec)
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
        if not (r["archive_path"] or "").strip() or not Path(r["archive_path"]).is_file():
            continue  # file gone — whisper would fail immediately
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
    return out[:_TRANSCRIBE_LIMIT]


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
    return {"videos": archive_db.list_videos(platform, channel)}


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
    limit: int = Query(20, ge=1, le=100000),
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
    hits = archive_db.search(
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
    )
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


@router.post("/api/archive/aliases")
async def archive_aliases(platform: str, video_id: str, canonical_key: str, note: str = ""):
    _require_platform(platform)
    if not canonical_key.strip():
        raise HTTPException(status_code=400, detail="canonical_key required")
    archive_db.set_alias(platform, video_id, canonical_key.strip(), note)
    return {"ok": True}


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
    try:
        archive_db.enqueue_job(job_id, kind, platform, video_id, priority=0)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail=f"job {job_id} already exists") from None
    threading.Thread(
        target=_start_frozen_archive_worker,
        daemon=True,
        name="archive-worker-kick",
    ).start()
    return {"ok": True, "id": job_id}


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
_DEEP_RESULT_CAP_PER_VIDEO = 5
_DEEP_SNIPPET_PAD = 120
_DEEP_FETCH_CONCURRENCY = 2
_DEEP_MIN_GAP_S = 1.5  # mirrors archive_ytdlp._ORIGINAL_MIN_GAP_S
_DEEP_SQL_CHUNK = 500
_DEEP_RUNNING_CAP = 2  # concurrent sweeps across ALL channels (bot-gate discipline)

_deep_jobs: dict[str, dict] = {}
_deep_jobs_lock = threading.Lock()
# Fetch pacing is GLOBAL, not per-job: two sweeps from two clients must
# still start yt-dlp calls >=_DEEP_MIN_GAP_S apart. Closure-local pace
# would let each job blast YouTube independently.
_deep_pace = {"last": 0.0}
_deep_pace_lock = threading.Lock()

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
    """First <=5 matches of the query against (start_sec, text) segments.

    LITERAL substring match over the deaccented/casefolded text (never
    regex — pt-BR queries like 'R$ 10' or '$100' must match literally, and
    no path may run a user-supplied pattern with the GIL held). Match
    offsets are mapped back to ORIGINAL text indices so the snippet keeps
    the accent characters; ts is the segment start in seconds."""
    q = _deaccent(raw_query.strip())
    if not q:
        return []
    out: list[dict] = []
    for start_sec, text in segments:
        deaccented, offmap = _deaccent_map(text)
        idx = deaccented.find(q)
        if idx < 0:
            continue
        # Map the match span back to original-text indices (the map is
        # monotone non-decreasing; end-offset is the char AFTER the match).
        orig_start = offmap[idx]
        orig_end = offmap[idx + len(q) - 1] + 1
        out.append(
            {"ts": int(start_sec), "snippet": _deep_snippet(text, orig_start, orig_end)}
        )
        if len(out) >= _DEEP_RESULT_CAP_PER_VIDEO:
            break
    return out


def _deep_enumerate(handle: str) -> tuple[list[dict], bool]:
    """All channel videos (uploads+shorts+streams), newest-first, deduped.

    Seam for tests. Each tab is listed via the guarded flat extract
    (list_channel_videos_sync); truncated is True when any tab failed or
    saturated the ceiling, or the merged set exceeded _DEEP_TAB_LIMIT — a
    partial sweep must never claim full coverage.

    The saturation asked for is the RAW crawl bound (list_order >=
    playlistend), not the show-more `has_more`: this sweep always asks at
    the 1000-row ceiling, where has_more is force-False by design (a deeper
    ask could never serve new rows) and would otherwise hide truncation.
    """
    from services.youtube_service import list_channel_videos_sync

    merged: dict[str, dict] = {}
    truncated = False
    for tab in ("videos", "shorts", "streams"):
        try:
            rows, _has_more, saturated = list_channel_videos_sync(
                handle,
                _DEEP_TAB_LIMIT,
                playlist=tab,
                enrich=False,
                return_has_more=True,
                return_crawl_saturation=True,
            )
        except Exception as exc:
            logger.debug("deep enumerate tab %s failed: %s", tab, exc)
            # A tab that errored yielded NOTHING — the result set is
            # silently incomplete; report it as truncated (honest partial).
            truncated = True
            continue
        truncated = truncated or bool(saturated) or len(rows) >= _DEEP_TAB_LIMIT
        for v in rows:
            vid = str(v.get("id") or "").strip()
            if vid and vid not in merged:
                merged[vid] = v
    items = list(merged.values())

    def _ts(v: dict) -> float:
        raw = str(v.get("created_at") or "")
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return 0.0

    items.sort(key=_ts, reverse=True)
    if len(items) > _DEEP_TAB_LIMIT:
        items = items[:_DEEP_TAB_LIMIT]
        truncated = True
    return items, truncated


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

    with _deep_jobs_lock:
        job = _deep_jobs.get(job_id)
    if job is None:
        return
    cancel: threading.Event = job["cancel"]
    results: list[dict] = []
    counters = {"scanned": 0, "no_transcript": 0}
    counters_lock = threading.Lock()

    def _bump(scanned: int = 0, missing: int = 0) -> None:
        """Counters are touched by both fetch workers — `+=` on a shared dict
        is not atomic, so the update is serialised."""
        with counters_lock:
            counters["scanned"] += scanned
            counters["no_transcript"] += missing

    def _flush() -> None:
        with counters_lock, _deep_jobs_lock:
            job["scanned"] = counters["scanned"]
            job["no_transcript"] = counters["no_transcript"]
            job["results"] = list(results)

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
        }
        kind = str(v.get("content_kind") or "").strip()
        if kind:
            # Same vocabulary as the videos.kind rows the unified search
            # ships (vod, not video) — the FE kind chips compose over it.
            hit["video_kind"] = {"video": "vod"}.get(kind, kind)
        results.append(hit)

    try:
        videos, truncated = _deep_enumerate(handle)
        if not _wait_pause():
            _deep_set(job, status="cancelled")
            return
        if not videos:
            _deep_set(job, status="error", error="channel enumeration returned no videos")
            return
        with _deep_jobs_lock:
            job["total"] = len(videos)
            job["truncated"] = bool(truncated)
        _deep_seed_video_rows(handle, videos)
        ids = [str(v.get("id") or "") for v in videos]
        covered, marked = _deep_covered_ids(ids)

        # Pass 1 — cached transcripts: match straight from the DB, zero
        # network. Marker-fresh videos are pre-skipped and counted.
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
                if yt_gate.classify_youtube_gate_error(exc):
                    yt_gate.note_youtube_gate(str(exc)[:200])
                    # The IP is gated — not this video. Do NOT stamp the
                    # marker; park until the freeze lifts, then retry once.
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
                                yt_gate.note_youtube_gate(str(exc2)[:200])
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
    except Exception as exc:
        logger.warning("deep search job %s failed: %s", job_id, exc)
        _flush()
        _deep_set(job, status="error", error=str(exc)[:300])


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
    job = {
        "status": "running",
        "error": None,
        "scanned": 0,
        "total": 0,
        "no_transcript": 0,
        "truncated": False,
        "results": [],
        "paused": threading.Event(),
        "cancel": threading.Event(),
        "started_at": time.monotonic(),
        "handle_norm": norm,
    }
    with _deep_jobs_lock:
        # One sweep per channel: a second POST for a handle that already
        # has a RUNNING sweep joins it instead of piling a duplicate
        # download (the sweep matches every cached transcript anyway, so
        # the running job will serve this query too).
        for jid, existing in _deep_jobs.items():
            if existing["status"] == "running" and existing.get("handle_norm") == norm:
                return {"job_id": jid, "joined": True}
        # Global cap: concurrent sweeps across DIFFERENT channels are
        # still bounded (bot-gate discipline; pace is shared anyway).
        if _deep_running_count_locked() >= _DEEP_RUNNING_CAP:
            raise HTTPException(
                status_code=409,
                detail="deep search capacity reached — cancel a running sweep first",
            )
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
            raise HTTPException(status_code=404, detail="unknown deep search job")
        snapshot = {
            "status": job["status"],
            "paused": job["status"] == "running" and job["paused"].is_set(),
            "scanned": job["scanned"],
            "total": job["total"],
            "no_transcript": job["no_transcript"],
            "truncated": job["truncated"],
            "results": list(job["results"]),
            "error": job["error"],
        }
    return snapshot


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
