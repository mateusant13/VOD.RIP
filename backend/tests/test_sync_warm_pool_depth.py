#!/usr/bin/env python3
"""Sync-wave pool depth: the measured 6, and the two laws that keep it honest.

The trade-off under test
------------------------
backend/app.py creates the YouTube startup sync-wave pool with a fixed
max_workers. It used to be 2, with a ponytail comment claiming "to avoid
tripping YouTube bot-gate on startup". Neither half of that claim survived
measurement, and the 2 cost real wall-clock on every boot.

MEASURED 2026-10-13 on this box, real YouTube, no mocks (harness kept in
tmp/_gateprobe/battery4.py -- it lives in tmp/ because the dev supervisor
watches backend/ and reloads the API on every .py write):

  wave = the URLs the LIVE settings.json actually selects (6 of them: 13 saved
  channels carry YouTube VODs, the wave takes the first 4 and the first URL per
  kind, so n=6, not 12). Runner = the verbatim shape of app.py
  (bootstrap_anonymous_session -> ThreadPoolExecutor(max_workers=K) -> _warm_one
  with WARM_WORK_SEMAPHORE and the 15 s per-URL cap). Governor ceiling pinned to
  the value the API process itself boots with ("rate_budget: primed ceilings from
  history: {'kick': 4.0, 'youtube': 6.8}" in tmp/vodrip-devall-api.log), i.e.
  6.8 rpm -> AUTO share 4.76 rpm -> one token every 12.61 s.

    K   wall_s (3 interleaved reps)          mean    cap hits (URLs over 15 s)
    2   35.19 / 30.05 / 33.53               32.92   4 / 1 / 0
    4   20.55 / 17.74 / 33.86               24.05   2 / 3 / 2
    6   14.84 /  5.11 / 15.55               11.83   0 / 0 / 1

  Bot-gate arms at ANY depth: ZERO. 19 wave runs / 114 warm calls / 1299
  InnerTube /player POSTs across the 2, 4 and 6 worker arms, checked through
  services.yt_gate.note_youtube_gate and services.preview.warm's own soft-neg
  streak. The live archive agrees: all 128 'bot_gate' rows in
  H:/VOD.RIP-data/archive.db.rate_limit_events carry in_flight=0 and
  recent_requests<=6, because the gate is an UNANIMOUS LOGIN_REQUIRED wall across
  >=3 client profiles (services/youtube_innertube.py:1227
  _bot_wall_is_unanimous), never a burst signal.

So 6 it is: one round for the real wave size, 2.8x faster than 2, and the gate
the old comment defended against never fired at 2, 4 or 6.

Why these tests are law tests, not behavioural ones
---------------------------------------------------
_warm_first_wave_sync is a closure defined inside the FastAPI lifespan, so it is
not importable and cannot be exercised without refactoring the lifespan itself
-- a refactor this change deliberately does not make. What IS testable offline
and deterministically is the contract the number must keep:

  1. the depth is the measured 6 (not drifted back to 2, not bumped past the
     global cap), and
  2. it stays inside WARM_WORK_SEMAPHORE (8), which is what actually bounds
     concurrent extract/mux work across every warm executor, and which
     user-click paths never acquire.

Both are read from the source with ast, so a revert or an over-reach fails here
rather than in a user's boot log.
"""
import ast
import io
import math
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

os.environ.setdefault("VODRIP_NO_DAEMONS", "1")

APP_PY = os.path.join(os.path.dirname(__file__), "..", "app.py")

# The wave size the live settings.json selects on this box, measured by porting
# app.py's selection loop (first 4 YouTube channels x first URL per kind). 12 is
# the structural maximum (4 channels x 3 kinds); 6 is what this box has today.
_MEASURED_WAVE_URLS = 6
_MAX_WAVE_URLS = 12

# The depth the 2026-10-13 measurement selected, and why not its neighbours:
#   2 -> mean 32.92 s wall, 4 -> mean 24.05 s, 6 -> mean 11.83 s.
_MEASURED_DEPTH = 6


