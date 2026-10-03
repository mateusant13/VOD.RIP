"""Sidecar files next to a finished download: transcript subtitles + chat .txt.

The transcript sidecar is an SRT (``<stem>.srt``) because that is the only
subtitle format video editors accept as a track (Premiere/DaVinci/Vegas/
FinalCut/Shotcut/Kdenlive/YouTube); a plain ``.txt`` is text to READ, not a
subtitle to import. Plain text stays available as an opt-in second file
(``AppSettings.download_transcript_sidecar_format``) for re-ingest/reading.

Two rules the file format alone does not buy:

* a TRIMMED download's cues are REBASED by ``crop_start`` — the media file
  starts at 0:00, so a cue carrying the source-VOD time is off by the trim
  point and lands the whole subtitle track out of sync;
* a missing transcript is never silent — the writer returns a status the
  caller surfaces (WARNING log + queue note), because "on by default" that
  quietly writes nothing is the failure mode this module exists to kill.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

from services.ytdlp_service import detect_platform
from utils import vod_id_from_url

logger = logging.getLogger(__name__)

# Accepted values of AppSettings.download_transcript_sidecar_format. 'srt' is
# the default (the editor-importable subtitle track); 'srt+txt' writes both;
# 'txt' is the legacy plain-text-only output.
SIDECAR_FORMAT_CHOICES = ("srt", "srt+txt", "txt")
_SIDECAR_EXT = {"srt": ".srt", "txt": ".txt"}

# A 10h ASR VOD carries tens of thousands of rows; a download worker thread
# has no reason to materialize more than this (mirrors chat_for's cap).
_TRANSCRIPT_ROW_LIMIT = 50_000

# YouTube caption cue bounds: the timedtext payloads carry a start per cue and
# no end, so the end is the next cue's start — clamped to keep a long gap (or a
# single-cue track) from producing a cue that swallows half the video.
_CUE_MIN_S = 0.1
_CUE_MAX_S = 6.0


def resolve_transcript_formats(value: Optional[str]) -> tuple[str, ...]:
    """Normalize a stored format preference to the ordered extension set to
    write ('srt' first so it is the primary path)."""
    val = (value or "srt").strip().lower()
    if val not in SIDECAR_FORMAT_CHOICES:
        val = "srt"
    return tuple(val.split("+"))


def _hms(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def ids_from_url(url: str) -> tuple[str, str]:
    plat = (detect_platform(url) or "").lower()
    vid = vod_id_from_url(url)
    if not vid and plat == "youtube":
        try:
            from services.youtube_innertube import extract_video_id
            vid = extract_video_id(url) or ""
        except Exception:
            vid = ""
    if not vid:
        m = re.search(
            r"(?:clips\.twitch\.tv/|(?:twitch|kick)\.tv/[^/]+/clip/)([A-Za-z0-9_-]+)",
            url or "",
            re.I,
        )
        if m:
            vid = m.group(1)
    return plat, vid


def _cue_bounds(row: dict) -> tuple[float, float]:
    """(start_sec, end_sec) of one transcript row, tolerant of the caption
    shape (start only, no end) and of legacy offset_sec/duration rows."""
    start = float(row.get("start_sec") or row.get("offset_sec") or 0)
    end = row.get("end_sec")
    if end is None:
        dur = row.get("duration")
        end = start + (float(dur) if dur else 0.0)
    return start, float(end)


def fill_cue_ends(rows: list[dict]) -> list[dict]:
    """Give every row a real ``end_sec``: an endless row (YouTube timedtext
    cues carry a start only) borrows the NEXT cue's start, clamped to
    [_CUE_MIN_S, _CUE_MAX_S] so a long silence or a single-cue track cannot
    produce a cue that swallows half the video."""
    out: list[dict] = []
    for i, row in enumerate(rows):
        start, end = _cue_bounds(row)
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        if end <= start:
            end = _cue_bounds(nxt)[0] if nxt is not None else start
        end = min(max(end, start + _CUE_MIN_S), start + _CUE_MAX_S)
        new = dict(row)
        new["start_sec"] = start
        new["end_sec"] = end
        out.append(new)
    return out


def format_transcript_txt(rows: list[dict], rebase_sec: float = 0.0) -> str:
    """SRT body: cue index, ``HH:MM:SS,mmm --> HH:MM:SS,mmm``, blank line.

    ``rebase_sec`` shifts every cue by the download's ``crop_start`` so a
    TRIMMED download's subtitle starts at 0:00 and stays aligned with its own
    media file (without it a clip cut at 410s opens on a cue reading
    00:06:50). A cue that straddles the trim point clamps to 0:00 — never a
    negative timestamp, which every SRT reader rejects.
    """
    base = max(0.0, float(rebase_sec or 0.0))
    blocks: list[str] = []
    index = 0
    for row in rows:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        start, end = _cue_bounds(row)
        start = max(0.0, start - base)
        end = max(0.0, end - base)
        if end <= start:
            end = start + _CUE_MIN_S
        index += 1
        blocks.append(f"{index}\n{_hms(start)} --> {_hms(end)}\n{text}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def format_chat_txt(rows: list[dict]) -> str:
    """One `user: message` per line — deliberately NO timestamps (the user
    asked for plain text video-editor import; the marker range already
    bounds the file, so a per-line clock adds nothing but noise)."""
    lines: list[str] = []
    for row in rows:
        user = (row.get("username") or "user").strip()
        text = (row.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"{user}: {text}")
    return "\n".join(lines) + ("\n" if lines else "")


def _transcript_rows_in_range(
    rows: list[dict],
    crop_start: Optional[float],
    crop_end: Optional[float],
) -> list[dict]:
    """Transcript rows intersecting the trim window (a caption straddling a
    boundary is kept — it is audible inside the trim). Open sides stay open;
    both None returns the rows unchanged (whole transcript)."""
    if crop_start is None and crop_end is None:
        return rows
    lo = float(crop_start) if crop_start is not None else float("-inf")
    hi = float(crop_end) if crop_end is not None else float("inf")
    kept: list[dict] = []
    for r in rows:
        start, end = _cue_bounds(r)
        if start <= hi and end >= lo:
            kept.append(r)
    return kept


def _atomic_write_text(path: Path, body: str) -> None:
    """Write through a temp sibling + ``os.replace``: a crash mid-write can
    never leave a truncated ``.srt`` for a video editor to choke on, and an
    existing file is swapped atomically instead of being truncated first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _clip_source_vod_id(slug: str) -> Optional[str]:
    """Twitch clip slug -> recorded source VOD id (clip chat is archived
    under the source VOD, not the clip slug). Best-effort read of the clip
    history file; failures return None."""
    try:
        from services.disk_hygiene import data_dir
        import json as _json
        raw = (Path(data_dir()) / "twitch_clips.json").read_text("utf-8")
        for entry in _json.loads(raw):
            if entry.get("id") == slug and entry.get("vod_id"):
                return str(entry["vod_id"])
    except Exception:
        logger.debug("clip source VOD lookup skipped", exc_info=True)
    return None


