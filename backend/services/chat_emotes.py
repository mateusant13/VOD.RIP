"""Chat emotes (BetterTTV + FrankerFaceZ + 7TV + official Twitch globals) — render-only, Twitch only.

Merges channel + global emote sets for a Twitch login into a flat
name -> {provider, url} map in Chatterino priority order:

    FFZ channel > BTTV channel > 7TV channel > FFZ global > BTTV global > 7TV global > Twitch global

(first provider holding a name wins). The chat panel replaces exact
case-sensitive whole-word tokens with inline <img>; stored message text and
search indexes are NEVER touched — the text column stays verbatim and the
emotes column is never selected by search.

Official Twitch globals ride along at the BOTTOM of the ladder: the global
set (`emoteSet(id: 0)` — GLOBALS + SMILIES) resolves on the GQL endpoint
with the same anonymous Client-Id the app already uses for VOD playback
(services.twitch_gql_service.TWITCH_GQL_CLIENT_ID, no OAuth), and emote ids
map to stable static-cdn.jtvnw.net v2 URLs. Any custom emote — channel or
global — with the same name shadows the official one (customs win).

Kick/YouTube have no BTTV/FFZ/7TV presence -> they return [].

Twitch id resolution: BTTV's name-based lookup (`/cached/users/twitch?name=`)
was REMOVED upstream (the route 404s with "Route not found", and the by-id
route returns BTTV's own internal user id, not the Twitch id), so the login
is resolved through ivr.fi, a public no-auth Twitch mirror. BTTV channel
emotes then come from its by-id route.

Resilience: every provider call is optional. Any failure degrades to the
remaining providers; a total failure returns [] (never raises), so chat
rendering never breaks because emotes fail. Results are cached in memory
(globals 1h, per-channel 15min) behind a lock.

ponytail: Chatterino persists a 14-day disk cache; the upgrade path is a
disk JSON cache (keyed by provider+login, with an etag/max-age refresh)
backed by this same merge logic. The id resolver's upgrade path is a Twitch
Helix users lookup (needs OAuth client creds this app does not hold) or BTTV
restoring its name route.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import requests

from services.twitch_gql_service import TWITCH_GQL_CLIENT_ID

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 12.0
_CHANNEL_TTL_SEC = 15 * 60
_GLOBAL_TTL_SEC = 60 * 60

_IVR_USER_URL = "https://api.ivr.fi/v2/twitch/user?login={login}"
_BTTV_USER_BY_ID_URL = "https://api.betterttv.net/3/cached/users/twitch/{twitch_id}"
_BTTV_GLOBAL_URL = "https://api.betterttv.net/3/cached/emotes/global"
_FFZ_ROOM_URL = "https://api.frankerfacez.com/v1/room/id/{twitch_id}"
_FFZ_GLOBAL_URL = "https://api.frankerfacez.com/v1/set/global"
_SEVENTV_USER_URL = "https://7tv.io/v3/users/twitch/{twitch_id}"
_SEVENTV_GLOBAL_URL = "https://7tv.io/v3/emote-sets/global"
# Official Twitch global emotes: the GQL `emoteSet(id: 0)` set (GLOBALS +
# SMILIES), same endpoint + anonymous Client-Id as the VOD playback queries.
_TWITCH_GQL_URL = "https://gql.twitch.tv/gql"
_TWITCH_GLOBAL_SET_QUERY = """query EmoteSet($id: ID!) {
  emoteSet(id: $id) {
    id
    emotes { id token }
  }
}"""
_TWITCH_EMOTE_URL_TEMPLATE = "https://static-cdn.jtvnw.net/emoticons/v2/{emote_id}/default/dark/1.0"

_SESSION = requests.Session()

# Bounded pool so a slow provider can't serialize the other fetches; the
# channel fetch (2 parallel + 1 inline) and the global fetch (4 parallel)
# each time out at _TIMEOUT_SEC.
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="chat-emotes")

_CACHE: dict[str, tuple[float, list[dict]]] = {}
_GLOBAL_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_LOCK = threading.Lock()


def fetch_emotes(platform: str, slug: str) -> list[dict]:
    """All custom emotes for a channel, merged in Chatterino priority order.

    Returns a list of {"name", "provider" ("bttv"|"ffz"|"7tv"), "url",
    "global"} with name collisions resolved to the highest-priority provider.
    Non-Twitch platforms and total network failure return [] (never raises).
    """
    plat = (platform or "").strip().lower()
    if plat != "twitch" or not slug:
        return []
    login = slug.strip()
    key = f"twitch:{login.lower()}"
    now = time.monotonic()
    with _CACHE_LOCK:
        ch = _CACHE.get(key)
        gl = _GLOBAL_CACHE.get("global")
    if ch is None or now - ch[0] >= _CHANNEL_TTL_SEC:
        channel = _fetch_channel(login)
        with _CACHE_LOCK:
            _CACHE[key] = (time.monotonic(), channel)
    else:
        channel = ch[1]
    if gl is None or now - gl[0] >= _GLOBAL_TTL_SEC:
        globals_merged = _fetch_globals()
        with _CACHE_LOCK:
            _GLOBAL_CACHE["global"] = (time.monotonic(), globals_merged)
    else:
        globals_merged = gl[1]
    return _merge([channel, globals_merged])


# ---------------------------------------------------------------------------
# Channel emotes (per login)
# ---------------------------------------------------------------------------

def _fetch_channel(login: str) -> list[dict]:
    """Channel emotes only. Resolve the Twitch id, then fetch BTTV/FFZ/7TV
    channel emotes in parallel. A failed resolution degrades to global-only
    (fetch_emotes merges globals afterwards) — never an error."""
    twitch_id = _resolve_twitch_id(login)
    if not twitch_id:
        # Degradation: the id resolver failed or the login is unknown, so no
        # provider's channel emotes can be fetched. fetch_emotes still merges
        # the global sets, so the result is partial (global-only) — the chat
        # panel renders raw text for missing names, which is the same state
        # as any channel without custom emotes.
        logger.debug("chat_emotes: no twitch id for %r — channel emotes skipped", login)
        return []
    fut_ffz = _POOL.submit(_fetch_ffz_channel, twitch_id)
    fut_7tv = _POOL.submit(_fetch_seventv_channel, twitch_id)
    # BTTV channel emotes come from the same by-id route the id feeds the
    # other providers (channelEmotes + sharedEmotes).
    bttv = _bttv_emotes_from_user(_get_json(_BTTV_USER_BY_ID_URL.format(twitch_id=twitch_id)))
    return _merge([fut_ffz.result(), bttv, fut_7tv.result()])


def _resolve_twitch_id(login: str) -> Optional[str]:
    """Twitch user id for a login.

    BTTV's name-based lookup was removed upstream (the route 404s with
    "Route not found" and the by-id route exposes only BTTV's internal user
    id), so the id comes from ivr.fi — a public, no-auth Twitch mirror. Its
    response is a list whose first element carries the real twitch id.
    ponytail: upgrade path is a Twitch Helix users lookup, which needs OAuth
    client credentials this app does not hold.
    """
    data = _get_json(_IVR_USER_URL.format(login=login))
    return _twitch_id_from_ivr(data)


def _twitch_id_from_ivr(payload: Optional[Any]) -> Optional[str]:
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return None
    twitch_id = payload[0].get("id")
    return str(twitch_id) if twitch_id else None


def _bttv_emotes_from_user(user: Optional[dict]) -> list[dict]:
    """channelEmotes + sharedEmotes from the BTTV cached-users response."""
    if not isinstance(user, dict):
        return []
    return _parse_bttv_emotes(user.get("channelEmotes"), global_flag=False) + \
        _parse_bttv_emotes(user.get("sharedEmotes"), global_flag=False)


def _fetch_ffz_channel(twitch_id: str) -> list[dict]:
    data = _get_json(_FFZ_ROOM_URL.format(twitch_id=twitch_id))
    return _parse_ffz_room(data)


def _fetch_seventv_channel(twitch_id: str) -> list[dict]:
    data = _get_json(_SEVENTV_USER_URL.format(twitch_id=twitch_id))
    return _parse_seventv_emotes(_seventv_channel_emotes(data), global_flag=False)


def _seventv_channel_emotes(data: Optional[dict]) -> list:
    """The v3 user response nests the channel's emotes under `emote_set`
    (top-level `emotes` is only present in emote-set responses like the
    global set). Fall back to top-level for API drift."""
    if not isinstance(data, dict):
        return []
    emote_set = data.get("emote_set")
    if isinstance(emote_set, dict) and emote_set.get("emotes"):
        return emote_set["emotes"]
    return data.get("emotes") or []


# ---------------------------------------------------------------------------
# Global emote sets (fetched in parallel, cached 1h)
# ---------------------------------------------------------------------------

def _fetch_globals() -> list[dict]:
    futs = {
        "bttv": _POOL.submit(_get_json, _BTTV_GLOBAL_URL),
        "ffz": _POOL.submit(_get_json, _FFZ_GLOBAL_URL),
        "7tv": _POOL.submit(_get_json, _SEVENTV_GLOBAL_URL),
        "twitch": _POOL.submit(_fetch_twitch_globals),
    }
    buckets = [
        _parse_ffz_global(futs["ffz"].result()),
        _parse_bttv_emotes(futs["bttv"].result(), global_flag=True),
        _parse_seventv_emotes((futs["7tv"].result() or {}).get("emotes"), global_flag=True),
        futs["twitch"].result(),
    ]
    return _merge(buckets)


def _fetch_twitch_globals() -> list[dict]:
    """Official Twitch global emotes via GQL — no OAuth.

    `emoteSet(id: 0)` is the global set (GLOBALS + SMILIES). It resolves on
    the same GQL endpoint with the same anonymous Client-Id the app already
    uses for VOD playback; emote ids feed the stable static-cdn v2 URL
    template, and the 1h global cache absorbs any upstream deletion. Any
    failure returns [] (degrades like the other providers — never raises).
    """
    try:
        resp = _SESSION.post(
            _TWITCH_GQL_URL,
            json=[{"query": _TWITCH_GLOBAL_SET_QUERY, "variables": {"id": "0"}}],
            headers={"Client-Id": TWITCH_GQL_CLIENT_ID, "Content-Type": "application/json"},
            timeout=_TIMEOUT_SEC,
        )
        if resp.status_code != 200:
            logger.debug("chat_emotes: twitch global emotes HTTP %d", resp.status_code)
            return []
        return _parse_twitch_emotes(resp.json())
    except (requests.RequestException, ValueError, OSError):
        logger.debug("chat_emotes: twitch global emotes fetch failed", exc_info=True)
        return []


def _parse_twitch_emotes(data: Optional[Any]) -> list[dict]:
    """GQL `emoteSet(id: 0)` payload -> [{name, provider: "twitch", url, global}].

    The batch response is a list with one operation result; the set nests
    under ``data.emoteSet.emotes`` as {id, token} pairs. Duplicate tokens
    (Twitch ships a few) collapse in the merge below."""
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return []
    emote_set = (data[0].get("data") or {}).get("emoteSet") or {}
    if not isinstance(emote_set, dict):
        return []
    out: list[dict] = []
    for emote in emote_set.get("emotes") or []:
        if not isinstance(emote, dict):
            continue
        token = emote.get("token")
        eid = emote.get("id")
        if not token or not eid:
            continue
        out.append({
            "name": str(token),
            "provider": "twitch",
            "url": _TWITCH_EMOTE_URL_TEMPLATE.format(emote_id=eid),
            "global": True,
        })
    return out


# ---------------------------------------------------------------------------
# Pure parsing / merging (self-checked below, no network)
# ---------------------------------------------------------------------------

def _get_json(url: str) -> Optional[Any]:
    """GET + JSON decode; None on any failure (HTTP != 200, timeout, bad body)."""
    try:
        resp = _SESSION.get(url, timeout=_TIMEOUT_SEC)
        if resp.status_code != 200:
            logger.debug("chat_emotes: HTTP %d for %s", resp.status_code, url)
            return None
        return resp.json()
    except (requests.RequestException, ValueError, OSError):
        logger.debug("chat_emotes: fetch failed for %s", url, exc_info=True)
        return None


def _merge(buckets: list[list[dict]]) -> list[dict]:
    """Dedupe by name keeping the first (highest-priority) provider. Bucket
    order encodes priority; keys stay case-sensitive (no case folding)."""
    by_name: dict[str, dict] = {}
    for bucket in buckets:
        for emote in bucket:
            by_name.setdefault(emote["name"], emote)
    return list(by_name.values())


def _parse_bttv_emotes(items: Optional[list], *, global_flag: bool) -> list[dict]:
    out: list[dict] = []
    for item in items or []:
        code = item.get("code")
        eid = item.get("id")
        if not code or not eid:
            continue
        out.append({
            "name": str(code),
            "provider": "bttv",
            "url": f"https://cdn.betterttv.net/emote/{eid}/2x.webp",
            "global": global_flag,
        })
    return out


def _ffz_emote_url(emoticon: dict) -> Optional[str]:
    """FFZ static urls are keyed 1/2/4; animated emotes carry a parallel
    `animated` dict. Prefer the 2x variant of animated, then static, then 1x."""
    animated = emoticon.get("animated") or {}
    urls = emoticon.get("urls") or {}
    for candidate in (animated.get("2"), urls.get("2"), animated.get("1"), urls.get("1")):
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _parse_ffz_set(ffz_set: Optional[dict], *, global_flag: bool) -> list[dict]:
    out: list[dict] = []
    for emote in (ffz_set or {}).get("emoticons") or []:
        name = emote.get("name")
        url = _ffz_emote_url(emote)
        if not name or not url:
            continue
        out.append({"name": str(name), "provider": "ffz", "url": url, "global": global_flag})
    return out


def _parse_ffz_room(data: Optional[dict]) -> list[dict]:
    """FFZ room response: the primary set is `room.set`; fall back to every
    set when the room lacks the marker (some rooms return sets only)."""
    if not isinstance(data, dict):
        return []
    room = data.get("room") or {}
    primary = str(room.get("set") or "")
    sets = data.get("sets") or {}
    if primary and primary in sets:
        return _parse_ffz_set(sets[primary], global_flag=False)
    out: list[dict] = []
    for ffz_set in sets.values():
        out.extend(_parse_ffz_set(ffz_set, global_flag=False))
    return out


def _parse_ffz_global(data: Optional[dict]) -> list[dict]:
    """Keep only the sets listed in `default_sets` (the channel-wide set is
    not part of the global collection even though it ships in the payload)."""
    if not isinstance(data, dict):
        return []
    default_sets = data.get("default_sets") or []
    sets = data.get("sets") or {}
    out: list[dict] = []
    for sid in default_sets:
        out.extend(_parse_ffz_set(sets.get(str(sid)) or {}, global_flag=True))
    return out


def _seventv_emote_url(data: Optional[dict]) -> Optional[str]:
    """7TV host.url is protocol-relative (//cdn.7tv.app/emote/{id}) with a
    files list; prefer the 4x WEBP, else 3x, else any WEBP."""
    host = (data or {}).get("host") or {}
    base = host.get("url") or ""
    if base.startswith("//"):
        base = "https:" + base
    elif base.startswith("/"):
        base = "https://cdn.7tv.app" + base
    if not base:
        return None
    webp = [
        f for f in host.get("files") or []
        if isinstance(f, dict) and (f.get("format") or "").upper() == "WEBP"
    ]
    pick = None
    for name in ("4x.webp", "3x.webp"):
        pick = next((f for f in webp if f.get("name") == name), None)
        if pick:
            break
    if pick is None and webp:
        pick = webp[0]
    if not pick or not pick.get("name"):
        return None
    return f"{base}/{pick['name']}"


def _parse_seventv_emotes(items: Optional[list], *, global_flag: bool) -> list[dict]:
    out: list[dict] = []
    for item in items or []:
        name = item.get("name")
        url = _seventv_emote_url(item.get("data"))
        if not name or not url:
            continue
        out.append({"name": str(name), "provider": "7tv", "url": url, "global": global_flag})
    return out


# ---------------------------------------------------------------------------
# Module self-check: pure parse/merge functions with inline fixtures, no
# network (same pattern as services/chat_sinks/kick_pusher.py).
# ---------------------------------------------------------------------------

_BTTV_FIXTURE = [
    {"id": "60ae5e8a4c9d8a3a5a5f4a5f", "code": "KEKW", "imageType": "png"},
    {"id": "5ea9e9d61e6a2c2f0f2b3a2e", "code": "PogChamp", "imageType": "png"},
]
_bttv_global = _parse_bttv_emotes(_BTTV_FIXTURE, global_flag=True)
assert len(_bttv_global) == 2
assert _bttv_global[0] == {
    "name": "KEKW",
    "provider": "bttv",
    "url": "https://cdn.betterttv.net/emote/60ae5e8a4c9d8a3a5a5f4a5f/2x.webp",
    "global": True,
}
assert _parse_bttv_emotes(None, global_flag=True) == []
assert _parse_bttv_emotes([{"id": "x", "code": ""}], global_flag=True) == []

# BTTV user lookup carries channel + shared emotes and the twitch id.
_user_fix = {
    "id": "123456",
    "username": "cellbit",
    "channelEmotes": [{"id": "abc", "code": "OMEGALUL"}],
    "sharedEmotes": [{"id": "def", "code": "SHARED"}],
}
_ch = _bttv_emotes_from_user(_user_fix)
assert [e["name"] for e in _ch] == ["OMEGALUL", "SHARED"]
assert all(e["global"] is False for e in _ch)
assert _bttv_emotes_from_user({"id": "1", "channelEmotes": [], "sharedEmotes": []}) == []
assert _bttv_emotes_from_user(None) == []

# Twitch id resolution: ivr.fi returns a list whose first item carries the id.
assert _twitch_id_from_ivr([{"id": "28579002", "login": "cellbit"}]) == "28579002"
assert _twitch_id_from_ivr([]) is None
assert _twitch_id_from_ivr(None) is None
assert _twitch_id_from_ivr([{}]) is None

# FFZ: animated 2x beats static 2x beats 1x.
assert _ffz_emote_url({"urls": {"1": "u1"}, "animated": {"2": "a2"}}) == "a2"
assert _ffz_emote_url({"urls": {"1": "u1", "2": "u2"}}) == "u2"
assert _ffz_emote_url({"urls": {"1": "u1"}}) == "u1"
assert _ffz_emote_url({}) is None
assert _parse_ffz_set(None, global_flag=False) == []

# FFZ room: primary set via room.set.
_room_fix = {
    "room": {"set": 12345},
    "sets": {
        "12345": {"id": 12345, "emoticons": [{"name": "KKona", "urls": {"2": "https://cdn.frankerfacez.com/emote/1/2"}}]}
    },
}
assert _parse_ffz_room(_room_fix) == [
    {"name": "KKona", "provider": "ffz", "url": "https://cdn.frankerfacez.com/emote/1/2", "global": False}
]
assert _parse_ffz_room(None) == []

# FFZ global keeps only default_sets.
_ffz_global_fix = _parse_ffz_global({
    "default_sets": [2],
    "sets": {
        "1": {"id": 1, "emoticons": [{"name": "skip", "urls": {"1": "x"}}]},
        "2": {"id": 2, "emoticons": [{"name": "keep", "urls": {"1": "y"}}]},
    },
})
assert [e["name"] for e in _ffz_global_fix] == ["keep"]
assert _ffz_global_fix[0]["global"] is True
assert _parse_ffz_global(None) == []

# 7TV: 4x.webp preferred, 3x fallback, any WEBP as last resort; both
# protocol-relative and bare /emote hosts normalize to https.
_7tv_fix = {
    "host": {
        "url": "//cdn.7tv.app/emote/01F1P9T5S8",
        "files": [
            {"name": "1x.webp", "format": "WEBP"},
            {"name": "4x.webp", "format": "WEBP"},
            {"name": "3x.webp", "format": "WEBP"},
        ],
    }
}
assert _seventv_emote_url(_7tv_fix) == "https://cdn.7tv.app/emote/01F1P9T5S8/4x.webp"
assert _seventv_emote_url({"host": {"url": "/emote/abc", "files": [{"name": "3x.webp", "format": "WEBP"}]}}) == \
    "https://cdn.7tv.app/emote/abc/3x.webp"
assert _seventv_emote_url({"host": {"url": "//cdn.7tv.app/emote/x", "files": [{"name": "2x.webp", "format": "WEBP"}]}}) == \
    "https://cdn.7tv.app/emote/x/2x.webp"
assert _seventv_emote_url({"host": {"url": "//cdn.7tv.app/emote/x", "files": [{"name": "1x.gif", "format": "GIF"}]}}) is None
assert _seventv_emote_url({}) is None
assert _parse_seventv_emotes(None, global_flag=False) == []
# Channel emotes nest under emote_set; top-level is the emote-set shape.
_seventv_user_fix = {"emote_set": {"emotes": [{"name": "GAMBA", "data": _7tv_fix}]}}
assert [e["name"] for e in _parse_seventv_emotes(_seventv_channel_emotes(_seventv_user_fix), global_flag=False)] == ["GAMBA"]
assert _seventv_channel_emotes({"emotes": [{"name": "x"}]})[0]["name"] == "x"
assert _seventv_channel_emotes(None) == []

# Official Twitch globals: emoteSet(id: 0) batch payload -> static-cdn v2 urls.
_twitch_gql_fix = [
    {"data": {"emoteSet": {"id": "0", "emotes": [
        {"id": "425618", "token": "LUL"},
        {"id": "25", "token": "Kappa"},
        {"id": "", "token": "skip-no-id"},
        {"id": "x", "token": ""},
        {"id": "425618", "token": "LUL"},  # duplicate token collapses later
    ]}}}
]
_twitch_global = _parse_twitch_emotes(_twitch_gql_fix)
assert _twitch_global[0] == {
    "name": "LUL",
    "provider": "twitch",
    "url": "https://static-cdn.jtvnw.net/emoticons/v2/425618/default/dark/1.0",
    "global": True,
}
assert len(_twitch_global) == 3
assert _parse_twitch_emotes(None) == []
assert _parse_twitch_emotes([{}]) == []
assert _parse_twitch_emotes([{"data": {"emoteSet": {"emotes": []}}}]) == []
assert _parse_twitch_emotes({"data": {"emoteSet": {"emotes": [{"id": "1", "token": "a"}]}}}) == []

# Chatterino priority ladder: FFZ ch > BTTV ch > 7TV ch > FFZ glob > BTTV glob > 7TV glob.
_ladder = _merge([
    [{"name": "A", "provider": "ffz", "url": "f", "global": False}],
    [{"name": "A", "provider": "bttv", "url": "b", "global": False}],
    [{"name": "A", "provider": "7tv", "url": "s", "global": False}],
    [{"name": "A", "provider": "ffz", "url": "g", "global": True}],
    [{"name": "A", "provider": "bttv", "url": "g", "global": True}],
    [{"name": "A", "provider": "7tv", "url": "g", "global": True}],
])
assert len(_ladder) == 1 and _ladder[0]["provider"] == "ffz" and _ladder[0]["url"] == "f"
# Official Twitch globals sit BELOW every custom bucket (customs win), and a
# custom with the same name beats the official emote.
assert _merge([_ladder, _twitch_global])[0]["provider"] == "ffz"
assert _merge([[{"name": "LUL", "provider": "bttv", "url": "custom", "global": False}], _twitch_global])[0] == {
    "name": "LUL", "provider": "bttv", "url": "custom", "global": False,
}
# Global wins only when no channel bucket holds the name.
assert _merge([[{"name": "A", "provider": "bttv", "url": "g", "global": True}]])[0]["global"] is True
# Case-sensitive: KEKW and kekw are distinct emotes.
assert len(_merge([_bttv_global, [{"name": "kekw", "provider": "7tv", "url": "s", "global": False}]])) == 3
# fetch_emotes degrades to [] for non-Twitch platforms and empty slugs.
assert fetch_emotes("kick", "x") == []
assert fetch_emotes("youtube", "x") == []
assert fetch_emotes("twitch", "") == []
assert fetch_emotes("", "x") == []
