"""
VOD.RIP — Auto-update via GitHub Releases.

Supports:
  Windows — Inno Setup.exe (in-app, AV-friendly) or verified portable zip
            (download + open folder; no in-app robocopy dropper)
  macOS   — replace VOD.RIP.app from release zip (verified)
  Linux   — replace binary folder from release zip (verified)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile
from typing import List, Optional, Tuple

import requests

GITHUB_REPO = "mateusant13/VOD.RIP"
CHECK_INTERVAL_SEC = 24 * 3600
CACHE_FILENAME = "update_cache.json"
PENDING_FILENAME = "update_pending.json"

logger = logging.getLogger(__name__)
from services.os_services import _NO_WINDOW

try:
    from services._version import USER_AGENT
except ImportError:  # pragma: no cover - dev module-load race
    USER_AGENT = "VOD.RIP/unknown"
_DETACHED_FLAGS = 0
if os.name == "nt":
    _DETACHED_FLAGS = (
        getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    )


@dataclass
class UpdateApplyResult:
    ok: bool
    message: str = ""


def _terminate_for_update(reason: str) -> None:
    """F7 (ANTIVIRUS_AUDIT): ``os._exit`` only when the Inno installer takes over."""
    logger.info("Update applicator exiting live process: %s", reason)
    os._exit(0)


def _install_dir() -> Path:
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            exe = Path(sys.executable).resolve()
            if exe.parent.name == "MacOS" and exe.parent.parent.name == "Contents":
                return exe.parent.parent.parent
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _parse_sha256_sidecar(text: str) -> Optional[str]:
    """Accept ``<hex>`` or ``<hex>  filename`` (GNU/BtbN style)."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        token = line.split()[0].strip().lower()
        if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
            return token
    return None


def _companion_sha256_url(assets: list, asset_name: str) -> Optional[str]:
    target = f"{asset_name}.sha256"
    for asset in assets:
        if (asset.get("name") or "") == target:
            return asset.get("browser_download_url")
    return None


