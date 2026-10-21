#!/usr/bin/env python3
"""Startup YouTube warm: the BOUND, not the pool depth.

This file used to pin ``max_workers=6`` for the startup sync-wave pool, on the
strength of a 2026-10-13 measurement (6 workers = 11.83 s wall for the 6-URL
wave, 2 = 32.92 s, 4 = 24.05 s). That measurement is obsolete, and keeping it
would pin a design that no longer works:

MEASURED 2026-10-20 on the live app, from its own log. Every warmed URL in the
boot wave reports:

    STARTUP_SYNC_WARM: <url> done in 248-261s      (12 of them in one boot)

The per-URL cap (15 s) only capped the WAIT - the abandoned extract thread kept
running, so a boot launched ~12 four-minute YouTube extracts plus a 20-URL
STARTUP_WAVE. py-spy on the live API caught those threads holding the GIL while
a preview POST that does no work took 2.5 s, /api/health went silent for 18.6 s
and the supervisor reclaimed the port ("[dev] reclaimed :7897 from an
unresponsive listener") - five boots in two minutes, each launching another
wave. The owner's report was "nada que eu clico como preview carrega".

A warm that lands in 250 s cannot beat the owner to a click, so depth is no
longer the lever. The contract that replaced it, pinned here:

  1. the sync warm is SEQUENTIAL - no pool inside _warm_first_wave_sync,
  2. it takes at most TWO channels and only their newest VOD (one kind),
  3. it STOPS on the first URL that breaches the cap, and
  4. it STOPS as soon as the owner is using the app,

and the follow-on STARTUP_WAVE queues ONE URL per channel for the same two
channels instead of five per channel for four.

These are law tests, not behavioural ones: both functions are closures inside
the FastAPI lifespan, so they are not importable. Reading the source with ast
means a revert fails here instead of in a user's boot log, which is exactly how
the 2026-10-13 numbers stayed honest for a week.
"""
import ast
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("VODRIP_NO_DAEMONS", "1")

APP_PY = os.path.join(os.path.dirname(__file__), "..", "app.py")


def _func_source(name: str) -> str:
    """Unparsed source of the closure called ``name`` in backend/app.py."""
    with io.open(APP_PY, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename="app.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(
        "app.py no longer defines %r -- if the startup warm was renamed or "
        "moved, update this test rather than deleting it" % name
    )


def test_sync_warm_is_sequential_not_a_wide_pool():
    """RED ARM: putting the ThreadPoolExecutor back fails here.

    A parallel wave only helps if the warms finish inside the round. Measured
    2026-10-20 they take ~250 s each, so a pool converts every boot into N
    concurrent four-minute extracts and the abandoned threads outlive the wave.
    """
    src = _func_source("_warm_first_wave_sync")
    assert "ThreadPoolExecutor" not in src, (
        "the startup sync warm must not fan out: measured 2026-10-20 each warm "
        "takes ~250 s, and the abandoned threads starve the click path."
    )


def test_sync_warm_covers_two_channels_and_their_newest_vod_only():
    """RED ARM: growing the selection back to 4x3 (12 URLs) fails here."""
    src = _func_source("_warm_first_wave_sync")
    assert "sorted_channels[:2]" in src, (
        "the sync warm must take at most two channels (found no [:2] slice)"
    )
    kinds = None
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "KINDS" for t in node.targets
        ):
            kinds = ast.literal_eval(node.value)
    assert kinds == ("vods",), (
        "the sync warm must warm the VODs kind only, got %r; clips/streams are "
        "warmed by the frontend when their tab is opened (2 more four-minute "
        "extracts at boot otherwise)." % (kinds,)
    )


def test_sync_warm_stops_on_the_first_slow_extract():
    """RED ARM: dropping the early return fails here.

    Without it a 250 s URL still lets the rest of the list start, which is how
    the wave outlived the boot it belonged to.
    """
    src = _func_source("_warm_first_wave_sync")
    assert "stopping after a slow extract" in src, (
        "the sync warm must abort the remaining URLs after one cap breach"
    )
    assert "return False" in src, (
        "_warm_one must report the breach (False) so the loop can stop"
    )


def test_sync_warm_yields_to_the_owner():
    """RED ARM: removing the user-activity check fails here.

    The owner outranks the archive (services/user_activity.py). A warm that
    keeps starting while the page is open is the defect this pins.
    """
    src = _func_source("_warm_first_wave_sync")
    assert "owner is using the app" in src, (
        "the sync warm must stop when the owner becomes active"
    )
    assert "user_is_active" in src, (
        "the sync warm must consult services.user_activity.user_is_active"
    )


def test_startup_wave_queues_one_url_per_channel_for_two_channels():
    """RED ARM: BATCH back to 5 (20 URLs) or channels back to 4 fails here."""
    src = _func_source("_startup_wave_warm")
    assert "BATCH = 1" in src, "STARTUP_WAVE must queue one URL per channel"
    assert "sorted_channels[:2]" in src, (
        "STARTUP_WAVE must cover at most two channels"
    )
