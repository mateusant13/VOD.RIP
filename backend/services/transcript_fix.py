"""Champion-name transcript post-fix — ddragon gazetteer + banded Levenshtein.

ASR engines routinely butcher champion names in PT/EN/es streams ("doutor
mundo" for Dr. Mundo, "diana" for Diana, "jarvan quatro" for Jarvan IV).
This module rewrites transcript word lists in place, right before they are
stored, so search/FTS, entity_watch, the subtitles panel and the UI all see
the corrected text — the fix lives at the ONE choke point shared by both
ASR engines (archive_transcribe._transcribe_audio_source) and the captions
path (archive_ytdlp), never at each consumer.

Design (approved gate spec — implement exactly this):
  * Normalization (both sides): lowercase; NFKD strip accents; strip
    apostrophes (') and &; strip all other non-alphanumerics. Edge
    punctuation is split off BEFORE matching and reattached AFTER
    replacement; internal punctuation is dropped for matching. Hyphens
    split into window tokens; the roman-numeral token iv -> 4 (the
    gazetteer keeps both 'jarvaniv' and 'jarvan4'); the source-side numeral
    map iv|4|quatro|four -> 4; window-only aliases
    doutor|doctor|dotor -> dr, mestre -> master (aliases apply ONLY inside
    full-name window matches, never standalone).
  * Gazetteer: union of ddragon pt_BR + en_US + es_ES champion names
    (normalized form -> canonical ddragon name; on locale collision prefer
    the job language, else pt_BR). Entries split into single-token and
    multi-token (<=3 tokens) units; multi-token COMPONENTS never enter the
    single-token candidates (their full-name concatenations do, so a glued
    'jarvaniv' still matches). Fetched via versions.json -> latest patch
    (pinned fallback constant), champion.json for the 3 locales ONCE per
    process under a single-flight lock, disk-cached with a 7-day TTL in the
    app cache dir; 10 s HTTP timeout; any failure -> stale cache -> empty
    gazetteer -> fix no-op (never raises, never blocks a job).
  * Pre-filters (single-token pass): skip empty/None words; skip cores with
    len < 3; skip PT/EN stopwords; skip blocklisted words on the STRONG
    path (the weak path re-opens them under confidence gating — see below).
    Windows are exempt from all single-token pre-filters.
  * Strong path: single-token normalized dist <= 1 (dist 0 via the exact
    dict) AND passed pre-filters; multi-token windows: normalized
    CONCATENATION dist <= 2 cumulative, per-token dist <= 1, whole-name
    len >= 5; longest names first, left-to-right, consumed spans excluded
    from the single-token pass. Windows are strong-path only.
  * Weak path (confidence-gated): parakeet only (never captions, never when
    confidence missing). Conditions: (dist == 2 AND core len >= 5) OR
    (blocklisted word AND dist <= 1) — AND word_conf below the engine
    threshold (parakeet 0.5; the env knob VODRIP_TRANSCRIPT_FIX_CONF
    overrides it as a single number).
  * Replacement: canonical ddragon casing + reattached edge punctuation;
    timestamps preserved (single token keeps its own start/end; a window
    collapses to one word spanning first.start / last.end); the segment
    text is rebuilt as ' '.join(corrected word texts) ONLY when at least
    one correction landed in the segment (untouched segments stay
    byte-identical — this preserves the verified join(words) == text
    contract). Idempotent: a dist-0 self-match (the word already carries
    the canonical spelling) is a no-op, not counted.
  * Stats counter {segments_touched, strong_replaced, weak_replaced,
    blocked_hits} — logged at info by the callers and merged into the job
    stats dict; rejected-by-blocklist near-misses log a sample at debug.

Env knobs (all optional): VODRIP_TRANSCRIPT_FIX (default on, set to
0/false/no/off to disable), VODRIP_TRANSCRIPT_FIX_CONF (single-number
confidence threshold overriding both engines), VODRIP_TRANSCRIPT_CACHE
(test/portable cache-dir override).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ENABLED_ENV = "VODRIP_TRANSCRIPT_FIX"
CONF_ENV = "VODRIP_TRANSCRIPT_FIX_CONF"
CACHE_ENV = "VODRIP_TRANSCRIPT_CACHE"

VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
CDN_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/data/{locale}/champion.json"
LOCALES = ("pt_BR", "en_US", "es_ES")
FALLBACK_VERSION = "16.16.1"
HTTP_TIMEOUT_S = 10.0
CACHE_TTL_S = 7 * 24 * 3600

# Word-level confidence thresholds (weak path): the ASR was unsure, so a
# near-miss champion name is a plausible correction. VODRIP_TRANSCRIPT_FIX_CONF
# overrides both as a single number.
_DEFAULT_CONF = {"parakeet": 0.5}
# Engines eligible for the weak path (captions never are — "captions").
_WEAK_ENGINES = frozenset({"parakeet"})

# Source-side token maps (exact token match, applied to window tokens; the
# numeral map also applies to single-token cores where it is vacuous — iv/4
# map to a 1-char core that the len >= 3 pre-filter drops anyway).
_NUMERAL_MAP = {"iv": "4", "4": "4", "quatro": "4", "four": "4"}
_ALIAS_MAP = {"doutor": "dr", "doctor": "dr", "dotor": "dr", "mestre": "master"}


def _norm_core(core: str, *, keep_hyphens: bool) -> str:
    """Lowercase -> NFKD strip accents -> drop apostrophes/& -> strip the
    remaining non-alphanumerics (hyphens kept only for window tokenization)."""
    s = unicodedata.normalize("NFKD", core.lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("'", "").replace("&", "")
    if keep_hyphens:
        return "".join(ch for ch in s if ch.isalnum() or ch == "-")
    return "".join(ch for ch in s if ch.isalnum())


def _split_edges(raw: str) -> tuple[str, str, str]:
    """(prefix, core, suffix): leading/trailing non-alphanumerics split off
    BEFORE matching and reattached AFTER replacement. Apostrophes and & are
    STRIPPED (part of the core), never preserved as edge punctuation."""
    pre = 0
    while pre < len(raw) and not raw[pre].isalnum():
        pre += 1
    suf = len(raw)
    while suf > pre and not raw[suf - 1].isalnum():
        suf -= 1
    return raw[:pre], raw[pre:suf], raw[suf:]


def _map_token(tok: str) -> str:
    return _NUMERAL_MAP.get(tok, _ALIAS_MAP.get(tok, tok))


def _banded(a: str, b: str, max_dist: int) -> Optional[int]:
    """Levenshtein distance capped at ``max_dist`` (None when exceeded).

    Banded DP: only cells with |i - j| <= max_dist are computed. Any edit
    path that leaves the band has cost > max_dist at that point (each edit
    moves i - j by at most 1), so the band is exact for the distances this
    module asks about. Length buckets make the callers skip candidates
    whose length differs by more than the band — the abs() check below is
    the same bound for direct callers."""
    la, lb = len(a), len(b)
    if abs(la - lb) > max_dist:
        return None
    if la == 0:
        return lb if lb <= max_dist else None
    if lb == 0:
        return la if la <= max_dist else None
    inf = max_dist + 1  # sentinel: out-of-band cells can never win
    prev = [j if j <= max_dist else inf for j in range(lb + 1)]
    for i in range(1, la + 1):
        lo = max(1, i - max_dist)
        hi = min(lb, i + max_dist)
        cur = [inf] * (lb + 1)
        cur[0] = i if i <= max_dist else inf
        row_min = cur[0]
        for j in range(lo, hi + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            cur[j] = v
            if v < row_min:
                row_min = v
        prev = cur
        if row_min > max_dist:
            # Every path crosses every row; all in-band cells of this row
            # exceed max_dist and out-of-band cells already do -> no match.
            return None
    return prev[lb] if prev[lb] <= max_dist else None


# --- stopwords + blocklist -------------------------------------------------
# Stdlib-curated sets (~100 words each, PT + EN union applied globally).
# Accented forms are normalized at import ("não" -> "nao"), so the runtime
# lookup happens on the normalized core, exactly like the source words.

_STOPWORDS_PT_RAW = """
a ao aos as a as com como da das de dela dele deles delas do dos e em entre
era essa essas esse esses esta estar estas este estes estava eu foi for fosse
fossem fui ha haja isso isto ja lhe lhes mais mas me mesmo meu meus minha
minhas muito na nas nem no nos nos nao num numa o os ou para pela pelas pelo
pelos por qual quando que quem se sem seu seus sua suas so tambem te tem
tendo tenha ter teu teus ti todo todos tu tua um uma umas uns voce voces vos
aquele aquela aquelas aqueles aquilo ele ela elas eles sobre contra depois
antes durante apos ate ainda assim apenas onde ai
""".split()

_STOPWORDS_EN_RAW = """
a about above after again against all am an and any are as at be because been
before being below between both but by can cannot could did do does doing down
during each few for from further had has have having he her here hers herself
him himself his how i if in into is it its itself let me more most my myself
no nor not of off on once only or other ought our ours ourselves out over own
same she should so some such than that the their theirs them themselves then
there these they this those through to too under until up very was we were
what when where which while who whom why with would you your yours yourself
yourselves
""".split()

# Collision blocklist (pt+en union, applied globally). Two families:
#   1. Common words / real names that collide with champion names at
#      dist <= 1 (mel, graves, sena, twitch, aurora, sona, ...).
#   2. Multi-token COMPONENTS (mundo, sol, fortune, fate, yi, sin, kench,
#      glasc, willump, ...) — never corrected as single tokens; the full
#      name only matches inside a window.
#   3. dist >= 1 respellings of plausible real names that would otherwise
#      "correct" a real person's name into a champion (caitlin -> Caitlyn,
#      evelyn -> Evelynn, lilia -> Lillia, nico, dana -> Diana, lena ->
#      Leona, sonia -> Sona, elisa -> Elise — all verified dist-1 against
#      the live ddragon singles; the rest of the list is the review seed).
# Dist-0 case-only collisions ('diana', 'leona', 'samira', 'camille',
# 'annie', 'lulu', 'darius', 'olaf', 'viktor', 'vladimir', 'katarina',
# 'morgana', 'zoe') stay UNBLOCKED — the replacement is harmless.
_BLOCKLIST_RAW = """
mel bardo graves sena eco milho carma karma set atroz sona aurora twitch rise
ash pike vain lock kale brand rumble jinx kindred talon smolder singed briar
bard nautilus lux mundo sol fortune fate yi sin kench glasc willump caitlin
evelyn lilia nico dana lena sonia elisa
""".split()

_STOPWORDS = frozenset(_norm_core(w, keep_hyphens=False) for w in _STOPWORDS_PT_RAW + _STOPWORDS_EN_RAW)
_BLOCKLIST = frozenset(_norm_core(w, keep_hyphens=False) for w in _BLOCKLIST_RAW)

_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _camel_split(name: str) -> list[list[str]]:
    """CamelCase names ("LeBlanc") split into window tokens — the ASR hears
    them as two words. [] when the name has no lowercase->uppercase edge."""
    pieces = _CAMEL_RE.split(name)
    if len(pieces) < 2:
        return []
    toks: list[str] = []
    for piece in pieces:
        for t in _norm_core(piece, keep_hyphens=True).split("-"):
            if t:
                toks.append(_map_token(t))
    return [toks] if 2 <= len(toks) <= 3 else []


# --- gazetteer -------------------------------------------------------------

class ChampionFixer:
    """Normalized ddragon gazetteer + match logic. Immutable after build, so
    one instance is shared safely by every transcription pool thread."""

    def __init__(self, names: dict[str, dict[str, str]]):
        """names: {locale: {champion_key: display_name}} — the shape the
        ddragon fetcher produces."""
        singles: dict[str, dict[str, str]] = {}  # key -> {family: canonical}
        multi: dict[tuple[str, ...], dict[str, str]] = {}  # tokens -> {family: canonical}
        for locale, champ_names in names.items():
            family = (locale or "").split("_")[0]
            if family not in ("pt", "en", "es"):
                continue
            for _key, name in champ_names.items():
                canon = (name or "").strip()
                if not canon:
                    continue
                toks = self._window_tokens(canon)
                if len(toks) == 1:
                    singles.setdefault(toks[0], {})[family] = canon
                    # CamelCase names ("LeBlanc") are heard as two words.
                    for split in _camel_split(canon):
                        multi.setdefault(tuple(split), {})[family] = canon
                elif len(toks) <= 3:
                    # Multi-token name: registered as a window AND as its
                    # concatenated single-token forms (a glued ASR output
                    # like 'jarvaniv'/'jarvan4' still matches). Individual
                    # COMPONENTS never enter the single-token candidates.
                    multi.setdefault(tuple(toks), {})[family] = canon
                    singles.setdefault("".join(toks), {})[family] = canon
                    raw = "".join(self._window_tokens(canon, mapped=False))
                    if raw != "".join(toks):
                        singles.setdefault(raw, {})[family] = canon
        self._singles = singles
        self._single_buckets: dict[int, list[str]] = {}
        for key in singles:
            self._single_buckets.setdefault(len(key), []).append(key)
        for lst in self._single_buckets.values():
            lst.sort()
        # Longest names first: (token count, concatenation length) desc,
        # then the token tuple for determinism.
        self._multi = sorted(
            multi.items(),
            key=lambda kv: (-len(kv[0]), -sum(len(t) for t in kv[0]), kv[0]),
        )

    @staticmethod
    def _window_tokens(name: str, *, mapped: bool = True) -> list[str]:
        """Normalized window tokens for a name: whitespace split, hyphens
        split, per-piece normalization, optional numeral/alias maps."""
        toks: list[str] = []
        for piece in name.split():
            for t in _norm_core(piece, keep_hyphens=True).split("-"):
                if t:
                    toks.append(_map_token(t) if mapped else t)
        return toks

    @staticmethod
    def _canonical(canon_map: dict[str, str], language: Optional[str]) -> str:
        """Locale-collision preference: the job language, else pt_BR."""
        fam = (language or "").split("-")[0].split("_")[0]
        if fam in canon_map:
            return canon_map[fam]
        if "pt" in canon_map:
            return canon_map["pt"]
        return next(iter(canon_map.values()))

    def _best_single(self, core: str, max_dist: int, language: Optional[str]):
        """(canonical, dist, matched_key) with dist <= max_dist, or None.
        dist 0 via the exact dict; dist 1/2 via length-bucketed banded
        Levenshtein (candidates whose length differs by more than the band
        can never be within it). Ties are broken deterministically —
        (dist, blocklisted, -len, key) — so a real name always beats a
        blocklisted lookalike at the same distance ('sena' -> Senna, not
        Sona) regardless of gazetteer order."""
        cmap = self._singles.get(core)
        if cmap is not None:
            return (self._canonical(cmap, language), 0, core)
        if max_dist <= 0:
            return None
        best = None  # (score, canonical, dist, key)
        for length in range(len(core) - max_dist, len(core) + max_dist + 1):
            for key in self._single_buckets.get(length, ()):
                d = _banded(core, key, max_dist)
                if d is None:
                    continue
                score = (d, key in _BLOCKLIST, -len(key), key)
                if best is None or score < best[0]:
                    best = (score, self._canonical(self._singles[key], language), d, key)
        if best is None:
            return None
        return (best[1], best[2], best[3])

    def _name_matches(self, src_toks: list[str], name_toks: tuple[str, ...]) -> bool:
        """Window match: whole-name len >= 5, cumulative concatenation
        dist <= 2, and per-token dist <= 1."""
        if len("".join(name_toks)) < 5:
            return False
        if _banded("".join(src_toks), "".join(name_toks), 2) is None:
            return False
        for src, nm in zip(src_toks, name_toks):
            if _banded(src, nm, 1) is None:
                return False
        return True

    def _find_windows(self, words: list[dict], language: Optional[str]):
        """Non-overlapping multi-token matches: [(start, end, canonical)].
        Longest names first, left-to-right; a window is whole words only
        (a name may never end mid-word) and is exempt from every
        single-token pre-filter (stopwords/len/blocklist)."""
        stream: list[list[str]] = []
        for w in words:
            _pre, core, _suf = _split_edges(w.get("word", ""))
            stream.append([
                _map_token(t) for t in _norm_core(core, keep_hyphens=True).split("-") if t
            ])
        n = len(words)
        consumed = [False] * n
        matches: list[tuple[int, int, str]] = []
        for start in range(n):
            if consumed[start]:
                continue
            for name_toks, canon_map in self._multi:
                need = len(name_toks)
                collected: list[tuple[int, str]] = []  # (word_idx, token)
                end = start
                while end < n and not consumed[end] and len(collected) < need:
                    for t in stream[end]:
                        collected.append((end, t))
                    end += 1
                if len(collected) != need:
                    continue  # words ran out / a consumed word interrupted
                if collected[-1][0] != end - 1:
                    continue  # unreachable: end-1 always contributed tokens
                src_toks = [t for _wi, t in collected]
                if not self._name_matches(src_toks, name_toks):
                    continue
                window_end = collected[-1][0] + 1
                matches.append((start, window_end, self._canonical(canon_map, language)))
                for ci in range(start, window_end):
                    consumed[ci] = True
                break  # longest-first: the first match at this start wins
        return matches

    def fix_segment(
        self,
        segment: dict,
        *,
        engine: str,
        language: Optional[str] = None,
        stats: Optional[dict] = None,
    ) -> bool:
        """Correct champion names in one transcript segment (in place).

        Returns True when at least one word changed AND the segment text was
        rebuilt as ' '.join(words) (untouched segments stay byte-identical).
        Segments whose words do not reconstruct the text (captions without
        full inline timestamps) and segments with no words are never
        touched — the join(words) == text contract is preserved."""
        words = segment.get("words")
        if not words:
            return False
        text = segment.get("text", "")
        if " ".join(w.get("word", "") for w in words) != text:
            return False  # cannot rebuild losslessly — skip
        changed = False

        # 1) Multi-token windows (strong path only; pre-filter-exempt).
        # Every matched window collapses to one word (span = first.start /
        # last.end) so its words are consumed and never re-processed by the
        # single-token pass; a dist-0 self-match (replacement text == the
        # joined originals) collapses WITHOUT counting — text byte-identical,
        # idempotent.
        windows = self._find_windows(words, language)
        applied_windows = 0
        for start, end, canon in reversed(windows):
            pre = _split_edges(words[start]["word"])[0]
            suf = _split_edges(words[end - 1]["word"])[2]
            joined = " ".join(w["word"] for w in words[start:end])
            replacement = pre + canon + suf
            span_start = words[start]["start"]
            span_end = words[end - 1]["end"]
            words[start:end] = [{"word": replacement, "start": span_start, "end": span_end}]
            if replacement != joined:
                applied_windows += 1
        if applied_windows:
            changed = True
            if stats is not None:
                stats["strong_replaced"] = stats.get("strong_replaced", 0) + applied_windows

        # 2) Single tokens over the remaining (unconsumed) words.
        threshold = self.conf_threshold(engine)
        for w in words:
            raw = w.get("word", "")
            if not raw:
                continue
            pre, core_raw, suf = _split_edges(raw)
            core = _map_token(_norm_core(core_raw, keep_hyphens=False))
            if not core or len(core) < 3:
                continue
            if core in _STOPWORDS:
                continue
            is_blocked = core in _BLOCKLIST
            candidate = self._best_single(core, 1, language)
            if is_blocked:
                if candidate is not None:
                    if stats is not None:
                        stats["blocked_hits"] = stats.get("blocked_hits", 0) + 1
                    logger.debug(
                        "transcript-fix: blocklisted near-miss %r -> %r (dist %d)",
                        core, candidate[0], candidate[1],
                    )
            elif candidate is not None:
                replacement = pre + candidate[0] + suf
                if replacement != raw:  # idempotent: canonical spelling = no-op
                    w["word"] = replacement
                    changed = True
                    if stats is not None:
                        stats["strong_replaced"] = stats.get("strong_replaced", 0) + 1
                continue
            # Weak path: parakeet only, never without a confidence.
            if engine not in _WEAK_ENGINES:
                continue
            conf = w.get("conf")
            if conf is None:
                continue
            cand2 = candidate or self._best_single(core, 2, language)
            if cand2 is None:
                continue
            canon2, dist, _key = cand2
            if not ((dist == 2 and len(core) >= 5) or (is_blocked and dist <= 1)):
                continue
            if conf >= threshold:
                continue
            replacement = pre + canon2 + suf
            if replacement != raw:
                w["word"] = replacement
                changed = True
                if stats is not None:
                    stats["weak_replaced"] = stats.get("weak_replaced", 0) + 1

        if changed:
            segment["text"] = " ".join(w.get("word", "") for w in words)
            if stats is not None:
                stats["segments_touched"] = stats.get("segments_touched", 0) + 1
        return changed

    @staticmethod
    def conf_threshold(engine: str) -> float:
        v = os.environ.get(CONF_ENV, "").strip()
        if v:
            try:
                return float(v)
            except ValueError:
                pass
        return _DEFAULT_CONF.get(engine, 0.5)


def new_stats() -> dict:
    """Fresh transcript-fix counter dict (merged into job/report stats)."""
    return {"segments_touched": 0, "strong_replaced": 0, "weak_replaced": 0, "blocked_hits": 0}


# --- fetch + cache (single-flight, 7-day TTL, never raises) ----------------

def enabled() -> bool:
    """VODRIP_TRANSCRIPT_FIX toggle — on unless set to 0/false/no/off."""
    return os.environ.get(ENABLED_ENV, "").strip().lower() not in ("0", "false", "no", "off")


def _cache_dir() -> Path:
    env = os.environ.get(CACHE_ENV, "").strip()
    if env:
        return Path(env)
    from services.settings import _get_appdata_dir, cache_root  # lazy: keeps import light

    root = cache_root()
    if root:
        return root / "transcript-fix"
    return _get_appdata_dir() / "transcript-fix"


def _cache_path() -> Path:
    return _cache_dir() / "gazetteer.json"


def _read_cache(path: Path) -> Optional[tuple[float, dict]]:
    """(fetched_at_epoch, names) or None when missing/corrupt."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched = float(payload.get("fetched_at", 0.0))
        names = payload.get("names")
        if fetched <= 0 or not isinstance(names, dict):
            return None
        return fetched, names
    except Exception:
        return None