def _youtube_caption_transcript(
    platform: str, video_id: str
) -> tuple[list[dict], Optional[str]]:
    """YouTube captions for a download that has no stored transcript yet.

    The download pipeline never archives, so ``transcript_source`` is empty for
    almost every plain download — and YouTube needs no ASR: the caption track
    already exists upstream. This reuses the ONE proven fetcher
    (``routers.subtitles._fetch_subtitles``: caption-first InnerTube tracklist,
    then yt-dlp; vtt -> json3 -> srv3 with 429 retry) rather than writing a
    second one, and stores the rows exactly like the archive ingest does
    (``archive_db.insert_transcript``) so the next download — and search —
    find them without a refetch. Returns ``(rows, lang)``; empty on miss.
    """
    from routers.subtitles import (  # lazy: router import inside a worker thread
        _fetch_subtitles,
        _subtitle_langs_default,
    )

    url = f"https://www.youtube.com/watch?v={video_id}"
    langs = [ln.strip() for ln in _subtitle_langs_default().split(",") if ln.strip()]
    payload = _fetch_subtitles(url, langs) or {}
    if not payload.get("has_subtitles"):
        return [], None
    segments = [
        {
            "seg_idx": i,
            "start_sec": float(r.get("offset_sec") or r.get("start_sec") or 0),
            "end_sec": float(r.get("end_sec") or 0),
            "text": str(r.get("text") or "").strip(),
            "words": r.get("words") or [],
        }
        for i, r in enumerate(payload.get("rows") or [])
        if str(r.get("text") or "").strip()
    ]
    if not segments:
        return [], None
    segments = fill_cue_ends(segments)
    lang = payload.get("lang")
    try:
        from services import archive_db

        if not archive_db.has_transcript(platform, video_id):
            archive_db.insert_transcript(platform, video_id, segments, lang=lang)
    except Exception:
        # A DB hiccup must not cost the user the .srt we already have in hand.
        logger.warning(
            "youtube captions for %s not archived (sidecar still written)",
            video_id, exc_info=True,
        )
    return segments, (str(lang) if lang else None)


