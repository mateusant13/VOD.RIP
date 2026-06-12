"""
VOD.RIP — Crash handler.

Installs a global exception hook that writes detailed crash reports to disk
at ``%APPDATA%/VOD.RIP/crash_reports/`` (or platform equivalent). Also
enables faulthandler so native/C-extension crashes produce a traceback.
"""

import faulthandler
import platform
import sys
import traceback
from datetime import datetime
from pathlib import Path


def install_crash_handler(app_data_dir: Path):
    """Install the global exception hook.

    Must be called early in the launcher, before any service code.
    """
    crash_dir = app_data_dir / "crash_reports"
    crash_dir.mkdir(parents=True, exist_ok=True)

    # Native crash handler (C extensions, segfaults).
    # Windowed builds have sys.stderr=None — write to a file instead.
    if sys.stderr is not None:
        faulthandler.enable(all_threads=True)
    else:
        try:
            fh_path = crash_dir / "faulthandler.log"
            with open(fh_path, "a", encoding="utf-8") as fh_file:
                faulthandler.enable(file=fh_file, all_threads=True)
        except Exception:
            pass

    # Python-level crash handler
    def _handle_uncaught(exc_type, exc_value, exc_tb):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        crash_file = crash_dir / f"crash_{timestamp}.txt"
        try:
            with open(crash_file, "w", encoding="utf-8") as f:
                # Header
                f.write("VOD.RIP Crash Report\n")
                f.write(f"Version: {_get_version()}\n")
                f.write(f"Time: {datetime.now().isoformat()}\n")
                f.write(f"Python: {sys.version}\n")
                f.write(f"Platform: {platform.platform()}\n")
                f.write(f"Frozen: {getattr(sys, 'frozen', False)}\n")
                f.write(f"Executable: {sys.executable}\n")
                f.write("=" * 60 + "\n\n")

                # Exception traceback
                traceback.print_exception(exc_type, exc_value, exc_tb, file=f)

                # All threads (useful for hangs in download workers)
                f.write("\n\n--- All threads ---\n")
                for tid, frame in sys._current_frames().items():
                    f.write(f"\nThread 0x{tid:x}:\n")
                    traceback.print_stack(frame, file=f)

        except Exception:
            pass

    sys.excepthook = _handle_uncaught


def _get_version() -> str:
    try:
        from services._version import __version__
        return __version__
    except Exception:
        return "unknown"
