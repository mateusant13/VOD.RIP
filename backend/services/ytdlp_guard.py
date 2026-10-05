"""Single gate for all yt-dlp — blocks getpot_wpc (Chrome); allows bgutil PO plugin."""

from __future__ import annotations

import collections
import contextlib
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Iterator, Optional

from services import rl_counter
from services import ytdlp_env  # noqa: F401
from services import ytdlp_outcomes

logger = logging.getLogger(__name__)

_YTDLP_LOCK = threading.Lock()
# Why no priority/bounded acquire here: a plain Lock has no priority, and a
# timeout would break legitimate long downloads (a 2-hour VOD legitimately
# holds this lock for minutes; a bounded acquire would fail preview extracts
# that merely wait behind it). Priority for live playback is instead handled
# structurally — live sessions never touch yt-dlp (pure CDN fetches) and run
# on their own LIVE_EXECUTOR — and the pathological holder (a 0 B/s stalled
# download) is reaped by DownloadManager's stall watchdog (STALL_WATCHDOG_SEC
# = 90s), which frees this lock with a clear error instead of holding it
# forever.
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


# --- remote challenge solver (yt-dlp "remote components") -------------------
#
# WHAT THIS IS. YouTube gates its adaptive (audio-only) streams behind an
# "n challenge" that must be solved by EXECUTING JavaScript. yt-dlp ships the
# `core` half of the solver locally but NOT the `lib` half, so out of the box
# the challenge cannot be solved and every audio-only itag (140/251) drops out
# of the format list. The observable symptom was that the transcription lane
# fell through `bestaudio` to muxed itag 18 and fed h264 video to a speech
# recogniser (see archive_ytdlp._AUDIO_ONLY_FORMAT_SPEC).
#
# THE SECURITY CONSEQUENCE, PLAINLY: turning this on makes yt-dlp DOWNLOAD
# JavaScript from a remote source (a GitHub release asset) and EXECUTE it,
# with the local JS runtime (deno/node), during challenge solving. That is
# remote code execution by design. It is bounded — the asset URL is pinned to
# a yt-dlp-pinned version tag, and yt-dlp verifies the downloaded script
# against a hash that is vendored INSIDE the installed yt-dlp
# (jsc/_builtin/vendor/_info.py HASHES), refusing it on mismatch — but it is
# a real, permanent trust grant, not a toggle we can pretend is free.
#
# THE KNOB. `VODRIP_YT_EXECUTE_REMOTE_CHALLENGE_SOLVER` reads as what it does.
# It is deliberately named "execute" so nobody enables it by accident. Set it
# to 1/true/yes/on to allow; 0/false/no/off (or unset — the default) forbids.
# ONE documented way to turn it off: set it to `0`. That restores the previous
# behaviour exactly: no remote fetch, challenge unsolved, audio-only formats
# absent, and the transcription lane requeues instead of downloading video.
#
# WHY IT LIVES HERE AND NOT IN A CALLER. `sanitize_ytdlp_opts` is the function
# that already decides `fetch_pot`, and it runs on the single guarded egress
# seam, so a future refactor cannot quietly drop the option: the regression
# test asserts the EFFECTIVE opts at the seam, not a constant.

# Only the pinned GitHub release asset. `ejs:npm` would pull npm packages at
# solve time; it is deliberately NOT enabled — one source, one grant.
EJS_REMOTE_SOLVER_COMPONENTS = ("ejs:github",)

#: The two cache entries `_web_release_source` can leave behind, named after
#: yt-dlp's own `ScriptType` values (verified: ScriptType.LIB.value == 'lib',
#: ScriptType.CORE.value == 'core'; `cache.py::_get_cache_fn` writes
#: `<root>/<section>/<key>.json`). Both are remote-fetched JavaScript and both
#: are read back by `_cached_source`, which does not consult
#: `remote_components` — so revoking the grant has to remove both.
_EJS_CACHED_SOLVER_FILES = ("core.json", "lib.json")

_EXECUTE_REMOTE_SOLVER_ENV = "VODRIP_YT_EXECUTE_REMOTE_CHALLENGE_SOLVER"

