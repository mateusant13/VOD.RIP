"""
Twitch clip creation — browser path + local history.

Clips are created in Twitch's own clip editor (clips.twitch.tv/create),
opened by the FRONTEND in the OS default browser with vodrip_* params; the
VOD.RIP cookie extension's content script (clip_assist.mjs) fills the title
and clicks Save using Twitch's own session cookie — no API token, scopes or
editor role needed. The backend never opens browsers and never calls Twitch.

Flow:
- POST /api/twitch/clips/record — the extension posts the published clip URL
  after the editor flow so the clip lands in history with a download button
  (idempotent by clip slug).
- GET/DELETE /api/twitch/clips/history — read / batch-remove history rows
  (<data_dir>/twitch_clips.json).
- POST/GET /api/debug/clip-events — append-only event-sequence log so a clip
  attempt can be replayed end to end (src: 'app' | 'ext' | 'api').
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from services.disk_hygiene import data_dir

logger = logging.getLogger(__name__)
router = APIRouter(tags=["twitch-clips"])

TWITCH_LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")
HISTORY_CAP = 200
HISTORY_FILE = "twitch_clips.json"

# clips.twitch.tv/<slug> or twitch.tv/<channel>/clip/<slug> (both public
# clip URL shapes; Twitch's own share links use the former). Query strings
# and fragments (e.g. ?t=... from share buttons) are stripped before matching.
_CLIP_URL_RE = re.compile(
    r"^https?://(?:clips\.twitch\.tv/|(?:www\.|m\.)?twitch\.tv/[A-Za-z0-9_]+/clip/)([A-Za-z0-9_-]+)/?$"
)


class TwitchClipHistoryDeleteRequest(BaseModel):
    ids: List[str]


class TwitchClipRecordRequest(BaseModel):
    """A clip created by the BROWSER path (cookie-extension content script
    posts the published URL after the Twitch editor flow) so the clip lands
    in the app history with a download button. This endpoint exists because
    the browser flow publishes on Twitch's site, outside the backend's
    sight."""

    url: str
    title: Optional[str] = None
    channel: Optional[str] = None
    vod_id: Optional[str] = None
    offset_sec: Optional[int] = None
    duration_sec: Optional[int] = None


class ClipEventBody(BaseModel):
    """One step of the clip flow's debugging event sequence.

    src: 'app' (React UI), 'ext' (browser cookie extension content script) or
    'api' (this backend). Every step gets a server-side timestamp; the app and
    the extension POST their steps here so a clip attempt can be replayed end
    to end from <data_dir>/clip-events.log (see GET /api/debug/clip-events).
    """

    src: str = "app"
    event: str
    data: Dict[str, Any] = {}


CLIP_EVENTS_FILE = "clip-events.log"
CLIP_EVENTS_KEEP = 2000  # lines retained after the size cap kicks in
CLIP_EVENTS_MAX_BYTES = 1_000_000
# DOM/network spam from the page-trace extension stays on disk only.
_CLIP_ECHO_QUIET = frozenset(
    {
        "trace_dom",
        "trace_network_start",
        "trace_network_end",
        "trace_focus",
        "trace_gql_op",
        "gql_op",
    }
)


def _format_clip_event(src: str, event: str, data: Dict[str, Any]) -> str:
    """One-line terminal summary for a clip-flow event."""
    bits: list[str] = []
    for key, value in (data or {}).items():
        if key in ("census", "traceId", "tabId"):
            continue
        if value is None or value == "":
            continue
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            text = str(value)
        if len(text) > 220:
            text = text[:220] + "…"
        bits.append(f"{key}={text}")
    extra = " ".join(bits[:16])
    return f"CLIP {src} {event}" + (f" {extra}" if extra else "")


def _echo_clip_event(src: str, event: str, data: Dict[str, Any]) -> None:
    """Print clip-flow events on the API terminal so both extensions are visible."""
    if event in _CLIP_ECHO_QUIET:
        return
    line = _format_clip_event(src, event, data)
    try:
        print(line, flush=True)
    except Exception:
        pass
    logger.info("%s", line)


def _append_clip_event(src: str, event: str, data: Dict[str, Any]) -> None:
    """Append one JSON line to the clip event log. Best-effort: a full disk or
    log failure must never break the clip flow itself."""
    try:
        _echo_clip_event(src, event, data)
        path = data_dir() / CLIP_EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "src": src,
                "event": event,
                **data,
            },
            ensure_ascii=False,
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        # ponytail: rotation = truncate to the tail once the file passes the
        # cap; upgrade path: proper log rotation if this ever ships to users.
        if path.stat().st_size > CLIP_EVENTS_MAX_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > CLIP_EVENTS_KEEP:
                path.write_text(
                    "\n".join(lines[-CLIP_EVENTS_KEEP:]) + "\n", encoding="utf-8"
                )
    except Exception as exc:  # logging must never fail the request
        logger.warning("clip event log write failed: %s", exc)


def _history_path() -> Path:
    return data_dir() / HISTORY_FILE


# Legacy rows from the pre-browser era opened clips.twitch.tv/create
# unconditionally; Twitch's SPA client-side-redirects that URL to its own
# /clips/500 error page for non-logged-in sessions. Those rows are
# permanently dead — drop them at read.
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


# The extension's content script POSTs JSON from https://clips.twitch.tv to
# this localhost app — cross-origin, so the browser requires a CORS preflight
# before the POST is sent. The app binds localhost only: these are headers,
# not an auth change. (The service-worker relay makes the preflight moot in
# the shipped flow; these routes keep the direct content-script path working.)
_CLIP_ORIGIN = "https://clips.twitch.tv"


def published_clip_range_from_gql(
    gql_offset: Optional[float],
    gql_duration: Optional[float],
) -> Optional[tuple]:
    """Map Twitch GQL clip fields to (end_sec, duration_sec).

    Frame compares of live clips showed videoOffsetSeconds is the VOD END
    of the published media (15s: 886/15 -> start 871; 19s: 896/18 -> start
    878). History stores END because downloads crop [end-duration, end].
    An offset that cannot be an end (offset <= duration) is treated as start.
    """
    if gql_offset is None or gql_duration is None:
        return None
    try:
        offset = int(round(float(gql_offset)))
        duration = int(round(float(gql_duration)))
    except (TypeError, ValueError):
        return None
    if duration < 5 or duration > 60 or offset < 0:
        return None
    if offset > duration + 2:
        return offset, duration
    return offset + duration, duration


def _apply_twitch_published_range(entry: Dict[str, Any], clip_url: str) -> Dict[str, Any]:
    """Overwrite vod/offset/duration with Twitch's published range when GQL answers."""
    try:
        from services.twitch_gql_service import get_clip_info_sync
        info = get_clip_info_sync(clip_url)
    except Exception as exc:
        logger.debug("clip record GQL enrich skipped: %s", exc)
        return entry
    vod_id = info.get("vod_id")
    if vod_id:
        entry["vod_id"] = str(vod_id)
    mapped = published_clip_range_from_gql(info.get("offset_sec"), info.get("duration"))
    if mapped:
        entry["offset_sec"], entry["duration_sec"] = mapped
    return entry


