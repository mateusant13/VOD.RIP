"""Contract for YOUTUBE_CHANNEL_FETCH_TIMEOUT_SEC (deps.py:122).

Measured 2026-10-13 on this box against live YouTube, each number from a
FRESH python process so the yt-dlp cold bootstrap is included (channel
@srdoglol, 122-index-row tab; @jynxzi, 2,302-row tab):

  videos, cold delta (limit 25 -> playlistend 75):  6.4 / 10.3 / 15.5 / 25.5 s  (n=4)
  videos, deep page (limit 250 -> playlistend 750): 44.4 s on @jynxzi (tab saturated),
                                                      4.4 s on @srdoglol (tab exhausted at 97)
  shorts, cold enrich (limit 25):                    70.2 s instrumented; 187.8 s with tab retries
      of which 57.6-178 s = 4 FULL single-video RSS probes (12.6-45 s each)
      that classified every candidate as "video" and added ZERO rows
  shorts, 2nd call in the same process (probe cache):  9.4 s
  shorts, enrich=False (tab only):                     9.6 s
  videos, enrich cost (one RSS GET):                 +0.2 s
  warm browse: L1 channel_cache hit 0.00 ms; disk index read 1.35 ms p50
      (one 15.4 s outlier from WAL contention with the live writer)
  launcher watchdog: __main_launcher__._server_health_watchdog polls
      /api/info (timeout=1.0 s) every 15 s; after 3 consecutive misses it
      restarts the API — a 45 s horizon of EVENT-LOOP unresponsiveness.

The three invariants pinned below are the ones those measurements
established — each test can fail, and each failure names the regression:

  1. the fetch timeout always fires BEFORE the watchdog horizon, so the
     user gets "timed out — try again" before any supervisor restart;
  2. every YouTube channel fetch call site is executor-bound AND wrapped in
     asyncio.wait_for(timeout=YOUTUBE_CHANNEL_FETCH_TIMEOUT_SEC) — an
     unbounded or inline call is what the old 90 s stall looked like;
  3. a saturated CHANNEL_EXECUTOR cannot stall the real /api/info handler:
     the watchdog watches the event loop, never this pool, which is why no
     fetch duration (44.4 s measured) can trip it.
"""
import ast
import asyncio
import time
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent


def _watchdog_horizon_sec() -> float:
    """Derive the launcher watchdog's kill horizon from its own source.

    Parses _server_health_watchdog rather than hardcoding 45: if the
    supervisor's poll interval or miss count changes, this moves with it.
    """
    src = (_BACKEND / "__main_launcher__.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_server_health_watchdog":
            interval = misses = None
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    name = getattr(stmt.targets[0], "id", None)
                    if isinstance(stmt.value, ast.Constant):
                        if name == "miss_interval":
                            interval = float(stmt.value.value)
                        elif name == "max_misses":
                            misses = int(stmt.value.value)
            assert interval is not None and misses is not None, (
                "watchdog constants not found — this test must track the launcher"
            )
            return interval * misses
    raise AssertionError("_server_health_watchdog not found in __main_launcher__.py")


def test_timeout_fires_before_the_watchdog_horizon() -> None:
    """30 s must stay under the watchdog's 3x15 s = 45 s horizon.

    RED ARM: set YOUTUBE_CHANNEL_FETCH_TIMEOUT_SEC >= horizon (e.g. 50) and
    this fails; shrink the watchdog to 2 misses (horizon 30) and it fails
    too — the coupling is pinned in both directions.
    """
    from deps import YOUTUBE_CHANNEL_FETCH_TIMEOUT_SEC

    horizon = _watchdog_horizon_sec()
    assert YOUTUBE_CHANNEL_FETCH_TIMEOUT_SEC < horizon, (
        f"fetch timeout {YOUTUBE_CHANNEL_FETCH_TIMEOUT_SEC}s would outlive the "
        f"watchdog horizon {horizon}s — a user-visible timeout must fire first"
    )


def test_channel_fetch_call_sites_are_executor_bound_and_timeout_wrapped() -> None:
    """Every YouTube fetch in routers/channels.py must be

        await asyncio.wait_for(
            loop.run_in_executor(CHANNEL_EXECUTOR, ...),
            timeout=YOUTUBE_CHANNEL_FETCH_TIMEOUT_SEC)

    RED ARM: unwrap the wait_for, drop the timeout, or move the call onto
    the event loop and this fails — those are exactly the shapes that
    stalled the API in the 90 s era.
    """
    src = (_BACKEND / "routers" / "channels.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    wrapped = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_wait_for = (
            (isinstance(func, ast.Attribute) and func.attr == "wait_for")
            or (isinstance(func, ast.Name) and func.id == "wait_for")
        )
        if not is_wait_for:
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        timeout = kwargs.get("timeout")
        assert timeout is not None, "asyncio.wait_for without a timeout kwarg"
        is_our_timeout = isinstance(timeout, ast.Name) and timeout.id == (
            "YOUTUBE_CHANNEL_FETCH_TIMEOUT_SEC"
        )
        if not is_our_timeout:
            continue  # clips/stream timeouts of other platforms
        # the awaited coroutine must be an executor dispatch on CHANNEL_EXECUTOR
        call = node.args[0]
        dispatches_channel_pool = False
        for sub in ast.walk(call):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "run_in_executor"
                and sub.args
                and isinstance(sub.args[0], ast.Name)
                and sub.args[0].id == "CHANNEL_EXECUTOR"
            ):
                dispatches_channel_pool = True
        assert dispatches_channel_pool, (
            "a YOUTUBE_CHANNEL_FETCH_TIMEOUT_SEC wait_for is not dispatching to "
            "CHANNEL_EXECUTOR — the fetch would run on the event loop"
        )
        wrapped += 1
    assert wrapped >= 4, (
        f"expected the 4 YouTube fetch call sites wrapped with the timeout, found {wrapped}"
    )


@pytest.mark.asyncio
async def test_saturated_channel_pool_cannot_stall_the_watchdog_probe() -> None:
    """The REAL /api/info handler must answer while every CHANNEL_EXECUTOR
    worker is busy running a slow fetch.

    This is the scenario the old 90 s ceiling was blamed for: it was never
    the fetch (measured worst case 44.4 s continues past its own 30 s
    timeout in this pool), it is only ever the event loop. Saturate the
    channel pool with 2.5 s blockers — one per worker — then probe.
    Budget: the watchdog's own 1.0 s request timeout.

    RED ARM: share CHANNEL_EXECUTOR with the /api/info path (or block the
    loop in the fetch) and the probe exceeds 1 s.
    """
    from deps import CHANNEL_EXECUTOR
    from routers.system import server_info

    # Warm the route exactly as the watchdog does, so the memoized feature
    # map does not make the first (saturated) probe pay a cold module load.
    await server_info()

    blockers = [
        CHANNEL_EXECUTOR.submit(time.sleep, 2.5) for _ in range(CHANNEL_EXECUTOR._max_workers)
    ]
    try:
        started = time.perf_counter()
        payload = await asyncio.wait_for(server_info(), timeout=1.0)
        elapsed = time.perf_counter() - started
        assert payload.get("name") == "VOD.RIP 🪦"
        assert elapsed < 1.0, (
            f"/api/info took {elapsed:.2f}s with the channel pool saturated — "
            "the watchdog would count that as a miss"
        )
    finally:
        for fut in blockers:
            fut.result(timeout=10)
