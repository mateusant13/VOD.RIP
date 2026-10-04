"""Root conftest — registers the opt-in test-impact plugin (tests/impact_plugin.py).

The plugin is inert unless enabled with ``--impact`` or ``VODRIP_IMPACT=1``;
see tests/test_impact_selfcheck.py for what it proves.
"""

pytest_plugins = ["tests.impact_plugin"]

import pytest
import os

# archive_db's module-level self-check (25-40s DB-backed invariants on the
# REAL archive) is gated behind VODRIP_ARCHIVE_SELFCHECK=1 — pytest keeps it
# on; the app boots with it off.
os.environ.setdefault("VODRIP_ARCHIVE_SELFCHECK", "1")
# cookie_store's module self-check (~1.2s) is gated the same way.
os.environ.setdefault("VODRIP_COOKIE_SELFCHECK", "1")
# Never spawn detached daemons (background_server/worker_server) from test
# processes: the app's lifespan spawns them unconditionally, the heartbeat
# dedupe is DB-scoped (scratch DBs make it a no-op), and the orphan
# launcher defeats tree-kill — every `with TestClient(app)` leaked one
# daemon that lived forever, burning CPU at 30+ accumulated instances.
os.environ.setdefault("VODRIP_NO_DAEMONS", "1")

import shutil
import stat
import sys
import tempfile
import time
import warnings
from pathlib import Path


# Scratch is not always a directory: services/youtube_session.py:98 does
# mkstemp(prefix="yt_anon_", suffix=".txt"), so a leaked cookie jar is a
# regular FILE. shutil.rmtree is directory-only and used to run with
# ignore_errors=True, which turned that NotADirectoryError into silence —
# the leak was invisible and only showed up as another test's leftover.
def _remove_scratch_node(p: Path) -> None:
    """Delete ONE scratch node — directory or file — and let failures raise.

    Generic on purpose: the old defect was not a missing prefix, it was that
    the wiper only knew how to delete one KIND of node. Dispatching on the
    node type is safe for every existing prefix because all of them are
    mkdtemp scratch, which is exactly what rmtree handles; file-shaped
    entries only become reachable through prefixes that create them.

    lstat (not stat) is deliberate: a symlink/junction inside the temp dir
    must be UNLINKED, never recursed into, so a link planted in temp can
    never walk the wipe out to %APPDATA%/VOD.RIP or any other real data.
    rmtree refuses a symlink outright and unlink removes a link, so both
    branches stay inside the temp dir.
    """
    if stat.S_ISDIR(p.lstat().st_mode):
        shutil.rmtree(p)  # strict — no ignore_errors, so nothing is hidden
    else:
        p.unlink()


# The OS temp root as it was when this conftest was imported, i.e. BEFORE any
# test module got the chance to rebind it. tests/test_transcribe_shards.py:49
# sets tempfile.tempdir (plus TMP/TEMP) to a private scope dir so its shard
# leak-checks are hermetic, and collection imports EVERY test module before the
# first test runs — so by session end tempfile.gettempdir() points at that
# private dir and the reaper was scanning the wrong root, silently skipping the
# vodrip-tests-* dir THIS conftest mkdtemps in the real temp dir. The scratch
# the reaper exists to clean is created before the rebind, so the root it lives
# under must be captured before it too.
_TEMP_ROOT_AT_IMPORT = Path(tempfile.gettempdir()).resolve()


