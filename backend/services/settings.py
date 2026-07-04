"""Settings manager — persists settings to a JSON file."""

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

from models.schemas import AppSettings


def _get_appdata_dir() -> Path:
    """Return the platform-appropriate user data directory for VOD.RIP."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "VOD.RIP"


class SettingsManager:
    def __init__(self):
        self._settings_dir = _get_appdata_dir()
        self._settings_file = self._settings_dir / "settings.json"
        self._lock = threading.Lock()
        self._settings = self._load()
        # Auto-create file with defaults if it doesn't exist
        if not self._settings_file.exists():
            self.save(self._settings)

    def _load(self) -> AppSettings:
        try:
            if self._settings_file.exists():
                data = json.loads(self._settings_file.read_text(encoding="utf-8"))
                if "download_folder_confirmed" not in data:
                    data["download_folder_confirmed"] = bool(
                        (data.get("download_folder") or "").strip()
                    )
                if "video_encoder" not in data:
                    data["video_encoder"] = "auto"
                return AppSettings(**data)
        except Exception:
        # ponytail: best-effort — return AppSettings(**data)
            pass
        return AppSettings()

    def get(self) -> AppSettings:
        with self._lock:
            return self._settings.model_copy()

    def save(self, settings: AppSettings):
        with self._lock:
            self._settings = settings
            self._settings_dir.mkdir(parents=True, exist_ok=True)
            # Atomic write: write to temp file, then replace to avoid corruption
            tmp = None
            try:
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(self._settings_dir),
                    prefix="settings_",
                    suffix=".tmp",
                )
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(settings.model_dump_json(indent=2))
                os.replace(tmp_path, str(self._settings_file))
                tmp = tmp_path
            finally:
                if tmp is not None and os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except Exception:
                    # ponytail: best-effort — I/O errors only
                        pass
