"""Pytest fixtures — isolate download JSON + archive/cookie DBs from real %APPDATA%."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import warnings
from pathlib import Path

import pytest

from download_test_utils import purge_download_manager

# Isolate the archive + cookie stores BEFORE any test module imports the app:
# services.archive_db runs a module-level self-check that opens the DB on
# import (and services.cookie_store similarly), so without this the first
# test module in a merged run (alphabetically test_api_integration.py via
# `from app import app`) would bind the shared connection to the REAL
# %APPDATA%/VOD.RIP/archive.db and the kind-column migration + self-check
# would run on user data. Per-test modules may still override the env with
# their own scratch DB; this guarantees they never fall through to the real
# one.
_TMP = Path(tempfile.mkdtemp(prefix="vodrip-tests-"))
# The OS scratch root every test mkdtemp dir (this _TMP, instant-preview-*,
# prefetch-data-, …) is a direct child of — the containment bound for
# _reset_preview_registry's registry unlink below.
_SCRATCH_ROOT = Path(tempfile.gettempdir()).resolve()
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")
os.environ["VODRIP_COOKIE_DB"] = str(_TMP / "cookies.db")
# Isolate the settings/history/queue JSON + cookie/whisper dirs the same way:
# app.py constructs DownloadManager/SettingsManager at import, before any
# per-test fixture can run, so an env override (not a patch) is the only
# thing that keeps those import-time singletons off the REAL %APPDATA%.
os.environ["VODRIP_APP_DATA"] = str(_TMP / "VOD.RIP")

# Pin the routed cache root (WS-8: cache_dir setting / biggest-fixed-drive
# auto pick) to scratch. Without this, the auto pick on a dev machine
# resolves to a REAL data drive (e.g. I:\) and any test that touches the
# whisper/yt-dlp/preview/embed cache paths would create dirs there. Per-cache
# env knobs (VODRIP_WHISPER_CACHE, VODRIP_EMBED_CACHE) still win; tests that
# need the real auto-pick behavior delenv VODRIP_CACHE_DIR.
os.environ.setdefault("VODRIP_CACHE_DIR", str(_TMP / "cache"))

# Same for the data root (Settings > Storage data-disk pick): the auto
# default resolves to the FASTEST real drive (fastest_disk -> PowerShell
# probe), so without this pin any test touching data_dir()/preview_root()
# would stall on a real probe and create dirs on a real data drive. Tests
# that exercise the auto behavior delenv VODRIP_DATA_DIR and patch the
# disk inventory (see test_disk_tiering.py). FORCE-set, not setdefault:
# _reset_preview_registry unlinks preview_root()/sessions.json per test,
# and preview_root() resolves through this env; a shell that pre-seeded
# VODRIP_DATA_DIR (the documented portable knob) must NOT redirect that
# unlink onto a real kd_preview registry. monkeypatch.delenv in the
# auto-behavior tests still wins (function scope restores afterwards).
os.environ["VODRIP_DATA_DIR"] = str(_TMP / "data")

# Snapshot of the REAL %APPDATA% archive.db taken here, before any test
# module import can run the archive/cookie-store self-checks. The cookie
# store's real file is the same archive.db (VODRIP_COOKIE_DB unset →
# appdata/archive.db), so one hash covers both stores. test_cookie_bridge.py
# asserts the file is still byte-identical at the end of a merged run.
def _sha256_or_none(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


REAL_APPDATA_DB_SHA256 = _sha256_or_none(
    Path(os.environ.get("APPDATA", "")) / "VOD.RIP" / "archive.db"
)


@pytest.fixture(autouse=True)
def _restore_vodrip_env():
    """Safety net: revert any VODRIP_* env var a test body leaves behind to
    its value at the start of that test.

    Kills the whole class of os.environ leaks (a *_real* suite hard-setting
    VODRIP_WHISPER_DEVICE/cache dirs and forgetting to restore). Module-
    scoped env fixtures (scratch-DB rebinds) are unaffected: they set up
    before this function-scoped fixture snapshots, so their values are part
    of the baseline and survive each per-test restore. Collection-time
    module-level writes are fixed at the root (moved into fixtures), so they
    never appear here."""
    saved = {k: v for k, v in os.environ.items() if k.startswith("VODRIP_")}
    yield
    for k in [k for k in os.environ if k.startswith("VODRIP_")]:
        os.environ.pop(k, None)
    os.environ.update(saved)


__all__ = ["purge_download_manager", "REAL_APPDATA_DB_SHA256"]


@pytest.fixture(autouse=True)
def _isolated_download_appdata(monkeypatch, tmp_path):
    app_dir = tmp_path / "VOD.RIP"
    app_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("services.settings._get_appdata_dir", lambda: app_dir)
    yield app_dir


@pytest.fixture
def download_test_counter():
    count = {"n": 0}

    def tick(mgr) -> None:
        count["n"] += 1
        if count["n"] % 10 == 0:
            purge_download_manager(mgr)

    return tick


# ── process-global isolation (sweep-tests §c) ───────────────────────────────
# The order-dependent failures in a merged run are all the same shape: a
# module-level singleton that production code shares across requests, mutated
# by one test file and never rewound. Each fixture below rewinds ONE such
# family before every test, so no file has to know which neighbour ran first.
#
# Rules for this section:
#   * function scope, autouse, cheap: nothing is imported unless the module is
#     already in sys.modules (an un-imported singleton has nothing to reset);
#   * clear in place, never rebind, unless rebind is the only way to undo a
#     module-attribute assignment a polluter made;
#   * guard every access with getattr so a refactor that moves/renames state
#     degrades to a no-op here rather than breaking the whole suite.

# Tripwire: every reset below degrades to a no-op when its target is renamed
# or moved (the getattr guards) — safe for the suite, but a SILENT no-op
# re-arms exactly the order-dependence this section exists to kill. Warn once
# per missing target instead of raising.
_TRIPWIRES: set[str] = set()


def _isolation_tripwire(key: str, message: str) -> None:
    if key not in _TRIPWIRES:
        _TRIPWIRES.add(key)
        warnings.warn(f"test-isolation: {message}", RuntimeWarning, stacklevel=2)


def _module_stem(request) -> str:
    """Bare module name (``test_yt_gate``). rpartition keeps the module gates
    below working if backend/tests ever gains an __init__.py (name becomes
    ``tests.test_yt_gate``); a bare equality would silently no-op them."""
    return request.node.module.__name__.rpartition(".")[2]


@pytest.fixture(autouse=True)
def _reset_ai_ask_rate_limit():
    """app._ask_hits is the /api/ai/ask limiter (10 req/min per client IP,
    app.py:1080-1100). The ASGITransport client presents as one fixed IP, so
    any file that issues >10 ask requests before test_ai_ask.py runs leaves
    the bucket full and its last ~5 tests answer 429 instead of 200/401/502.
    Clear in place: _ask_rate_ok writes into the same dict object."""
    app_mod = sys.modules.get("app")
    hits = getattr(app_mod, "_ask_hits", None) if app_mod else None
    lock = getattr(app_mod, "_ask_rate_lock", None) if app_mod else None
    if not isinstance(hits, dict):
        if app_mod is not None:
            _isolation_tripwire(
                "ai_ask._ask_hits", "app._ask_hits missing — rate-limit reset disabled"
            )
        return
    if lock is not None:
        with lock:
            hits.clear()
    else:
        hits.clear()


@pytest.fixture(autouse=True)
def _reset_preview_registry():
    """Undo the two process-global leaks of the preview singleton.

    1. get_session rebound: test_live_preview_ux_guard._live_session assigns
       ``session_mod.get_session = lambda sid: session`` at module level with
       no restore. Every later module whose production path reads the
       module-global (open_replay_hls_proxy session.py:3919, resolve_upstream
       :5020) then gets the twitch xqc.m3u8 session for ANY id —
       'No archive for this preview session' / 'Unknown preview resource'.
       Rebind ONLY that name (the repair test_live_session_lifecycle.py:37-42
       does locally) — see the blast-radius comment at the rebind site.

    2. the persisted registry: _manager._sessions plus sessions.json under
       preview_root(). Rows reach the file through _persist_sessions() (fired
       by delete_session/create_session), and a later file that builds a
       FRESH PreviewManager — test_session_lru_e2e.py:18 — reloads them in
       __init__ (session.py:475 → :522), inflating its hand-counted dict:
       'assert 15 == 13'. Proven pair:
           pytest tests/test_preview_e2e.py tests/test_session_lru_e2e.py
    """
    session_mod = sys.modules.get("services.preview.session")
    if session_mod is None:
        return
    manager = getattr(session_mod, "_manager", None)
    if manager is None:
        _isolation_tripwire(
            "preview._manager",
            "services.preview.session._manager missing — registry reset disabled",
        )
        return
    # Narrow rebind: get_session is the ONLY preview alias with a proven
    # emitter (test_live_preview_ux_guard.py:76; repo-wide grep finds no test
    # reassigning delete_session/create_session/create_live_session/
    # peek_session/_cleanup_stale_sessions). Rebinding those too would
    # silently clobber any future intentional module-level patch of them
    # before every test — breadth loses to blast radius. Unconditional
    # setattr: the old `is not bound` identity guard was tautologically True
    # (getattr on an instance fabricates a fresh bound-method object).
    bound = getattr(manager, "get_session", None)
    if bound is not None:
        session_mod.get_session = bound
    else:
        _isolation_tripwire(
            "preview.get_session",
            "PreviewManager.get_session missing — module rebind disabled",
        )
    lock = getattr(manager, "_lock", None)
    sessions = getattr(manager, "_sessions", None)
    if isinstance(sessions, dict):
        if lock is not None:
            with lock:
                sessions.clear()
        else:
            sessions.clear()
    else:
        _isolation_tripwire(
            "preview._sessions",
            "PreviewManager._sessions missing — in-memory registry clear disabled",
        )
    # Threat model (same as the VODRIP_DATA_DIR force-pin above): the
    # registry file lives under preview_root() <- data_dir() <- env.
    # The pin is the first lock; this containment check is the second —
    # only ever unlink a kd_preview/sessions.json that resolves inside
    # the OS scratch root every test mkdtemp dir lives in (this conftest's
    # _TMP and peers like prefetch-data- are all direct children of it),
    # never a real data drive / appdata registry. resolve() so a symlinked
    # or differently-cased env value can't smuggle a real path through.
    try:
        registry = manager._registry_path()
    except Exception:  # noqa: BLE001 — refactor moves the seam -> no-op
        registry = None
        _isolation_tripwire(
            "preview.registry_path",
            "PreviewManager._registry_path() unusable — persisted-registry reset disabled",
        )
    # A containment mismatch alone is NOT the rename signal: tests may
    # legitimately redirect preview_root() (test_preview_residual_fixes
    # monkeypatches it to tmp_path), and skipping those is correct.
    if registry is not None and registry.name == "sessions.json":
        try:
            parents = registry.resolve().parents
        except OSError:
            parents = ()
        if (
            registry.parent.name == "kd_preview"
            and _SCRATCH_ROOT in parents
        ):
            try:
                registry.unlink()
            except OSError:
                pass


@pytest.fixture(autouse=True)
def _reset_resolve_caches():
    """Hygiene for the module-level caches the extract/InnerTube/subtitle
    paths share across tests: ytdlp_hls' five _EXTRACT_* dicts,
    youtube_innertube's _LAST_PLAYABILITY / _ORIGINAL_META_CACHE, and the
    routers/subtitles LRU + single-flight table.

    Precedent per family: test_extract_swr_metadata.py:49-61,
    test_youtube_gate_classification.py:25-30,
    test_subtitles_caption_first.py:144-150 — all three clear their own state
    and rely on nobody else's writes landing between them and the assertion.
    Stale entries here are verdicts ('this video is 360p', 'this URL is
    fatal', 'abc123XYZ has no captions'), so a leftover makes the next file's
    assertion read someone else's fixture. Note: this is defence-in-depth —
    the info_video family's real cause was sys.modules instance divergence in
    test_snapshot_key_unify.py, fixed there.
    """
    hls = sys.modules.get("services.ytdlp_hls")
    if hls is not None:
        # Clear under the prod writers' lock: every _EXTRACT_* mutation holds
        # _EXTRACT_CACHE_LOCK (ytdlp_hls.py:188; writers :354-357, :1142-1146,
        # :1193-1194, :1336-1347), and a daemon warm/extract thread from an
        # earlier test can still be inside those writers at this fixture's
        # setup moment.
        hls_lock = getattr(hls, "_EXTRACT_CACHE_LOCK", None)
        for name in (
            "_EXTRACT_INFO_CACHE",
            "_EXTRACT_INFLIGHT",
            "_EXTRACT_FATAL_CACHE",
            "_EXTRACT_NEG_CACHE",
            "_EXTRACT_SWR_INFLIGHT",
        ):
            table = getattr(hls, name, None)
            if not isinstance(table, dict):
                _isolation_tripwire(
                    f"hls.{name}", f"services.ytdlp_hls.{name} missing — cache reset disabled"
                )
                continue
            if hls_lock is not None:
                with hls_lock:
                    table.clear()
            else:
                table.clear()
    it = sys.modules.get("services.youtube_innertube")
    if it is not None:
        meta_lock = getattr(it, "_ORIGINAL_META_LOCK", None)
        for name in ("_LAST_PLAYABILITY", "_ORIGINAL_META_CACHE"):
            table = getattr(it, name, None)
            if not isinstance(table, dict):
                _isolation_tripwire(
                    f"it.{name}", f"services.youtube_innertube.{name} missing — cache reset disabled"
                )
                continue
            # _ORIGINAL_META_CACHE is written under _ORIGINAL_META_LOCK
            # (youtube_innertube.py:1370-1382). _LAST_PLAYABILITY is unlocked
            # in prod itself (:601-610); piggybacking the module's meta lock
            # costs nothing and stays correct if that ever gains a guard.
            if meta_lock is not None:
                with meta_lock:
                    table.clear()
            else:
                table.clear()
    subs = sys.modules.get("routers.subtitles")
    if subs is not None:
        cache = getattr(subs, "_subs_cache", None)
        data = getattr(cache, "_data", None)
        if isinstance(data, dict):
            lock = getattr(cache, "_lock", None)
            if lock is not None:
                with lock:
                    data.clear()
            else:
                data.clear()
        inflight = getattr(subs, "_inflight", None)
        if isinstance(inflight, dict):
            i_lock = getattr(subs, "_inflight_lock", None)
            if i_lock is not None:
                with i_lock:
                    inflight.clear()
            else:
                inflight.clear()


@pytest.fixture(autouse=True)
def _reset_yt_gate_jobs(request):
    """Leftover archive_jobs rows make test_yt_gate's claim assertions read
    someone else's job: test_claim_skips_youtube_jobs_during_gate (line 123)
    asserts _claim_next_job() returns ITS twitch row, and any claimable row
    an earlier file enqueued into this scratch DB (the sweep report saw a
    transcribe-twitch-__events_hook__ row) wins the ORDER BY instead. The
    module's own test_claim_clears_when_gate_lifts already does this DELETE
    inline (test_yt_gate.py:146); doing it at setup for every test in the
    module is the same wipe, applied before the claim instead of after.
    Scoped to the module on purpose: rows in other files' DBs are theirs."""
    if _module_stem(request) != "test_yt_gate":
        return
    db = sys.modules.get("services.archive_db")
    if db is None:
        _isolation_tripwire(
            "gate.archive_db",
            "services.archive_db not loaded — yt_gate job wipe disabled",
        )
        return
    # Fragility note: execute() wipes archive_jobs in whatever DB
    # VODRIP_ARCHIVE_DB names at THIS instant (archive_db re-keys its
    # connection on the env path). Contained today — test_yt_gate pins its
    # own scratch DB at import (:13-14) and nothing rebinds the env
    # mid-module — but a future module-scoped rebind here would silently
    # retarget or miss the wipe.
    try:
        db.execute("DELETE FROM archive_jobs")
    except Exception:  # noqa: BLE001 — table not created yet in this DB
        pass


@pytest.fixture(autouse=True)
def _ensure_vad_scratch(request):
    """test_vad_batched's TTS helper writes into its import-time
    ``_TMP`` (vodrip-vad-*) via PowerShell System.Speech; if any scratch
    sweep in the merged run has rmtree'd it by the time the file runs,
    SetOutputToWaveFile raises DirectoryNotFoundException (the sweep report's
    failure). The wiper itself was not pinned down — the root conftest only
    wipes at session start (6 h age) and session end — so recreate the dir
    instead of hunting it. makedirs(exist_ok=True) is a no-op standalone."""
    if _module_stem(request) != "test_vad_batched":
        return
    mod = sys.modules.get("test_vad_batched") or sys.modules.get("tests.test_vad_batched")
    tmp = getattr(mod, "_TMP", None) if mod is not None else None
    if tmp is None:
        _isolation_tripwire(
            "vad._TMP", "test_vad_batched._TMP unavailable — scratch recreate disabled"
        )
        return
    os.makedirs(tmp, exist_ok=True)


