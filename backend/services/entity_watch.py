"""Entity watcher — detect saved words/phrases AND saved channels across all
transcriptions.

Two modes:
- auto: entities derived from saved channels (guiven, srdogg, mandiocaa, ...);
  matched by exact word-boundary spelling PLUS auto-derived ASR variants
  (phonetic folds, and 'sr'->'senhor/senior/seu' expansions for sr-prefixed
  slugs), so 'senhor dog'/'senior dog' hit the channel srdogg.
- manual: the user marks a word or phrase; it matches as an exact
  word-boundary phrase. Phrase entities imply the entity itself
  ('o guiven é muito ruim' is a stronger signal, not a different target).

False-positive guards (user-specified):
  - 'mandioca' alone must NOT match the entity 'mandiocaa' — canonical forms
    match by exact spelling/fold only, never Damerau.
  - 'lanche' must NOT match 'arthur lanches' — multi-word forms require the
    full word sequence.

The watcher daemon scans NEW transcript rows (watermark = max scanned
transcripts.id) every minute, bounded per pass; scans are idempotent
(unique key in entity_hits).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Callable, Optional

from services import archive_db
from services.archive_db import (
    _ACCENT_FOLD,
    _damerau_levenshtein,
    _phonetic_fold,
    _TOKEN_RE,
)

logger = logging.getLogger(__name__)

SCAN_INTERVAL_SEC = 60.0
_SCAN_BATCH = 5000  # rows per pass; the daemon catches up across passes
_SNIPPET_MAX = 200


# ---------------------------------------------------------------------------
# Auto variant derivation
# ---------------------------------------------------------------------------

def _sr_expansions(slug: str) -> list[str]:
    """BR-PT ASR expansions for sr-prefixed slugs: 'srdogg' is usually heard
    as 'senhor dog' / 'senior dog' / 'seu dog' (or spelled 'sr dog').

    Both the raw rest ('dogg') and its de-doubled form ('dog') are used —
    the user's own example is 'senhor dog', and ASR often drops the double
    consonant entirely."""
    if not slug.startswith("sr") or len(slug) <= 2:
        return []
    rest = slug[2:]
    rests = [rest]
    collapsed = re.sub(r"(.)\1+$", r"\1", rest)
    if collapsed != rest:
        rests.append(collapsed)
    out: list[str] = []
    for prefix in ("senhor", "senior", "seu", "sr"):
        for r in rests:
            out.append(f"{prefix} {r}")
            out.append(f"{prefix} {_phonetic_fold(r)}")
    return out


def auto_variants(slugs: list[str]) -> list[str]:
    """Variant aliases for an auto entity derived from a saved channel.

    Single-word aliases stay raw (no phonetic fold): the fold collapses
    doubled letters, which would make 'mandioca' match 'mandiocaa'. The
    sr-expansions are multi-word forms — safe under the window matcher.
    """
    out: list[str] = []
    for slug in slugs:
        slug = (slug or "").strip().lower()
        if not slug:
            continue
        out.append(slug)
        out.extend(_sr_expansions(slug))
    seen: set[str] = set()
    deduped = []
    for v in out:
        if v not in seen:
            seen.add(v)
            deduped.append(v)
    return deduped


def _entity_slug(entity: dict) -> str:
    for key in ("kickSlug", "twitchSlug", "youtubeSlug"):
        v = str(entity.get(key) or "").strip().lower()
        if v:
            return v
    return ""


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _within_budget(a: str, b: str) -> bool:
    """Per-word fuzzy budget for multi-word variant windows.

    Words of 3 chars or fewer match EXACTLY only: Damerau on 'seu'/'sr'
    bridges 'eu dou um tapa' onto 'seu dog' (real-corpus false positive —
    'eu'~'seu', 'dou'~'dog' each within 1 edit). Longer words keep the
    len//3 budget ('senhor' -> 'senior'). Exact/fold equality is checked by
    the caller before this, so a==b here implies the fold path failed."""
    if len(a) <= 3:
        return a == b
    return _damerau_levenshtein(a, b, max(1, len(a) // 3)) is not None


def _norm(token: str) -> str:
    """Accent-insensitive lowercase (spelling-preserving — no phonetic fold)."""
    return token.lower().translate(_ACCENT_FOLD)


def match_entity_in_text(entity: dict, text: str) -> Optional[dict]:
    """Return {variant} when `text` contains the entity, else None.

    Single-word forms match by exact spelling only (accent-insensitive) —
    NEVER by phonetic fold or Damerau, because the fold collapses doubled
    letters and would make 'mandioca' hit the entity 'mandiocaa' (the fold
    of both is 'mandioka'). Multi-word forms match per-word within a sliding
    window: exact spelling, phonetic fold ('arthur' -> 'artur' via the silent
    h), or Damerau within len//3 ('senhor dog' -> 'senior dog'). The window
    requires every word to match, so a single common word like 'lanche' can
    never hit 'arthur lanches'.
    """
    tokens = [t for t in _TOKEN_RE.split((text or "").lower()) if t]
    if not tokens:
        return None
    normed = [_norm(t) for t in tokens]
    folded = [_phonetic_fold(t) for t in tokens]

    def single_word(tok: str) -> bool:
        ntok = _norm(tok)
        for t, n in zip(tokens, normed):
            if t == tok or n == ntok:
                return True
        return False

    def multi_word(ftoks: list[str]) -> bool:
        n = len(ftoks)
        if n > len(tokens):
            return False
        nftoks = [_norm(t) for t in ftoks]
        fftoks = [_phonetic_fold(t) for t in ftoks]
        for i in range(len(tokens) - n + 1):
            win = tokens[i:i + n]
            nwin = normed[i:i + n]
            fwin = folded[i:i + n]
            if all(a == b or na == nb for a, b, na, nb in zip(win, ftoks, nwin, nftoks)):
                return True
            if all(
                fa == fb or _within_budget(a, b)
                for a, b, fa, fb in zip(win, ftoks, fwin, fftoks)
            ):
                return True
        return False

    def matches(form: str) -> bool:
        ftoks = [t for t in _TOKEN_RE.split(form.lower()) if t]
        if not ftoks:
            return False
        if len(ftoks) == 1:
            return single_word(ftoks[0])
        return multi_word(ftoks)

    if matches(entity["text"]):
        return {"variant": None}
    for alias in entity.get("aliases") or []:
        alias = (alias or "").strip()
        if alias and matches(alias):
            return {"variant": alias}
    return None


# ---------------------------------------------------------------------------
# Auto-entity sync from saved channels
# ---------------------------------------------------------------------------

def _default_channels() -> list:
    from deps import settings_mgr

    return settings_mgr.get().saved_channels or []


def sync_auto_entities(channels_provider: Optional[Callable] = None) -> int:
    """Upsert one auto entity per saved channel; disable auto entities whose
    channel was removed from the save list. Returns the enabled count."""
    try:
        channels = (channels_provider or _default_channels)() or []
    except Exception:
        logger.debug("entity sync: settings unavailable", exc_info=True)
        channels = []
    active_sources: set[str] = set()
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        slug = _entity_slug(ch)
        if not slug:
            continue
        active_sources.add(slug)
        slugs = [str(ch.get(k) or "").strip().lower() for k in ("kickSlug", "twitchSlug", "youtubeSlug")]
        name = str(ch.get("name") or "").strip()
        if name:
            canonical = name
        else:
            canonical = next((s for s in slugs if s), slug)
        archive_db.upsert_watched_entity(
            canonical,
            kind="auto",
            source_channel=slug,
            aliases=auto_variants([s for s in slugs if s]),
            enabled=True,
        )
    for ent in archive_db.list_watched_entities():
        if ent["kind"] == "auto" and ent["source_channel"] not in active_sources:
            archive_db.set_watched_entity(ent["id"], enabled=False)
    return len(active_sources)


# ---------------------------------------------------------------------------
# Scan + daemon
# ---------------------------------------------------------------------------

def run_scan_once(*, entities: Optional[list[dict]] = None, limit: Optional[int] = None) -> dict:
    """One incremental scan pass over new transcript rows. Returns stats."""
    if entities is None:
        sync_auto_entities()
        entities = [e for e in archive_db.list_watched_entities() if e["enabled"]]
    cursor = archive_db.entity_watch_cursor()
    rows = archive_db.transcript_rows_after(cursor, limit or _SCAN_BATCH)
    hits: list[dict] = []
    for row in rows:
        for ent in entities:
            m = match_entity_in_text(ent, row["text"] or "")
            if m:
                hits.append({
                    "entity_id": ent["id"],
                    "platform": row["platform"],
                    "video_id": row["video_id"],
                    "seg_idx": row["seg_idx"],
                    "offset_sec": row["start_sec"],
                    "snippet": (row["text"] or "")[:_SNIPPET_MAX],
                    "variant": m["variant"],
                })
    archive_db.record_entity_hits(hits)
    new_cursor = rows[-1]["id"] if rows else cursor
    archive_db.set_entity_watch_cursor(new_cursor)
    return {"scanned": len(rows), "hits": len(hits), "cursor": new_cursor}


_stop = threading.Event()
_thread: Optional[threading.Thread] = None
_lock = threading.Lock()


def _watcher_enabled() -> bool:
    try:
        from deps import settings_mgr

        return bool(getattr(settings_mgr.get(), "entity_watch_enabled", True))
    except Exception:
        return True


def start_entity_watcher(*, poll_interval: float = SCAN_INTERVAL_SEC,
                         scan_limit: Optional[int] = None) -> threading.Thread:
    """Start the entity watcher daemon (idempotent)."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        _stop.clear()
        t = threading.Thread(
            target=_run_loop,
            args=(float(poll_interval), scan_limit),
            daemon=True,
            name="entity-watcher",
        )
        _thread = t
        t.start()
        return t