def _write_cache(path: Path, names: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": FALLBACK_VERSION, "fetched_at": time.time(), "names": names},
            ensure_ascii=False,
        )
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        logger.debug("transcript-fix: cache write failed", exc_info=True)


def _fetch_latest_version() -> str:
    with urllib.request.urlopen(VERSIONS_URL, timeout=HTTP_TIMEOUT_S) as resp:
        versions = json.load(resp)
    if not isinstance(versions, list) or not versions:
        raise ValueError("ddragon versions.json empty")
    latest = versions[0]
    return latest if isinstance(latest, str) and latest else FALLBACK_VERSION


def _fetch_gazetteer_names() -> Optional[dict]:
    """{locale: {champion_key: name}} for the latest patch, or None on any
    failure (network, parse, empty payload). Never raises."""
    try:
        version = _fetch_latest_version()
        out: dict[str, dict[str, str]] = {}
        for locale in LOCALES:
            url = CDN_URL.format(version=version, locale=locale)
            with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as resp:
                payload = json.load(resp)
            data = payload.get("data") or {}
            out[locale] = {
                key: str(champ.get("name", "")).strip()
                for key, champ in data.items()
                if champ and champ.get("name")
            }
        if not out.get("pt_BR"):
            return None
        return out
    except Exception:
        logger.warning("transcript-fix: ddragon fetch failed — using cache or no-op")
        return None


