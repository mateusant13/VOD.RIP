"""Kick live chat sink — Pusher WebSocket over stdlib sockets (no PyPI deps).

Kick is the ONLY live chat capture point (no retro chat API), so this sink
must be robust: auto-reconnect with backoff and re-subscribe on every
connect. The Pusher app key is the public constant from kick.com's JS bundle
(overridable via VODRIP_KICK_PUSHER_KEY); the chatroom id and stream start
come from the same ``/api/v2/channels/<slug>`` payload the live-status
poller reads (kick_api_service).

offset_sec = (msg created_at − stream_start) / 1000, anchored on the first
seen message when the stream start is unknown (ChatSink.handle_row).
"""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Optional

from services.chat_sinks._ws import WSClient, WSClosed
from services.chat_sinks.base import ChatSink

logger = logging.getLogger(__name__)

PUSHER_HOST = "ws-us2.pusher.com"
# ponytail: kick.com ships a first-party gateway (wss://websockets.kick.com/viewer/v1/connect,
# client token in its env config) as the long-term replacement for Pusher; this sink keeps
# the public Pusher endpoint (still served, key below) and the stdlib WS client swaps hosts
# when Kick kills it. Upstream: replace PUSHER_HOST/DEFAULT_APP_KEY and the subscribe event.
PUSHER_PORT = 443
# Public app key embedded in kick.com's client bundle; env override in case
# Kick rotates it (the sink logs the key it actually used).
DEFAULT_APP_KEY = "32cbd69e4b950bf97679"
CHAT_EVENT = "App\\Events\\ChatMessageEvent"
RECONNECT_BASE_SEC = 3.0
RECONNECT_MAX_SEC = 60.0


def resolve_chat_config(slug: str) -> dict:
    """Live payload → {chatroom_id, livestream_id, start_time (epoch s), title}.

    Reuses the exact Kick v2 channel payload the live poller reads (via
    kick_api_service._get_json) — no second API implementation. Missing
    fields yield None so the sink degrades to first-message anchoring.
    """
    from services.kick_api_service import _get_json

    data = _get_json(f"/api/v2/channels/{slug}", f"https://kick.com/{slug}")
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected Kick channel API response")
    ls = data.get("livestream") if isinstance(data.get("livestream"), dict) else {}
    chatroom = ls.get("chatroom") if isinstance(ls.get("chatroom"), dict) else {}
    # Offline channels carry the chatroom at the payload top level; live
    # payloads nest it under livestream. Try both so the sink can subscribe
    # even before a stream starts (chat usually opens at stream start).
    if not chatroom.get("id") and isinstance(data.get("chatroom"), dict):
        chatroom = data["chatroom"]
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    return {
        "chatroom_id": _int_or_none(ls.get("chatroom_id") or chatroom.get("id")),
        "livestream_id": _int_or_none(ls.get("id")),
        "start_time": _float_or_none(ls.get("start_time")),  # epoch seconds
        "title": ls.get("session_title") or user.get("username") or slug,
    }


