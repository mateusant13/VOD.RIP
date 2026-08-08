"""Root conftest — registers the opt-in test-impact plugin (tests/impact_plugin.py).

The plugin is inert unless enabled with ``--impact`` or ``VODRIP_IMPACT=1``;
see tests/test_impact_selfcheck.py for what it proves.
"""

pytest_plugins = ["tests.impact_plugin"]


def pytest_collection_modifyitems(config, items):
    """Mark tests in ``*_real*.py`` files as ``real`` (live network/env).

    Real-network suites (test_*_real*.py) exercise YouTube/Twitch/Kick/CDN
    endpoints and depend on live tokens, bot-gate state and channel uptime.
    They are expensive (~15min for the full set) and fail for environmental
    reasons that are not regressions. pytest.ini's ``addopts = -m "not real"``
    skips them by default; run them explicitly with ``pytest -m real``.
    """
    for item in items:
        if item.path.name.endswith("_real.py"):
            item.add_marker("real")
