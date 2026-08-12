"""Sidecar files next to a finished download: transcript .txt and chat .txt."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from services.ytdlp_service import detect_platform
from utils import vod_id_from_url

logger = logging.getLogger(__name__)


def _hms(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def _chat_clock(sec: float) -> str:
    sec = max(0, int(round(float(sec))))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


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


def format_transcript_txt(rows: list[dict]) -> str:
    blocks: list[str] = []
    for i, row in enumerate(rows, start=1):
        start = float(row.get("start_sec") or row.get("offset_sec") or 0)
        end = float(row.get("end_sec") or (start + float(row.get("duration") or 2)))
        if end <= start:
            end = start + 0.5
        text = (row.get("text") or "").strip()
        if not text:
            continue
        blocks.append(f"{i}\n{_hms(start)} --> {_hms(end)}\n{text}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def format_chat_txt(rows: list[dict]) -> str:
    lines: list[str] = []
    for row in rows:
        off = float(row.get("offset_sec") or 0)
        user = (row.get("username") or "user").strip()
        text = (row.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{_chat_clock(off)}] {user}: {text}")
    return "\n".join(lines) + ("\n" if lines else "")


def write_transcript_sidecar(output_file: str, platform: str, video_id: str) -> Optional[str]:
    if not platform or not video_id:
        return None
    try:
        from services import archive_db
        src = archive_db.transcript_source(platform, video_id)
        if not src:
            return None
        rows = archive_db.transcript_for(src[0], src[1])
        body = format_transcript_txt(rows)
        if not body.strip():
            return None
        path = Path(output_file).with_suffix(".txt")
        path.write_text(body, encoding="utf-8")
        return str(path)
    except Exception:
        logger.debug("transcript sidecar skipped", exc_info=True)
        return None


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


def write_download_sidecars(
    output_file: str,
    url: str,
    *,
    include_transcript: bool,
    include_chat: bool,
    crop_start: Optional[float],
    crop_end: Optional[float],
    chat_before_sec: float,
    chat_after_sec: float,
    platform: Optional[str] = None,
) -> dict[str, Any]:
    plat, vid = ids_from_url(url)
    if platform:
        plat = platform.lower()
    out: dict[str, Any] = {}
    if include_transcript:
        out["transcript"] = write_transcript_sidecar(output_file, plat, vid)
    if include_chat:
        start = None if crop_start is None else max(0.0, float(crop_start) - max(0.0, chat_before_sec))
        end = None if crop_end is None else float(crop_end) + max(0.0, chat_after_sec)
        chat_path = str(Path(output_file).with_suffix(".chat.txt"))
        out["chat"] = write_chat_sidecar(
            chat_path, plat, vid, start_sec=start, end_sec=end,
        )
    return out
