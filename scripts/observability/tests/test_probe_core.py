"""Unit tests for the probe's judgement layer.

These are the acceptance criteria of the observability substrate, written as
assertions. Each one names the false green it exists to prevent.
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import probe_core as core  # noqa: E402
import probe  # noqa: E402

HEALTH_BUDGET = core.DEFAULT_HEALTH_BUDGET_MS
PREVIEW_BUDGET = core.DEFAULT_PREVIEW_BUDGET_MS


def closed_port() -> int:
    """A port with nothing listening on it, closed by construction."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def ok_sample(ms: float, **kw):
    s = {"ok": True, "status": 200, "ms": ms, "ts": "2026-10-05T00:00:00+00:00",
         "epoch": 1000.0, "pid": 10, "proc_start": "2026-10-05T01:00:00+00:00"}
    s.update(kw)
    return s


def dead_sample(ms: float = 0.0, **kw):
    s = {"ok": False, "status": None, "ms": ms, "error": "ConnectionRefusedError: refused",
         "ts": "2026-10-05T00:00:10+00:00", "epoch": 1005.0, "pid": 10,
         "proc_start": "2026-10-05T01:00:00+00:00"}
    s.update(kw)
    return s


# ==========================================================================
# 1. UNREACHABLE is never a 0 ms pass
# ==========================================================================
class TestUnreachableIsNotAPass:
    def test_unreachable_is_not_measured(self):
        v = core.classify(dead_sample(), HEALTH_BUDGET)
        assert v["verdict"] == core.NOT_MEASURED
        assert v["reason_class"] == "unreachable"

    def test_unreachable_never_reports_a_zero_latency_pass(self):
        """The exact shape of the original lie: 0 ms standing in for 'unreachable'."""
        v = core.classify(dead_sample(ms=0.0), HEALTH_BUDGET)
        assert v["verdict"] != core.PASS
        assert v["reason_class"] != "within_budget"
        assert "unreachable" in v["reason"]

    def test_percentile_of_an_empty_population_is_none_not_zero(self):
        assert core.percentile([], 50) is None
        assert core.percentile([], 95) is None

    def test_unreachable_does_not_pollute_the_latency_distribution(self):
        samples = [ok_sample(12.0), dead_sample(ms=0.0), ok_sample(20.0)]
        out = core.summarise("health", samples, HEALTH_BUDGET, window="t", nominal_interval_s=0)
        assert out["verdict"] == core.PASS
        assert out["n"] == 3
        assert out["n_measured"] == 2
        assert out["not_measured_count"] == 1
        # A 0 ms unreachable attempt must not drag p50 down to the floor.
        assert out["p50_ms"] == 16.0
        assert out["max_ms"] == 20.0

    def test_all_unreachable_population_is_not_measured(self):
        out = core.summarise("health", [dead_sample(), dead_sample()], HEALTH_BUDGET,
                             window="t", nominal_interval_s=0)
        assert out["verdict"] == core.NOT_MEASURED
        assert out["n_measured"] == 0
        assert out["p50_ms"] is None and out["p95_ms"] is None and out["max_ms"] is None
        assert "0 of them reached" in out["reason"]

    def test_empty_population_is_not_measured(self):
        out = core.summarise("health", [], HEALTH_BUDGET, window="t", nominal_interval_s=0)
        assert out["verdict"] == core.NOT_MEASURED
        assert out["n"] == 0
        assert "no health samples" in out["reason"]

    def test_real_closed_port_is_not_measured_end_to_end(self):
        """Not a mock: a real socket connect to a real closed port."""
        port = closed_port()
        ident = probe.serving_identity(f"http://127.0.0.1:{port}")
        assert ident["pid"] is None  # nothing is listening, so no identity is claimed
        s = probe.take_health(f"http://127.0.0.1:{port}", HEALTH_BUDGET, 5.0, ident)
        assert s["verdict"] == core.NOT_MEASURED
        assert s["status"] is None
        out = core.summarise("health", [s], HEALTH_BUDGET, window="t", nominal_interval_s=0)
        assert out["verdict"] == core.NOT_MEASURED
        assert out["p50_ms"] is None
        assert core.validate_verdict(core.build_verdict([out], {})) == []