def transcript_sidecar(
    output_file: str,
    platform: str,
    video_id: str,
    *,
    crop_start: Optional[float] = None,
    crop_end: Optional[float] = None,
    formats: Iterable[str] = ("srt",),
    allow_youtube_captions: bool = True,
) -> dict[str, Any]:
    """Write the transcript sidecar(s) and REPORT what happened.

    Resolution order: the archive's own rows -> a clip's source VOD rows ->
    (YouTube only) the upstream caption track. Returns::

        {"path": <primary path|None>, "paths": {"srt": ..., "txt": ...},
         "status": "written"|"unavailable"|"error", "source": str,
         "detail": str}

    ``status`` is what makes a missing transcript non-silent: the caller logs
    it at WARNING and shows ``detail`` on the queue item.
    """
    result: dict[str, Any] = {
        "path": None, "paths": {}, "status": "error", "source": "", "detail": "",
    }
    fmt_list = tuple(f for f in formats if f in _SIDECAR_EXT) or ("srt",)
    plat = (platform or "").strip().lower()
    vid = (video_id or "").strip()
    if not plat or not vid or not output_file:
        result["detail"] = "no platform/video id for the transcript sidecar"
        return result

    rows: list[dict] = []
    try:
        from services import archive_db

        src = archive_db.transcript_source(plat, vid)
        if src:
            result["source"] = "archive"
            rows = archive_db.transcript_for(src[0], src[1], limit=_TRANSCRIPT_ROW_LIMIT)
        elif plat in ("twitch", "kick"):
            # A clip download carries the CLIP SLUG as its video id, but the
            # transcript lives under the SOURCE VOD — same miss the chat
            # writer already resolves, and the reason clip transcripts almost
            # never fired before.
            vod_id = _clip_source_vod_id(vid)
            if vod_id:
                src = archive_db.transcript_source(plat, vod_id)
                if src:
                    result["source"] = "archive-clip-vod"
                    rows = archive_db.transcript_for(
                        src[0], src[1], limit=_TRANSCRIPT_ROW_LIMIT
                    )
    except Exception:
        logger.warning("transcript sidecar lookup failed for %s/%s", plat, vid, exc_info=True)
        result["detail"] = f"archive lookup failed for {plat}/{vid}"
        return result

    if not rows and plat == "youtube" and allow_youtube_captions:
        # No archived transcript and no ASR needed: fetch the captions the
        # uploader (or YouTube's auto-captions) already published.
        try:
            rows, _lang = _youtube_caption_transcript(plat, vid)
        except Exception:
            logger.warning(
                "youtube caption fetch failed for %s", vid, exc_info=True
            )
            rows = []
        if rows:
            result["source"] = "youtube-captions"
    if not rows and not result["source"]:
        result["source"] = "archive"

    kept = _transcript_rows_in_range(rows, crop_start, crop_end) if rows else []
    body = format_transcript_txt(kept, rebase_sec=crop_start or 0.0)
    if not body.strip():
        result["status"] = "unavailable"
        if rows:
            result["detail"] = (
                f"transcript found but no cue falls inside the trimmed window "
                f"({crop_start}-{crop_end}s)"
            )
        else:
            result["detail"] = (
                f"no transcript for {plat}/{vid} — "
                + (
                    "YouTube served no caption track"
                    if plat == "youtube"
                    else "archive it first (Twitch/Kick need ASR)"
                )
            )
        return result

    stem = Path(output_file)
    for fmt in fmt_list:
        path = stem.with_suffix(_SIDECAR_EXT[fmt])
        try:
            _atomic_write_text(path, body)
        except Exception:
            logger.warning("transcript sidecar write failed: %s", path, exc_info=True)
            continue
        result["paths"][fmt] = str(path)
    if not result["paths"]:
        result["status"] = "error"
        result["detail"] = f"could not write {', '.join(fmt_list)} next to {stem.name}"
        return result
    result["path"] = next(
        (result["paths"][f] for f in fmt_list if f in result["paths"]), None
    )
    result["status"] = "written"
    return result


