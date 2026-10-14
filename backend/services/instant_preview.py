"""Instant previews — the first 6 seconds of each saved channel's newest VOD.

For every saved channel the archive scheduler kicks a background pass that
picks EXACTLY ONE platform (first available in the order twitch > kick >
youtube — Twitch GQL is the app's most mature extractor, then Kick API, then
YouTube which is bot-gated) and downloads only the first
``PREVIEW_DURATION_SEC`` seconds of the channel's most recent VOD into
``<data_dir>/previews/<channel_id>.mp4`` + a ``<channel_id>.json`` sidecar.

The pass is best-effort end to end: any failure (extract, download, gate)
skips the channel for this pass and the next periodic pass retries. YouTube
work honours the process-wide bot-gate (services.yt_gate — the same gate the
archive worker uses): the platform is never hit while the freeze is active,
and a gate error we trigger arms the cooldown so the pacing stays intact.
The pass runs on its own daemon thread so the scheduler loop never blocks on
a download (yt-dlp's process-wide lock serializes real downloads anyway).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PREVIEW_DURATION_SEC = 6.0  # constant: the first 6 seconds of the VOD
PLATFORM_ORDER = ("twitch", "kick", "youtube")
_SLUG_KEYS = {"twitch": "twitchSlug", "kick": "kickSlug", "youtube": "youtubeSlug"}

_worker_thread: Optional[threading.Thread] = None
_worker_lock = threading.Lock()

# --- cache ceiling -----------------------------------------------------------
# Measured read-only on this box, 2026-10-13, from <data_dir>/previews
# (H:\VOD.RIP-data\previews, the live archive root):
#   66 preview files = 320,615,319 bytes (305.8 MiB); median file 4.7 MiB;
#   largest single file 6,913,258 bytes (a 2560x1440 VOD sliced at 1080p60).
#   29 of those 66 files (144,735,411 bytes = 138.0 MiB) belonged to channels
#   that are NOT in saved_channels any more, and nothing ever reclaimed them:
#   the only deletion path was remove_channel_previews(), which the settings
#   route calls for the one channel it is deleting right now.
# The ceiling is therefore a TOTAL over the directory, not a per-channel count:
# one channel owns exactly one mp4 (a newer VOD overwrites the same path), so a
# per-channel count can only ever be 1 and would be a law that cannot fail.
# 200 MiB sits ABOVE the measured live set (37 live channels = 167.7 MiB at
# ~4.6 MiB each) and BELOW the measured bloated directory, so the first sweep
# after this lands reclaims dead weight instead of a live preview.
PREVIEW_CACHE_MAX_BYTES = 200 * 1024 * 1024
# Room to land one more preview before the next sweep. Two files at the
# measured largest size (6.9 MiB) fit inside it, so the pass that fills the
# directory is not the pass that empties it.
PREVIEW_CACHE_KEEP_FREE_BYTES = 16 * 1024 * 1024


def previews_dir() -> Path:
    """Root dir for instant preview files — the shared data root (disk_hygiene.data_dir)."""
    from services.disk_hygiene import data_dir

    return data_dir() / "previews"


def _preview_paths(channel_id: str) -> tuple[Path, Path]:
    cid = str(channel_id or "").strip()
    if not cid:
        raise ValueError("channel_id required")
    d = previews_dir()
    return d / f"{cid}.mp4", d / f"{cid}.json"


def _slug(channel: dict, platform: str) -> str:
    return (channel.get(_SLUG_KEYS[platform]) or "").strip()


def pick_platform(channel: dict) -> Optional[str]:
    """First platform with a slug, in twitch > kick > youtube order."""
    for p in PLATFORM_ORDER:
        if _slug(channel, p):
            return p
    return None


def _youtube_gate_active() -> bool:
    from services.yt_gate import youtube_gate_active

    return youtube_gate_active()


def _note_gate_if_youtube(exc: BaseException) -> None:
    """Arm the process-wide YouTube bot-gate when a failure is a gate signal
    (same classifier the archive worker uses — keeps pacing intact)."""
    from services.yt_gate import classify_youtube_gate_error, note_youtube_gate

    if classify_youtube_gate_error(exc):
        # surface/origin: this is the on-demand preview the user is
        # watching — a user-initiated metadata request, not worker traffic.
        note_youtube_gate(str(exc)[:200], surface="metadata", origin="user")


# ---------------------------------------------------------------------------
# Platform legs — module-level so tests inject fakes without network.
# ---------------------------------------------------------------------------

def _fetch_latest_vod(channel: dict, platform: str) -> Optional[dict]:
    """Metadata of the channel's most recent VOD on ONE platform, or None.

    Reuses the app's existing per-channel newest-VOD fetchers verbatim:
    Twitch GQL videos query (twitch_gql_service), Kick VOD API
    (kick_api_service), YouTube channel-streams extract (youtube_service).
    Returned dict is the normalized preview contract: {platform, title,
    vod_url, vod_id, video_id, duration_sec}.
    """
    if platform == "twitch":
        from services.twitch_gql_service import list_channel_videos_sync

        rows = list_channel_videos_sync(_slug(channel, "twitch").lower(), limit=1, source="user")
        if not rows:
            return None
        r = rows[0]
        vid = str(r.get("id") or "")
        return {
            "platform": "twitch",
            "title": r.get("title") or "Untitled",
            "vod_url": f"https://www.twitch.tv/videos/{vid}",
            "vod_id": vid,
            "video_id": None,
            "duration_sec": r.get("duration"),
        }
    if platform == "kick":
        from services.kick_api_service import list_channel_videos_api

        vids = list_channel_videos_api(_slug(channel, "kick").lower(), limit=1, origin="user")
        if not vids:
            return None
        v = vids[0]
        slug = _slug(channel, "kick").lower()
        vid = str(v.id or "")
        return {
            "platform": "kick",
            "title": v.title or "Untitled",
            "vod_url": v.url or f"https://kick.com/{slug}/videos/{vid}",
            "vod_id": vid,
            "video_id": None,
            "duration_sec": v.duration,
        }
    # youtube — the app's channel browser lists past live streams via the
    # /streams tab (same convention as routers/channels.py content=streams).
    from services.youtube_service import list_channel_videos_sync

    rows = list_channel_videos_sync(_slug(channel, "youtube"), limit=1, playlist="streams")
    if not rows:
        return None
    r = rows[0]
    vid = str(r.get("id") or "")
    return {
        "platform": "youtube",
        "title": r.get("title") or "Untitled",
        "vod_url": f"https://www.youtube.com/watch?v={vid}",
        "vod_id": None,
        "video_id": vid,
        "duration_sec": r.get("duration"),
    }


def _download_range(url: str, output_path: Path, start_sec: float, end_sec: float) -> None:
    """Slice start_sec..end_sec of a VOD via the app's proven clip download
    path (ytdlp_download.download_video_sync — the same function the
    /api/download/clip endpoint runs: HLS section downloads with
    download_ranges / ffmpeg trim)."""
    from deps import settings_mgr
    from services.ytdlp_download import download_video_sync

    output_path.parent.mkdir(parents=True, exist_ok=True)
    download_video_sync(
        url=url,
        output_path=str(output_path),
        quality="720",
        crop_start=start_sec,
        crop_end=end_sec,
        settings_mgr=settings_mgr,
    )


def _drop_orphan_previews(known: set) -> int:
    """Delete previews whose channel is no longer saved, and count the bytes.

    MEASURED (read-only, 2026-10-13, H:\VOD.RIP-data\previews): 29 of the 66
    files there — 144,735,411 bytes = 138.0 MiB — belong to channel ids that
    are NOT in saved_channels. The only deletion path before this was
    remove_channel_previews(), which the settings route calls for the one
    channel it is deleting right now, so any other removal (an indexed
    channel being forgotten, a settings file rolled back) left its 4-6 MiB
    payload behind forever. LRU alone cannot fix that: a channel deleted
    yesterday has a newer mtime than a quiet live channel nobody has
    refreshed in a month, so the ceiling would eat the live one first.
    """
    d = previews_dir()
    if not d.is_dir():
        return 0
    freed = 0
    for jp in list(d.glob("*.json")):
        cid = jp.stem
        if cid in known:
            continue
        mp4 = d / f"{cid}.mp4"
        for path in (jp, mp4):
            try:
                if path.is_file():
                    freed += path.stat().st_size
                    path.unlink()
            except OSError:
                logger.debug("orphan preview cleanup failed for %s", path, exc_info=True)
    for mp4 in list(d.glob("*.mp4")):
        if mp4.stem in known:
            continue
        try:
            if mp4.is_file():
                freed += mp4.stat().st_size
                mp4.unlink()
        except OSError:
            logger.debug("orphan mp4 cleanup failed for %s", mp4, exc_info=True)
    if freed:
        logger.info("previews cache: dropped %d orphan bytes (%d channels kept)", freed, len(known))
    return freed


def _enforce_preview_cache_ceiling(keep: Optional[Path] = None) -> int:
    """Bound the previews directory to PREVIEW_CACHE_MAX_BYTES, LRU first.

    A preview is one mp4 + its json sidecar, so the pair is evicted together
    (an mp4 with no sidecar is invisible to GET /api/previews/status; a
    sidecar with no mp4 advertises a media_url that 404s). Least-recently-used
    by mtime goes first. *keep* names the pair the caller just produced, so a
    refresh that lands above the ceiling still serves itself.

    Best-effort end to end: a file that cannot be unlinked is skipped, never
    raised, because this runs on the scheduler's background thread.
    """
    d = previews_dir()
    if not d.is_dir():
        return 0
    keep_mp4 = Path(keep) if keep is not None else None
    pairs: list[tuple[float, Path, Path, int]] = []
    total = 0
    for jp in d.glob("*.json"):
        cid = jp.stem
        mp4 = d / f"{cid}.mp4"
        if not mp4.is_file():
            continue  # dangling sidecar: no media, counts for nothing
        try:
            size = jp.stat().st_size + mp4.stat().st_size
            mtime = mp4.stat().st_mtime
        except OSError:
            continue
        pairs.append((mtime, mp4, jp, size))
        total += size
    target = PREVIEW_CACHE_MAX_BYTES - PREVIEW_CACHE_KEEP_FREE_BYTES
    if total <= target:
        return 0
    freed = 0
    for _mtime, mp4, jp, size in sorted(pairs, key=lambda item: item[0]):
        if total - freed <= target:
            break
        if keep_mp4 is not None and mp4 == keep_mp4:
            continue
        for path in (mp4, jp):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.debug("preview eviction failed for %s", path, exc_info=True)
        freed += size
    if freed:
        logger.info(
            "previews cache: evicted %d bytes to stay under %d (dir was %d)",
            freed, PREVIEW_CACHE_MAX_BYTES, total,
        )
    return freed


# ---------------------------------------------------------------------------
# Pass
# ---------------------------------------------------------------------------

def _preview_current(channel_id: str, latest: dict) -> bool:
    """True when the on-disk preview already covers this exact VOD (skip re-download)."""
    mp4_path, json_path = _preview_paths(channel_id)
    if not mp4_path.is_file() or not json_path.is_file():
        return False
    try:
        sidecar = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        sidecar.get("platform") == latest["platform"]
        and sidecar.get("vod_url") == latest["vod_url"]
    )


def _probe_id(latest: dict) -> Optional[str]:
    """Stable identity of the newest VOD the preview gate keys on."""
    return (
        latest.get("vod_url")
        or latest.get("vod_id")
        or latest.get("video_id")
    )


def _refresh_channel(channel: dict, channel_id: str) -> Optional[Path]:
    """Refresh one channel's preview; returns the mp4 it wrote, else None.
    The caller passes that path to the ceiling sweep as *keep*: the preview
    the user is about to read must not be the one evicted."""
    platform = pick_platform(channel)
    if platform is None:
        return None  # no slug on any platform — nothing to preview
    if platform == "youtube" and _youtube_gate_active():
        return None  # bot-gate freeze — never hit YouTube while frozen (worker rule)
    # PROBE GATE: the newest-VOD metadata fetch is the probe, and the FFmpeg
    # slice follows it. With no proof of a new VOD since the last check, the
    # whole pass is skipped and the per-channel backoff advances, so a quiet
    # channel is not re-probed on every scheduler pass.
    from services import probe_gate, rate_budget

    if rate_budget.in_backoff(platform) or rate_budget.auto_exhausted(platform):
        return None  # budget dry — the next pass (cadence-stretched) will retry
    if not probe_gate.should_probe(platform, channel_id):
        return None
    latest = _fetch_latest_vod(channel, platform)
    if not latest:
        probe_gate.note_probe(platform, channel_id, found_new=False)
        raise RuntimeError(f"no VODs found on {platform}")
    if _preview_current(channel_id, latest):
        probe_gate.note_probe(
            platform, channel_id, found_new=False, latest_id=_probe_id(latest)
        )
        return None  # same VOD already previewed — no re-download
    mp4_path, json_path = _preview_paths(channel_id)
    try:
        _download_range(latest["vod_url"], mp4_path, 0.0, PREVIEW_DURATION_SEC)
    except Exception:
        # A failed download must not leave a stale sidecar advertising a
        # preview whose mp4 is missing/truncated — drop the pair.
        json_path.unlink(missing_ok=True)
        raise
    if not mp4_path.is_file() or mp4_path.stat().st_size == 0:
        raise RuntimeError("preview download produced no output")
    sidecar = {
        "platform": latest["platform"],
        "title": latest["title"],
        "vod_url": latest["vod_url"],
        "vod_id": latest["vod_id"],
        "video_id": latest["video_id"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_sec": PREVIEW_DURATION_SEC,
    }
    json_path.write_text(json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")
    probe_gate.note_probe(
        platform, channel_id, found_new=True, latest_id=_probe_id(latest)
    )
    logger.info(
        "instant preview %s: %s %s (first %.0fs)",
        channel_id, latest["platform"], latest["vod_url"], PREVIEW_DURATION_SEC,
    )
    return mp4_path


def run_pass(channels) -> None:
    """Best-effort pass over every saved channel. Any failure skips the
    channel and retries on the next pass — never raises out of the loop."""
    known = {str(ch.get("id") or "").strip() for ch in (channels or []) if isinstance(ch, dict)}
    known.discard("")
    # 1. Reclaim what no saved channel owns any more — the measured leak.
    _drop_orphan_previews(known)
    # 2. Bound the directory BEFORE the downloads: the pass must not write
    #    into an over-ceiling directory and then evict what it just produced.
    _enforce_preview_cache_ceiling()
    fresh: Optional[Path] = None
    for ch in channels or []:
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("id") or "").strip()
        if not cid:
            continue
        try:
            written = _refresh_channel(ch, cid)
            if written is not None:
                fresh = written
        except Exception as exc:  # noqa: BLE001 — one channel never blocks the rest
            logger.info("instant preview %s skipped: %s", cid, exc)
            _note_gate_if_youtube(exc)
    # 3. One more sweep catches the previews this pass just added — but never
    #    the one it just wrote, which the caller is about to read.
    _enforce_preview_cache_ceiling(keep=fresh)


def refresh_async(channels=None) -> None:
    """Kick a background pass (one at a time — a slow download pass never
    queues behind itself). Called from the archive scheduler pass, so a
    channel add/edit (kick_scheduler_pass) also refreshes previews."""
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_thread = threading.Thread(
            target=run_pass, args=(list(channels or []),), daemon=True,
            name="instant-preview",
        )
        _worker_thread.start()


def remove_channel_previews(channel_id: str) -> None:
    """Delete a channel's preview files (channel removed from saved_channels)."""
    cid = str(channel_id or "").strip()
    if not cid:
        return
    mp4_path, json_path = _preview_paths(cid)
    for p in (mp4_path, json_path):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            logger.debug("instant preview cleanup failed for %s", p, exc_info=True)


