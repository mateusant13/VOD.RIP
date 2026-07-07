"""Launch script for the Python Kick & Twitch Downloader"""
import atexit
import faulthandler
import logging
import subprocess
import sys
import os
import traceback

# ponytail: YTDLP_NO_PLUGINS via ytdlp_env.py — blocks getpot_wpc Chrome spawning
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

    # dev-all.mjs releases the port before spawning us; skip duplicate work unless standalone.
    if os.environ.get("VODRIP_SKIP_PORT_RELEASE", "").strip() != "1":
        from services.server_lifecycle import release_api_port

        release_api_port(port, skip_pid=os.getpid())

    # Install deps if needed
    assert_ytdlp_safe()
    try:
        import fastapi  # noqa: F401
        import yt_dlp  # noqa: F401
    except ImportError:
        print("Installing dependencies...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
    
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
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=use_reload)

if __name__ == "__main__":
    main()
