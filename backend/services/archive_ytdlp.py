"""YouTube ingestion adapter for the local archive ("local Google").

Fetches metadata, auto captions (VTT), and live-chat replay (NDJSON) for a
YouTube video and writes them through archive_db. Channel walk uses the flat
playlist extractor so the whole channel is one lightweight call.

Live chat is best-effort: only streams (live_status == 'was_live') have chat
replay; VODs yield zero chat rows and report chat: 'none'. Captions come from
YouTube's auto-generated tracks (pt preferred, then en, then any language).

All offsets written to the archive are float seconds into the stream
(archive contract). Chat offset uses yt-dlp's replay fragment
videoOffsetTimeMsec (already stream-relative), not the wall-clock
timestampUsec.
"""
from __future__ import annotations

import json
import logging
import re
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services import archive_db
from services.ytdlp_ffmpeg import _ytdlp_engine_opts
from services.ytdlp_guard import guarded_youtube_dl, guarded_youtube_dl_channel

logger = logging.getLogger(__name__)

PLATFORM = "youtube"
# Priority rule: YouTube > Twitch > Kick (lower number wins).
_CAPTION_LANG_PREF = ("pt", "pt-br", "en", "en-orig")

_TS_RE = re.compile(r"^(\d{8})$")
_CUE_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})"
)
_WORD_TS_RE = re.compile(r"<(\d{2}):(\d{2}):(\d{2})\.(\d{3})><c>([^<]*)</c>")
_TAG_RE = re.compile(r"<[^>]+>")


# --- canonical_key (identical helper in every platform adapter) ----------

