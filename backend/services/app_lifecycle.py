"""Desktop window lifecycle — PyWebView + system tray integration."""

from __future__ import annotations

import logging
import os
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


def _flush_frontend_state() -> None:
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
        _window.evaluate_js(
            "(function(){try{if(window.__vodripFlushPanelLayout)"
            "window.__vodripFlushPanelLayout();}catch(e){}})();"
        )
        time.sleep(0.35)
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
        except Exception as exc:
            _logger.debug("flush geometry on hide: %s", exc)
    elif _save_geometry_cb:
        try:
            _save_geometry_cb()
        except Exception as exc:
            _logger.debug("save geometry on hide: %s", exc)
    try:
        if _window is not None:
            _window.hide()
    except Exception as exc:
        _logger.debug("hide window: %s", exc)
    return False


def show_window() -> None:
    try:
        if _window is not None:
            _window.show()
            if hasattr(_window, "restore"):
                _window.restore()
        elif _show_window_cb:
            _show_window_cb()
    except Exception as exc:
        _logger.debug("show window: %s", exc)


def request_app_exit() -> None:
    """Fully quit the application (tray Quit, Settings exit, /api/exit)."""
    global _allow_close
    _allow_close = True

    def _do_exit() -> None:
        _flush_frontend_state()
        if _flush_geometry_cb:
            try:
                _flush_geometry_cb()
            except Exception as exc:
                _logger.debug("flush geometry on exit: %s", exc)

        if _tray is not None:
            try:
                _tray.stop()
            except Exception as exc:
                _logger.debug("stop tray: %s", exc)

        from services.shutdown_util import shutdown_downloads_and_children

        shutdown_downloads_and_children()

        try:
            import os as _os

            from services.server_lifecycle import stop_api_server

            port = int(_os.environ.get("PORT", 7897))
            stop_api_server(port)
        except Exception as exc:
            _logger.debug("stop api server: %s", exc)

        if _window is not None:
            try:
                _window.destroy()
            except Exception as exc:
                _logger.debug("destroy window: %s", exc)

        os._exit(0)

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
    except Exception as exc:
        _logger.debug("read window geometry: %s", exc)
    return geom or None
