"""INFO/WARNING logs for YouTube preview + download (visible without DEBUG)."""

from __future__ import annotations

import collections
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("VOD.RIP.youtube")

_LAST_EXTRACT_SOURCE: dict[str, str] = {}


def auth_hint(session: Any = None) -> str:
    if session is None:
        return "anonymous"
    bits: list[str] = []
    if getattr(session, "cookie_file", None):
        bits.append("cookies_file")
    browser = getattr(session, "cookies_from_browser", None)
    if browser:
        bits.append(f"browser={browser}")
    if getattr(session, "po_token", None):
        bits.append("po_token")
    if getattr(session, "visitor_data", None):
        bits.append("visitor_data")
    return "+".join(bits) if bits else "anonymous"


def format_summary(info: Optional[dict]) -> str:
    if not info:
        return "formats=0"
    fmts = info.get("formats") or []
    heights = sorted({int(f.get("height") or 0) for f in fmts if f.get("height")})
    heights = [h for h in heights if h > 0]
    muxed = sum(
        1 for f in fmts
        if f.get("acodec") not in ("none", None) and f.get("vcodec") not in ("none", None)
    )
    hls = sum(1 for f in fmts if "m3u8" in str(f.get("protocol") or ""))
    dash = sum(
        1 for f in fmts
        if (f.get("url") or "").startswith("https://") and "m3u8" not in str(f.get("protocol") or "")
    )
    return (
        f"formats={len(fmts)} heights={heights[:10]} muxed={muxed} hls={hls} dash_https={dash}"
    )


def last_extract_source(video_id: str) -> str:
    return _LAST_EXTRACT_SOURCE.get(video_id, "")


def log_extract_ok(video_id: str, source: str, info: dict, session: Any = None) -> None:
    _LAST_EXTRACT_SOURCE[video_id] = source
    log.info(
        "extract ok video=%s source=%s auth=%s %s",
        video_id,
        source,
        auth_hint(session),
        format_summary(info),
    )
_AGE_GATE_MARKERS = (
    "sign in to confirm your age",
    "confirm your age",
    "age-restricted",
    "age restricted",
    "age-gate",
    "age gate",
    "age_verification_required",
    "inappropriate for some users",
    # yt-dlp serves the refusal in the LOGIN/UI language of the request, so an
    # English-only list silently misses every non-English install — the live
    # pt-BR failure ("Faça login para confirmar sua idade") missed entirely and
    # was reported as a retryable transient. Language-neutral phrasings
    # ("confirm"/"confirmar" + "age"/"idade") are listed per language, plus the
    # per-language noun/adjective pairs, so a translated refusal still matches.
    "confirmar sua idade",                     # pt-BR
    "confirmar a sua idade",                   # pt-BR (pt-PT)
    "confirme sua idade",                      # pt-BR (imperative)
    "confirm your age",                        # pt-PT
    "faça login para confirmar sua idade",     # pt-BR (full refusal)
    "faca login para confirmar sua idade",     # pt-BR (no accents)
    "login para confirmar sua idade",          # pt-BR (substring)
    "verifique sua idade",                     # pt-BR
    "conteúdo restrito por idade",             # pt-BR
    "conteudo restrito por idade",             # pt-BR (no accents)
    "restrito por idade",                      # pt-BR
    "confirma tu edad",                        # es
    "verificación de edad",                    # es
    "contenido restringido por edad",          # es
    "confirmez votre âge",                     # fr
    "vérifiez votre âge",                      # fr
    "bestätige dein alter",                    # de
    "confirma la tua età",                     # it
    "age verification required",               # en (spaced variant)
    "age verification",                        # en (generic, catches en/en-GB)
)


def is_age_gate_error(exc: BaseException) -> bool:
    """True when the error is a definitive YouTube age gate (login required).

    Distinct from the transient bot gate ("Sign in to confirm you're not a
    bot") — retrying an age-gated video NEVER succeeds without logged-in
    cookies. Verified against yt-dlp 2026.07.04 + current wiki (2026-08-12):
    no anonymous player client (web, web_embedded, android_vr, web_safari)
    passes the age gate anymore; the app's cookie_bridge login flow is the
    only path.

    Marker matching is case-insensitive and must stay LANGUAGE-COMPLETE: this
    predicate drives the 403-vs-503 status, the user-facing text, and the
    terminal (no-retry) decision. A marker miss on a non-English refusal makes
    the app report a permanent, credential-blocked failure as a transient
    "try again" and re-queue it forever.
    """
    low = str(exc).lower()
    return any(marker in low for marker in _AGE_GATE_MARKERS)


