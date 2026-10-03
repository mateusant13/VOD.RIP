import json

from services import error_log
from services.error_log import (
    _ERROR_RING_MAX,
    _ERROR_ROTATE_SLACK,
    _error_log_path,
    clear_error_ring_for_tests,
    get_error_ring,
    record_error,
)


def test_error_log_keeps_latest_500_and_redacts_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_APP_DATA", str(tmp_path / "appdata"))
    clear_error_ring_for_tests()
    log_path = _error_log_path()
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "\n".join(json.dumps({"ts": i, "kind": "old", "message": f"old-{i}"}) for i in range(500))
        + "\n",
        encoding="utf-8",
    )

    record_error("request", "cookie=SECRET authorization=Bearer TOKEN https://example.test/x?token=QUERY")

    # record_error is APPEND-ONLY: a single error must not rewrite the file.
    # The file may now hold _ERROR_RING_MAX + 1 lines; rotation is amortized.
    file_rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(file_rows) == _ERROR_RING_MAX + 1

    # The API still surfaces exactly the latest 500 records (oldest dropped).
    rows = get_error_ring(_ERROR_RING_MAX)
    assert len(rows) == _ERROR_RING_MAX
    assert rows[0]["message"] == "old-1"
    assert "SECRET" not in rows[-1]["message"]
    assert "TOKEN" not in rows[-1]["message"]
    assert "QUERY" not in rows[-1]["message"]
    assert "[REDACTED]" in rows[-1]["message"]

    # The API must still expose retained errors after a process restart.
    clear_error_ring_for_tests()
    assert get_error_ring(1)[0]["message"] == rows[-1]["message"]


def test_error_log_amortized_rotation_keeps_window(tmp_path, monkeypatch):
    """Drive many errors; the on-disk file is compacted, latest 500 survive."""
    monkeypatch.setenv("VODRIP_APP_DATA", str(tmp_path / "appdata"))
    clear_error_ring_for_tests()
    log_path = _error_log_path()
    log_path.parent.mkdir(parents=True)

    # Seed a full window, then push well past the rotation slack.
    log_path.write_text(
        "\n".join(json.dumps({"ts": i, "kind": "old", "message": f"old-{i}"}) for i in range(_ERROR_RING_MAX))
        + "\n",
        encoding="utf-8",
    )
    for i in range(_ERROR_ROTATE_SLACK + 50):
        record_error("bulk", f"new-{i}")

    # Rotation fired: file is back within the retention window (+slack).
    file_lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(file_lines) <= _ERROR_RING_MAX + _ERROR_ROTATE_SLACK

    # And the API contract is unchanged: exactly the latest 500, newest last.
    rows = get_error_ring(_ERROR_RING_MAX)
    assert len(rows) == _ERROR_RING_MAX
    assert rows[-1]["message"] == f"new-{_ERROR_ROTATE_SLACK + 49}"
    assert all(r["message"].startswith(("old-", "new-")) for r in rows)


def test_error_log_counter_resets_on_clear(tmp_path, monkeypatch):
    """clear_error_ring_for_tests re-hydrates the line counter."""
    monkeypatch.setenv("VODRIP_APP_DATA", str(tmp_path / "appdata"))
    clear_error_ring_for_tests()
    log_path = _error_log_path()
    log_path.parent.mkdir(parents=True)
    log_path.write_text("x\n", encoding="utf-8")  # deliberately not JSON
    record_error("k", "m")
    # The unreadable first line is skipped by get_error_ring, the new one kept.
    assert get_error_ring(1)[0]["message"] == "m"
    assert error_log._ERROR_FILE_LINES == 2