# ==========================================================================
# 2. over budget is FAIL
# ==========================================================================
class TestBudgets:
    def test_over_budget_is_fail_not_a_note(self):
        v = core.classify(ok_sample(10_891.0), HEALTH_BUDGET)
        assert v["verdict"] == core.FAIL
        assert v["reason_class"] == "over_budget"
        assert "10891.0 ms over the 1000 ms budget" in v["reason"]

    def test_the_budget_a_sample_was_judged_against_is_recorded(self):
        v = core.classify(ok_sample(4_000.0), PREVIEW_BUDGET)
        assert v["verdict"] == core.FAIL
        assert v["budget_ms"] == PREVIEW_BUDGET
        out = core.summarise("preview", [ok_sample(4_000.0)], PREVIEW_BUDGET, window="t",
                             nominal_interval_s=0)
        assert out["budget_ms"] == PREVIEW_BUDGET
        assert "3000 ms budget" in out["reason"]

    def test_under_budget_passes(self):
        assert core.classify(ok_sample(32.0), PREVIEW_BUDGET)["verdict"] == core.PASS
        assert core.classify(ok_sample(15.0), HEALTH_BUDGET)["verdict"] == core.PASS

    def test_exactly_on_budget_passes_and_one_ms_over_fails(self):
        assert core.classify(ok_sample(1000.0), HEALTH_BUDGET)["verdict"] == core.PASS
        assert core.classify(ok_sample(1000.1), HEALTH_BUDGET)["verdict"] == core.FAIL

    def test_http_error_is_a_measured_failure(self):
        v = core.classify({"ok": False, "status": 500, "ms": 30.0, "error": "HTTP 500"},
                          HEALTH_BUDGET)
        assert v["verdict"] == core.FAIL
        assert v["reason_class"] == "http_error"

    def test_window_with_one_failure_fails_and_keeps_the_population(self):
        samples = [ok_sample(5.0), ok_sample(6.0), ok_sample(22_875.0), ok_sample(7.0)]
        out = core.summarise("preview", samples, PREVIEW_BUDGET, window="t", nominal_interval_s=0)
        assert out["verdict"] == core.FAIL
        assert out["n"] == 4 and out["n_measured"] == 4
        assert out["pass_count"] == 3 and out["fail_count"] == 1
        assert out["over_budget_count"] == 1
        assert out["p95_ms"] is not None and out["max_ms"] == 22_875.0
        assert out["failures"] and "budget" in out["failures"][0]["reason"]


