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
    for url in _PROBE_URLS:
        warm = await client.get(url)
        assert warm.status_code == 200, url

    with _ExclusiveHolder(db_path):
        per_url: dict[str, list[float]] = {u: [] for u in _PROBE_URLS}
        poller_errors: list[BaseException] = []
        stop = threading.Event()
        index = itertools.count()

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
        t_post0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                client.post("/api/settings", json=payload), timeout=60
            )
        finally:
            stop.set()
            poller.join(timeout=15)
        post_elapsed = time.perf_counter() - t_post0

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
    # 2) The contract: every concurrent probe answered fast while it spun.
    #    Per-endpoint, so a failure names the route that blocked rather than
    #    just "something was slow".
    assert any(per_url.values()), "poller captured nothing"
    for url, got in per_url.items():
        assert got, f"{url} was never sampled"
        assert max(got) < LIVE_BUDGET_S, (
            f"{len(got)} probes, worst {url} round-trip {max(got)*1000:.0f}ms"
            " while a WAL-busy save spun — the event loop was blocked"
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
