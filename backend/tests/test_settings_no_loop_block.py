"""Regression: WAL-busy settings saves must never wedge the event loop.

The dev-backend wedge (receipt TC-20260913): POST /api/settings writes
sqlite (mark_channel_priority) synchronously on the event loop; with
busy_timeout=10000 a WAL-busy write spins IN-PROCESS for 10s, freezing
every endpoint at once. Fix = asyncio.to_thread offload of the sync
settings/health IO (routers/settings.py, routers/system.py) plus the
lock-free schema-ready fast path for first-touch readers
(services/archive_db._ensure_schema_ready) — without the latter, a fresh
worker thread's first read still serialises behind the write lock and
/api/health takes the full 10s.

These tests hold BEGIN EXCLUSIVE on the scratch archive DB from a side
thread (with a dirty txn), fire the real save path, and probe /api/health
and /api/info from an OS thread via run_coroutine_threadsafe — the probe
round-trip is exactly what a frozen loop inflates past the 100ms budget.
"""
from __future__ import annotations

import asyncio
import itertools
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from deps import settings_mgr
from models.schemas import AppSettings
from services import archive_db

# busy_timeout on the app's own connections (archive_db._open_conn); the
# blocked save must spin for close to this before erroring out.
BUSY_TIMEOUT_S = 10.0

# Contract: supervisors must see an endpoint answer in well under 100ms while
# a save is stuck behind the lock.
LIVE_BUDGET_S = 0.1
# How many times the warm-up may retry an endpoint that is still paying a cold
# cost. Bounded, so a genuinely slow idle endpoint fails the warm-up by name.
_WARM_ATTEMPTS = 6
# The warm-up always takes at least this many samples, even after one lands
# inside budget. A baseline of ONE fast call is a lottery: 226ms on one day and
# 1277ms on another, and the derived budget inherits whichever we happened to
# catch. The baseline is the worst of >=5 IDLE round-trips instead.
_WARM_MIN_SAMPLES = 5
# /api/asr/runtime is a pure filesystem stat of the models path: its handler
# runs in a worker thread, so its round-trip measures the DISK and the EXECUTOR,
# never the loop. Measured with the endpoint warmed: 226ms for one probe while
# the save spun (and ~1030ms cold, per the note above). Gate run 5 measured the
# gap that makes a fixed budget the wrong instrument: while the save spun, the
# loop-bound endpoints peaked at 27.9ms and /api/asr/runtime at 1277ms. A
# blocked loop delays EVERY request, so a 45x gap on ONE endpoint is a worker
# delay, not a wedge - and the fixed budget blamed the loop for it. This number
# is now only the WARM-UP target (what "warmed" means for the retry below).
_ENDPOINT_BUDGET_S = {"/api/asr/runtime": 1.0}

# Endpoints whose handler runs in a worker thread. They are graded against their
# own worst IDLE round-trip (times _BASELINE_MULTIPLE, with a floor so a fast
# baseline cannot make the budget unmeetable) instead of against the fixed
# loop contract. Everything else keeps the fixed 100ms budget.
_EXECUTOR_ENDPOINTS = ("/api/asr/runtime",)
_BASELINE_MULTIPLE = 4.0
_BASELINE_FLOOR_S = 0.25

# When an endpoint is THIS slow while IDLE, its liveness cannot be measured at
# all and the warm-up fails by name instead of pretending. It is deliberately
# far above the graded budgets: the warm-up failing on a merely slow endpoint
# would be a disk assertion wearing a liveness label (measured: /api/asr/runtime
# took 1277ms while IDLE right after a mutation run, and 226ms an hour earlier).
_IDLE_UNUSABLE_S = {"/api/asr/runtime": 5.0}

# The assertion that actually deserves the name "the event loop was blocked": a
# coroutine re-arms itself on the loop every _LOOP_BEAT_S and records how LATE it
# wakes. A frozen loop is late by the freeze; a worker-thread delay is not.
_LOOP_BEAT_S = 0.02
_LOOP_STALL_BUDGET_S = 0.25
# How long the beat is allowed to take its FINAL wake after the save returns.
# Generous on purpose: a 20ms timer that cannot get a slot for 10s is a wedge by
# itself, while 10s of headroom cannot be reached by an ordinary loaded box.
_BEAT_DRAIN_TIMEOUT_S = 10.0
# The probe URLs rotate one-per-sample so a single run covers every distinct
# offload shape the app uses; adding an endpoint here is how its liveness gets
# pinned.
#   /api/health       run_in_executor(LIVENESS)  sqlite reads + health fields
#   /api/info         run_in_executor(INFO_EXEC)  named pool
#   GET /api/settings to_thread(reconcile)        sqlite read + settings write
#   /api/asr/runtime  run_in_executor(LIVENESS)  pure filesystem stat
_PROBE_URLS = (
    "/api/health",
    "/api/info",
    "/api/settings",
    "/api/asr/runtime",
)


