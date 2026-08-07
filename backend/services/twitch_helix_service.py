"""Twitch Helix (official API) metadata layer — optional OAuth token (issue #4).

Slots in FRONT of the public GQL path: when ``settings.twitch_helix_token``
is set (auto-lifted from the cookie bridge's ``auth-token``, or pasted by a
non-extension user), VOD metadata + channel listings go through Helix first
and fall back to GQL on ANY failure (bad token, rate limit, missing VOD) —
silent, never surfaced to the caller.

Latency contract: the token is NEVER validated up front. The interactive
path (preview/info click) uses the token directly on the first call — one
Helix round trip, same latency as GQL; a broken token costs one extra
failed call before the GQL fallback. Boot/settings-save only persist the
cookie value (auto-lift), they never ping the API.

The Client-Id is the same anonymous twitch.tv web client id the GQL service
uses: the browser ``auth-token`` cookie IS a user access token issued to
that client, so ``Bearer <auth-token>`` + this Client-Id authenticates Helix
requests. ponytail: this pairing is community-verified, not a documented
flow — any Helix rejection simply lands on the GQL fallback, and the
upgrade path is a registered Twitch app + its own OAuth flow.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

HELIX_API = "https://api.twitch.tv/helix"
HELIX_TIMEOUT_S = 12.0

# Helix duration format: "4h21m33s", "1h2m", "33s".
_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
# Helix thumbnails carry %{width}x%{height} placeholders.
_THUMB_PLACEHOLDER_RE = re.compile(r"%\{width\}x%\{height\}")


class HelixError(RuntimeError):
    """Helix HTTP failure with the status code preserved for precise mapping.

    A RuntimeError subclass so existing GQL-fallback callers keep working.
    """

    def __init__(self, status: Optional[int], message: str):
        super().__init__(
            f"Twitch Helix HTTP {status}: {message}" if status is not None else message
        )
        self.status = status
        self.message = message


def current_token() -> str:
    """Stored helix bearer token ('' when unset). Never validates."""
    try:
        from deps import settings_mgr

        return (getattr(settings_mgr.get(), "twitch_helix_token", "") or "").strip()
    except Exception:
        return ""


def token_available() -> bool:
    return bool(current_token())


def _helix_get(
    path: str, params: Dict[str, Any], client_id: Optional[str] = None
) -> Dict[str, Any]:
    """One authenticated Helix GET; raises HelixError/RuntimeError on failure.

    The caller (twitch_gql_service) catches and falls back to GQL, so this
    never surfaces to users — keep the error message diagnostic-only.
    ``client_id`` defaults to the GQL client; pass the token's own client
    (from token_info) when the endpoint must see a matching Client-Id.
    """
    token = current_token()
    if not token:
        raise RuntimeError("no twitch helix token configured")
    from services.twitch_gql_service import TWITCH_GQL_CLIENT_ID  # lazy: no import cycle

    url = f"{HELIX_API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Client-Id": client_id or TWITCH_GQL_CLIENT_ID,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HELIX_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        raise HelixError(e.code, detail) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Twitch Helix request failed: {e}") from e


def _post_url(path: str, params: Optional[Dict[str, Any]]) -> str:
    """HELIX_API + path, with params urlencoded as the query string ('' when none)."""
    url = f"{HELIX_API}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    return url


def _helix_post(
    path: str,
    body: Optional[Dict[str, Any]] = None,
    client_id: str = "",
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One authenticated Helix POST; raises HelixError/RuntimeError.

    Clip endpoints (POST /clips, POST /videos/clips) take their fields as
    URL query parameters — pass them via ``params`` and leave ``body`` None.
    ``body`` remains for any endpoint that wants a JSON payload.
    """
    token = current_token()
    if not token:
        raise RuntimeError("no twitch helix token configured")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Client-Id": client_id,
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        _post_url(path, params),
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HELIX_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        raise HelixError(e.code, detail) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Twitch Helix request failed: {e}") from e