def _clip_cors_preflight() -> Response:
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": _CLIP_ORIGIN,
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
        },
    )


def _set_clip_cors(response: Response) -> None:
    """Allow the clips.twitch.tv content script to READ the POST response
    (without ACAO the request lands but the caller's fetch rejects)."""
    response.headers["Access-Control-Allow-Origin"] = _CLIP_ORIGIN


@router.options("/api/debug/clip-events")
def clip_events_options() -> Response:
    """CORS preflight for the clip-assist content script (clips.twitch.tv)."""
    return _clip_cors_preflight()


@router.options("/api/twitch/clips/record")
def clip_record_options() -> Response:
    """CORS preflight for the clip-assist content script (clips.twitch.tv)."""
    return _clip_cors_preflight()


@router.post("/api/debug/clip-events")
def post_clip_event(body: ClipEventBody, response: Response) -> Dict[str, Any]:
    """Event-sequence sink for the clip flow (app UI + browser extension POST
    their steps here; timestamps are added server-side). Localhost-only app,
    append-only log — validation is a sanity guard, not an auth boundary."""
    _set_clip_cors(response)
    if body.src not in ("app", "ext", "api"):
        print(f"CLIP drop {body.src} {body.event}: invalid src", flush=True)
        raise HTTPException(status_code=422, detail="invalid src")
    if not body.event or len(body.event) > 120:
        print(f"CLIP drop {body.src} {body.event!r}: invalid event", flush=True)
        raise HTTPException(status_code=422, detail="invalid event")
    if not isinstance(body.data, dict) or len(json.dumps(body.data)) > 8000:
        print(f"CLIP drop {body.src} {body.event}: data too large", flush=True)
        raise HTTPException(status_code=422, detail="data too large")
    _append_clip_event(body.src, body.event, body.data)
    return {"ok": True}


@router.get("/api/debug/clip-events")
def get_clip_events(limit: int = 200) -> List[Dict[str, Any]]:
    """Read back the last N clip events (debugging helper)."""
    try:
        lines = (data_dir() / CLIP_EVENTS_FILE).read_text("utf-8").splitlines()
    except FileNotFoundError:
        return []
    out: List[Dict[str, Any]] = []
    for line in lines[-max(1, min(limit, CLIP_EVENTS_KEEP)):]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue  # a corrupt line must not hide the rest
    return out


@router.post("/api/twitch/clips/record")
def record_twitch_clip(req: TwitchClipRecordRequest, response: Response) -> Dict[str, Any]:
    """Record a browser-path clip into history (idempotent by clip slug)."""
    _set_clip_cors(response)
    raw = (req.url or "").strip()
    # Strip ?query/#fragment — the extension may post the tab's full URL.
    for sep in ("?", "#"):
        raw = raw.split(sep, 1)[0]
    m = _CLIP_URL_RE.match(raw)
    if not m:
        raise HTTPException(status_code=422, detail="invalid twitch clip url")
    slug = m.group(1)
    login = (req.channel or "").strip()
    if login and not TWITCH_LOGIN_RE.fullmatch(login):
        raise HTTPException(status_code=422, detail="invalid channel")
    if req.offset_sec is not None and (not isinstance(req.offset_sec, int) or req.offset_sec < 0):
        raise HTTPException(status_code=422, detail="offset_sec must be a non-negative int")
    if req.duration_sec is not None and (
        not isinstance(req.duration_sec, int) or not (5 <= req.duration_sec <= 60)
    ):
        raise HTTPException(status_code=422, detail="duration_sec out of range (5..60)")

    entry = {
        "id": slug,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "channel": login or "unknown",
        "vod_id": req.vod_id,
        "offset_sec": req.offset_sec,
        "duration_sec": req.duration_sec,
        "title": (req.title or "").strip() or None,
        "url": f"https://clips.twitch.tv/{slug}",
        "status": "created",
    }
    entry = _apply_twitch_published_range(entry, entry["url"])
    history = _load_history()
    # Idempotent: the extension may re-post on retry or when both flows
    # publish — never duplicate the row for the same clip.
    history = [e for e in history if e.get("id") != slug]
    history.insert(0, entry)
    _save_history(history[:HISTORY_CAP])
    _append_clip_event("api", "api_clip_recorded", {**entry})
    return {"ok": True, "id": slug, "url": entry["url"]}