def _wipe_vodrip_scratch(min_age_s: float, root: Path | None = None) -> None:
    """Delete leftover test/scratch dirs in the system temp dir.

    Tests mkdtemp scratch dirs (vodrip-tests-*, ai-ask-tests-*, …) at
    MODULE IMPORT — every pytest process, even --collect-only, leaks one,
    and killed/interrupted runs never clean the transcribe shard dirs
    (vodrip-shards-*). Observed: 1,497 dirs / ~44 GB on a dev box.
    Live worker shards are never touched here (the worker reaps its own
    stale ones); only dirs untouched for min_age_s are removed.

    DISK-01: the wipe used to glob only ``vodrip-*`` — every non-vodrip
    test prefix (archive-*, ai-ask-*, kd_test/, …) leaked forever. The
    prefix list below mirrors every mkdtemp(prefix=…) in backend/tests
    today; new test scratch MUST use the ``vodrip-`` prefix so the generic
    rule covers it (the list is the safety net for legacy names).

    DISK-01b: the mkstemp families (yt_anon_*) are FILES, so the wiper now
    deletes any node type and SURFACES what it could not remove (warn after
    a retry) instead of swallowing the error.

    ``root`` defaults to the CURRENT gettempdir() so a test can point the wipe
    at its own tmp_path (test_exhaust_disk_scratch does exactly that). The
    session fixture passes _TEMP_ROOT_AT_IMPORT instead, because the root that
    holds this session's own scratch is the one captured at import — see the
    comment there.

    SAFETY: iterates the temp dir's own children only, and never follows a
    link (see _remove_scratch_node). The real data root is
    %APPDATA%\\VOD.RIP (override VODRIP_APP_DATA), which is not under the
    temp dir, and the prefix/name allowlists below match nothing there."""
    tdir = Path(root) if root is not None else Path(tempfile.gettempdir()).resolve()
    now = time.time()
    stuck = []
    for p in sorted(tdir.iterdir()):
        name = p.name
        if not (
            name.startswith(_SCRATCH_PREFIXES) or name in _SCRATCH_NAMES
        ):
            continue
        if name.startswith("vodrip-shards-"):
            continue  # worker-owned, transient while a job runs
        try:
            age_ok = now - p.lstat().st_mtime >= min_age_s
        except OSError:
            continue  # vanished between iterdir() and lstat()
        if not age_ok:
            continue
        # Retry once: on Windows a scratch node can be transiently locked by
        # a process that is still exiting. A second failure is a real leak and
        # is reported below rather than lost.
        for attempt in (1, 2):
            try:
                _remove_scratch_node(p)
                break
            except OSError:
                if not os.path.lexists(p):
                    break  # someone else got there first — not a leak
                if attempt == 2:
                    stuck.append(name)
    if stuck:
        warnings.warn(
            "conftest scratch wipe could not remove %d node(s) after a "
            "retry: %s" % (len(stuck), ", ".join(stuck[:10])),
            UserWarning,
            stacklevel=2,
        )


# Every scratch dir prefix tests create in the system temp dir (mkdtemp).
_SCRATCH_PREFIXES = (
    "vodrip-", "ai-ask-tests-", "archive-chat-group-", "archive-enrich-v2-",
    "archive-jobs-api-", "archive-jobs-retry-", "archive-phonetic-",
    "archive-ranking-", "archive-robust-", "archive-search-filters-",
    "archive-semantic-", "archive-semantic-real-", "archive-spam-collapse-",
    "archive-transcribe-download-", "chat-backfill-jobs-",
    "chat-full-history-", "chat-full-history-real-", "chat-txt-export-",
    "content-dedup-test-", "dash_clip_", "dash_window_hls_test_",
    "gv_deep_", "gv_par_", "hls_clip_", "hook-job-media-",
    "impact-selfcheck-", "ingest-chat-backfill-", "instant-preview-",
    "instant-preview-app-", "instant-preview-cache-", "instant-preview-data-",
    "kind-rebuild-", "kind-rebuild-alter-", "persist-fixes-", "prefetch-",
    "prefetch-app-", "prefetch-cache-", "prefetch-data-", "preflight_adopt_",
    "prog-head-grace-", "prog_clip_", "retention-test-",
    "scheduler-yt-chat-backfill-", "search-titles-", "tk-local-",
    "transcribe-cross-", "transcribe-cross-app-", "transcript-fix-",
    "transcript-fix-app-", "transcript-pipeline-", "transcript-pipeline-app-",
    "twitch-clip-chat-", "watchdog-test-", "window_hls_test_",
    "ws1-arch-", "ws1-queue-", "yt-captions-test-", "yt-display-names-", "yt-transcribe-", "twitch-transcribe-", "kick-transcribe-", "bw-a4-", "bw-auth-", "bw-crash-",
    "yt-gate-", "yt-policy-test-", "yt_anon_", "ytdlp_aud_", "ytdlp_seg_",
)
# Bare scratch dir names (not mkdtemp-prefixed) in the temp dir.
# Deliberately NOT "VOD.RIP": in the SYSTEM temp dir that is the running
# app's own tree (routers/disk.py:79, services/updater.py:169,
# routers/live.py:1049), and in a test's tmp_path it is the autouse
# _isolated_download_appdata dir. Never scratch — see
# test_wipe_leaves_bare_vodrip_dir_alone.
_SCRATCH_NAMES = ("kd_test", "vodrip-search-lab")