_EJS_STATE_REPORTED: bool = False
_EJS_PURGE_DONE: bool = False


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def execute_remote_challenge_solver() -> bool:
    """Whether yt-dlp may download+execute the remote n-challenge solver.

    Default False. The owner authorised enabling it, but it stays a NAMED,
    reversible setting rather than a hardcoded default, so "is this box
    executing remote JS?" is one env var to answer.
    """
    return _env_flag(_EXECUTE_REMOTE_SOLVER_ENV, default=False)


def _ejs_vendor_manifest() -> dict:
    """Pinned solver version + expected script hash from the INSTALLED yt-dlp.

    Read from the package so "what code ran" is answerable later: the version
    tag yt-dlp requests and the sha3-512 it verifies the download against both
    come from here, not from us.
    """
    try:
        from yt_dlp.extractor.youtube.jsc._builtin.vendor import _info

        return {
            "version": _info.VERSION,
            "lib_min_hash": _info.HASHES.get("yt.solver.lib.min.js", ""),
        }
    except Exception:  # pragma: no cover - yt-dlp internals moved
        return {}


def _report_ejs_state_once() -> None:
    """Log the resolved component/version/hash exactly once per process.

    This is the audit line: it names the source, the pinned version and the
    hash that gates the download, so a log search answers "did this box
    execute remote JS, and which bytes?".
    """
    global _EJS_STATE_REPORTED
    if _EJS_STATE_REPORTED:
        return
    _EJS_STATE_REPORTED = True
    if not execute_remote_challenge_solver():
        logger.info(
            "yt-dlp remote challenge solver DISABLED (%s not set) — n-challenge "
            "stays unsolved, audio-only formats absent",
            _EXECUTE_REMOTE_SOLVER_ENV,
        )
        return
    man = _ejs_vendor_manifest()
    logger.warning(
        "yt-dlp remote challenge solver ENABLED: yt-dlp will DOWNLOAD and "        "EXECUTE JavaScript from github.com/yt-dlp/ejs releases (v%s) to solve "
        "YouTube's n-challenge. Expected lib.min.js sha3-512=%s. Turn off with "
        "%s=0.",
        man.get("version", "unknown"),
        (man.get("lib_min_hash") or "unknown")[:16],
        _EXECUTE_REMOTE_SOLVER_ENV,
    )


def _apply_remote_challenge_solver(out: dict[str, Any]) -> dict[str, Any]:
    """Set (or clear) `remote_components` on the effective yt-dlp opts.

    Always writes the key — enabled OR disabled — so a caller that previously
    set `remote_components` cannot smuggle it past the seam, and so the
    disabled state is explicit rather than "whatever the caller passed".

    WHEN DISABLED, IT ALSO PURGES THE CACHED SOLVER SCRIPTS. This is not
    decoration — it is what makes the documented revert real. yt-dlp caches
    BOTH halves of the challenge solver under
    `<cachedir>/challenge-solver/<script_type>.json` (ejs.py
    `_web_release_source` -> `ie.cache.store`), and its `_cached_source` reads
    them WITHOUT consulting `remote_components`. So `remote_components=[]`
    alone stops the FETCH but not the USE: a box where the solver ran once
    would keep executing the cached scripts, and "I set it to 0" would be a
    false claim. Purge makes off mean off.

    BOTH files, not just `lib.json`. `_iter_script_sources` yields
    PYPACKAGE -> CACHE -> BUILTIN -> WEB, so a cached entry outranks the
    vendored one, and `_web_release_source` is what wrote BOTH `core.json`
    and `lib.json` (it is called per `script_type`). Purging only `lib.json`
    would leave the box loading and executing a cached remote `core.js` while
    the operator was told the grant was fully off. The behavioural symptom
    hides it — the vendored `lib` stubs are 245-byte shims that need the npm
    package, so the n-challenge stays unsolved either way — which is exactly
    why the security claim has to be checked against the source order rather
    than against "did the formats come back".
    """
    _report_ejs_state_once()
    if execute_remote_challenge_solver():
        out["remote_components"] = list(EJS_REMOTE_SOLVER_COMPONENTS)
    else:
        out["remote_components"] = []
        _purge_cached_solver_script(out.get("cachedir"))
    return out


