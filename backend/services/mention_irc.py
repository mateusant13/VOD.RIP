"""24/7 Twitch IRC mention logger.

Stays joined to every saved Twitch channel (anonymous justinfan), like
Chatterino staying connected. Only persists PRIVMSG lines whose text
contains a saved channel name as a whole word (xqc, asmongold, ...).

Rows land in archive messages so search for that channel name still
returns hits when transcripts/chat history are missing.
"""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
from typing import Optional

from services.chat_sinks.twitch_irc import (
    IRC_HOST, IRC_PORT, _ANON_NICK, _ANON_PASS, is_ping, parse_privmsg,
)

logger = logging.getLogger(__name__)
RECONNECT_SEC = 8.0

_stop = threading.Event()
_thread: Optional[threading.Thread] = None
_word_re_cache: tuple[tuple[str, ...], re.Pattern[str]] | None = None


def _saved_twitch_logins() -> list[str]:
    try:
        from deps import settings_mgr
        chans = settings_mgr.get().saved_channels or []
    except Exception:
        return []
    out = []
    for ch in chans:
        slug = (getattr(ch, "twitchSlug", None) or (ch.get("twitchSlug") if isinstance(ch, dict) else "") or "")
        slug = str(slug).strip().lstrip("#").lower()
        if slug:
            out.append(slug)
        name = (getattr(ch, "name", None) or (ch.get("name") if isinstance(ch, dict) else "") or "")
        name = str(name).strip().lower()
        if name and name not in out:
            out.append(name)
    return sorted(set(out))


def _mention_pattern(names: list[str]) -> re.Pattern[str] | None:
    global _word_re_cache
    key = tuple(names)
    if _word_re_cache and _word_re_cache[0] == key:
        return _word_re_cache[1]
    if not names:
        _word_re_cache = (key, re.compile(r"(?!x)x"))
        return _word_re_cache[1]
    pat = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z0-9_])", re.I)
    _word_re_cache = (key, pat)
    return pat


def _persist(login: str, row: dict, names: list[str]) -> None:
    text = row.get("text") or ""
    pat = _mention_pattern(names)
    if not pat or not pat.search(text):
        return
    from services import archive_db
    vid = f"mention-{login}"
    try:
        archive_db.upsert_video({
            "platform": "twitch",
            "video_id": vid,
            "channel": login,
            "title": f"Mentions in #{login}",
            "kind": "vod",
            "status": "known",
        })
    except Exception:
        pass
    try:
        archive_db.insert_messages("twitch", vid, [row])
    except Exception:
        logger.debug("mention persist failed", exc_info=True)


def _run() -> None:
    while not _stop.is_set():
        names = _saved_twitch_logins()
        if not names:
            _stop.wait(15)
            continue
        try:
            _session(names)
        except Exception as exc:
            logger.warning("mention IRC session error: %s", exc)
        _stop.wait(RECONNECT_SEC)


def _session(names: list[str]) -> None:
    sock = socket.create_connection((IRC_HOST, IRC_PORT), timeout=10.0)
    sock.settimeout(20.0)
    try:
        def send(msg: str) -> None:
            sock.sendall((msg + "\r\n").encode("utf-8"))
        send(f"PASS {_ANON_PASS}")
        send(f"NICK {_ANON_NICK}")
        send("CAP REQ :twitch.tv/tags")
        for login in names:
            send(f"JOIN #{login}")
        logger.info("mention IRC joined %s channels", len(names))
        buf = b""
        last_refresh = time.monotonic()
        while not _stop.is_set():
            if time.monotonic() - last_refresh > 60:
                now = _saved_twitch_logins()
                if now != names:
                    return  # reconnect with new set
                last_refresh = time.monotonic()
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("IRC closed")
            buf += chunk
            while b"\r\n" in buf:
                raw, buf = buf.split(b"\r\n", 1)
                line = raw.decode("utf-8", "replace")
                if is_ping(line):
                    send("PONG " + line[5:])
                    continue
                row = parse_privmsg(line)
                if not row:
                    continue
                ch = names[0]
                if " PRIVMSG #" in line:
                    try:
                        ch = line.split(" PRIVMSG #", 1)[1].split(" ", 1)[0].strip().lower()
                    except Exception:
                        ch = names[0]
                _persist(ch, row, names)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def start_mention_irc() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, name="mention-irc", daemon=True)
    _thread.start()
    logger.info("mention IRC logger started")


def stop_mention_irc(timeout: float = 3.0) -> None:
    _stop.set()
    th = _thread
    if th:
        th.join(timeout=timeout)