@pytest.fixture(autouse=True)
def _reset_settings():
    """Same isolation pattern as test_api_integration.py."""
    original_file = settings_mgr._settings_file
    temp_file = original_file.parent / f"settings_nolock_{os.getpid()}.json"
    settings_mgr._settings_file = temp_file
    settings_mgr._settings = AppSettings()
    # Mirror what SettingsManager._load() does in production: the in-memory
    # state carries the snapshot it was read from, so save() can tell its own
    # edits from another writer's commits (services/settings.py).
    settings_mgr._settings._vodrip_base = settings_mgr._settings.model_copy(
        deep=True
    )
    yield
    settings_mgr._settings_file = original_file
    temp_file.unlink(missing_ok=True)


@pytest.fixture()
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class _ExclusiveHolder:
    """Side thread holding BEGIN EXCLUSIVE + a dirty txn on the scratch DB.

    Same recipe as the verified smoke repro: a distinct sqlite3 connection
    in another thread of this same process; WAL EXCLUSIVE blocks writers
    (the save's busy_timeout spin) while readers stay free.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.held = threading.Event()
        self.error: Exception | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        conn = None
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("BEGIN EXCLUSIVE")
            # Dirty the txn (one row into an existing table) so a second
            # writer is blocked even on WAL read-snapshot nuances.
            conn.execute(
                "INSERT INTO channel_snapshots (platform, channel_key, fetched_at)"
                " VALUES ('twitch','holder-dirty','2026-01-01T00:00:00+00:00')"
            )
            self.held.set()
            self._stop.wait(timeout=90)
        except Exception as exc:  # noqa: BLE001 — surfaced by the test body
            self.error = exc
            self.held.set()
        finally:
            if conn is not None:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                conn.close()

    def __enter__(self) -> "_ExclusiveHolder":
        # Warm the shared write conn + schema on this (main) thread FIRST so
        # the holder's EXCLUSIVE only blocks real statements, not one-time
        # schema migration.
        archive_db.has_pending_jobs()
        self._thread.start()
        assert self.held.wait(timeout=15), "holder never reported"
        assert self.error is None, f"holder failed to take EXCLUSIVE: {self.error}"
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        self._thread.join(timeout=15)


@pytest.mark.asyncio
async def test_liveness_endpoints_stay_live_during_busy_settings_save(client):
    """POST /api/settings (real sqlite write, WAL-blocked → 10s spin) must
    not stop the liveness endpoints from answering <100ms.

    Four endpoints are probed, one per distinct offload shape, so a wedge in
    any single one of them is caught by the same run:
      /api/health       - run_in_executor(LIVENESS_EXECUTOR, _probe) (sqlite reads + health fields)
      /api/info         - run_in_executor(INFO_EXECUTOR)
      GET /api/settings - asyncio.to_thread(_load_settings_with_reconcile),
                          which both reads sqlite and WRITES settings.json
      /api/asr/runtime  - run_in_executor(LIVENESS_EXECUTOR, runtime_status), pure filesystem IO
    """
    db_path = Path(os.environ["VODRIP_ARCHIVE_DB"])
    loop = asyncio.get_running_loop()

    # A brand-new twitch channel routes the save through
    # _prioritize_new_channels → mark_channel_priority → archive_db.execute
    # — the exact write that used to wedge the loop.
    payload = {
        "saved_channels": [{"id": "locktest", "displayName": "Lock Test", "twitchSlug": "locktestchan"}]
    }

    # Warm every probed endpoint BEFORE the lock is held. The first call to an
    # endpoint pays one-time cost that is NOT a loop block and would be
    # measured as one: schema creation on the fresh scratch DB, and (verified
    # here) ~1.5s of filesystem probing inside the ASR runtime's cold
    # `runtime_status()` — a drive spin-up on the models path. Measured cold,
    # /api/asr/runtime peaked at ~1030ms while health/info/settings stayed at
    # 2-28ms in the same run: the request was slow IN A WORKER THREAD, the loop
    # was fine. Timing those cold calls against a 100ms budget would assert on
    # disk warmth, not liveness. Everything measured below is steady-state.
    # ...so warm each endpoint until it answers INSIDE the budget, not merely
    # once. One warm call does not keep the models path warm for the 10s that
    # follow: measured in a 13-minute parallel run, /api/asr/runtime still cost
    # 1273ms after its single warm call, and the failure blamed the event loop
    # for a cold drive. The retry is bounded, so an endpoint that is slow while
    # IDLE fails HERE, by name, instead of being reported as a blocked loop.
    idle_worst: dict[str, float] = {}
    for url in _PROBE_URLS:
        attempts: list[float] = []
        for _attempt in range(_WARM_ATTEMPTS):
            t_warm = time.perf_counter()
            warm = await client.get(url)
            assert warm.status_code == 200, url
            attempts.append(time.perf_counter() - t_warm)
            if (len(attempts) >= _WARM_MIN_SAMPLES
                    and attempts[-1] < _ENDPOINT_BUDGET_S.get(url, LIVE_BUDGET_S)):
                break
        else:
            worst_idle = max(attempts)
            unusable = _IDLE_UNUSABLE_S.get(url, 1.0)
            assert worst_idle < unusable, (
                f"{url} answered in {worst_idle:.3f}s while IDLE (unusable above "
                f"{unusable * 1000:.0f}ms) — the endpoint is slow with no lock "
                "held, so the liveness assertion below cannot mean anything"
            )
        # The worst IDLE round-trip is this endpoint's own baseline. The
        # during-save budget below is derived from it, so "slower than idle" is
        # measured against the endpoint rather than against a number chosen for
        # it on another day, on another disk, with another game running.
        idle_worst[url] = max(attempts)

    with _ExclusiveHolder(db_path):
        per_url: dict[str, list[float]] = {u: [] for u in _PROBE_URLS}
        poller_errors: list[BaseException] = []
        stop = threading.Event()
        index = itertools.count()
        loop_delays: list[float] = []
        beat_marks: list[float] = []

        async def _loop_beat() -> None:
            # A beat that re-arms itself ON the loop and records how LATE it
            # wakes. This - not a request round-trip - is the loop's own
            # responsiveness: a request can be slow in a worker thread while
            # the loop is perfectly free (measured: 1277ms on /api/asr/runtime
            # with the loop answering /api/health in 28ms in the same window).
            while True:
                t0 = time.perf_counter()
                await asyncio.sleep(_LOOP_BEAT_S)
                now = time.perf_counter()
                loop_delays.append(max(0.0, now - t0 - _LOOP_BEAT_S))
                beat_marks.append(now)
                if stop.is_set():
                    # One final wake AFTER the spin. Cancelling the beat in the
                    # finally instead would drop exactly this delay: cancellation
                    # is delivered before an overdue sleep resumes, so a block
                    # that ended with the save (the handler doing sync work after
                    # its last await) would never be recorded and the test would
                    # pass over a frozen loop. The beat exits here instead.
                    return

        async def _probe_one(url: str) -> None:
            resp = await client.get(url)
            assert resp.status_code == 200, url

        def _next_url() -> str:
            return _PROBE_URLS[next(index) % len(_PROBE_URLS)]

        def _poller() -> None:
            # Time from SUBMISSION (not inside the coroutine): while the loop
            # is frozen the coroutine body never even starts, so timing inside
            # it would hide the queueing delay. This is the in-process
            # equivalent of the smoke poller's request->response wall time.
            # Single writer thread, so appending needs no lock.
            while not stop.is_set():
                url = _next_url()
                t_send = time.perf_counter()
                fut = asyncio.run_coroutine_threadsafe(_probe_one(url), loop)
                try:
                    fut.result(timeout=60)
                except asyncio.CancelledError:
                    # A probe still queued when the client/loop tears down is
                    # cancelled. Do NOT record a sample for it: its "latency"
                    # is submission->cancellation (the loop draining the save),
                    # which can be hundreds of ms and says nothing about the
                    # endpoint. Only COMPLETED probes measure a round-trip,
                    # and cancellation is only reachable AFTER stop.set() —
                    # i.e. after the save's spin is over — so this can never
                    # discard a sample from inside the measured window.
                    break
                except BaseException as exc:  # noqa: BLE001 - re-raised below
                    # A real probe failure (non-200 assert, endpoint
                    # exception, 60s result timeout = hard wedge) must FAIL
                    # the test, not die as an "unhandled thread exception"
                    # warning that pytest reports as a pass. Collected and
                    # re-asserted on the main thread.
                    poller_errors.append(exc)
                    break
                per_url[url].append(time.perf_counter() - t_send)
                time.sleep(0.05)
        poller = threading.Thread(target=_poller, daemon=True)
        poller.start()
        beat = asyncio.create_task(_loop_beat())
        t_post0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                client.post("/api/settings", json=payload), timeout=60
            )
        finally:
            # The SPIN is over the moment the POST returns. Measured here and
            # not after the shutdown below: poller.join(timeout=15) can consume
            # its whole timeout, and a 10s spin then reads as 26s (measured 2 of
            # 4 runs) - which made "the save really spun" true for the wrong
            # reason and inflated the window the loop beat is compared against.
            post_elapsed = time.perf_counter() - t_post0
            t_stop = time.perf_counter()
            stop.set()
            # Join the poller OFF the loop. poller.join() is synchronous, and
            # the poller may be sitting in fut.result(timeout=60) on a coroutine
            # that only the loop can run, so joining it from the loop deadlocks
            # the loop for the whole timeout. Measured 2 of 4 runs: 15s of frozen
            # loop caused by THIS test's teardown, which the beat then reported
            # as a stall of 14,983ms - the instrument blocking the thing it
            # measures. to_thread leaves the loop free, the pending request runs,
            # and the thread exits in milliseconds.
            await asyncio.to_thread(poller.join, 15)
            poller_exit_s = time.perf_counter() - t_stop
            beat_unfinished = False
            try:
                await asyncio.wait_for(beat, timeout=_BEAT_DRAIN_TIMEOUT_S)
            except asyncio.TimeoutError:
                beat.cancel()
                beat_unfinished = True

    # A probe that failed/raised on the poller thread must fail THIS test —
    # otherwise the exception dies as an "unhandled thread exception"
    # warning and pytest reports a pass over a dead poller. Checked BEFORE
    # the latency asserts: if the poller died, the timing data below is
    # incomplete and the real cause has to surface first.
    assert not poller_errors, (
        f"{len(poller_errors)} probe(s) failed while the save spun: "
        f"{poller_errors!r}"
    )

    # 1) The wedge really happened: the save's write spun behind the held
    #    EXCLUSIVE for (almost) the whole busy_timeout. If this is fast the
    #    lock took no effect and the timing below is meaningless.
    assert post_elapsed > BUSY_TIMEOUT_S * 0.8, (
        f"save completed in {post_elapsed:.1f}s — expected the 10s busy_timeout"
        " spin; the EXCLUSIVE hold did not block the write"
    )
    assert resp.status_code == 200
    # Response shape unchanged (validation + save still work).
    body = resp.json()
    assert body["saved_channels"][0]["id"] == "locktest"
    # 2) THE loop assertion. The module docstring's contract is "the loop
    #    froze, so every endpoint queued"; this measures exactly that, and it
    #    cannot be satisfied by a worker thread. Gate run 5 is why it exists:
    #    the endpoint round-trips named /api/asr/runtime (1277ms) as a blocked
    #    loop while /api/health answered in 28ms in the same window.
    assert loop_delays, "the loop beat captured nothing"
    assert not beat_unfinished, (
        f"the loop beat did not get its final wake within"
        f" {_BEAT_DRAIN_TIMEOUT_S:.0f}s of the save returning — the loop was still"
        " blocked when the test ended"
    )
    worst_stall = max(loop_delays)
    # THE loop assertion. The module docstring's contract is "the loop froze, so
    # every endpoint queued"; this measures exactly that, and it cannot be
    # satisfied by a worker thread. Gate run 5 is why it exists: the endpoint
    # round-trips named /api/asr/runtime (1277ms) as a blocked loop while
    # /api/health answered in 28ms in the same window. Checked BEFORE the
    # coverage guard, because a stall is the verdict and coverage is only the
    # sanity check on the instrument: a long stall makes the beat cover less of
    # the window, so coverage would otherwise hide the real number.
    assert worst_stall < _LOOP_STALL_BUDGET_S, (
        f"the event loop stalled {worst_stall * 1000:.0f}ms while a WAL-busy save"
        f" spun (budget {_LOOP_STALL_BUDGET_S * 1000:.0f}ms; spin {post_elapsed:.1f}s,"
        f" {len(loop_delays)} beats) — the loop was blocked"
    )
    # A beat that died early would report no stall and pass, so show that it
    # COVERED the window it claims to have watched. Measured: one beat per 20ms,
    # 442 of them across a 12.5s spin. The 0.5 factor is drift margin: a box
    # loaded enough to wake the beat every 40ms instead of every 20ms still
    # covers half the window.
    assert len(loop_delays) * _LOOP_BEAT_S >= post_elapsed * 0.5, (
        f"the loop beat covered only {len(loop_delays) * _LOOP_BEAT_S:.1f}s of the"
        f" {post_elapsed:.1f}s spin — it did not watch the window it reports on"
        f" (beats from +{(beat_marks[0] - t_post0):.1f}s to"
        f" +{(beat_marks[-1] - t_post0):.1f}s of the spin, worst delay"
        f" {max(loop_delays) * 1000:.0f}ms, poller exit {poller_exit_s:.1f}s;"
        " endpoints"
        + "".join(f" {u}={max(v) * 1000:.0f}ms" for u, v in per_url.items() if v)
        + ")"
    )
    # 3) Corroborating detail, per-endpoint so a failure names the route. The
    #    loop-bound routes are held to the fixed contract; the executor-bound
    #    one is held to its OWN idle baseline, because its handler does not run
    #    on the loop and its round-trip is the disk's and the pool's.
    # The poller must actually be gone: a thread still waiting on its last
    # request has an UNMEASURED window, and "no slow sample" would then mean "no
    # sample" - the shape of a check that can never fail. Before the join moved
    # off the loop this could not even be asserted, because the join itself was
    # what kept the thread alive.
    assert not poller.is_alive(), (
        f"the poller thread was still alive {poller_exit_s:.1f}s after stop - its"
        " last request never completed, so the window is unmeasured"
    )
    assert any(per_url.values()), "poller captured nothing"
    for url, got in per_url.items():
        assert got, f"{url} was never sampled"
        idle = idle_worst.get(url, 0.0)
        if url in _EXECUTOR_ENDPOINTS:
            budget = max(idle * _BASELINE_MULTIPLE, _BASELINE_FLOOR_S)
            why = ("its handler runs in a worker thread, so this is a worker/"
                   "executor delay, not a blocked loop")
        else:
            budget = _ENDPOINT_BUDGET_S.get(url, LIVE_BUDGET_S)
            why = ("a loop-bound route missed its contract (the loop beat above is "
                   "the direct evidence of whether the LOOP was blocked)")
        assert max(got) < budget, (
            f"{len(got)} probes, worst {url} round-trip {max(got)*1000:.0f}ms"
            f" (budget {budget*1000:.0f}ms, idle baseline {idle*1000:.0f}ms) while a"
            f" WAL-busy save spun — {why}"
        )


@pytest.mark.asyncio
async def test_get_settings_reconcile_shape_preserved(client, monkeypatch):
    """GET /api/settings must still merge indexed channels into
    saved_channels with the identical entry shape (the offloaded body
    cannot change the payload)."""
    import routers.live as live_mod

    monkeypatch.setattr(live_mod, "trigger_live_detection", lambda channel_id: None)
    archive_db.has_pending_jobs()  # ensure schema before direct write
    archive_db.touch_channel_snapshot("twitch", "reconchan")

    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "download_threads" in data  # AppSettings shape
    merged = {str(c.get("id")): c for c in data["saved_channels"]}
    assert "idx_reconchan" in merged
    entry = merged["idx_reconchan"]
    assert entry["twitchSlug"] == "reconchan"
    # The reconcile is persisted (not just response-only), as before.
    assert "idx_reconchan" in {str(c.get("id")) for c in settings_mgr.get().saved_channels}


@pytest.mark.asyncio
async def test_update_settings_validation_raises_off_loop(client):
    """The 400 guards inside the sync body must propagate through the
    to_thread await unchanged (status + detail identical)."""
    resp = await client.post("/api/settings", json={"download_folder": "C:\\Windows"})
    assert resp.status_code == 400
    assert "system-critical path" in resp.json()["detail"]