def _purge_cached_solver_script(cachedir: Any) -> None:
    """Delete yt-dlp's cached n-challenge solver script, best-effort.

    Only ever removes the TWO known files (`challenge-solver/core.json` and
    `challenge-solver/lib.json`) inside the cache dir yt-dlp itself would use,
    and nothing else — the same cache dir holds the po_token (`sigfuncs`) and
    the POT, and deleting those would break authentication. Never raises: a
    failure here must not break yt-dlp, it is logged and the run continues.
    Guarded to run once per process so a per-extract call does not stat the
    disk on every request.
    """
    global _EJS_PURGE_DONE
    if _EJS_PURGE_DONE:
        return
    _EJS_PURGE_DONE = True
    root: Optional[Path] = None
    try:
        if isinstance(cachedir, str) and cachedir:
            root = Path(cachedir)
        else:
            cache_home = os.environ.get("XDG_CACHE_HOME", "") or (
                os.path.join(Path.home(), ".cache")
            )
            root = Path(cache_home) / "yt-dlp"
        for name in _EJS_CACHED_SOLVER_FILES:
            target = root / "challenge-solver" / name
            if target.is_file():
                target.unlink()
                logger.info(
                    "yt-dlp: purged cached n-challenge solver script %s (solver "
                    "disabled; the grant is fully off, not just unfetched)",
                    target,
                )
    except OSError as exc:
        logger.warning("yt-dlp: could not purge cached solver script: %s", exc)


def sanitize_ytdlp_opts(opts: dict[str, Any]) -> dict[str, Any]:
    """Strip blocked keys; enable bgutil fetch_pot when the POT server is up.

    Also decides `remote_components` — the option that lets yt-dlp download and
    execute the remote n-challenge solver. See the block comment above it; the
    knob is VODRIP_YT_EXECUTE_REMOTE_CHALLENGE_SOLVER (=0 reverts).
    """
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
    return _apply_remote_challenge_solver(out)


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
def guarded_youtube_dl(opts: dict[str, Any]) -> Iterator[Any]:
    """Only supported way to construct YoutubeDL — one instance at a time.

    Instrumentation: this is the single funnel every YouTube metadata /
    download request passes through, so it is where the YouTube request
    count that yt_gate's rate-limit history reports comes from. One
    in-memory increment per context entry (see services.rl_counter) — the
    process-wide lock below already serializes construction, so this adds
    no contention and no DB write. yt-dlp's individual HTTP requests are
    NOT counted separately: they are invisible from here, and pretending
    otherwise would inflate the number a throttle is calibrated on.
    """
    import yt_dlp  # lazy: keeps yt-dlp (~0.5s) off the app import path

    assert_ytdlp_safe()
    safe = sanitize_ytdlp_opts(opts)
    safe.setdefault("logger", ytdlp_console_logger())
    safe.setdefault("js_runtimes", ytdlp_js_runtimes())
    rl_counter.count_request("youtube")
    with _YTDLP_LOCK:
        with yt_dlp.YoutubeDL(safe) as ydl:
            yield ydl


@contextlib.contextmanager
def guarded_youtube_dl_channel(opts: dict[str, Any]) -> Iterator[Any]:
    """Flat channel playlists — separate lock so preview segment yt-dlp can't starve lists.

    Counted like guarded_youtube_dl: one in-memory increment per context
    entry, no DB write.
    """
    import yt_dlp  # lazy

    assert_ytdlp_safe()
    safe = sanitize_ytdlp_opts(opts)
    safe.setdefault("logger", ytdlp_console_logger())
    safe.setdefault("js_runtimes", ytdlp_js_runtimes())
    rl_counter.count_request("youtube")
    with _YTDLP_CHANNEL_LOCK:
        with yt_dlp.YoutubeDL(safe) as ydl:
            yield ydl


YTDLP_EXTRACT_LOCK = _YTDLP_LOCK
YTDLP_CHANNEL_LOCK = _YTDLP_CHANNEL_LOCK

assert_ytdlp_safe()
out_never = sanitize_ytdlp_opts({
    "extractor_args": {"youtube": {"fetch_pot": ["auto"], "player_client": ["ios"]}},
})
assert out_never["extractor_args"]["youtube"]["fetch_pot"] in (["auto"], ["never"])