def _load_gazetteer_names() -> Optional[dict]:
    """Fresh cache -> it; stale/missing -> fetch with stale-cache fallback on
    failure; nothing anywhere -> None (fix no-op)."""
    path = _cache_path()
    cached = _read_cache(path)
    if cached is not None and time.time() - cached[0] < CACHE_TTL_S:
        return cached[1]
    names = _fetch_gazetteer_names()
    if names is not None:
        _write_cache(path, names)
        return names
    if cached is not None:
        logger.warning("transcript-fix: ddragon fetch failed — serving stale cache")
        return cached[1]
    return None


_fixer: Optional[ChampionFixer] = None
_fix_failed = False  # once-per-process fetch failure latch (fix no-op)
_fix_lock = threading.Lock()


def get_fixer() -> Optional[ChampionFixer]:
    """Process-global fixer. Loaded once under a single-flight lock (the
    transcription pool races here); a failed load latches so the fetch is
    attempted ONCE per process and never blocks a job."""
    global _fixer, _fix_failed
    if not enabled():
        return None
    if _fixer is not None or _fix_failed:
        return _fixer
    with _fix_lock:
        if _fixer is not None or _fix_failed:
            return _fixer
        names = _load_gazetteer_names()
        if not names:
            _fix_failed = True
            return None
        _fixer = ChampionFixer(names)
        logger.info(
            "transcript-fix: gazetteer ready (%d locales, %d single keys)",
            len(names), len(_fixer._singles),
        )
        return _fixer


