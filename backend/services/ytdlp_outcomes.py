"""Closed vocabulary of EXPECTED yt-dlp outcomes, and the classifier for them.

Why this module exists: the app's error ring (``services/error_log.py``, latest
500 records) was ~100% expected yt-dlp outcomes. Measured over the 500 retained
records of ``%APPDATA%\\VOD.RIP\\logs\\errors.jsonl`` (window 2026-09-15 ->
2026-10-05, 96.7 KB), 411 of 500 were ``yt-dlp: ERROR: ...`` lines and every
one was a knowable condition, not a defect:

  * 144  "This channel does not have a streams/shorts tab"  -> ``tab_absent``
  *  54  "Unable to download API page: HTTP Error 404"      -> ``channel_gone``
  * 106  "Faça login para confirmar que você não é um bot"   -> ``bot_wall_unauthenticated``
  *  25  "This live event will begin in a few moments"      -> ``live_upcoming``
  *  22  "Offline."                                         -> ``live_offline``
  *  the rest: members-only / removed / SME-claim videos, also expected.

Two consequences, both real:

  1. A genuine defect is evicted within hours. A 500-record ring that is
     ~100% expected outcomes has almost no headroom left for a real error.
  2. The channels burning a governor token, a yt-dlp spawn and ~8s EVERY cycle
     look identical, in the log the owner reads, to channels that are merely
     transiently offline.

So: a small CLOSED set of expected conditions, each with a stable machine code.
A code, not a sentence, for the reason the ``captions_unavailable_kind``
vocabulary records: the stored value is the contract, the human phrase is
derived at read time, and a wording edit can never break a persisted row.

THE CLASSIFIER IS CONSERVATIVE BY CONSTRUCTION. Matching is whole-marker and
anchored on a distinctive phrase, and anything not matched is ``UNKNOWN`` -
a real error that keeps its log record. A line is never guessed into an
expected bucket. Adding a code is a deliberate act; widening a marker is the
only way to make a new message "expected", and every marker below is a phrase
that cannot plausibly describe a defect.

The three codes that are PERMANENT PER-CHANNEL (``tab_absent``,
``channel_gone``) are also the ones ``services.youtube_service`` learns and
skips; the rest are transient conditions of a single attempt and are recorded
as state, never as a permanent verdict. That split lives in
``PERMANENT_CODES`` rather than being re-derived at each call site.
"""
from __future__ import annotations

import re
from typing import Optional

# --- the vocabulary ---------------------------------------------------------
#
# A CODE, not a sentence. Persisted into youtube_channel_outcomes.outcome_code;
# the phrase is derived at read time (OUTCOME_TEXT). Same discipline as
# archive_db.CAPTIONS_PARK_AGE_GATE_* -- storing English prose would make a
# client string-match it to know which remedy applies, and a wording edit would
# silently break every stored row.

# The channel has no such tab (no /streams, no /shorts). Permanent: a channel
# that never livestreams keeps answering this, and youtube_service asks for
# exactly one explicitly-requested tab per walk (youtube_service.py:657-662).
TAB_ABSENT = "tab_absent"
# The channel/tab itself 404s. Permanent: the handle is gone or renamed.
CHANNEL_GONE = "channel_gone"
# YouTube's IP-level bot wall in Portuguese. TRANSIENT and IP-scoped, NOT a
# per-channel verdict -- exactly the distinction dc01ea3 had to make for the
# age gate, and the reason this is not in PERMANENT_CODES.
BOT_WALL_UNAUTHENTICATED = "bot_wall_unauthenticated"
# A live stream that has not started yet / is offline. Transient.
LIVE_UPCOMING = "live_upcoming"
LIVE_OFFLINE = "live_offline"
# The video itself is not obtainable: members-only, removed by the uploader,
# SME copyright claim, deleted. Per-VIDEO and expected; not a channel verdict.
VIDEO_RESTRICTED = "video_restricted"

UNKNOWN = "unknown"

#: Every expected condition this module recognises. ``UNKNOWN`` is deliberately
#: NOT here: it is the absence of a match, not an outcome we "expect".
EXPECTED_CODES: tuple[str, ...] = (
    TAB_ABSENT,
    CHANNEL_GONE,
    BOT_WALL_UNAUTHENTICATED,
    LIVE_UPCOMING,
    LIVE_OFFLINE,
    VIDEO_RESTRICTED,
)

#: The subset that is a PERMANENT, PER-CHANNEL condition -- a channel that
#: answers "no streams tab" today will answer it again tomorrow, so the walk
#: can learn it and skip the request. Transient codes (bot wall, live state)
#: are NOT here: skipping those would hide a channel that recovers, which is
#: the bug the release path exists to avoid.
PERMANENT_CODES: frozenset[str] = frozenset({TAB_ABSENT, CHANNEL_GONE})

# The human phrase for each code, derived at READ time (never persisted).
# Deliberately not the whole 411-record vocabulary: a message that is not in
# this map is, by construction, a real error and keeps its log record.
OUTCOME_TEXT: dict[str, str] = {
    TAB_ABSENT: "This channel does not have that tab (no /streams or /shorts listing).",
    CHANNEL_GONE: "The channel is gone or renamed (HTTP 404 on its tab).",
    BOT_WALL_UNAUTHENTICATED: (
        "YouTube's bot wall refused an unauthenticated request — transient and "
        "IP-scoped, not a verdict about the channel."
    ),
    LIVE_UPCOMING: "The live event has not started yet.",
    LIVE_OFFLINE: "The live event is offline.",
    VIDEO_RESTRICTED: "The video is members-only, removed, or copyright-claimed.",
}