def _utc_date(value: Any) -> Optional[str]:
    """YYYY-MM-DD (UTC) from ISO string, unix timestamp, or YYYYMMDD."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%Y-%m-%d")
    s = str(value).strip()
    m = _TS_RE.match(s)
    if m:
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _norm_title(title: str) -> str:
    """NFKD-strip diacritics, lowercase, collapse non-alnum runs to '-'."""
    norm = unicodedata.normalize("NFKD", title or "")
    norm = "".join(ch for ch in norm if not unicodedata.combining(ch))
    norm = re.sub(r"[^0-9a-z]+", "-", norm.lower()).strip("-")
    return norm or "untitled"


def _canonical_key(title: str, started_at: Any) -> str:
    """Normalized title + UTC date of started_at, per cross-platform contract.

    NFKD diacritic-stripped lowercase title with runs of non-alphanumerics
    collapsed to single '-' and trimmed, then '|' + YYYY-MM-DD (UTC date of
    started_at). Unknown started_at -> creation date is used by the caller;
    if none is available the key is title-only. Empty title -> "untitled".
    """
    return f"{_norm_title(title)}|{_utc_date(started_at)}" if _utc_date(started_at) else _norm_title(title)


# --- parsing ---------------------------------------------------------------

def _parse_vtt(text: str) -> list[dict]:
    """Convert a VTT caption document to archive transcript segments.

    Segments: {seg_idx, start_sec, end_sec, text, words}. words is a
    best-effort list of {word, start, end} built from YouTube's inline
    word timestamps (<00:00:03.199><c> sei.</c>); empty when the track has
    none. Filler cues (empty text) are dropped.
    """
    segments: list[dict] = []
    idx = 0
    # Group lines into cue blocks on truly empty lines; YouTube auto-captions
    # put a leading whitespace-only continuation line INSIDE a block
    # (" \nNão sei."), so whitespace-only lines must NOT split blocks.
    blocks: list[list[str]] = []
    cur: list[str] = []
    for ln in text.splitlines():
        if ln == "":
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append(ln)
    if cur:
        blocks.append(cur)
    for block_lines in blocks:
        if not block_lines:
            continue
        m = _CUE_RE.match(block_lines[0].strip())
        if not m:
            continue  # WEBVTT header, NOTE/STYLE/REGION blocks
        start = _cue_secs(*m.groups()[:4])
        end = _cue_secs(*m.groups()[4:])
        raw = " ".join(block_lines[1:])
        cleaned = _TAG_RE.sub("", raw).replace("\n", " ").strip()
        if not cleaned:
            continue
        words = _vtt_words(raw, end)
        segments.append({
            "seg_idx": idx,
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
            "text": cleaned,
            "words": words,
        })
        idx += 1
    return segments


def _cue_secs(hh: str, mm: str, ss: str, mmm: str) -> float:
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(mmm) / 1000.0


def _vtt_words(raw: str, cue_end: float) -> list[dict]:
    """Timestamped tokens only; untagged words carry no time -> skipped."""
    out: list[dict] = []
    for m in _WORD_TS_RE.finditer(raw):
        start = _cue_secs(*m.groups()[:4])
        word = m.group(5).strip()
        if not word:
            continue
        out.append({"word": word, "start": round(start, 3), "end": round(cue_end, 3)})
    for j in range(len(out) - 1, 0, -1):
        out[j - 1]["end"] = out[j]["start"]
    return out


def _iso_from_usec(usec: Any) -> Optional[str]:
    if not usec:
        return None
    try:
        return datetime.fromtimestamp(int(usec) / 1e6, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    except (TypeError, ValueError, OSError):
        return None


def _parse_live_chat(text: str) -> list[dict]:
    """NDJSON from yt-dlp's .live_chat.json -> archive message rows.

    Rows: {offset_sec, user_id, username, text, badges, emotes, ts}.
    offset_sec = replay fragment videoOffsetTimeMsec/1000 (stream-relative);
    falls back to wall-clock timestampUsec/1e6 when the fragment lacks one.
    """
    rows: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            action_line = json.loads(line)
        except json.JSONDecodeError:
            continue
        rac = action_line.get("replayChatItemAction") or {}
        frag_ms = rac.get("videoOffsetTimeMsec")
        for act in rac.get("actions") or []:
            item = ((act.get("addChatItemAction") or {}).get("item") or {})
            renderer = None
            for key in (
                "liveChatTextMessageRenderer",
                "liveChatPaidMessageRenderer",
                "liveChatMembershipItemRenderer",
            ):
                renderer = item.get(key)
                if renderer:
                    break
            if not renderer:
                continue
            runs = ((renderer.get("message") or {}).get("runs") or [])
            text = "".join(str(run.get("text", "")) for run in runs).strip()
            if not text:
                continue
            author = (renderer.get("authorName") or {}).get("simpleText") or ""
            user_id = renderer.get("authorExternalChannelId") or None
            usec = renderer.get("timestampUsec")
            if frag_ms is not None:
                offset = float(frag_ms) / 1000.0
            else:
                offset = float(usec or 0) / 1e6
            badges = [
                b.get("liveChatAuthorBadgeRenderer", {}).get("tooltip", "")
                for b in (renderer.get("authorBadges") or [])
                if b.get("liveChatAuthorBadgeRenderer", {}).get("tooltip")
            ]
            emotes = [
                {"id": run.get("emoji", {}).get("emojiId"),
                 "text": (run.get("emoji", {}).get("shortcuts") or [""])[0]}
                for run in runs
                if run.get("emoji")
            ]
            rows.append({
                "offset_sec": round(offset, 3),
                "user_id": user_id,
                "username": author,
                "text": text,
                "badges": badges,
                "emotes": emotes,
                "ts": _iso_from_usec(usec),
            })
    return rows


# --- extraction -------------------------------------------------------------

def _yt_opts(outdir: Path) -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "extractor_args": {"youtube": {"player_client": ["android", "web_safari"]}},
        **_ytdlp_engine_opts(),
        # live_chat arrives as a subtitle entry (ext 'json'); with
        # skip_download yt-dlp never writes subtitles, so ingest_video calls
        # ydl._write_subtitles explicitly inside the guarded instance.
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["live_chat"],
        "outtmpl": str(outdir / "%(id)s.%(ext)s"),
    }


def _pick_caption(info: dict) -> tuple[Optional[str], Optional[str]]:
    """Choose the best auto-caption track: pt > en > first with a vtt URL."""
    ac = info.get("automatic_captions") or {}
    subs = info.get("subtitles") or {}
    merged = dict(ac)
    for k, v in subs.items():
        merged.setdefault(k, v)
    for lang in _CAPTION_LANG_PREF:
        for entry in merged.get(lang) or []:
            if entry.get("ext") == "vtt" and entry.get("url"):
                return lang, entry["url"]
    for lang, entries in merged.items():
        if lang == "live_chat":
            continue
        for entry in entries:
            if entry.get("ext") == "vtt" and entry.get("url"):
                return lang, entry["url"]
    return None, None


def _video_url(id_or_url: str) -> str:
    if "://" in id_or_url:
        return id_or_url
    return f"https://www.youtube.com/watch?v={id_or_url.strip()}"


def _video_id_from_url(url: str, fallback: str) -> str:
    m = re.search(r"[?&]v=([\w-]{6,})", url)
    if not m:
        m = re.search(r"youtu\.be/([\w-]{6,})", url)
    return m.group(1) if m else fallback


def ingest_video(id_or_url: str, *, temp_dir: Optional[Path] = None) -> dict:
    """Ingest one YouTube video: metadata + captions + best-effort live chat.

    Returns a report dict (video_id, title, channel, started_at,
    duration_sec, canonical_key, caption_lang, transcript_segments,
    chat_messages, chat: 'replay'|'none'|'error'). Raises on metadata
    extraction failure; caption/chat failures are reported, not raised.
    """
    url = _video_url(id_or_url)
    video_id = _video_id_from_url(url, id_or_url.strip())
    job_id = f"yt-ingest-{video_id}-{int(time.time())}"
    _db_write(archive_db.enqueue_job, job_id, "ingest", PLATFORM, video_id)
    report: dict = {
        "video_id": video_id,
        "title": "",
        "channel": "",
        "started_at": None,
        "duration_sec": None,
        "canonical_key": "",
        "caption_lang": None,
        "transcript_segments": 0,
        "chat_messages": 0,
        "chat": "none",
        "chat_error": None,
        "caption_error": None,
    }
    own_dir = temp_dir is None
    outdir = Path(tempfile.mkdtemp(prefix=f"yt-{video_id}-")) if own_dir else Path(temp_dir)
    try:
        with guarded_youtube_dl(_yt_opts(outdir)) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise ValueError(f"no extract info for {url}")
            if info.get("id"):
                video_id = str(info["id"])
            title = str(info.get("title") or "")
            channel = str(info.get("channel") or info.get("uploader") or "")
            started_at = _started_at_iso(info)
            duration = _float_or_none(info.get("duration"))
            key = _canonical_key(title, started_at)
            report.update({
                "video_id": video_id,
                "title": title,
                "channel": channel,
                "started_at": started_at,
                "duration_sec": duration,
                "canonical_key": key,
            })

            # Dedupe: YouTube is the highest-priority platform, so nothing can
            # outrank it; still record the key + alias for cross-platform dedupe.
            # Idempotent re-ingest: drop any earlier messages/transcripts first.
            _db_write(_clear_video_data, video_id)
            if key:
                _db_write(archive_db.set_alias, PLATFORM, video_id, key, note="auto")
            _db_write(archive_db.upsert_video, {
                "platform": PLATFORM,
                "video_id": video_id,
                "channel": channel or "unknown",
                "title": title or video_id,
                "started_at": started_at,
                "duration_sec": duration,
                "archive_path": None,
                "canonical_key": key or None,
                "status": "known",
            })

            # Live chat replay (best-effort; VODs have none).
            if info.get("live_status") in ("was_live", "is_live"):
                try:
                    chat_file = None
                    sub_base = ydl.prepare_filename(info, "subtitle")
                    written = ydl._write_subtitles(info, sub_base)
                    for sub, _final in written or []:
                        if str(sub).endswith(".live_chat.json"):
                            chat_file = Path(sub)
                            break
                    if chat_file and chat_file.is_file():
                        rows = _parse_live_chat(chat_file.read_text(encoding="utf-8"))
                        if rows:
                            _db_write(archive_db.insert_messages, PLATFORM, video_id, rows)
                            report["chat_messages"] = len(rows)
                            report["chat"] = "replay"
                        else:
                            report["chat"] = "none"  # replay exists but is empty
                except Exception as exc:
                    logger.warning("live chat failed for %s: %s", video_id, exc)
                    report["chat"] = "error"
                    report["chat_error"] = str(exc)

            # Auto captions -> transcript segments.
            try:
                lang, vtt_url = _pick_caption(info)
                report["caption_lang"] = lang
                if vtt_url:
                    data = ydl.urlopen(vtt_url).read().decode("utf-8", "replace")
                    segments = _parse_vtt(data)
                    if segments:
                        _db_write(archive_db.insert_transcript, PLATFORM, video_id, segments)
                        report["transcript_segments"] = len(segments)
            except Exception as exc:
                logger.warning("captions failed for %s: %s", video_id, exc)
                report["caption_error"] = str(exc)

        try:
            _db_write(archive_db.update_job, job_id, status="done", progress=1.0)
        except Exception as exc:  # job bookkeeping must not fail the ingest
            logger.warning("job status update failed for %s: %s", video_id, exc)
    except Exception:
        try:
            _db_write(archive_db.update_job, job_id, status="failed", error="extract error")
        except Exception:
            pass
        raise
    finally:
        if own_dir:
            try:
                for f in outdir.iterdir():
                    f.unlink(missing_ok=True)
                outdir.rmdir()
            except OSError:
                pass
    return report


def _started_at_iso(info: dict) -> Optional[str]:
    ts = info.get("timestamp")
    if ts:
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(
                timespec="seconds"
            )
        except (TypeError, ValueError, OSError):
            pass
    ud = str(info.get("upload_date") or "").strip()
    if _TS_RE.match(ud):
        return f"{ud[0:4]}-{ud[4:6]}-{ud[6:8]}T00:00:00+00:00"
    return None


def _float_or_none(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# --- channel walk -----------------------------------------------------------


# --- shared DB helpers ------------------------------------------------------

_LOCK_RETRIES = 6
_LOCK_SLEEP_S = 5.0


def _db_write(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a DB write with bounded retry on cross-process lock contention.

    The archive DB is shared with the running backend plus the other
    platform adapters (twitch/kick/chat backfills can hold a write txn for
    many seconds); sqlite busy_timeout alone (10s) is not always enough.
    """
    import sqlite3
    import time as _time

    last: Optional[BaseException] = None
    for attempt in range(_LOCK_RETRIES):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            last = exc
            if "locked" not in str(exc).lower():
                raise
            if attempt < _LOCK_RETRIES - 1:
                _time.sleep(_LOCK_SLEEP_S)
    assert last is not None
    raise last


