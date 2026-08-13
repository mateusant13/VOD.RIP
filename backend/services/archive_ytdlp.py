"""YouTube ingestion adapter for the local archive ("local Google").

Fetches metadata, auto captions (VTT), and live-chat replay (NDJSON) for a
YouTube video and writes them through archive_db. Channel walk uses the flat
playlist extractor so the whole channel is one lightweight call.

Live chat is best-effort: only streams (live_status == 'was_live') have chat
replay; VODs yield zero chat rows and report chat: 'none'. Captions come from
YouTube's auto-generated tracks — the primary language (pt preferred, then en,
then any) plus a secondary of the other family (pt/en) when both exist.

All offsets written to the archive are float seconds into the stream
(archive contract). Chat offset uses yt-dlp's replay fragment
videoOffsetTimeMsec (already stream-relative), not the wall-clock
timestampUsec.
"""
from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
import tempfile
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services import archive_db, transcript_fix
from services.chat_sinks.yt_live import _base_usec_from_info
from services.ytdlp_ffmpeg import _ytdlp_engine_opts
from services.ytdlp_guard import guarded_youtube_dl, guarded_youtube_dl_channel

logger = logging.getLogger(__name__)

PLATFORM = "youtube"
# Priority rule: YouTube > Twitch > Kick (lower number wins).
_CAPTION_LANG_PREF = ("pt", "pt-br", "en", "en-orig")
# Payload fallback order: VTT (word timestamps) -> json3 -> srv3 (XML). The
# timedtext API has been observed to rate-limit the VTT endpoint (HTTP 429)
# while json3/srv3 still serve.
_CAPTION_FMTS = ("vtt", "json3", "srv3")
_CAPTION_RETRIES = 2
_CAPTION_BACKOFF_S = 1.0

_TS_RE = re.compile(r"^(\d{8})$")
_CUE_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})"
)
_WORD_TS_RE = re.compile(r"<(\d{2}):(\d{2}):(\d{2})\.(\d{3})><c>([^<]*)</c>")
_TAG_RE = re.compile(r"<[^>]+>")
# YouTube ASR inserts a speaker-turn marker at caption speaker changes. It
# arrives HTML-escaped ("&gt;&gt;") in the raw payloads; strip both forms.
_TURN_MARKER_RE = re.compile(r"(?:&gt;|>){2,}")


def _strip_turn_markers(text: str) -> str:
    """Remove YouTube ASR speaker-turn markers (">>" / "&gt;&gt;") from caption
    text and collapse the whitespace they leave behind.

    The marker is a cue artifact (speaker change), never spoken content, so it
    must not reach the transcripts table or the preview subtitles panel. A cue
    that contained only markers collapses to '' and is dropped by callers."""
    cleaned = _TURN_MARKER_RE.sub(" ", text)
    return re.sub(r"\s+", " ", cleaned).strip()


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

def _clean_caption_text(raw: str) -> str:
    """Raw caption text -> stored transcript text (source-level cleaning).

    YouTube timedtext ships the caption text XML-escaped, so an ASR speaker
    change marker arrives as '&gt;&gt;' (and a spoken '&' as '&amp;'); the
    json3/srv3 fallback payloads can carry the same marker raw. Unescape
    first, then drop every '>>' marker (the ASR emits it to flag speaker
    changes, often mid-segment) so transcripts read as plain speech. Words
    are cleaned through the same pass.
    ponytail: a '>>' that is genuinely part of the spoken text (possible in
    manual tracks) is dropped too — the marker cannot be told apart from
    it, and it has no search value.
    """
    text = html.unescape(raw)
    return re.sub(r"\s*>{2,}\s*", " ", text).strip()


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
        cleaned = _clean_caption_text(_TAG_RE.sub("", raw).replace("\n", " "))
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
        word = _clean_caption_text(m.group(5))
        if not word:
            continue
        out.append({"word": word, "start": round(start, 3), "end": round(cue_end, 3)})
    for j in range(len(out) - 1, 0, -1):
        out[j - 1]["end"] = out[j]["start"]
    return out


def _parse_json3(text: str) -> list[dict]:
    """Convert a json3 caption document (timedtext ?fmt=json3) to segments.

    Same shape as _parse_vtt: {seg_idx, start_sec, end_sec, text, words}.
    Events carry absolute tStartMs/dDurationMs (observed: same timeline as
    srv3's <p t=...>); per-segment tOffsetMs become word timestamps. Events
    without a duration run to the next event's start (+2s fallback).
    """
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    events = doc.get("events") or [] if isinstance(doc, dict) else []
    starts = []
    for ev in events:
        t = ev.get("tStartMs")
        starts.append(float(t) / 1000.0 if t is not None else None)
    segments: list[dict] = []
    idx = 0
    for i, ev in enumerate(events):
        start = starts[i]
        if start is None:
            continue
        dur = ev.get("dDurationMs")
        if dur is not None:
            end = start + float(dur) / 1000.0
        else:
            end = next((s for s in starts[i + 1:] if s is not None), start + 2.0)
        segs = ev.get("segs") or []
        text = _clean_caption_text("".join(str(s.get("utf8", "")) for s in segs))
        if not text:
            continue
        words = []
        for s in segs:
            off = s.get("tOffsetMs")
            if off is None:
                continue
            w = _clean_caption_text(str(s.get("utf8", "")))
            if not w:
                continue
            words.append({
                "word": w,
                "start": round(start + float(off) / 1000.0, 3),
                "end": round(end, 3),
            })
        for j in range(len(words) - 1, 0, -1):
            words[j - 1]["end"] = words[j]["start"]
        segments.append({
            "seg_idx": idx,
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
            "text": text,
            "words": words,
        })
        idx += 1
    return segments