def fix_segment(
    segment: dict,
    *,
    engine: str,
    language: Optional[str] = None,
    stats: Optional[dict] = None,
) -> bool:
    """Module-level entry: no-op (fast) when the fix is disabled or the
    gazetteer is unavailable — never raises, never blocks a job."""
    fixer = get_fixer()
    if fixer is None:
        return False
    return fixer.fix_segment(segment, engine=engine, language=language, stats=stats)


# --- module self-check (pure logic — no network, no disk, no settings) -----
assert _banded("kitten", "sitting", 3) == 3
assert _banded("kitten", "sitting", 2) is None, "band must cap the distance"
assert _banded("diana", "diana", 1) == 0
assert _banded("diana", "diiiana", 1) is None, "two inserts exceed the band"
assert _banded("diana", "diiiana", 2) == 2
assert _banded("", "abc", 2) is None and _banded("", "ab", 2) == 2
assert _split_edges("diana,") == ("", "diana", ",")
assert _split_edges("(diana)") == ("(", "diana", ")")
assert _split_edges("diana") == ("", "diana", "")
assert _norm_core("Dianá", keep_hyphens=False) == "diana"
assert _norm_core("K'Sante", keep_hyphens=False) == "ksante"
assert _norm_core("Nunu & Willump", keep_hyphens=True) == "nunuwillump"
assert _norm_core("well-known", keep_hyphens=False) == "wellknown"
assert _map_token("iv") == "4" and _map_token("quatro") == "4"
assert _map_token("four") == "4" and _map_token("4") == "4"
assert _map_token("doutor") == "dr" and _map_token("doctor") == "dr"
assert _map_token("mestre") == "master" and _map_token("mundial") == "mundial"
_check_names = {
    "pt_BR": {
        "Diana": "Diana", "LeBlanc": "LeBlanc", "JarvanIV": "Jarvan IV",
        "Nunu": "Nunu e Willump", "DrMundo": "Dr. Mundo",
    },
    "en_US": {"Nunu": "Nunu & Willump"},
}
_check = ChampionFixer(_check_names)
_seg = {"text": "diana", "words": [{"word": "diana", "start": 0.0, "end": 0.5}]}
assert _check.fix_segment(_seg, engine="parakeet") is True
assert _seg["text"] == "Diana" and _seg["words"][0]["word"] == "Diana"
_seg = {"text": "jarvan quatro", "words": [
    {"word": "jarvan", "start": 0.0, "end": 0.4}, {"word": "quatro", "start": 0.4, "end": 0.9}]}