def write_transcript_sidecar(
    output_file: str,
    platform: str,
    video_id: str,
    *,
    crop_start: Optional[float] = None,
    crop_end: Optional[float] = None,
    formats: Iterable[str] = ("srt",),
    allow_youtube_captions: bool = True,
) -> Optional[str]:
    """Primary transcript sidecar path, or None. Thin wrapper over
    :func:`transcript_sidecar` for callers that only need the path; use the
    latter when the outcome must be surfaced (it distinguishes
    'unavailable' from 'could not write')."""
    return transcript_sidecar(
        output_file, platform, video_id,
        crop_start=crop_start, crop_end=crop_end,
        formats=formats, allow_youtube_captions=allow_youtube_captions,
    )["path"]


def write_chat_sidecar(
    dest: str,
    platform: str,
    video_id: str,
    *,
    start_sec: Optional[float] = None,
    end_sec: Optional[float] = None,
) -> Optional[str]:
    if not platform or not video_id:
        return None
    try:
        from services import archive_db
        rows = archive_db.chat_for(platform, video_id)
        if not rows and platform.lower() == "twitch":
            # A clip download carries the clip slug as its video id, but the
            # chat archive lives under the source VOD id — resolve it so clip
            # downloads get their chat txt too.
            vod_id = _clip_source_vod_id(video_id)
            if vod_id:
                rows = archive_db.chat_for(platform, vod_id)
        if start_sec is not None or end_sec is not None:
            lo = float(start_sec) if start_sec is not None else float("-inf")
            hi = float(end_sec) if end_sec is not None else float("inf")
            rows = [r for r in rows if lo <= float(r.get("offset_sec") or 0) <= hi]
        body = format_chat_txt(rows)
        if not body.strip():
            return None
        path = Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return str(path)
    except Exception:
        logger.debug("chat sidecar skipped", exc_info=True)
        return None


_THUMB_SIZE_CAP = 8 * 1024 * 1024
_THUMB_FETCH_TIMEOUT = 10.0


def _resolve_thumb_placeholders(url: str) -> str:
    """Substitute Twitch/Kick %{width}x%{height} thumbnail templates (mirror
    of the frontend resolveVideoThumbnail, 48x36 keeps the file small)."""
    return (
        url.replace("%{width}", "48")
        .replace("%{height}", "36")
        .replace("{width}", "48")
        .replace("{height}", "36")
    )


