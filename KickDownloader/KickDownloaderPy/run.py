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

    port = os.environ.get("PORT", "7897")
    
    # Install deps if needed
    try:
        import fastapi
        import yt_dlp
    except ImportError:
        print("Installing dependencies...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
    
    print("================================================")
    print("  Kick & Twitch Downloader v2.0 (Python)")
    print(f"  Open http://localhost:{port} in your browser")
    print("================================================")
    
    import uvicorn

    # Uvicorn --reload on Windows often leaves a hung parent that accepts
    # connections but never responds (Playwright + file-watch reload). Opt in
    # with KICK_RELOAD=1 when you need auto-reload.
    use_reload = os.environ.get("KICK_RELOAD", "").strip() == "1"
    uvicorn.run("main:app", host="0.0.0.0", port=int(port), reload=use_reload)

if __name__ == "__main__":
    main()