def parse_chat_event(event_text: str, stream_start_ms: Optional[float] = None) -> Optional[dict]:
    """Parse one Pusher event frame into an archive chat row (or None).

    Accepts the full event object (``{"event": ..., "data": "..."}``) — the
    ``data`` value is itself a JSON-encoded message object (Pusher
    convention). Handles both the modern nested shape (``message.content`` /
    ``user.username``) and the legacy flat shape (``content`` / ``username``).
    """
    try:
        event = json.loads(event_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(event, dict):
        return None
    name = event.get("event") or ""
    if name not in (CHAT_EVENT, "chat_message"):
        return None
    data = event.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    text = data.get("content")
    if text is None:
        msg = data.get("message") if isinstance(data.get("message"), dict) else {}
        text = msg.get("content")
        if not isinstance(text, str):
            return None
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
    identity = sender.get("identity") if isinstance(sender.get("identity"), dict) else {}
    username = user.get("username") or data.get("username") or sender.get("username") or ""
    user_id = user.get("id") or data.get("user_id") or sender.get("id")
    created_ms = _created_at_ms(data.get("created_at"))
    badges = data.get("badges") if isinstance(data.get("badges"), list) else \
        identity.get("badges") if isinstance(identity.get("badges"), list) else []
    row: dict = {
        "offset_sec": ((created_ms - stream_start_ms) / 1000.0
                       if (created_ms is not None and stream_start_ms is not None)
                       else None),
        "user_id": _int_or_none(user_id) if user_id is not None else None,
        "username": str(username),
        "text": str(text),
        "badges": badges,
        "emotes": data.get("emotes") if isinstance(data.get("emotes"), list) else [],
        "ts": _iso_from_epoch_ms(created_ms),
    }
    return row


class KickPusherSink(ChatSink):
    platform = "kick"
    kind = "pusher"

    def __init__(self, *, slug: str, app_key: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        self.slug = (slug or self.channel).strip()
        self.app_key = (app_key or
                        __import__("os").environ.get("VODRIP_KICK_PUSHER_KEY") or
                        DEFAULT_APP_KEY)
        self.chatroom_id: Optional[int] = None
        self._ws: Optional[WSClient] = None

    def _interrupt(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _run(self) -> None:
        backoff = RECONNECT_BASE_SEC
        while not self.stop_requested():
            try:
                self._connect_and_read()
                backoff = RECONNECT_BASE_SEC
            except Exception as exc:
                if self.stop_requested():
                    break
                self._log.warning("kick pusher %s error (%s); reconnect in %.0fs",
                                  self.slug, exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_SEC)
        self.disconnect_reason = self.disconnect_reason or "stopped"

    def _connect_and_read(self) -> None:
        if self.chatroom_id is None:
            config = resolve_chat_config(self.slug)
            self.chatroom_id = config.get("chatroom_id")
            # Kick gives the stream start in the live payload — adopt it when
            # the watchdog did not already have one.
            start = config.get("start_time")
            if start and self.stream_start_ts is None:
                self.stream_start_ts = float(start) * 1000.0
            if not self.title and config.get("title"):
                self.title = config["title"]
        if not self.chatroom_id:
            raise RuntimeError(f"no chatroom id for kick channel {self.slug}")

        url = (f"wss://{PUSHER_HOST}/app/{self.app_key}"
               "?protocol=7&client=js&version=8.4.0&flash=false")
        ws = WSClient(url, origin="https://kick.com")
        self._ws = ws
        try:
            ws.connect()
            ws.send_text(json.dumps({
                "event": "pusher:subscribe",
                "data": {"channel": f"chat.{self.chatroom_id}"},
            }))
            self._log.info("kick pusher subscribed to chat.%s (app key %s...)",
                           self.chatroom_id, self.app_key[:8])
            while not self.stop_requested():
                frame = ws.recv_text(timeout=30.0)
                try:
                    ev = json.loads(frame)
                except ValueError:
                    continue
                name = ev.get("event") if isinstance(ev, dict) else ""
                if name == "pusher:ping":
                    ws.send_text(json.dumps({"event": "pusher:pong", "data": "{}"}))
                    continue
                if name == "pusher:subscribe_succeeded":
                    self._log.info("kick pusher subscribe succeeded for chat.%s",
                                   self.chatroom_id)
                    continue
                if name in (CHAT_EVENT, "chat_message"):
                    row = parse_chat_event(frame, self.stream_start_ts)
                    if row:
                        self.handle_row(row)
        finally:
            self._ws = None
            try:
                ws.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# canonical_key — identical copy in twitch_irc.py / kick_pusher.py / yt_live.py
# (Main's cross-platform dedupe contract; keep the three copies the same).
# ---------------------------------------------------------------------------

def _canonical_key(title: str, started_at: Optional[str]) -> str:
    """Normalized title + UTC date: NFKD → drop combining marks → lower →
    runs of [^0-9a-z] → '-' → strip → '|' + YYYY-MM-DD (empty title → 'untitled')."""
    t = unicodedata.normalize("NFKD", title or "")
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^0-9a-z]+", "-", t.lower()).strip("-")
    if not t:
        t = "untitled"
    day = _utc_date(started_at)
    return f"{t}|{day}" if day else t


def _utc_date(started_at: Optional[str]) -> Optional[str]:
    """UTC YYYY-MM-DD from an ISO string or a numeric epoch (s/ms)."""
    value = started_at
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 10_000_000_000:  # epoch ms
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------

def _int_or_none(value) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _created_at_ms(value) -> Optional[float]:
    """Kick created_at is ISO-8601 UTC; tolerate numeric epochs too."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        return ts if ts > 10_000_000_000 else ts * 1000.0  # s → ms
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


def _iso_from_epoch_ms(ms: Optional[float]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat(timespec="seconds")


# Module self-check: parsing both payload shapes + canonical key, no network.
_MODERN = ('{"event":"App\\\\Events\\\\ChatMessageEvent","channel":"chat.123",'
           '"data":"{\\"id\\":42,\\"user\\":{\\"id\\":7,\\"username\\":\\"lubu\\",'
           '\\"slug\\":\\"lubu\\"},\\"message\\":{\\"content\\":\\"gg!\\"},'
           '\\"created_at\\":\\"2026-08-01T22:30:05.000000Z\\"}"}')
_row = parse_chat_event(_MODERN, _created_at_ms("2026-08-01T22:30:00Z"))
assert _row is not None
assert _row["username"] == "lubu" and _row["user_id"] == 7 and _row["text"] == "gg!"
assert abs(_row["offset_sec"] - 5.0) < 1e-9, _row
_LEGACY = ('{"event":"chat_message","data":"{\\"id\\":1,\\"user_id\\":2,'
           '\\"username\\":\\"old\\",\\"content\\":\\"hi\\",'
           '\\"created_at\\":\\"2026-08-01T22:30:10Z\\"}"}')
_row2 = parse_chat_event(_LEGACY, _created_at_ms("2026-08-01T22:30:00Z"))
assert _row2 is not None and _row2["username"] == "old" and _row2["text"] == "hi"
assert abs(_row2["offset_sec"] - 10.0) < 1e-9
assert parse_chat_event('{"event":"pusher:ping","data":"{}"}') is None
assert _canonical_key("Último dia do Mundial!", "2026-08-01T22:30:00Z") == \
    "ultimo-dia-do-mundial|2026-08-01"
assert _canonical_key("Stream!", 1750000000) == "stream|2025-06-15"