# Stamped into an archive_jobs.error for an age-gate failure so the queue's
# terminal check (archive_db.update_job) can recognise it from the error TEXT
# alone — the same contract the existing terminal markers use. It must survive
# the [:400] truncation applied to job errors, so it goes at the FRONT.
AGE_GATE_JOB_MARKER = "age-gate-needs-login"


def is_age_gate_job_error(error: Optional[str]) -> bool:
    """True when a stored job error is a parked age-gate failure."""
    return AGE_GATE_JOB_MARKER in (error or "").lower()


# Monitor-only taxonomy marker for the subtitles PO-Token policy (yt-dlp
# `SUBS_PO_TOKEN_POLICY`, declared in yt_dlp/extractor/youtube/_base.py,
# currently `required=False` but "in rollout"). When the policy flips on for a
# client, yt-dlp does NOT raise: `_report_pot_subtitles_skipped` silently
# DISCARDS the affected subtitle tracks — for the default clients it even routes
# the note through `write_debug(..., only_once=True)`, so a caption regression
# would reach users as "some languages are missing" with nothing visible at our
# log level. The predicate below is what the extract loggers match on.
#
# Detection only — deliberately no retry, no client swap, no status change:
# subtitles are not on the preview/download path this service gates on, and
# acting on a policy that is not yet enforced would be guessing.
_SUBS_PO_TOKEN_POLICY_MARKERS = (
    # yt_dlp/extractor/youtube/_video.py::_report_pot_subtitles_skipped
    "client subtitles require a po token which was not provided",
    "subtitles po token",
    # the remedy hint it prints, which names the context explicitly
    ".subs+xxx",
)


def is_subs_pot_policy_error(text: Any) -> bool:
    """True when `text` mentions the YouTube subtitles PO-Token policy.

    Accepts a string or exception; matching is case-insensitive on the whole
    message. Never used to classify a failure — see the marker comment.
    """
    low = str(text).lower()
    return any(marker in low for marker in _SUBS_PO_TOKEN_POLICY_MARKERS)


assert is_subs_pot_policy_error(
    "dQw4w9WgXcQ: Some WEB client subtitles require a PO Token which was not "
    "provided. They will be discarded since they are not downloadable as-is. "
    'You can manually pass a Subtitles PO Token for this client with '
    '--extractor-args "youtube:po_token=WEB.subs+XXX" .'
)
assert not is_subs_pot_policy_error("Sign in to confirm you're not a bot")
assert not is_subs_pot_policy_error("WEB client formats require a GVS PO Token")


# --- SUBS_PO_TOKEN_POLICY monitor (event-driven, no polling) ---------------
#
# The policy is detection-only by design (see the marker comment above), so
# the only thing an operator can act on is *whether it is firing yet*. That
# needs a durable record, not a log grep: every stamp site calls
# `record_subs_pot_event`, which appends a timestamped line to a bounded
# JSONL under the cache root and keeps a small in-memory ring for the status
# read. No threads, no timers, no network — the file is touched only when
# yt-dlp itself reported the policy.
_POT_EVENT_FILE = "subs_pot_policy.jsonl"
_POT_RING_MAX = 50
_POT_FILE_MAX = 200
_POT_DETAIL_MAX = 300
_POT_EVENTS: "collections.deque[dict]" = collections.deque(maxlen=_POT_RING_MAX)
_POT_LOCK = threading.Lock()
# JSONL -> ring rehydration happens on the first status read after boot, so a
# restart does not reset the window count to zero.
_POT_REHYDRATED = False


def _pot_event_path() -> Path:
    """JSONL path: routed cache root, else the historical appdata log folder.

    Resolved per call (never memoized) so the per-cache env knobs keep
    working — including VODRIP_CACHE_DIR, which tests pin to scratch.
    """
    root = None
    try:
        from services.settings import cache_root  # lazy: keeps import light

        root = cache_root()
    except Exception:
        root = None
    if root:
        return root / "youtube-diag" / _POT_EVENT_FILE
    from services.settings import _get_appdata_dir

    return _get_appdata_dir() / "logs" / _POT_EVENT_FILE


def _pot_sanitize(text: str) -> str:
    """Redact secret-bearing values (the policy line can quote a token hint)
    and bound the stored detail. Lazy import: error_log pulls nothing heavy
    but stays off this module's import graph."""
    try:
        from services.error_log import _sanitize_message

        text = _sanitize_message(text)
    except Exception:
        text = str(text)
    return text[:_POT_DETAIL_MAX]


