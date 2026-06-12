"""
VOD.RIP — Auto-update via GitHub Releases.

Supports:
  Windows — silent Setup.exe (Inno) or portable zip in-place update
  macOS   — replace VOD.RIP.app from release zip
  Linux   — replace binary folder from release zip
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from zipfile import ZipFile
from typing import List, Optional, Tuple

import requests

GITHUB_REPO = "mateusant13/VOD.RIP"
CHECK_INTERVAL_SEC = 24 * 3600
CACHE_FILENAME = "update_cache.json"
PENDING_FILENAME = "update_pending.json"

logger = logging.getLogger(__name__)
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _install_dir() -> Path:
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            exe = Path(sys.executable).resolve()
            # .../VOD.RIP.app/Contents/MacOS/VOD-RIP
            if exe.parent.name == "MacOS" and exe.parent.parent.name == "Contents":
                return exe.parent.parent.parent
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


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
            resp = requests.get(url, timeout=15)
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

            asset = self._find_platform_asset(data.get("assets") or [])
            if not asset:
                logger.info("No release asset for this platform (latest %s)", latest_tag)
                return None

            result = {
                "version": latest_tag,
                "download_url": asset["browser_download_url"],
                "asset_name": asset.get("name", ""),
                "asset_kind": asset.get("_kind", "zip"),
                "release_url": data.get("html_url", ""),
                "release_notes": (data.get("body") or "")[:4000],
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
        except Exception:
            pass
        return None

    def download_and_install(self, release_info: dict) -> bool:
        download_url = release_info.get("download_url") or ""
        if not download_url:
            return False

        tmp_dir = Path(tempfile.gettempdir()) / "VOD.RIP-Updates"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        asset_name = release_info.get("asset_name") or f"VOD.RIP-{release_info.get('version')}"
        dest = tmp_dir / asset_name

        logger.info("Downloading update %s ...", release_info.get("version"))
        try:
            with requests.get(download_url, stream=True, timeout=600) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as handle:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            handle.write(chunk)
        except Exception as exc:
            logger.error("Update download failed: %s", exc)
            return False

        kind = release_info.get("asset_kind") or self._guess_kind(dest.name)
        if kind == "setup":
            return self._launch_windows_setup(dest)
        return self._apply_zip_update(dest)

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
        except Exception:
            return True

    def _save_cache(self, data: dict) -> None:
        try:
            self.app_data_dir.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass

    def _save_pending(self, data: dict) -> None:
        try:
            self.app_data_dir.mkdir(parents=True, exist_ok=True)
            self.pending_path.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass

    def _clear_pending(self) -> None:
        try:
            if self.pending_path.is_file():
                self.pending_path.unlink()
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

    def _launch_windows_setup(self, installer: Path) -> bool:
        logger.info("Launching installer: %s", installer)
        # Use the normal installer UI (not silent/hidden) — fewer antivirus false positives.
        if os.name == "nt":
            os.startfile(str(installer))
        else:
            subprocess.Popen([str(installer)], close_fds=True)
        os._exit(0)

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
            return self._apply_windows_zip(extract_dir, install_dir)
        return self._apply_linux_zip(extract_dir, install_dir)

    def _apply_windows_zip(self, extract_dir: Path, install_dir: Path) -> bool:
        source = extract_dir
        nested = list(extract_dir.glob("VOD-RIP.EXE")) + list(extract_dir.glob("VOD-RIP.exe"))
        if nested:
            source = nested[0].parent
        exe = source / "VOD-RIP.EXE"
        if not exe.is_file():
            exe = source / "VOD-RIP.exe"
        script = extract_dir.parent / "vodrip-update.ps1"
        script.write_text(
            "\n".join([
                "Start-Sleep -Seconds 2",
                f'$src = "{source}"',
                f'$dst = "{install_dir}"',
                "robocopy $src $dst /E /R:2 /W:2 /NFL /NDL /NJH /NJS",
                "if ($LASTEXITCODE -ge 8) { exit 1 }",
                f'Start-Process "{install_dir / "VOD-RIP.EXE"}"',
            ]),
            encoding="utf-8",
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            close_fds=True,
            creationflags=_NO_WINDOW,
        )
        os._exit(0)

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
        os._exit(0)

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
        os._exit(0)


def _safe_extractall(archive: ZipFile, target: Path) -> None:
    """Extract zip members while validating paths to prevent Zip Slip."""
    for member in archive.infolist():
        # Resolve the member path and ensure it stays inside target.
        member_path = target.resolve() / member.filename
        resolved = member_path.resolve()
        if not str(resolved).startswith(str(target.resolve())):
            raise SecurityError(
                f"Zip member '{member.filename}' would extract outside target"
            )
    archive.extractall(target)


class SecurityError(Exception):
    pass


def background_check(app_data_dir: Path, current_version: str) -> None:
    try:
        checker = UpdateChecker(current_version, app_data_dir)
        release = checker.check()
        if release:
            logger.info("Update available: v%s", release.get("version"))
    except Exception as exc:
        logger.debug("Background update check: %s", exc)