# ==========================================================================
# 3. restart and unreachable_window events
# ==========================================================================
class TestEvents:
    def test_identity_change_is_a_restart(self):
        prev = ok_sample(5.0, pid=39848, proc_start="2026-10-05T01:57:40+00:00")
        cur = ok_sample(5.0, epoch=1020.0, pid=40001, proc_start="2026-10-05T05:10:00+00:00")
        events = core.detect_events(prev, cur, nominal_interval_s=20.0)
        restarts = [e for e in events if e["event"] == "restart"]
        assert len(restarts) == 1
        assert restarts[0]["from"] == {"pid": 39848, "proc_start": "2026-10-05T01:57:40+00:00"}
        assert restarts[0]["to"]["pid"] == 40001

    def test_same_pid_different_start_time_is_still_a_restart(self):
        """Windows recycles pids, so a pid alone is not an identity."""
        prev = ok_sample(5.0, pid=39848, proc_start="2026-10-05T01:57:40+00:00")
        cur = ok_sample(5.0, epoch=1020.0, pid=39848, proc_start="2026-10-05T05:10:00+00:00")
        assert [e["event"] for e in core.detect_events(prev, cur, 20.0)] == ["restart"]

    def test_identical_identity_is_not_a_restart(self):
        prev = ok_sample(5.0)
        cur = ok_sample(5.0, epoch=1020.0)
        assert [e for e in core.detect_events(prev, cur, 20.0) if e["event"] == "restart"] == []

    def test_unknown_identity_never_claims_continuity_or_restart(self):
        prev = ok_sample(5.0, pid=None, proc_start=None)
        cur = ok_sample(5.0, epoch=1020.0, pid=40001, proc_start="x")
        assert [e for e in core.detect_events(prev, cur, 20.0) if e["event"] == "restart"] == []

    def test_gap_yields_unreachable_window_with_duration(self):
        prev = ok_sample(5.0, epoch=1000.0)
        cur = ok_sample(5.0, epoch=1000.0 + 18 * 3600)  # the stale-heartbeat scale
        events = core.detect_events(prev, cur, nominal_interval_s=20.0)
        windows = [e for e in events if e["event"] == "unreachable_window"]
        assert len(windows) == 1
        assert windows[0]["duration_s"] == 18 * 3600
        assert windows[0]["expected_max_s"] == 60.0

    def test_gap_within_the_interval_is_not_an_outage(self):
        prev = ok_sample(5.0, epoch=1000.0)
        cur = ok_sample(5.0, epoch=1019.0)
        assert [e for e in core.detect_events(prev, cur, 20.0)
                if e["event"] == "unreachable_window"] == []

    def test_pass_to_unreachable_and_back_are_recorded_as_edges(self):
        prev = ok_sample(5.0, epoch=1000.0)
        dead = dead_sample(epoch=1020.0)
        back = ok_sample(5.0, epoch=1040.0)
        assert "unreachable" in [e["event"] for e in core.detect_events(prev, dead, 20.0)]
        assert "recovered" in [e["event"] for e in core.detect_events(dead, back, 20.0)]

    def test_fold_events_surfaces_restarts_in_a_window_summary(self):
        samples = [
            ok_sample(5.0, epoch=1000.0, pid=1, proc_start="t0"),
            ok_sample(5.0, epoch=1020.0, pid=2, proc_start="t1"),
            ok_sample(5.0, epoch=1040.0, pid=2, proc_start="t1"),
        ]
        out = core.summarise("health", samples, HEALTH_BUDGET, window="t",
                             nominal_interval_s=20.0)
        assert out["restarts"] == 1
        assert out["verdict"] == core.PASS  # a restart is an event, not automatically a failure

    def test_first_and_none_samples_never_crash_the_event_detector(self):
        assert core.detect_events(None, ok_sample(5.0), 20.0) == []
        assert core.detect_events(ok_sample(5.0), None, 20.0) == []


