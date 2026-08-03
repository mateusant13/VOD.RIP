"""
Archive routes — read/write the local SQLite store (chat, transcripts, video
index, dedupe, job queue). Consumers: ingestion adapters (YouTube/Twitch/Kick)
and the search UI.
"""

import asyncio
import logging
import sqlite3
import threading
import time
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from services import archive_db, archive_twitch

logger = logging.getLogger(__name__)
router = APIRouter(tags=["archive"])


# --- Twitch chat backfill (background) --------------------------------------

# A full 12h VOD has tens of thousands of comments; backfill_chat's default
# max_messages=200 would stop a real VOD at 200 rows (TwitchProbe-verified:
# 211 GQL pages across 6 VODs, zero 429s). Re-runs are incremental and
# idempotent (seed = MAX(messages.offset_sec)), so a big cap only ever
# fetches what is still missing.
_BACKFILL_MAX_MESSAGES = 100_000

_backfill_inflight: set[str] = set()  # video_ids currently backfilling
_backfill_lock = threading.Lock()     # guards the set + throttle clock
_last_auto_kick = 0.0                 # monotonic clock of the last auto-kick
_AUTO_KICK_MIN_GAP_S = 15.0           # min seconds between auto-kicks
_AUTO_KICK_LIMIT = 2                  # newest chat-less videos per search
# Completion cooldown: a chat-less VOD (comments disabled / purged) would
# otherwise be re-kicked on every search; remember recent attempts so
# auto-kick skips them for a while. Manual backfill is never throttled.
_backfill_attempted_at: dict[str, float] = {}
_BACKFILL_COOLDOWN_S = 600.0


async def _run_backfill(video_id: str, channel: str) -> None:
    """Background task: run backfill_chat in a worker thread; drop the
    in-flight marker and stamp the completion time on exit."""
    try:
        await asyncio.to_thread(
            archive_twitch.backfill_chat,
            channel, video_id,
            max_messages=_BACKFILL_MAX_MESSAGES,
        )
    except Exception:
        logger.exception("chat backfill failed for twitch/%s", video_id)
    finally:
        with _backfill_lock:
            _backfill_inflight.discard(video_id)
            _backfill_attempted_at[video_id] = time.monotonic()


def _kick_backfill(video_id: str, channel: str) -> str:
    """Start a background Twitch chat backfill; returns the status word.

    'queued' — task started now; 'running' — already in flight;
    'already' — chat rows exist, nothing to do; 'failed' — could not start."""
    if not (channel or "").strip():
        return "failed"
    with _backfill_lock:
        if video_id in _backfill_inflight:
            return "running"
    if archive_db.query(
        "SELECT 1 FROM messages WHERE platform='twitch' AND video_id=? LIMIT 1",
        (video_id,),
    ):
        return "already"
    try:
        with _backfill_lock:
            _backfill_inflight.add(video_id)
        asyncio.get_running_loop().create_task(_run_backfill(video_id, channel))
        return "queued"
    except Exception:
        logger.exception("could not start chat backfill for twitch/%s", video_id)
        with _backfill_lock:
            _backfill_inflight.discard(video_id)
        return "failed"


