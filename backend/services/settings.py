"""Settings manager — persists settings to a JSON file."""

import json
import threading
from pathlib import Path

from models.schemas import AppSettings


class SettingsManager:
    def __init__(self):
        self._settings_dir = Path.home() / ".config" / "KickDownloader"
        self._settings_file = self._settings_dir / "settings.json"
        self._lock = threading.Lock()
        self._settings = self._load()

    def _load(self) -> AppSettings:
        try:
            if self._settings_file.exists():
                data = json.loads(self._settings_file.read_text(encoding="utf-8"))
                return AppSettings(**data)
        except Exception:
            pass
        return AppSettings()

    def get(self) -> AppSettings:
        with self._lock:
            return self._settings.model_copy()

    def save(self, settings: AppSettings):
        with self._lock:
            self._settings = settings
            self._settings_dir.mkdir(parents=True, exist_ok=True)
            self._settings_file.write_text(
                settings.model_dump_json(indent=2),
                encoding="utf-8",
            )
