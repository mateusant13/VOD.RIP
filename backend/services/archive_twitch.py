"""Twitch archive adapter — VOD metadata ingest + chat backfill via public GQL.

Transport/patterns are shared with :mod:`services.twitch_gql_service`
(same endpoint, same anonymous Client-Id — channel-videos and video-comments
queries need no device auth).

Retention reality: Twitch keeps Past Broadcasts for 14 days (non-affiliate)
or 90 days (partner/affiliate); Highlights persist indefinitely. Backfill is
therefore only meaningful for VODs still inside their retention window —
comment fetching for a purged VOD returns nothing. Callers target recent VODs.

Chat paging is OFFSET-based (``contentOffsetSeconds``), never cursor-based:
cursor pagination on ``VideoCommentsByOffsetOrCursor`` triggers KPSDK
browser-integrity challenges, offset paging does not. Two quirks observed
against gql.twitch.tv (2026-08):

* ``first`` is capped at 100; the API additionally throttles each page to a
  ~60-node window regardless of ``first``, so page size is a hint, not a
  stop condition — termination is "no node advanced the offset".
* ``pageInfo.hasNextPage`` is not trustworthy on offset paging (reports True
  even past the last comment); we do not rely on it.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from services import archive_db
from services import twitch_gql_service
from services.archive_scheduler import _enqueue_chat_job

logger = logging.getLogger(__name__)

# GQL caps `first` for video comments at 100 (verified live).
PAGE_SIZE = 100
# Exponential backoff on 429: start 1s, double, cap at 30s.
BACKOFF_START_SEC = 1.0
BACKOFF_MAX_SEC = 30.0
BACKOFF_MAX_ATTEMPTS = 8
# Kindness pause between pages (rate limit is per-IP; we are deliberately small).
PAGE_DELAY_SEC = 0.5

# Offset-based comments query. `body` is not exposed on
# VideoCommentMessage; text is reconstructed from fragment texts (emote
# fragments carry the token text). No `pageInfo` — see module docstring.
VIDEO_COMMENTS_QUERY = """
query VideoCommentsByOffsetOrCursor($videoID: ID!, $contentOffsetSeconds: Int!, $first: Int) {
  video(id: $videoID) {
    comments(first: $first, contentOffsetSeconds: $contentOffsetSeconds) {
      edges {
        node {
          id
          contentOffsetSeconds
          createdAt
          commenter { id login displayName }
          message {
            fragments { text emote { id } }
            userBadges { id setID version title }
          }
        }
      }
    }
  }
}
"""


class _RateLimited(RuntimeError):
    """HTTP 429 from gql.twitch.tv — caller should back off and retry."""


class _TransientError(RuntimeError):
    """Retryable transport hiccup (5xx, network, 'service error' GQL)."""


# --- metadata ingest -------------------------------------------------------

def _canonical_key(title: str, started_at: Optional[str]) -> str:
    """Cross-platform dedupe key: slugified title + '|' + UTC date (YYYY-MM-DD).

    Identical copy of the shared convention across platform adapters
    (Main-mandated algorithm): NFKD-normalize, drop combining marks, lower,
    collapse runs of [^0-9a-z] to '-', trim; date from the ISO timestamp
    prefix. Title-only when no date is available; "untitled" when the
    slugified title is empty.
    """
    nfkd = unicodedata.normalize("NFKD", title or "")
    plain = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    slug = re.sub(r"[^0-9a-z]+", "-", plain.lower()).strip("-")
    if not slug:
        slug = "untitled"
    day = ""
    if started_at:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", started_at)
        if m:
            day = m.group(1)
    return f"{slug}|{day}" if day else slug


def _find_higher_priority(
    groups: List[dict], canonical_key: str, higher_platforms: tuple[str, ...] = ("youtube",)
) -> Optional[dict]:
    """Return the first member of *higher_platforms* holding *canonical_key*.

    Priority rule: YouTube > Twitch > Kick. A Twitch VOD is skipped when
    YouTube already holds the same canonical key; Kick membership never
    blocks Twitch (the user rule skips Kick only when the content exists
    elsewhere).
    """
    for group in groups:
        if group.get("canonical_key") != canonical_key:
            continue
        for video in group.get("videos") or []:
            if video.get("platform") in higher_platforms:
                return video
    return None


def list_recent_vods(channel: str, limit: int = 3) -> List[dict]:
    """Latest *limit* VODs (metadata only) for a channel login, via public GQL."""
    return twitch_gql_service.list_channel_videos_sync(channel, limit=limit)


def ingest_channel_vods(channel: str, limit: int = 3) -> List[dict]:
    """Upsert the latest *limit* VODs of *channel* into the archive.

    Consults :func:`archive_db.dedupe_view` first: when a higher-priority
    platform (YouTube) already holds the same canonical key, the row is
    upserted as status 'known' with a video_aliases note and flagged
    ``skipped`` — callers must not download/backfill it. Metadata-only:
    no download happens here; the download slice consumes these rows.
    """
    channel = (channel or "").strip().lower()
    if not channel:
        raise ValueError("channel required")
    groups = archive_db.dedupe_view()
    results: List[dict] = []
    for vod in list_recent_vods(channel, limit=limit):
        vid = str(vod.get("id") or "").strip()
        if not vid:
            continue
        key = _canonical_key(vod.get("title"), vod.get("created_at"))
        higher = _find_higher_priority(groups, key)
        note = ""
        if higher is not None:
            note = (
                f"duplicate of {higher['platform']} {higher['video_id']} "
                f"(canonical {key}); skipped download/backfill"
            )
            archive_db.set_alias("twitch", vid, key, note)
        archive_db.upsert_video({
            "platform": "twitch",
            "video_id": vid,
            "channel": channel,
            "title": vod.get("title") or "Untitled",
            "started_at": vod.get("created_at"),
            "ended_at": None,
            "duration_sec": vod.get("duration"),
            "archive_path": None,
            "canonical_key": key,
            "status": "known",
        })
        results.append({
            "video_id": vid,
            "channel": channel,
            "title": vod.get("title"),
            "canonical_key": key,
            "skipped": higher is not None,
            "note": note,
        })
    return results


# --- chat backfill ---------------------------------------------------------

# Cap concurrent Twitch backfills at 2 (measured safe for the per-IP GQL
# 429 limiter — see backfill_chat). Shared by scheduler legs, router kicks
# and the archive worker pool.
_BACKFILL_SEM = threading.BoundedSemaphore(2)


def _message_row(node: dict) -> dict:
    """Map a GQL comment node to an archive messages row.

    Text is fragment-concatenation (emote fragments carry the token text);
    badges/emotes are stored as lists in the row. Deleted commenters yield
    empty username (schema requires non-null).
    """
    msg = node.get("message") or {}
    fragments = msg.get("fragments") or []
    text = "".join(f.get("text") or "" for f in fragments)
    emotes: List[dict] = []
    for f in fragments:
        emote = f.get("emote")
        if emote and emote.get("id"):
            emotes.append({"id": emote["id"], "token": f.get("text") or ""})
    badges = [
        {"setID": b.get("setID"), "version": b.get("version"), "title": b.get("title")}
        for b in (msg.get("userBadges") or [])
    ]
    commenter = node.get("commenter") or {}
    return {
        "offset_sec": float(node.get("contentOffsetSeconds") or 0.0),
        "user_id": commenter.get("id"),
        "username": commenter.get("displayName") or commenter.get("login") or "",
        "text": text or "",
        "badges": badges,
        "emotes": emotes,
        "ts": node.get("createdAt"),
    }


def _post_comments_page(video_id: str, offset_sec: int, page_size: int) -> List[dict]:
    """Fetch one page of comments with offset >= *offset_sec* (GQL Int arg).

    Returns comment nodes (possibly empty). Raises _RateLimited on 429,
    _TransientError on 5xx/network/'service error', RuntimeError otherwise.
    """
    payload = json.dumps({
        "query": VIDEO_COMMENTS_QUERY,
        "variables": {
            "videoID": video_id,
            "contentOffsetSeconds": int(offset_sec),
            "first": int(page_size),
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        twitch_gql_service.TWITCH_GQL_URL,
        data=payload,
        headers={
            "Client-Id": twitch_gql_service.TWITCH_GQL_CLIENT_ID,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        if e.code == 429:
            raise _RateLimited(f"Twitch GQL 429: {detail}") from e
        if e.code >= 500:
            raise _TransientError(f"Twitch GQL HTTP {e.code}: {detail}") from e
        raise RuntimeError(f"Twitch GQL HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise _TransientError(f"Twitch GQL request failed: {e}") from e

    if body.get("errors"):
        msg = body["errors"][0].get("message", "Unknown GQL error")
        if "service error" in msg.lower():
            raise _TransientError(msg)
        raise RuntimeError(msg)
    edges = ((body.get("data") or {}).get("video") or {}).get("comments", {}).get("edges") or []
    return [edge.get("node") for edge in edges if edge.get("node")]


def _backfill_outward(
    vid: str,
    seed_offset_sec: float,
    max_messages: int,
    page_size: int,
    progress_cb: Optional[Callable[[float], None]],
    *, interactive: bool = False,
) -> tuple[float, int, int, int]:
    """Playhead-first sweep for the preview backfill (Chatterino-style).

    Order: (1) one forward page at the seed — the playhead's immediate
    future lands first; (2) a backward walk below the lowest stored offset
    with an exponentially-widening (span-adaptive) stride — near-playhead
    past next, then expanding; (3) the plain forward continuation from the
    deepest stored offset. Returns (last_seen, inserted, pages,
    backoff_retries).

    The backward phase only ever requests offsets below the lowest stored
    row and drops nodes at/above it (they are the forward phase's job), so
    re-runs stay idempotent and no row is inserted twice. Same 100/page
    pages and PAGE_DELAY_SEC pacing as the forward sweep — the ordering
    buys latency, not rate-limit headroom."""
    inserted = 0
    pages = 0
    backoff_retries = 0
    seed = max(0.0, float(seed_offset_sec))

    # Phase 1 — seed page: what the viewer is about to see, in ~1 s.
    nodes, backoff_retries = _fetch_page_with_backoff(
        vid, seed, page_size, backoff_retries, interactive=interactive,
    )
    pages += 1
    if nodes:
        row = archive_db.query(
            "SELECT MAX(offset_sec) m FROM messages "
            "WHERE platform='twitch' AND video_id=?",
            (vid,),
        )
        hi0 = float(row[0]["m"] or -1.0)
        new = [
            n for n in nodes
            if float(n.get("contentOffsetSeconds") or 0.0) > hi0
        ]
        if new:
            archive_db.insert_messages("twitch", vid, [_message_row(n) for n in new])
            inserted += len(new)
            if progress_cb is not None:
                progress_cb(inserted / max_messages)

    # Phase 2 — backward walk from the lowest stored offset down to 0.
    row = archive_db.query(
        "SELECT MIN(offset_sec) lo FROM messages "
        "WHERE platform='twitch' AND video_id=?",
        (vid,),
    )
    lo = float(row[0]["lo"] or seed)
    stride = 1.0
    while lo > 0 and inserted < max_messages:
        target = max(0.0, lo - stride)
        nodes, backoff_retries = _fetch_page_with_backoff(
            vid, target, page_size, backoff_retries, interactive=interactive,
        )
        pages += 1
        if not nodes:
            break  # nothing at/below target — backward sweep is done
        span = 0.0
        if len(nodes) >= 2:
            span = float(nodes[-1].get("contentOffsetSeconds") or 0.0) - float(
                nodes[0].get("contentOffsetSeconds") or 0.0
            )
        new = [
            n for n in nodes
            if float(n.get("contentOffsetSeconds") or 0.0) < lo
        ]
        if new:
            rows = [_message_row(n) for n in new]
            archive_db.insert_messages("twitch", vid, rows)
            inserted += len(rows)
            lo = min(float(n.get("contentOffsetSeconds") or 0.0) for n in new)
            if len(rows) < page_size // 2:
                # Sparse chat or a page dominated by already-stored territory:
                # leap by the page's chat span so each page still yields
                # ~page_size new rows instead of re-probing the same window.
                stride = max(stride * 2.0, span)
            if target <= 0.0:
                break  # reached the first message — nothing exists below it
            if progress_cb is not None:
                progress_cb(inserted / max_messages)
        else:
            if target <= 0.0:
                break  # page at 0 all already-stored → [0, lo) is empty
            stride = max(stride * 2.0, span)
        time.sleep(PAGE_DELAY_SEC)

    # Phase 3 — forward continuation from the deepest stored offset (the
    # seed page may have spilled past it), preserving re-run seeding.
    row = archive_db.query(
        "SELECT MAX(offset_sec) hi FROM messages "
        "WHERE platform='twitch' AND video_id=?",
        (vid,),
    )
    return float(row[0]["hi"] or seed), inserted, pages, backoff_retries


def backfill_chat(
    channel: str,
    video_id: str,
    *,
    max_messages: int = 200,
    page_size: int = PAGE_SIZE,
    seed_offset_sec: Optional[float] = None,
    progress_cb: Optional[Callable[[float], None]] = None,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Backfill chat for one Twitch VOD into the archive.

    Offset-based pagination (never cursor-based — see module docstring).
    Stops at *max_messages*, at end of chat (a page whose nodes all sit at
    or below the last seen offset — the API clamps its window to the tail),
    or when a hard error escapes the 429 backoff loop. One archive_jobs row
    (kind 'chat') tracks the run; pass *job_id* to track the CALLER's
    already-claimed job (the archive worker), otherwise one is enqueued
    here (router/preview kicks).

    With *seed_offset_sec* the sweep starts at that playhead and pages
    OUTWARD (backward below the seed first, then forward from the deepest
    stored offset — see _backfill_outward); without it the sweep is the
    plain incremental forward run from MAX(offset_sec). *progress_cb*
    (inserted / max_messages) fires after every stored page.

    Backoff on 429: 1s → double → cap 30s; after BACKOFF_MAX_ATTEMPTS the
    _RateLimited is re-raised so the caller can schedule a retry later.
    Two lanes: the worker (job_id passed) is the paced background lane —
    full 429 backoff; router/preview kicks (job_id omitted) are the
    interactive lane — non-blocking semaphore ('busy' status when the
    background lane holds both per-IP slots) and single-attempt fetches
    (fail fast on a gate, no retry-loop; the scheduler re-queues later).
    """
    vid = str(video_id or "").strip()
    if not vid.isdigit():
        raise ValueError(f"video_id must be numeric, got {video_id!r}")
    channel = (channel or "").strip().lower()
    if not channel:
        raise ValueError("channel required")
    page_size = max(1, min(int(page_size), PAGE_SIZE))
    max_messages = max(1, int(max_messages))

    # Two lanes (user requirement): the archive worker always passes its
    # already-claimed job id; router/preview kicks never do. The worker is
    # the paced background lane; kicks are the interactive lane — they fail
    # fast (busy/rate-limited) instead of queueing behind background work.
    interactive = job_id is None
    if job_id:
        job_id = str(job_id)
    else:
        # Stable kick-lane job id + the shared scheduler dedupe: a repeat
        # kick for the same video (restart, second search, preview reopen)
        # is skipped when chat rows exist or a 'chat' job is queued/
        # running/done, and a stale failed row is requeued in place. The
        # old time-based id bypassed both — restarts duplicated chat rows
        # and a same-second re-kick crashed on an uncaught IntegrityError.
        # A done job on a chat-less VOD is the terminal no-chat marker
        # (comments disabled / purged), so later kicks are permanent no-ops.
        job_id = f"tw-backfill-{vid}"
        if not _enqueue_chat_job("twitch", vid, job_id=job_id, retry_fresh_failed=True):
            return {
                "video_id": vid,
                "channel": channel,
                "inserted": 0,
                "pages": 0,
                "backoff_retries": 0,
                "stopped": "already" if archive_db.has_chat("twitch", vid) else "queued",
            }
    archive_db.update_job(job_id, status="running")

    # Job-row heartbeat: every stored page refreshes the row (progress +
    # heartbeat) in BOTH lanes. _claim_next_job reclaims a 'running' chat
    # job whose heartbeat went stale (dead/hung executor) instead of
    # waiting out the flat 2h window, so a wedged fetch can't hold the row
    # for hours. The worker lane used to stamp this in its own progress_cb;
    # backfill_chat owns it now (single touch per page).
    user_cb = progress_cb

    def _progress(p: float) -> None:
        archive_db.update_job(job_id, progress=min(0.999, p))
        if user_cb is not None:
            user_cb(p)

    progress_cb = _progress

    inserted = 0
    pages = 0
    backoff_retries = 0
    # Seed from the deepest stored offset so re-runs are idempotent and
    # incremental: nodes already in the archive are skipped, only later
    # chat is fetched. (messages has no unique key — dupes would be real.)
    existing = archive_db.query(
        "SELECT MAX(offset_sec) m FROM messages WHERE platform='twitch' AND video_id=?",
        (vid,),
    )
    last_seen = float(existing[0]["m"] or 0.0)  # highest offset stored; next page starts at floor
    dur_row = archive_db.query(
        "SELECT duration_sec FROM videos WHERE platform='twitch' AND video_id=?",
        (vid,),
    )
    duration = float(dur_row[0]["duration_sec"] or 0.0) if dur_row else 0.0

    # Per-IP GQL concurrency cap (2). The background worker blocks on the
    # semaphore (paced by design); an interactive kick never queues behind
    # it — non-blocking acquire, clear 'busy' status, and the job row is
    # requeued so the worker drains it on a later pass.
    if interactive and not _BACKFILL_SEM.acquire(blocking=False):
        archive_db.update_job(
            job_id, status="queued",
            error="chat backfill busy (background backfills in flight) — worker will pick this up",
        )
        return {
            "video_id": vid,
            "channel": channel,
            "inserted": 0,
            "pages": 0,
            "backoff_retries": 0,
            "stopped": "busy",
        }
    if not interactive:
        _BACKFILL_SEM.acquire()
    try:
        return _backfill_locked(
            job_id, vid, channel, last_seen, duration,
            max_messages, page_size, seed_offset_sec, progress_cb,
            interactive=interactive,
        )
    finally:
        _BACKFILL_SEM.release()


def _backfill_locked(
    job_id: str, vid: str, channel: str, last_seen: float, duration: float,
    max_messages: int, page_size: int,
    seed_offset_sec: Optional[float],
    progress_cb: Optional[Callable[[float], None]],
    *, interactive: bool,
) -> Dict[str, Any]:
    """The fetch sweep — caller MUST hold _BACKFILL_SEM.

    Two lanes: the background worker (interactive=False) is paced by design
    and retries 429 with backoff; an interactive kick (interactive=True)
    fails fast — no retry-loop — and the job row becomes 'failed' so the
    worker re-queues it on a later pass."""
    inserted = 0
    pages = 0
    backoff_retries = 0

    def _finish() -> Dict[str, Any]:
        archive_db.update_job(job_id, status="done", progress=1.0)
        return {
            "video_id": vid,
            "channel": channel,
            "inserted": inserted,
            "pages": pages,
            "backoff_retries": backoff_retries,
            "stopped": "end_of_chat",
        }

    if duration and last_seen >= duration - 5.0:
        # Chat already covers the whole stream (re-run or trailing error
        # after end-of-chat) — nothing left to fetch.
        return _finish()
    # GQL 429 backoff is per-IP: parallel Twitch backfills tripped the
    # limiter and collapsed throughput (measured: 2 parallel = max safe).
    # The scheduler used to cap this externally (TWITCH_BACKFILL_MAX_INFLIGHT);
    # the archive worker pool can now run several backfills at once, so the
    # cap lives with the fetch itself. ponytail: a token-bucket per IP would
    # be the real fix, but every VOD.RIP box is a single-IP desktop app.
    if seed_offset_sec is not None:
        # Preview kick: seed at the client's playhead (Chatterino-style) so
        # near-playhead messages arrive in the first pages. Re-runs anchor
        # on the stored range either way.
        last_seen, inserted, pages, backoff_retries = _backfill_outward(
            vid,
            max(0.0, float(seed_offset_sec)),
            max_messages,
            page_size,
            progress_cb,
            interactive=interactive,
        )
    try:
        while inserted < max_messages:
            nodes, backoff_retries = _fetch_page_with_backoff(
                vid, last_seen, page_size, backoff_retries, interactive=interactive,
            )
            pages += 1
            if not nodes:
                break
            rows: List[dict] = []
            for node in nodes:
                off = float(node.get("contentOffsetSeconds") or 0.0)
                if off <= last_seen:
                    continue  # same-second boundary re-fetch — already stored
                last_seen = off
                rows.append(_message_row(node))
            if rows:
                archive_db.insert_messages("twitch", vid, rows)
                inserted += len(rows)
                if progress_cb is not None:
                    progress_cb(inserted / max_messages)
            else:
                # Whole page was dups: window clamped at the chat tail → end.
                break
            if inserted >= max_messages:
                break
            time.sleep(PAGE_DELAY_SEC)
        archive_db.update_job(job_id, status="done", progress=1.0)
    except Exception as exc:
        # Twitch's comments GQL answers pages at/past end-of-chat with a
        # 'service error' instead of an empty edge list — a fetch failure
        # once the stored chat already covers the stream is completion.
        if duration and last_seen >= duration - 5.0:
            return _finish()
        archive_db.update_job(job_id, status="failed", error=str(exc)[:500])
        raise
    return {
        "video_id": vid,
        "channel": channel,
        "inserted": inserted,
        "pages": pages,
        "backoff_retries": backoff_retries,
        "stopped": "max_messages" if inserted >= max_messages else "end_of_chat",
    }


def _fetch_page_with_backoff(
    video_id: str, last_seen: float, page_size: int, retries: int,
    *, interactive: bool = False,
) -> tuple[List[dict], int]:
    """One page fetch, retrying 429 (exponential, capped) and transients.

    Two lanes (user requirement): the background worker (interactive=False)
    retries 429/transients with exponential jittered backoff — it is paced
    by design and requeues on exhaustion. Interactive kicks (router/preview,
    interactive=True) fail FAST: a single attempt, no sleep, so a gated or
    flaky fetch never hangs the user's chat; the scheduler re-queues the
    video on its next pass. Returns (nodes, retries) — the retry tally is
    incremented on every backoff sleep so the caller can report honest
    rate-limit stats.
    """
    attempts = 1 if interactive else BACKOFF_MAX_ATTEMPTS
    backoff = BACKOFF_START_SEC
    for attempt in range(attempts):
        try:
            return _post_comments_page(video_id, int(last_seen), page_size), retries
        except _RateLimited:
            retries += 1
            if attempt + 1 >= attempts:
                raise
            # Jittered sleep: a synchronized multi-worker retry would hit the
            # per-IP limiter in lockstep (same backoff schedule = same request
            # wall). ±25% spreads colliding workers.
            time.sleep(backoff + random.uniform(0.0, 0.25 * backoff))
            backoff = min(backoff * 2.0, BACKOFF_MAX_SEC)
        except _TransientError:
            retries += 1
            if attempt + 1 >= attempts:
                raise
            time.sleep(min(backoff, 5.0) + random.uniform(0.0, 0.5))
            backoff = min(backoff * 2.0, BACKOFF_MAX_SEC)
    return [], retries  # pragma: no cover — loop always returns or raises


# --- self-check ------------------------------------------------------------
# Contract invariants, run on import (no network, no DB writes).

assert _canonical_key("Watchparty do Mundial!", "2026-08-01T22:30:00Z") == (
    "watchparty-do-mundial|2026-08-01"
)
assert _canonical_key("Último dia do Mundial!", "2026-08-01T16:53:51Z") == (
    "ultimo-dia-do-mundial|2026-08-01"
)  # Main-refined: diacritics stripped before collapsing
assert _canonical_key("  Título!!!   com Símbolos  ", "2026-07-31T17:00:18Z") == (
    "titulo-com-simbolos|2026-07-31"
)
assert _canonical_key("No Date Here", None) == "no-date-here"
assert _canonical_key("", "2026-08-01T00:00:00Z") == "untitled|2026-08-01"
assert _canonical_key("!!!", None) == "untitled"

_sample_node = {
    "id": "c1",
    "contentOffsetSeconds": 42.5,
    "createdAt": "2026-08-01T20:00:00Z",
    "commenter": {"id": "u1", "login": "lubu", "displayName": "lubu"},
    "message": {
        "fragments": [
            {"text": "LUL", "emote": {"id": "425618"}},
            {"text": " gg "},
            {"text": "Pog", "emote": {"id": "305954156"}},
        ],
        "userBadges": [
            {"id": "b1", "setID": "subscriber", "version": "3", "title": "3-Month Subscriber"}
        ],
    },
}
_sample_row = _message_row(_sample_node)
assert _sample_row["offset_sec"] == 42.5
assert _sample_row["username"] == "lubu"
assert _sample_row["user_id"] == "u1"
assert _sample_row["text"] == "LUL gg Pog"
assert _sample_row["badges"] == [{"setID": "subscriber", "version": "3", "title": "3-Month Subscriber"}]
assert _sample_row["emotes"] == [
    {"id": "425618", "token": "LUL"},
    {"id": "305954156", "token": "Pog"},
]
assert _message_row({"contentOffsetSeconds": 1, "message": {"fragments": []}})[
    "username"
] == ""

_groups = [
    {"canonical_key": "k|2026-01-01", "videos": [{"platform": "youtube", "video_id": "abc"}]},
    {"canonical_key": "k|2026-01-02", "videos": [{"platform": "kick", "video_id": "k1"}]},
]
assert _find_higher_priority(_groups, "k|2026-01-01")["video_id"] == "abc"
assert _find_higher_priority(_groups, "k|2026-01-02") is None  # Kick never blocks Twitch
assert _find_higher_priority(_groups, "k|2026-01-03") is None
assert PAGE_SIZE == 100
