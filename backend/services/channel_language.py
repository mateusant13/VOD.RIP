"""Per-channel language detection: platform clues + transcript evidence.

Owner decision (WS-3): **videos.channel_language** (TEXT, nullable) is the
single persisted owner — every video row of a channel carries the channel's
language. Everything the API/UI shows (channel payloads, archive search
hits, preview sessions) reads it. It is written by exactly two paths:

  * platform clue at channel-fetch time (persist_platform_clue) —
    Twitch GQL video.language, Kick channel payload, YouTube innertube
    audio track;
  * transcript evidence (persist_aggregated) — after transcription, when
    the whisper/caption language tally overrides the stored decision.

Resolution precedence (by confidence, not rule order): user per-channel
override (1.0) > videos.original_language consensus, the WS-4 column read
defensively (0.95) > stored decision that a fresh fetch re-stamped (0.85) >
transcript tally (0.4 + 0.5*fraction) > stored decision whose snapshot went
stale (0.5) > unknown (None).

Staleness rule: a stored decision is "fresh" while channel_snapshots says
the channel was fetched within CLUE_STALE_DAYS; after that it decays below a
strong tally, so a channel whose transcript evidence contradicts its old
platform clue converges to the measured language on the next aggregation.
ponytail: per-segment whisper probabilities are not persisted (transcripts
has only the normalized lang tag), so the tally weights segments equally;
upgrade path is a lang_probability column + weighted tally.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from services import archive_db

logger = logging.getLogger(__name__)

# Language families the pipeline knows; anything else keeps its raw ISO-ish
# code lowercased ('ja', 'ko', ...) so non-pt/en/es channels still flow.
KNOWN = ("pt", "en", "es")

# Minimum transcript segments before the tally may decide a channel's
# language (a handful of stray sections is noise, not a signal).
MIN_TALLY_SEGMENTS = 10
# Stored decision is "fresh" while the channel was fetched within this many
# days; older stamps decay below a strong transcript tally.
CLUE_STALE_DAYS = 90.0
# Worker-driven re-aggregation throttle per (platform, channel).
REAGGREGATE_MIN_GAP_SEC = 600.0

_lock = threading.Lock()
_last_run: dict[tuple[str, str], float] = {}


def normalize_language(code: Any) -> Optional[str]:
    """Normalize a language clue/tag to its family code.

    'pt-BR'/'pt-pt' -> 'pt'; 'en-US' -> 'en'; 'es-ES'/'es-419' -> 'es';
    any other non-empty code is lowercased and kept as-is; ''/None/'auto'
    -> None (unknown / no hint)."""
    if code is None:
        return None
    text = str(code).strip().lower()
    if not text or text == "auto":
        return None
    base = text.split("-")[0]
    return base or None


def _stored_evidence(platform: str, channel: str) -> list[dict]:
    """Stored decision (videos.channel_language) as a candidate, freshness-weighted.

    The stored value is the last decision — platform clue or aggregation
    re-stamp. Its confidence comes from channel_snapshots: a recent fetch
    means the clue was (re)confirmed by the platform, an old one means the
    decision is stale and a strong tally may outrank it."""
    rows = archive_db.channel_video_languages(platform, channel)
    if not rows:
        return []
    age = archive_db.channel_snapshot_age_sec(platform, channel)
    fresh = age is None or age < CLUE_STALE_DAYS * 86400.0
    out = []
    for r in rows:
        lang = normalize_language(r.get("language"))
        if not lang:
            continue
        out.append({
            "language": lang,
            "confidence": 0.85 if fresh else 0.5,
            "source": "platform",
            "videos": int(r.get("videos") or 0),
            "fresh": fresh,
        })
    return out


def _transcript_evidence(platform: str, channel: str) -> tuple[list[dict], int]:
    """Transcript tally candidates: dominant language across every section.

    Returns (candidates, total_segments); [] when evidence is below the
    minimum — the tally must never decide a channel on a handful of rows."""
    rows = archive_db.channel_language_tally(platform, channel)
    total = sum(int(r.get("segments") or 0) for r in rows)
    if total < MIN_TALLY_SEGMENTS:
        return [], total
    out = []
    for r in rows:
        lang = normalize_language(r.get("language"))
        if not lang:
            continue
        frac = int(r.get("segments") or 0) / total
        # 0.4 + 0.5*frac: full dominance scores 0.9, bare majority 0.65 —
        # above a stale stored decision (0.5), below a fresh one (0.85).
        out.append({
            "language": lang,
            "confidence": 0.4 + 0.5 * frac,
            "source": "transcript",
            "segments": int(r.get("segments") or 0),
            "videos": int(r.get("videos") or 0),
        })
    return out, total


def _original_language_evidence(platform: str, channel: str) -> list[dict]:
    """WS-4 clue: videos.original_language consensus (defensive read).

    WS-4 adds original_language in parallel; the column may be absent on
    this build (archive_db.channel_original_languages returns None then).
    When present and a >=50% majority of the channel's videos agree, it is
    the highest-confidence non-user clue (YouTube's declared audio
    language)."""
    rows = archive_db.channel_original_languages(platform, channel)
    if not rows:
        return []
    total = sum(int(r.get("videos") or 0) for r in rows)
    out = []
    for r in rows:
        lang = normalize_language(r.get("language"))
        if not lang:
            continue
        frac = (int(r.get("videos") or 0) / total) if total else 0.0
        if frac >= 0.5:
            out.append({
                "language": lang,
                "confidence": 0.95,
                "source": "original_title",
                "videos": int(r.get("videos") or 0),
            })
    return out


def aggregate_channel_language(
    platform: str,
    channel: str,
    *,
    overrides: Optional[dict[str, str]] = None,
) -> dict:
    """Best-effort language for a channel: {language, confidence, source}.

    Pure function over the DB (no writes); the UI/API read path. Highest
    confidence wins; ties break by source precedence override > original >
    platform > transcript (measured evidence is the last resort, not the
    first)."""
    channel_l = (channel or "").strip().lower()
    if not channel_l:
        return {"language": None, "confidence": 0.0, "source": "none"}
    overrides = overrides or {}
    override = overrides.get(channel) or overrides.get(channel_l)
    ov_lang = normalize_language(override)
    if ov_lang:
        return {"language": ov_lang, "confidence": 1.0, "source": "override"}

    cands = (
        _original_language_evidence(platform, channel)
        + _stored_evidence(platform, channel)
        + _transcript_evidence(platform, channel)[0]
    )
    if not cands:
        return {"language": None, "confidence": 0.0, "source": "none"}
    _SOURCE_ORDER = {"override": 0, "original_title": 1, "platform": 2, "transcript": 3}
    best = max(
        cands,
        key=lambda c: (c["confidence"], -_SOURCE_ORDER.get(c.get("source"), 9)),
    )
    return {
        "language": best["language"],
        "confidence": round(best["confidence"], 3),
        "source": best["source"],
    }


def persist_platform_clue(platform: str, channel: str, clue: Any) -> Optional[str]:
    """Stamp videos.channel_language from a fresh platform fetch.

    Non-null clues overwrite the stored decision (the platform just
    re-confirmed it); a failed/empty clue leaves the stored value intact so
    a transient fetch failure never wipes a known language."""
    lang = normalize_language(clue)
    if lang:
        archive_db.set_channel_language(platform, channel, lang)
    return lang


def persist_aggregated(platform: str, channel: str) -> Optional[str]:
    """Recompute + persist the channel language from all evidence.

    Writes only when a language was actually decided (never NULLs out a
    known value — a no-evidence channel keeps its last decision). Returns
    the chosen code or None."""
    result = aggregate_channel_language(platform, channel)
    lang = result.get("language")
    if lang:
        archive_db.set_channel_language(platform, channel, lang)
        logger.info("channel language %s/%s -> %s (%s, conf %s)",
                    platform, channel, lang, result.get("source"), result.get("confidence"))
    return lang


def on_transcribe_done(platform: str, video_id: str) -> Optional[str]:
    """Post-transcription hook: re-aggregate the video's channel (throttled).

    Called by the whisper worker after a successful job — new transcript
    evidence should converge the channel language. At most one pass per
    (platform, channel) every REAGGREGATE_MIN_GAP_SEC."""
    channel = archive_db.video_channel(platform, video_id)
    if not channel:
        return None
    now = time.monotonic()
    key = (platform, channel.lower())
    with _lock:
        last = _last_run.get(key)
        if last is not None and now - last < REAGGREGATE_MIN_GAP_SEC:
            return None
        _last_run[key] = now
    try:
        return persist_aggregated(platform, channel)
    except Exception:
        logger.debug("channel language aggregation failed for %s/%s", platform, channel, exc_info=True)
        return None


def run_aggregation(platform: Optional[str] = None) -> dict:
    """Full pass: recompute every channel language from all evidence.

    Used by the self-check and as an on-demand backfill; returns
    {channels, decided} counters."""
    sql = (
        "SELECT DISTINCT platform, channel FROM videos "
        "WHERE channel IS NOT NULL AND channel != ''"
    )
    params: list[Any] = []
    if platform:
        sql += " AND platform = ?"
        params.append(platform)
    pairs = archive_db.query(sql, params)
    decided = 0
    for r in pairs:
        if persist_aggregated(r["platform"], r["channel"]):
            decided += 1
    return {"channels": len(pairs), "decided": decided}