def write_thumbnail_sidecar(thumbnail: Optional[str], output_file: str) -> Optional[str]:
    """Persist a local thumbnail next to the finished download.

    Prefers a yt-dlp-written sidecar (<stem>.jpg/.webp from writethumbnail);
    else downloads the remote thumbnail URL to <stem>.thumb.jpg. Returns the
    local path or None. ponytail: best-effort — a thumb failure must never
    fail the download; the queue UI falls back to the Play placeholder."""
    try:
        if not output_file:
            return None
        stem = Path(output_file)
        for ext in (".jpg", ".jpeg", ".webp", ".png"):
            candidate = stem.with_suffix(ext)
            if candidate.is_file() and candidate.stat().st_size > 0:
                return str(candidate)
        if not thumbnail or not str(thumbnail).lower().startswith(
            ("http://", "https://", "file://")
        ):
            return None
        import urllib.request

        url = _resolve_thumb_placeholders(str(thumbnail))
        req = urllib.request.Request(
            url, headers={"User-Agent": __import__("services._version", fromlist=["USER_AGENT"]).USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=_THUMB_FETCH_TIMEOUT) as resp:
            body = resp.read(_THUMB_SIZE_CAP + 1)
        if len(body) > _THUMB_SIZE_CAP or not body:
            return None
        dest = Path(f"{stem}.thumb.jpg")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return str(dest)
    except Exception:
        logger.debug("thumbnail sidecar skipped", exc_info=True)
        return None


def resolve_remote_thumbnail(url: str, platform: Optional[str]) -> Optional[str]:
    """Best-effort thumbnail URL for a download that has none yet.

    YouTube is derived locally (i.ytimg.com pattern, same as the frontend);
    Twitch/Kick ask their APIs (GQL/Kick) once per download, short timeout.
    ponytail: failure returns None — the sidecar is best-effort by design."""
    plat = (platform or "").lower()
    try:
        if plat == "youtube":
            m = re.search(
                r"(?:[?&]v=|youtu\.be/|/shorts/|/live/)([a-zA-Z0-9_-]{11})",
                url or "",
            )
            if m:
                return f"https://i.ytimg.com/vi/{m.group(1)}/mqdefault.jpg"
        elif plat == "twitch":
            from services.ytdlp_service import is_clip_url
            if is_clip_url(url):
                from services.twitch_gql_service import get_clip_info_sync
                return (get_clip_info_sync(url) or {}).get("thumbnail")
            from services.twitch_gql_service import get_video_info_sync
            return (get_video_info_sync(url) or {}).get("thumbnail")
        elif plat == "kick":
            from services.kick_api_service import (
                get_clip_info_sync as kick_clip,
                get_video_info_sync as kick_video,
            )
            from services.ytdlp_service import is_clip_url
            fn = kick_clip if is_clip_url(url) else kick_video
            return (fn(url) or {}).get("thumbnail")
    except Exception:
        logger.debug("remote thumbnail resolution skipped", exc_info=True)
    return None


def write_download_sidecars(
    output_file: str,
    url: str,
    *,
    include_transcript: bool,
    include_chat: bool,
    crop_start: Optional[float],
    crop_end: Optional[float],
    chat_start_sec: Optional[float],
    chat_end_sec: Optional[float],
    platform: Optional[str] = None,
    transcript_formats: Optional[str] = None,
) -> dict[str, Any]:
    plat, vid = ids_from_url(url)
    if platform:
        plat = platform.lower()
    out: dict[str, Any] = {}
    if include_transcript:
        res = transcript_sidecar(
            output_file, plat, vid,
            crop_start=crop_start, crop_end=crop_end,
            formats=resolve_transcript_formats(transcript_formats),
        )
        out["transcript"] = res["path"]
        # Added keys — the caller surfaces a non-"written" status instead of
        # letting an empty sidecar look like a successful download.
        out["transcript_paths"] = res["paths"]
        out["transcript_status"] = res["status"]
        out["transcript_detail"] = res["detail"]
        out["transcript_source"] = res["source"]
    if include_chat:
        # The chat txt range is the user's START/END chat markers when set;
        # absent markers fall back to the download's trim window so the
        # exported chat matches the downloaded section; no trim at all
        # leaves the range open (whole archived chat).
        chat_path = str(Path(output_file).with_suffix(".chat.txt"))
        cs, ce = chat_start_sec, chat_end_sec
        if cs is None and ce is None:
            cs, ce = crop_start, crop_end
        out["chat"] = write_chat_sidecar(
            chat_path, plat, vid,
            start_sec=cs, end_sec=ce,
        )
    return out
