"""Windows run-at-boot: HKCU Run key for the VOD-RIP.exe --autostart launch.

Autostart is per-user (HKCU, no admin). The Run value points at the frozen
exe with --autostart so the launcher can distinguish a boot launch from a
user launch and set VODRIP_BACKGROUND=1 (quiet pacing). Dev builds (not
frozen) are never registered — sys.executable is the Python interpreter,
not the app exe. ponytail: Windows only; other OSes are no-ops (the app
has no autostart story there yet).
"""

from __future__ import annotations

import logging
import os
import sys

_logger = logging.getLogger(__name__)

try:
    import winreg
except ImportError:  # non-Windows
    winreg = None

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "VOD.RIP"

_BACKGROUND_ENV = "VODRIP_BACKGROUND"


def background_mode() -> bool:
    """True when launched via --autostart (VODRIP_BACKGROUND=1): the app
    runs hidden-to-tray at boot, so background work (transcribe, index,
    chat capture) should pace itself quieter — fewer CPU lanes, wider
    gaps, smaller per-pass budgets. A user launch never sets this."""
    return os.environ.get(_BACKGROUND_ENV, "") == "1"


def _command() -> str:
    """'<exe> --autostart' — the value stored in the Run key. Computed per
    call (sys.executable can differ under tests; the registry path is
    written only on settings saves, so caching buys nothing)."""
    return f'"{sys.executable}" --autostart'


def set_windows_autostart(enabled: bool) -> bool:
    """Register/remove the HKCU Run entry. True on success (or no-op
    non-Windows/dev); False when the registry write fails."""
    if os.name != "nt" or winreg is None or not getattr(sys, "frozen", False):
        return True  # not applicable — setting still persists, launch is manual
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(key, _VALUE_NAME)
                except FileNotFoundError:
                    pass  # not registered — nothing to remove
        return True
    except OSError as exc:
        _logger.warning("autostart registry update failed: %s", exc)
        return False


def windows_autostart_enabled() -> bool:
    """Read back whether the Run key currently points at --autostart."""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _VALUE_NAME)
        return value == _command()
    except FileNotFoundError:
        return False
    except OSError:
        return False


# --- module self-check (pure logic — no registry access) -------------------

assert _command() == f'"{sys.executable}" --autostart', "Run value must carry --autostart"
