"""Launch script for the Python Kick & Twitch Downloader"""
import subprocess
import sys
import os

def main():
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
    uvicorn.run("main:app", host="0.0.0.0", port=int(port), reload=True)

if __name__ == "__main__":
    main()
