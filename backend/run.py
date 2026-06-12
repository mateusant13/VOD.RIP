"""Launch script for the Python Kick & Twitch Downloader"""
import faulthandler
import subprocess
import sys
import os
import traceback


def _install_fatal_hooks() -> None:
    faulthandler.enable(all_threads=True)

    def _excepthook(exc_type, exc, tb):
        print("\n===== UNCAUGHT EXCEPTION =====", flush=True)
        traceback.print_exception(exc_type, exc, tb)

    sys.excepthook = _excepthook


def main():
    _install_fatal_hooks()
    # Debug mode: `python run.py --debug full --spawn-server [--headed]`
    if "--debug" in sys.argv:
        sys.argv = [a for a in sys.argv if a != "--debug"]
        from debug_cli import main as debug_main
        raise SystemExit(debug_main())

    port = int(os.environ.get("PORT", "7897"))

    # dev-all.mjs releases the port before spawning us; skip duplicate work unless standalone.
    if os.environ.get("VODRIP_SKIP_PORT_RELEASE", "").strip() != "1":
        from services.server_lifecycle import release_api_port

        release_api_port(port, skip_pid=os.getpid())

    # Install deps if needed
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