assert _check.fix_segment(_seg, engine="parakeet") is True
assert _seg["text"] == "Jarvan IV" and _seg["words"] == [
    {"word": "Jarvan IV", "start": 0.0, "end": 0.9}], _seg["words"]
_seg = {"text": "le blanc", "words": [
    {"word": "le", "start": 0.0, "end": 0.3}, {"word": "blanc", "start": 0.3, "end": 0.7}]}
assert _check.fix_segment(_seg, engine="parakeet") is True
assert _seg["text"] == "LeBlanc" and _seg["words"][0]["word"] == "LeBlanc"
_seg = {"text": "nunu e willump", "words": [
    {"word": "nunu", "start": 0.0, "end": 0.3}, {"word": "e", "start": 0.3, "end": 0.4},
    {"word": "willump", "start": 0.4, "end": 0.9}]}
assert _check.fix_segment(_seg, engine="parakeet", language="pt") is True
assert _seg["text"] == "Nunu e Willump"
_seg = {"text": "nunu willump", "words": [
    {"word": "nunu", "start": 0.0, "end": 0.4}, {"word": "willump", "start": 0.4, "end": 0.9}]}
assert _check.fix_segment(_seg, engine="parakeet", language="en") is True
assert _seg["text"] == "Nunu & Willump", "en form must keep its own canonical"
_seg = {"text": "Jarvan IV", "words": [
    {"word": "Jarvan", "start": 0.0, "end": 0.4}, {"word": "IV", "start": 0.4, "end": 0.9}]}
assert _check.fix_segment(_seg, engine="parakeet") is False, "dist-0 self-match must be a no-op"
_seg = {"text": "jarvan iv", "words": [
    {"word": "jarvan", "start": 0.0, "end": 0.4}, {"word": "iv", "start": 0.4, "end": 0.9}]}
assert _check.fix_segment(_seg, engine="parakeet") is True
assert _seg["text"] == "Jarvan IV"
assert _check.fix_segment(_seg, engine="parakeet") is False, "second pass must be idempotent"
_check2 = ChampionFixer({"pt_BR": {"Senna": "Senna", "Sona": "Sona"}})
_seg = {"text": "sena", "words": [{"word": "sena", "start": 0.0, "end": 0.5, "conf": 0.2}]}
assert _check2.fix_segment(_seg, engine="parakeet") is True and _seg["text"] == "Senna", (
    "dist-1 tie must prefer the non-blocklisted real name")
