"""VODRIP_SLOW_QUERY_MS is OFF by default, and query() never touches a clock.

Added 2026-10-14. py-spy sampled the API every 100 ms while a preview was
clicked in a real browser: in the 24.8 s rep, TEN distinct threads were
inside archive_db.query at once, versus six in the 4-5 s reps. Read latency
was the only thing that tracked the click latency, and nothing else I
measured did. This knob turns that correlation into a number.

It costs nothing when unset - the red arm below proves the fast path does not
even call perf_counter.
"""
import threading
import time
from unittest.mock import patch

import pytest

from services import archive_db


def test_default_is_off():
    assert archive_db._SLOW_QUERY_MS == 0


def test_query_does_not_touch_a_clock_when_disabled():
    """RED if the fast path starts timing: that is pure cost for nothing."""
    calls = []
    real = time.perf_counter

    def spy():
        calls.append(1)
        return real()

    with patch.object(time, "perf_counter", spy):
        with patch.object(archive_db, "_get_read_conn") as conn:
            conn.return_value.execute.return_value.fetchall.return_value = []
            archive_db.query("select 1")
    assert calls == [], "query() timed itself with the diagnostic disabled"


def test_a_slow_read_is_logged_when_enabled(monkeypatch, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="services.archive_db"):
        monkeypatch.setattr(archive_db, "_SLOW_QUERY_MS", 0)
        with patch.object(archive_db, "_get_read_conn") as conn:
            conn.return_value.execute.return_value.fetchall.return_value = []
            archive_db.query("select 1")
        assert "slow read" not in caplog.text

        # re-read the threshold from the env, as the module does at import
        monkeypatch.setattr(archive_db, "_SLOW_QUERY_MS", 250)
        with patch.object(time, "perf_counter", side_effect=[0.0, 1.0]):
            with patch.object(archive_db, "_get_read_conn") as conn:
                conn.return_value.execute.return_value.fetchall.return_value = []
                archive_db.query("select 1")
        assert "slow read" in caplog.text


def test_the_read_path_is_unchanged():
    """Control: the diagnostic must not alter what query() returns."""
    with patch.object(archive_db, "_get_read_conn") as conn:
        conn.return_value.execute.return_value.fetchall.return_value = [(1,)]
        with patch.object(archive_db, "_SLOW_QUERY_MS", 250):
            with patch.object(time, "perf_counter", side_effect=[0.0, 0.5]):
                assert archive_db.query("select 1") == [(1,)]
