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
import tempfile
import time
from pathlib import Path


def _wipe_vodrip_scratch(min_age_s: float) -> None:
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
    rule covers it (the list is the safety net for legacy names)."""
    tdir = Path(tempfile.gettempdir())
    now = time.time()
    for p in tdir.iterdir():
        name = p.name
        if not (
            name.startswith(_SCRATCH_PREFIXES) or name in _SCRATCH_NAMES
        ):
            continue
        if name.startswith("vodrip-shards-"):
            continue  # worker-owned, transient while a job runs
        try:
            if now - p.stat().st_mtime >= min_age_s:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


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
    "yt-gate-", "yt-policy-test-", "ytdlp_aud_", "ytdlp_seg_",
)
# Bare scratch dir names (not mkdtemp-prefixed) in the temp dir.
_SCRATCH_NAMES = ("kd_test", "vodrip-search-lab")


@pytest.fixture(scope="session", autouse=True)
def _reap_vodrip_scratch():
    """Stop the temp-dir accumulation: wipe stale scratch at session start,
    and every scratch dir this session created at session end."""
    _wipe_vodrip_scratch(min_age_s=6 * 3600.0)  # stale from dead processes
    yield
    _wipe_vodrip_scratch(min_age_s=0.0)  # this session's own leftovers


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