def token_info() -> Dict[str, Any]:
    """Validate the stored token via id.twitch.tv/oauth2/validate.

    Returns the validate payload ({client_id, login, scopes, ...}). Raises
    HelixError(401) for an invalid/expired token and RuntimeError on network
    failure. The clip router uses the client_id for the Client-Id header
    (Helix requires it to match the token's app) and pre-checks scopes.
    """
    token = current_token()
    if not token:
        raise RuntimeError("no twitch helix token configured")
    req = urllib.request.Request(
        "https://id.twitch.tv/oauth2/validate",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HELIX_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        raise HelixError(e.code, detail) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Twitch OAuth validate failed: {e}") from e


def _duration_to_sec(value: Any) -> Optional[int]:
    """'4h21m33s' / '1h2m' / '33s' -> seconds (None when unparseable)."""
    if not value:
        return None
    m = _DURATION_RE.match(str(value).strip().lower())
    if not m or not any(m.groups()):
        return None
    h, mm, s = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mm * 60 + s


def _thumbnail_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return _THUMB_PLACEHOLDER_RE.sub("320x180", value)


def _duration_string(seconds: Optional[int]) -> Optional[str]:
    if seconds is None:
        return None
    from services.twitch_gql_service import _format_duration  # lazy: no import cycle

    return _format_duration(seconds)


def list_channel_videos_sync(login: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Latest *limit* VODs for a channel login via Helix, in the GQL row
    shape. Raises on any failure (caller falls back to GQL)."""
    login = (login or "").strip().lower()
    if not login:
        return []
    limit = max(1, min(int(limit), 100))

    data = _helix_get("/users", {"login": login})
    users = (data or {}).get("data") or []
    if not users:
        raise RuntimeError(f"twitch user not found via helix: {login}")
    user_id = str(users[0].get("id") or "")

    data = _helix_get("/videos", {"user_id": user_id, "first": limit, "sort": "time"})
    rows: List[Dict[str, Any]] = []
    for v in (data or {}).get("data") or []:
        vid = str(v.get("id") or "").strip()
        if not vid:
            continue
        duration = _duration_to_sec(v.get("duration"))
        rows.append({
            "id": vid,
            "platform": "Twitch",
            "title": v.get("title") or "Untitled",
            "duration": duration,
            "duration_string": _duration_string(duration),
            "created_at": v.get("created_at") or None,
            "views": v.get("view_count"),
            "thumbnail_url": _thumbnail_url(v.get("thumbnail_url")),
            "url": v.get("url") or f"https://www.twitch.tv/videos/{vid}",
            "content_kind": "vod",
            # Helix language = the broadcast language ('en', 'pt', ...).
            "language": v.get("language") or None,
        })
    return rows


def video_metadata_sync(vid: str) -> Dict[str, Any]:
    """Metadata for one VOD via Helix, in the GQL get_video_info payload
    shape (without the playback/size enrichment — the caller adds that).
    Raises on any failure (caller falls back to GQL)."""
    data = _helix_get("/videos", {"id": str(vid)})
    vids = (data or {}).get("data") or []
    if not vids:
        raise RuntimeError(f"Twitch video not found via helix: {vid}")
    v = vids[0]
    duration = _duration_to_sec(v.get("duration"))
    return {
        "id": str(v.get("id") or vid),
        "title": v.get("title") or "Untitled",
        "uploader": v.get("user_name") or v.get("user_login"),
        "channel": v.get("user_login"),
        "duration": duration,
        "duration_string": _duration_string(duration),
        "thumbnail": _thumbnail_url(v.get("thumbnail_url")),
        "views": v.get("view_count"),
        "category": v.get("game_name") or v.get("game_id"),
        "webpage_url": v.get("url") or f"https://www.twitch.tv/videos/{vid}",
        "qualities": [],
        "platform": "Twitch",
        "created_at": v.get("created_at"),
    }


def auto_lift_token() -> bool:
    """Persist the cookie-bridge Twitch ``auth-token`` into settings.

    Zero-manual-steps path for extension users. Rules (issue #4):
    - field empty -> fill from the cookie;
    - field set -> replace ONLY when the cookie export is NEWER than the
      stored token (a stale browser cookie never clobbers a manual paste).
    Never raises; returns True when settings were updated.
    """
    try:
        from services.cookie_bridge import cookie_dict, resolve_cookiefile

        path = resolve_cookiefile("twitch")
        token = ((cookie_dict("twitch") or {}).get("auth-token") or "").strip()
        if not token:
            return False
        mtime = 0.0
        if path:
            try:
                mtime = Path(path).stat().st_mtime
            except OSError:
                mtime = 0.0

        from deps import settings_mgr

        current = settings_mgr.get()
        existing = (getattr(current, "twitch_helix_token", "") or "").strip()
        updated_at = float(getattr(current, "twitch_helix_token_updated_at", 0.0) or 0.0)
        if existing == token:
            return False
        if existing and mtime <= updated_at:
            return False
        settings_mgr.save(current.model_copy(update={
            "twitch_helix_token": token,
            "twitch_helix_token_updated_at": time.time(),
        }))
        return True
    except Exception as exc:
        logger.debug("helix token auto-lift skipped: %s", exc)
        return False


# --- module self-check (pure parsing — no I/O, no network) -----------------
assert _duration_to_sec("4h21m33s") == 4 * 3600 + 21 * 60 + 33
assert _duration_to_sec("1h2m") == 3720
assert _duration_to_sec("33s") == 33
assert _duration_to_sec("") is None
assert _duration_to_sec("PT1H") is None  # ISO-8601 is not the helix wire format
assert _duration_to_sec(None) is None
assert _thumbnail_url("https://x/thumb0-%{width}x%{height}.jpg") == (
    "https://x/thumb0-320x180.jpg"
)
assert _post_url("/videos/clips", None) == f"{HELIX_API}/videos/clips"
assert _post_url("/videos/clips", {"broadcaster_id": "1", "title": "a b"}) == (
    f"{HELIX_API}/videos/clips?broadcaster_id=1&title=a+b"
)
assert _thumbnail_url(None) is None