# ==========================================================================
# 4. the verdict file: pass vs not_measured must be distinguishable
# ==========================================================================
class TestVerdictFile:
    def _doc(self, samples, budget=HEALTH_BUDGET):
        chk = core.summarise("health", samples, budget, window="t", nominal_interval_s=0)
        return core.build_verdict([chk], {"strict": False}), chk

    def test_pass_and_not_measured_are_different_in_the_file(self, tmp_path):
        doc_pass, _ = self._doc([ok_sample(12.0), ok_sample(14.0)])
        doc_nm, _ = self._doc([dead_sample(), dead_sample()])
        p = tmp_path / "pass.json"
        n = tmp_path / "nm.json"
        core.write_verdict(p, doc_pass)
        core.write_verdict(n, doc_nm)
        got_p, got_n = core.load_verdict(p), core.load_verdict(n)
        assert got_p["checks"][0]["verdict"] == core.PASS
        assert got_n["checks"][0]["verdict"] == core.NOT_MEASURED
        assert got_p["verdict"] == core.PASS
        assert got_n["verdict"] == core.NOT_MEASURED
        assert got_n["not_measured"] == ["health"]
        assert got_n["failed"] == []

    def test_rolled_up_verdict_is_never_greener_than_its_weakest_check(self):
        good = core.summarise("health", [ok_sample(5.0)], HEALTH_BUDGET, "t", 0)
        bad = core.summarise("preview", [ok_sample(9_000.0)], PREVIEW_BUDGET, "t", 0)
        doc = core.build_verdict([good, bad], {})
        assert doc["verdict"] == core.FAIL
        assert doc["totals"] == {core.PASS: 1, core.FAIL: 1, core.NOT_MEASURED: 0}

    def test_one_not_measured_check_sinks_the_roll_up_below_pass(self):
        good = core.summarise("health", [ok_sample(5.0)], HEALTH_BUDGET, "t", 0)
        unknown = core.summarise("preview", [], PREVIEW_BUDGET, "t", 0)
        doc = core.build_verdict([good, unknown], {})
        assert doc["verdict"] == core.NOT_MEASURED
        assert doc["not_measured"] == ["preview"]

    def test_validator_rejects_a_doctored_pass_where_nothing_was_measured(self, tmp_path):
        """The doctoring test: the validator must bite, not decorate."""
        doc, chk = self._doc([dead_sample()])
        assert core.validate_verdict(doc) == []
        doctored = json.loads(json.dumps(doc))
        doctored["checks"][0]["verdict"] = core.PASS
        doctored["checks"][0]["reason"] = "all good"
        problems = core.validate_verdict(doctored)
        assert any("unmeasured check cannot be a pass" in p for p in problems)

    def test_validator_rejects_not_measured_that_still_claims_measurements(self):
        _, chk = self._doc([ok_sample(5.0), dead_sample()])
        doctored = json.loads(json.dumps(core.build_verdict([chk], {})))
        doctored["checks"][0]["verdict"] = core.NOT_MEASURED
        problems = core.validate_verdict(doctored)
        assert any("n_measured" in p for p in problems)

    def test_validator_requires_a_population_and_a_reason(self):
        doc = {"schema": core.VERDICT_SCHEMA, "checks": [
            {"check": "health", "verdict": core.PASS, "n": 3, "n_measured": 3,
             "p50_ms": 5.0, "p95_ms": 5.0, "reason": ""}], "totals": {}}
        assert any("without a reason" in p for p in core.validate_verdict(doc))

    def test_validator_rejects_an_unknown_verdict_token(self):
        doc = {"schema": core.VERDICT_SCHEMA, "checks": [
            {"check": "health", "verdict": "green", "n": 1, "n_measured": 1, "reason": "x"}]}
        assert any("is not one of" in p for p in core.validate_verdict(doc))

    def test_validator_rejects_a_missing_population(self):
        doc = {"schema": core.VERDICT_SCHEMA, "checks": [
            {"check": "health", "verdict": core.PASS, "n_measured": 1, "reason": "x"}]}
        assert any("population n" in p for p in core.validate_verdict(doc))

    def test_verdict_file_is_valid_json_on_disk_and_round_trips(self, tmp_path):
        doc, _ = self._doc([ok_sample(5.0), ok_sample(6.0)])
        p = core.write_verdict(tmp_path / "v.json", doc)
        assert json.loads(p.read_text(encoding="utf-8"))["schema"] == core.VERDICT_SCHEMA
        assert core.validate_verdict(core.load_verdict(p)) == []

    def test_write_leaves_no_temp_file_behind(self, tmp_path):
        core.write_verdict(tmp_path / "v.json", self._doc([ok_sample(5.0)])[0])
        assert [p.name for p in tmp_path.iterdir()] == ["v.json"]

    def test_single_sample_is_labelled_as_a_point_in_time_reading(self):
        """One 200 is a sample. The file has to say so out loud."""
        out = core.summarise("health", [ok_sample(12.0)], HEALTH_BUDGET, "t", 0)
        assert out["verdict"] == core.PASS
        assert out["single_sample"] is True
        assert "SINGLE SAMPLE" in out["reason"]
        assert "not a distribution" in out["reason"]

    def test_multi_sample_summary_is_not_labelled_single(self):
        out = core.summarise("health", [ok_sample(12.0), ok_sample(13.0)], HEALTH_BUDGET, "t", 0)
        assert out["single_sample"] is False


# ==========================================================================
# 5. exit codes
# ==========================================================================
class TestExitCodes:
    def test_all_pass_is_zero(self):
        assert core.gate_exit_code([{"verdict": core.PASS}]) == 0

    def test_any_fail_is_one(self):
        assert core.gate_exit_code([{"verdict": core.PASS}, {"verdict": core.FAIL}]) == 1

    def test_not_measured_is_zero_by_default_but_three_under_strict(self):
        checks = [{"verdict": core.PASS}, {"verdict": core.NOT_MEASURED}]
        assert core.gate_exit_code(checks) == 0
        assert core.gate_exit_code(checks, strict=True) == 3

    def test_fail_outranks_strict_not_measured(self):
        assert core.gate_exit_code([{"verdict": core.FAIL}, {"verdict": core.NOT_MEASURED}],
                                   strict=True) == 1

    def test_empty_check_list_is_zero_not_a_pass_claim(self):
        assert core.gate_exit_code([]) == 0


