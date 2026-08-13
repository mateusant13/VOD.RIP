"""background_server.py — detached "slow and steady" background daemon.

When the app is CLOSED this process keeps the archive alive at low
resource cost: scheduler ingests (Twitch/Kick/YouTube metadata, chat
backfill, transcribe enqueue), live-chat capture, mention/entity
scanning, and periodic disk hygiene + VOD retention all continue, paced
by VODRIP_BACKGROUND=1 (6-min passes, budget-1 enqueues, 2.5x chat
gaps) so the machine stays quiet no matter how long the app stays
closed. The transcribe queue keeps its own detached consumer
(worker_server.py), spawned here on demand whenever jobs exist.

Ownership handoff is heartbeat-based, mirroring worker_server.py:
  - The app stamps 'app-activity' every 30s while it lives. As long as
    that heartbeat is fresh the app owns the background work and this
    daemon stands down (checks every TICK_S).
  - When the heartbeat goes stale (app closed/crashed) this daemon
    starts the in-process services, spawns the archive worker for any
    pending queue, and runs periodic hygiene/retention.
  - When the app comes back the services are stopped (watchdog flushes
    its sinks) and the daemon returns to watching.

First-wins guard: a fresh 'background' heartbeat means another
background daemon already owns the machine — print and exit 0 (the
app spawns this at every boot; only the first instance survives).

Stdlib only.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
LOG_PATH = BACKEND_DIR / "logs" / "background.log"

# App-liveness: the app stamps 'app-activity' every 30s while alive, so
# 120s without a stamp (~4 missed) means it is closed.
APP_GONE_AFTER_S = 120.0
TICK_S = 60.0
HEARTBEAT_EVERY_S = 45.0
# Disk hygiene + VOD retention cadence (the app runs these once at boot).
HYGIENE_EVERY_S = 6 * 3600.0
TAG = "background"

_log = logging.getLogger(__name__)


def _app_alive() -> bool:
    from services import archive_db

    return archive_db.worker_live(age_s=int(APP_GONE_AFTER_S), tag="app-activity")


def _maybe_spawn_worker() -> None:
    """(Re)start the detached archive worker when the queue has work and
    no worker owns it. worker_server's own first-wins guard dedupes
    against an in-process or other detached worker, so a losing spawn is
    a harmless immediate exit 0."""
    from services import archive_db

    if not archive_db.has_pending_jobs():
        return
    if archive_db.worker_live(age_s=45):
        return
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--archive-worker-launch"]
    else:
        cmd = [sys.executable, str(BACKEND_DIR / "worker_server.py")]
    try:
        subprocess.Popen(
            cmd,
            cwd=str(BACKEND_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                if os.name == "nt" else 0
            ),
        )
        _log.info("spawned archive worker (queue has pending jobs)")
    except Exception:
        _log.debug("archive worker spawn failed", exc_info=True)


# --- service lifecycle (mirrors app.py lifespan; all start/stop are
# --- idempotent, so repeated ticks while the app stays closed are no-ops)

_STARTED: set[str] = set()


def _start_services() -> None:
    from services import archive_scheduler, archive_watchdog, entity_watch, mention_irc

    for name, start in (
        ("scheduler", archive_scheduler.start_archive_scheduler),
        ("watchdog", archive_watchdog.start_archive_watchdog),
        ("mention", mention_irc.start_mention_irc),
        ("entity", entity_watch.start_entity_watcher),
    ):
        if name in _STARTED:
            continue
        try:
            start()
            _STARTED.add(name)
        except Exception:
            _log.debug("%s start failed", name, exc_info=True)
    if _STARTED:
        _log.info("background services running: %s", ", ".join(sorted(_STARTED)))


def _stop_services() -> None:
    from services import archive_scheduler, archive_watchdog, entity_watch, mention_irc

    for name, stop in (
        ("scheduler", archive_scheduler.stop_archive_scheduler),
        ("watchdog", archive_watchdog.stop_archive_watchdog),
        ("mention", mention_irc.stop_mention_irc),
        ("entity", entity_watch.stop_entity_watcher),
    ):
        if name not in _STARTED:
            continue
        try:
            stop()
        except Exception:
            _log.debug("%s stop failed", name, exc_info=True)
    if _STARTED:
        _log.info("background services stopped")
    _STARTED.clear()


def _run_hygiene() -> None:
    """Periodic disk hygiene + VOD retention (mirror of the app's
    boot-maintenance daemon; best-effort, never fatal)."""
    try:
        from services.disk_hygiene import run_startup_hygiene

        run_startup_hygiene()
    except Exception:
        _log.debug("hygiene failed", exc_info=True)
    try:
        from services.archive_retention import enforce_archive_vod_retention

        stats = enforce_archive_vod_retention()
        if stats.get("deleted_files"):
            _log.info(
                "retention: removed %d video file(s), cleared %d row(s)",
                stats.get("deleted_files"),
                stats.get("cleared_rows"),
            )
    except Exception:
        _log.debug("retention failed", exc_info=True)


def main() -> int:
    from services import archive_db

    (BACKEND_DIR / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_PATH),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _log.info("background daemon starting (log %s)", LOG_PATH)

    # First-wins guard BEFORE starting anything: another background daemon
    # (e.g. from the previous app session, still alive) owns the machine.
    # A guard failure (SQLite lock contention) must never let this process
    # run unguarded — exit 1; the app's next boot respawns it.
    try:
        if archive_db.worker_live(age_s=90, tag=TAG):
            _log.info("background daemon already running — nothing to do.")
            return 0
    except Exception:
        _log.warning(
            "first-wins guard failed — exiting instead of running unguarded",
            exc_info=True,
        )
        return 1

    # Quiet pacing for every pass/enqueue/child this process starts. Read
    # per-pass by the scheduler and per-interval by the transcribe child.
    os.environ["VODRIP_BACKGROUND"] = "1"

    stop = threading.Event()

    def _heartbeat() -> None:
        while not stop.wait(HEARTBEAT_EVERY_S):
            try:
                archive_db.worker_heartbeat(TAG, pid=os.getpid())
            except Exception:
                _log.debug("heartbeat failed", exc_info=True)

    threading.Thread(target=_heartbeat, daemon=True, name="background-hb").start()
    _log.info("background daemon watching (will take over when app closes)")

    last_hygiene = time.monotonic()
    try:
        while True:
            # First-wins is only checked at start; a replacement daemon that
            # won the guard later (heartbeat aged out) must make the loser
            # exit instead of piling up (41 duplicates observed). Exit 0
            # whenever a DIFFERENT process owns the live heartbeat.
            try:
                owner = archive_db.worker_heartbeat_owner(TAG, age_s=90)
                if owner is not None and owner != os.getpid():
                    _log.info("another background daemon took over — exiting.")
                    return 0
            except Exception:
                _log.debug("take-over check failed", exc_info=True)
            # A test/CLI scratch DB that vanished means the harness that
            # spawned this daemon is gone — never outlive it (spawned with
            # a per-test scratch DB, the guard is invisible between them).
            scratch = os.environ.get("VODRIP_ARCHIVE_DB", "").strip()
            if scratch and not Path(scratch).exists():
                _log.info("scratch DB removed — exiting.")
                return 0
            if _app_alive():
                _stop_services()
                time.sleep(TICK_S)
                continue
            # The detached scheduler keeps ingesting YouTube with the app
            # closed; that needs PO tokens, and the POT server is now a
            # detached orphan that outlives the app. Ping-first + respawn,
            # so this is a no-op while a live POT answers on 4416.
            try:
                from services import youtube_pot_service

                youtube_pot_service.ensure_pot_server_started()
            except Exception:
                _log.debug("POT ensure failed", exc_info=True)
            _start_services()
            _maybe_spawn_worker()
            if time.monotonic() - last_hygiene >= HYGIENE_EVERY_S:
                _run_hygiene()
                last_hygiene = time.monotonic()
            time.sleep(TICK_S)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        _stop_services()
        _log.info("background daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
