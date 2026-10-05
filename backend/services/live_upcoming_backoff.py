"""A live that has not started must stop being polled on a fixed cadence.

MEASURED DEFECT (tmp/vodrip-devall-api.log, 2026-10-05 00:57 -> 05:03, 32
occurrences of one id at a ~126 s interval):
``yt-dlp: ERROR: [youtube] ZvW6Id7tmHs: Este evento ao vivo comecar em breve.``
That id has 0 rows in the live archive ``H:\\VOD.RIP-data\\archive.db`` (13,051
videos, read-only), so every one of those probes bought nothing: no video row,
no archive work, no live stream - just an "it has not started yet" answer,
charged against the learned YouTube budget that the owner's preview shares.

The cadence came from the channel live-badge poll: ``routers/live.py``
``_LIVE_STATUS_TTL_SEC = 60`` re-probes a channel's live status every TTL, and
each probe reaches ``services/live_capture.youtube_live_info`` -> the yt-dlp
extract. A scheduled stream that starts in three hours therefore costs ~180
probes, and each one is a real yt-dlp egress that draws from the same 4.04 rpm
pool the preview is paced against.

THE FIX IS A BACKOFF, not a bigger TTL and not a suppression. It has to be
bounded in the wall-clock life of a scheduled stream, and it has to be REVERSIBLE
the moment the stream actually goes live - a backoff that strands a live that
started would be a worse defect than the one it fixes. So:

  * the ladder is finite and terminates: after ``LIVE_UPCOMING_ATTEMPT_CAP``
    refusals a not-started live stops being probed at all
    (:func:`is_exhausted`), rather than re-offering work forever;
  * :func:`note_started` clears the state completely, so a live that comes up
    is served on the very next badge poll;
  * the first rung is never shorter than the 60 s badge TTL, because re-probing
    faster than the poll that produced the refusal is the behaviour being fixed.

Scope: this module is the STATE and the LADDER. Deciding when to call
:func:`note_not_started` is ``live_capture``'s job, at the one place the not-
started outcome is actually observed.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional, Tuple

#: Seconds to wait before re-probing a live that has not started yet.
#:
#: Starts at the live-badge TTL (60 s, ``routers/live.py``
#: ``_LIVE_STATUS_TTL_SEC``) because anything shorter re-probes faster than the
#: poll that produced the refusal, then escalates ~2x to a 15-minute ceiling.
#: A stream that starts inside the first minute still shows its badge on the
#: first poll that can see it; a stream scheduled hours out costs a handful of
#: probes instead of hundreds.
LIVE_UPCOMING_BACKOFF: Tuple[float, ...] = (60.0, 120.0, 240.0, 480.0, 900.0)

#: After this many consecutive "not started" refusals the live stops being
#: probed entirely. The owner re-opening the channel list, or the channel
#: changing, clears it - see :func:`note_started` and :func:`reset`.
LIVE_UPCOMING_ATTEMPT_CAP = 12

# How many distinct not-started lives we remember. Small on purpose: this is a
# hot per-poll map, and a channel that is genuinely scheduled is one of a few.
_MAX_TRACKED = 64


class _State:
    __slots__ = ("attempts", "next_allowed_at")

    def __init__(self, attempts: int, next_allowed_at: float) -> None:
        self.attempts = attempts
        self.next_allowed_at = next_allowed_at


_lock = threading.Lock()
_states: Dict[str, _State] = {}


def _now() -> float:
    return time.monotonic()


def _ladder(attempts: int) -> float:
    """Seconds to wait after the ``attempts``-th consecutive refusal."""
    idx = max(0, int(attempts) - 1)
    if idx >= len(LIVE_UPCOMING_BACKOFF):
        return LIVE_UPCOMING_BACKOFF[-1]
    return LIVE_UPCOMING_BACKOFF[idx]


def note_not_started(video_id: str, attempts: int = 0) -> bool:
    """Record that ``video_id`` is a live that has not started. Always True.

    ``attempts`` is the caller's consecutive-refusal count; passing it keeps the
    count in the caller's loop rather than in this module's, so a caller that
    already tracks it does not have to read it back. The wait is taken from
    :data:`LIVE_UPCOMING_BACKOFF` and the state is advanced either way.
    """
    vid = (video_id or "").strip()
    if not vid:
        return True
    n = max(0, int(attempts))
    with _lock:
        prior = _states.get(vid)
        n = max(n, (prior.attempts + 1) if prior else 1)
        if n > LIVE_UPCOMING_ATTEMPT_CAP:
            n = LIVE_UPCOMING_ATTEMPT_CAP
        _states[vid] = _State(attempts=n, next_allowed_at=_now() + _ladder(n))
        if len(_states) > _MAX_TRACKED:
            # Evict the entry whose next attempt is furthest out - the one
            # costing the least if we forget it.
            victim = max(_states.items(), key=lambda kv: kv[1].next_allowed_at)[0]
            if victim != vid:
                _states.pop(victim, None)
    return True


def note_started(video_id: str) -> None:
    """Clear any backoff for ``video_id``: it is live, or is no longer tracked.

    The polarity that makes the backoff safe to ship. Called when an extract
    reports the stream is actually live (or has moved on to a real video), so a
    live that finally begins is served on the next badge poll.
    """
    vid = (video_id or "").strip()
    if not vid:
        return
    with _lock:
        _states.pop(vid, None)


def is_backing_off(video_id: str) -> bool:
    """True when ``video_id`` must NOT be re-probed yet (or ever again)."""
    vid = (video_id or "").strip()
    if not vid:
        return False
    with _lock:
        st = _states.get(vid)
        if st is None:
            return False
        return st.attempts >= LIVE_UPCOMING_ATTEMPT_CAP or _now() < st.next_allowed_at


def is_exhausted(video_id: str) -> bool:
    """True once a not-started live has burned the whole ladder."""
    vid = (video_id or "").strip()
    if not vid:
        return False
    with _lock:
        st = _states.get(vid)
        return st is not None and st.attempts >= LIVE_UPCOMING_ATTEMPT_CAP


def attempts(video_id: str) -> int:
    """Consecutive not-started refusals recorded for ``video_id``.

    0 for an id that was never seen - which is NOT the same as "tried and
    succeeded", so callers must not read 0 as good news on its own.
    """
    vid = (video_id or "").strip()
    if not vid:
        return 0
    with _lock:
        st = _states.get(vid)
        return st.attempts if st else 0


def retry_in(video_id: str) -> Optional[float]:
    """Seconds until ``video_id`` may be probed again; None if not tracked.

    None means "no state" OR "never again" (exhausted) - use
    :func:`is_backing_off` to tell those apart.
    """
    vid = (video_id or "").strip()
    if not vid:
        return None
    with _lock:
        st = _states.get(vid)
        if st is None:
            return None
        return max(0.0, st.next_allowed_at - _now())


def state(video_id: str) -> Optional[Dict[str, object]]:
    """Snapshot of the backoff state, for a status/diagnostic readout."""
    vid = (video_id or "").strip()
    if not vid:
        return None
    with _lock:
        st = _states.get(vid)
        if st is None:
            return None
        return {
            "video_id": vid,
            "attempts": st.attempts,
            "next_allowed_at": st.next_allowed_at,
            "retry_in_s": round(max(0.0, st.next_allowed_at - _now()), 1),
            "exhausted": st.attempts >= LIVE_UPCOMING_ATTEMPT_CAP,
        }


def tracked() -> List[str]:
    """Video ids currently carrying backoff state (diagnostics)."""
    with _lock:
        return sorted(_states)


def reset() -> None:
    """Drop all state. Tests, and the owner re-adding a channel."""
    with _lock:
        _states.clear()
