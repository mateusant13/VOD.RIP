"""Single gate for all yt-dlp — blocks getpot_wpc (Chrome); allows bgutil PO plugin."""

from __future__ import annotations

import collections
import contextlib
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Iterator

from services import rl_counter
from services import ytdlp_env  # noqa: F401
from services import ytdlp_outcomes

logger = logging.getLogger(__name__)


# --- priority gate -----------------------------------------------------------
#
# MEASURED DEFECT this addresses: yt-dlp egress had no priority at all. The
# comment that used to sit here recorded the gap verbatim -- "Why no
# priority/bounded acquire here: a plain Lock has no priority" -- and the log
# shows the cost. `rate_budget: pacing yt-dlp yt_channel_list for 21.2s` recurs
# throughout tmp/vodrip-devall-api.log at the learned 4.04 rpm ceiling, and in
# the same window a real preview session measured `server_ms=22875`. A
# background caller that finishes a slice and immediately re-offers work wins
# every race against a preview that is already waiting, so the preview never
# gets a turn.
#
# WHAT THIS DOES *NOT* DO, deliberately:
#   * It does NOT time out a holder, and it does NOT preempt one. A 2-hour VOD
#     legitimately holds yt-dlp for minutes; a bounded acquire that failed it
#     would break real downloads, and nothing here interrupts a running
#     holder. Priority applies only at the instant the gate is GRANTED, which
#     is what makes it safe to let a live download finish in peace. The
#     pathological holder (a 0 B/s stall) stays DownloadManager's job
#     (STALL_WATCHDOG_SEC = 90s), which is the right mechanism for that.
#   * It does NOT starve background work. Arrival order is kept within a
#     class, so a walk with real work still runs it. The defect is a caller
#     that re-offers forever and a user who never gets a turn -- not
#     background work itself.
#   * It does NOT change the two-lane lock split. YTDLP_EXTRACT_LOCK and
#     YTDLP_CHANNEL_LOCK stay two distinct real locks, as
#     backend/tests/test_ytdlp_guard.py and the import-time assert in
#     ytdlp_hls.py require. This gate is the ADMISSION point in front of
#     them, not a replacement for them.
class _PriorityGate:
    """One-at-a-time admission that grants to the best WAITING caller.

    ``kind`` is ``"interactive"`` (a person is waiting on it: the preview
    resolve) or anything else -- ``background`` and ``download`` share the low
    class, because a long VOD download is exactly the holder that must not be
    cut off by either. FIFO within a class.
    """

    INTERACTIVE = "interactive"

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._busy = False
        self._waiters: list = []  # list[[is_high, seq]] in arrival order
        self._seq = 0

    def reset(self) -> None:
        """Test-only: drop waiter bookkeeping. Never releases a live holder."""
        with self._cond:
            self._waiters.clear()
            self._seq = 0

    @contextlib.contextmanager
    def acquire(self, kind: str = "background"):
        high = kind == self.INTERACTIVE
        with self._cond:
            self._seq += 1
            ticket = [high, self._seq]
            self._waiters.append(ticket)
            try:
                while True:
                    # THE RULE, in one place: when the gate is free, it goes to
                    # the EARLIEST-WAITING interactive caller if any is queued,
                    # otherwise to the earliest waiter of any class.
                    #
                    # Deliberately NOT "an interactive that arrived before me":
                    # a background walk that was already queued must still yield
                    # to a preview that arrives while it waits. That re-offering
                    # walk is the measured defect -- it wins the next grant every
                    # time otherwise, and the owner's preview never gets a turn.
                    head_high = next(
                        (w for w in self._waiters if w[0]), None
                    )
                    winner = head_high if head_high is not None else (
                        self._waiters[0] if self._waiters else None
                    )
                    if not self._busy and winner is ticket:
                        self._waiters.remove(ticket)
                        self._busy = True
                        break
                    self._cond.wait(0.02)
            except BaseException:
                if ticket in self._waiters:
                    self._waiters.remove(ticket)
                self._cond.notify_all()
                raise
        try:
            yield
        finally:
            with self._cond:
                self._busy = False
                self._cond.notify_all()


#: Shared admission point in front of the two lane locks.
priority_lock = _PriorityGate()


# The two lane locks stay real, distinct threading.Lock objects: the extract
# lane and the channel-listing lane remain separate mutexes. This change is
# about WHO GETS THE NEXT TURN, not about merging the lanes.
_YTDLP_LOCK = threading.Lock()
_YTDLP_CHANNEL_LOCK = threading.Lock()

_FORBIDDEN_PLUGIN_MARKERS = ("getpot_wpc", "getpot-wpc")
_BLOCKED_YOUTUBE_KEYS = frozenset()
_YTDLP_FORBIDDEN_PLUGIN_CACHED: bool | None = None