def _clear_video_data(video_id: str) -> None:
    """Drop messages/transcripts/aliases for one video (idempotent re-ingest).

    Keeps FTS5 in sync (regular FTS5 tables do not cascade on base DELETE).
    """
    conn = archive_db.get_conn()
    with conn:
        conn.execute(
            "DELETE FROM messages_fts WHERE rowid IN "
            "(SELECT id FROM messages WHERE platform=? AND video_id=?)",
            (PLATFORM, video_id),
        )
        conn.execute(
            "DELETE FROM transcripts_fts WHERE rowid IN "
            "(SELECT id FROM transcripts WHERE platform=? AND video_id=?)",
            (PLATFORM, video_id),
        )
        conn.execute("DELETE FROM messages WHERE platform=? AND video_id=?", (PLATFORM, video_id))
        conn.execute("DELETE FROM transcripts WHERE platform=? AND video_id=?", (PLATFORM, video_id))
        conn.execute("DELETE FROM video_aliases WHERE platform=? AND video_id=?", (PLATFORM, video_id))


def list_channel_videos(channel_url: str, *, tab: str = "streams", limit: int = 3) -> list[dict]:
    """Flat-playlist walk of a channel tab; returns [{id, title, duration}].

    Default tab is 'streams' — past live streams are the videos most likely
    to have chat replay (acceptance requires at least one chat source).
    Falls back to the 'videos' tab when the requested tab is empty.
    """
    entries: list[dict] = []
    for t in (tab, "videos") if tab != "videos" else ("videos",):
        url = channel_url.rstrip("/")
        if t:
            url += f"/{t}"
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": False,
            "extract_flat": True,
            "socket_timeout": 30,
            **_ytdlp_engine_opts(),
        }
        with guarded_youtube_dl_channel(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        for e in info.get("entries") or []:
            if not e.get("id"):
                continue
            entries.append({
                "id": str(e["id"]),
                "title": str(e.get("title") or ""),
                "duration": _float_or_none(e.get("duration")),
            })
        if entries:
            break
    return entries[:limit]


# --- module-level self-checks (repo convention; offline) ---------------------

assert _utc_date("2026-08-01T22:30:00+00:00") == "2026-08-01"
assert _utc_date("20260731") == "2026-07-31"
assert _utc_date(1785507647) == "2026-07-31"
assert _utc_date(None) is None
assert _canonical_key("Watchparty do Mundial!", "2026-08-01T22:30:00Z") == \
    "watchparty-do-mundial|2026-08-01"
assert _canonical_key("Último dia do Mundial!", "2026-08-01T22:30:00Z") == \
    "ultimo-dia-do-mundial|2026-08-01"
assert _canonical_key("  A--B!! C?  ", "2026-01-02T00:00:00+00:00") == "a-b-c|2026-01-02"
assert _canonical_key("No date here", None) == "no-date-here"
assert _canonical_key("!!!", "2026-01-02T00:00:00+00:00") == "untitled|2026-01-02"

_vtt_sample = (
    "WEBVTT\nKind: captions\nLanguage: pt\n\n"
    "00:00:03.000 --> 00:00:20.470 align:start position:0%\n"
    " \nNão<00:00:03.199><c> sei.</c>\n\n"
    "00:00:20.470 --> 00:00:20.480 align:start position:0%\n \n \n\n"
    "00:00:20.480 --> 00:00:46.830 align:start position:0%\n \nIh.\n"
)
_segs = _parse_vtt(_vtt_sample)
assert len(_segs) == 2, f"filler cues must be dropped, got {len(_segs)}"
assert _segs[0]["start_sec"] == 3.0 and _segs[0]["end_sec"] == 20.47
assert _segs[0]["text"] == "Não sei."
assert _segs[0]["words"] == [{"word": "sei.", "start": 3.199, "end": 20.47}]
assert _segs[1]["text"] == "Ih."

_lc_sample = (
    '{"replayChatItemAction": {"videoOffsetTimeMsec": "1234", "actions": ['
    '{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {'
    '"message": {"runs": [{"text": "oi "}, {"text": "titi", "emoji": '
    '{"emojiId": "x1", "shortcuts": [":titi:"]}}]}, '
    '"authorName": {"simpleText": "@fulano"}, "authorExternalChannelId": "UC1", '
    '"timestampUsec": "1785453737783005", '
    '"authorBadges": [{"liveChatAuthorBadgeRenderer": {"tooltip": "Owner"}}]}}}}]}}\n'
)
_rows = _parse_live_chat(_lc_sample)
assert len(_rows) == 1
assert _rows[0]["offset_sec"] == 1.234
assert _rows[0]["username"] == "@fulano"
assert _rows[0]["text"] == "oi titi"
assert _rows[0]["badges"] == ["Owner"]
assert _rows[0]["emotes"] == [{"id": "x1", "text": ":titi:"}]
assert _rows[0]["ts"] == "2026-07-30T23:22:17+00:00"
