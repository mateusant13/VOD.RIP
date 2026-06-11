"""
VOD.RIP — Auto-update service.

Checks GitHub Releases API for new versions once per day and can download
and launch the platform-appropriate installer in silent mode.

Respects GitHub's 60 req/h unauthenticated rate limit by caching the last
check result and only re-checking every 24 hours.
"""

import json
import logging
import os
import subprocess
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if __import__('os').name == 'nt' else 0
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests

GITHUB_REPO = "your-username/vod-rip"  # TODO: set before first public release
CHECK_INTERVAL_SEC = 24 * 3600  # Once per day
CACHE_FILENAME = "update_cache.json"

logger = logging.getLogger(__name__)


class UpdateChecker:
    """Check for new VOD.RIP releases via the GitHub Releases API.

    Usage:
        checker = UpdateChecker("1.0.0", app_data_dir)
        release = checker.check()  # Returns None if up-to-date
        if release:
            checker.download_and_install(release)
    """

    def __init__(self, current_version: str, app_data_dir: Path):
        self.current_version = current_version.lstrip("v")
        self.cache_path = app_data_dir / CACHE_FILENAME

    # ── Public API ──────────────────────────────────────────────────────────

    def check(self) -> Optional[dict]:
        """Return release info if a newer version is available, else None.

        The returned dict has keys:
            ``version``       — e.g. "1.1.0"
            ``download_url``  — direct download URL for the platform asset
            ``release_url``   — GitHub release page
            ``release_notes`` — markdown body of the release
        """
        if not self._should_check():
            return None

        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                self._save_cache({"last_check": time.time(), "error": resp.status_code})
                logger.warning("Update check HTTP %d for %s", resp.status_code, url)
                return None

            data = resp.json()
            latest_tag = data.get("tag_name", "").lstrip("v")

            if not latest_tag or latest_tag == self.current_version:
                self._save_cache({"last_check": time.time(), "latest": latest_tag})
                return None

            # Find the installer asset for this platform
            asset = self._find_platform_asset(data.get("assets", []))
            if not asset:
                logger.info(
                    "New version %s found but no matching asset for this platform",
                    latest_tag,
                )
                return None

            result = {
                "version": latest_tag,
                "download_url": asset["browser_download_url"],
                "release_url": data["html_url"],
                "release_notes": (data.get("body") or "")[:2000],
            }
            self._save_cache({
                "last_check": time.time(),
                "latest": latest_tag,
                "download_url": result["download_url"],
            })
            return result

        except requests.RequestException as e:
            logger.warning("Update check failed: %s", e)
            return None

    def download_and_install(self, release_info: dict) -> bool:
        """Download the platform installer and launch it, then exit.

        Returns ``True`` if the installer was launched (the current process
        will exit shortly after). Returns ``False`` on failure.
        """
        download_url = release_info.get("download_url", "")
        if not download_url:
            logger.error("No download URL in release info")
            return False

        tmp_dir = Path(tempfile.gettempdir()) / "VOD.RIP-Updates"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        ext = self._platform_installer_ext()
        installer_path = tmp_dir / f"VOD.RIP-{release_info['version']}{ext}"

        logger.info("Downloading update %s ...", release_info["version"])
        try:
            resp = requests.get(download_url, stream=True, timeout=300)
            resp.raise_for_status()
            with open(installer_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        except Exception as e:
            logger.error("Update download failed: %s", e)
            return False

        logger.info("Download complete (%d bytes)", installer_path.stat().st_size)
        self._launch_installer(installer_path)
        return True

    # ── Internal ────────────────────────────────────────────────────────────

    def _should_check(self) -> bool:
        if not self.cache_path.is_file():
            return True
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return (time.time() - data.get("last_check", 0)) > CHECK_INTERVAL_SEC
        except Exception:
            return True

    def _save_cache(self, data: dict):
        try:
            self.cache_path.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass

    def _platform_installer_ext(self) -> str:
        if sys.platform == "win32":
            return "-Setup.exe"
        elif sys.platform == "darwin":
            return ".dmg"
        else:
            return "-x86_64.AppImage"

    def _platform_keyword(self) -> str:
        """Keyword to match in the asset name for this platform."""
        if sys.platform == "win32":
            return "Setup.exe"
        elif sys.platform == "darwin":
            return ".dmg"
        else:
            return ".AppImage"

    def _find_platform_asset(self, assets: list) -> Optional[dict]:
        keyword = self._platform_keyword()
        for asset in assets:
            name = asset.get("name", "")
            if keyword in name:
                return asset
        return None

    def _launch_installer(self, path: Path):
        """Launch the platform installer and exit the current process."""
        logger.info("Launching installer: %s", path)

        if sys.platform == "win32":
            subprocess.Popen(
                [
                    str(path),
                    "/VERYSILENT",
                    "/SUPPRESSMSGBOXES",
                    "/CLOSEAPPLICATIONS",
                    "/RESTARTAPPLICATIONS",
                ],
                close_fds=True,
                        creationflags=_NO_WINDOW,
            )
        elif sys.platform == "darwin":
            # On macOS, open the DMG — the user still has to drag to /Applications
            subprocess.Popen(["open", str(path)], creationflags=_NO_WINDOW)
        else:
            # Linux: make executable and run
            path.chmod(0o755)
            subprocess.Popen([str(path)], creationflags=_NO_WINDOW)

        logger.info("Exiting for update...")
        os._exit(0)
