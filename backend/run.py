"""Launch script for the Python Kick & Twitch Downloader"""
import atexit
import faulthandler
import logging
import subprocess
import sys
import os
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
