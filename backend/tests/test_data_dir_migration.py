"""data_dir wiring: archive_db._db_path follows the data-disk resolver and
the one-time DB migration runs before the first connection at a new path."""
from pathlib import Path

from services import archive_db, disk_detect, disk_hygiene, settings as _settings


def test_db_path_precedence(monkeypatch, tmp_path):
    appdata = _settings._get_appdata_dir()  # conftest-patched scratch dir
    # 1. VODRIP_ARCHIVE_DB (test/portable override) wins over everything.
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(tmp_path / "a" / "archive.db"))
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path / "data"))
    assert archive_db._db_path() == tmp_path / "a" / "archive.db"

    # 2. Cleared -> data_dir env (the Settings > Storage data-disk pick).
    monkeypatch.delenv("VODRIP_ARCHIVE_DB")
    assert archive_db._db_path() == tmp_path / "data" / "archive.db"

    # 3. Cleared -> auto: fastest usable drive when one exists; no drives
    #    here, so the app-data location remains the fallback.
    monkeypatch.delenv("VODRIP_DATA_DIR")
    monkeypatch.setattr(disk_hygiene, "_auto_data_dir", None)
    monkeypatch.setattr(disk_detect, "disk_inventory", lambda: [])
    assert archive_db._db_path() == appdata / "archive.db"

    # 4. Auto with a usable fastest drive -> <fastest>\VOD.RIP-data.
    monkeypatch.setattr(disk_hygiene, "_auto_data_dir", None)
    monkeypatch.setattr(
        disk_detect,
        "disk_inventory",
        lambda: [{
            "drive": "H:\\", "label": "", "total_bytes": 10**12,
            "free_bytes": 90 * 1024**3, "media_type": "NVMe",
            "bus_type": "NVMe", "speed_rank": 1,
        }],
    )
    assert archive_db._db_path() == Path("H:\\VOD.RIP-data") / "archive.db"


def test_db_migrates_to_data_dir_before_first_open(monkeypatch, tmp_path):
    monkeypatch.delenv("VODRIP_ARCHIVE_DB")
    appdata = _settings._get_appdata_dir()  # conftest-patched scratch dir
    data = tmp_path / "data"
    appdata.mkdir(parents=True, exist_ok=True)
    (appdata / "archive.db").write_bytes(b"seed-db")
    (appdata / "whisper_manifest").mkdir()
    (appdata / "whisper_manifest" / "kick__x.jsonl").write_text("{}\n")
    monkeypatch.setenv("VODRIP_DATA_DIR", str(data))

    assert not (data / "archive.db").exists()
    archive_db._migrate_db_to_data_dir(data / "archive.db")
    assert (data / "archive.db").read_bytes() == b"seed-db"
    assert (data / "whisper_manifest" / "kick__x.jsonl").read_text() == "{}\n"

    # Idempotent: an existing target DB is never overwritten.
    (data / "archive.db").write_bytes(b"newer-db")
    archive_db._migrate_db_to_data_dir(data / "archive.db")
    assert (data / "archive.db").read_bytes() == b"newer-db"

    # Same-directory target is a no-op.
    archive_db._migrate_db_to_data_dir(appdata / "archive.db")
    assert (appdata / "archive.db").read_bytes() == b"seed-db"
