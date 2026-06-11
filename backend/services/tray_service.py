"""
VOD.RIP — System tray icon service.

Used as the *fallback* UI when PyWebView is unavailable (e.g. WebView2
missing on Windows 10). The tray icon lets the user:

- Open the WebUI in their default browser
- Open the downloads folder
- View the application log
- Quit the application cleanly (shutting down downloads, ffmpeg, etc.)

On platforms where PyWebView works, this module is not imported at all.
"""

import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy imports — pystray + Pillow may not be installed in dev without the
# distribution extras. If they're missing, TrayService.__init__ warns and
# the caller can decide how to handle it (e.g. just keep the server alive).


class TrayService:
    """System tray icon for VOD.RIP lifecycle management."""

    def __init__(
        self,
        port: int = 7897,
        shutdown_callback: Optional[callable] = None,
        on_show: Optional[callable] = None,
        downloads_folder: Optional[str] = None,
        log_path: Optional[str] = None,
    ):
        self.port = port
        self.shutdown_callback = shutdown_callback
        self.on_show = on_show
        self.downloads_folder = downloads_folder or self._default_downloads()
        self.log_path = log_path
        self._icon = None
        self._running = threading.Event()

    @staticmethod
    def _default_downloads() -> str:
        return str(Path.home() / "Downloads")

    def _get_icon_image(self):
        """Load the tray icon image. Falls back to a coloured pixel."""
        try:
            from PIL import Image

            # Look for icon alongside the executable, then in dev paths
            candidates = []
            if getattr(sys, "frozen", False):
                base = Path(sys.executable).parent
                candidates.extend([
                    base / "icon.ico",
                    base / "icon.png",
                    base / "_internal" / "icon.ico",
                    base / "_internal" / "icon.png",
                ])
            else:
                base = Path(__file__).parent.parent.parent
                candidates.extend([
                    base / "build" / "icon.ico",
                    base / "build" / "icon.png",
                    base / "backend" / "static" / "favicon.ico",
                ])

            for path in candidates:
                if path.is_file():
                    return Image.open(path)

        except Exception as exc:
            logger.debug("Could not load tray icon image: %s", exc)

        # Fallback: tiny green square
        try:
            from PIL import Image
            return Image.new("RGB", (16, 16), color="#53fc18")
        except Exception:
            return None

    def _on_open_ui(self, icon=None, item=None):
        if self.on_show:
            self.on_show()
        else:
            webbrowser.open(f"http://127.0.0.1:{self.port}")

    def _on_open_downloads(self, icon=None, item=None):
        folder = self.downloads_folder or self._default_downloads()
        if os.name == "nt":
            os.startfile(folder)
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", folder])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", folder])

    def _on_open_log(self, icon=None, item=None):
        if self.log_path and os.path.isfile(self.log_path):
            if os.name == "nt":
                os.startfile(self.log_path)
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", "-R", self.log_path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", self.log_path])

    def _on_quit(self, icon=None, item=None):
        if self._icon:
            self._icon.stop()
        if self.shutdown_callback:
            self.shutdown_callback()

    def run(self):
        """Start the tray icon (blocking). Call from the main thread."""
        try:
            import pystray
            from pystray import Menu, MenuItem
        except ImportError:
            logger.warning(
                "pystray not installed — tray icon unavailable. "
                "Install with: pip install pystray Pillow"
            )
            # Block indefinitely so the process doesn't exit
            self._running.wait()
            return

        image = self._get_icon_image()
        if image is None:
            logger.warning("No tray icon image available — skipping tray")
            self._running.wait()
            return

        menu = Menu(
            MenuItem("Open VOD.RIP", self._on_open_ui, default=True),
            MenuItem("Open Downloads Folder", self._on_open_downloads),
            MenuItem("View Log", self._on_open_log),
            Menu.SEPARATOR,
            MenuItem("Quit", self._on_quit),
        )

        self._icon = pystray.Icon(
            "VOD.RIP",
            image,
            "VOD.RIP",
            menu,
        )
        self._icon.run()

    def stop(self):
        """Stop the tray icon from another thread."""
        if self._icon:
            self._icon.stop()
        self._running.set()
