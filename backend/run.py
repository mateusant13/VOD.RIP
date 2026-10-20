"""Launch script for the Python Kick & Twitch Downloader"""
import atexit
import faulthandler
import logging
import socket
import subprocess
import sys
import os
import threading
import time
import traceback

# ponytail: YTDLP_NO_PLUGINS=1 set in services/ytdlp_env.py — silences the
# bundled bgutil duplicate-register warning (PoTokenProvider BgUtilHTTP already
# registered) that fires when yt-dlp 2026.07.04 plugin discovery runs twice.
# getpot_wpc is still blocked by ytdlp_guard.assert_ytdlp_safe().
from services import ytdlp_env  # noqa: F401
from services.ytdlp_guard import assert_ytdlp_safe


def _install_fatal_hooks() -> None:
    faulthandler.enable(all_threads=True)

    def _excepthook(exc_type, exc, tb):
        print("\n===== UNCAUGHT EXCEPTION =====", flush=True)
        traceback.print_exception(exc_type, exc, tb)

    sys.excepthook = _excepthook


# How long to leave a starting server alone, how often to look, and how many
# consecutive misses mean "this process no longer serves anything".
_WATCHDOG_GRACE_SEC = 60.0
_WATCHDOG_INTERVAL_SEC = 5.0
_WATCHDOG_STRIKES = 3


def _start_listener_watchdog(host: str, port: int) -> None:
    """Exit this process once it stops owning its listening socket.

    Uvicorn's accept loop can die on Windows without stopping the process. The
    real signature, captured 2026-10-20 from the dev log:

        OSError: [WinError 64] The specified network name is no longer available
        ERROR asyncio: Accept failed on a socket
        socket: <asyncio.TransportSocket fd=1992 ... laddr=('127.0.0.1', 7897)>

    After that line the process kept running, kept its 367 MB RSS, and kept the
    archive DB open - while nothing was listening. `uvicorn.run()` never
    returned, so nothing ever tore it down.

    That zombie is worse than a crash, because a supervisor cannot tell it from
    a healthy instance: it is a SECOND writer on the same SQLite file, so the
    healthy process hits `database is locked` on its first write, its health
    check fails, and it gets restarted into the same wall. Observed on this box:
    two `run.py` processes, one without a listener, `database is locked` in
    `worker_heartbeat`, and a `GET /api/channel/outcome-parks` that never
    answered until the zombie was killed.

    A refused connection means no listener - a busy one still accepts into its
    backlog - and three refusals 5 s apart cannot be a reload blip.
    """
    if os.environ.get("VODRIP_NO_LISTENER_WATCHDOG") == "1":
        return
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host

    def _probe() -> bool:
        try:
            with socket.create_connection((probe_host, port), timeout=2.0):
                return True
        except OSError:
            return False

    def _watch() -> None:
        started = time.monotonic()
        strikes = 0
        # Arm only after the listener has answered once. A cold boot on this box
        # takes longer than the grace period (archive open + WAL checkpoint), and
        # a watchdog that counts boot seconds as strikes would kill a perfectly
        # healthy process mid-startup - a restart loop that looks exactly like
        # the bug it is meant to prevent.
        armed = False
        while True:
            time.sleep(_WATCHDOG_INTERVAL_SEC)
            if not armed:
                if _probe():
                    armed = True
                    started = time.monotonic()
                continue
            if time.monotonic() - started < _WATCHDOG_GRACE_SEC:
                continue
            if _probe():
                strikes = 0
                continue
            strikes += 1
            if strikes >= _WATCHDOG_STRIKES:
                logging.getLogger("VOD.RIP").error(
                    "listener watchdog: nothing is listening on %s:%d after %d "
                    "checks - exiting so the supervisor restarts a clean process "
                    "instead of leaving a DB-holding zombie",
                    probe_host,
                    port,
                    strikes,
                )
                sys.stdout.flush()
                sys.stderr.flush()
                # os._exit, not sys.exit: the accept loop is already broken and a
                # graceful shutdown would wait on the same dead loop.
                os._exit(1)

    threading.Thread(target=_watch, name="listener-watchdog", daemon=True).start()


def _install_logging() -> None:
    """Console INFO logs for dev (frozen EXE configures logging in __main_launcher__)."""
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("VOD.RIP.youtube").setLevel(logging.INFO)
    logging.getLogger("VOD.RIP.preview_timing").setLevel(logging.INFO)
    # Central ERROR ring/file (latest-500, sanitized) — must attach even in
    # dev so uncaught server/app 500s land in the inspectable error log.
    try:
        from services.error_log import install_error_handler

        install_error_handler()
    except Exception:
        pass