def _sync_wave_pool_kwargs():
    """max_workers / thread_name_prefix of the pool created inside
    _warm_first_wave_sync in backend/app.py, read with ast."""
    with io.open(APP_PY, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename="app.py")
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_warm_first_wave_sync":
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and getattr(sub.func, "id", "") == "ThreadPoolExecutor"
                ):
                    kw = {k.arg: ast.literal_eval(k.value) for k in sub.keywords}
                    found.append((sub.lineno, kw))
    assert len(found) == 1, (
        "expected exactly one ThreadPoolExecutor inside _warm_first_wave_sync, "
        "found %r -- if you split the wave into several pools, update this test"
        % (found,)
    )
    return found[0]


def _sync_wave_max_workers():
    return _sync_wave_pool_kwargs()[1]["max_workers"]


def test_sync_wave_pool_depth_is_the_measured_six():
    """RED ARM: reverting to 2 (or 4) fails here.

    2 -> mean 32.92 s and 4 -> mean 24.05 s of wave wall-clock, against 11.83 s
    at 6, and 2 additionally puts URLs over the 15 s per-URL cap. The depth is
    the one the measurement selected, not a round number someone likes better.
    """
    lineno, kw = _sync_wave_pool_kwargs()
    assert kw.get("thread_name_prefix") == "yt-sync-warm", (
        "the sync-wave pool must stay named 'yt-sync-warm' so its threads are "
        "identifiable in a dump: %r" % (kw,)
    )
    assert kw["max_workers"] == _MEASURED_DEPTH, (
        "app.py:%d sync-wave max_workers=%r, measured value is %d "
        "(wall 32.92 s at 2, 24.05 s at 4, 11.83 s at 6, 0 gate arms at all "
        "three). If you are changing this, re-measure first -- the harness is "
        "tmp/_gateprobe/battery4.py."
        % (lineno, kw["max_workers"], _MEASURED_DEPTH)
    )


def test_sync_wave_pool_depth_stays_under_the_global_warm_cap():
    """RED ARM: raising the depth to 9+ fails here.

    WARM_WORK_SEMAPHORE (deps.py) is the ONE cap on concurrent extract/mux work
    across every warm executor (WARM 3 + GESTURE 2 + FULL 1 + ANON 6 + this
    wave). The wave must stay inside it, or it starves the other warm paths --
    and user-click paths never take the semaphore, so a deeper wave can only
    steal from background warm, never protect a click.
    """
    from deps import WARM_WORK_SEMAPHORE  # noqa: E402

    assert isinstance(WARM_WORK_SEMAPHORE, threading.BoundedSemaphore)
    permits = WARM_WORK_SEMAPHORE._value
    depth = _sync_wave_max_workers()
    assert depth <= permits, (
        "sync-wave depth %d exceeds the global warm cap of %d: the wave would "
        "starve WARM_EXECUTOR/GESTURE/FULL/ANON warm at boot. Keep it under "
        "the semaphore." % (depth, permits)
    )
    # And the cap really is the bound, not decoration: 8 in, 9th blocks.
    got = [WARM_WORK_SEMAPHORE.acquire(blocking=False) for _ in range(permits)]
    assert all(got)
    assert not WARM_WORK_SEMAPHORE.acquire(blocking=False)
    for _ in range(permits):
        WARM_WORK_SEMAPHORE.release()


def test_sync_wave_pool_depth_covers_the_real_wave_in_one_round():
    """RED ARM: any depth below the measured wave size leaves URLs queueing.

    The wave's own wall-clock is ceil(n / K) rounds of the governor's one-token
    pace (12.61 s at the 6.8 rpm ceiling this box boots with), and the next
    startup phase does not begin until this wave returns. At the measured wave
    size, 6 is the smallest depth that is a single round -- which is exactly the
    32.92 s -> 11.83 s the measurement recorded.
    """
    depth = _sync_wave_max_workers()
    rounds_now = math.ceil(_MEASURED_WAVE_URLS / depth)
    rounds_old = math.ceil(_MEASURED_WAVE_URLS / 2)
    assert rounds_now == 1, (
        "depth %d needs %d rounds for the measured %d-URL wave; 6 needs 1. "
        "The 32.9 s mean at depth 2 was 3 rounds of the 12.61 s governor pace."
        % (depth, rounds_now, _MEASURED_WAVE_URLS)
    )
    assert rounds_old > rounds_now, "the old depth of 2 must still cost more rounds"
    # The structural maximum must not regress past 2 rounds either.
    assert math.ceil(_MAX_WAVE_URLS / depth) <= 2
