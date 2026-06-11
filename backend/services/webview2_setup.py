"""Detect WebView2 on Windows and guide the user to install it from Microsoft.

We intentionally do NOT download or execute installers from inside VOD.RIP —
that pattern is flagged as trojan/dropper behavior by antivirus software.
"""

from __future__ import annotations

import logging
import os
import webbrowser

logger = logging.getLogger(__name__)

_WEBVIEW2_CLSID = "{F3017226-FE2A-4295-8BDF-00B3D09F7BF5}"
_WEBVIEW2_INSTALL_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


def webview2_installed() -> bool:
    if os.name != "nt":
        return True
    try:
        import winreg
    except ImportError:
        return True

    subkey = rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLSID}"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for access in (0, getattr(winreg, "KEY_WOW64_64KEY", 0)):
            try:
                with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | access) as key:
                    version, _ = winreg.QueryValueEx(key, "pv")
                    if version and str(version) not in ("0.0.0.0", "0.0.0"):
                        return True
            except OSError:
                continue
    return False


def ensure_webview2() -> bool:
    """Return True when WebView2 is available. Otherwise show a setup dialog."""
    if webview2_installed():
        return True
    if os.name != "nt":
        return False
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
    root.geometry("480x240")
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
            "Click below to open Microsoft's official installer in your browser.\n"
            "After it finishes, click “Check again” or restart VOD.RIP.\n\n"
            "Tip: the VOD.RIP Setup.exe installer can install WebView2 for you."
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
