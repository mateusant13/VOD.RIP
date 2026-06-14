"""
VOD.RIP — Production launch entry point (all platforms).

This module is the single entry point for the packaged application. It is
not imported during development — ``main.py`` is still the dev entry point.

Responsibilities
----------------
1. Set up logging to the platform-appropriate user data directory.
2. Install the global crash handler.
3. Ensure Windows Start Menu shortcuts (portable builds).
4. Start FastAPI / uvicorn on 127.0.0.1.
5. Wait for the server to be ready.
6. Launch the PyWebView native desktop window with system-tray minimize.
7. If PyWebView is unavailable, fall back to the default browser + tray icon.
8. On Quit: cancel all downloads, kill ffmpeg, exit.
"""

import logging
import multiprocessing
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# Prevent console windows from popping up on Windows subprocess calls
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# Import shared path helper from settings (single source of truth).
from services.settings import SettingsManager, _get_appdata_dir

# ---------------------------------------------------------------------------
# Version (single source of truth)
# ---------------------------------------------------------------------------

try:
    from services._version import __version__
except ImportError:
    __version__ = "1.0.0"

# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------


def _get_install_dir() -> Path:
    """Return the directory containing the executable (or the dev source dir)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    # In development, the launcher lives in backend/__main_launcher__.py
    return Path(__file__).resolve().parent


def _get_resources_dir() -> Path:
    """On macOS, bundled resources live in ``Contents/Resources`` inside the
    .app bundle.  On other platforms the resources are alongside the exe."""
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        candidate = _get_install_dir().parent / "Resources"
        if candidate.is_dir():
            return candidate
    return _get_install_dir()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _setup_logging() -> Path:
    """Configure file-based logging and return the path to the log file."""
    app_data = _get_appdata_dir()
    log_dir = app_data / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(str(log_path), encoding="utf-8"),
        ],
    )

    # Suppress chatty dependencies
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    logger = logging.getLogger("VOD.RIP")
    logger.info("=== VOD.RIP %s starting ===", __version__)
    logger.info("Platform: %s | Frozen: %s", sys.platform, getattr(sys, "frozen", False))
    logger.info("Install dir: %s", _get_install_dir())
    logger.info("App data dir: %s", app_data)
    logger.info("Log path: %s", log_path)
    return log_path


# ---------------------------------------------------------------------------
# Environment setup
# ---------------------------------------------------------------------------


def _enable_windows_dpi_awareness() -> None:
    """Enable per-monitor DPI awareness on Windows (prevents blurry UI on scaled displays)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        ctypes.windll.shcore.SetProcessDpiAwarenessContext(-4)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    except Exception:
        pass


def _setup_environment():
    """Set environment variables needed by the runtime before service imports."""
    os.environ["KICK_SERVE_UI"] = "1"
    _enable_windows_dpi_awareness()


def _ensure_start_menu_shortcuts() -> None:
    """Create Start Menu entry on first run (portable zip users)."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        from services.windows_shortcuts import (
            ensure_windows_shortcuts,
            install_dir_from_runtime,
            resolve_windows_exe,
        )

        install_dir = install_dir_from_runtime()
        ensure_windows_shortcuts(resolve_windows_exe(install_dir), install_dir)
    except Exception as exc:
        logging.getLogger("VOD.RIP").debug("Start Menu shortcuts: %s", exc)


def _start_background_update_check() -> None:
    """Check GitHub Releases once per day in packaged builds."""
    if not getattr(sys, "frozen", False):
        return
    try:
        from services.updater import background_check

        threading.Thread(
            target=background_check,
            args=(_get_appdata_dir(), __version__),
            daemon=True,
        ).start()
    except Exception as exc:
        logging.getLogger("VOD.RIP").debug("Background update check: %s", exc)

def _check_linux_webkit() -> None:
    """On Linux, check if WebKitGTK is available before PyWebView tries GTK."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import gi  # type: ignore[import-untyped]
        gi.require_version("WebKit2", "4.1")
        from gi.repository import WebKit2  # noqa: F401
        logger = logging.getLogger("VOD.RIP")
        logger.debug("WebKitGTK 4.1 detected")
    except (ImportError, ValueError):
        # Try 4.0 as fallback
        try:
            gi.require_version("WebKit2", "4.0")
            from gi.repository import WebKit2  # noqa: F401
        except (ImportError, ValueError):
            logger = logging.getLogger("VOD.RIP")
            logger.warning(
                "PyGObject or WebKitGTK not found — native window unavailable on Linux. "
                "Install: sudo apt install python3-gi gir1.2-webkit2-4.1 (Debian/Ubuntu) "
                "or sudo dnf install python3-gobject webkit2gtk4.1 (Fedora)"
            )
    except Exception:
        pass


_WINDOWS_APP_ID = "mateusant13.VODRIP.1"


def _set_windows_app_identity() -> None:
    """Group taskbar / jump-list entries under VOD.RIP with the correct icon."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_WINDOWS_APP_ID)
    except Exception as exc:
        logging.getLogger("VOD.RIP").debug("AppUserModelID: %s", exc)


def _resolve_app_icon_path() -> str | None:
    """Return the best icon path for the window, tray, and taskbar.

    On Windows returns ``.ico``, on macOS returns ``.icns`` (or ``.ico``
    fallback), on Linux returns ``.png`` or ``.ico``.
    """
    candidates: list[Path] = []
    install = _get_install_dir()
    resources = _get_resources_dir()
    dev_root = Path(__file__).resolve().parent.parent

    for base in (install, resources, install / "_internal", dev_root):
        candidates.extend([
            base / "icon.ico",
            base / "assets" / "icon.ico",
            base / "icon.png",
            base / "assets" / "icon.png",
        ])
        if sys.platform == "darwin":
            candidates.extend([
                base / "icon.icns",
                base / "assets" / "icon.icns",
            ])

    seen: set[str] = set()
    for path in candidates:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            return str(path.resolve())
    return None


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def _start_server(port: int):
    """Import the FastAPI app and run uvicorn (blocking — runs in a thread)."""
    logger = logging.getLogger("VOD.RIP")
    from services.server_lifecycle import register_uvicorn_server, should_stop_supervisor

    if should_stop_supervisor():
        return
    try:
        from main import app
        import uvicorn
    except Exception:
        logger.exception("Server thread failed to import app")
        raise

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_config=None,
        access_log=False,
        ws_max_size=16777216,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    register_uvicorn_server(server)
    try:
        server.run()
    except Exception:
        logger.exception("Uvicorn server stopped with an error")
        raise


def _server_supervisor(port: int):
    """Keep the FastAPI server alive — restart automatically after crashes."""
    from services.server_lifecycle import should_stop_supervisor

    logger = logging.getLogger("VOD.RIP")
    while not should_stop_supervisor():
        try:
            _start_server(port)
            if should_stop_supervisor():
                break
            logger.error("Uvicorn exited unexpectedly — restarting in 2s")
        except Exception:
            if should_stop_supervisor():
                break
            logger.exception("Server thread crashed — restarting in 2s")
        if should_stop_supervisor():
            break
        time.sleep(2)
    logger.info("API server supervisor stopped")


def _wait_for_server(port: int, timeout_sec: int = 15) -> bool:
    """Poll the API health endpoint. Returns ``True`` when ready."""
    import requests as http_requests

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            r = http_requests.get(f"http://127.0.0.1:{port}/api/info", timeout=1)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Window geometry persistence
# ---------------------------------------------------------------------------


def _sanitized_window_geometry(wg: dict) -> dict:
    """Drop saved geometry that would place the window off-screen."""
    if not wg:
        return {}
    out = dict(wg)
    try:
        w = int(out.get("width") or 0)
        h = int(out.get("height") or 0)
        if w < 320 or h < 240:
            out.pop("width", None)
            out.pop("height", None)
        x, y = out.get("x"), out.get("y")
        if x is not None and y is not None:
            xi, yi = int(x), int(y)
            if xi < -8000 or yi < -8000 or xi > 12000 or yi > 12000:
                out.pop("x", None)
                out.pop("y", None)
    except Exception:
        return {}
    return out


def _make_geometry_saver(settings_mgr: SettingsManager):
    """Return a debounced callback that persists native window geometry."""
    lock = threading.Lock()
    timer: list = [None]

    def _flush():
        from services.app_lifecycle import read_window_geometry

        geom = read_window_geometry()
        if not geom:
            return
        try:
            current = settings_mgr.get()
            current.window_geometry = geom
            settings_mgr.save(current)
        except Exception as exc:
            logging.getLogger("VOD.RIP").debug("save window geometry: %s", exc)

    def save():
        with lock:
            if timer[0] is not None:
                timer[0].cancel()
            timer[0] = threading.Timer(0.5, _flush)
            timer[0].daemon = True
            timer[0].start()

    def flush():
        with lock:
            if timer[0] is not None:
                timer[0].cancel()
                timer[0] = None
        _flush()

    return save, flush


# ---------------------------------------------------------------------------
# UI launch
# ---------------------------------------------------------------------------


def _launch_pywebview(port: int) -> bool:
    """Open PyWebView with system-tray minimize-on-close behavior."""
    logger = logging.getLogger("VOD.RIP")
    _set_windows_app_identity()
    icon_path = _resolve_app_icon_path()
    if icon_path:
        logger.info("Using application icon: %s", icon_path)

    try:
        import webview
    except ImportError:
        logger.info("pywebview not installed — cannot open native window")
        return False

    from services.app_lifecycle import (
        on_user_close_attempt,
        register_tray,
        register_window,
        request_app_exit,
        set_flush_geometry_callback,
        set_save_geometry_callback,
        show_window,
    )
    from services.tray_service import TrayService

    debug_mode = os.environ.get("VODRIP_DEBUG", "") == "1"
    app_data = _get_appdata_dir()
    settings_mgr = SettingsManager()
    app_settings = settings_mgr.get()
    save_geometry, flush_geometry = _make_geometry_saver(settings_mgr)

    import inspect
    _create_window_sig = inspect.signature(webview.create_window)
    _create_window_params = set(_create_window_sig.parameters.keys())

    kwargs = dict(
        title="VOD.RIP 🪦",
        url=f"http://127.0.0.1:{port}",
        width=1280,
        height=800,
        min_size=(800, 600),
        resizable=True,
        fullscreen=False,
        confirm_close=False,
        text_select=True,
        background_color="#0A0A0A",
    )

    wg = _sanitized_window_geometry(app_settings.window_geometry or {})
    if wg.get("width") and wg.get("height"):
        kwargs["width"] = int(wg["width"])
        kwargs["height"] = int(wg["height"])
    if wg.get("x") is not None and wg.get("y") is not None:
        if "x" in _create_window_params:
            kwargs["x"] = int(wg["x"])
        if "y" in _create_window_params:
            kwargs["y"] = int(wg["y"])

    if "maximized" in _create_window_params and wg.get("maximized", True):
        kwargs["maximized"] = True

    set_save_geometry_callback(save_geometry)
    set_flush_geometry_callback(flush_geometry)

    tray = TrayService(
        port=port,
        shutdown_callback=request_app_exit,
        on_show=show_window,
        downloads_folder=app_settings.download_folder,
        log_path=str(app_data / "logs" / "app.log"),
    )
    register_tray(tray)
    tray_thread = threading.Thread(target=tray.run, daemon=True)
    tray_thread.start()

    if sys.platform == "win32":
        backends = ["edgechromium", "mshtml", None]
    elif sys.platform == "darwin":
        backends = ["cocoa", None]
    else:
        backends = ["gtk", None]
    for backend in backends:
        window = None
        try:
            logger.info("Trying PyWebView backend: %s", backend or "auto")
            window = webview.create_window(**kwargs)
            register_window(window)

            window.events.closing += on_user_close_attempt
            if hasattr(window.events, "resized"):
                window.events.resized += lambda w, h: save_geometry()
            if hasattr(window.events, "moved"):
                window.events.moved += lambda x, y: save_geometry()

            start_kwargs = dict(gui=backend)
            if debug_mode and "debug" in _create_window_params:
                start_kwargs["debug"] = True
            if icon_path:
                start_kwargs["icon"] = icon_path

            webview.start(**start_kwargs)
            logger.info("PyWebView closed normally")
            return True
        except Exception as exc:
            logger.warning("PyWebView backend %s failed: %s", backend or "auto", exc)
            try:
                webview._state = type(webview._state)() if hasattr(webview, "_state") else None
            except Exception:
                pass
            try:
                webview.windows = []
            except Exception:
                pass
            continue

    logger.error("No PyWebView backend works — cannot open native window")
    return False


def _launch_browser_and_tray(port: int, *, webview2_missing: bool = False):
    """Fallback UI: open the default browser + system tray icon."""
    import webbrowser

    logger = logging.getLogger("VOD.RIP")
    logger.warning("Native window unavailable — opening in default browser")

    if os.name == "nt" and not webview2_missing:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            messagebox.showwarning(
                "VOD.RIP",
                "Could not open as a native window.\n\n"
                "The Microsoft Edge WebView2 Runtime may be required.\n"
                "Download it from:\n"
                "https://go.microsoft.com/fwlink/p/?LinkId=2124703\n\n"
                "The app will open in your browser instead."
            )
            root.destroy()
        except Exception:
            pass

    webbrowser.open(f"http://127.0.0.1:{port}")

    from services.app_lifecycle import request_app_exit
    from services.tray_service import TrayService

    app_data = _get_appdata_dir()
    settings_mgr = SettingsManager()
    tray = TrayService(
        port=port,
        shutdown_callback=request_app_exit,
        downloads_folder=settings_mgr.get().download_folder,
        log_path=str(app_data / "logs" / "app.log"),
    )
    tray.run()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def _shutdown(port: int = 7897):
    """Cancel active downloads, stop uvicorn, and release the API port."""
    from services.server_lifecycle import stop_api_server
    from services.shutdown_util import shutdown_downloads_and_children

    logger = logging.getLogger("VOD.RIP")
    logger.info("Shutting down ...")
    shutdown_downloads_and_children()
    stop_api_server(port)
    logger.info("Shutdown complete")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    """Application entry point."""
    multiprocessing.freeze_support()

    log_path = _setup_logging()
    _setup_environment()
    _set_windows_app_identity()

    _ensure_start_menu_shortcuts()
    _start_background_update_check()

    # Linux: warn early if PyWebView's WebKitGTK runtime is missing
    _check_linux_webkit()

    app_data = _get_appdata_dir()
    try:
        from services.crash_handler import install_crash_handler
        install_crash_handler(app_data)
    except Exception as exc:
        logging.getLogger("VOD.RIP").warning("Crash handler install failed: %s", exc)

    port = int(os.environ.get("PORT", 7897))
    logger = logging.getLogger("VOD.RIP")

    logger.info("Starting FastAPI on 127.0.0.1:%d", port)

    from services.server_lifecycle import release_api_port

    release_api_port(port, skip_pid=os.getpid())

    server_thread = threading.Thread(target=_server_supervisor, args=(port,), daemon=True)
    server_thread.start()

    if not _wait_for_server(port):
        logger.critical("FastAPI server did not become ready within timeout")
        sys.exit(1)

    logger.info("Server ready — launching UI")

    webview2_ok = True
    # F-up-2026-06: check WebView2 BEFORE attempting PyWebView on a packaged
    # Windows build. If we know WebView2 is missing, going straight to the
    # setup dialog saves a wasted PyWebView attempt (which would either fail
    # silently with a stale EBWebView cache, or briefly open a half-broken
    # window before crashing). The browser-mode fallback is reserved for
    # systems where we genuinely don't know whether WebView2 is present
    # (e.g. dev mode on Linux/macOS).
    webview2_known_missing = False
    if os.name == "nt" and getattr(sys, "frozen", False):
        try:
            from services.webview2_setup import webview2_installed as _w2i
            if not _w2i():
                logger.info("WebView2 missing at startup — showing setup guide before UI launch")
                from services.webview2_setup import ensure_webview2
                webview2_ok = ensure_webview2()
                if not webview2_ok:
                    webview2_known_missing = True
        except Exception as exc:
            logger.warning("WebView2 pre-check failed: %s", exc)

    if not webview2_known_missing:
        if _launch_pywebview(port):
            # Success: PyWebView window is up. Shut down the API server and exit.
            _shutdown(port)
            sys.exit(0)
        if os.name == "nt" and getattr(sys, "frozen", False):
            try:
                from services.webview2_setup import ensure_webview2, webview2_installed
                if not webview2_installed():
                    webview2_ok = ensure_webview2()
                    if webview2_ok and _launch_pywebview(port):
                        _shutdown(port)
                        sys.exit(0)
            except Exception as exc:
                logger.warning("WebView2 setup failed: %s", exc)
                webview2_ok = False
    else:
        webview2_ok = False

    # PyWebView never succeeded — fall back to the browser mode + tray.
    _launch_browser_and_tray(port, webview2_missing=not webview2_ok)
    _shutdown(port)
    sys.exit(0)


if __name__ == "__main__":
    main()
