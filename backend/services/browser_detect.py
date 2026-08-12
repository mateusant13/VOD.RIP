"""Detect the user's real browser for cookie-extension auto-install.

Order of evidence:
1. Windows default HTTPS handler (UserChoice ProgId + open command path).
2. Running browser processes.
3. Profile folders whose cookies/history were touched recently.
4. Standard install locations (Program Files, LocalAppData).

If the first User Data / Profiles folder has no recent files, other known
roots are tried (Chrome Beta/Canary, Opera GX, Firefox profiles.ini).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# Actively used: cookies/history written in the last 14 days.
PROFILE_HOT_SEC = 14 * 24 * 3600
# Still plausible: last 90 days. Older than this is last-resort only.
PROFILE_WARM_SEC = 90 * 24 * 3600

CHROMIUM = frozenset({"chrome", "msedge", "brave", "opera", "opera_gx", "chromium"})
SUPPORTED = frozenset({*CHROMIUM, "firefox"})


@dataclass(frozen=True)
class BrowserHit:
    name: str
    exe: Optional[Path]
    profile_dir: Optional[Path] = None
    source: str = ""
    recency_sec: Optional[float] = None  # age of newest profile marker; None = unknown
    score: float = 0.0
    extras: dict = field(default_factory=dict)

    @property
    def family(self) -> str:
        return "firefox" if self.name == "firefox" else "chromium"


def _pf() -> Path:
    return Path(os.environ.get("ProgramFiles") or r"C:\Program Files")


def _pf86() -> Path:
    return Path(os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)")


def _local() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or "")


def _roaming() -> Path:
    return Path(os.environ.get("APPDATA") or "")


def _exe_candidates(name: str) -> list[Path]:
    local = _local()
    rels: list[Path] = []
    if name == "chrome":
        rels = [
            _pf() / r"Google\Chrome\Application\chrome.exe",
            _pf86() / r"Google\Chrome\Application\chrome.exe",
            local / r"Google\Chrome\Application\chrome.exe",
        ]
    elif name == "msedge":
        rels = [
            _pf86() / r"Microsoft\Edge\Application\msedge.exe",
            _pf() / r"Microsoft\Edge\Application\msedge.exe",
        ]
    elif name == "brave":
        rels = [
            _pf() / r"BraveSoftware\Brave-Browser\Application\brave.exe",
            local / r"BraveSoftware\Brave-Browser\Application\brave.exe",
        ]
    elif name == "opera":
        rels = [
            local / r"Programs\Opera\opera.exe",
            _pf() / r"Opera\opera.exe",
        ]
        # versioned LocalAppData\Programs\Opera\*\opera.exe
        root = local / r"Programs\Opera"
        if root.is_dir():
            rels.extend(sorted(root.glob("*/opera.exe"), key=lambda p: p.stat().st_mtime, reverse=True)[:4])
    elif name == "opera_gx":
        rels = [
            local / r"Programs\Opera GX\opera.exe",
            _pf() / r"Opera GX\opera.exe",
        ]
        root = local / r"Programs\Opera GX"
        if root.is_dir():
            rels.extend(sorted(root.glob("*/opera.exe"), key=lambda p: p.stat().st_mtime, reverse=True)[:4])
    elif name == "firefox":
        rels = [
            _pf() / r"Mozilla Firefox\firefox.exe",
            _pf86() / r"Mozilla Firefox\firefox.exe",
            local / r"Mozilla Firefox\firefox.exe",
        ]
    elif name == "chromium":
        rels = [local / r"Chromium\Application\chrome.exe"]
    seen: list[Path] = []
    for p in rels:
        try:
            if p.is_file() and p not in seen:
                seen.append(p)
        except OSError:
            continue
    which = shutil.which("msedge" if name == "msedge" else ("opera" if name in ("opera", "opera_gx") else name))
    if which:
        wp = Path(which)
        if wp.is_file() and wp not in seen:
            seen.append(wp)
    return seen


def find_browser_exe(name: str) -> Optional[Path]:
    hits = _exe_candidates(name)
    return hits[0] if hits else None


def _newest_mtime(paths: Iterable[Path]) -> Optional[float]:
    newest: Optional[float] = None
    now = time.time()
    for p in paths:
        try:
            if not p.exists():
                continue
            mt = p.stat().st_mtime
            if mt > now + 3600:
                continue
            if newest is None or mt > newest:
                newest = mt
        except OSError:
            continue
    return newest


def _chromium_marker_files(user_data: Path) -> list[Path]:
    out: list[Path] = [user_data / "Local State"]
    # Prefer Default, then any Profile *
    dirs = [user_data / "Default", *sorted(user_data.glob("Profile *"))]
    for d in dirs:
        out.extend([
            d / "Network" / "Cookies",
            d / "Cookies",
            d / "History",
            d / "Preferences",
            d / "Visited Links",
        ])
    return out


def _firefox_marker_files(profile: Path) -> list[Path]:
    return [
        profile / "cookies.sqlite",
        profile / "places.sqlite",
        profile / "prefs.js",
        profile / "sessionstore.jsonlz4",
    ]


def _chromium_profile_roots(name: str) -> list[Path]:
    local, roam = _local(), _roaming()
    if name == "chrome":
        return [
            local / r"Google\Chrome\User Data",
            local / r"Google\Chrome Beta\User Data",
            local / r"Google\Chrome SxS\User Data",
        ]
    if name == "msedge":
        return [
            local / r"Microsoft\Edge\User Data",
            local / r"Microsoft\Edge Beta\User Data",
        ]
    if name == "brave":
        return [local / r"BraveSoftware\Brave-Browser\User Data"]
    if name == "opera":
        return [
            roam / r"Opera Software\Opera Stable",
            roam / r"Opera Software\Opera Developer",
        ]
    if name == "opera_gx":
        return [roam / r"Opera Software\Opera GX Stable"]
    if name == "chromium":
        return [local / r"Chromium\User Data"]
    return []


def _firefox_profiles() -> list[Path]:
    ini = _roaming() / r"Mozilla\Firefox\profiles.ini"
    root = _roaming() / r"Mozilla\Firefox\Profiles"
    found: list[Path] = []
    if ini.is_file():
        try:
            text = ini.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        base = ini.parent
        for m in re.finditer(r"(?im)^Path=(.+)$", text):
            raw = m.group(1).strip()
            p = Path(raw) if Path(raw).is_absolute() else (base / raw)
            if p.is_dir():
                found.append(p)
    if root.is_dir():
        for p in root.iterdir():
            if p.is_dir() and p not in found:
                found.append(p)
    return found


def profile_recency(name: str) -> tuple[Optional[Path], Optional[float]]:
    """Return (best profile/user-data dir, age_seconds of newest marker)."""
    now = time.time()
    best_dir: Optional[Path] = None
    best_mtime: Optional[float] = None
    if name == "firefox":
        for prof in _firefox_profiles():
            mt = _newest_mtime(_firefox_marker_files(prof))
            if mt is None:
                continue
            if best_mtime is None or mt > best_mtime:
                best_mtime, best_dir = mt, prof
    else:
        for root in _chromium_profile_roots(name):
            if not root.is_dir():
                continue
            mt = _newest_mtime(_chromium_marker_files(root))
            if mt is None:
                continue
            if best_mtime is None or mt > best_mtime:
                best_mtime, best_dir = mt, root
    if best_mtime is None:
        return None, None
    return best_dir, max(0.0, now - best_mtime)


def _score(recency_sec: Optional[float], *, is_default: bool, is_running: bool) -> float:
    score = 0.0
    if is_default:
        score += 100
    if is_running:
        score += 40
    if recency_sec is None:
        score += 1
    elif recency_sec <= PROFILE_HOT_SEC:
        score += 30
    elif recency_sec <= PROFILE_WARM_SEC:
        score += 10
    else:
        score += 2
    if recency_sec is not None:
        score += max(0.0, 5.0 - recency_sec / (30 * 24 * 3600))
    return score


def _progid_to_name(prog_id: str) -> Optional[str]:
    pl = (prog_id or "").lower()
    if "firefox" in pl:
        return "firefox"
    if "operagx" in pl or "opera gx" in pl:
        return "opera_gx"
    if "opera" in pl:
        return "opera"
    if "brave" in pl:
        return "brave"
    if "chrome" in pl and "edge" not in pl:
        return "chrome"
    if "edge" in pl or "msedge" in pl:
        return "msedge"
    if "chromium" in pl:
        return "chromium"
    return None


def _command_exe(command: str) -> Optional[Path]:
    m = re.search(r'"([^"]+\.exe)"', command, re.I)
    raw = m.group(1) if m else (command.split(" ", 1)[0].strip('"') if command else "")
    if not raw:
        return None
    p = Path(os.path.expandvars(raw))
    return p if p.is_file() else None


def windows_default_https() -> tuple[Optional[str], Optional[Path], str]:
    """(browser_name, exe_path, prog_id) from the system default HTTPS app."""
    if sys.platform != "win32":
        return None, None, ""
    try:
        import winreg
    except ImportError:
        return None, None, ""
    prog_id = ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
        ) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")
            prog_id = str(prog_id or "")
    except OSError:
        return None, None, ""
    name = _progid_to_name(prog_id)
    exe: Optional[Path] = None
    for hive, path in (
        (winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Classes\{prog_id}\shell\open\command"),
    ):
        try:
            with winreg.OpenKey(hive, path) as key:
                cmd, _ = winreg.QueryValueEx(key, None)
            exe = _command_exe(str(cmd or ""))
            if exe:
                break
        except OSError:
            continue
    if exe is None and name:
        exe = find_browser_exe(name)
    return name, exe, prog_id


def running_browser_names() -> set[str]:
    names: set[str] = set()
    try:
        import subprocess
        proc = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=8, text=True, errors="replace",
        )
        blob = (proc.stdout or "").lower()
    except Exception:
        return names
    mapping = {
        "chrome.exe": "chrome",
        "msedge.exe": "msedge",
        "brave.exe": "brave",
        "opera.exe": "opera",
        "firefox.exe": "firefox",
        "chromium.exe": "chromium",
    }
    for exe, name in mapping.items():
        if exe in blob:
            names.add(name)
    # Opera GX uses opera.exe — recency of GX profile distinguishes it later.
    return names


def detect_browsers() -> list[BrowserHit]:
    default_name, default_exe, prog_id = windows_default_https()
    running = running_browser_names()
    hits: list[BrowserHit] = []
    seen: set[str] = set()
    order = ("chrome", "msedge", "brave", "opera", "opera_gx", "firefox", "chromium")
    for name in order:
        exe = find_browser_exe(name)
        if name == default_name and default_exe:
            exe = default_exe
        prof, age = profile_recency(name)
        installed = exe is not None or prof is not None
        if not installed and name != default_name and name not in running:
            continue
        is_default = name == default_name
        is_running = name in running or (name == "opera_gx" and "opera" in running)
        # Stale profile: if the first root is cold, profile_recency already
        # scanned fallbacks (Beta/Canary/GX). If still colder than WARM and
        # this is not default/running, skip unless it is the only install.
        if age is not None and age > PROFILE_WARM_SEC and not is_default and not is_running:
            # keep as weak candidate
            pass
        hit = BrowserHit(
            name=name,
            exe=exe,
            profile_dir=prof,
            source="default" if is_default else ("running" if is_running else "install"),
            recency_sec=age,
            score=_score(age, is_default=is_default, is_running=is_running),
            extras={"prog_id": prog_id} if is_default else {},
        )
        hits.append(hit)
        seen.add(name)
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


def pick_auto_install_browser() -> Optional[BrowserHit]:
    """Best browser to drive cookie-extension auto-install against."""
    hits = [h for h in detect_browsers() if h.name in SUPPORTED]
    if not hits:
        return None
    # Prefer a hot default; otherwise highest score with an exe.
    with_exe = [h for h in hits if h.exe and h.exe.is_file()]
    pool = with_exe or hits
    return pool[0]


def infer_default_browser() -> Optional[str]:
    name, _, _ = windows_default_https()
    if name in SUPPORTED:
        return "edge" if name == "msedge" else name
    hit = pick_auto_install_browser()
    if not hit:
        return None
    return "edge" if hit.name == "msedge" else hit.name