def record_subs_pot_event(video_id: str, detail: str, source: str) -> None:
    """Stamp one policy sighting (thread-safe). Never raises: a diagnostic
    sink must not break the extract path that hit the policy."""
    entry = {
        "ts": time.time(),
        "video_id": str(video_id or ""),
        "source": str(source or ""),
        "detail": _pot_sanitize(detail),
    }
    with _POT_LOCK:
        _POT_EVENTS.append(entry)
        # Read-modify-write under the SAME lock as the ring append (mirrors
        # error_log): two unlocked writers would each read the pre-append
        # file and the second replace() would silently drop the first line.
        try:
            pp = _pot_event_path()
            pp.parent.mkdir(parents=True, exist_ok=True)
            on_disk: list[str] = []
            try:
                if pp.exists():
                    on_disk = pp.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                on_disk = []
            # Keep the file bounded to the latest _POT_FILE_MAX records;
            # rewrite atomically (temp + replace) so a crash never leaves a
            # torn JSONL.
            keep = (on_disk + [json.dumps(_pot_line(entry), ensure_ascii=False)])[-_POT_FILE_MAX:]
            payload = "\n".join(keep) + "\n"
            tmp = pp.with_name(pp.name + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(pp)
        except Exception as exc:  # noqa: BLE001 — diagnostics must not raise
            log.debug("subs POT event persist failed: %s", exc)


def _pot_line(entry: dict) -> dict:
    """JSONL shape: epoch ts (window math) + ISO ts (greppable by eye)."""
    return {
        "ts": entry["ts"],
        "iso": datetime.fromtimestamp(entry["ts"], tz=timezone.utc).isoformat(),
        "video_id": entry["video_id"],
        "source": entry["source"],
        "detail": entry["detail"],
    }


def _pot_rehydrate() -> None:
    """Load the JSONL tail into the ring once per process (missing/corrupt
    lines are skipped — the file is a diagnostic, not a source of truth)."""
    global _POT_REHYDRATED
    with _POT_LOCK:
        if _POT_REHYDRATED:
            return
        _POT_REHYDRATED = True
        if _POT_EVENTS:
            return  # already live; a boot-time file must not double-count
        try:
            pp = _pot_event_path()
            raw = pp.read_text(encoding="utf-8", errors="replace").splitlines() if pp.exists() else []
        except Exception:
            return
        for line in raw[-_POT_RING_MAX:]:
            try:
                rec = json.loads(line)
                _POT_EVENTS.append(
                    {
                        "ts": float(rec["ts"]),
                        "video_id": str(rec.get("video_id") or ""),
                        "source": str(rec.get("source") or ""),
                        "detail": str(rec.get("detail") or ""),
                    }
                )
            except Exception:
                continue


def _pot_status_from_entries(entries: list[dict], window_sec: float) -> dict:
    """Pure status math over a ring snapshot (no lock, no disk)."""
    cutoff = time.time() - window_sec
    in_window = sum(1 for e in entries if e["ts"] >= cutoff)
    return {
        "last": dict(entries[-1]) if entries else None,
        "count_in_window": in_window,
        "window_sec": float(window_sec),
        "total_events": len(entries),
    }


def subs_pot_policy_status(window_sec: float = 3600.0) -> dict:
    """Last sighting + how many fired in the trailing `window_sec`.

    Shape: {last: {ts, video_id, source, detail} | None, count_in_window,
    window_sec, total_events}. Reads the in-memory ring (rehydrated from the
    JSONL on first call), so it is cheap enough for a response field.
    """
    _pot_rehydrate()
    with _POT_LOCK:
        entries = list(_POT_EVENTS)
    return _pot_status_from_entries(entries, window_sec)


def reset_subs_pot_policy_state() -> None:
    """Drop ring + rehydration flag (test hook: each case owns its scratch
    cache dir, so the boot-time file must be re-readable)."""
    global _POT_REHYDRATED
    with _POT_LOCK:
        _POT_EVENTS.clear()
        _POT_REHYDRATED = False


assert _pot_status_from_entries([], 60.0) == {
    "last": None,
    "count_in_window": 0,
    "window_sec": 60.0,
    "total_events": 0,
}
_POT_FIXTURE = {"ts": time.time(), "video_id": "abc", "source": "extract_fail", "detail": "d"}
assert _pot_status_from_entries([_POT_FIXTURE], 60.0)["count_in_window"] == 1
assert _pot_status_from_entries([_POT_FIXTURE], 60.0)["last"] == _POT_FIXTURE
assert _pot_status_from_entries(
    [{"ts": 1.0, "video_id": "", "source": "", "detail": ""}, _POT_FIXTURE], 60.0
)["count_in_window"] == 1
assert _pot_sanitize("https://x/watch?v=a&pot=SECRET").endswith("pot=[REDACTED]")


PLAYABILITY_TRUST_S = 300.0

# Sentinel for "the caller did not precompute a hint, so look it up from url".
# None is a real value and means "no structured answer for this url".
_NO_HINT: object = object()


def structured_playability(url: Optional[str]) -> Optional[tuple[str, str]]:
    """("unavailable"|"gate", reason) from the structured playability record.

    The English substring tables below cannot see a localized failure. Measured
    2026-10-11: YouTube answered a dead video with its pt-BR reason, which
    matched no branch, so a dead video became HTTP 500 and "Preview failed -
    try again." -- a hard retry for something that can never play. The innertube
    probe that ran inside the same request had recorded (status, reason, kind)
    for that video id, and "kind" is language-independent ("fatal" = retrying
    cannot help), so it is the half that is safe to trust.

    Only the two kinds the recorder actually writes are accepted, and every
    field access sits inside the guard, so a malformed or future record is "no
    structured answer" rather than an exception raised while handling another
    one. The record is per video id and at most PLAYABILITY_TRUST_S old; it is
    deliberately sticky across probes of the same id (a soft probe never
    downgrades a stored fatal), so a video that failed once as fatal keeps that
    verdict for the window even if a later probe is soft.
    """
    if not url:
        return None
    try:
        from services import youtube_innertube as it

        vid = it.extract_video_id(url)
        rec = it.innertube_playability_verdict(vid, max_age_s=PLAYABILITY_TRUST_S)
        if not rec:
            return None
        kind, _status, reason = rec
        if kind == "fatal":
            return ("unavailable", reason or "")
        if kind == "retry":
            return ("gate", reason or "")
        return None
    except Exception:
        return None


# The English substring tables, in ONE precedence order shared by both outputs.
# They used to be walked in different orders, so a blob carrying both a
# definitive phrase and a transient one -- which is what yt-dlp prints when it
# tries one player client per line -- could pair HTTP 404 with "try again".
# A tie goes to the transient branch on purpose: a wrong 404 tells the user a
# playable video is gone forever, a wrong 503 costs one retry.
_TRANSIENT_MARKERS = (
    "cookie",
    "blocked",
    "bot",
    "dpapi",
    "decrypt",
    "po_token",
    "sign in",
    "preview unavailable",  # soft-gate chain collapse -- transient, not 404
)
_CHANNEL_MEMBERSHIP_MARKERS = ("members-only content", "join this channel")
_PRIVATE_VIDEO_MARKERS = ("private video",)
_UNAVAILABLE_MARKERS = (
    "video has been removed",
    "video is not available",
    "video is unavailable",
    "video unavailable",
)


def _english_failure_class(exc: BaseException) -> Optional[str]:
    """One failure class for one error text, or None if the tables match nothing.

    One of "age_gate" | "transient" | "members" | "private" | "unavailable".
    Both youtube_http_status and youtube_user_message map this single answer, so
    the status and the message can no longer disagree about the same exception.
    """
    if is_age_gate_error(exc):
        return "age_gate"
    low = str(exc).lower()
    if any(x in low for x in _TRANSIENT_MARKERS):
        return "transient"
    if any(x in low for x in _CHANNEL_MEMBERSHIP_MARKERS):
        return "members"
    if any(x in low for x in _PRIVATE_VIDEO_MARKERS):
        return "private"
    if any(x in low for x in _UNAVAILABLE_MARKERS):
        return "unavailable"
    return None


def youtube_http_status(
    exc: BaseException, *, url: Optional[str] = None, hint: Any = _NO_HINT
) -> int:
    """Map a sanitized YouTube error to an HTTP status code.

    Returns 403 for permanent member/permission errors, 404 for unavailable
    videos, 503 for transient bot/cookie/auth issues, 500 for unknown.

    `url` is an optional hint. When the English tables above match nothing, the
    structured playability record for that URL decides. The tables are consulted
    FIRST on purpose: a soft bot/age gate answers with the same generic
    "Video unavailable" reason as a dead video, so a structured "fatal" must
    never get ahead of a soft gate the text already recognised.

    `hint` is the precomputed result of structured_playability(url), for callers
    that also need the message and must not look the record up twice; the
    default means "not precomputed".
    """
    kind = _english_failure_class(exc)
    if kind == "age_gate":
        return 403  # needs a logged-in account — retrying never helps
    if kind == "transient":
        return 503
    if kind in ("members", "private"):
        return 403
    if kind == "unavailable":
        return 404
    structured = structured_playability(url) if hint is _NO_HINT else hint
    if structured is not None:
        return 404 if structured[0] == "unavailable" else 503
    return 500


def age_gate_actionable_message(*, preview: bool = False) -> str:
    """Actionable text for an age-gate refusal, honest about session state.

    Two states, because they need different user actions and telling the user
    to "sign in" when they already did is worse than saying nothing:

      no session configured -> set up the app's own YouTube sign-in
      session rejected      -> re-capture (cookies were rotated)

    An age-restricted video is public; it just needs a signed-in,
    age-verified account. YouTube has closed the anonymous paths, so no
    amount of retrying substitutes for credentials — the message must not
    imply a retry could fix it."""
    verb = "watched" if preview else "downloaded"
    try:
        from services.youtube_session import youtube_session_configured

        configured = youtube_session_configured()
    except Exception:
        # Never let a probe failure turn an honest message into a generic one.
        configured = False
    if not configured:
        return (
            f"Age-restricted video — no signed-in YouTube session is configured, "
            f"so it cannot be {verb}. Open Settings > Cookie Bridge, sign in to "
            f"YouTube, then retry this job."
        )
    return (
        f"Age-restricted video — the configured YouTube session was rejected "
        f"(YouTube rotates account cookies while a YouTube tab is open), so it "
        f"cannot be {verb}. Sign in again from a private window via "
        f"Settings > Cookie Bridge, then retry this job."
    )


def youtube_user_message(
    exc: BaseException,
    *,
    preview: bool = False,
    url: Optional[str] = None,
    hint: Any = _NO_HINT,
) -> str:
    """Sanitize YouTube errors for API/UI — never mention cookies or bot jargon.

    `url` is the same optional hint as youtube_http_status: it lets a localized
    definitive failure still say "This video is unavailable." instead of the
    generic retry text. `hint` is the same precomputed value as in
    youtube_http_status; pass it when one error becomes both a status and a
    message, so the two cannot disagree about the record.
    """
    # Definitive age gate — retrying never helps without a logged-in account;
    # the app's YouTube sign-in flow (cookie_bridge) is the only unlock.
    kind = _english_failure_class(exc)
    if kind == "age_gate":
        return age_gate_actionable_message(preview=preview)
    if kind == "transient":
        return (
            "YouTube preview is temporarily restricted. Try again in a few minutes."
            if preview
            else "Could not load this YouTube video — try again in a moment."
        )
    # Definitive unplayable states — retrying will never help; say so.
    if kind == "members":
        return "Members-only video — requires channel membership."
    if kind == "private":
        return "This video is private."
    if kind == "unavailable":
        return "This video is unavailable."
    structured = structured_playability(url) if hint is _NO_HINT else hint
    if structured is not None and structured[0] == "unavailable":
        return "This video is unavailable."
    if structured is not None and structured[0] == "gate":
        # Localized soft gate — the same wording as the English bot/cookie
        # table above, so a 503 never reads as a generic preview failure.
        return (
            "YouTube preview is temporarily restricted. Try again in a few minutes."
            if preview
            else "Could not load this YouTube video — try again in a moment."
        )
    if preview:
        return "Preview failed — try again."
    return "Could not load video info — try again."


def youtube_error_response(
    exc: BaseException, *, preview: bool = False, url: Optional[str] = None
) -> tuple[int, str]:
    """(status, message) for one error, from ONE structured-playability lookup.

    youtube_http_status and youtube_user_message each consult the record when
    handed a url, so calling them separately can pair a 404 with "try again"
    (or a 503 with "This video is unavailable.") if a probe for the same video
    lands between the two calls. The error handler that raises the
    HTTPException uses this instead.
    """
    hint = structured_playability(url)
    return (
        youtube_http_status(exc, hint=hint),
        youtube_user_message(exc, preview=preview, hint=hint),
    )


assert "restricted" in youtube_user_message(RuntimeError("cookie database locked"), preview=True).lower()
assert "restricted" in youtube_user_message(RuntimeError("Sign in to confirm you're not a bot"), preview=True).lower()
# Age gate is definitive (needs login, 403, explicit message) — bot gate stays
# transient (503, "try again"). Verified 2026-08-12 against yt-dlp 2026.07.04:
# no anonymous client passes the age gate, so retry messaging would be a lie.
assert is_age_gate_error(RuntimeError("Sign in to confirm your age"))
assert is_age_gate_error(RuntimeError("This video is age-restricted"))
assert not is_age_gate_error(RuntimeError("Sign in to confirm you're not a bot"))
assert not is_age_gate_error(RuntimeError("This video is unavailable"))
assert "age-restricted" in youtube_user_message(RuntimeError("Sign in to confirm your age"), preview=True).lower()
assert "age-restricted" in youtube_user_message(RuntimeError("This video is age-restricted"), preview=False).lower()
assert "try again" not in youtube_user_message(RuntimeError("Sign in to confirm your age"), preview=False).lower()
# Transient gate collapses map to 503, definitive dead-video messages to 404 —
# a soft "Video unavailable" must never surface as a hard 404.
assert youtube_http_status(RuntimeError("YouTube preview unavailable for this video")) == 503
assert youtube_http_status(RuntimeError("Sign in to confirm you're not a bot")) == 503
assert youtube_http_status(RuntimeError("Sign in to confirm your age")) == 403
assert youtube_http_status(RuntimeError("This video is unavailable")) == 404
assert youtube_http_status(RuntimeError("This video has been removed by the uploader")) == 404
assert youtube_http_status(RuntimeError("members-only content")) == 403


def log_extract_fail(
    video_id: str,
    reason: str,
    session: Any = None,
    *,
    exc: Optional[BaseException] = None,
    detail: str = "",
    final: bool = False,
) -> None:
    msg = f"extract fail video={video_id} reason={reason} auth={auth_hint(session)}"
    if detail:
        msg = f"{msg} {detail}"
    if exc is not None and is_subs_pot_policy_error(exc):
        # Log-only stamp: the taxonomy name is what makes the silent subtitle
        # discard greppable. Nothing downstream branches on it. The monitor
        # records the sighting (window count + last event) — still no retry,
        # no client swap, no status change.
        msg = f"{msg} marker=SUBS_PO_TOKEN_POLICY"
        record_subs_pot_event(video_id, f"{msg}: {exc}", "extract_fail")
    sink = log.warning if final else log.debug
    if exc is not None:
        sink("%s: %s", msg, exc)
    else:
        sink(msg)


def log_preview_resolve(
    platform: str,
    kind: str,
    heights: list[int],
    *,
    custom_master: bool,
    entry_url: str,
) -> None:
    log.info(
        "preview resolve platform=%s kind=%s heights=%s synthetic_master=%s entry=%s",
        platform,
        kind,
        heights[:12],
        custom_master,
        (entry_url or "")[:120],
    )


def log_preview_session(
    session_id: str,
    platform: str,
    kind: str,
    heights: list[int],
    *,
    custom_master: bool,
    entry_url: str,
) -> None:
    log.info(
        "preview session=%s platform=%s kind=%s heights=%s synthetic_master=%s entry=%s",
        session_id[:8],
        platform,
        kind,
        heights[:12],
        custom_master,
        (entry_url or "")[:120],
    )


def log_preview_upstream(
    route: str,
    session_id: str,
    upstream_status: int,
    nbytes: int,
    ctype: str,
    upstream_url: str,
    *,
    note: str = "",
) -> None:
    suspicious = (
        upstream_status >= 400
        or (upstream_status == 200 and nbytes == 0)
        or (route.endswith("playlist") and nbytes > 0 and not upstream_url.lower().endswith((".m3u8", ".mp4", ".m4s", ".ts")))
    )
    msg = (
        f"preview {route} session={session_id[:8]} upstream_http={upstream_status} "
        f"bytes={nbytes} ctype={ctype or '-'} url={(upstream_url or '')[:120]}"
    )
    if note:
        msg = f"{msg} {note}"
    if suspicious:
        log.warning(msg)
    else:
        log.info(msg)


def log_download(
    download_id: str,
    event: str,
    *,
    url: str = "",
    platform: str = "",
    detail: str = "",
) -> None:
    log.info(
        "download id=%s event=%s platform=%s url=%s %s",
        download_id[:12],
        event,
        platform,
        (url or "")[:100],
        detail.strip(),
    )


assert auth_hint(None) == "anonymous"