def stop_entity_watcher(timeout: float = 5.0) -> None:
    global _thread
    _stop.set()
    t, _thread = _thread, None
    if t is not None:
        t.join(timeout=timeout)


def _run_loop(poll_interval: float, scan_limit: Optional[int]) -> None:
    while not _stop.is_set():
        if _watcher_enabled():
            try:
                stats = run_scan_once(limit=scan_limit)
                if stats["scanned"]:
                    logger.info(
                        "entity watcher: scanned %d rows, %d hits (cursor %d)",
                        stats["scanned"], stats["hits"], stats["cursor"],
                    )
            except Exception:
                logger.debug("entity watcher scan failed", exc_info=True)
        _stop.wait(poll_interval)


# ---------------------------------------------------------------------------
# Self-check (module import): every user-specified matcher contract.
# ---------------------------------------------------------------------------

def _selfcheck() -> None:
    cases: list[tuple[dict, str, bool]] = [
        # (entity, transcript text, should_hit)
        ({"text": "guiven", "kind": "auto", "aliases": []}, "o guiven é muito bom", True),
        ({"text": "srdogg", "kind": "auto",
          "aliases": ["senhor dog", "senior dog", "seu dog", "sr dog"]},
         "a gente viu o senhor dog ontem", True),
        ({"text": "srdogg", "kind": "auto",
          "aliases": ["senhor dog", "senior dog", "seu dog", "sr dog"]},
         "o senior dog ganhou de novo", True),
        ({"text": "mandiocaa", "kind": "auto", "aliases": []}, "mandiocaa apareceu", True),
        ({"text": "arthur lanches", "kind": "manual", "aliases": []},
         "o arthur lanches é muito engraçado", True),
        ({"text": "arthur lanches", "kind": "manual", "aliases": []},
         "quero um lanche agora", False),
        ({"text": "mandiocaa", "kind": "auto", "aliases": []}, "a mandioca é gostosa", False),
        ({"text": "o guiven é muito ruim", "kind": "manual", "aliases": []},
         "gente, o guiven é muito ruim mesmo", True),
    ]
    for ent, text, expected in cases:
        got = match_entity_in_text(ent, text) is not None
        assert got == expected, f"entity={ent['text']!r} text={text!r}: expected {expected}, got {got}"
    # Real-corpus regression: 'eu dou um tapa' must NOT match srdogg variants
    # ('seu dog' via 'eu'~'seu' + 'dou'~'dog'); 'a s do panton' must NOT
    # match 'sr dog'.
    sr = {"text": "srdogg", "kind": "auto",
          "aliases": ["senhor dog", "senior dog", "seu dog", "sr dog"]}
    assert match_entity_in_text(sr, "É toda vez que eu dou um tapa, mano") is None, (
        "'eu dou um tapa' must not match 'seu dog'"
    )
    assert match_entity_in_text(sr, "cara, a s do panton demora muito") is None, (
        "'a s do panton' must not match 'sr dog'"
    )
    # Case/accent insensitivity + fold bridging ('arthur' -> 'artur' via
    # silent-h drop is a variant alias, not canonical).
    assert match_entity_in_text({"text": "guiven", "kind": "auto", "aliases": []},
                                "GUIVEN falou") is not None
    # Multi-word forms bridge phonetic variants automatically ('arthur' ->
    # 'artur' via the silent-h fold in the window matcher) — no alias needed.
    assert match_entity_in_text(
        {"text": "arthur lanches", "kind": "manual", "aliases": []},
        "o artur lanches voltou") is not None
    # sr expansion auto-derivation covers the aliases used above.
    variants = auto_variants(["srdogg"])
    for v in ("srdogg", "senhor dog", "senhor dogg", "senior dog", "seu dog", "sr dog"):
        assert v in variants, f"auto_variants must include {v!r}, got {variants}"
    assert "mandioca" not in auto_variants(["mandiocaa"])


_selfcheck()
