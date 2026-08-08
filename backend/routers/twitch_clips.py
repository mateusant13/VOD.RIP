"""
Twitch clip creation — official Helix API (Chatterino-style) + local history.

POST /api/twitch/clip calls Helix with the stored user token
(settings.twitch_helix_token — cookie-bridge auto-lift or manual paste in
Settings → Official APIs):

- VOD clips:  POST /helix/videos/clips  ?broadcaster_id=&editor_id=&vod_id=&vod_offset=&duration=&title=
              scopes editor:manage:clips | channel:manage:clips
              (query params per the official reference — NOT a JSON body;
              vod_offset = seconds from video START to clip END — the same
              END reference the frontend selection uses; duration 5..60s;
              title is required by the endpoint, blank -> broadcaster login)
- live clips: POST /helix/clips  ?broadcaster_id=&title=
              scope clips:edit (broadcaster must be live)

Success returns {ok: true, id, edit_url}; failures come back as
{ok: false, error: {code, message}} with a precise mapping:
401 unauthorized, 403 missing_scope, 404 not_found, 429 rate_limited,
422/503 clip_failed, plus no_token / network / invalid_request.

The edit_url (valid 24h) is opened by the FRONTEND in the OS default
browser — the backend never opens browsers. Every created clip is recorded
to <data_dir>/twitch_clips.json.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services import twitch_helix_service as ths
from services.disk_hygiene import data_dir

logger = logging.getLogger(__name__)
router = APIRouter(tags=["twitch-clips"])

TWITCH_CLIP_MAX_SEC = 60
TWITCH_CLIP_MIN_SEC = 5
TWITCH_CLIP_TITLE_MAX = 140
TWITCH_LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")
HISTORY_CAP = 200
HISTORY_FILE = "twitch_clips.json"

LIVE_CLIP_SCOPE = "clips:edit"
VOD_CLIP_SCOPES = ("editor:manage:clips", "channel:manage:clips")


class TwitchClipRequest(BaseModel):
    broadcaster_login: str
    vod_id: Optional[str] = None
    offset_sec: Optional[int] = None
    duration_sec: Optional[int] = None
    # User-chosen clip title ("" -> Helix VOD clips default to the broadcaster
    # login since the endpoint requires a title; live clips omit it so Twitch
    # auto-titles). Becomes the local filename on download.
    title: Optional[str] = None


class TwitchClipHistoryDeleteRequest(BaseModel):
    ids: List[str]


def _history_path() -> Path:
    return data_dir() / HISTORY_FILE


# Legacy pre-Helix rows opened clips.twitch.tv/create unconditionally; Twitch's
# SPA client-side-redirects that URL to its own /clips/500 error page for
# non-logged-in sessions. Those rows are permanently dead — drop them at read.
_LEGACY_CREATE_URL_PREFIX = "https://clips.twitch.tv/create"


def _load_history() -> List[Dict[str, Any]]:
    try:
        raw = _history_path().read_text("utf-8")
        entries = json.loads(raw)
        if not isinstance(entries, list):
            return []
        return [
            e
            for e in entries
            if not str(e.get("url", "")).startswith(_LEGACY_CREATE_URL_PREFIX)
        ]
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
    except Exception as exc:  # ponytail: history is best-effort, never blocks the response
        logger.warning("twitch clip history write failed: %s", exc)


def _error(code: str, message: str) -> Dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _helix_failure(exc: Exception) -> Dict[str, Any]:
    """Map a HelixError/RuntimeError to the {ok:false, error:{code,message}} contract."""
    status = getattr(exc, "status", None)
    base = {
        400: "Twitch rejected the clip request — check the selection and try again",
        401: "Twitch token is invalid or expired — re-authenticate or paste a fresh token in Settings → Official APIs",
        403: "Twitch rejected the token for clip creation — it needs "
        "editor:manage:clips or channel:manage:clips (live: clips:edit). "
        "Paste an OAuth token with the scope in Settings → Official APIs",
        404: "Video or broadcaster not found (or VODs are disabled on the channel)",
        429: "Twitch rate limit hit — wait a moment and try again",
        422: "Twitch rejected the clip request — check the selection and try again",
        503: "Twitch clip service is unavailable — try again in a moment",
    }.get(status)
    code = {
        400: "invalid_request",
        401: "unauthorized",
        403: "missing_scope",
        404: "not_found",
        429: "rate_limited",
        422: "clip_failed",
        503: "clip_failed",
    }.get(status, "clip_failed")

    if status is not None:
        base = base or "Twitch clip creation failed — try again in a moment"
        detail = getattr(exc, "message", None) or ""
        try:
            helix_msg = json.loads(detail).get("message") if detail else None
        except Exception:
            helix_msg = None
        message = f"{base} — {helix_msg}" if helix_msg else base
        return _error(code, message)
    # Network-level failure (URLError / timeout / malformed response).
    return _error("network", "Could not reach Twitch — check your connection and try again")


def _token_and_client() -> Dict[str, Any]:
    """Validate the stored token; {'error': ...} or {'client_id', 'scopes', 'login'}."""
    if not ths.token_available():
        return _error(
            "no_token",
            "No Twitch token configured — paste one in Settings → Official APIs "
            "(live clips need clips:edit; VOD clips need editor:manage:clips "
            "or channel:manage:clips)",
        )
    try:
        info = ths.token_info()
    except Exception as exc:
        return _helix_failure(exc)
    client_id = (info or {}).get("client_id")
    if not client_id:
        return _error(
            "unauthorized",
            "Twitch token validation failed — re-authenticate or paste a fresh "
            "token in Settings → Official APIs",
        )
    return {
        "client_id": client_id,
        "user_id": str((info or {}).get("user_id") or "").strip(),
        "scopes": set((info or {}).get("scopes") or []),
        "login": (info or {}).get("login"),
    }


def _record_clip(
    data: Dict[str, Any],
    login: str,
    req: TwitchClipRequest,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract id/edit_url from a successful Helix response and record history.

    ``title`` is the effective clip title sent to Helix (VOD clips always get
    one — blank defaults to the broadcaster login); defaults to req.title.
    """
    rows = (data or {}).get("data") or []
    if not rows:
        return _error("clip_failed", "Twitch did not return a clip — try again in a moment")
    clip_id = str(rows[0].get("id") or "").strip()
    edit_url = str(rows[0].get("edit_url") or "").strip()
    if not clip_id or not edit_url:
        return _error(
            "clip_failed", "Twitch clip response missing id/edit_url — try again"
        )

    entry = {
        "id": clip_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "channel": login,
        "vod_id": req.vod_id,
        "offset_sec": req.offset_sec,
        "duration_sec": req.duration_sec,
        "title": title if title is not None else req.title,
        "url": edit_url,
        "status": "created",
    }
    history = _load_history()
    history.insert(0, entry)
    _save_history(history[:HISTORY_CAP])
    return {"ok": True, "id": clip_id, "edit_url": edit_url}