class UpdateChecker:
    def __init__(self, current_version: str, app_data_dir: Path):
        self.current_version = (current_version or "0").lstrip("v")
        self.app_data_dir = Path(app_data_dir)
        self.cache_path = self.app_data_dir / CACHE_FILENAME
        self.pending_path = self.app_data_dir / PENDING_FILENAME

    def check(self, *, force: bool = False) -> Optional[dict]:
        if not force and not self._should_check():
            pending = self.get_pending()
            if pending:
                return pending
            return None

        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        try:
            resp = requests.get(url, timeout=15, headers={"User-Agent": USER_AGENT})
            if resp.status_code != 200:
                self._save_cache({"last_check": time.time(), "error": resp.status_code})
                logger.warning("Update check HTTP %d", resp.status_code)
                return None

            data = resp.json()
            latest_tag = (data.get("tag_name") or "").lstrip("v")
            if not latest_tag or not self._is_newer(latest_tag, self.current_version):
                self._clear_pending()
                self._save_cache({"last_check": time.time(), "latest": latest_tag})
                return None

            assets = data.get("assets") or []
            asset = self._find_platform_asset(assets)
            if not asset:
                logger.info("No release asset for this platform (latest %s)", latest_tag)
                return None

            asset_name = asset.get("name", "")
            result = {
                "version": latest_tag,
                "download_url": asset["browser_download_url"],
                "asset_name": asset_name,
                "asset_kind": asset.get("_kind", "zip"),
                "release_url": data.get("html_url", ""),
                "release_notes": (data.get("body") or "")[:4000],
                "sha256_url": _companion_sha256_url(assets, asset_name),
            }
            self._save_pending(result)
            self._save_cache({"last_check": time.time(), "latest": latest_tag})
            return result
        except requests.RequestException as exc:
            logger.warning("Update check failed: %s", exc)
            return None

    def get_pending(self) -> Optional[dict]:
        if not self.pending_path.is_file():
            return None
        try:
            data = json.loads(self.pending_path.read_text(encoding="utf-8"))
            if data.get("version") and self._is_newer(data["version"], self.current_version):
                return data
        # ponytail: broad except Exception — narrow to specific exception types
        except Exception:
            pass
        return None

    def download_and_install(self, release_info: dict) -> UpdateApplyResult:
        download_url = release_info.get("download_url") or ""
        if not download_url:
            return UpdateApplyResult(False, "No download URL")

        tmp_dir = Path(tempfile.gettempdir()) / "VOD.RIP-Updates"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        asset_name = release_info.get("asset_name") or f"VOD.RIP-{release_info.get('version')}"
        dest = tmp_dir / asset_name

        logger.info("Downloading update %s ...", release_info.get("version"))
        try:
            with requests.get(download_url, stream=True, timeout=600, headers={"User-Agent": USER_AGENT}) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as handle:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            handle.write(chunk)
        # ponytail: broad except Exception — narrow to specific exception types
        except Exception as exc:
            logger.error("Update download failed: %s", exc)
            return UpdateApplyResult(False, f"Download failed: {exc}")

        if not self._verify_release_checksum(dest, release_info):
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            return UpdateApplyResult(
                False,
                "Update rejected: SHA-256 checksum missing or mismatch. "
                "Download the release manually from GitHub.",
            )

        kind = release_info.get("asset_kind") or self._guess_kind(dest.name)
        if kind == "setup":
            return self._launch_windows_setup(dest)
        if sys.platform == "win32":
            return self._offer_windows_portable_update(dest, release_info)
        if self._apply_zip_update(dest):
            return UpdateApplyResult(True, "Installing update")
        return UpdateApplyResult(False, "Update failed")

    def _verify_release_checksum(self, dest: Path, release_info: dict) -> bool:
        """F1: verify publisher checksum before any extract or execute.

        SHA-256 proves the file was not tampered with in transit; it does NOT
        grant SmartScreen reputation (that requires Authenticode signing).
        """
        sha_url = release_info.get("sha256_url")
        if not sha_url:
            logger.warning(
                "No .sha256 sidecar for %s — refusing in-app apply (download from GitHub instead)",
                release_info.get("asset_name"),
            )
            return False
        try:
            resp = requests.get(sha_url, timeout=30, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            expected = _parse_sha256_sidecar(resp.text)
            if not expected:
                logger.error("Could not parse SHA-256 sidecar from %s", sha_url)
                return False
            actual = _sha256_file(dest)
            if actual != expected:
                logger.error(
                    "SHA-256 mismatch for %s (expected %s, got %s)",
                    dest.name, expected, actual,
                )
                return False
            logger.info("SHA-256 verified for %s", dest.name)
            return True
        # ponytail: broad except Exception — narrow to specific exception types
        except Exception as exc:
            logger.error("SHA-256 verification failed: %s", exc)
            return False

    # ── internals ───────────────────────────────────────────────────────

    def _is_newer(self, latest: str, current: str) -> bool:
        def parts(v: str) -> List[int]:
            out: List[int] = []
            for piece in v.lstrip("v").replace("-", ".").split("."):
                try:
                    out.append(int(piece))
                except ValueError:
                    break
            return out or [0]

        a, b = parts(latest), parts(current)
        length = max(len(a), len(b))
        a.extend([0] * (length - len(a)))
        b.extend([0] * (length - len(b)))
        return a > b

    def _should_check(self) -> bool:
        if not self.cache_path.is_file():
            return True
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return (time.time() - float(data.get("last_check", 0))) > CHECK_INTERVAL_SEC
        # ponytail: broad except Exception — narrow to specific exception types
        except Exception:
            return True

    def _save_cache(self, data: dict) -> None:
        try:
            self.app_data_dir.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(data), encoding="utf-8")
        # ponytail: broad except Exception — narrow to specific exception types
        except Exception:
            pass

    def _save_pending(self, data: dict) -> None:
        try:
            self.app_data_dir.mkdir(parents=True, exist_ok=True)
            self.pending_path.write_text(json.dumps(data), encoding="utf-8")
        # ponytail: broad except Exception — narrow to specific exception types
        except Exception:
            pass

    def _clear_pending(self) -> None:
        try:
            if self.pending_path.is_file():
                self.pending_path.unlink()
        # ponytail: broad except Exception — narrow to specific exception types
        except Exception:
            pass

    def _platform_keywords(self) -> Tuple[List[str], List[str]]:
        if sys.platform == "win32":
            return (["Setup.exe", "Windows"], ["Windows", ".zip"])
        if sys.platform == "darwin":
            return ([], ["macOS", ".zip"])
        return ([], ["Linux", ".zip"])

    def _find_platform_asset(self, assets: list) -> Optional[dict]:
        preferred, fallback = self._platform_keywords()
        for keyword in preferred:
            for asset in assets:
                name = asset.get("name") or ""
                if keyword in name:
                    copy = dict(asset)
                    copy["_kind"] = "setup"
                    return copy
        for keyword in fallback:
            for asset in assets:
                name = asset.get("name") or ""
                if keyword in name and name.lower().endswith(".zip"):
                    copy = dict(asset)
                    copy["_kind"] = "zip"
                    return copy
        return None

    @staticmethod
    def _guess_kind(filename: str) -> str:
        lower = filename.lower()
        if lower.endswith(".exe") and "setup" in lower:
            return "setup"
        return "zip"

    def _launch_windows_setup(self, installer: Path) -> UpdateApplyResult:
        logger.info("Launching installer: %s", installer)
        if os.name == "nt":
            os.startfile(str(installer))
        else:
            subprocess.Popen([str(installer)], close_fds=True)
        _terminate_for_update("launching Windows installer; letting installer take over")
        return UpdateApplyResult(True, "Installer launched")

    def _offer_windows_portable_update(self, archive: Path, release_info: dict) -> UpdateApplyResult:
        """F1: no in-app robocopy/PowerShell dropper on Windows portable builds.

        The verified zip is left in %TEMP%\\VOD.RIP-Updates; we open the folder
        and the GitHub release page so the user can replace files deliberately.
        Prefer the Inno Setup.exe asset for one-click updates.
        """
        release_url = (release_info.get("release_url") or "").strip()
        if release_url:
            try:
                webbrowser.open(release_url)
            # ponytail: broad except Exception — narrow to specific exception types
            except Exception:
                logger.debug("Could not open release URL", exc_info=True)
        if os.name == "nt":
            try:
                abspath = str(archive.resolve())
                escaped = abspath.replace('"', '\\"')
                subprocess.Popen(
                    ["explorer.exe", f'/select,"{escaped}"'],
                    creationflags=_NO_WINDOW,
                )
            # ponytail: broad except Exception — narrow to specific exception types
            except Exception:
                try:
                    os.startfile(str(archive.parent))
                # ponytail: broad except Exception — narrow to specific exception types
                except Exception:
                    logger.debug("Could not open update folder", exc_info=True)
        msg = (
            f"Verified update saved to {archive}. "
            "Extract it over your VOD.RIP folder, or install the Setup.exe from the release page."
        )
        logger.info(msg)
        return UpdateApplyResult(True, msg)

    def _apply_zip_update(self, zip_path: Path) -> bool:
        install_dir = _install_dir()
        extract_dir = zip_path.parent / "extract"
        if extract_dir.exists():
            shutil.rmtree(extract_dir, ignore_errors=True)
        extract_dir.mkdir(parents=True, exist_ok=True)

        with ZipFile(zip_path, "r") as archive:
            _safe_extractall(archive, extract_dir)

        if sys.platform == "darwin":
            return self._apply_macos_zip(extract_dir, install_dir)
        if sys.platform == "win32":
            return False
        return self._apply_linux_zip(extract_dir, install_dir)

    def _apply_linux_zip(self, extract_dir: Path, install_dir: Path) -> bool:
        source = extract_dir
        if not (extract_dir / "VOD-RIP").is_file() and not (extract_dir / "_internal").is_dir():
            for child in extract_dir.iterdir():
                if child.is_dir():
                    source = child
                    break
        script = extract_dir.parent / "vodrip-update.sh"
        has_rsync = shutil.which("rsync") is not None
        if has_rsync:
            copy_cmd = 'rsync -a --delete "$src/" "$dst/"'
        else:
            copy_cmd = 'cp -a "$src/." "$dst/"'
        script.write_text(
            "\n".join([
                "#!/bin/sh",
                "sleep 2",
                f'src="{source}"',
                f'dst="{install_dir}"',
                copy_cmd,
                'exec "$dst/VOD-RIP"',
            ]),
            encoding="utf-8",
        )
        script.chmod(0o755)
        subprocess.Popen(["/bin/sh", str(script)], close_fds=True)
        _terminate_for_update("Linux rsync/cp updater script spawned; releasing file locks")
        return True

    def _apply_macos_zip(self, extract_dir: Path, install_dir: Path) -> bool:
        app_bundle = next(extract_dir.rglob("VOD.RIP.app"), None)
        if app_bundle is None:
            logger.error("macOS update zip missing VOD.RIP.app")
            return False
        parent = install_dir.parent
        script = extract_dir.parent / "vodrip-update.sh"
        script.write_text(
            "\n".join([
                "#!/bin/sh",
                "sleep 2",
                f'src="{app_bundle}"',
                f'dst="{parent / "VOD.RIP.app"}"',
                'rm -rf "$dst"',
                'ditto "$src" "$dst"',
                'open "$dst"',
            ]),
            encoding="utf-8",
        )
        script.chmod(0o755)
        subprocess.Popen(["/bin/sh", str(script)], close_fds=True)
        _terminate_for_update("macOS ditto updater script spawned; releasing file locks")
        return True


def _safe_extractall(archive: ZipFile, target: Path) -> None:
    """Extract zip members while validating paths to prevent Zip Slip."""
    for member in archive.infolist():
        member_path = target.resolve() / member.filename
        resolved = member_path.resolve()
        if not str(resolved).startswith(str(target.resolve())):
            raise SecurityError(
                f"Zip member '{member.filename}' would extract outside target"
            )
    archive.extractall(target)


class SecurityError(Exception):
    pass
