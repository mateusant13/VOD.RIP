"""YouTube live chat sink — tails yt-dlp's incremental .live_chat.json.

Spawns yt-dlp (python API, app auth wiring from youtube_session) with
``skip_download + writesubtitles + subtitleslangs=[live_chat]`` — the
equivalent of ``--skip-download --write-subs --sub-langs live_chat``, which
streams live chat without downloading the video (the archive's VOD ingest
owns the video). ``--live-from-start`` would imply a full DVR video download,
so we intentionally skip it.

NOTE: while LIVE, YouTube delivers top-chat only (newest-first); the
`live_chat.json` file is written line-by-line as actions arrive
(yt-dlp's YoutubeLiveChatFD flushes every fragment).

offset = (timestampUsec − stream_start_usec) / 1e6, with the stream start
taken from the yt-dlp info dict (release_timestamp/start_time) when present;
otherwise offsets anchor on the first seen message (ChatSink.handle_row).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.chat_sinks.base import ChatSink
from services.live_capture import _YT_LIVE_PAGE_RE

logger = logging.getLogger(__name__)

_PAGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
_CHAT_FILE_GLOB = "*.live_chat.json*"  # matches both .part (live) and final rename
_FILE_APPEAR_DEADLINE_SEC = 30.0


def resolve_live_video_id(handle: str) -> Optional[str]:
    """Resolve ``@handle/live`` (or channel/UC.../live) → current videoId."""
    live_url = (
        f"https://www.youtube.com/channel/{handle}/live"
        if handle.startswith("UC")
        else f"https://www.youtube.com/@{handle.lstrip('@')}/live"
    )
    try:
        import requests

        resp = requests.get(
            live_url,
            headers={"User-Agent": _PAGE_UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=15,
            allow_redirects=True,
        )
    except Exception:
        return None
    m = _YT_LIVE_PAGE_RE.search(resp.content or b"")
    return m.group(1).decode() if m else None


def parse_live_chat_line(line: str, base_usec: Optional[float] = None) -> Optional[dict]:
    """Parse one .live_chat.json line into an archive chat row (or None).

    Accepts both the live wrapper (``replayChatItemAction`` + ``isLive``)
    and raw replay actions — yt-dlp emits either depending on mode.
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    actions: Optional[list] = None
    rca = obj.get("replayChatItemAction")
    if isinstance(rca, dict):
        actions = rca.get("actions")
        if not isinstance(actions, list):
            actions = None
    if not actions and isinstance(obj.get("addChatItemAction"), dict):
        actions = [obj]
    if not actions:
        return None
    for action in actions:
        if not isinstance(action, dict):
            continue
        add = action.get("addChatItemAction")
        if not isinstance(add, dict):
            continue
        item = add.get("item")
        if not isinstance(item, dict):
            continue
        renderer = (item.get("liveChatTextMessageRenderer")
                    or item.get("liveChatPaidMessageRenderer"))
        if not isinstance(renderer, dict):
            continue
        return _row_from_renderer(renderer, base_usec)
    return None


def _row_from_renderer(renderer: dict, base_usec: Optional[float]) -> Optional[dict]:
    ts_usec = _int_or_none(renderer.get("timestampUsec"))
    ts_ms = ts_usec / 1000.0 if ts_usec is not None else None
    author = renderer.get("authorName") or {}
    username = author.get("simpleText") if isinstance(author, dict) else ""
    if not username:
        username = str(renderer.get("authorNameString") or "")
    text = _runs_text(renderer.get("message"))
    amount = renderer.get("purchaseAmountText") or {}
    if isinstance(amount, dict) and amount.get("simpleText"):
        text = f"{amount['simpleText']}: {text}"
    badges = [
        _badge_tooltip(b) for b in (renderer.get("authorBadges") or [])
        if _badge_tooltip(b)
    ]
    return {
        "offset_sec": ((ts_usec - base_usec) / 1e6
                       if (ts_usec is not None and base_usec is not None) else None),
        "user_id": renderer.get("authorExternalChannelId"),
        "username": username,
        "text": text,
        "badges": badges,
        "emotes": [],
        "ts": _iso_from_epoch_ms(ts_ms),
    }