def _create_vod_clip(req: TwitchClipRequest, login: str) -> Dict[str, Any]:
    info = _token_and_client()
    if "error" in info:
        return info
    if not (set(VOD_CLIP_SCOPES) & info["scopes"]):
        return _error(
            "missing_scope",
            "Twitch token lacks the VOD clip scope (needs editor:manage:clips "
            "or channel:manage:clips). The Cookie Bridge token is your "
            "twitch.tv browser login — Twitch does not grant clip scopes to "
            "it. Paste an OAuth token with editor:manage:clips or "
            "channel:manage:clips in Settings → Official APIs",
        )
    editor_id = info.get("user_id") or ""
    if not editor_id:
        return _error(
            "unauthorized",
            "Twitch token validation failed — re-authenticate or paste a fresh "
            "token in Settings → Official APIs",
        )
    try:
        # broadcaster_id/editor_id are required query params: the channel's
        # user id and the token user's id (editor_id must match the token).
        users = ths._helix_get(
            "/users", {"login": login}, client_id=info["client_id"]
        )
        rows = (users or {}).get("data") or []
        broadcaster_id = str(rows[0].get("id") or "") if rows else ""
        if not broadcaster_id:
            return _error("not_found", "Twitch broadcaster not found")

        # Official shape (dev.twitch.tv/docs/api/reference/#create-clip-from-vod):
        # all fields are URL query params. vod_offset = seconds from video
        # START to clip END (existing END-reference semantics); duration picks
        # the clip length 5..60s; title is required — blank -> broadcaster
        # login so a nameless clip still has a deterministic title.
        params: Dict[str, Any] = {
            "broadcaster_id": broadcaster_id,
            "editor_id": editor_id,
            "vod_id": str(req.vod_id),
            "vod_offset": int(req.offset_sec),
            "title": (req.title or "").strip() or login,
        }
        if req.duration_sec is not None:
            params["duration"] = int(req.duration_sec)
        title = params["title"]
        data = ths._helix_post(
            "/videos/clips", params=params, client_id=info["client_id"]
        )
    except Exception as exc:
        return _helix_failure(exc)
    return _record_clip(data, login, req, title)


