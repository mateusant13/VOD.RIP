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
    def test_the_guard_sees_a_real_process_that_mentions_pytest(self):
        """Control for the exclusion below: the query really reads live command
        lines, so an exclusion test cannot pass by seeing nothing at all."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)  # pytest"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            try:
                seen = gate.live_pytest_processes()
            except RuntimeError as exc:  # pragma: no cover - broken host only
                pytest.skip(f"guard query unavailable: {exc}")
            pids = {entry["pid"] for entry in seen}
            assert proc.pid in pids, (
                f"the guard did not see live pid {proc.pid}, whose command line "
                f"mentions pytest; it saw {sorted(pids)}"
            )
            excluded = gate.live_pytest_processes(exclude_pids={proc.pid})
            assert proc.pid not in {entry["pid"] for entry in excluded}, (
                "exclude_pids did not remove the gate's own child: the gate would "
                "then skip the backend suite it just ran itself"
            )
        finally:
            proc.kill()
            proc.wait()

    def test_a_step_records_the_pid_it_spawned(self):
        """The exclusion has something to exclude only if run() keeps the pid."""
        check = gate.Check("pid_probe", [sys.executable, "-c", "print(1)"]).run(60)
        assert check.pid is not None and check.pid > 0
        assert check.exit_code == 0
        assert check.detail["duration_s"] >= 0



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

    def test_the_reported_count_is_a_total_not_a_truncated_floor(self, tmp_path):
        """``node_modules_state`` reports a POPULATION, so it must be exact.

        The sibling test above asserts only ``n >= MIN_NODE_MODULE_FILES``, which
        passes just as happily on a truncated 500 as on a true 10,848 - a green
        that cannot go red. This one pins the exact number.

        Measured defect (2026-10-06, this repo, ``<repo>/node_modules``):
        ``count_files(nm, cap=MIN_NODE_MODULE_FILES)`` returned exactly 500 while
        an uncapped walk of the SAME directory returned 10,848, and the gate
        published ``node_modules holds 500 file(s)`` into the verdict file as if
        it were a total. A floor presented as a total is the one number a lane
        cannot audit: 500 sits exactly ON the trust floor, so "at the threshold"
        and "ten thousand files over it" are indistinguishable to the reader.
        """
        nm = tmp_path / "node_modules"
        (nm / "pkg").mkdir(parents=True)
        expected = gate.MIN_NODE_MODULE_FILES + 5
        for i in range(expected):
            (nm / "pkg" / f"f{i}.js").write_text("x", encoding="utf-8")

        n, note = gate.node_modules_state(tmp_path)

        # Exact, not ">=". A capped walk returns MIN_NODE_MODULE_FILES here and
        # fails this line; an uncapped walk returns `expected` and passes.
        assert n == expected, (
            f"node_modules_state reported {n}, but the tree holds exactly "
            f"{expected}. A truncated count was published as a total."
        )
        # printed == matched: the human-readable note must carry the same number.
        assert str(expected) in note, (
            f"note {note!r} does not carry the measured total {expected}"
        )

    def test_the_cap_remains_available_for_early_exit_callers(self, tmp_path):
        """The ``cap`` argument is a speed knob and stays; the REPORTING site
        simply must not use it. A caller that wants an early exit can still get
        one, and it is the caller's job to label the result a floor."""
        d = tmp_path / "tree"
        (d / "pkg").mkdir(parents=True)
        for i in range(40):
            (d / "pkg" / f"f{i}.js").write_text("x", encoding="utf-8")
        assert gate.count_files(d, cap=10) == 10
        assert gate.count_files(d, cap=None) == 40


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
        import probe

        def lying_get(url, timeout_s):
            return {"ok": True, "status": 200, "ms": 0.0, "bytes": 0}  # the lie

        monkeypatch.setattr(probe, "timed_get", lying_get)
        # Parsed by the probe's OWN parser and then overridden. A hand-
        # written Namespace is a fixture that rots, and its rot surfaces as
        # an AttributeError inside a grade rather than as a red arm.
        args = probe.build_parser().parse_args([])
        args.api = "http://127.0.0.1:7897"
        args.archive = probe.LIVE_ARCHIVE
        args.jsonl = str(tmp_path / "l.jsonl")
        args.verdict = str(tmp_path / "v.json")
        args.health_budget_ms = 1000.0
        args.preview_budget_ms = 3000.0
        args.window_minutes = 30.0
        args.strict = False
        args.selftest_arm = True
        args.repo = str(tmp_path)
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

