"""Detect WebView2 on Windows and guide the user to install it from Microsoft.

We intentionally do NOT download or execute installers from inside VOD.RIP —
that pattern is flagged as trojan/dropper behavior by antivirus software.
"""

from __future__ import annotations

import logging
import os
import time
import webbrowser
from pathlib import Path

logger = logging.getLogger(__name__)

_WEBVIEW2_CLSID = "{F3017226-FE2A-4295-8BDF-00B3D09F7BF5}"
_WEBVIEW2_INSTALL_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
_INVALID_VERSIONS = frozenset(("", "0.0.0.0", "0.0.0"))

# Same registry locations checked by installer/installer.iss (plus 64-bit HKLM).
_REG_PATHS = (
    rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLSID}",
    rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLSID}",
    rf"Software\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLSID}",
)


def _version_ok(version: object) -> bool:
    return bool(version) and str(version) not in _INVALID_VERSIONS


def _reg_has_webview2(hive, subkey: str) -> bool:
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ) as key:
            version, _ = winreg.QueryValueEx(key, "pv")
            return _version_ok(version)
    except OSError:
        return False


def _webview2_on_disk() -> bool:
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        root = os.environ.get(env)
        if not root:
            continue
        app_dir = Path(root) / "Microsoft" / "EdgeWebView" / "Application"
        if not app_dir.is_dir():
            continue
        for child in app_dir.iterdir():
            if child.is_dir() and (child / "msedgewebview2.exe").is_file():
                return True
    return False


def webview2_installed() -> bool:
    if os.name != "nt":
        return True
    try:
        import winreg
    except ImportError:
        return True

    for subkey in _REG_PATHS:
        hive = winreg.HKEY_CURRENT_USER if subkey.startswith("Software") else winreg.HKEY_LOCAL_MACHINE
        if _reg_has_webview2(hive, subkey):
            return True

    return _webview2_on_disk()


def ensure_webview2() -> bool:
    """Return True when WebView2 is available. Otherwise show a setup dialog."""
    if webview2_installed():
        return True
    if os.name != "nt":
        return False

    # Registry can lag a few seconds right after the Setup installer finishes WebView2.
    for _ in range(8):
        time.sleep(0.5)
        if webview2_installed():
            return True

    return _show_setup_dialog()


def open_webview2_download_page() -> None:
    webbrowser.open(_WEBVIEW2_INSTALL_URL)


def _show_setup_dialog() -> bool:
    """Guide the user to install WebView2 from Microsoft (no bundled/silent install)."""
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        logger.warning("WebView2 missing and tkinter unavailable")
        return False

    root = tk.Tk()
    root.title("VOD.RIP — WebView2 required")
    root.geometry("500x250")
    root.resizable(False, False)
    root.configure(bg="#0A0A0A")
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    result = {"ok": False}

    frame = tk.Frame(root, bg="#0A0A0A", padx=24, pady=20)
    frame.pack(expand=True, fill="both")

    tk.Label(
        frame,
        text="Desktop window needs WebView2",
        fg="#ffffff",
        bg="#0A0A0A",
        font=("Segoe UI", 12, "bold"),
        anchor="w",
    ).pack(fill="x")

    tk.Label(
        frame,
        text=(
            "VOD.RIP uses Microsoft's free WebView2 runtime for the native app window.\n\n"
            "If you just ran the VOD.RIP installer, click “Check again” — registration can\n"
            "take a few seconds after setup finishes.\n\n"
            "Otherwise open Microsoft's installer below, complete it, then click “Check again”.\n"
            "You can also use browser mode to run VOD.RIP without the native window."
        ),
        fg="#a1a1aa",
        bg="#0A0A0A",
        font=("Segoe UI", 9),
        justify="left",
        anchor="w",
    ).pack(fill="x", pady=(10, 16))

    status = tk.StringVar(value="")
    status_label = tk.Label(
        frame, textvariable=status, fg="#fbbf24", bg="#0A0A0A", font=("Segoe UI", 9), anchor="w",
    )
    status_label.pack(fill="x", pady=(0, 10))

    btn_row = tk.Frame(frame, bg="#0A0A0A")
    btn_row.pack(fill="x")

    def on_download() -> None:
        open_webview2_download_page()
        status.set("Install WebView2 from the page that opened, then click “Check again”.")

    def on_retry() -> None:
        if webview2_installed():
            result["ok"] = True
            root.destroy()
        else:
            status.set("WebView2 not detected yet. Finish Microsoft's installer, then retry.")

    def on_browser() -> None:
        result["ok"] = False
        root.destroy()

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass

    ttk.Button(btn_row, text="Open Microsoft installer", command=on_download).pack(side="left", padx=(0, 8))
    ttk.Button(btn_row, text="Check again", command=on_retry).pack(side="left", padx=(0, 8))
    ttk.Button(btn_row, text="Use browser mode", command=on_browser).pack(side="right")

    root.mainloop()
    return result["ok"] or webview2_installed()
