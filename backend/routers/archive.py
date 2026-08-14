"""
Archive routes — read/write the local SQLite store (chat, transcripts, video
index, dedupe, job queue). Consumers: ingestion adapters (YouTube/Twitch/Kick)
and the search UI.
"""

import asyncio
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from services import archive_db, archive_twitch
from services.archive_scheduler import TRANSCRIBE_PRIORITY_HIGH, _chat_job_guard

logger = logging.getLogger(__name__)
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
    job, latest job not failed-within-1h, and a live worker — without a
    worker the jobs would sit queued forever for users who never opted in."""
    if platform:
        plats = {p.strip().lower() for p in platform.split(",") if p.strip()}
        if plats and not plats.intersection({"youtube", "twitch", "kick"}):
            return []
    now = time.monotonic()
    with _transcribe_lock:
        if now - _last_transcribe_kick < _TRANSCRIBE_MIN_GAP_S:
            return []
    if not archive_db.worker_live():
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
    mode: str = Query("exact"),
):
    # Semantic (embedding) search is expensive per candidate — its cap stays
    # tight. Literal-word queries may page through every match ("infinite
    # results"): the frontend asks for a large limit and renders incrementally.
    if not isinstance(mode, str):
        mode = "exact"
    mode = (mode or "exact").strip().lower()
    if mode not in ("exact", "broad", "semantic"):
        mode = "exact"
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
    [] + error (never a 500) — the UI surfaces it as a note."""
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
        return {"hits": [], "error": f"'{channel}' has no YouTube handle — add one in Settings"}
    from services.youtube_service import search_channel_videos_sync

    try:
        items = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None, search_channel_videos_sync, handle, q, max(1, min(int(limit), 50))
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
    always False. half <= 0 → "from offset onward" per member, merged by
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
        for m in members:
            msgs, _ = archive_db.chat_window(m["platform"], m["video_id"], offset, half, limit)
            window.extend(msgs)
        window.sort(key=lambda r: (r["offset_sec"], order[r["platform"]]))
        return {"messages": window, "truncated": False, "platforms": platforms, "next_offsets": {}}
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
