"""
Twitch clip creation — semi-automatic editor route + local history.

Creating a clip with the official Helix API (POST /helix/videos/clips) needs
OAuth with channel:manage:clips (broadcaster) or editor:manage:clips
(editor) — a plain viewer cannot create clips on arbitrary channels. Instead
we open Twitch's own clip editor pre-positioned on the selected moment, the
route Chatterino uses (internal UI route, not a stable public API):

    https://clips.twitch.tv/create?vodID=<id>&broadcasterLogin=<login>&offsetSeconds=<sec>

The user is logged in in their default browser, fine-tunes the range (5-60s)
and publishes there. Every editor-open is recorded to
<data_dir>/twitch_clips.json so the UI can show a history of clip attempts.

The URL is opened in the OS default browser (never inside the WebView2
window, whose cookie store is separate from the user's browser session).
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from deps import OS_EXECUTOR
from services.disk_hygiene import data_dir

logger = logging.getLogger(__name__)
router = APIRouter(tags=["twitch-clips"])

TWITCH_CLIP_MAX_SEC = 60
TWITCH_LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")
HISTORY_CAP = 200
HISTORY_FILE = "twitch_clips.json"
EDITOR_HOST = "https://clips.twitch.tv/create"


class TwitchClipRequest(BaseModel):
    broadcaster_login: str
    vod_id: Optional[str] = None
    offset_sec: Optional[int] = None
    duration_sec: Optional[int] = None
    open_browser: bool = True


def _history_path() -> Path:
    return data_dir() / HISTORY_FILE


def _load_history() -> List[Dict[str, Any]]:
    try:
        raw = _history_path().read_text("utf-8")
        entries = json.loads(raw)
        return entries if isinstance(entries, list) else []
    except FileNotFoundError:
        return []
    except Exception as exc:  # ponytail: corrupt/missing history must never fail the endpoint
        logger.warning("twitch clip history unreadable: %s", exc)
        return []


def _save_history(entries: List[Dict[str, Any]]) -> None:
    try:
        _history_path().parent.mkdir(parents=True, exist_ok=True)
        _history_path().write_text(
            json.dumps(entries, indent=2, ensure_ascii=False), "utf-8"
        )
    except Exception as exc:  # ponytail: history is best-effort, never blocks the open
        logger.warning("twitch clip history write failed: %s", exc)


def _open_in_default_browser(url: str) -> None:
    """Open the editor in the user's default browser (not the WebView2)."""
    try:
        import webbrowser

        webbrowser.open(url, new=2)
        return
    except Exception as exc:
        logger.debug("webbrowser.open failed, trying os.startfile: %s", exc)
    try:
        os.startfile(url)  # Windows: ShellExecute → default browser
    except Exception as exc:
        logger.warning("failed to open twitch clip editor URL: %s", exc)


@router.get("/api/twitch/clips/history")
async def twitch_clips_history(limit: int = 100) -> List[Dict[str, Any]]:
    return _load_history()[: max(1, min(limit, HISTORY_CAP))]


@router.post("/api/twitch/clip")
async def create_twitch_clip(req: TwitchClipRequest) -> Dict[str, Any]:
    login = (req.broadcaster_login or "").strip()
    if not TWITCH_LOGIN_RE.fullmatch(login):
        raise HTTPException(
            status_code=422,
            detail="invalid twitch broadcaster_login",
        )

    if req.vod_id is not None:
        if not req.vod_id.isdigit():
            raise HTTPException(status_code=422, detail="vod_id must be numeric")
        if req.offset_sec is None or req.offset_sec < 0:
            raise HTTPException(
                status_code=422, detail="offset_sec required for VOD clips"
            )
        if req.duration_sec is None or not (
            1 <= req.duration_sec <= TWITCH_CLIP_MAX_SEC
        ):
            raise HTTPException(
                status_code=422,
                detail=f"duration_sec must be 1..{TWITCH_CLIP_MAX_SEC}",
            )
        url = (
            f"{EDITOR_HOST}?vodID={req.vod_id}"
            f"&broadcasterLogin={login}"
            f"&offsetSeconds={int(req.offset_sec)}"
        )
    else:
        # Live stream: the web editor opens the channel's current broadcast
        # and lets the user pick the last-30s window. No vodID/offset to pass.
        if req.offset_sec is not None or req.duration_sec is not None:
            raise HTTPException(
                status_code=422,
                detail="offset/duration only valid with vod_id",
            )
        url = f"{EDITOR_HOST}?broadcasterLogin={login}"

    entry = {
        "id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "channel": login,
        "vod_id": req.vod_id,
        "offset_sec": req.offset_sec,
        "duration_sec": req.duration_sec,
        "url": url,
        "status": "editor_opened",
    }

    history = _load_history()
    history.insert(0, entry)
    _save_history(history[:HISTORY_CAP])

    if req.open_browser:
        OS_EXECUTOR.submit(_open_in_default_browser, url)

    return {"ok": True, "url": url, "id": entry["id"]}
