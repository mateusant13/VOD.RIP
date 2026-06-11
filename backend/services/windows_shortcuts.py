"""Windows Start Menu shortcuts for VOD.RIP (portable and installed builds)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_START_MENU_FOLDER = "VOD.RIP"
_APP_NAME = "VOD.RIP"


def _programs_dir() -> Path:
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / _START_MENU_FOLDER


def _escape_ps(value: str) -> str:
    return value.replace("'", "''")


def ensure_windows_shortcuts(exe_path: Path, working_dir: Path, *, desktop: bool = False) -> None:
    """Create or refresh Start Menu (and optional Desktop) shortcuts."""
    if os.name != "nt":
        return
    if not exe_path.is_file():
        return

    programs = _programs_dir()
    programs.mkdir(parents=True, exist_ok=True)
    start_lnk = programs / f"{_APP_NAME}.lnk"

    lines = [
        "$WshShell = New-Object -ComObject WScript.Shell",
        f"$s = $WshShell.CreateShortcut('{_escape_ps(str(start_lnk))}')",
        f"$s.TargetPath = '{_escape_ps(str(exe_path.resolve()))}'",
        f"$s.WorkingDirectory = '{_escape_ps(str(working_dir.resolve()))}'",
        f"$s.IconLocation = '{_escape_ps(str(exe_path.resolve()))},0'",
        f"$s.Description = '{_APP_NAME} — Kick & Twitch downloader'",
        "$s.Save()",
    ]

    if desktop:
        desktop_dir = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
        if desktop_dir.is_dir():
            desktop_lnk = desktop_dir / f"{_APP_NAME}.lnk"
            lines.extend([
                f"$d = $WshShell.CreateShortcut('{_escape_ps(str(desktop_lnk))}')",
                f"$d.TargetPath = '{_escape_ps(str(exe_path.resolve()))}'",
                f"$d.WorkingDirectory = '{_escape_ps(str(working_dir.resolve()))}'",
                f"$d.IconLocation = '{_escape_ps(str(exe_path.resolve()))},0'",
                f"$d.Description = '{_APP_NAME} — Kick & Twitch downloader'",
                "$d.Save()",
            ])

    script = "\n".join(lines)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            timeout=30,
            creationflags=_NO_WINDOW,
            check=False,
        )
        logger.info("Start Menu shortcut ensured: %s", start_lnk)
    except Exception as exc:
        logger.debug("Start Menu shortcut failed: %s", exc)


def resolve_windows_exe(install_dir: Path) -> Path:
    for name in ("VOD-RIP.EXE", "VOD-RIP.exe", "vod-rip.exe"):
        candidate = install_dir / name
        if candidate.is_file():
            return candidate
    return install_dir / "VOD-RIP.EXE"


def install_dir_from_runtime() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent
