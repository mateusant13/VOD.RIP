"""Desktop window lifecycle — PyWebView + system tray integration."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional

_logger = logging.getLogger(__name__)

_window: Any = None
_tray: Any = None
_allow_close = False
_show_window_cb: Optional[Callable[[], None]] = None
_save_geometry_cb: Optional[Callable[[], None]] = None
_flush_geometry_cb: Optional[Callable[[], None]] = None


def register_window(window: Any) -> None:
    global _window
    _window = window


def register_tray(tray: Any) -> None:
    global _tray
    _tray = tray


def set_show_window_callback(cb: Callable[[], None]) -> None:
    global _show_window_cb
    _show_window_cb = cb


def set_save_geometry_callback(cb: Callable[[], None]) -> None:
    global _save_geometry_cb
    _save_geometry_cb = cb


def set_flush_geometry_callback(cb: Callable[[], None]) -> None:
    global _flush_geometry_cb
    _flush_geometry_cb = cb


def _flush_frontend_state(*, fast: bool = False) -> None:
    """Read panel layout from the UI and persist to settings.json before shutdown."""
    if _window is None or not hasattr(_window, "evaluate_js"):
        return
    try:
        import json

        from services.settings import SettingsManager

        raw = _window.evaluate_js(
            "(function(){try{return window.__vodripReadPanelLayout"
            "?JSON.stringify(window.__vodripReadPanelLayout()):null;}catch(e){return null;}})()"
        )
        if raw and raw not in ("null", "undefined"):
            layout = json.loads(raw)
            mgr = SettingsManager()
            current = mgr.get()
            current.panel_layout = layout
            mgr.save(current)
            _logger.info("Panel layout saved on exit")
            return
        if fast:
            return
        _window.evaluate_js(
            "(function(){try{if(window.__vodripFlushPanelLayout)"
            "window.__vodripFlushPanelLayout();}catch(e){}})();"
        )
        time.sleep(0.35)
    # ponytail: broad except Exception — narrow to specific exception types
    except Exception as exc:
        _logger.debug("flush frontend state: %s", exc)


def on_user_close_attempt() -> bool:
    """Intercept window close — hide to tray unless a full exit was requested."""
    global _allow_close
    if _allow_close:
        return True
    # Do not call evaluate_js here — it blocks the WebView UI thread and can
    # freeze or crash Edge Chromium when the user clicks the window X button.
    if _flush_geometry_cb:
        try:
            _flush_geometry_cb()
        # ponytail: broad except Exception — narrow to specific exception types
        except Exception as exc:
            _logger.debug("flush geometry on hide: %s", exc)
    elif _save_geometry_cb:
        try:
            _save_geometry_cb()
        # ponytail: broad except Exception — narrow to specific exception types
        except Exception as exc:
            _logger.debug("save geometry on hide: %s", exc)
    try:
        if _window is not None:
            _window.hide()
    # ponytail: broad except Exception — narrow to specific exception types
    except Exception as exc:
        _logger.debug("hide window: %s", exc)
    return False


def show_window() -> None:
    try:
        if _window is not None:
            _window.show()
            if hasattr(_window, "restore"):
                _window.restore()
            _raise_window_foreground(_window)
        elif _show_window_cb:
            _show_window_cb()
    # ponytail: broad except Exception — narrow to specific exception types
    except Exception as exc:
        _logger.debug("show window: %s", exc)


def _raise_window_foreground(window: Any) -> None:
    """Best-effort bring the native window above other apps (Windows)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        SW_RESTORE = 9
        ASFW_ANY = 0xFFFFFFFF
        GA_ROOT = 2

        try:
            user32.AllowSetForegroundWindow(ASFW_ANY)
        # ponytail: broad except Exception — narrow to specific exception types
        except Exception:
            pass

        hwnd = 0
        native = getattr(window, "native", None)
        if native is not None:
            hwnd = int(
                getattr(native, "Handle", None)
                or getattr(native, "hwnd", None)
                or native
                or 0
            )
        if not hwnd:
            hwnd = _find_vodrip_hwnd_windows()
        if not hwnd:
            return

        root = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
        user32.ShowWindow(root, SW_RESTORE)
        user32.SetForegroundWindow(root)
    # ponytail: broad except Exception — narrow to specific exception types
    except Exception as exc:
        _logger.debug("raise window foreground: %s", exc)


def _find_vodrip_hwnd_windows() -> int:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    found: list[int] = []

    def _cb(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(512)
        if user32.GetWindowTextW(hwnd, title, 512) and title.value.startswith("VOD.RIP"):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found[0] if found else 0


def request_app_exit() -> None:
    """Fully quit the application (tray Quit, Settings exit, /api/exit)."""
    global _allow_close
    _allow_close = True

    def _do_exit() -> None:
        # Hide immediately — user sees the app close without waiting for teardown.
        if _window is not None:
            try:
                _window.hide()
            # ponytail: broad except Exception — narrow to specific exception types
            except Exception as exc:
                _logger.debug("hide window on exit: %s", exc)

        _flush_frontend_state(fast=True)
        if _flush_geometry_cb:
            try:
                _flush_geometry_cb()
            # ponytail: broad except Exception — narrow to specific exception types
            except Exception as exc:
                _logger.debug("flush geometry on exit: %s", exc)

        if _tray is not None:
            try:
                _tray.stop()
            # ponytail: broad except Exception — narrow to specific exception types
            except Exception as exc:
                _logger.debug("stop tray: %s", exc)

        from services.shutdown_util import shutdown_downloads_and_children

        shutdown_downloads_and_children()

        try:
            import os as _os

            from services.server_lifecycle import stop_api_server

            port = int(_os.environ.get("PORT", 7897))
            # Process is exiting — signal uvicorn but do not wait up to 4s for the port.
            stop_api_server(port, wait_for_port=False)
        # ponytail: broad except Exception — narrow to specific exception types
        except Exception as exc:
            _logger.debug("stop api server: %s", exc)

        if _window is not None:
            try:
                _window.destroy()
            # ponytail: broad except Exception — narrow to specific exception types
            except Exception as exc:
                _logger.debug("destroy window: %s", exc)

        sys.exit(0)

    # Run teardown off the WebView closing thread so evaluate_js / destroy do not deadlock.
    threading.Thread(target=_do_exit, daemon=True).start()


def read_window_geometry() -> Optional[Dict[str, Any]]:
    """Snapshot current native window size/position for persistence."""
    if _window is None:
        return None
    geom: Dict[str, Any] = {}
    try:
        if hasattr(_window, "width") and _window.width:
            geom["width"] = int(_window.width)
        if hasattr(_window, "height") and _window.height:
            geom["height"] = int(_window.height)
        if hasattr(_window, "x") and _window.x is not None:
            geom["x"] = int(_window.x)
        if hasattr(_window, "y") and _window.y is not None:
            geom["y"] = int(_window.y)
    # ponytail: broad except Exception — narrow to specific exception types
    except Exception as exc:
        _logger.debug("read window geometry: %s", exc)
    return geom or None