def _badge_tooltip(badge) -> Optional[str]:
    """Tooltip from a live-chat authorBadge (nested renderer shape)."""
    if not isinstance(badge, dict):
        return None
    renderer = badge.get("liveChatAuthorBadgeRenderer")
    if isinstance(renderer, dict):
        return renderer.get("tooltip")
    return badge.get("tooltip")


def _runs_text(message) -> str:
    if not isinstance(message, dict):
        return ""
    runs = message.get("runs")
    if isinstance(runs, list):
        return "".join(
            r.get("text", "") for r in runs
            if isinstance(r, dict) and isinstance(r.get("text"), str)
        )
    simple = message.get("simpleText")
    return simple if isinstance(simple, str) else ""


class YTLiveSink(ChatSink):
    platform = "youtube"
    kind = "ytchat"

    def __init__(self, *, handle: str, **kwargs):
        super().__init__(**kwargs)
        self.handle = (handle or self.channel).lstrip("@")
        self._base_usec: Optional[float] = None

    def _run(self) -> None:
        vid = resolve_live_video_id(self.handle)
        if not vid:
            self.disconnect_reason = "could not resolve live video id"
            self._log.warning("yt chat %s: no live video id resolved", self.handle)
            return
        tmpdir = tempfile.mkdtemp(prefix="vodrip-ytchat-")
        try:
            ydl = _make_ydl(tmpdir, self.video_id, self._log)
            try:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}",
                                        download=False)
            except Exception as exc:
                self.disconnect_reason = f"yt-dlp extract failed: {exc}"
                self._log.warning("yt chat %s extract failed: %s", self.handle, exc)
                return
            if not info or not info.get("is_live"):
                self.disconnect_reason = "stream not live at connect"
                return
            self._base_usec = _base_usec_from_info(info)
            self._log.info("yt chat capture started for %s (vid %s, base_usec=%s)",
                           self.handle, vid, self._base_usec)
            worker = threading.Thread(
                target=_run_download, args=(ydl, info, self._log),
                daemon=True, name=self.name + "-ydlp",
            )
            worker.start()
            self._tail(tmpdir, worker)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _tail(self, tmpdir: str, worker: threading.Thread) -> None:
        chat_file = None
        chat_path: Optional[Path] = None
        deadline = time.monotonic() + _FILE_APPEAR_DEADLINE_SEC
        while not self.stop_requested():
            if chat_file is None:
                hits = _chat_files(tmpdir)
                if hits:
                    chat_path = hits[-1]
                    chat_file = chat_path.open("r", encoding="utf-8", errors="replace")
                elif time.monotonic() < deadline and worker.is_alive():
                    time.sleep(0.5)
                    continue
                else:
                    if not worker.is_alive():
                        self.disconnect_reason = (
                            self.disconnect_reason or "yt-dlp ended without chat file")
                    return
            line = chat_file.readline()
            if line:
                row = parse_live_chat_line(line, self._base_usec)
                if row:
                    self.handle_row(row)
                continue
            # EOF — the live FD writes <name>.live_chat.json.part and renames
            # to the final name when the stream ends; follow the newest file.
            hits = _chat_files(tmpdir)
            if hits and hits[-1] != chat_path:
                # yt-dlp renames the .part file it was streaming into (atomic
                # move), so the final file is the SAME data. Reopen at the
                # saved position — reopening from byte 0 would re-send the
                # whole chat into the archive and duplicate every message.
                pos = chat_file.tell()
                chat_file.close()
                chat_path = hits[-1]
                chat_file = chat_path.open("r", encoding="utf-8", errors="replace")
                chat_file.seek(pos)
                continue
            if not worker.is_alive():
                self.disconnect_reason = (
                    self.disconnect_reason or "stream ended (yt-dlp finished)")
                return
            time.sleep(0.5)


def _make_ydl(tmpdir: str, video_id: str, log: logging.Logger):
    """yt-dlp configured with the app's YouTube auth (cookies/POT/visitor)."""
    import yt_dlp

    from services.youtube_session import (
        apply_ytdlp_cookie_opts,
        youtube_session_from_settings,
        ytdlp_extractor_args,
    )

    yt_session = youtube_session_from_settings(video_id=video_id)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "writesubtitles": True,
        "subtitleslangs": ["live_chat"],
        "outtmpl": str(Path(tmpdir) / "%(title)s.%(ext)s"),
        "noplaylist": True,
    }
    try:
        opts["extractor_args"] = ytdlp_extractor_args(yt_session)
        apply_ytdlp_cookie_opts(opts, yt_session)
    except Exception as exc:
        log.warning("yt chat: auth wiring unavailable, using anonymous yt-dlp: %s", exc)
    from services.ytdlp_guard import ytdlp_console_logger

    opts["logger"] = ytdlp_console_logger()
    return yt_dlp.YoutubeDL(opts)