def _forbidden_plugin_present() -> bool:
    global _YTDLP_FORBIDDEN_PLUGIN_CACHED
    if _YTDLP_FORBIDDEN_PLUGIN_CACHED is not None:
        return _YTDLP_FORBIDDEN_PLUGIN_CACHED
    try:
        from yt_dlp.plugins import directories as plugin_dirs

        roots = plugin_dirs()
    except Exception:
        _YTDLP_FORBIDDEN_PLUGIN_CACHED = False
        return False
    for root in roots:
        try:
            base = Path(root)
            if not base.is_dir():
                continue
            for entry in base.iterdir():
                name = entry.name.lower()
                if any(marker in name for marker in _FORBIDDEN_PLUGIN_MARKERS):
                    _YTDLP_FORBIDDEN_PLUGIN_CACHED = True
                    return True
        except OSError:
            continue
    _YTDLP_FORBIDDEN_PLUGIN_CACHED = False
    return False


def _pot_auto_enabled() -> bool:
    try:
        from services.youtube_pot_service import pot_service_ping

        return pot_service_ping()
    except Exception:
        return False


def assert_ytdlp_safe() -> None:
    """Fail fast if getpot_wpc PO plugin is installed (spawns headless Chrome)."""
    if _forbidden_plugin_present():
        raise RuntimeError(
            "yt-dlp getpot_wpc plugin must not be installed — it spawns headless Chrome",
        )


def sanitize_ytdlp_opts(opts: dict[str, Any]) -> dict[str, Any]:
    """Strip blocked keys; enable bgutil fetch_pot when the POT server is up."""
    out = dict(opts)
    ext = out.get("extractor_args")
    if not isinstance(ext, dict):
        ext = {}
    else:
        ext = dict(ext)
    yt = dict(ext.get("youtube") or {})
    for key in _BLOCKED_YOUTUBE_KEYS:
        yt.pop(key, None)
    if _pot_auto_enabled():
        yt["fetch_pot"] = ["auto"]
    else:
        yt["fetch_pot"] = ["never"]
    bgutil = dict(ext.get("youtubepot-bgutilhttp") or {})
    if _pot_auto_enabled():
        from services.youtube_pot_service import POT_DEFAULT_BASE

        bgutil.setdefault("base_url", [POT_DEFAULT_BASE])
    ext["youtube"] = yt
    if bgutil:
        ext["youtubepot-bgutilhttp"] = bgutil
    out["extractor_args"] = ext
    return out


_EXPECTED_YTDLP_MARKERS = (
    "not currently live",
    "this video is not available",
    "video unavailable",
    "sign in to confirm your age",
    "faça login para confirmar sua idade",
    "this live stream recording is not available",
    "começará em breve",
    "foi encerrado",
    "não está disponível",
)


_YTDLP_VIDEO_PREFIX_RE = re.compile(r"^\[[a-z0-9:_-]+\] [A-Za-z0-9_-]{6,}: ", re.I)


class _YtdlpConsoleLogger:
    """yt-dlp logger that keeps REAL errors visible but drops expected
    extractor failures (offline channel, deleted video, age gate) from the
    console — those are normal conditions surfaced in the UI/job rows.
    Warnings are deduped process-wide on the NORMALIZED message (video-id
    prefix stripped), so per-video repeats of the same environmental note
    (EJS "no JS runtime", GVS PO token, SABR/DRM skips) log once.

    WHY THE VOCABULARY IS A SEPARATE MODULE: an earlier version of this filter
    was a substring list (_EXPECTED_YTDLP_MARKERS) tested against the message.
    It did not match the messages that actually dominated the live error ring
    — of 500 retained records, 411 were yt-dlp errors and the highest-volume
    shapes ("This channel does not have a streams tab", "Unable to download
    API page: HTTP Error 404", "Faça login para confirmar que você não é um
    bot") were NOT in the list, so ~324 of them reached the ring as errors
    while genuinely expected conditions. services.ytdlp_outcomes is a closed
    vocabulary with anchored markers and, crucially, an `unknown` default: a
    line it does not recognise stays a real error and keeps its log record.

    Errors are counted per code (process-wide) so an operator can tell "the
    filter is working" from "yt-dlp went quiet" — a bare drop would make both
    look identical, which is the honesty discipline 3f4ceb9 applied to
    /api/asr/runtime."""

    _seen_warnings: set[str] = set()  # class-level: shared across instances
    _expected_counts: "collections.Counter[str]" = collections.Counter()

    def __init__(self) -> None:
        pass

    def debug(self, msg):
        pass

    def warning(self, msg):
        text = str(msg)
        norm = _YTDLP_VIDEO_PREFIX_RE.sub("", text, count=1)
        if norm in self._seen_warnings:
            return
        self._seen_warnings.add(norm)
        logger.warning("yt-dlp: %s", text)

    def error(self, msg):
        text = str(msg)
        code = ytdlp_outcomes.classify(text)
        if ytdlp_outcomes.is_expected(code):
            type(self)._expected_counts[code] += 1
            logger.debug("yt-dlp expected error [%s]: %s", code, msg)
            return
        # NOT expected (or unrecognised): a real error. It keeps its log
        # record — the ring buffer's budget belongs to defects.
        logger.error("yt-dlp: %s", msg)

    @classmethod
    def expected_counts(cls) -> "collections.Counter[str]":
        """{outcome_code: times seen} since process start. A code absent here
        was never observed — which is NOT the same as a count of zero."""
        return collections.Counter(cls._expected_counts)


