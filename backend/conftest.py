"""Root conftest — registers the opt-in test-impact plugin (tests/impact_plugin.py).

The plugin is inert unless enabled with ``--impact`` or ``VODRIP_IMPACT=1``;
see tests/test_impact_selfcheck.py for what it proves.
"""

pytest_plugins = ["tests.impact_plugin"]

import os

# archive_db's module-level self-check (25-40s DB-backed invariants on the
# REAL archive) is gated behind VODRIP_ARCHIVE_SELFCHECK=1 — pytest keeps it
# on; the app boots with it off.
os.environ.setdefault("VODRIP_ARCHIVE_SELFCHECK", "1")
# cookie_store's module self-check (~1.2s) is gated the same way.
os.environ.setdefault("VODRIP_COOKIE_SELFCHECK", "1")


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
