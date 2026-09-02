import json

from services.error_log import _error_log_path, clear_error_ring_for_tests, record_error


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

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 500
    assert rows[0]["message"] == "old-1"
    assert "SECRET" not in rows[-1]["message"]
    assert "TOKEN" not in rows[-1]["message"]
    assert "QUERY" not in rows[-1]["message"]
    assert "[REDACTED]" in rows[-1]["message"]