def _parse_srv3(text: str) -> list[dict]:
    """Convert a srv3 (timedtext XML) caption document to segments.

    <p t=... d=...> paragraphs with optional <s t=...> word spans. Observed
    on real tracks: t is absolute milliseconds on the same timeline as
    json3's tStartMs (verified against a live pt auto-caption track), and
    the a="1" attribute marks karaoke continuation paragraphs (empty filler)
    rather than a different clock. ponytail: if a track ever carries true
    relative offsets, the upgrade is cumulative accumulation — none of the
    formats we see today need it.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    segments: list[dict] = []
    idx = 0
    for p in root.iter("p"):
        t = p.get("t")
        if t is None:
            continue
        start = float(t) / 1000.0
        d = p.get("d")
        end = start + (float(d) / 1000.0 if d else 0.0)
        text = _clean_caption_text("".join(p.itertext()))
        if not text:
            continue
        words = []
        for s in p.iter("s"):
            off = s.get("t")
            if off is None:
                continue
            w = _clean_caption_text("".join(s.itertext()))
            if not w:
                continue
            words.append({
                "word": w,
                "start": round(start + float(off) / 1000.0, 3),
                "end": round(end, 3),
            })
        for j in range(len(words) - 1, 0, -1):
            words[j - 1]["end"] = words[j]["start"]
        segments.append({
            "seg_idx": idx,
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
            "text": text,
            "words": words,
        })
        idx += 1
    return segments


def _parse_caption(fmt: str, data: str) -> list[dict]:
    """Dispatch a caption payload to its parser (vtt is the default)."""
    if fmt == "json3":
        return _parse_json3(data)
    if fmt == "srv3":
        return _parse_srv3(data)
    return _parse_vtt(data)


def _iso_from_usec(usec: Any) -> Optional[str]:
    if not usec:
        return None
    try:
        return datetime.fromtimestamp(int(usec) / 1e6, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    except (TypeError, ValueError, OSError):
        return None


def _text_or_empty(value: Any) -> str:
    """Coerce one raw yt-dlp live-chat renderer field to text (trust boundary).

    Renderer fields are documented strings but some arrive as numbers
    (authorNameTextColor has been observed as a raw packed-ARGB int); a bare
    .strip() on those crashed the whole chat ingest ("'int' object has no
    attribute 'strip'"), dropping every chat row for the video. Numbers
    become their str() form and are then rejected by the downstream format
    checks (e.g. the #RRGGBB color regex), so no row is lost for a shape
    variance."""
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value)


def _parse_live_chat(text: str, base_usec: Optional[float] = None) -> list[dict]:
    """NDJSON from yt-dlp's .live_chat.json -> archive message rows.

    Rows: {offset_sec, user_id, username, text, badges, emotes, ts, color}.
    offset_sec = replay fragment videoOffsetTimeMsec/1000 (stream-relative);
    fragments that lack it (the live-captured early phase) fall back to
    (timestampUsec - base_usec)/1e6 against the stream start. Rows with
    neither anchor are skipped — a raw wall-clock epoch is never a
    VOD-relative offset (it would land a 1.7e9-second row in the middle of
    the chat timeline and break the panel's time mapping).
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
            msg = renderer.get("message")
            runs = msg.get("runs") if isinstance(msg, dict) else None
            runs = runs if isinstance(runs, list) else []
            text = "".join(
                str(run.get("text", "")) if isinstance(run, dict) else ""
                for run in runs
            ).strip()
            if not text:
                continue
            author_name = renderer.get("authorName")
            author = _text_or_empty(
                author_name.get("simpleText") if isinstance(author_name, dict) else None
            )
            user_id = _text_or_empty(renderer.get("authorExternalChannelId")) or None
            usec = renderer.get("timestampUsec")
            if frag_ms is not None:
                offset = float(frag_ms) / 1000.0
            elif base_usec is not None and usec is not None:
                offset = (float(usec) - base_usec) / 1e6
            else:
                continue  # no anchor — cannot produce a VOD-relative offset
            badges = []
            for b in renderer.get("authorBadges") or []:
                badge = b.get("liveChatAuthorBadgeRenderer") if isinstance(b, dict) else None
                tip = _text_or_empty(badge.get("tooltip") if isinstance(badge, dict) else None)
                if tip:
                    badges.append(tip)
            emotes = []
            for run in runs:
                emoji = run.get("emoji") if isinstance(run, dict) else None
                if not isinstance(emoji, dict):
                    continue
                shortcuts = emoji.get("shortcuts")
                emotes.append({
                    "id": _text_or_empty(emoji.get("emojiId")) or None,
                    "text": _text_or_empty(
                        shortcuts[0] if isinstance(shortcuts, list) and shortcuts else None
                    ),
                })
            # YouTube live-chat renderers carry the author's chat color as
            # #RRGGBB (authorNameTextColor); Twitch GQL VOD comments have no
            # equivalent, so twitch rows stay NULL and the UI uses the
            # per-platform palette. Only well-formed hex is stored. Some
            # renderers send it as a raw packed-ARGB int — _text_or_empty
            # coerces it and the hex check below rejects it (NULL).
            color = _text_or_empty(renderer.get("authorNameTextColor"))
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                color = None
            rows.append({
                "offset_sec": round(offset, 3),
                "user_id": user_id,
                "username": author,
                "text": text,
                "badges": badges,
                "emotes": emotes,
                "ts": _iso_from_usec(usec),
                "color": color,
            })
    return rows


# --- extraction -------------------------------------------------------------

def _yt_opts(outdir: Path, *, video_id: Optional[str] = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
        **_ytdlp_engine_opts(),
        # live_chat arrives as a subtitle entry (ext 'json'); with
        # skip_download yt-dlp never writes subtitles, so ingest_video calls
        # ydl._write_subtitles explicitly inside the guarded instance.
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["live_chat"],
        "outtmpl": str(outdir / "%(id)s.%(ext)s"),
    }
    _apply_youtube_session(opts, video_id=video_id)
    return opts


def _apply_youtube_session(opts: dict, *, video_id: Optional[str] = None) -> None:
    """Wire the app's YouTube auth conditioning into yt-dlp opts.

    Mirrors the app (ytdlp_download / live_capture / chat_sinks.yt_live):
    one session from settings — cookies + po_token + visitor_data when
    configured, anonymous bootstrap otherwise — feeds extractor_args and
    the cookiefile. Never assumes cookies: anonymous is the app's own
    fallback, and the archive path works in both modes. Any wiring failure
    degrades to the legacy bare clients instead of dying."""
    try:
        from services.youtube_session import (
            apply_ytdlp_cookie_opts,
            ytdlp_extractor_args,
            youtube_session_from_settings,
        )

        session = youtube_session_from_settings(video_id=video_id)
        opts["extractor_args"] = ytdlp_extractor_args(session)
        apply_ytdlp_cookie_opts(opts, session)
    except Exception as exc:
        logger.warning("youtube auth wiring unavailable — bare yt-dlp fallback: %s", exc)
        opts.setdefault(
            "extractor_args", {"youtube": {"player_client": ["android", "web_safari"]}}
        )


@contextmanager
def _guarded_youtube_dl(outdir: Path, *, video_id: Optional[str] = None):
    """guarded_youtube_dl context that records the YouTube bot gate on failure.

    Gate/rate-limit errors arm the process-wide cooldown (services.yt_gate)
    so the worker stops hammering YouTube and requeues its jobs; the
    exception still propagates so each caller keeps its own fail/requeue
    contract."""
    try:
        with guarded_youtube_dl(_yt_opts(outdir, video_id=video_id)) as ydl:
            yield ydl
    except Exception as exc:
        from services.yt_gate import classify_youtube_gate_error, note_youtube_gate

        if classify_youtube_gate_error(exc):
            note_youtube_gate(str(exc)[:200])
        raise


def _is_gate_error(exc: BaseException) -> bool:
    """True when the extract failure signals the IP-level YouTube gate.

    The canonical classifier (services.yt_gate.classify_youtube_gate_error)
    covers the EN bot-wall spellings ("Sign in to confirm you're not a bot",
    "preview unavailable..."). YouTube's player API also serves the gate as
    a localized transient block — observed live: an entire channel failing
    with "Esse conteúdo não está disponível. Tente de novo mais tarde."
    ("This content is not available. Please try again later.") while the
    yt_gate/warm cooldowns were armed. The 'try again later' family is
    transient by wording (a dead video says "unavailable" with no retry
    hint), so classifying it as gate is safe: worst case one short freeze.
    ponytail: per-locale marker list; upgrade path is a shared classifier in
    yt_gate that maps localized playability reasons once they stabilize."""
    from services.yt_gate import classify_youtube_gate_error

    if classify_youtube_gate_error(exc):
        return True
    msg = (str(exc) or "").lower()
    return (
        ("try again later" in msg or "tente de novo mais tarde" in msg)
        and ("not available" in msg or "não está disponível" in msg)
    )


def _lang_group(lang: str) -> Optional[str]:
    """Language family of a caption track code: 'pt' | 'en' | 'es' | None."""
    base = (lang or "").lower().split("-")[0]
    return base if base in ("pt", "en", "es") else None


def _best_in_group(merged: dict, group: str, fmt: str) -> Optional[tuple[str, str]]:
    """Best (lang, url) track of one language family for a format, or None."""
    for lang in _CAPTION_LANG_PREF:
        if _lang_group(lang) != group:
            continue
        for entry in merged.get(lang) or []:
            if entry.get("ext") == fmt and entry.get("url"):
                return lang, entry["url"]
    for lang, entries in merged.items():
        if _lang_group(lang) != group or lang in _CAPTION_LANG_PREF:
            continue
        for entry in entries:
            if entry.get("ext") == fmt and entry.get("url"):
                return lang, entry["url"]
    return None


def _pick_captions_for(info: dict, fmt: str, *, family: Optional[str] = None) -> list[tuple[str, str]]:
    """Best auto-caption tracks for one format: [primary, secondary?].

    Primary keeps the old rule (pt > pt-br > en > first). Secondary is the
    best track of the OTHER language family (pt vs en) when both exist, so a
    bilingual video stores both transcripts. Single-family videos yield one
    element; nothing serving this format yields [].

    family: restrict picks to ONE language family ('pt'/'en'/'es') — the
    channel's effective language, so a channel known to be PT stores only pt
    captions (the old rule stored pt AND en rows for the same segment).
    None keeps the legacy both-families rule."""
    ac = info.get("automatic_captions") or {}
    subs = info.get("subtitles") or {}
    merged = dict(ac)
    for k, v in subs.items():
        merged.setdefault(k, v)
    if family:
        best = _best_in_group(merged, family, fmt)
        return [best] if best else []
    out: list[tuple[str, str]] = []
    for lang in _CAPTION_LANG_PREF:
        for entry in merged.get(lang) or []:
            if entry.get("ext") == fmt and entry.get("url"):
                out.append((lang, entry["url"]))
                break
        if out:
            break
    if not out:
        for lang, entries in merged.items():
            if lang == "live_chat":
                continue
            for entry in entries:
                if entry.get("ext") == fmt and entry.get("url"):
                    out.append((lang, entry["url"]))
                    break
            if out:
                break
    if out:
        group = _lang_group(out[0][0])
        if group in ("pt", "en"):
            second = _best_in_group(merged, "en" if group == "pt" else "pt", fmt)
            if second and second[0] != out[0][0]:
                out.append(second)
    return out


def _pick_caption_for(info: dict, fmt: str) -> tuple[Optional[str], Optional[str]]:
    """Best auto-caption track for one format (primary only)."""
    picks = _pick_captions_for(info, fmt)
    return (picks[0][0], picks[0][1]) if picks else (None, None)


def _fetch_caption(ydl: Any, info: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Fetch the best caption track with format fallback (vtt -> json3 -> srv3).

    Each format gets up to _CAPTION_RETRIES attempts with a 1s backoff on
    HTTP 429 (rate limiting); any failure falls through to the next format.
    Returns (lang, fmt, payload) or (None, None, None) when nothing served.
    ponytail: a real retry policy (exponential backoff + jitter, per-format)
    belongs in a shared HTTP client, not in this adapter.
    """
    for fmt in _CAPTION_FMTS:
        lang, url = _pick_caption_for(info, fmt)
        if not url:
            continue
        for attempt in range(_CAPTION_RETRIES):
            try:
                data = ydl.urlopen(url).read().decode("utf-8", "replace")
                return lang, fmt, data
            except Exception as exc:
                if getattr(exc, "code", None) == 429 and attempt < _CAPTION_RETRIES - 1:
                    time.sleep(_CAPTION_BACKOFF_S)
                    continue
                logger.warning(
                    "caption fetch %s (%s) failed for %s: %s",
                    fmt, lang, info.get("id"), exc,
                )
                break  # next format
    return None, None, None


def _fetch_captions(ydl: Any, info: dict, *, family: Optional[str] = None) -> list[tuple[str, str, str]]:
    """Fetch every picked caption track (primary + secondary family).

    Same policy as _fetch_caption (vtt -> json3 -> srv3, 429 retry/backoff)
    applied per track; one track per language family. family restricts the
    picks to one language family (see _pick_captions_for). Returns
    [(lang, fmt, payload), ...] — empty when nothing served."""
    out: list[tuple[str, str, str]] = []
    for fmt in _CAPTION_FMTS:
        for lang, url in _pick_captions_for(info, fmt, family=family):
            if any(existing_lang == lang for existing_lang, _, _ in out):
                continue  # one track per language family is enough
            for attempt in range(_CAPTION_RETRIES):
                try:
                    data = ydl.urlopen(url).read().decode("utf-8", "replace")
                    out.append((lang, fmt, data))
                    break
                except Exception as exc:
                    if getattr(exc, "code", None) == 429 and attempt < _CAPTION_RETRIES - 1:
                        time.sleep(_CAPTION_BACKOFF_S)
                        continue
                    logger.warning(
                        "caption fetch %s (%s) failed for %s: %s",
                        fmt, lang, info.get("id"), exc,
                    )
                    break  # next track/format
        if len(out) >= 2:
            break
    return out


def _channel_effective_language(channel: str) -> Optional[str]:
    """Effective language family for a YouTube channel (None = unknown).

    Mirrors the channels-router read (WS-3, routers/channels.py): the
    multi-signal aggregation with per-channel settings overrides (override >
    original-language consensus > platform clue > transcript tally). Used to
    restrict caption ingest to the channel's language so a known-PT channel
    never stores en AND pt rows for the same segment; unknown channels keep
    the legacy all-families rule."""
    if not (channel or "").strip():
        return None
    from services.channel_language import aggregate_channel_language

    try:
        from deps import settings_mgr  # lazy: keeps module import light

        overrides = getattr(settings_mgr.get(), "channel_asr_languages", None) or {}
    except Exception:
        overrides = {}
    result = aggregate_channel_language(PLATFORM, channel, overrides=overrides)
    return result.get("language")


def _video_url(id_or_url: str) -> str:
    if "://" in id_or_url:
        return id_or_url
    return f"https://www.youtube.com/watch?v={id_or_url.strip()}"


def _video_id_from_url(url: str, fallback: str) -> str:
    m = re.search(r"[?&]v=([\w-]{6,})", url)
    if not m:
        m = re.search(r"youtu\.be/([\w-]{6,})", url)
    return m.group(1) if m else fallback


def _fetch_live_chat(ydl: Any, info: dict, video_id: str) -> tuple[int, str, Optional[str]]:
    """Best-effort live-chat replay fetch -> (count, status, error).

    status: 'replay' | 'none' | 'error'. Never raises; failures are
    reported so callers can log and continue (chat is best-effort)."""
    try:
        chat_file = None
        sub_base = ydl.prepare_filename(info, "subtitle")
        written = ydl._write_subtitles(info, sub_base)
        for sub, _final in written or []:
            if str(sub).endswith(".live_chat.json"):
                chat_file = Path(sub)
                break
        if chat_file and chat_file.is_file():
            rows = _parse_live_chat(
                chat_file.read_text(encoding="utf-8"),
                base_usec=_base_usec_from_info(info),
            )
            if rows:
                _db_write(archive_db.insert_messages, PLATFORM, video_id, rows)
                return len(rows), "replay", None
            return 0, "none", None  # replay exists but is empty
        return 0, "none", None
    except Exception as exc:
        logger.warning("live chat failed for %s: %s", video_id, exc)
        return 0, "error", str(exc)


def ingest_video(id_or_url: str, *, temp_dir: Optional[Path] = None) -> dict:
    """Ingest one YouTube video: metadata + captions.

    Chat history is NOT fetched inline anymore — the scheduler enqueues a
    kind='chat' job right after a successful ingest and the archive worker
    (detached, supervised) fetches it via backfill_live_chat. This keeps
    ingest cheap and makes chat survive app close + crashes.

    Returns a report dict (video_id, title, channel, started_at,
    duration_sec, canonical_key, caption_lang, transcript_segments,
    chat_messages: 0, chat: 'none'). Raises on metadata extraction failure;
    caption failures are reported, not raised.
    """
    url = _video_url(id_or_url)
    video_id = _video_id_from_url(url, id_or_url.strip())
    # Stable job id: the scheduler re-attempts a failed ingest on its own
    # (kind='ingest' rows are visibility-only — the worker never claims
    # them), so a per-attempt timestamped id piled one permanent 'retrying'
    # row into the jobs panel per attempt. Reusing one row per video keeps
    # the panel truthful: attempts/error/deadline update in place and a
    # successful re-ingest flips the same row to done.
    job_id = f"yt-ingest-{video_id}"
    try:
        _db_write(archive_db.enqueue_job, job_id, "ingest", PLATFORM, video_id, priority=0)
    except sqlite3.IntegrityError:
        pass  # row from an earlier attempt — reuse it in place
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
        with _guarded_youtube_dl(outdir, video_id=video_id) as ydl:
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
            # Idempotent re-ingest: drop re-derivable rows (transcripts,
            # aliases) first; fetched chat is preserved (see _clear_video_data).
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

            # Auto captions -> transcript segments (vtt -> json3 -> srv3
            # fallback; failures stay non-fatal, reported as caption_error).
            # Channel-language-aware: a known family stores ONLY that family
            # (the old rule stored pt AND en rows for the same segment);
            # unknown channels keep the legacy both-families rule.
            eff_lang = _channel_effective_language(channel)
            try:
                tracks = _fetch_captions(
                    ydl, info,
                    family=eff_lang if eff_lang in ("pt", "en", "es") else None,
                )
                if tracks:
                    report["caption_lang"] = tracks[0][0]
                for lang, fmt, data in tracks:
                    segments = _parse_caption(fmt, data)
                    if segments:
                        # Champion-name post-fix: STRONG path only (engine
                        # "captions" is never weak-path eligible) and only on
                        # segments whose words reconstruct the text (no inline
                        # timestamps -> skipped inside fix_segment), so the
                        # join(words) == text contract is preserved.
                        fix_stats = transcript_fix.new_stats()
                        if transcript_fix.enabled():
                            for seg in segments:
                                transcript_fix.fix_segment(
                                    seg, engine="captions", language=lang,
                                    stats=fix_stats,
                                )
                            report["transcript_fix"] = fix_stats
                        _db_write(
                            archive_db.insert_transcript,
                            PLATFORM, video_id, segments,
                            lang=lang,
                        )
                        report["transcript_segments"] += len(segments)
            except Exception as exc:
                logger.warning("captions failed for %s: %s", video_id, exc)
                report["caption_error"] = str(exc)

            # Persistent no-captions verdict: an ingest that stored zero
            # caption segments stamps videos.captions_unavailable_at so the
            # scheduler stops re-extracting the video every pass/boot (the
            # in-memory 1h backoff dies with the process); a later ingest
            # that DID store segments clears the marker, making the video
            # a re-extract candidate again. A caption fetch error stamps
            # too — nothing was stored, and the marker only delays the
            # next attempt by CAPTIONS_UNAVAILABLE_FRESH_S.
            if report["transcript_segments"] > 0:
                _db_write(archive_db.clear_captions_unavailable, PLATFORM, video_id)
                # Race: a transcribe job may have been queued before the
                # captions landed (search/preview kick, an earlier scheduler
                # pass). Captions now serve as the transcript — resolve the
                # job as done so no ASR work is spent on the video.
                _db_write(
                    archive_db.execute,
                    "UPDATE archive_jobs SET status='done', progress=1.0 "
                    "WHERE id=? AND status IN ('queued','running')",
                    (f"transcribe-youtube-{video_id}",),
                )
            else:
                _db_write(archive_db.mark_captions_unavailable, PLATFORM, video_id)

        try:
            _db_write(archive_db.update_job, job_id, status="done", progress=1.0)
        except Exception as exc:  # job bookkeeping must not fail the ingest
            logger.warning("job status update failed for %s: %s", video_id, exc)
    except Exception as exc:
        try:
            # Truthful job error: the old generic 'extract error' hid the
            # real cause (bot-gate / DRM / dead video) AND kept the TASK10
            # requeue on the plain exponential curve — the gate-aware rate
            # path keys off the error text, and 'extract error' alone never
            # matched it. A gate-classified failure names the gate so the
            # retry deadline tracks the freeze instead of hot-retrying.
            if _is_gate_error(exc):
                from services.yt_gate import note_youtube_gate

                note_youtube_gate(str(exc)[:200])
                error = "extract error: YouTube bot-gate active — retrying after it clears"
            else:
                error = f"extract error: {exc}"
            _db_write(archive_db.update_job, job_id, status="failed", error=error)
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


# --- transcribe-time audio download ----------------------------------------

def download_bestaudio(video_id: str, outdir: Path) -> Path:
    """Download the best audio track of one video into outdir.

    Reuses the app's YouTube auth conditioning (cookies + po_token +
    visitor_data, anonymous bootstrap fallback) and the bot-gate-aware
    guard: a gate/rate-limit failure arms the process-wide cooldown
    (services.yt_gate) and propagates so the transcribe worker requeues
    the job instead of failing it. Returns the downloaded media file
    (webm/m4a — decode_audio handles either)."""
    from services.ytdlp_guard import guarded_youtube_dl

    url = _video_url(video_id)
    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "outtmpl": str(outdir / "%(id)s.%(ext)s"),
        **_ytdlp_engine_opts(),
    }
    _apply_youtube_session(opts, video_id=video_id)
    try:
        with guarded_youtube_dl(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as exc:
        from services.yt_gate import classify_youtube_gate_error, note_youtube_gate

        if classify_youtube_gate_error(exc) or _is_gate_error(exc):
            note_youtube_gate(str(exc)[:200])
        raise
    files = [f for f in outdir.iterdir() if f.is_file()]
    if not files:
        raise RuntimeError(f"yt-dlp produced no audio for {video_id}")
    return max(files, key=lambda f: f.stat().st_size)


def _is_permanent_download_error(exc: BaseException) -> bool:
    """True when the audio download failure can never resolve.

    DRM-protected, age-gated, deleted, private and geo-blocked videos
    return the same error on every attempt — retrying them only hammers
    YouTube. The worker marks such videos transcript_kind='blocked'
    (terminal) instead of letting the retry machinery re-enqueue forever.
    Gate markers ('not a bot' etc.) are NOT permanent — the caller checks
    those first. Transient network errors are not listed either: they flow
    through the normal backoff."""
    msg = (str(exc) or "").lower()
    return any(
        m in msg
        for m in (
            "drm",
            "protected by drm",
            "sign in to confirm your age",
            "age-restricted",
            "age restricted",
            "confirm your age",
            "this video is unavailable",
            "video unavailable",
            "this video is private",
            "private video",
            "has been removed",
            "was removed",
            "removed by the uploader",
            "not available in your country",
            "geo-restricted",
            "geo blocked",
            "geo-blocked",
            "playback on other websites",
            "members-only",
            "premium content",
        )
    )


def backfill_live_chat(video_id: str) -> dict:
    """Retro chat-only backfill for one already-ingested YouTube video.

    Refetches live-chat replay for videos whose chat ingest crashed
    historically (the authorNameTextColor int bug dropped every chat row)
    but whose captions already archived — the scheduler's covered-skip
    would never re-ingest them. Chat-only: no caption re-fetch (they exist
    already; re-fetching adds YouTube API pressure for nothing).

    Returns report {video_id, chat_messages, chat: 'replay'|'none'|'error'}.
    Raises on metadata extraction failure; chat failures are reported, not
    raised (same contract as ingest_video)."""
    url = _video_url(video_id)
    vid = _video_id_from_url(url, video_id.strip())
    report: dict = {
        "video_id": vid,
        "chat_messages": 0,
        "chat": "none",
        "chat_error": None,
    }
    outdir = Path(tempfile.mkdtemp(prefix=f"yt-chat-{vid}-"))
    try:
        with _guarded_youtube_dl(outdir, video_id=vid) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise ValueError(f"no extract info for {url}")
            if info.get("id"):
                vid = str(info["id"])
            report["video_id"] = vid
            if info.get("live_status") in ("was_live", "is_live"):
                n, status, err = _fetch_live_chat(ydl, info, vid)
                report["chat_messages"] = n
                report["chat"] = status
                report["chat_error"] = err
    finally:
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
    """Drop re-derivable rows for one video before a re-ingest.

    Transcripts + aliases are re-derived from the fresh extract and must be
    wiped so the re-ingest stores clean rows. Messages are PRESERVED when
    the video already has chat: wiping them would make the scheduler's chat
    leg (has_chat gate) refetch everything on every pass — a refetch loop
    that re-pays the whole replay fetch each re-ingest. A video with no
    chat rows loses nothing (DELETE on an empty set is a no-op). FTS5
    index entries cascade via the AFTER DELETE triggers on the content
    tables (external-content FTS owns no row data)."""
    conn = archive_db.get_conn()
    with conn:
        if not archive_db.has_chat(PLATFORM, video_id):
            conn.execute(
                "DELETE FROM messages WHERE platform=? AND video_id=?", (PLATFORM, video_id)
            )
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


# --- WS-4 original-title backfill -------------------------------------------

# Never hammer YouTube: a min gap between fetches + a cooldown for videos
# that failed or yielded no language clue. The cooldown is persisted in
# videos.original_fetch_failed_at (survives restarts AND coordinates the
# daemon sweep with the router sync in the same process); the in-memory
# dict is kept as a fast-path cache so a same-process retry never pays a
# SQL read.
_ORIGINAL_MIN_GAP_S = 1.5
_ORIGINAL_FAIL_COOLDOWN_S = 3600.0
_original_last_fetch = 0.0
_original_failed_at: dict[str, float] = {}
_original_throttle_lock = threading.Lock()
# The language the hl-free player response serves on this machine (see
# innertube_original_meta's decision comment). The player title is the
# ORIGINAL title only when it matches the video's original language; for
# other original languages the title is an auto-translation.
_SERVING_LANG = "pt"
_VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")


def _mark_original_failed(video_id: str) -> None:
    now = time.monotonic()
    with _original_throttle_lock:
        _original_failed_at[video_id] = now
        if len(_original_failed_at) > 4096:
            _original_failed_at.pop(next(iter(_original_failed_at)), None)
    # Durable stamp: the cooldown must survive restarts (a permanently
    # failing video is otherwise re-fetched once per app restart forever).
    # Best-effort — a DB hiccup must not crash the backfill sweep.
    try:
        archive_db.execute(
            "UPDATE videos SET original_fetch_failed_at = ? "
            "WHERE platform='youtube' AND video_id=?",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), video_id),
        )
    except Exception:  # noqa: BLE001 — persistence must never break the sweep
        logger.debug("original-failed stamp failed for %s", video_id, exc_info=True)


def _original_failed_recently(video_id: str) -> bool:
    with _original_throttle_lock:
        at = _original_failed_at.get(video_id)
        if at is not None and (time.monotonic() - at) < _ORIGINAL_FAIL_COOLDOWN_S:
            return True  # fast path — stamped in this process
    # Durable check: a cooldown stamped by an earlier process (or an older
    # pass) is honored too. SQL compare, not a dict lookup.
    try:
        row = archive_db.query(
            "SELECT original_fetch_failed_at FROM videos "
            "WHERE platform='youtube' AND video_id=?",
            (video_id,),
        )
    except Exception:  # noqa: BLE001 — read must never break the sweep
        return False
    if not row or not row[0]["original_fetch_failed_at"]:
        return False
    try:
        failed = datetime.fromisoformat(row[0]["original_fetch_failed_at"])
    except (TypeError, ValueError):
        return False
    fresh = (datetime.now(timezone.utc) - failed).total_seconds() < _ORIGINAL_FAIL_COOLDOWN_S
    if fresh:
        # Prime the fast-path cache so a same-process retry skips the SQL.
        with _original_throttle_lock:
            _original_failed_at.setdefault(video_id, time.monotonic())
    return fresh


def _original_throttle_wait() -> None:
    """Block until the min gap since the last fetch has elapsed."""
    with _original_throttle_lock:
        global _original_last_fetch
        now = time.monotonic()
        remaining = _ORIGINAL_MIN_GAP_S - (now - _original_last_fetch)
        _original_last_fetch = now
    if remaining > 0:
        time.sleep(remaining)


def backfill_original_titles(channel: str, *, limit: int = 20) -> dict:
    """Fetch original (non-auto-translated) titles for a channel's YouTube rows.

    Wired into the channel sync path AFTER the walk: every sync fills rows
    that still lack videos.original_title (new rows at ingest and legacy
    rows alike), throttled (min gap between fetches, recently-failed videos
    skipped) so the backfill never hammers YouTube.

    Per-video source: innertube_original_meta (hl-free player fetch — see
    its decision comment). original_title/original_language are stored with
    set_original_title; the stored `title` is never touched. original_language
    is the WS-3 channel-detection clue (column contract: videos.original_language).

    Returns a report dict {candidates, fetched, skipped, no_language}."""
    from services.youtube_innertube import innertube_original_meta

    candidates = archive_db.videos_missing_original_title(PLATFORM, channel, limit)
    report = {"candidates": len(candidates), "fetched": 0, "skipped": 0, "no_language": 0}
    for row in candidates:
        video_id = str(row.get("video_id") or "")
        # Only real 11-char YouTube ids reach the network (flat-playlist
        # fakes / synthetic watchdog rows never do).
        if not _VIDEO_ID_RE.fullmatch(video_id) or _original_failed_recently(video_id):
            report["skipped"] += 1
            continue
        _original_throttle_wait()
        try:
            meta = innertube_original_meta(video_id)
        except Exception as exc:
            logger.debug("original-title backfill fetch failed %s: %s", video_id, exc)
            meta = None
        if not meta or not meta.get("title"):
            _mark_original_failed(video_id)
            report["skipped"] += 1
            continue
        lang = meta.get("language")
        if not lang:
            # No language clue (no caption tracks: age-gated/member-only) —
            # the player title may be a translation, so keep original_title
            # empty rather than store a wrong "original". Retried after the
            # cooldown.
            _mark_original_failed(video_id)
            report["no_language"] += 1
            continue
        if lang == _SERVING_LANG:
            original_title = meta["title"]
        elif lang == "en":
            # The walk's default hl is en, so the stored title IS the en
            # original (verified: browse lang=en matches stored EN titles).
            original_title = str(row.get("title") or "")
        else:
            # Neither source serves this language (e.g. de on a pt/en box) —
            # record the language as a clue, leave the title alone.
            original_title = None
        _db_write(
            archive_db.set_original_title, PLATFORM, video_id,
            original_title, lang,
        )
        report["fetched"] += 1
    return report


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

# YouTube ASR speaker-turn markers ("&gt;&gt;" / ">>") must never reach segment
# text: stripped at parse time, whitespace collapsed, marker-only cues dropped.
_vtt_markers = (
    "WEBVTT\nKind: captions\nLanguage: pt\n\n"
    "00:00:00.000 --> 00:00:04.000 align:start position:0%\n"
    " \n&gt;&gt; E aí maranguap.\n\n"
    "00:00:04.000 --> 00:00:08.000 align:start position:0%\n"
    " \nvelho. &gt;&gt; Nossa, &gt;&gt; suporte.\n\n"
    "00:00:08.000 --> 00:00:09.000 align:start position:0%\n"
    " \n&gt;&gt;\n"
)
_segs_m = _parse_vtt(_vtt_markers)
assert len(_segs_m) == 2, f"marker-only cue must be dropped, got {len(_segs_m)}"
assert _segs_m[0]["text"] == "E aí maranguap."
assert _segs_m[1]["text"] == "velho. Nossa, suporte."
_json3_markers = (
    '{"events": [{"tStartMs": 0, "dDurationMs": 4000, "segs": ['
    '{"utf8": "&gt;&gt; "}, {"utf8": "E aí ", "tOffsetMs": 0},'
    '{"utf8": "maranguap", "tOffsetMs": 500}]}]}'
)
_jm = _parse_json3(_json3_markers)
assert _jm[0]["text"] == "E aí maranguap", _jm[0]["text"]
assert _parse_json3(
    '{"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "&gt;&gt;"}]}]}'
) == [], "a marker-only json3 event must be dropped"
_srv3_markers = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<timedtext format="3"><body>'
    '<p t="0" d="4000">&gt;&gt; oi <s t="200">tudo</s> bem</p>'
    '</body></timedtext>'
)
_sm = _parse_srv3(_srv3_markers)
assert _sm[0]["text"] == "oi tudo bem", _sm[0]["text"]


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

# No videoOffsetTimeMsec: anchored against the stream start (base_usec)…
_lc_no_frag = (
    '{"replayChatItemAction": {"actions": ['
    '{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {'
    '"message": {"runs": [{"text": "sem offset"}]}, '
    '"authorName": {"simpleText": "@ancla"}, "timestampUsec": "1785453743783005"'
    '}}}}]}}\n'
)
_rows2 = _parse_live_chat(_lc_no_frag, base_usec=1785453737783005)
assert len(_rows2) == 1, _rows2
assert abs(_rows2[0]["offset_sec"] - 6.0) < 1e-9, _rows2[0]["offset_sec"]
# …and SKIPPED when no anchor exists (an epoch wall-clock is never an offset).
assert _parse_live_chat(_lc_no_frag) == []

# Trust boundary: renderer fields are documented strings but some arrive as
# numbers (authorNameTextColor as a raw packed-ARGB int crashed the whole
# ingest with "'int' object has no attribute 'strip'").
_lc_int_color = (
    '{"replayChatItemAction": {"videoOffsetTimeMsec": "1500", "actions": ['
    '{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {'
    '"message": {"runs": [{"text": "cor numerica"}]}, '
    '"authorName": {"simpleText": "@numerico"}, '
    '"authorExternalChannelId": "UC2", "timestampUsec": "1785453737783005", '
    '"authorNameTextColor": 3019898879}}}}]}}\n'
)
_rows_int = _parse_live_chat(_lc_int_color)
assert len(_rows_int) == 1, _rows_int
assert _rows_int[0]["offset_sec"] == 1.5
assert _rows_int[0]["username"] == "@numerico"
assert _rows_int[0]["color"] is None, "an int color is not well-formed hex -> NULL"

_json3_sample = (
    '{"events": ['
    '{"tStartMs": 0, "dDurationMs": 211879, "segs": [{"utf8": ""}]},'
    '{"tStartMs": 18800, "dDurationMs": 7160, "segs": ['
    '{"utf8": "Não ", "tOffsetMs": 0},'
    '{"utf8": "somos ", "tOffsetMs": 346},'
    '{"utf8": "estranhos", "tOffsetMs": 692}]},'
    '{"tStartMs": 25960, "dDurationMs": 3000, "segs": [{"utf8": " "}]}'
    ']}'
)
_j3 = _parse_json3(_json3_sample)
assert len(_j3) == 1, f"empty/filler events must be dropped, got {len(_j3)}"
assert _j3[0]["start_sec"] == 18.8 and _j3[0]["end_sec"] == 25.96
assert _j3[0]["text"] == "Não somos estranhos"
assert _j3[0]["words"] == [
    {"word": "Não", "start": 18.8, "end": 19.146},
    {"word": "somos", "start": 19.146, "end": 19.492},
    {"word": "estranhos", "start": 19.492, "end": 25.96},
]
assert _parse_json3("not json") == []
assert _parse_json3("42") == []

_srv3_sample = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<timedtext format="3"><body>'
    '<p t="18800" d="7160"><s ac="0">Não </s><s t="346" ac="0">somos </s>'
    '<s t="692" ac="0">estranhos</s></p>'
    '<p t="25960" w="1" a="1">\n</p>'
    '<p t="25960" d="3000">Ih.</p>'
    '</body></timedtext>'
)
_s3 = _parse_srv3(_srv3_sample)
assert len(_s3) == 2, f"empty continuation paragraphs must be dropped, got {len(_s3)}"
assert _s3[0]["start_sec"] == 18.8 and _s3[0]["end_sec"] == 25.96
assert _s3[0]["text"] == "Não somos estranhos"
assert _s3[0]["words"][0] == {"word": "somos", "start": 19.146, "end": 19.492}
assert _s3[1]["text"] == "Ih." and _s3[1]["start_sec"] == 25.96
assert _parse_srv3("<broken") == []
assert _parse_caption("json3", _json3_sample) == _j3
assert _parse_caption("srv3", _srv3_sample) == _s3
assert _parse_caption("vtt", _vtt_sample) == _segs


def resolve_youtube_display_names(limit: int = 20) -> int:
    """Resolve UC channel ids → display names for youtube chat rows.

    YouTube live-chat payloads only carry the @handle; the name viewers see
    is the author channel's display title. One yt-dlp channel extract per
    distinct UC id (bounded by the caller's throttle), resolved names cached
    in messages.display_name so re-runs skip known ids. Bot-walled channels
    (503) stay NULL and are retried on a later run — the USER search filter
    falls back to the @-stripped handle meanwhile.

    Uses the channel-dedicated lock so preview segment yt-dlp can't starve.
    """
    ids = archive_db.youtube_chat_user_ids_without_display_name(limit)
    if not ids:
        return 0
    resolved = 0
    for uid in ids:
        name = None
        try:
            with guarded_youtube_dl_channel(
                {"quiet": True, "no_warnings": True, "skip_download": True}
            ) as ydl:
                info = ydl.extract_info(
                    f"https://www.youtube.com/channel/{uid}", download=False
                )
            info = info or {}
            name = info.get("title") or info.get("channel") or info.get("uploader")
        except Exception as exc:  # noqa: BLE001 — bot wall / dead channel
            logger.debug("display-name resolve failed for %s: %s", uid, exc)
            continue
        if name:
            try:
                archive_db.set_message_display_name("youtube", uid, str(name))
                resolved += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("display-name store failed for %s: %s", uid, exc)
    return resolved