def _install_shutdown_hook() -> None:
    def _atexit_shutdown() -> None:
        try:
            from services.shutdown_util import shutdown_downloads_and_children

            shutdown_downloads_and_children()
        except Exception:
            pass

    atexit.register(_atexit_shutdown)


def main():
    _install_fatal_hooks()
    _install_logging()
    _install_shutdown_hook()
    # Truthful log ordering: when stdout is a PIPE (dev supervisor, Tee-Object,
    # `| tee`) Python block-buffers it, so the banner and uvicorn's own lines
    # land in the log out of order — the banner once appeared 3.1s AFTER
    # "Uvicorn running". Line-buffering stdout makes the piped log match real
    # event order. No-op-safe for a TTY and for a redirected file handle.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(line_buffering=True)
        except Exception:
            pass
    # Debug mode removed — the `--debug` flag pointed to a missing `debug_cli.py`.
    # ponytail: Restore when a real debug CLI is built. For now, ignore --debug.
    if "--debug" in sys.argv:
        print("Debug mode is not available in this build.", file=sys.stderr)
        sys.argv = [a for a in sys.argv if a != "--debug"]

    port = int(os.environ.get("PORT", "7897"))
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    # dev-all.mjs releases the port before spawning us; skip duplicate work unless standalone.
    if os.environ.get("VODRIP_SKIP_PORT_RELEASE", "").strip() != "1":
        from services.server_lifecycle import guard_api_port, release_api_port

        # First-wins: a healthy API on the port belongs to another supervisor
        # (dev-all session, tray app). An automatic restart (hub/watchdog)
        # must never kill it — that caused a murder loop where a hub-restarted
        # instance POSTed /api/exit to the dev API 0.5s after it bound.
        # VODRIP_TAKE_PORT=1 forces the old takeover behavior.
        if guard_api_port(port):
            sys.exit(0)
        release_api_port(port, skip_pid=os.getpid())

    # Install deps if needed
    assert_ytdlp_safe()
    try:
        import fastapi  # noqa: F401
        import yt_dlp  # noqa: F401
    except ImportError:
        if os.environ.get("VODRIP_ALLOW_PIP_INSTALL") == "1":
            print("Installing dependencies...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
        else:
            print("WARNING: Missing dependencies. Set VODRIP_ALLOW_PIP_INSTALL=1 to auto-install.")
    
    ui_url = os.environ.get("KICK_UI_URL", "http://localhost:5173")
    print("================================================")
    print("  Kick & Twitch Downloader v2.0 (Python)")
    print(f"  UI (dev):     {ui_url}  — npm run dev")
    print(f"  API:          http://localhost:{port}")
    print("  (Set KICK_SERVE_UI=1 after npm run build-copy to serve UI on API port)")
    print("================================================")
    
    import uvicorn

    # Uvicorn --reload on Windows often leaves a hung parent that accepts
    # connections but never responds (Playwright + file-watch reload). Opt in
    # with KICK_RELOAD=1 when you need auto-reload.
    use_reload = os.environ.get("KICK_RELOAD", "").strip() == "1"

    # ponytail: Windows TIME_WAIT holds the socket for ~30s after the old
    # process exits. dev-all kills the old API, but the socket lingers.
    # Retry bind with backoff instead of crashing. uvicorn reports bind
    # failure as SystemExit (not OSError), so catch both. If the port was
    # taken by a healthy VOD.RIP API meanwhile (lost supervisor race),
    # first-wins: exit 0 instead of retrying a doomed bind.
    import time as _time

    bind_host = (os.environ.get("VODRIP_BIND") or "127.0.0.1").strip() or "127.0.0.1"

    # One watchdog for the process, started before the first bind attempt so a
    # process that loses its listener later cannot outlive it (see the docstring).
    _start_listener_watchdog(bind_host, port)

    max_bind_attempts = 3
    for attempt in range(1, max_bind_attempts + 1):
        try:
            uvicorn.run("main:app", host=bind_host, port=port, reload=use_reload)
            break
        except (OSError, SystemExit) as exc:
            if isinstance(exc, OSError) and exc.errno != 10048:  # WSAEADDRINUSE
                raise
            from services.server_lifecycle import vodrip_api_healthy

            if vodrip_api_healthy(port):
                print(f"VOD.RIP API won :{port} — exiting")
                sys.exit(0)
            if attempt < max_bind_attempts:
                wait_s = attempt * 2
                print(
                    f"Port {port} still in TIME_WAIT (attempt {attempt}/{max_bind_attempts}) — retrying in {wait_s}s",
                    file=sys.stderr,
                )
                _time.sleep(wait_s)
                continue
            raise

if __name__ == "__main__":
    main()