def _maybe_auto_backfill(*, platform: Optional[str], channel: Optional[str], source: str) -> None:
    """Lazily kick chat backfill for the newest chat-less Twitch VODs in scope.

    Runs inside the search response (create_task, never awaited): when the
    search covers chat and Twitch, the 2 newest videos with zero chat rows
    (matching the channel filter, if any) get a background backfill.
    Throttled to one burst per _AUTO_KICK_MIN_GAP_S; in-flight and
    recently-attempted videos are skipped."""
    global _last_auto_kick
    if source not in ("both", "chat"):
        return
    if platform and "twitch" not in [
        p.strip().lower() for p in platform.split(",") if p.strip()
    ]:
        return
    now = time.monotonic()
    with _backfill_lock:
        if now - _last_auto_kick < _AUTO_KICK_MIN_GAP_S:
            return
    sql = (
        "SELECT v.video_id, v.channel FROM videos v "
        "WHERE v.platform='twitch' AND NOT EXISTS ("
        "  SELECT 1 FROM messages m WHERE m.platform='twitch' AND m.video_id=v.video_id)"
    )
    params: list[Any] = []
    if channel:
        slugs = [c.strip() for c in channel.split(",") if c.strip()]
        if slugs:
            sql += " AND lower(v.channel) IN (" + ",".join("?" * len(slugs)) + ")"
            params.extend(s.lower() for s in slugs)
    sql += " ORDER BY v.started_at DESC LIMIT ?"
    params.append(_AUTO_KICK_LIMIT)
    rows = archive_db.query(sql, params)
    kicked = False
    for r in rows:
        vid = r["video_id"]
        if not (r["channel"] or "").strip():
            continue  # nothing to backfill against without a channel
        with _backfill_lock:
            if vid in _backfill_inflight:
                continue
            if now - _backfill_attempted_at.get(vid, 0.0) < _BACKFILL_COOLDOWN_S:
                continue
            _backfill_inflight.add(vid)
            kicked = True
        asyncio.get_running_loop().create_task(_run_backfill(vid, r["channel"] or ""))
    if kicked:
        with _backfill_lock:
            _last_auto_kick = now


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
    q: str = Query(..., min_length=1),
    platform: str | None = None,
    channel: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    kind: str | None = None,
    source: str = "both",
    video_id: str | None = None,
    lang: str | None = None,
    limit: int = Query(20, ge=1, le=100),
):
    # platform/kind accept comma-separated lists ("twitch,kick").
    for p in (platform or "").split(","):
        if p.strip():
            _require_platform(p)
    for label, value in (("date_from", date_from), ("date_to", date_to)):
        if value and not _is_iso_date(value):
            raise HTTPException(status_code=400, detail=f"{label} must be YYYY-MM-DD")
    bad_kinds = [
        k for k in (k.strip().lower() for k in (kind or "").split(","))
        if k and k not in archive_db.KINDS
    ]
    if bad_kinds:
        raise HTTPException(status_code=400, detail=f"kind must be one of {archive_db.KINDS}")
    # source restricts to one content kind; channel accepts comma-separated
    # slugs ("a,b" → IN match) but never empty segments.
    source = (source or "both").strip().lower()
    if source not in ("both", "chat", "transcript"):
        raise HTTPException(
            status_code=400,
            detail="source must be one of both, chat, transcript",
        )
    if channel and any(not s.strip() for s in channel.split(",")):
        raise HTTPException(status_code=400, detail="channel must be non-empty slugs")
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
    )
    # Lazily kick chat backfill for chat-less Twitch VODs in scope (fires a
    # background task; never blocks or alters the response).
    _maybe_auto_backfill(platform=platform or None, channel=channel or None, source=source)
    return {"hits": hits}


@router.get("/api/archive/videos/{platform}/{video_id}/chat")
async def archive_chat_window(platform: str, video_id: str, offset: float = 0.0, half: float = 30.0):
    _require_platform(platform)
    return {"messages": archive_db.chat_window(platform, video_id, offset, half)}


@router.post("/api/archive/videos/{platform}/{video_id}/chat/backfill")
async def archive_chat_backfill(platform: str, video_id: str):
    """Queue a background Twitch chat backfill for one archived VOD.

    Twitch-only (Kick/YouTube chats are archived at ingest). Status words:
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
    return {"segments": archive_db.transcript_for(platform, video_id)}


@router.get("/api/archive/dedupe")
async def archive_dedupe():
    return {"groups": archive_db.dedupe_view()}


@router.post("/api/archive/aliases")
async def archive_aliases(platform: str, video_id: str, canonical_key: str, note: str = ""):
    _require_platform(platform)
    if not canonical_key.strip():
        raise HTTPException(status_code=400, detail="canonical_key required")
    archive_db.set_alias(platform, video_id, canonical_key.strip(), note)
    return {"ok": True}


@router.get("/api/archive/jobs")
async def archive_jobs(limit: int = Query(50, ge=1, le=500)):
    return {"jobs": archive_db.list_jobs(limit)}


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
        archive_db.enqueue_job(job_id, kind, platform, video_id)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail=f"job {job_id} already exists") from None
    return {"ok": True, "id": job_id}