# Module-level sqlite connections the tests create stay OPEN for the life of
# the pytest process (archive_db: one shared write conn plus one read conn per
# thread that called query(); cookie_store: one lazy module conn). On Windows an
# open file cannot be unlinked, so the scratch dir holding archive.db /
# cookies.db could not be deleted at session end — the reaper below reported it
# as unremovable and one vodrip-tests-* dir leaked per run. Releasing the
# handles is the fix; the warning stays because a handle from a live foreign
# process is still a real leak worth surfacing.
_DB_MODULES = ("services.archive_db", "services.cookie_store")


def _close_scratch_db_handles() -> int:
    """Close the sqlite connections this process holds. Returns the count.

    Resolves the modules through sys.modules on purpose: importing a DB module
    that no test touched would CREATE the very scratch DB the reaper is about
    to delete. A module that was never loaded holds no handle.
    """
    closed = 0
    for name in _DB_MODULES:
        mod = sys.modules.get(name)
        closer = getattr(mod, "close_connections", None) if mod else None
        if closer is None:
            continue
        try:
            closed += int(closer() or 0)
        except Exception:  # noqa: BLE001 — teardown must not mask the wipe
            pass
    return closed


@pytest.fixture(scope="session", autouse=True)
def _reap_vodrip_scratch():
    """Stop the temp-dir accumulation: wipe stale scratch at session start,
    and every scratch dir this session created at session end.

    ORDER at session end is load-bearing: the DB handles MUST be released
    before the wipe, or the reaper can only warn about the dirs this very
    session locked. The close lives in THIS fixture's teardown (not in a
    sibling session fixture in tests/conftest.py) so the two steps cannot be
    reordered by pytest's fixture-finalization order: they are two statements
    in one function, in the order the disk requires.

    Both wipes target _TEMP_ROOT_AT_IMPORT, not the live gettempdir(): by the
    time this teardown runs, a test module may have rebound tempfile.tempdir
    to a private dir, and the scratch this session created is under the root
    captured at import. (See _TEMP_ROOT_AT_IMPORT.)
    """
    _wipe_vodrip_scratch(min_age_s=6 * 3600.0,
                         root=_TEMP_ROOT_AT_IMPORT)  # stale from dead procs
    yield
    _close_scratch_db_handles()
    _wipe_vodrip_scratch(min_age_s=0.0,
                         root=_TEMP_ROOT_AT_IMPORT)  # this session's own


def pytest_collection_modifyitems(config, items):
    """Mark tests in ``*_real*.py`` files as ``real`` (live network/env).

    Real-network suites (test_*_real*.py) exercise YouTube/Twitch/Kick/CDN
    endpoints and depend on live tokens, bot-gate state and channel uptime.
    They are expensive (~15min for the full set) and fail for environmental
    reasons that are not regressions. pytest.ini's ``addopts = -m "not real"``
    skips them by default; run them explicitly with ``pytest -m real``.

    Explicit invocation of a *_real* file is itself the opt-in: when every
    collected item lives in a *_real* file, the default 'not real'
    deselection is flipped to 'real' so `pytest tests/test_foo_real.py`
    collects the tests instead of exiting 5 with '0 tests (1 deselected)'.
    Directory/merged runs keep the default opt-in behavior.
    """
    for item in items:
        if item.path.name.endswith("_real.py"):
            item.add_marker("real")
    if items and all(item.path.name.endswith("_real.py") for item in items):
        # Runs before pytest's own deselect_by_mark (conftest hooks are
        # registered later), so this override takes effect for this run only.
        config.option.markexpr = "real"