def ytdlp_console_logger():
    """A yt-dlp-compatible logger (debug/info/warning/error) for the `logger=`
    option that filters expected extractor failures from the console."""
    return _YtdlpConsoleLogger()


import functools as _functools
import shutil as _shutil


@_functools.lru_cache(maxsize=1)
def ytdlp_js_runtimes() -> dict[str, dict]:
    """Enable locally-available JS runtimes for yt-dlp's n-challenge solver.

    yt-dlp 2026.07.04 enables only deno by default; with no JS runtime the
    YouTube extractor warns on EVERY extract that formats may be missing
    (n-challenge unsolved). Node ships with any dev setup — enabling it
    recovers those formats. Deno stays first when installed (yt-dlp priority).
    """
    return {name: {} for name in ("deno", "node", "bun") if _shutil.which(name)}


    """A yt-dlp-compatible logger (debug/info/warning/error) for the `logger=`
    option that filters expected extractor failures from the console."""
    return _YtdlpConsoleLogger()


@contextlib.contextmanager
def guarded_youtube_dl(opts: dict[str, Any], kind: str = "background") -> Iterator[Any]:
    """Only supported way to construct YoutubeDL — one instance at a time.

    Instrumentation: this is the single funnel every YouTube metadata /
    download request passes through, so it is where the YouTube request
    count that yt_gate's rate-limit history reports comes from. One
    in-memory increment per context entry (see services.rl_counter) —
    the process-wide gate below already serializes construction, so this adds
    no contention and no DB write. yt-dlp's individual HTTP requests are
    NOT counted separately: they are invisible from here, and pretending
    otherwise would inflate the number a throttle is calibrated on.

    ``kind="interactive"`` marks the request as one a person is waiting on
    (the preview resolve). It draws from the USER rate-budget reserve and,
    at the lock, is granted ahead of any background walk that is already
    QUEUED. It never preempts a holder -- see ``_PriorityGate``.
    """
    import yt_dlp  # lazy: keeps yt-dlp (~0.5s) off the app import path

    assert_ytdlp_safe()
    safe = sanitize_ytdlp_opts(opts)
    safe.setdefault("logger", ytdlp_console_logger())
    safe.setdefault("js_runtimes", ytdlp_js_runtimes())
    rl_counter.count_request("youtube")
    with priority_lock.acquire(kind=kind):
        with _YTDLP_LOCK:
            with yt_dlp.YoutubeDL(safe) as ydl:
                yield ydl


@contextlib.contextmanager
def guarded_youtube_dl_channel(
    opts: dict[str, Any], kind: str = "background"
) -> Iterator[Any]:
    """Flat channel playlists — the background enumeration path.

    Counted like guarded_youtube_dl: one in-memory increment per context
    entry, no DB write.

    It passes through the SAME priority gate as the extract path, while still
    holding its own distinct lane lock. The separate lane lock alone could
    never order the two: a walk holding its own lock and re-offering work never
    contends with a waiting preview at all, which is exactly how the preview
    ended up waiting on a 21.2 s rate-budget pace instead. A walk that is
    ALREADY running is still never interrupted, and an interactive request
    that arrives while it runs takes the very next grant.
    """
    import yt_dlp  # lazy

    assert_ytdlp_safe()
    safe = sanitize_ytdlp_opts(opts)
    safe.setdefault("logger", ytdlp_console_logger())
    safe.setdefault("js_runtimes", ytdlp_js_runtimes())
    rl_counter.count_request("youtube")
    with priority_lock.acquire(kind=kind):
        with _YTDLP_CHANNEL_LOCK:
            with yt_dlp.YoutubeDL(safe) as ydl:
                yield ydl


#: Unchanged: two distinct real locks, as test_ytdlp_guard.py and the
#: import-time assert in ytdlp_hls.py both require.
YTDLP_EXTRACT_LOCK = _YTDLP_LOCK
YTDLP_CHANNEL_LOCK = _YTDLP_CHANNEL_LOCK


assert_ytdlp_safe()
out_never = sanitize_ytdlp_opts({
    "extractor_args": {"youtube": {"fetch_pot": ["auto"], "player_client": ["ios"]}},
})
assert out_never["extractor_args"]["youtube"]["fetch_pot"] in (["auto"], ["never"])