# ==========================================================================
# 7. the probe's verdict file belongs to THIS run, not the previous one
# ==========================================================================
class TestAStaleVerdictIsNotThisRunAnswer:
    """The verdict file, not the exit code, is what the gate grades the probe
    from -- so a document left behind by an earlier run is an answer this run
    never earned. A probe that fails to write is SILENCE, not a crash, and a
    gate that fills silence in from disk grades the tree off a run that no
    longer describes it.
    """

    def _repo_and_fake_probe(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "tmp").mkdir(parents=True)
        here = tmp_path / "fake-observability"
        here.mkdir()
        # Starts, exits 0, writes nothing -- and leaves a marker so the test can
        # tell "ran and stayed silent" from "was refused before it started".
        (here / "probe.py").write_text(
            "import pathlib, sys\n"
            "pathlib.Path(__file__).with_name('probe-ran').write_text('ran', encoding='utf-8')\n"
            "sys.exit(0)\n",
            encoding="utf-8")
        return repo, here

    def _stale_healthy_verdict(self, verdict: Path) -> dict | None:
        healthy = core.summarise("health", [{"check": "health", "ok": True, "status": 200,
                                            "ms": 12.0}], 2500.0, window="a previous run")
        assert healthy["verdict"] == core.PASS, healthy
        core.write_verdict(verdict, core.build_verdict([healthy], {"gate": "a previous run"}))
        return core.load_verdict(verdict)

    def _probe_check(self, doc):
        return next(c for c in doc["checks"] if c["check"] == "probe")

    def test_a_healthy_verdict_from_a_previous_run_is_cleared_not_read(self, monkeypatch,
                                                                      tmp_path, capsys):
        repo, here = self._repo_and_fake_probe(tmp_path)
        verdict = repo / "tmp" / "verify-verdict.json"
        stale = self._stale_healthy_verdict(verdict)
        # The trap is armed first: the file on disk IS a readable healthy verdict.
        assert stale is not None and stale["verdict"] == core.PASS
        monkeypatch.setattr(gate, "HERE", here)
        rc = gate.main(["--repo", str(repo), "--fast", "--skip-laws",
                        "--verdict", str(verdict)])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert (here / "probe-ran").exists(), "the probe must have run in this arm"
        doc = core.load_verdict(verdict)
        assert doc is not None
        assert "probe.health" not in [c["check"] for c in doc["checks"]], (
            "a previous run's check was graded as this run's answer")
        probe_check = self._probe_check(doc)
        assert probe_check["verdict"] == core.NOT_MEASURED
        assert "wrote no verdict file" in probe_check["reason"]
        assert core.validate_verdict(doc) == []

    def test_a_verdict_path_that_cannot_be_cleared_refuses_the_probe(self, monkeypatch,
                                                                    tmp_path, capsys):
        repo, here = self._repo_and_fake_probe(tmp_path)
        verdict = repo / "tmp" / "verify-verdict.json"
        monkeypatch.setattr(gate, "HERE", here)
        monkeypatch.setattr(gate, "_clear_stale_verdict",
                            lambda p: f"cannot remove the previous verdict file {p}: denied")
        rc = gate.main(["--repo", str(repo), "--fast", "--skip-laws",
                        "--verdict", str(verdict)])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert not (here / "probe-ran").exists(), (
            "the probe ran against a verdict path this run could not own")
        doc = core.load_verdict(verdict)
        assert doc is not None
        probe_check = self._probe_check(doc)
        assert probe_check["verdict"] == core.NOT_MEASURED
        assert probe_check["reason_class"] == "stale_verdict_guard"
        assert "cannot remove the previous verdict file" in probe_check["reason"]

    def test_the_clear_helper_reports_a_path_it_cannot_remove(self, tmp_path):
        # A directory is the cheapest real path unlink() refuses on both
        # platforms, so this drives the helper rather than the filesystem.
        target = tmp_path / "verdict-dir"
        target.mkdir()
        stale = gate._clear_stale_verdict(target)
        assert stale is not None and "cannot remove the previous verdict file" in stale
        # A path that was simply never written is not an error: most runs start clean.
        assert gate._clear_stale_verdict(tmp_path / "never-written.json") is None