# ==========================================================================
# 6. the archive guard
# ==========================================================================
class TestArchiveGuard:
    def test_orphan_archives_are_refused(self):
        for orphan in probe.ORPHAN_ARCHIVES:
            path = orphan.replace("%APPDATA%", r"C:\Users\Administrador\AppData\Roaming")
            out = probe.pick_real_youtube_video(path)
            assert out["ok"] is False
            assert out["video_id"] is None
            assert "ORPHANED" in out["reason"]

    def test_a_missing_archive_is_not_measured_not_an_empty_id(self):
        out = probe.pick_real_youtube_video(r"I:\TEMP\definitely-not-here\archive.db")
        assert out["ok"] is False
        assert out["video_id"] is None
        assert "archive read failed" in out["reason"]

    def test_preview_check_without_a_video_is_not_measured(self):
        ident = {"pid": 1, "proc_start": "t"}
        s = probe.take_preview("http://127.0.0.1:1", PREVIEW_BUDGET, 1.0, ident,
                               {"ok": False, "reason": "archive read failed (boom)"})
        assert s["verdict"] == core.NOT_MEASURED
        assert "archive read failed" in s["reason"]


# ==========================================================================
# 7. reading the history the running draft probe already wrote
# ==========================================================================
class TestDraftCompatibility:
    """The earlier tmp/liveness_probe.py wrote `event`, not `check`, and had no `epoch`.

    A strict reader scored its whole history as n=0 and reported NOT_MEASURED
    over real samples. Measured 2026-10-05 against the live app: n=0 before the
    shim, n=94 after. The samples were there; the reader was blind.
    """

    DRAFT_HEALTH = ('{"event": "health", "seq": 1, "ok": true, "status": 200, '
                    '"ms": 10891.0, "bytes": 215, "ts": "2026-10-05T05:01:48+00:00"}')
    DRAFT_PREVIEW = ('{"event": "preview", "url": "https://x", "ok": true, "status": 200, '
                     '"ms": 922.0, "ts": "2026-10-05T05:01:49+00:00"}')
    CURRENT = ('{"event": "sample", "check": "health", "epoch": 1000.0, "ok": true, '
               '"status": 200, "ms": 12.0, "ts": "2026-10-05T05:01:48+00:00"}')

    def test_draft_event_becomes_a_check(self):
        out = probe.normalise_record(json.loads(self.DRAFT_HEALTH))
        assert out["check"] == "health"

    def test_draft_ts_becomes_an_epoch(self):
        out = probe.normalise_record(json.loads(self.DRAFT_HEALTH))
        assert isinstance(out["epoch"], float)
        assert out["epoch"] > 1_700_000_000

    def test_a_current_record_is_untouched(self):
        out = probe.normalise_record(json.loads(self.CURRENT))
        assert out["check"] == "health"
        assert out["epoch"] == 1000.0

    def test_a_mixed_history_is_read_as_one_population(self, tmp_path):
        p = tmp_path / "liveness.jsonl"
        p.write_text("\n".join([self.CURRENT, self.DRAFT_HEALTH, self.DRAFT_PREVIEW,
                                "", "not json at all"]) + "\n", encoding="utf-8")
        health = probe.read_window(p, None, check="health")
        preview = probe.read_window(p, None, check="preview")
        assert len(health) == 2
        assert len(preview) == 1
        # The 10,891 ms draft sample must surface as a budget failure, not vanish.
        out = core.summarise("health", health, HEALTH_BUDGET, window="mixed", nominal_interval_s=20)
        assert out["verdict"] == core.FAIL
        assert out["n"] == 2 and out["max_ms"] == 10_891.0
        assert out["over_budget_count"] == 1

    def test_a_window_cutoff_still_applies_to_draft_records(self, tmp_path):
        p = tmp_path / "liveness.jsonl"
        p.write_text(self.DRAFT_HEALTH + "\n", encoding="utf-8")
        assert probe.read_window(p, 9_999_999_999.0, check="health") == []
        assert len(probe.read_window(p, 0.0, check="health")) == 1

    def test_an_absurd_ts_costs_that_record_not_the_whole_history(self, tmp_path):
        """datetime.timestamp() raises OSError on Windows for extreme dates.

        Measured 2026-10-11: datetime.fromisoformat("9999-12-31T23:59:59")
        .timestamp() raises OSError [Errno 22] -- neither TypeError nor
        ValueError -- so it escaped normalise_record and was then swallowed by
        read_window's own `except OSError: return []`: one unreadable ts
        emptied the whole window. That is the exact blindness this class exists
        to fix, one line further in. The record is kept without an epoch, and
        the good record beside it survives.
        """
        p = tmp_path / "liveness.jsonl"
        absurd = ('{"check": "health", "ok": true, "status": 200, "ms": 12.0, '
                  '"ts": "9999-12-31T23:59:59"}')
        p.write_text(self.CURRENT + "\n" + absurd + "\n", encoding="utf-8")

        assert probe.normalise_record(json.loads(absurd)).get("epoch") is None
        rows = probe.read_window(p, None, check="health")
        assert len(rows) == 2, rows