def outcome_text(code: str) -> str:
    """Human phrase for a stored code. Derived at read time, never persisted."""
    return OUTCOME_TEXT.get(code, f"Unrecognised yt-dlp outcome ({code or 'none'}).")


def is_expected(code: str) -> bool:
    """True only for a condition we deliberately recognise as expected."""
    return code in EXPECTED_CODES


def is_permanent(code: str) -> bool:
    """True for a learnable, skippable PER-CHANNEL condition."""
    return code in PERMANENT_CODES


# --- markers ----------------------------------------------------------------
#
# Anchored on a distinctive phrase, matched case-insensitively against the
# lowercased text. Every marker is a phrase that describes an expected
# condition in full - none of them is a generic substring like "error" or
# "unavailable" that a real defect could contain. That is what makes the
# "unrecognised -> unknown" guarantee hold.

# "This channel does not have a streams tab" / "... a shorts tab"
_TAB_ABSENT_RE = re.compile(
    r"this channel does not have an? (?:streams|shorts|live|videos) tab",
    re.I,
)
# "Unable to download API page: HTTP Error 404: Not Found" on a channel tab.
# Scoped to 404 so a transient 5xx on the same path stays a real error.
_CHANNEL_GONE_RE = re.compile(
    r"unable to download (?:api page|webpage).*http error 404", re.I
)
# The bot wall, in the Portuguese YouTube serves to a pt-BR locale, plus the
# English "sign in to confirm you're not a bot" phrasing.
_BOT_WALL_RE = re.compile(
    r"fa[cç]a login para confirmar que voc[eê] n[aã]o [eé] um bot"
    r"|sign in to confirm you'?re not a bot"
    r"|sign in to confirm that you'?re not a bot",
    re.I,
)
# Transient live state. "begin in a few moments" is the scheduled-lobby
# marker; "offline" alone is anchored so it cannot match a longer sentence
# that merely contains the word.
_LIVE_UPCOMING_RE = re.compile(r"this live event will begin in a few moments", re.I)
_LIVE_OFFLINE_RE = re.compile(r"(?:^|[:\s])offline\.?\s*$", re.I)
# Per-video expected verdicts, pt-BR and en. Each names a specific known
# YouTube state, so none can swallow a defect.
_VIDEO_RESTRICTED_RE = re.compile(
    r"seja membro do canal para ter acesso"
    r"|join this channel to get access to members-only content"
    r"|you must be logged into an account that has access to this subscriber-only content"
    r"|este v[ií]deo foi removido pelo usu[aá]rio"
    r"|this video has been removed by the uploader"
    r"|foi bloqueado devido ao conte[uú]do reivindicado por sme"
    r"|blocked due to community guidelines"
    r"|this live stream recording is not available"
    r"|n[aã]o est[aá] dispon[ií]vel",
    re.I,
)


def classify(text: str) -> str:
    """Classify one yt-dlp error/exception outcome into the vocabulary.

    Returns one of EXPECTED_CODES, or UNKNOWN. UNKNOWN is the DEFAULT and is
    returned for anything not matched by a marker above -- a line is never
    guessed into an expected bucket, because the whole point of the module is
    that an unrecognised message keeps its error-log record.

    Order matters only where two markers could both match; the more specific
    (per-channel, permanent) reading wins, so a channel-level 404 is not
    absorbed by the more general video-restricted pattern.
    """
    raw = "" if text is None else str(text)
    if not raw.strip():
        # An empty/absent message says nothing; it must not read as expected.
        return UNKNOWN
    # Try the per-channel markers FIRST: they are the actionable, learnable
    # verdicts, and a general marker must not pre-empt them.
    if _CHANNEL_GONE_RE.search(raw):
        return CHANNEL_GONE
    if _TAB_ABSENT_RE.search(raw):
        return TAB_ABSENT
    if _BOT_WALL_RE.search(raw):
        return BOT_WALL_UNAUTHENTICATED
    if _LIVE_UPCOMING_RE.search(raw):
        return LIVE_UPCOMING
    if _LIVE_OFFLINE_RE.search(raw):
        return LIVE_OFFLINE
    if _VIDEO_RESTRICTED_RE.search(raw):
        return VIDEO_RESTRICTED
    return UNKNOWN


def classify_exception(exc: BaseException) -> str:
    """Classify a raised yt-dlp error. Same vocabulary, same conservatism.

    Kept as its own name because the call sites differ: youtube_service has the
    exception object, the console logger has yt-dlp's already-formatted
    message string. Both must agree, so both route through classify().
    """
    return classify(f"{type(exc).__name__}: {exc}")


def is_expected_text(text: str) -> bool:
    """True when a yt-dlp message is a recognised EXPECTED condition."""
    return is_expected(classify(text))


def channel_from_message(text: str) -> Optional[str]:
    """The ``@handle`` a channel-tab outcome names, when it names one.

    yt-dlp renders these as ``[youtube:tab] @seeelbr/streams: ...`` - the
    tab is appended to the handle with a slash, so the handle is the segment
    between "@" and that slash. Returns None when the line names no channel
    (a per-video outcome, or a bot wall), because a bot wall is IP state and
    attributing it to one channel would be a lie the skip path would act on.
    """
    m = re.search(r"\[youtube:tab\]\s*@([A-Za-z0-9._-]+)(?:/([A-Za-z0-9]+))?", str(text or ""))
    return m.group(1) if m else None


def tab_from_message(text: str) -> Optional[str]:
    """The tab a channel-tab outcome names (``streams``/``shorts``/...)."""
    m = re.search(r"\[youtube:tab\]\s*@([A-Za-z0-9._-]+)(?:/([A-Za-z0-9]+))?", str(text or ""))
    return (m.group(2) or "").lower() or None
