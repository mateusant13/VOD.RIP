"""Kick ingestion adapter — archive VOD metadata + best-effort download.

Reuses the repo's existing Kick pipeline (services.kick_api_service):
metadata via the public Kick JSON API (curl_cffi, no browser), downloads via
download_vod_sync (curl_cffi + HLS segments + ffmpeg).

No Kick cookie settings exist in the app (only youtube_cookies_file /
youtube_cookies_browser, which are YouTube-scoped — verified), so there is no
cookie file to hand yt-dlp; the curl_cffi path IS the repo's Kick download
strategy and survives Cloudflare via browser impersonation.

Dedupe rule (user-mandated, cross-platform):
  canonical_key = _canonical_key(title, started_at) — same normalized
  title + UTC start date as the YouTube/Twitch adapters (Main-mandated
  format: NFKD + diacritic-strip, non-alnum runs -> '-', "title|YYYY-MM-DD").
  Before downloading, consult archive_db.dedupe_view(); when a higher-
  priority platform (youtube > twitch > kick) already holds the same
  canonical_key, the row stays status='known' with archive_path=None and
  the download is skipped. The videos table has no note column, so the
  skip/failure reason lives in video_aliases.note (archive_db.set_alias —
  dedupe_view() already joins aliases) plus an archive_job row whose
  error field carries the note.

  Only Kick-exclusive content is downloaded. Downloads are best-effort:
  budget-capped (default 300 s/video — user rule), 720p, cancelled via
  cancel_event. Failures (Cloudflare block, missing HLS source, timeout)
  are recorded as status='failed' with the error in archive_jobs.error.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from typing import Optional

from services import archive_content_dedup, archive_db, kick_api_service
from services.kick_models import KickVideo
from services.settings import _get_appdata_dir

logger = logging.getLogger(__name__)

PLATFORM = "kick"
_PRIORITY = {"youtube": 0, "twitch": 1, "kick": 2}
_DEFAULT_BUDGET_SEC = 300.0  # user rule: never burn more than 5 min per video


def _enforce_retention() -> None:
    """Apply archive VOD retention after a kick file lands — a 6th VOD
    immediately evicts the oldest file. Best-effort, never fatal."""
    try:
        from services.archive_retention import enforce_archive_vod_retention

        stats = enforce_archive_vod_retention()
        if stats["deleted_files"]:
            logger.info(
                "archive_kick: retention removed %d file(s), cleared %d row(s)",
                stats["deleted_files"],
                stats["cleared_rows"],
            )
    except Exception:
        logger.debug("archive_kick: retention skipped", exc_info=True)


def _canonical_key(title: Optional[str], started_at: Optional[str]) -> Optional[str]:
    """Cross-platform dedupe key — Main-mandated format (identical in all
    platform adapters): NFKD-normalize, drop combining marks (so 'Último'
    and 'ULTIMO' match), lowercase, collapse runs of [^0-9a-z] to '-',
    then '|' + UTC date (YYYY-MM-DD). Title-only when no date is known;
    'untitled' when the title part is empty."""
    t = unicodedata.normalize("NFKD", (title or "") or "")
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    norm = re.sub(r"[^0-9a-z]+", "-", t.lower()).strip("-") or "untitled"
    date = _start_date(started_at)
    return f"{norm}|{date}" if date else norm


def _start_date(value: Optional[str]) -> Optional[str]:
    """UTC date prefix of an ISO timestamp, e.g. '2026-07-30T23:21:30Z' ->
    '2026-07-30'. Kick API timestamps are already UTC."""
    if not value:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(value).strip())
    return m.group(1) if m else None


def dedupe_decision(canonical_key: Optional[str], groups: list[dict]) -> tuple[str, Optional[str]]:
    """Apply the priority rule (youtube > twitch > kick) to one candidate.

    Returns ('download', None) when the content is kick-exclusive (or the
    key is unknown), else ('skip', <highest-priority platform that holds it>).
    """
    if not canonical_key:
        return ("download", None)
    for g in groups:
        if g.get("canonical_key") != canonical_key:
            continue
        blockers = [
            v.get("platform")
            for v in g.get("videos", [])
            if v.get("platform") in _PRIORITY and v.get("platform") != PLATFORM
        ]
        if blockers:
            return ("skip", sorted(blockers, key=lambda p: _PRIORITY[p])[0])
        return ("download", None)
    return ("download", None)


def _archive_dir(archive_dir: Optional[str] = None) -> Path:
    base = archive_dir or os.environ.get("VODRIP_ARCHIVE_DIR", "") or (
        _get_appdata_dir() / "archive"
    )
    return Path(base) / "kick"


def _ensure_job(job_id: str, video_id: str) -> None:
    try:
        archive_db.enqueue_job(job_id, "ingest", PLATFORM, video_id, priority=0)
    except sqlite3.IntegrityError:
        pass  # already queued by a previous run


def _download_with_budget(
    url: str, out_path: str, budget_sec: float, quality: Optional[str]
) -> dict:
    """download_vod_sync in a daemon thread, hard-capped at budget_sec via
    cancel_event (the HLS fetcher and ffmpeg pump poll it)."""
    cancel_event = threading.Event()
    result: dict = {}

    def _run() -> None:
        try:
            kick_api_service.download_vod_sync(
                url, out_path, quality=quality, cancel_event=cancel_event
            )
            result["ok"] = True
        except BaseException as exc:  # noqa: BLE001 — report every failure mode
            result["ok"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=_run, daemon=True, name="kick-archive-dl")
    t.start()
    t.join(max(1.0, budget_sec))
    if t.is_alive():
        cancel_event.set()
        t.join(20.0)  # grace for segment fetchers / ffmpeg to observe the cancel
        if t.is_alive():
            return {"ok": False, "error": f"timed out after {budget_sec:.0f}s and did not stop on cancel"}
        return {"ok": False, "error": f"timed out after {budget_sec:.0f}s (cancelled)"}
    return result


def _record_note(video_id: str, canonical_key: Optional[str], note: str) -> None:
    """Persist a skip/failure reason. videos has no note column, so it goes
    to video_aliases.note (surfaced by dedupe_view) when a key exists."""
    if canonical_key:
        archive_db.set_alias(PLATFORM, video_id, canonical_key, note)
    else:
        logger.warning("archive_kick: no canonical_key for %s — note only in job: %s", video_id, note)


def _ingest_one(
    v: KickVideo,
    slug: str,
    *,
    download: bool,
    max_download_sec: float,
    quality: Optional[str],
    archive_dir: Optional[str],
) -> dict:
    key = _canonical_key(v.title, v.created_at)
    base = {
        "platform": PLATFORM,
        "video_id": v.id,
        "channel": slug,
        "title": v.title,
        "started_at": v.created_at,
        "duration_sec": v.duration,
        "archive_path": None,
        "canonical_key": key,
        "status": "known",
    }

    # Re-run guard (BEFORE the metadata upsert, which would overwrite
    # status): a previously completed download is not re-fetched, and the
    # refresh preserves status='ready' + archive_path (upserting the bare
    # base dict would clobber both).
    existing = archive_db.query(
        "SELECT status, archive_path, content_sha256 FROM videos WHERE platform=? AND video_id=?",
        (PLATFORM, v.id),
    )
    if existing and existing[0]["status"] == "ready":
        path = existing[0]["archive_path"]
        if path and Path(path).is_file():
            ready = {**base, "status": "ready", "archive_path": path}
            if not existing[0]["content_sha256"]:
                # Lazy one-time backfill for rows that predate the hash
                # column: hashing the file lets future duplicate downloads
                # dedupe onto it. Best-effort — the row is already valid.
                try:
                    ready["content_sha256"] = archive_content_dedup.sha256_file(path)
                except OSError:
                    pass
            archive_db.upsert_video(ready)
            _enforce_retention()
            return {**base, "action": "already_ready", "status": "ready",
                    "archive_path": path, "seconds_spent": 0.0}

    archive_db.upsert_video(base)  # metadata first — archive lists it regardless of download
    job_id = f"kick-{v.id}"
    _ensure_job(job_id, v.id)

    action, blocker = dedupe_decision(key, archive_db.dedupe_view())
    if action == "skip":
        note = f"skipped: same content already archived on {blocker} (youtube > twitch > kick)"
        _record_note(v.id, key, note)
        archive_db.update_job(job_id, status="done", error=note)
        logger.info("archive_kick: %s SKIP (dup on %s): %s", v.id, blocker, v.title)
        return {**base, "action": "skipped_dup", "blocker": blocker, "note": note, "seconds_spent": 0.0}

    if not download:
        note = "metadata only (download disabled)"
        archive_db.update_job(job_id, status="done", error=note)
        return {**base, "action": "metadata_only", "note": note, "seconds_spent": 0.0}

    out_dir = _archive_dir(archive_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{v.id}.mp4"
    t0 = time.monotonic()
    outcome = _download_with_budget(v.url or f"https://kick.com/{slug}/videos/{v.id}",
                                    str(out_path), max_download_sec, quality)
    spent = time.monotonic() - t0

    if outcome.get("ok"):
        # Content dedup: when the bytes already exist under another row, the
        # fresh copy is dropped and BOTH rows reference the one file.
        reg = archive_content_dedup.register_archive_file(
            str(out_path), platform=PLATFORM, video_id=v.id
        )
        archive_db.upsert_video({**base, "status": "ready",
                                 "archive_path": reg["archive_path"],
                                 "content_sha256": reg["content_sha256"]})
        archive_db.update_job(job_id, status="done")
        logger.info("archive_kick: %s DOWNLOADED (%ds): %s", v.id, int(spent), v.title)
        _enforce_retention()
        return {**base, "action": "downloaded", "status": "ready",
                "archive_path": reg["archive_path"],
                "seconds_spent": round(spent, 1)}

    err = str(outcome.get("error", "unknown failure"))
    try:
        out_path.unlink(missing_ok=True)  # drop partial file
    except OSError:
        pass
    archive_db.upsert_video({**base, "status": "failed"})
    note = f"download failed: {err}"
    _record_note(v.id, key, note)
    archive_db.update_job(job_id, status="failed", error=err)
    logger.warning("archive_kick: %s FAILED (%ds): %s", v.id, int(spent), err)
    return {**base, "action": "failed", "status": "failed", "error": err,
            "seconds_spent": round(spent, 1)}


def ingest_channel(
    slug: str,
    *,
    limit: int = 3,
    download: bool = True,
    max_download_sec: float = _DEFAULT_BUDGET_SEC,
    quality: Optional[str] = "720",
    archive_dir: Optional[str] = None,
) -> list[dict]:
    """List the channel's recent VODs, upsert metadata, apply the dedupe
    rule, then best-effort download kick-exclusive ones.

    ponytail: quality defaults to 720p so a budget-capped run archives
    useful footage without pulling a multi-GB 1080p60 stream; pass
    quality=None for the top variant when the budget allows.
    """
    slug = (slug or "").strip().lower()
    if not slug:
        raise ValueError("Kick channel slug is required")
    vids = kick_api_service.list_channel_videos_api(slug, limit)
    return [
        _ingest_one(v, slug, download=download, max_download_sec=max_download_sec,
                    quality=quality, archive_dir=archive_dir)
        for v in vids[: max(1, int(limit))]
    ]


# --- self-check (pure logic; no network, no DB) ---------------------------

assert _canonical_key("Watchparty do Mundial!", "2026-08-01T22:30:00Z") == "watchparty-do-mundial|2026-08-01", (
    "canonical key must match the shared cross-platform format"
)
assert _canonical_key("Último dia do Mundial!", "2026-08-01T22:30:00Z") == "ultimo-dia-do-mundial|2026-08-01", (
    "diacritics must be stripped before collapsing (Último == ULTIMO)"
)
assert _canonical_key("ULTIMO DIA DO MUNDIAL!", "2026-08-01T22:30:00Z") == "ultimo-dia-do-mundial|2026-08-01"
assert _canonical_key("  LOL CLASSICO  CHEGOU HJ !PIX ", "2026-07-30T23:21:30Z") == "lol-classico-chegou-hj-pix|2026-07-30"
assert _canonical_key("No Date", None) == "no-date"
assert _canonical_key("!!!", "2026-08-01") == "untitled|2026-08-01"
assert _start_date("2026-07-30 23:21:30") == "2026-07-30"
assert dedupe_decision("k|2026-01-01", [{"canonical_key": "k|2026-01-01", "videos": [{"platform": "youtube"}, {"platform": "kick"}]}]) == ("skip", "youtube")
assert dedupe_decision("k|2026-01-01", [{"canonical_key": "k|2026-01-01", "videos": [{"platform": "twitch"}]}]) == ("skip", "twitch")
assert dedupe_decision("k|2026-01-01", [{"canonical_key": "k|2026-01-01", "videos": [{"platform": "kick"}]}]) == ("download", None)
assert dedupe_decision("k|2026-01-01", [{"canonical_key": "k|2026-01-01", "videos": [{"platform": "kick"}, {"platform": "youtube"}, {"platform": "twitch"}]}]) == ("skip", "youtube")
assert dedupe_decision("k|2026-01-01", []) == ("download", None)
assert dedupe_decision(None, [{"canonical_key": "x", "videos": [{"platform": "youtube"}]}]) == ("download", None)


if __name__ == "__main__":
    import json

    _slug = os.environ.get("VODRIP_KICK_CHANNEL", "titiltei")
    _limit = int(os.environ.get("VODRIP_KICK_LIMIT", "3"))
    _no_dl = os.environ.get("VODRIP_KICK_NO_DOWNLOAD", "").strip().lower() in ("1", "true")
    print(json.dumps(
        ingest_channel(_slug, limit=_limit, download=not _no_dl),
        indent=2, ensure_ascii=False, default=str,
    ))
