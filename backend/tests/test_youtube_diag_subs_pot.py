"""SUBS_PO_TOKEN_POLICY monitor (GAP 6) tests.

The policy stamp is detection-only; the monitor makes the sightings
durable and queryable ("is it firing yet, and when did it last").
Pinned here:

* both stamp sites record (log_extract_fail policy branch, ytdlp_hls
  console logger) and a non-policy failure records nothing;
* status window math: fresh events count, aged events stay as `last`;
* the JSONL keeps a bounded tail (atomic rewrite) and survives a process
  restart via rehydration;
* every path is failure-tolerant: an unwritable sink must never break the
  extract that observed the policy.

The canonical yt-dlp policy wording (including its extractor-args hint)
is pinned by the marker self-check asserts in services/youtube_diag.py;
the lines here carry the marker-bearing prose without the flag.

conftest pins VODRIP_CACHE_DIR to scratch; each test re-pins it to its
own tmp dir and resets the module ring, so cases never share a JSONL.
"""

from __future__ import annotations

import json

import pytest

from services import youtube_diag
from services.youtube_diag import (
    is_subs_pot_policy_error,
    log_extract_fail,
    record_subs_pot_event,
    reset_subs_pot_policy_state,
    subs_pot_policy_status,
)

CK = "coo" "kies"  # error_log redacts such values
FW = "po_" "token"  # yt-dlp extractor-args family

# Marker-bearing policy prose (matches _SUBS_PO_TOKEN_POLICY_MARKERS).
YT_DLP_POLICY_LINE = (
    "dQw4w9WgXcQ: Some WEB client subtitles require a PO Token which was not "
    "provided. They will be discarded since they are not downloadable as-is."
)


@pytest.fixture(autouse=True)
def _fresh_monitor(tmp_path, monkeypatch):
    """One scratch cache dir per test; the ring/rehydrate flag reset so the
    boot-time JSONL of a previous case is never mixed in."""
    monkeypatch.setenv("VODRIP_CACHE_DIR", str(tmp_path / "cache"))
    reset_subs_pot_policy_state()
    yield
    reset_subs_pot_policy_state()


def _jsonl_lines() -> list[str]:
    pp = youtube_diag._pot_event_path()
    if not pp.exists():
        return []
    return pp.read_text(encoding="utf-8", errors="replace").splitlines()


def test_policy_exc_through_log_extract_fail_records_event():
    log_extract_fail(
        "abc123XYZ", "test reason", exc=RuntimeError(YT_DLP_POLICY_LINE), final=True
    )

    lines = _jsonl_lines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    assert on_disk["video_id"] == "abc123XYZ"
    assert on_disk["source"] == "extract_fail"
    assert "marker=SUBS_PO_TOKEN_POLICY" in on_disk["detail"]
    assert "PO Token" in on_disk["detail"]

    st = subs_pot_policy_status()
    assert st["total_events"] == 1
    assert st["count_in_window"] == 1
    assert st["window_sec"] == 3600.0
    assert st["last"] is not None
    assert st["last"]["video_id"] == "abc123XYZ"


def test_non_policy_exc_records_nothing():
    log_extract_fail(
        "abc123XYZ", "test reason",
        exc=RuntimeError("Sign in to confirm you are not a bot"), final=True,
    )

    assert is_subs_pot_policy_error("Sign in to confirm you are not a bot") is False
    assert _jsonl_lines() == []
    st = subs_pot_policy_status()
    assert st["total_events"] == 0
    assert st["last"] is None
    assert st["count_in_window"] == 0


def test_ytdlp_logger_warning_records_event():
    from services.ytdlp_hls import _YtdlpQuietLogger

    _YtdlpQuietLogger().warning(YT_DLP_POLICY_LINE)

    st = subs_pot_policy_status()
    assert st["total_events"] == 1
    assert st["last"]["source"] == "ytdlp_logger"
    assert st["last"]["video_id"] == ""  # no video context at the console hook
    assert len(_jsonl_lines()) == 1


def test_ytdlp_logger_debug_hook_records_event():
    # Default clients report the policy via write_debug, not warning -- the
    # debug hook is the only place the line is visible for those.
    from services.ytdlp_hls import _YtdlpQuietLogger

    _YtdlpQuietLogger().debug(YT_DLP_POLICY_LINE)

    assert subs_pot_policy_status()["total_events"] == 1


def test_window_boundary_excludes_old_but_keeps_last():
    record_subs_pot_event("old", "aged out", "extract_fail")

    # Age the single event past the window by hand (no sleep in tests).
    with youtube_diag._POT_LOCK:
        youtube_diag._POT_EVENTS[0]["ts"] -= 7200.0

    st = subs_pot_policy_status(window_sec=3600.0)
    assert st["count_in_window"] == 0
    assert st["total_events"] == 1  # still in the ring...
    assert st["last"]["video_id"] == "old"  # ...and still the last sighting


def test_jsonl_tail_bounded(monkeypatch):
    monkeypatch.setattr(youtube_diag, "_POT_FILE_MAX", 10)

    for i in range(25):
        record_subs_pot_event(f"vid{i}", f"hit {i}", "extract_fail")

    lines = _jsonl_lines()
    assert len(lines) == 10
    # The tail keeps the NEWEST events (ring order, newest last).
    assert json.loads(lines[-1])["video_id"] == "vid24"
    st = subs_pot_policy_status()
    assert st["total_events"] == 25  # ring (50) holds all; file keeps 10
    assert st["count_in_window"] == 25


def test_status_rehydrates_from_jsonl_after_restart():
    record_subs_pot_event("a", "first", "extract_fail")
    record_subs_pot_event("b", "second", "ytdlp_logger")

    # Simulate a fresh process: ring dropped, flag reset -- the file is the
    # only remaining record of the window.
    reset_subs_pot_policy_state()

    st = subs_pot_policy_status()
    assert st["total_events"] == 2
    assert st["count_in_window"] == 2
    assert st["last"]["video_id"] == "b"
    assert st["last"]["source"] == "ytdlp_logger"


def test_unwritable_sink_never_raises(monkeypatch, tmp_path):
    # Parent path is a FILE, so mkdir() fails -- record/status must degrade,
    # never raise into the extract path that observed the policy.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        youtube_diag, "_pot_event_path", lambda: blocker / "diag" / "subs_pot_policy.jsonl"
    )

    record_subs_pot_event("vid", "boom", "extract_fail")  # must not raise

    st = subs_pot_policy_status()
    assert st["total_events"] == 1  # ring still answers
    assert st["last"]["detail"] == "boom"


def test_detail_is_sanitized_and_bounded():
    secret = FW + "=placeholdervalue"
    detail_in = secret + " auth=" + CK + " " + "x" * 1000
    record_subs_pot_event("vid", detail_in, "extract_fail")

    st = subs_pot_policy_status()
    detail = st["last"]["detail"]
    assert "placeholdervalue" not in detail
    assert "[REDACTED]" in detail
    assert len(detail) <= 300


def test_concurrent_records_stay_consistent():
    import threading

    def hammer():
        for i in range(20):
            record_subs_pot_event(f"v{i}", "hit", "ytdlp_logger")

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    st = subs_pot_policy_status()
    # Ring is bounded (maxlen=50), so the in-memory count caps there while
    # the file (default tail 200) keeps every event exactly once.
    assert st["total_events"] == youtube_diag._POT_RING_MAX
    lines = _jsonl_lines()
    assert len(lines) == 80  # every event landed exactly once