def list_previews() -> list[dict]:
    """Status entries for GET /api/previews/status — one per channel with a
    complete mp4+sidecar pair, sorted by channel_id."""
    d = previews_dir()
    if not d.is_dir():
        return []
    out: list[dict] = []
    for jp in sorted(d.glob("*.json")):
        cid = jp.stem
        if not (d / f"{cid}.mp4").is_file():
            continue  # mp4 evicted — never advertise a media_url that 404s
        try:
            sidecar = json.loads(jp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({
            "channel_id": cid,
            "platform": sidecar.get("platform"),
            "title": sidecar.get("title") or "",
            "vod_url": sidecar.get("vod_url") or "",
            "vod_id": sidecar.get("vod_id"),
            "video_id": sidecar.get("video_id"),
            "generated_at": sidecar.get("generated_at") or "",
            "media_url": f"/api/previews/{cid}/media",
        })
    return out


# --- self-check (pure logic; no network, no DB) -----------------------------

assert pick_platform({"id": "a", "twitchSlug": "t", "kickSlug": "k", "youtubeSlug": "y"}) == "twitch"
assert pick_platform({"id": "a", "twitchSlug": "", "kickSlug": "k", "youtubeSlug": "y"}) == "kick"
assert pick_platform({"id": "a", "twitchSlug": " ", "kickSlug": "", "youtubeSlug": "y"}) == "youtube"
assert pick_platform({"id": "a", "twitchSlug": "", "kickSlug": "", "youtubeSlug": ""}) is None
assert pick_platform({}) is None
