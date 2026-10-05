"""Unit tests for the verification runner.

The runner is the thing that reports a green, so its own rules are tested here:
one pytest at a time, no piped exit codes, and an empty node_modules is not a
pass. Every one of those has already produced a false green in this repo.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
GATE_DIR = HERE.parent
REPO_ROOT = GATE_DIR.parents[1]
sys.path.insert(0, str(GATE_DIR))

import probe_core as core  # noqa: E402
import verify_gate as gate  # noqa: E402


# ==========================================================================
# 1. one pytest at a time
# ==========================================================================
class TestOnePytestAtATime:
    def test_no_live_pytest_means_the_guard_is_clear(self):
        verdict, reason, _ = gate.pytest_guard_decision([])
        assert verdict == core.PASS
        assert "no other pytest is live" in reason

    def test_a_live_pytest_makes_the_suite_not_measured_not_failed(self):
        live = [{"pid": 41968, "cmdline": "python -m pytest tests/..."}]
        verdict, reason, detail = gate.pytest_guard_decision(live)
        assert verdict == core.NOT_MEASURED
        assert "another pytest is live" in reason
        assert "pid 41968" in reason
        assert detail["live_pytest"] == 1
        # Never a fail: the gate must not go red for refusing to double up.
        assert verdict != core.FAIL

    def test_an_unreadable_guard_does_not_authorise_a_run(self):
        verdict, reason, _ = gate.pytest_guard_decision(None, guard_error="WMI query timed out")
        assert verdict == core.NOT_MEASURED
        assert "must not authorise a run" in reason

    def test_fast_mode_is_not_measured(self):
        verdict, reason, _ = gate.pytest_guard_decision([], fast=True)
        assert verdict == core.NOT_MEASURED
        assert "--fast" in reason

    def test_the_real_guard_query_never_counts_this_gate_as_a_live_pytest(self):
        """Self-exclusion: the gate must not skip itself forever."""
        try:
            live = gate.live_pytest_processes()
        except RuntimeError as exc:  # pragma: no cover - only on a broken host
            pytest.skip(f"guard query unavailable: {exc}")
        assert isinstance(live, list)
        for entry in live:
            assert "verify_gate.py" not in entry.get("cmdline", "")
            assert "verify.ps1" not in entry.get("cmdline", "")


# ==========================================================================
# 2. node_modules can exist and be empty
# ==========================================================================
class TestNodeModules:
    def test_missing_node_modules_is_reported_as_zero_files(self, tmp_path):
        n, note = gate.node_modules_state(tmp_path)
        assert n == 0
        assert "does not exist" in note

    def test_an_existing_but_empty_node_modules_is_zero_files(self, tmp_path):
        """The exact defect: the directory exists, so the check 'passes', and nothing runs."""
        (tmp_path / "node_modules").mkdir()
        n, note = gate.node_modules_state(tmp_path)
        assert n == 0
        assert "0 file" in note
        assert n < gate.MIN_NODE_MODULE_FILES

    def test_a_stub_tree_is_still_too_small(self, tmp_path):
        nm = tmp_path / "node_modules"
        nm.mkdir()
        (nm / "package.json").write_text("{}", encoding="utf-8")
        n, _ = gate.node_modules_state(tmp_path)
        assert n == 1
        assert n < gate.MIN_NODE_MODULE_FILES

    def test_a_junctioned_node_modules_is_actually_counted(self, tmp_path):
        """A worktree's node_modules is a JUNCTION into the main checkout.

        The first version of count_files used os.walk, which does not descend
        into a junction. Measured on 2026-10-05: PowerShell saw 10,850 files
        while the walker saw 709 for the same directory - and 709 is close
        enough to the 500-file floor that the two counters could disagree
        across a safety threshold. The walk must follow the junction.
        """
        import os
        real = tmp_path / "real_modules"
        (real / "pkg").mkdir(parents=True)
        for i in range(gate.MIN_NODE_MODULE_FILES + 5):
            (real / "pkg" / f"f{i}.js").write_text("x", encoding="utf-8")
        link = tmp_path / "node_modules"
        try:
            os.symlink(real, link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError) as exc:
            pytest.skip(f"cannot create a directory link here: {exc}")
        n, _ = gate.node_modules_state(tmp_path)
        assert n >= gate.MIN_NODE_MODULE_FILES, (
            f"junctioned node_modules counted {n} files, expected >= "
            f"{gate.MIN_NODE_MODULE_FILES}")

    def test_a_self_referential_junction_terminates(self, tmp_path):
        """A junction pointing at its own ancestor must not hang the gate."""
        import os
        d = tmp_path / "loop"
        d.mkdir()
        try:
            os.symlink(tmp_path, d / "self", target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError) as exc:
            pytest.skip(f"cannot create a directory link here: {exc}")
        (d / "a.txt").write_text("x", encoding="utf-8")
        assert gate.count_files(d, cap=100) >= 1

    def test_a_populated_tree_is_accepted(self, tmp_path):
        nm = tmp_path / "node_modules"
        (nm / "pkg").mkdir(parents=True)
        for i in range(gate.MIN_NODE_MODULE_FILES + 5):
            (nm / "pkg" / f"f{i}.js").write_text("x", encoding="utf-8")
        n, _ = gate.node_modules_state(tmp_path)
        assert n >= gate.MIN_NODE_MODULE_FILES


# ==========================================================================
# 3. never pipe a native command when you need its exit code
# ==========================================================================
class TestExitCodesAreReal:
    def test_exit_zero_is_a_pass(self):
        c = gate.ExecCheck("ok", [sys.executable, "-c", "print('hi')"]).run(60)
        v = c.verdict()
        assert c.exit_code == 0
        assert v["verdict"] == core.PASS
        assert "exit 0" in v["reason"]

    def test_a_nonzero_exit_is_a_failure(self):
        c = gate.ExecCheck("bad", [sys.executable, "-c", "raise SystemExit(3)"]).run(60)
        v = c.verdict()
        assert c.exit_code == 3
        assert v["verdict"] == core.FAIL
        assert "exit 3" in v["reason"]

    def test_a_failing_command_that_prints_a_thousand_lines_is_still_caught(self):
        """A `cmd | Select-Object -First N` truncates here and reports a green.

        The exit code is read from the process, so the truncation cannot hide it.
        """
        script = ("import sys\n"
                  "for i in range(1000): print('noise', i)\n"
                  "sys.stdout.flush()\n"
                  "raise SystemExit(7)\n")
        c = gate.ExecCheck("noisy", [sys.executable, "-c", script]).run(120)
        v = c.verdict()
        assert c.exit_code == 7
        assert v["verdict"] == core.FAIL
        # And the full output really did land in the log, untruncated.
        assert c.log.exists()
        assert "noise 999" in c.log.read_text(encoding="utf-8", errors="replace")

    def test_a_command_that_cannot_start_is_not_measured_not_pass(self):
        c = gate.ExecCheck("ghost", ["definitely-not-a-real-binary-xyz"]).run(30)
        v = c.verdict()
        assert c.exit_code is None
        assert v["verdict"] == core.NOT_MEASURED
        assert "not measured" in v["reason"]

    def test_a_hanging_command_times_out_as_not_measured(self):
        c = gate.ExecCheck("hang", [sys.executable, "-c", "import time; time.sleep(30)"]).run(1.5)
        v = c.verdict()
        assert c.exit_code is None
        assert v["verdict"] == core.NOT_MEASURED
        assert "timed out" in v["reason"]

    def test_a_failing_check_makes_the_gate_exit_nonzero(self):
        c = gate.ExecCheck("bad", [sys.executable, "-c", "raise SystemExit(1)"]).run(60)
        assert core.gate_exit_code([c.verdict()]) == 1


# ==========================================================================
# 3b. the gate must not die while reporting a failure
# ==========================================================================
class TestReportingSurvivesAwkwardBytes:
    """Measured 2026-10-05: a full gate run died with UnicodeEncodeError.

    vitest marks a failed file with U+276F (a right-pointing angle quote). This
    console is cp1252, so printing the log tail of a FAILING vitest run raised
    `'charmap' codec can't encode character '\\u276f'` - the gate crashed at the
    exact moment it was reporting a failure, and the exit 1 that followed came
    from the crash rather than from the gate's verdict. A reporter that dies
    while reporting is worse than no reporter.
    """

    VITEST_TAIL = (" FAIL  src/downloadLayout.test.ts > suite\n"
                   " \u276f src/downloadLayout.test.ts (1 test | 1 failed)\n"
                   " \u00e9\u00e0\u00fc non-ascii in a filename\n")

    def test_log_tail_survives_the_vitest_failure_glyph(self, tmp_path):
        p = tmp_path / "vitest.log"
        p.write_text(self.VITEST_TAIL, encoding="utf-8")
        tail = gate.log_tail(p, 10)
        assert tail  # did not raise
        assert tail.isascii(), "the tail must be console-safe"
        assert "non-ascii" in tail

    def test_safe_text_folds_and_bounds(self):
        assert gate.safe_text("a\u276fb").isascii()
        long = gate.safe_text("x" * 5000, limit=100)
        assert len(long) < 200
        assert "truncated" not in long  # the marker is "+N chars", not the word
        assert "+4900 chars" in long

    def test_safe_text_leaves_plain_ascii_untouched(self):
        assert gate.safe_text("exit 3 in 12.4s") == "exit 3 in 12.4s"

    def test_force_utf8_stdio_is_idempotent_and_never_raises(self, capsys):
        gate.force_utf8_stdio()
        gate.force_utf8_stdio()
        # The exact string that crashed the gate must now print without raising.
        print(gate.safe_text(self.VITEST_TAIL))
        out = capsys.readouterr().out
        assert out.isascii()
        assert "1 failed" in out

    def test_a_failing_step_with_non_ascii_output_is_still_reported_as_fail(self, tmp_path):
        """The full loop: a tool that fails AND prints a glyph must yield FAIL, not a crash."""
        # Written to a real file so the glyph is a real byte sequence, with no
        # escape nesting to get wrong.
        child = tmp_path / "failing_tool.py"
        child.write_text(
            "import sys\n"
            "sys.stdout.reconfigure(encoding='utf-8')\n"
            "print(' \u276f src/x.test.ts (1 test | 1 failed)')\n"
            "print(' \u00e9\u00e0\u00fc accented')\n"
            "raise SystemExit(1)\n",
            encoding="utf-8")
        c = gate.ExecCheck("glyphs", [sys.executable, str(child)]).run(60)
        v = c.verdict()
        assert c.exit_code == 1
        assert v["verdict"] == core.FAIL
        raw = c.log.read_text(encoding="utf-8", errors="replace")
        assert "\u276f" in raw, "the glyph must actually reach the log"
        tail = gate.log_tail(c.log, 10)
        assert tail.isascii() and "1 failed" in tail


# ==========================================================================
# 4. end to end: an entirely unmeasured gate must not claim a pass
# ==========================================================================
class TestGateEndToEnd:
    def _run(self, *extra, repo: Path):
        return subprocess.run(
            [sys.executable, str(GATE_DIR / "verify_gate.py"),
             "--repo", str(repo), "--skip-probe", "--fast",
             "--verdict", str(repo / "tmp" / "verify-verdict.json"), *extra],
            capture_output=True, text=True, timeout=300, cwd=str(repo))

    def test_empty_repo_gate_is_all_not_measured_and_exits_zero(self, tmp_path):
        """No node_modules, no app, no pytest. Nothing was learned; nothing may claim green."""
        proc = self._run(repo=tmp_path)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "NOT_MEASURED" in proc.stdout
        assert "GATE VERDICT: NOT_MEASURED" in proc.stdout
        # The word PASS must never appear as a verdict for an unmeasured check.
        assert "  PASS " not in proc.stdout
        doc = core.load_verdict(tmp_path / "tmp" / "verify-verdict.json")
        assert doc is not None
        assert doc["verdict"] == core.NOT_MEASURED
        assert set(doc["failed"]) == set()
        assert set(doc["not_measured"]) >= {"probe", "tsc", "vitest"}
        assert core.validate_verdict(doc) == []

    def test_strict_makes_an_unmeasured_gate_exit_three(self, tmp_path):
        proc = self._run("--strict", repo=tmp_path)
        assert proc.returncode == 3, proc.stdout + proc.stderr
        assert "GATE VERDICT: NOT_MEASURED" in proc.stdout

    def test_the_gate_writes_a_machine_readable_verdict_with_populations(self, tmp_path):
        self._run(repo=tmp_path)
        doc = core.load_verdict(tmp_path / "tmp" / "verify-verdict.json")
        for c in doc["checks"]:
            assert c["verdict"] in core.VERDICTS
            assert isinstance(c["n"], int) and c["n"] >= 0
            assert str(c["reason"]).strip()
        assert doc["meta"]["gate"] == "scripts/verify.ps1"


# ==========================================================================
# 5. the gate must bite: a closed port has to be caught
# ==========================================================================
class TestGateBites:
    def test_selftest_arm_against_a_closed_port_is_not_measured(self, tmp_path):
        proc = subprocess.run(
            [sys.executable, str(GATE_DIR / "probe.py"), "--selftest-arm",
             "--verdict", str(tmp_path / "v.json"),
             "--jsonl", str(tmp_path / "l.jsonl")],
            capture_output=True, text=True, timeout=180)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "closed port" in proc.stdout
        assert "[NOT_MEASURED" in proc.stdout
        assert "expected 'not_measured'" not in proc.stdout
        # The arm writes its own file so it can never clobber a real verdict.
        assert not (tmp_path / "v.json").exists()
        doc = core.load_verdict(tmp_path / "selftest-verdict.json")
        assert doc is not None
        assert doc["verdict"] == core.NOT_MEASURED
        assert core.validate_verdict(doc) == []

    def test_selftest_arm_goes_red_if_a_probe_lied_about_an_unreachable_port(self, monkeypatch,
                                                                            tmp_path):
        """Mutation arm: the selftest must catch a probe that invents a pass.

        The original defect, reproduced exactly - a transport failure dressed up
        as `200 in 0.0 ms`. Without this arm, a selftest that always exits 0
        proves nothing, and a gate never seen red is not a gate.
        """
        import argparse
        import probe

        def lying_get(url, timeout_s):
            return {"ok": True, "status": 200, "ms": 0.0, "bytes": 0}  # the lie

        monkeypatch.setattr(probe, "timed_get", lying_get)
        args = argparse.Namespace(
            api="http://127.0.0.1:7897", archive=probe.LIVE_ARCHIVE,
            jsonl=str(tmp_path / "l.jsonl"), verdict=str(tmp_path / "v.json"),
            health_budget_ms=1000.0, preview_budget_ms=3000.0, window_minutes=30.0,
            strict=False, selftest_arm=True, repo=str(tmp_path))
        assert probe._selftest_bites(args) == 9, "the selftest did not catch a fabricated 200"

    def test_a_budget_violation_alone_makes_the_window_fail(self):
        """The over-budget arm: a real 200 can still be a failure."""
        out = core.summarise("health", [{"ok": True, "status": 200, "ms": 10_891.0,
                                         "ts": "t", "epoch": 1.0, "pid": 1,
                                         "proc_start": "s"}],
                             1000.0, window="t", nominal_interval_s=0)
        assert out["verdict"] == core.FAIL
        assert out["over_budget_count"] == 1
        assert core.validate_verdict(core.build_verdict([out], {})) == []


# ==========================================================================
# 6. real inputs, not fakes
# ==========================================================================
class TestRealInputs:
    def test_the_live_archive_guard_knows_which_file_is_live(self):
        import probe
        assert probe.LIVE_ARCHIVE.lower() == r"h:\vod.rip-data\archive.db".lower()
        assert any("G:" in o for o in probe.ORPHAN_ARCHIVES)
        assert any("APPDATA" in o for o in probe.ORPHAN_ARCHIVES)

    def test_a_real_video_id_comes_from_the_live_archive_read_only(self):
        import probe
        if not Path(probe.LIVE_ARCHIVE).exists():
            pytest.skip("live archive not present on this host")
        out = probe.pick_real_youtube_video(probe.LIVE_ARCHIVE)
        assert out["ok"] is True, out
        assert out["video_id"] and isinstance(out["video_id"], str)
        assert len(out["video_id"]) == 11
        assert out["archive"] == probe.LIVE_ARCHIVE

    def test_serving_identity_against_a_closed_port_is_unknown_not_guessed(self):
        import socket
        import probe
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        ident = probe.serving_identity(f"http://127.0.0.1:{port}")
        assert ident["pid"] is None
        assert ident["proc_start"] is None
        assert ident["identity_error"]

    def test_a_url_without_a_port_cannot_yield_an_identity(self):
        import probe
        assert probe._port_from_url("http://127.0.0.1:7897") == 7897
        assert probe._port_from_url("http://example.com") is None
        ident = probe.serving_identity("http://example.com")
        assert ident["pid"] is None
