"""Proof-of-a-new-VOD gate: an expensive probe needs a reason to run.

The measured defect this closes: the archive scheduler re-enumerated every
saved channel's YouTube tabs on every pass (three yt-dlp channel walks each),
the instant-preview pass re-fetched each channel's newest VOD metadata on
every pass, and each FFmpeg preview followed. When a platform answered with a
rate limit the work was not deferred -- it was retried on the next pass, and
the next, and the next: 1,317 yt-dlp invocations and 648 rate-limit lines in
4,000 log lines, all buying the same "nothing changed" answer.

THE RULE: an expensive probe runs only when there is proof of a NEW VOD since
the last successful check. Two proof shapes are accepted:

  * **Explicit proof** -- the caller has a cheap "newest id" (an RSS feed entry,
    a preview sidecar's ``vod_url``) and passes it as ``latest_id``.
    :func:`should_probe` answers False while it equals the recorded id, so the
    expensive call is skipped outright.
  * **No cheap proof** -- the caller has no new id yet (the first probe, or a
    platform without a cheap listing). :func:`should_probe` becomes a bounded
    cadence: it allows one probe, and after each probe that found nothing new
    :func:`note_probe` arms an exponential backoff (60s -> 120s -> ... -> 30min
    ceiling) for that channel. A channel with no new VODs is therefore probed
    once per backoff window, never once per pass.

A probe that DOES find a new VOD calls ``note_probe(found_new=True)``, which
clears the backoff completely, so a channel that starts uploading again is
served on the very next opportunity -- a gate that stranded new content would
be a worse defect than the polling it fixes.

State is process-local and best-effort persisted to a JSON file (a few writes
per pass, never per request) so a daemon restart does not reset every channel
to "probe now" and stampede the platform. Every persistence failure degrades
to in-memory only. Never raises.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

_log = logging.getLogger(__name__)

#: First backoff after a probe that found nothing new (seconds). 60s matches
#: the live-badge TTL: re-probing faster than the poll that produced the
#: answer is the behaviour being fixed.
BACKOFF_BASE_S = 60.0
BACKOFF_FACTOR = 2.0
BACKOFF_MAX_S = 1800.0
_MAX_TRACKED = 512

_lock = threading.Lock()
_states: Dict[str, Dict[str, Any]] = {}
_loaded = False
_path_override: Optional[Path] = None
_clock: Callable[[], float] = time.monotonic


def set_clock(fn: Callable[[], float]) -> None:
    """Inject the time source (tests). All cadence maths reads this."""
    global _clock
    _clock = fn


def configure(path: Optional[os.PathLike | str]) -> None:
    """Pin the state file (tests / portable runs). None restores the default."""
    global _path_override, _loaded
    with _lock:
        _path_override = Path(path) if path else None
        _states.clear()
        _loaded = False


def _state_path() -> Path:
    env = os.environ.get("VODRIP_PROBE_GATE", "").strip()
    if env:
        return Path(env)
    if _path_override is not None:
        return _path_override
    try:
        from services.disk_hygiene import data_dir

        return data_dir() / "probe_gate.json"
    except Exception:  # noqa: BLE001
        return Path("probe_gate.json")


def _load() -> None:
    """Load persisted state once. Best-effort: a corrupt file means no memory."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        path = _state_path()
        if not path.is_file():
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(value, dict):
                    _states[key] = value
    except Exception:  # noqa: BLE001
        _log.debug("probe_gate: state load failed", exc_info=True)


def _save_locked() -> None:
    """Persist state (caller holds ``_lock``). Best-effort."""
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(_states, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        _log.debug("probe_gate: state save failed", exc_info=True)


def _key(platform: str, key: str) -> str:
    return f"{(platform or '').strip().lower()}::{(key or '').strip().lower()}"


def _backoff_for(no_new_streak: int) -> float:
    if no_new_streak <= 0:
        return 0.0
    return min(BACKOFF_MAX_S, BACKOFF_BASE_S * (BACKOFF_FACTOR ** (no_new_streak - 1)))


def should_probe(
    platform: str,
    key: str,
    *,
    latest_id: Optional[str] = None,
    now: Optional[float] = None,
) -> bool:
    """True when an expensive probe for ``(platform, key)`` may run now.

    With ``latest_id`` (explicit proof): True only when it differs from the id
    recorded by the last probe; the caller must then call :func:`note_probe`.
    Without it (no cheap proof): a time gate -- True on the first call and once
    the armed backoff window has elapsed, False inside it."""
    if not key:
        return True  # nothing to key on -- never gate work we cannot attribute
    _load()
    with _lock:
        st = _states.get(_key(platform, key))
        if latest_id is not None:
            if st is None:
                return True
            last = st.get("last_id")
            return not (last is not None and str(last) == str(latest_id))
        if st is None:
            return True
        next_at = float(st.get("next_probe_at") or 0.0)
        return (now if now is not None else _clock()) >= next_at


def note_probe(
    platform: str,
    key: str,
    *,
    found_new: bool,
    latest_id: Optional[str] = None,
    now: Optional[float] = None,
) -> None:
    """Record the outcome of a probe; arm or clear the per-channel backoff.

    ``found_new=True`` clears the backoff and stores ``latest_id``; a
    no-new outcome advances the streak and sets the next allowed probe time."""
    if not key:
        return
    _load()
    with _lock:
        k = _key(platform, key)
        st = _states.setdefault(
            k, {"last_id": None, "no_new_streak": 0, "next_probe_at": 0.0}
        )
        if latest_id is not None:
            st["last_id"] = str(latest_id)
        if found_new:
            st["no_new_streak"] = 0
            st["next_probe_at"] = 0.0
        else:
            st["no_new_streak"] = int(st.get("no_new_streak") or 0) + 1
            now_s = now if now is not None else _clock()
            st["next_probe_at"] = now_s + _backoff_for(st["no_new_streak"])
        if len(_states) > _MAX_TRACKED:
            # Evict the entry whose next probe is furthest out -- the one
            # costing the least if we forget it (same policy as the live
            # upcoming backoff's bounded map).
            victim = max(
                _states.items(),
                key=lambda kv: float(kv[1].get("next_probe_at") or 0.0),
            )[0]
            if victim != k:
                _states.pop(victim, None)
        _save_locked()


def backoff_remaining(
    platform: str, key: str, *, now: Optional[float] = None
) -> float:
    """Seconds until the next time-gated probe is allowed (0 when allowed)."""
    if not key:
        return 0.0
    _load()
    with _lock:
        st = _states.get(_key(platform, key))
        if st is None:
            return 0.0
        next_at = float(st.get("next_probe_at") or 0.0)
        return max(0.0, next_at - (now if now is not None else _clock()))


def state(platform: str, key: str) -> Optional[Dict[str, Any]]:
    """Snapshot of one channel's gate state (diagnostics)."""
    _load()
    with _lock:
        st = _states.get(_key(platform, key))
        return dict(st) if st else None


def reset() -> None:
    """Drop all state (tests / operator reset) and delete the persisted file.

    ``_loaded`` is set True so a subsequent call does NOT reload the file we
    just removed — a reset that silently repopulated itself from disk would be
    no reset at all."""
    global _loaded
    with _lock:
        _states.clear()
        _loaded = True
        try:
            _state_path().unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 — best-effort
            pass
