"""Twitch live chat sink — anonymous read-only IRC (justinfan user).

Connects to irc.twitch.tv:6667 with the anonymous login Twitch itself uses
for logged-out viewers, requests the tags capability (CAP REQ twitch.tv/tags)
and joins ``#<login>``. PRIVMSG lines carry tmi-sent-ts (epoch ms),
display-name, user-id, badges and emotes in the tag block.

offset_sec = (tmi-sent-ts − stream_start_ts) / 1000, with stream_start_ts
(epoch ms) taken from the live-status payload (Helix ``started_at`` when the
app has Helix creds). When the stream start is unknown, offsets anchor on the
first seen message (handled by ChatSink.handle_row).

Reconnects with exponential backoff (2s → 30s cap) on socket errors.
"""
from __future__ import annotations

import logging
import re
import socket
import time
import unicodedata
from datetime import datetime, timezone
from typing import Optional

from services.chat_sinks.base import ChatSink

logger = logging.getLogger(__name__)

IRC_HOST = "irc.twitch.tv"
IRC_PORT = 6667
_ANON_NICK = "justinfan12345"  # anonymous read-only user, per Twitch docs
_ANON_PASS = "SCHMOOPIIE"      # constant Twitch accepts for anonymous logins
RECONNECT_BASE_SEC = 2.0
RECONNECT_MAX_SEC = 30.0

_PRIVMSG_RE = re.compile(r"^:(\S+)!\S+@\S+ PRIVMSG #(\S+) :(.*)$", re.S)


def is_ping(line: str) -> bool:
    return line.startswith("PING ")


def parse_privmsg(line: str, stream_start_ms: Optional[float] = None) -> Optional[dict]:
    """Parse one IRC line into an archive chat row (or None).

    ``stream_start_ms`` is the stream's epoch-ms start; when omitted the row
    carries ``offset_sec=None`` and ChatSink.handle_row anchors it on the
    first seen message.
    """
    if " PRIVMSG " not in line:
        return None
    tags: dict[str, str] = {}
    rest = line
    if line.startswith("@"):
        head, _, rest = line.partition(" ")
        for kv in head[1:].split(";"):
            key, _, value = kv.partition("=")
            tags[key] = value
    m = _PRIVMSG_RE.match(rest)
    if not m:
        return None
    prefix_user, _channel, text = m.groups()
    ts_ms = _int_or_none(tags.get("tmi-sent-ts"))
    username = tags.get("display-name") or prefix_user
    badges = [b for b in (tags.get("badges") or "").split(",") if b]
    emotes = [e.split(":")[0] for e in (tags.get("emotes") or "").split(",") if e]
    row: dict = {
        "offset_sec": ((ts_ms - stream_start_ms) / 1000.0
                       if (ts_ms is not None and stream_start_ms is not None)
                       else None),
        "user_id": tags.get("user-id") or None,
        "username": username,
        "text": text,
        "badges": badges,
        "emotes": emotes,
        "ts": _iso_from_epoch_ms(ts_ms),
    }
    return row


class TwitchIRCSink(ChatSink):
    platform = "twitch"
    kind = "irc"

    def __init__(self, *, login: str, **kwargs):
        super().__init__(**kwargs)
        self.login = (login or self.channel).lstrip("#").lower()

    def _interrupt(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _run(self) -> None:
        backoff = RECONNECT_BASE_SEC
        while not self.stop_requested():
            try:
                self._connect_once()
                backoff = RECONNECT_BASE_SEC
            except Exception as exc:
                if self.stop_requested():
                    break
                self._log.warning("twitch IRC %s error (%s); reconnect in %.0fs",
                                  self.login, exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_SEC)
        self.disconnect_reason = self.disconnect_reason or "stopped"

    def _connect_once(self) -> None:
        sock = socket.create_connection((IRC_HOST, IRC_PORT), timeout=10.0)
        self._sock = sock
        sock.settimeout(15.0)
        try:
            self._send(sock, f"PASS {_ANON_PASS}")
            self._send(sock, f"NICK {_ANON_NICK}")
            self._send(sock, "CAP REQ :twitch.tv/tags")
            self._send(sock, f"JOIN #{self.login}")
            self._log.info("twitch IRC joined #%s as %s", self.login, _ANON_NICK)
            buf = b""
            while not self.stop_requested():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    raise ConnectionError("IRC connection closed by server")
                buf += chunk
                while b"\r\n" in buf:
                    raw, buf = buf.split(b"\r\n", 1)
                    self._handle_line(raw.decode("utf-8", "replace"))
        finally:
            self._sock = None
            try:
                sock.close()
            except OSError:
                pass

    def _handle_line(self, line: str) -> None:
        if is_ping(line):
            if self._sock is not None:
                try:
                    self._send(self._sock, "PONG " + line[5:])
                except OSError:
                    pass
            return
        if " PRIVMSG " not in line:
            return
        row = parse_privmsg(line, self.stream_start_ts)
        if row:
            self.handle_row(row)

    @staticmethod
    def _send(sock: socket.socket, text: str) -> None:
        sock.sendall((text + "\r\n").encode("utf-8"))


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

def _int_or_none(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _iso_from_epoch_ms(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat(timespec="seconds")


# Module self-check: parser + canonical_key math, no network.
_SAMPLE = (
    "@badge-info=;badges=moderator/1,bits/100;color=#0C8CFF;display-name=ModBot;"
    "emotes=25:0-4,1902:12-19;id=abc123;mod=1;room-id=123456;subscriber=0;"
    "tmi-sent-ts=1750000000000;turbo=0;user-id=987654;user-type=mod "
    ":modbot!modbot@modbot.tmi.twitch.tv PRIVMSG #lubumr :Hello Kappa"
)
_row = parse_privmsg(_SAMPLE, 1750000000000)
assert _row is not None
assert abs(_row["offset_sec"] - 0.0) < 1e-9, _row
assert _row["username"] == "ModBot" and _row["user_id"] == "987654"
assert _row["badges"] == ["moderator/1", "bits/100"] and _row["emotes"] == ["25", "1902"]
assert _row["text"] == "Hello Kappa"
_row2 = parse_privmsg(_SAMPLE, 1749999900000)
assert _row2 is not None and abs(_row2["offset_sec"] - 100.0) < 1e-9
assert parse_privmsg("PING :tmi.twitch.tv") is None
assert is_ping("PING :tmi.twitch.tv") and not is_ping("@badges=1 ...")
assert _canonical_key("Último dia do Mundial!", "2026-08-01T22:30:00Z") == \
    "ultimo-dia-do-mundial|2026-08-01"
assert _canonical_key("Watchparty do Mundial!", "2026-08-01T22:30:00Z") == \
    "watchparty-do-mundial|2026-08-01"
assert _canonical_key("!!!", None) == "untitled"
assert _canonical_key("A  B__C", 1750000000) == "a-b-c|2025-06-15"
