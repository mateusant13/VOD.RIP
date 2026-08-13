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
        note_youtube_gate(str(exc)[:200])


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

        rows = list_channel_videos_sync(_slug(channel, "twitch").lower(), limit=1)
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

        vids = list_channel_videos_api(_slug(channel, "kick").lower(), limit=1)
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


def _refresh_channel(channel: dict, channel_id: str) -> None:
    platform = pick_platform(channel)
    if platform is None:
        return  # no slug on any platform — nothing to preview
    if platform == "youtube" and _youtube_gate_active():
        return  # bot-gate freeze — never hit YouTube while frozen (worker rule)
    latest = _fetch_latest_vod(channel, platform)
    if not latest:
        raise RuntimeError(f"no VODs found on {platform}")
    if _preview_current(channel_id, latest):
        return  # same VOD already previewed — no re-download
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
    logger.info(
        "instant preview %s: %s %s (first %.0fs)",
        channel_id, latest["platform"], latest["vod_url"], PREVIEW_DURATION_SEC,
    )


def run_pass(channels) -> None:
    """Best-effort pass over every saved channel. Any failure skips the
    channel and retries on the next pass — never raises out of the loop."""
    for ch in channels or []:
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("id") or "").strip()
        if not cid:
            continue
        try:
            _refresh_channel(ch, cid)
        except Exception as exc:  # noqa: BLE001 — one channel never blocks the rest
            logger.info("instant preview %s skipped: %s", cid, exc)
            _note_gate_if_youtube(exc)


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