def _run_download(ydl, info: dict, log: logging.Logger) -> None:
    """Run the live_chat subtitle download (writes the .live_chat.json file)."""
    try:
        # process_ie_result skips re-extraction; with skip_download set it
        # runs only the subtitle downloader. Fall back to a full download()
        # if the internal API drifts across yt-dlp versions.
        ydl.process_ie_result(info, download=True)
    except (AttributeError, TypeError):
        try:
            ydl.download([info["webpage_url"]])
        except Exception as exc:
            log.warning("yt chat downloader ended with error: %s", exc)
    except Exception as exc:
        log.warning("yt chat downloader ended with error: %s", exc)


def _base_usec_from_info(info: dict) -> Optional[float]:
    for key in ("release_timestamp", "start_time"):
        v = info.get(key)
        if isinstance(v, (int, float)) and v:
            return float(v) * 1e6
    return None


def _chat_files(tmpdir: str) -> list:
    """Newest-first live_chat files, excluding yt-dlp's -FragN.part fragments.

    The live FD writes <title>.live_chat.json.part while streaming and
    renames it to <title>.live_chat.json when the stream ends; the fragment
    files (…-FragN.part) must never be opened by the tail reader or their
    post-download rename fails with WinError 32 on Windows.
    """
    return sorted(
        (p for p in Path(tmpdir).iterdir() if _is_chat_file(p.name)),
        key=lambda p: p.stat().st_mtime,
    )


def _is_chat_file(name: str) -> bool:
    return name.endswith(".live_chat.json") or name.endswith(".live_chat.json.part")


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


def _iso_from_epoch_ms(ms: Optional[float]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat(timespec="seconds")


# Module self-check: live-chat line parsing + canonical key, no network.
_LIVE_LINE = json.dumps({
    "replayChatItemAction": {
        "actions": [{
            "addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
                "message": {"runs": [{"text": "hello "}, {"text": "world"}]},
                "authorName": {"simpleText": "Lofi Girl"},
                "authorExternalChannelId": "UCjQ44ZOnBaqL2da2VpGdJAw",
                "timestampUsec": "1750000005000000",
                "authorBadges": [{"liveChatAuthorBadgeRenderer": {"tooltip": "Verified"}}],
            }}},
        }],
    },
    "videoOffsetTimeMsec": "1000",
    "isLive": True,
})
_row = parse_live_chat_line(_LIVE_LINE, 1750000000000000)
assert _row is not None
assert _row["username"] == "Lofi Girl" and _row["text"] == "hello world"
assert _row["badges"] == ["Verified"]
assert abs(_row["offset_sec"] - 5.0) < 1e-9, _row
_paid = json.dumps({
    "replayChatItemAction": {"actions": [{
        "addChatItemAction": {"item": {"liveChatPaidMessageRenderer": {
            "purchaseAmountText": {"simpleText": "$5.00"},
            "message": {"runs": [{"text": "ty!"}]},
            "authorName": {"simpleText": "fan"},
            "timestampUsec": "1750000010000000",
        }}},
    }]},
})
_row2 = parse_live_chat_line(_paid, 1750000000000000)
assert _row2 is not None and _row2["text"] == "$5.00: ty!" and _row2["username"] == "fan"
assert abs(_row2["offset_sec"] - 10.0) < 1e-9
assert parse_live_chat_line("not json") is None
assert parse_live_chat_line('{"replayChatItemAction":{"actions":[{"addChatItemAction":{"item":{"liveChatPlaceholderItemRenderer":{}}}}]}}') is None
assert _canonical_key("Último dia do Mundial!", "2026-08-01T22:30:00Z") == \
    "ultimo-dia-do-mundial|2026-08-01"
assert _canonical_key("Lofi hip hop radio", 1750000000) == "lofi-hip-hop-radio|2025-06-15"
