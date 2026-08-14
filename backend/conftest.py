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
    """Delete leftover ``vodrip-*`` scratch dirs in the system temp dir.

    Tests mkdtemp scratch dirs (vodrip-tests-*, vodrip-ws2-panel-*, …) at
    MODULE IMPORT — every pytest process, even --collect-only, leaks one,
    and killed/interrupted runs never clean the transcribe shard dirs
    (vodrip-shards-*). Observed: 1,497 dirs / ~44 GB on a dev box.
    Live worker shards are never touched here (the worker reaps its own
    stale ones); only dirs untouched for min_age_s are removed."""
    tdir = Path(tempfile.gettempdir())
    now = time.time()
    for p in tdir.glob("vodrip-*"):
        if p.name.startswith("vodrip-shards-"):
            continue  # worker-owned, transient while a job runs
        try:
            if now - p.stat().st_mtime >= min_age_s:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


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