# ==========================================================================
# 8. The concurrent-platforms check grades the arms it actually got
# ==========================================================================
# MEASURED 2026-10-11: this check could never pass while the app was well. A
# gate run graded two samples that answered HTTP 200 (1453 ms and 3969 ms) as
# FAIL "HTTP 200", reason_class http_error, http_error_count=2. The merged
# sample inherited ok=False from the blank default in _concurrent_sample, and
# core._verdict carries no "ok" key, so classify() read every sample as an
# error. These tests pin the arms' truth onto the merged sample.
CONCURRENT_VIDEOS = {
    "youtube": {"ok": True, "video_id": "aaaaaaaaaaa"},
    "twitch": {"ok": True, "video_id": "1234567890"},
    "kick": {"ok": True, "video_id": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"},
}
CONCURRENT_IDENT = {"pid": 4242, "proc_start": "2026-10-11T00:00:00+00:00"}


class TestConcurrentPlatformsGradesItsArms:
    def _run(self, monkeypatch, answers):
        def fake_post(url, payload, timeout_s):
            assert url.endswith("/api/preview/session"), url
            return dict(answers[payload["platform"]])

        monkeypatch.setattr(probe, "timed_post_json", fake_post)
        s = probe.take_concurrent_platforms(
            "http://127.0.0.1:1", 3000.0, 5.0, CONCURRENT_IDENT,
            CONCURRENT_VIDEOS)
        return core.summarise("concurrent-platforms", [s], 3000.0,
                              window="one run")

    def test_three_arms_answering_within_budget_is_a_pass(self, monkeypatch):
        answers = {p: {"ok": True, "status": 200, "ms": 900.0, "error": None}
                   for p in CONCURRENT_VIDEOS}
        out = self._run(monkeypatch, answers)
        assert out["verdict"] == core.PASS, out["reason"]
        assert out["reason_class"] == "within_budget"
        assert out["max_ms"] == 900.0
        assert out["http_error_count"] == 0

    def test_the_slowest_arm_is_the_graded_number(self, monkeypatch):
        answers = {"youtube": {"ok": True, "status": 200, "ms": 300.0},
                   "twitch": {"ok": True, "status": 200, "ms": 4200.0},
                   "kick": {"ok": True, "status": 200, "ms": 800.0}}
        out = self._run(monkeypatch, answers)
        assert out["verdict"] == core.FAIL
        assert out["reason_class"] == "over_budget"
        assert out["max_ms"] == 4200.0

    def test_a_5xx_arm_is_an_http_error(self, monkeypatch):
        answers = {"youtube": {"ok": True, "status": 200, "ms": 900.0},
                   "twitch": {"ok": True, "status": 200, "ms": 900.0},
                   "kick": {"ok": False, "status": 503, "ms": 120.0,
                            "error": "HTTP 503"}}
        out = self._run(monkeypatch, answers)
        assert out["verdict"] == core.FAIL
        assert out["reason_class"] == "http_error"
        assert out["http_error_count"] == 1

    def test_an_answered_arm_with_no_timing_makes_the_whole_sample_unmeasured(self, monkeypatch):
        answers = {"youtube": {"ok": True, "status": 200, "ms": 900.0},
                   "twitch": {"ok": True, "status": 200, "ms": 900.0},
                   "kick": {"ok": True, "status": 200, "ms": None}}
        out = self._run(monkeypatch, answers)
        assert out["verdict"] == core.NOT_MEASURED
        assert out["reason_class"] == "no_timing"
        assert out["max_ms"] is None

    def test_an_unreachable_arm_is_unmeasured_not_a_zero_ms_pass(self, monkeypatch):
        answers = {"youtube": {"ok": True, "status": 200, "ms": 900.0},
                   "twitch": {"ok": False, "status": None, "ms": None,
                              "error": "ConnectionRefusedError: refused"},
                   "kick": {"ok": True, "status": 200, "ms": 900.0}}
        out = self._run(monkeypatch, answers)
        assert out["verdict"] == core.NOT_MEASURED
        # summarise() owns the aggregate vocabulary: one unreachable sample is
        # the whole population, so it reports the population-wide class.
        assert out["reason_class"] == "all_unreachable"
        assert out["n_measured"] == 0

    def test_a_malformed_status_is_graded_not_raised(self, monkeypatch):
        """A status nobody can read is an error, not an exception."""
        answers = {"youtube": {"ok": True, "status": "not-a-status", "ms": 900.0},
                   "twitch": {"ok": True, "status": 200, "ms": 900.0},
                   "kick": {"ok": True, "status": 200, "ms": 900.0}}
        out = self._run(monkeypatch, answers)
        assert out["verdict"] == core.FAIL
        assert out["reason_class"] == "http_error"

    def test_classify_never_raises_on_an_unreadable_status(self):
        v = core.classify({"ok": True, "status": "nope", "ms": 5.0}, 3000.0)
        assert v["verdict"] == core.FAIL
        assert v["reason_class"] == "http_error"
        # The stored status stays the raw one so the second pass agrees.
        assert core.classify(dict(v, ok=True), 3000.0)["verdict"] == core.FAIL

    def test_classify_never_raises_on_a_status_of_infinity(self):
        """int(float("inf")) raises OverflowError, not ValueError.

        Measured 2026-10-11 on this box: int(float("inf")) -> OverflowError,
        int(float("nan")) -> ValueError. The handler caught only the latter, so
        a status that says "no number at all" escaped a function whose whole
        contract is that it never raises -- and escaped the same way in
        probe._status_code, one layer up, where it would lose a whole arm.
        """
        for status in (float("inf"), float("-inf"), float("nan")):
            v = core.classify({"ok": True, "status": status, "ms": 5.0}, 3000.0)
            assert v["verdict"] == core.FAIL, status
            assert v["reason_class"] == "http_error", status
            # the raw value is stored, so the second pass reaches the same answer
            assert core.classify(dict(v, ok=True), 3000.0)["verdict"] == core.FAIL
        assert probe._status_code(float("inf")) is None
        assert probe._is_2xx_3xx(float("inf")) is False


class TestUnknownPlatformsAreRefused:
    """A platform with no shape and no URL builder is not a candidate."""

    def test_an_unknown_platform_is_refused(self):
        assert probe._shape_ok("myspace", "aaaaaaaaaaa") is False
        assert probe.platform_url("myspace", {"video_id": "aaaaaaaaaaa"}) is None

    def test_every_known_platform_still_matches_its_own_shape(self):
        assert probe._shape_ok("youtube", "aaaaaaaaaaa") is True
        assert probe._shape_ok("twitch", "1234567890") is True
        assert probe._shape_ok("kick", "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0") is True

    def test_a_wrong_shaped_id_is_still_refused(self):
        assert probe._shape_ok("youtube", "too-short") is False
        assert probe._shape_ok("twitch", "abc") is False
        assert probe._shape_ok("youtube", "") is False

    def test_every_platform_the_check_uses_has_both_a_shape_and_a_url(self):
        for p in probe.CONCURRENT_PLATFORMS:
            assert p in probe._VIDEO_ID_SHAPE, p
            assert p in probe._PLATFORM_URL, p

# ==========================================================================
# 9. The preview checks warm the session before they grade it
# ==========================================================================
# MEASURED 2026-10-11, against the live app: the newest archived video answered
# its FIRST /api/preview/session in 26,719 ms (HTTP 200) and then, with the same
# session_id, in 43 ms, 6 ms and 16 ms. Gate run 4 graded that first call against
# the 3000 ms budget and went red - and the previous run passed with 16 ms on a
# session that happened to be warm. A check whose verdict is decided by "did the
# archive grow since the last preview" is not measuring the app. These tests pin
# which of the two calls is graded, in both directions.
PREVIEW_VIDEO = {"ok": True, "video_id": "G7n-8q6PuwE", "channel": "Gaveta"}


class TestPreviewGradesTheWarmCall:
    def _run(self, monkeypatch, cold, warm):
        calls = []

        def fake_post(url, payload, timeout_s):
            calls.append(timeout_s)
            return dict(cold if len(calls) == 1 else warm)

        monkeypatch.setattr(probe, "timed_post_json", fake_post)
        s = probe.take_preview("http://127.0.0.1:1", 3000.0, 5.0,
                               CONCURRENT_IDENT, PREVIEW_VIDEO)
        return s, calls

    def test_a_cold_session_is_recorded_but_not_graded(self, monkeypatch):
        s, calls = self._run(monkeypatch,
                             {"ok": True, "status": 200, "ms": 26_719.0},
                             {"ok": True, "status": 200, "ms": 6.0})
        assert len(calls) == 2, calls
        assert s["path"] == "warm"
        assert s["ms"] == 6.0
        assert s["cold_ms"] == 26_719.0
        assert s["verdict"] == core.PASS, s["reason"]
        assert "not graded" in s["reason"]

    def test_the_graded_call_is_still_graded(self, monkeypatch):
        """Control: a slow WARM call must fail, or the warm-up hides the app."""
        s, _ = self._run(monkeypatch,
                         {"ok": True, "status": 200, "ms": 26_719.0},
                         {"ok": True, "status": 200, "ms": 9_000.0})
        assert s["verdict"] == core.FAIL
        assert s["ms"] == 9_000.0

    def test_a_cold_session_that_never_opens_is_not_a_pass(self, monkeypatch):
        s, _ = self._run(monkeypatch,
                         {"ok": False, "status": None, "ms": 180_000.0,
                          "error": "TimeoutError: timed out"},
                         {"ok": True, "status": 200, "ms": 6.0})
        assert s["verdict"] != core.PASS
        assert s["path"] == "cold"
        assert "cold session could not be created" in s["reason"]

    def test_the_cold_warmup_gets_a_timeout_the_real_path_fits_in(self, monkeypatch):
        """A 27 s cold session must not be killed by the 5 s request timeout."""
        _, calls = self._run(monkeypatch,
                             {"ok": True, "status": 200, "ms": 26_719.0},
                             {"ok": True, "status": 200, "ms": 6.0})
        assert calls[0] >= probe.PREVIEW_COLD_TIMEOUT_S
        assert calls[1] == 5.0

    def test_the_concurrent_round_grades_the_warm_arms(self, monkeypatch):
        """The concurrency check must not grade three cold extractions."""

        def fake_post(url, payload, timeout_s):
            # The warm-ups are the calls that ask for the long timeout, so this
            # fake needs no counter and no lock to tell them apart.
            cold = timeout_s >= probe.PREVIEW_COLD_TIMEOUT_S
            return {"ok": True, "status": 200,
                    "ms": 26_719.0 if cold else 40.0}

        monkeypatch.setattr(probe, "timed_post_json", fake_post)
        s = probe.take_concurrent_platforms("http://127.0.0.1:1", 3000.0, 5.0,
                                            CONCURRENT_IDENT, CONCURRENT_VIDEOS)
        out = core.summarise("concurrent-platforms", [s], 3000.0, window="one run")
        assert out["verdict"] == core.PASS, out["reason"]
        assert out["max_ms"] == 40.0
        assert [a["warmup_ms"] for a in s["arms"]] == [26_719.0] * 3