def _create_live_clip(req: TwitchClipRequest, login: str) -> Dict[str, Any]:
    info = _token_and_client()
    if "error" in info:
        return info
    if LIVE_CLIP_SCOPE not in info["scopes"]:
        return _error(
            "missing_scope",
            "Twitch token lacks clips:edit (needed for live clips). The "
            "Cookie Bridge token is your twitch.tv browser login — Twitch "
            "does not grant clip scopes to it. Paste an OAuth token with "
            "clips:edit in Settings → Official APIs",
        )
    try:
        users = ths._helix_get("/users", {"login": login}, client_id=info["client_id"])
        rows = (users or {}).get("data") or []
        broadcaster_id = str(rows[0].get("id") or "") if rows else ""
        if not broadcaster_id:
            return _error("not_found", "Twitch broadcaster not found")
        # Official shape (POST /clips): broadcaster_id (+ optional title) are
        # URL query params, not a JSON body.
        params: Dict[str, Any] = {"broadcaster_id": broadcaster_id}
        if req.title:
            params["title"] = req.title
        data = ths._helix_post(
            "/clips", params=params, client_id=info["client_id"]
        )
    except Exception as exc:
        return _helix_failure(exc)
    return _record_clip(data, login, req)


@router.get("/api/twitch/clips/history")
async def twitch_clips_history(limit: int = 100) -> List[Dict[str, Any]]:
    return _load_history()[: max(1, min(limit, HISTORY_CAP))]


@router.delete("/api/twitch/clips/history")
def delete_twitch_clips_history(
    req: TwitchClipHistoryDeleteRequest,
) -> Dict[str, Any]:
    """Batch-remove clip history entries by id (history file format unchanged)."""
    ids = {str(i).strip() for i in (req.ids or []) if str(i).strip()}
    if not ids:
        raise HTTPException(status_code=422, detail="ids must be a non-empty list")
    if len(ids) > HISTORY_CAP:
        raise HTTPException(
            status_code=422, detail=f"too many ids (max {HISTORY_CAP})"
        )
    history = _load_history()
    kept = [e for e in history if str(e.get("id") or "").strip() not in ids]
    removed = len(history) - len(kept)
    if removed:
        _save_history(kept[:HISTORY_CAP])
    return {"ok": True, "removed": removed}


@router.post("/api/twitch/clip")
def create_twitch_clip(req: TwitchClipRequest) -> Dict[str, Any]:
    login = (req.broadcaster_login or "").strip()
    if not TWITCH_LOGIN_RE.fullmatch(login):
        raise HTTPException(
            status_code=422,
            detail="invalid twitch broadcaster_login",
        )

    # Whitespace-only title -> None -> Twitch auto-titles the clip.
    title = (req.title or "").strip()
    if len(title) > TWITCH_CLIP_TITLE_MAX:
        raise HTTPException(
            status_code=422,
            detail=f"title must be {TWITCH_CLIP_TITLE_MAX} chars or less",
        )
    req.title = title or None

    if req.vod_id is not None:
        if not req.vod_id.isdigit():
            raise HTTPException(status_code=422, detail="vod_id must be numeric")
        if req.offset_sec is None or req.offset_sec < 0:
            raise HTTPException(
                status_code=422, detail="offset_sec required for VOD clips"
            )
        if req.duration_sec is None or not (
            TWITCH_CLIP_MIN_SEC <= req.duration_sec <= TWITCH_CLIP_MAX_SEC
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"duration_sec must be "
                    f"{TWITCH_CLIP_MIN_SEC}..{TWITCH_CLIP_MAX_SEC}"
                ),
            )
        return _create_vod_clip(req, login)

    # Live stream: Helix clips the current broadcast's recent window.
    if req.offset_sec is not None or req.duration_sec is not None:
        raise HTTPException(
            status_code=422,
            detail="offset/duration only valid with vod_id",
        )
    return _create_live_clip(req, login)


# --- module self-check (error mapping — no I/O, no network) ---------------
def _fake_exc(status: Optional[int]) -> ths.HelixError:
    return ths.HelixError(status, '{"error":"x","status":%s,"message":"helix said no"}' % status)


_net = _helix_failure(RuntimeError("boom"))
assert _net["ok"] is False and _net["error"]["code"] == "network"
assert _helix_failure(_fake_exc(401))["error"]["code"] == "unauthorized"
assert _helix_failure(_fake_exc(403))["error"]["code"] == "missing_scope"
_403 = _helix_failure(_fake_exc(403))["error"]["message"]
assert "editor:manage:clips" in _403 and "Settings → Official APIs" in _403
assert _helix_failure(_fake_exc(404))["error"]["code"] == "not_found"
assert _helix_failure(_fake_exc(429))["error"]["code"] == "rate_limited"
assert _helix_failure(_fake_exc(422))["error"]["code"] == "clip_failed"
assert _helix_failure(_fake_exc(503))["error"]["code"] == "clip_failed"
assert _helix_failure(_fake_exc(500))["error"]["code"] == "clip_failed"
assert "helix said no" in _helix_failure(_fake_exc(404))["error"]["message"]
assert _error("no_token", "x") == {"ok": False, "error": {"code": "no_token", "message": "x"}}
