"""Tests for the law layer: the runner, the checker library, the gate fold.

Two of these are regressions for greens that were MEASURED fake, and those
two are worth more than the passing ones:

* a red-input document that no longer applies used to report PASS.  The runner
  now refuses (NO_VERDICT / RED_NOT_EVALUATED) and the gate folds that to
  not_measured - never into a pass.  Red not reproduced is an UNFALSIFIABLE
  law, and an unfalsifiable law is not evidence.
* a checker whose verdict word contradicts its own exit code is not a pass.
  The gate re-checks the numbers, so a word that lies fails closed.

The maxims this file exists to police:
  an unmeasured check is never a pass;
  a guard that cannot read must not authorise a run;
  a law that cannot fail is not evidence.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
GATE_DIR = HERE.parent
REPO_ROOT = GATE_DIR.parents[1]
LAWS_DIR = REPO_ROOT / "scripts" / "laws"
REGISTRY = LAWS_DIR / "laws.vodrip.json"
VERIFY_PS1 = REPO_ROOT / "scripts" / "verify.ps1"
for _p in (str(GATE_DIR), str(LAWS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import check_preview_ceiling_order as ceiling  # noqa: E402
import check_preview_deadline_key as deadline  # noqa: E402
import check_probe_concurrent_platforms as concurrent  # noqa: E402
import lawchecks  # noqa: E402
import lawrunner  # noqa: E402
import probe_core as core  # noqa: E402
import verify_gate as gate  # noqa: E402


# === fixture checkers ====================================================

FAKE_CHECKER = '''\
"""Stand-in law: prints the verdict word the fixture asked for and exits with
the matching code - or, in one case, a deliberately WRONG code, because the
word a checker prints is a claim and its exit code is the only evidence.
"""
import os
import sys

WORDS = {"PASS": 0, "FAIL": 1, "NO_VERDICT": 2}
NAME = os.environ.get("VODRIP_LAW_NAME", "unnamed")
ON_RED_ARM = bool(os.environ.get("VODRIP_LAW_RED_INPUT"))
MODE = os.environ.get("OD_LAWTEST_MODE", "plain")

if MODE == "no_line":
    # exit 0 and print nothing: the runner may not invent a verdict line
    sys.exit(0)
if MODE == "lying_word":
    WORD, CODE = "PASS", 1
elif MODE == "red_silent" and ON_RED_ARM:
    # exits nonzero having printed NOTHING: an arm that never said what it found
    # has not reproduced the red, and it is not a pass either
    sys.exit(1)
elif MODE == "red_two_lines" and ON_RED_ARM:
    # two verdict lines: the law never said ONE thing, so there is no word left
    # to corroborate against its exit code
    print(f"law-verdict: FAIL {NAME} | first of two", flush=True)
    print(f"law-verdict: PASS {NAME} | second of two", flush=True)
    sys.exit(1)
elif MODE == "red_lying_word" and ON_RED_ARM:
    WORD, CODE = "FAIL", 0
elif MODE == "red_hang" and ON_RED_ARM:
    import time
    time.sleep(120)
    sys.exit(1)
elif ON_RED_ARM:
    WORD = os.environ.get("OD_LAWTEST_RED_WORD", "PASS")
    CODE = WORDS[WORD]
else:
    WORD = os.environ.get("OD_LAWTEST_CLEAN_WORD", "PASS")
    CODE = WORDS[WORD]
ARM = "red" if ON_RED_ARM else "clean"
print(f"law-verdict: {WORD} {NAME} | fixture law, mode {MODE}, arm {ARM}", flush=True)
sys.exit(CODE)
'''

# A law that MEASURES its subject: it applies the red input to an in-memory
# image and may only pass when the mutated image really breaks.  This is the
# shape every real law in scripts/laws/ has, so this fixture exercises
# lawchecks itself rather than a mock of it.
HONEST_CHECKER = '''\
"""A stand-in law that measures its subject instead of trusting it."""
import os
import sys

import lawchecks


def main() -> int:
    root = lawchecks.repo_root()
    name = lawchecks.law_name("subject")
    tree = lawchecks.load_named_files(root, ["subject.txt"])
    clean = tree["subject.txt"]
    if os.environ.get(lawchecks.ENV_RED_INPUT):
        mutated = lawchecks.apply_red_input(tree, root)
        if mutated is None or mutated["subject.txt"] == clean:
            return lawchecks.finish(
                lawchecks.WORD_NO_VERDICT,
                "the red document changed nothing, so a green here would be unearned")
        return lawchecks.finish(
            lawchecks.WORD_FAIL,
            f"the red document reproduced the broken subject for {name}")
    if clean != "RED LINE PRESENT\\n":
        return lawchecks.finish(
            lawchecks.WORD_FAIL,
            f"subject.txt drifted from its pinned anchor: {clean!r}")
    return lawchecks.finish(
        lawchecks.WORD_PASS,
        f"the subject holds its pinned anchor ({name})")


sys.exit(lawchecks.run(main))
'''


# A law whose verdict line IS printed and then buried under a flood: it measures
# whether "the line is not in the part that was kept" is reported as truncation
# rather than as "the law printed no line at all".
NOISY_CHECKER = '''\
"""A law that prints its verdict first and then far more than the runner keeps."""
import sys

print("law-verdict: PASS noisy-law | printed first, before the flood", flush=True)
line = ("x" * 1023) + chr(10)
for _ in range(128):
    sys.stdout.write(line)
sys.stdout.flush()
sys.exit(0)
'''

# A law that leaves a child behind on its red arm: it measures whether a killed
# arm takes its whole process TREE with it, or whether a descendant survives the
# verdict and keeps running after the run is over.
DESCENDANT_CHECKER = '''\
"""A law whose red arm spawns a child and then hangs."""
import os
import subprocess
import sys
import time
from pathlib import Path

if not os.environ.get("VODRIP_LAW_RED_INPUT"):
    print("law-verdict: PASS descendant-law | clean arm, no child spawned", flush=True)
    sys.exit(0)

# Spawn the child FIRST and publish its pid only after Popen has returned, so the
# pid in the file is always the pid of a process that really was created.
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
Path(os.environ["OD_LAWTEST_PIDFILE"]).write_text(str(child.pid), encoding="utf-8")
time.sleep(120)
sys.exit(1)
'''



# A law whose scratch must mean ONE directory to the law AND to its child.  A
# relative scratch cannot promise that: the runner, the law and the law's own
# child each resolve the same string against a different cwd, so one string
# names three directories.
REL_SCRATCH_CHECKER = '''\
"""A law that hands its scratch to a child and checks the child saw the same one."""
import os
import subprocess
import sys
from pathlib import Path

NAME = os.environ.get("VODRIP_LAW_NAME", "unnamed")
ON_RED_ARM = bool(os.environ.get("VODRIP_LAW_RED_INPUT"))
scratch = os.environ.get("VODRIP_LAW_SCRATCH", "")

if not Path(scratch).is_absolute():
    print(f"law-verdict: NO_VERDICT {NAME} | the runner handed the law a relative scratch: {scratch!r}", flush=True)
    sys.exit(2)

handoff = Path(scratch) / "handoff.txt"
handoff.parent.mkdir(parents=True, exist_ok=True)
handoff.write_text("written by the law", encoding="utf-8")

child = subprocess.run(
    [sys.executable, "-c", "from pathlib import Path; print(Path('handoff.txt').resolve())"],
    cwd=scratch, capture_output=True, text=True, encoding="utf-8", errors="replace")
seen = (child.stdout or "").strip()
if child.returncode != 0 or seen != str(handoff.resolve()):
    print(f"law-verdict: NO_VERDICT {NAME} | a child with cwd={scratch!r} saw {seen!r}, not {str(handoff.resolve())!r}", flush=True)
    sys.exit(2)

WORD = "FAIL" if ON_RED_ARM else "PASS"
print(f"law-verdict: {WORD} {NAME} | one scratch, one directory, both readers", flush=True)
sys.exit(1 if ON_RED_ARM else 0)
'''


def _pid_alive(pid: int) -> bool:
    """Ask the OS whether that pid is still running, without signalling it."""
    if os.name == "nt":
        proc = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                              capture_output=True, text=True)
        return str(pid) in (proc.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# === fixture plumbing ===================================================

def _abs(path: Path) -> str:
    """Absolute, forward-slashed: the form argv and JSON want on Windows."""
    return str(path.resolve()).replace(os.sep, "/")


FAKE_RED_DOC = {
    "mutations": [{
        "file": "subject.txt",
        "find": "RED LINE PRESENT",
        "put": "GREEN LINE PRESENT",
        "expect": 1,
    }],
}

def write_fixture(tmp_path, monkeypatch, *, checker=FAKE_CHECKER,
                  red_doc=FAKE_RED_DOC, name="fixture-law", mode=None,
                  clean_word="PASS", red_word="PASS", expect_rc=0,
                  timeout_s=60, omit_schema=False, omit_red_file=False):
    """Write a one-law registry, its checker and its red document."""
    (tmp_path / "subject.txt").write_text("RED LINE PRESENT\n", encoding="utf-8")
    checker_path = tmp_path / "checker.py"
    checker_path.write_text(checker, encoding="utf-8")
    red_path = tmp_path / "red.json"
    if red_doc is not None and not omit_red_file:
        red_path.write_text(json.dumps(red_doc), encoding="utf-8")
    law = {
        "name": name,
        "description": f"fixture law {name}",
        "cmd": ["<python>", _abs(checker_path)],
        "expect_rc": expect_rc,
        "red_input": _abs(red_path),
        "timeout_s": timeout_s,
    }
    doc = {} if omit_schema else {"schema_version": 1}
    doc["laws"] = [law]
    registry = tmp_path / "laws.json"
    registry.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(LAWS_DIR))
    if mode is not None:
        monkeypatch.setenv("OD_LAWTEST_MODE", mode)
    monkeypatch.setenv("OD_LAWTEST_CLEAN_WORD", clean_word)
    monkeypatch.setenv("OD_LAWTEST_RED_WORD", red_word)
    return registry, red_path


def run_runner(tmp_path, registry, *, timeout_s=180):
    """Run the real runner over a fixture registry; return (rc, row, folded)."""
    out = tmp_path / "report.json"
    scratch = tmp_path / "scratch"
    rc = lawrunner.main(["--laws", _abs(registry), "--json", "--out", _abs(out),
                         "--scratch", _abs(scratch), "--repo", _abs(tmp_path)])
    assert out.is_file(), "the runner promised a report and did not write one"
    report = json.loads(out.read_text(encoding="utf-8"))
    rows = report["rows"]
    assert len(rows) == 1, f"expected one law row, got {len(rows)}"
    return rc, rows[0], gate.fold_law_row(rows[0])

# === the runner's verdict tree ===========================================

FOLDS_TO = {
    "PASS": core.PASS,
    "RED": core.FAIL,
    "MISMATCH": core.FAIL,
    "NO_VERDICT": core.NOT_MEASURED,
}
EXITS = {core.PASS: lawrunner.EXIT_GREEN,
         core.FAIL: lawrunner.EXIT_RED,
         core.NOT_MEASURED: lawrunner.EXIT_NO_VERDICT}

class TestRunnerVerdictTree:
    """Every cell of the tree, measured through the real runner process."""

    @pytest.mark.parametrize(
        "clean_word,red_word,expect_rc,verdict,red_verdict",
        [
            ("PASS", "FAIL", 0, "PASS", "RED_CONFIRMED"),
            ("PASS", "PASS", 0, "MISMATCH", "RED_NOT_REPRODUCED"),
            ("FAIL", "FAIL", 0, "RED", "RED_CONFIRMED"),
            ("NO_VERDICT", "FAIL", 0, "RED", "RED_CONFIRMED"),
            ("PASS", "PASS", 2, "RED", "RED_CONFIRMED"),
            ("PASS", "NO_VERDICT", 0, "NO_VERDICT", "RED_NOT_EVALUATED"),
            ("NO_VERDICT", "NO_VERDICT", 0, "NO_VERDICT", "RED_NOT_EVALUATED"),
        ])
    def test_verdict_tree(self, tmp_path, monkeypatch, clean_word, red_word,
                        expect_rc, verdict, red_verdict):
        registry, _ = write_fixture(tmp_path, monkeypatch,
                                    clean_word=clean_word, red_word=red_word,
                                    expect_rc=expect_rc)
        rc, row, folded = run_runner(tmp_path, registry)

        assert row["verdict"] == verdict, row
        assert row["red_input_verdict"] == red_verdict, row
        # the word the runner prints and the numbers it printed with it must
        # agree, or the row is not evidence
        assert row["expect_rc"] == expect_rc
        assert row["real_rc"] == {"PASS": 0, "FAIL": 1,
                                  "NO_VERDICT": 2}[clean_word]
        assert row["red_rc"] == {"PASS": 0, "FAIL": 1,
                                "NO_VERDICT": 2}[red_word]
        assert folded["verdict"] == FOLDS_TO[verdict], folded
        assert rc == EXITS[folded["verdict"]], folded

    def test_a_red_arm_that_declined_is_never_a_pass(self, tmp_path, monkeypatch):
        """The regression that used to print PASS: red exits 2 with no verdict."""
        registry, _ = write_fixture(tmp_path, monkeypatch,
                                    clean_word="PASS", red_word="NO_VERDICT")
        rc, row, folded = run_runner(tmp_path, registry)

        assert row["verdict"] == "NO_VERDICT"
        assert row["red_input_verdict"] == "RED_NOT_EVALUATED"
        assert "red_arm_no_verdict" in (row["red_cause"] or ""), row["red_cause"]
        assert folded["verdict"] == core.NOT_MEASURED
        assert folded["reason_class"] == "no_verdict"
        assert rc == lawrunner.EXIT_NO_VERDICT

    def test_a_law_that_cannot_fail_is_a_mismatch(self, tmp_path, monkeypatch):
        """Green on both arms: unfalsifiable, therefore not evidence."""
        registry, _ = write_fixture(tmp_path, monkeypatch,
                                    clean_word="PASS", red_word="PASS")
        rc, row, folded = run_runner(tmp_path, registry)

        assert row["verdict"] == "MISMATCH"
        assert row["red_input_verdict"] == "RED_NOT_REPRODUCED"
        assert folded["verdict"] == core.FAIL
        assert folded["reason_class"] == "red_not_reproduced"
        assert "its green arm proves nothing" in folded["reason"], folded["reason"]
        assert rc == lawrunner.EXIT_RED

    def test_a_red_arm_that_printed_nothing_is_no_verdict(self, tmp_path, monkeypatch):
        """Red exits 1 having said nothing: silence is not a reproduction."""
        registry, _ = write_fixture(tmp_path, monkeypatch, mode="red_silent")
        rc, row, folded = run_runner(tmp_path, registry)

        assert row["red_word"] is None
        assert row["red_rc"] == 1
        assert row["verdict"] == "NO_VERDICT"
        assert "no_verdict_word" in (row["red_cause"] or ""), row["red_cause"]
        assert row["red_input_verdict"] == "RED_NOT_EVALUATED"
        assert folded["verdict"] == core.NOT_MEASURED
        assert rc == lawrunner.EXIT_NO_VERDICT

    def test_a_red_arm_that_said_two_things_is_no_verdict(self, tmp_path, monkeypatch):
        registry, _ = write_fixture(tmp_path, monkeypatch, mode="red_two_lines")
        rc, row, _folded = run_runner(tmp_path, registry)

        assert row["red_word"] is None
        assert row["verdict"] == "NO_VERDICT"
        assert "ambiguous_verdict_word" in (row["red_cause"] or ""), row["red_cause"]
        assert rc == lawrunner.EXIT_NO_VERDICT

    def test_a_red_word_that_contradicts_its_exit_code_is_no_verdict(
            self, tmp_path, monkeypatch):
        """Red prints FAIL and exits 0: the word is not corroborated by the code."""
        registry, _ = write_fixture(tmp_path, monkeypatch, mode="red_lying_word")
        rc, row, folded = run_runner(tmp_path, registry)

        assert row["red_word"] == "FAIL"
        assert row["red_rc"] == 0
        assert row["verdict"] == "NO_VERDICT"
        assert "verdict_word_contradicts_rc" in (row["red_cause"] or ""), row["red_cause"]
        assert row["red_input_verdict"] == "RED_NOT_EVALUATED"
        assert folded["verdict"] == core.NOT_MEASURED
        assert rc == lawrunner.EXIT_NO_VERDICT

    def test_a_red_arm_that_hung_is_no_verdict_not_a_mismatch(
            self, tmp_path, monkeypatch):
        """Clean passes and red never finishes: that is not 'the law cannot fail'."""
        registry, _ = write_fixture(tmp_path, monkeypatch, mode="red_hang",
                                    timeout_s=1)
        rc, row, folded = run_runner(tmp_path, registry)

        assert row["real_rc"] == 0
        assert row["red_rc"] is None
        assert "law_timeout" in (row["red_cause"] or ""), row["red_cause"]
        assert row["verdict"] == "NO_VERDICT"
        assert row["red_input_verdict"] == "RED_NOT_EVALUATED"
        assert folded["verdict"] == core.NOT_MEASURED
        assert rc == lawrunner.EXIT_NO_VERDICT

    def test_output_beyond_the_capture_cap_is_no_verdict_not_a_missing_line(
            self, tmp_path, monkeypatch):
        """A verdict line cut off by the cap is not 'the law printed no line'."""
        monkeypatch.setattr(lawrunner, "_CAPTURE_CAP", 2048)
        registry, _ = write_fixture(tmp_path, monkeypatch, checker=NOISY_CHECKER)
        rc, row, folded = run_runner(tmp_path, registry)

        assert row["stdout_truncated"] is True
        assert row["real_word"] is None
        assert row["verdict"] == "NO_VERDICT"
        assert "output_truncated" in (row["real_cause"] or ""), row["real_cause"]
        assert folded["verdict"] == core.NOT_MEASURED
        assert rc == lawrunner.EXIT_NO_VERDICT

    def test_the_capture_files_are_not_inside_the_arms_scratch(
            self, tmp_path, monkeypatch):
        """The law owns its scratch and may clear it; the capture lives outside it."""
        registry, _ = write_fixture(tmp_path, monkeypatch, clean_word="PASS",
                                    red_word="FAIL")
        _rc, row, _folded = run_runner(tmp_path, registry)

        assert row["verdict"] == "PASS", row
        capture = tmp_path / "scratch" / "_capture"
        for arm in ("clean", "red"):
            assert (capture / f"fixture-law.{arm}" / "_arm_stdout.txt").is_file()
            assert not list((tmp_path / "scratch" / f"fixture-law.{arm}").rglob("*"))

    def test_a_law_the_window_never_reached_is_refused_not_spawned(
            self, tmp_path, monkeypatch, capsys):
        """The window is re-checked before each law, so a closed window spawns nothing."""
        registry, _ = write_fixture(tmp_path, monkeypatch, clean_word="PASS",
                                    red_word="FAIL")
        decls, _lines, _raw = lawrunner.load_law_file(registry)
        clock = {"t": 0.0}

        def ticking() -> float:
            clock["t"] += 100.0
            return clock["t"]

        scratch = tmp_path / "s"
        rows, window = lawrunner.evaluate(decls, run_timeout_s=10.0, cwd=tmp_path,
                                          scratch=scratch, now=ticking)
        out = capsys.readouterr().out

        assert len(rows) == 1
        row = rows[0]
        assert row.real_rc is None and row.red_rc is None
        assert row.real_word is None and row.red_word is None
        assert row.verdict == "NO_VERDICT"
        assert "run_window_exhausted" in (row.real_cause or ""), row.real_cause
        assert "LAW-REFUSED fixture-law" in out
        assert window["run_window_s"] == 10.0
        # nothing was spawned: even the scratch tree the arms would have used is
        # absent, so the refusal is a refusal and not a late spawn
        assert not scratch.exists()

    def test_a_spawn_after_the_deadline_starts_nothing_at_all(self, tmp_path):
        """The window is checked at the spawn seam, not only in the caller's loop.

        Between the loop's check and the process starting, the caller reads the
        red input, builds two environments and creates three directories.  A
        check that only happens upstream is therefore not a check on the spawn,
        and a law can start after the window that was supposed to refuse it.
        """
        marker = tmp_path / "ran.txt"
        capture = tmp_path / "capture"
        rc, cause, out, err, out_cut, err_cut = lawrunner._spawn(
            [sys.executable, "-c",
             f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"],
            cwd=tmp_path, env=dict(os.environ), timeout_s=30.0,
            capture_dir=capture, deadline=0.0)

        assert rc is None, "an arm past the deadline must not have run"
        assert cause is not None and cause.startswith("window_closed"), cause
        assert not marker.exists(), "the process started despite the closed window"
        assert not capture.exists(), "a refused arm must not leave a capture dir behind"
        assert (out, err, out_cut, err_cut) == ("", "", False, False)

    def test_a_spawn_refuses_at_the_last_moment_before_the_process_exists(self, tmp_path):
        """The deadline is re-checked immediately before Popen, not only before mkdir.

        mkdir and the two capture opens take real time, so the first check is not
        a check on the spawn.  A clock that is inside the window on the first
        reading and past it on the second measures the second check: the process
        must not exist, and the marker it would have written must not be there.
        """
        marker = tmp_path / "ran.txt"
        capture = tmp_path / "capture"
        readings = iter([0.0, 100.0])

        rc, cause, out, err, out_cut, err_cut = lawrunner._spawn(
            [sys.executable, "-c",
             f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"],
            cwd=tmp_path, env=dict(os.environ), timeout_s=30.0,
            capture_dir=capture, deadline=50.0, now=lambda: next(readings))

        assert rc is None
        assert cause is not None and cause.startswith("window_closed"), cause
        # the first check passed, so the capture dir exists: that is what makes
        # this a measurement of the SECOND check rather than of the first
        assert capture.exists()
        assert not marker.exists(), "the process started after the window closed"
        assert (out, err, out_cut, err_cut) == ("", "", False, False)

    def test_a_law_that_reaches_its_spawn_after_the_deadline_is_refused(
            self, tmp_path, monkeypatch):
        """Both arms are refused, and neither arm's output file is ever created."""
        registry, _ = write_fixture(tmp_path, monkeypatch, clean_word="PASS",
                                    red_word="FAIL")
        decls, _lines, _raw = lawrunner.load_law_file(registry)
        scratch = tmp_path / "s"

        row = lawrunner.evaluate_declaration(decls[0], cwd=tmp_path,
                                             scratch_root=scratch,
                                             deadline=0.0, now=lambda: 1.0)

        assert row.real_rc is None and row.red_rc is None
        assert "window_closed" in (row.real_cause or ""), row.real_cause
        assert "window_closed" in (row.red_cause or ""), row.red_cause
        assert row.verdict == "NO_VERDICT"
        assert row.red_input_verdict == "RED_NOT_EVALUATED"
        # Nothing was spawned: the capture tree holds no arm directory and no
        # stdout file, so this is a refusal and not a late start.
        assert not list((scratch / "_capture").rglob("*"))

    def test_a_timed_out_law_takes_its_children_with_it(self, tmp_path, monkeypatch):
        """A killed arm must not leave a descendant running behind it."""
        pidfile = tmp_path / "child.pid"
        monkeypatch.setenv("OD_LAWTEST_PIDFILE", str(pidfile))
        registry, _ = write_fixture(tmp_path, monkeypatch,
                                    checker=DESCENDANT_CHECKER, timeout_s=3)
        rc, row, _folded = run_runner(tmp_path, registry)

        assert pidfile.is_file(), "the fixture never got far enough to spawn its child"
        child_pid = int(pidfile.read_text(encoding="utf-8").strip())
        assert row["red_rc"] is None
        assert row["verdict"] == "NO_VERDICT"
        assert not _pid_alive(child_pid), (
            f"descendant pid {child_pid} outlived the arm that spawned it")
        assert rc == lawrunner.EXIT_NO_VERDICT

    def test_a_report_that_cannot_be_written_is_said_not_crashed(
            self, tmp_path, monkeypatch, capsys):
        """An unwritable --out is a refusal with a reason, not a traceback."""
        registry, _ = write_fixture(tmp_path, monkeypatch, clean_word="PASS",
                                    red_word="FAIL")
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        rc = lawrunner.main(["--laws", _abs(registry), "--json",
                             "--out", _abs(blocker / "report.json"),
                             "--scratch", _abs(tmp_path / "s"),
                             "--repo", _abs(tmp_path)])
        captured = capsys.readouterr()

        assert rc == lawrunner.EXIT_NO_VERDICT
        assert "LAW-REFUSED report_not_written" in captured.out
        assert "report_unwritable" in captured.out + captured.err

    def test_a_registry_that_reads_two_ways_is_refused(self, tmp_path):
        """A duplicate key makes the declaration ambiguous, so it is refused."""
        registry = tmp_path / "laws.json"
        registry.write_text('{"schema_version": 1, "laws": [], "laws": []}',
                            encoding="utf-8")

        with pytest.raises(lawrunner.LawError) as excinfo:
            lawrunner.load_law_file(registry)

        assert "not valid JSON" in str(excinfo.value)
        assert "duplicate object key" in str(excinfo.value)

    def test_a_relative_scratch_still_means_one_directory(self, tmp_path, monkeypatch):
        """The runner may not hand a law a relative scratch.

        Measured before the fix, with a relative --scratch: the arm materialised
        its files one level deeper than the path it launched its probe with, the
        probe exited 2 on a path that did not exist, and the law reported
        NO_VERDICT for what was really the runner's own path handling.
        """
        write_fixture(tmp_path, monkeypatch, checker=REL_SCRATCH_CHECKER)
        monkeypatch.chdir(tmp_path)

        rc = lawrunner.main(["--laws", "laws.json", "--json", "--out", "report.json",
                             "--scratch", "scratch", "--repo", "."])

        report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
        row = report["rows"][0]
        assert row["verdict"] == "PASS", row["stdout_tail"] + row["stderr_tail"]
        assert row["red_input_verdict"] == "RED_CONFIRMED", row["red_cause"]
        assert rc == lawrunner.EXIT_GREEN
        # the law really wrote into the scratch the runner named, at the one
        # absolute place both readers agreed on
        assert (tmp_path / "scratch" / "fixture-law.clean" / "handoff.txt").is_file()

    def test_a_word_that_contradicts_its_exit_code_is_no_verdict(
            self, tmp_path, monkeypatch):
        registry, _ = write_fixture(tmp_path, monkeypatch, mode="lying_word")
        rc, row, folded = run_runner(tmp_path, registry)

        assert row["real_word"] == "PASS"
        assert row["real_rc"] == 1
        assert row["verdict"] == "NO_VERDICT"
        assert "verdict_word_contradicts_rc" in (row["real_cause"] or "")
        assert folded["verdict"] == core.NOT_MEASURED
        assert rc == lawrunner.EXIT_NO_VERDICT

    def test_no_verdict_line_at_all_is_no_verdict(self, tmp_path, monkeypatch):
        registry, _ = write_fixture(tmp_path, monkeypatch, mode="no_line")
        rc, row, folded = run_runner(tmp_path, registry)

        assert row["real_word"] is None
        assert row["verdict"] == "NO_VERDICT"
        assert folded["verdict"] == core.NOT_MEASURED
        assert rc == lawrunner.EXIT_NO_VERDICT

    def test_a_registry_without_schema_version_is_refused(self, tmp_path, monkeypatch, capsys):
        registry, _ = write_fixture(tmp_path, monkeypatch, omit_schema=True)
        rc = lawrunner.main(["--laws", _abs(registry), "--json",
                             "--scratch", _abs(tmp_path / "s"),
                             "--repo", _abs(tmp_path)])

        assert rc == lawrunner.EXIT_USAGE
        # a usage refusal is an error, not a report: it belongs on stderr,
        # and a stdout carrying no verdict must not look like one
        captured = capsys.readouterr()
        assert "schema_version" in captured.err, captured.err
        assert captured.out == "", captured.out

    def test_a_laws_path_that_is_not_a_file_is_refused_with_a_reason(self, tmp_path, capsys):
        """The loader names a bad --laws path; it does not leak an OSError."""
        missing = tmp_path / "no-such-registry.json"
        rc = lawrunner.main(["--laws", _abs(missing), "--json",
                             "--scratch", _abs(tmp_path / "s"),
                             "--repo", _abs(tmp_path)])
        captured = capsys.readouterr()

        assert rc == lawrunner.EXIT_USAGE
        assert "not a readable file" in captured.err, captured.err
        assert captured.out == ""

    def test_a_law_file_that_cannot_be_read_is_a_law_error_not_an_oserror(self, tmp_path):
        """A directory is the cheapest real path read_text() refuses.

        discover_law_file() rejects it first, so this drives load_law_file()
        itself: that wrap is what the runner's exit-code discipline rests on.
        """
        directory = tmp_path / "registry-dir"
        directory.mkdir()

        with pytest.raises(lawrunner.LawError) as excinfo:
            lawrunner.load_law_file(directory)

        assert "cannot read law file" in str(excinfo.value), str(excinfo.value)

    def test_an_unknown_argument_is_a_usage_refusal(self, capsys):
        rc = lawrunner.main(["--nope"])
        captured = capsys.readouterr()

        assert rc == lawrunner.EXIT_USAGE
        assert "unknown argument" in captured.err, captured.err
        assert captured.out == ""

    def test_a_missing_red_document_is_no_verdict_not_a_spawn(
            self, tmp_path, monkeypatch):
        registry, red = write_fixture(tmp_path, monkeypatch,
                                    omit_red_file=True)
        rc, row, folded = run_runner(tmp_path, registry)

        assert row["verdict"] == "NO_VERDICT"
        assert "red_input_missing" in (row["real_cause"] or "")
        # neither arm was ever spawned: the runner knows the red doc is gone
        assert row["real_rc"] is None and row["red_rc"] is None
        # neither arm was ever spawned, so the red arm has no tails at all;
        # the clean arm never ran either, and its stdout says exactly that
        assert "not evaluated" in row["stdout_tail"], row["stdout_tail"]
        assert row["red_stdout_tail"] == "" and row["red_stderr_tail"] == ""
        assert folded["verdict"] == core.NOT_MEASURED
        assert rc == lawrunner.EXIT_NO_VERDICT

    def test_a_law_that_times_out_is_no_verdict(self, tmp_path, monkeypatch):
        slow = "import time\nimport sys\ntime.sleep(30)\nsys.exit(0)\n"
        registry, _ = write_fixture(tmp_path, monkeypatch, checker=slow,
                                    timeout_s=1)
        rc, row, folded = run_runner(tmp_path, registry)

        assert row["real_rc"] is None
        assert "law_timeout" in (row["real_cause"] or "")
        assert row["verdict"] == "NO_VERDICT"
        assert folded["verdict"] == core.NOT_MEASURED
        assert rc == lawrunner.EXIT_NO_VERDICT

    def test_the_report_pins_the_argv_it_ran(self, tmp_path, monkeypatch):
        registry, _ = write_fixture(tmp_path, monkeypatch,
                                    clean_word="PASS", red_word="FAIL")
        out = tmp_path / "report.json"
        lawrunner.main(["--laws", _abs(registry), "--json", "--out", _abs(out),
                        "--scratch", _abs(tmp_path / "s"),
                        "--repo", _abs(tmp_path)])
        report = json.loads(out.read_text(encoding="utf-8"))
        row = report["rows"][0]

        # "<python>" is the checked-in token and must never reach a real argv
        assert row["cmd"][0] == sys.executable
        assert "<python>" not in row["cmd"]
        assert report["schema_version"]

# === a law that actually measures its subject ==========================

def _doc(*mutations):
    return {"mutations": [dict(m) for m in mutations]}

def _mut(find_, put_, expect=1):
    return {"file": "subject.txt", "find": find_, "put": put_, "expect": expect}

class TestHonestCheckerEndToEnd:
    """The fixture law reads lawchecks and judges the red tree for real."""

    def test_a_red_document_that_applies_makes_the_law_red(self, tmp_path, monkeypatch):
        registry, _ = write_fixture(tmp_path, monkeypatch, checker=HONEST_CHECKER,
                                    red_doc=_doc(_mut("RED LINE PRESENT", "GREEN LINE PRESENT")))
        rc, row, folded = run_runner(tmp_path, registry)

        assert row["real_word"] == "PASS"
        assert row["red_word"] == "FAIL"
        assert row["verdict"] == "PASS"
        assert row["red_input_verdict"] == "RED_CONFIRMED"
        assert folded["verdict"] == core.PASS
        assert rc == lawrunner.EXIT_GREEN

    def test_a_drifted_red_document_is_no_verdict(self, tmp_path, monkeypatch):
        drifted = _doc(_mut("RED LINE PRESENTZ", "GREEN LINE PRESENT"))
        registry, _ = write_fixture(tmp_path, monkeypatch, checker=HONEST_CHECKER, red_doc=drifted)
        rc, row, folded = run_runner(tmp_path, registry)

        assert row["real_word"] == "PASS"
        assert row["red_word"] == "NO_VERDICT"
        assert row["verdict"] == "NO_VERDICT"
        assert row["red_input_verdict"] == "RED_NOT_EVALUATED"
        assert "red_arm_no_verdict" in (row["red_cause"] or "")
        assert folded["verdict"] == core.NOT_MEASURED
        assert rc == lawrunner.EXIT_NO_VERDICT

    def test_a_drifted_count_is_no_verdict(self, tmp_path, monkeypatch):
        drifted = _doc(_mut("RED LINE PRESENT", "GREEN LINE PRESENT", 99))
        registry, _ = write_fixture(tmp_path, monkeypatch, checker=HONEST_CHECKER, red_doc=drifted)
        rc, row, folded = run_runner(tmp_path, registry)

        assert row["verdict"] == "NO_VERDICT"
        assert folded["verdict"] == core.NOT_MEASURED
        assert rc == lawrunner.EXIT_NO_VERDICT

    def test_a_red_document_that_changes_nothing_is_no_verdict(self, tmp_path, monkeypatch):
        noop = _doc(_mut("RED LINE PRESENT", "RED LINE PRESENT"))
        registry, _ = write_fixture(tmp_path, monkeypatch, checker=HONEST_CHECKER, red_doc=noop)
        rc, row, folded = run_runner(tmp_path, registry)

        assert row["verdict"] == "NO_VERDICT"
        assert folded["verdict"] == core.NOT_MEASURED
        assert rc == lawrunner.EXIT_NO_VERDICT

    def test_a_broken_red_document_is_no_verdict(self, tmp_path, monkeypatch):
        registry, red = write_fixture(tmp_path, monkeypatch, checker=HONEST_CHECKER, red_doc={"mutations": []})
        rc, row, folded = run_runner(tmp_path, registry)

        assert row["verdict"] == "NO_VERDICT"
        assert folded["verdict"] == core.NOT_MEASURED
        assert rc == lawrunner.EXIT_NO_VERDICT

    def test_the_working_tree_is_never_written(self, tmp_path, monkeypatch):
        """The red image is judged in memory; the working tree is read-only."""
        registry, _ = write_fixture(tmp_path, monkeypatch, checker=HONEST_CHECKER,
                                    red_doc=_doc(_mut("RED LINE PRESENT", "GREEN LINE PRESENT")))
        subject = tmp_path / "subject.txt"
        before = subject.read_bytes()
        stat_before = subject.stat().st_mtime_ns
        rc, row, _folded = run_runner(tmp_path, registry)
        assert row["verdict"] == "PASS", row

        assert subject.read_bytes() == before
        assert subject.stat().st_mtime_ns == stat_before
        # this law judges an in-memory image, so neither arm wrote a subject
        # next to the one it was measuring
        for arm in ("clean", "red"):
            arm_dir = tmp_path / "scratch" / f"fixture-law.{arm}"
            written = sorted(str(p) for p in arm_dir.rglob("*") if p.is_file())
            assert not written, (arm, written)


# === lawchecks, on its own =============================================

class TestApplyRedInput:
    def test_no_env_means_clean(self, monkeypatch):
        monkeypatch.delenv(lawchecks.ENV_RED_INPUT, raising=False)
        assert lawchecks.apply_red_input({"subject.txt": "A"}, Path(".")) is None

    def test_the_mutation_applies_and_the_snapshot_survives(self, tmp_path, monkeypatch):
        path = tmp_path / "red.json"
        path.write_text(json.dumps(_doc(_mut("RED LINE PRESENT", "GREEN LINE PRESENT"))), encoding="utf-8")
        monkeypatch.setenv(lawchecks.ENV_RED_INPUT, _abs(path))
        tree = {"subject.txt": "RED LINE PRESENT\n"}
        mutated = lawchecks.apply_red_input(tree, tmp_path)

        assert mutated["subject.txt"] == "GREEN LINE PRESENT\n"
        assert tree["subject.txt"] == "RED LINE PRESENT\n"

    @pytest.mark.parametrize(
        "doc,problem",
        [
            ({}, "declares no mutations"),
            ({"mutations": []}, "declares no mutations"),
            ({"mutations": {}}, "declares no mutations"),
            ([1, 2], "must be a JSON object"),
            (_doc({"file": "subject.txt", "find": "A"}), "no 'put'"),
            (_doc({"file": "subject.txt", "find": "A", "put": "B"}), "integer 'expect'"),
            (_doc(_mut("RED LINE PRESENT", "Y", expect="2")), "integer 'expect'"),
            (_doc(_mut("RED LINE PRESENT", "Y", expect=True)), "integer 'expect'"),
            (_doc(_mut("RED LINE PRESENT", "Y", expect=99)), "no longer applies"),
            (_doc(_mut("NOWHERE", "Y")), "no longer applies"),
            (_doc({"file": "nope.txt", "find": "A", "put": "B", "expect": 1}), "not in the tree"),
            (_doc({"file": "../escape.txt", "find": "A", "put": "B", "expect": 1}), "not in the tree"),
            (_doc(_mut("RED LINE PRESENT", "RED LINE PRESENT")), "replaces"),
        ])
    def test_every_refusal_is_a_no_verdict(self, tmp_path, monkeypatch, doc, problem):
        path = tmp_path / "red.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        monkeypatch.setenv(lawchecks.ENV_RED_INPUT, _abs(path))
        tree = {"subject.txt": "RED LINE PRESENT\n"}

        with pytest.raises(lawchecks.NoVerdict) as hold:
            lawchecks.apply_red_input(tree, tmp_path)
        assert problem in str(hold.value), str(hold.value)

    def test_a_document_that_is_not_json_is_no_verdict(self, tmp_path, monkeypatch):
        path = tmp_path / "red.json"
        path.write_text("not json at all", encoding="utf-8")
        monkeypatch.setenv(lawchecks.ENV_RED_INPUT, _abs(path))
        tree = {"subject.txt": "A"}

        with pytest.raises(lawchecks.NoVerdict) as hold:
            lawchecks.apply_red_input(tree, tmp_path)
        assert "unreadable" in str(hold.value)

    def test_a_document_that_is_not_a_file_is_no_verdict(self, tmp_path, monkeypatch):
        monkeypatch.setenv(lawchecks.ENV_RED_INPUT, _abs(tmp_path / "gone.json"))
        with pytest.raises(lawchecks.NoVerdict) as hold:
            lawchecks.apply_red_input({"a.txt": "A"}, tmp_path)
        assert "not found" in str(hold.value)

class TestOneVerdictLineEver:
    """The module contract is exactly one line, and each half of it is measured.

    Both failures were reachable: an unknown word printed before its lookup, so
    the traceback handler printed a second line; and a checker that returned
    without printing exited 0, which a shell reads as success.
    """

    def test_an_unknown_word_is_refused_before_anything_is_printed(self, capsys, monkeypatch):
        monkeypatch.setenv(lawchecks.ENV_NAME, "subject")
        with pytest.raises(ValueError) as hold:
            lawchecks.finish("MAYBE", "why not")

        assert "not a verdict word" in str(hold.value), str(hold.value)
        assert capsys.readouterr().out == "", "an invalid word must not reach stdout"

    def test_run_refuses_a_checker_that_returns_without_printing(self, capsys, monkeypatch):
        monkeypatch.setenv(lawchecks.ENV_NAME, "subject")
        monkeypatch.setattr(lawchecks, "_FINISHED", False)

        rc = lawchecks.run(lambda: 0)

        out = capsys.readouterr().out
        assert rc == lawchecks.EXIT_NO_VERDICT
        assert out.count("law-verdict:") == 1, out
        assert "without printing a verdict line" in out, out

    def test_a_checker_that_finishes_keeps_its_own_code_and_one_line(self, capsys, monkeypatch):
        monkeypatch.setenv(lawchecks.ENV_NAME, "subject")
        monkeypatch.setattr(lawchecks, "_FINISHED", False)

        rc = lawchecks.run(lambda: lawchecks.finish(lawchecks.WORD_PASS, "measured"))

        out = capsys.readouterr().out
        assert rc == lawchecks.EXIT_PASS
        assert out.count("law-verdict:") == 1, out

    def test_a_second_verdict_line_is_refused_not_printed(self, capsys, monkeypatch):
        monkeypatch.setenv(lawchecks.ENV_NAME, "subject")
        monkeypatch.setattr(lawchecks, "_FINISHED", False)

        first = lawchecks.finish(lawchecks.WORD_PASS, "measured")
        second = lawchecks.finish(lawchecks.WORD_FAIL, "arguing with itself")

        out = capsys.readouterr().out
        assert first == lawchecks.EXIT_PASS
        assert second == lawchecks.EXIT_NO_VERDICT
        assert out.count("law-verdict:") == 1, out
        assert lawchecks.WORD_PASS in out, out

    def test_an_exception_after_a_verdict_cannot_print_a_second_line(self, capsys, monkeypatch):
        monkeypatch.setenv(lawchecks.ENV_NAME, "subject")
        monkeypatch.setattr(lawchecks, "_FINISHED", False)

        def checker():
            lawchecks.finish(lawchecks.WORD_PASS, "measured")
            raise lawchecks.NoVerdict("and then it fell over")

        rc = lawchecks.run(checker)

        out = capsys.readouterr().out
        assert rc == lawchecks.EXIT_NO_VERDICT
        assert out.count("law-verdict:") == 1, out
        assert lawchecks.WORD_PASS in out, out

    def test_a_second_run_does_not_inherit_the_first_verdict(self, capsys, monkeypatch):
        monkeypatch.setenv(lawchecks.ENV_NAME, "subject")
        monkeypatch.setattr(lawchecks, "_FINISHED", False)

        first = lawchecks.run(lambda: lawchecks.finish(lawchecks.WORD_PASS, "measured"))
        second = lawchecks.run(lambda: 0)

        out = capsys.readouterr().out
        assert first == lawchecks.EXIT_PASS
        assert second == lawchecks.EXIT_NO_VERDICT, "a second run must not read the first run's flag"
        assert out.count("law-verdict:") == 2, out
        assert out.count("without printing a verdict line") == 1, out


class TestTheRealCheckersCanFail:
    """A guard whose failure cannot be produced is decoration, not a guard.

    What each test drives, stated exactly: the ceiling and deadline guards run
    over the real source and then over a one-line mutation of it; the concurrent
    guards run the extracted pure functions the checker applies to its inputs
    (`mirror_findings` and the sample parser), because those are where the rules
    live and running a probe to reach them would make the test a network test;
    and three ceiling guards run over small synthetic images, which is the only
    way to reach a state the real tree cannot be in (an empty union, a
    declaration with no semicolon, a table with no object literal). Every test
    names the finding it requires, so a guard that stopped firing fails here
    instead of passing quietly.
    """

    @staticmethod
    def _src() -> str:
        return (REPO_ROOT / "src" / "previewRetry.ts").read_text(encoding="utf-8")

    def test_the_ceiling_law_is_quiet_on_the_real_tree(self):
        findings, pairs = ceiling.findings_for(self._src())

        assert findings == [], findings
        assert "session 45000<= 75000" in pairs, pairs

    def test_a_stage_with_no_ceiling_in_either_table_is_a_finding(self):
        text = self._src().replace(
            "export type PreviewRetryStage = 'session' | 'playback';",
            "export type PreviewRetryStage = 'session' | 'playback' | 'warmup';", 1)
        assert "'warmup'" in text, "the union mutation did not apply"

        findings, _pairs = ceiling.findings_for(text)

        assert any("warmup" in f and "neither ceiling table" in f for f in findings), findings

    def test_the_verdict_table_check_is_quiet_on_the_real_tree(self):
        assert deadline.check_table({"src/previewRetry.ts": self._src()}) == []

    def test_a_duplicated_stage_block_in_the_verdict_table_is_a_finding(self):
        text = self._src()
        m = deadline.STAGE_BLOCK_RE.search(text)
        assert m is not None, "the table no longer has a stage block"
        mutated = text[:m.end()] + m.group(0) + text[m.end():]

        findings = deadline.check_table({"src/previewRetry.ts": mutated})

        assert any("declares stage session 2 times" in f for f in findings), findings

    def test_a_duplicated_outcome_key_in_a_stage_is_a_finding(self):
        text = self._src()
        mutated = text.replace(
            "    stalled: 'No response from server',",
            "    stalled: 'dup',\n    stalled: 'No response from server',", 1)
        assert mutated != text, "the outcome mutation did not apply"

        findings = deadline.check_table({"src/previewRetry.ts": mutated})

        assert any("declares stalled 2 times" in f for f in findings), findings

    def test_a_corrupt_sample_line_stops_the_mirror(self):
        with pytest.raises(lawchecks.NoVerdict) as hold:
            concurrent._parse_samples('{"check": "a"}\nnot json\n')

        assert "line 2" in str(hold.value), str(hold.value)
        assert concurrent._parse_samples('{"check": "a"}\n\n') == [{"check": "a"}]

    def test_a_union_with_no_readable_stage_is_a_finding(self):
        findings, _pairs = ceiling.findings_for("export type PreviewRetryStage = never;\n")

        assert any("no readable stage name" in f for f in findings), findings

    def test_a_type_declaration_with_no_semicolon_is_a_finding(self):
        findings, _pairs = ceiling.findings_for(
            "export type PreviewRetryStage = 'session' | 'playback'\n")

        assert any("never ends with a semicolon" in f for f in findings), findings

    def test_a_ceiling_table_with_no_object_literal_is_a_finding(self):
        findings, _pairs = ceiling.findings_for(
            "export const PREVIEW_FIRST_OUTPUT_CEILING_MS = 5\n")

        assert any("no object literal to read" in f for f in findings), findings

    @staticmethod
    def _offline_samples(arms=None):
        return [{
            "check": concurrent.CHECK,
            "status": None,
            "ms": None,
            "arms": arms if arms is not None else [
                {"platform": p, "status": None, "ms": None} for p in concurrent.PLATFORMS],
        }]

    def _consistent(self):
        samples = self._offline_samples()
        entry = core.summarise(concurrent.CHECK, samples, None, "law-window", 0.0)
        return entry, samples

    def test_the_mirror_is_quiet_on_a_printout_that_matches_its_samples(self):
        entry, samples = self._consistent()

        assert concurrent.mirror_findings(core, entry, samples) == []

    def test_the_mirror_refuses_a_pass_with_nothing_measured(self):
        entry, samples = self._consistent()
        entry["verdict"] = core.PASS
        entry["n_measured"] = 0

        findings = concurrent.mirror_findings(core, entry, samples)

        assert any("empty window is not a pass" in f for f in findings), findings

    def test_the_mirror_refuses_a_percentile_its_samples_do_not_carry(self):
        entry, samples = self._consistent()
        entry["p95_ms"] = 12.5

        findings = concurrent.mirror_findings(core, entry, samples)

        assert any("p95_ms" in f for f in findings), findings

    def test_the_mirror_refuses_an_arm_set_that_is_not_three_platforms(self):
        samples = self._offline_samples(arms=[
            {"platform": "youtube", "status": None, "ms": None},
            {"platform": "youtube", "status": None, "ms": None},
            {"platform": "twitch", "status": None, "ms": None},
        ])
        entry = core.summarise(concurrent.CHECK, samples, None, "law-window", 0.0)

        findings = concurrent.mirror_findings(core, entry, samples)

        assert any("attempted" in f and "kick" in f for f in findings), findings

    def test_the_mirror_refuses_an_arm_that_reached_the_endpoint_offline(self):
        samples = self._offline_samples(arms=[
            {"platform": "youtube", "status": 200, "ms": 3.0},
            {"platform": "twitch", "status": None, "ms": None},
            {"platform": "kick", "status": None, "ms": None},
        ])
        entry = core.summarise(concurrent.CHECK, samples, None, "law-window", 0.0)

        findings = concurrent.mirror_findings(core, entry, samples)

        assert any("reached the endpoint offline" in f for f in findings), findings


# === the registry that ships ============================================

def _real_laws():
    doc = json.loads(REGISTRY.read_text(encoding="utf-8"))
    return doc.get("laws") or []

def _checker_path(cmd):
    assert isinstance(cmd, list) and cmd, cmd
    assert all(isinstance(part, str) and part for part in cmd), cmd
    target = Path(cmd[1])
    if target.is_absolute():
        return target
    if cmd[1].startswith("scripts/"):
        return REPO_ROOT / cmd[1]
    return LAWS_DIR / cmd[1]

class TestTheRealRegistry:
    """The registry is itself a law subject: a broken registry is a red gate."""

    def test_the_registry_declares_itself(self):
        doc = json.loads(REGISTRY.read_text(encoding="utf-8"))
        assert doc.get("schema_version") == 1
        laws = doc.get("laws")
        assert isinstance(laws, list) and laws
        assert len(laws) <= lawrunner.MAX_LAWS
        names = [law.get("name") for law in laws]
        assert len(set(names)) == len(names)

        for law in laws:
            for key in ("name", "description", "cmd", "expect_rc", "red_input", "timeout_s"):
                assert law.get(key) not in (None, "", []), (law.get("name"), key)
            # expect_rc is what the CLEAN arm must produce, and 0 is honest:
            # a law whose green exits nonzero is not measurable by this runner.
            assert isinstance(law["expect_rc"], int)
            assert not isinstance(law["expect_rc"], bool)
            assert 0 <= law["expect_rc"] <= 255
            assert law["expect_rc"] == 0, law["name"]
            assert 0 < float(law["timeout_s"]) <= lawrunner.MAX_LAW_TIMEOUT_S
            assert str(law["cmd"][0]) == "<python>"

    def test_the_registry_cannot_silently_shrink(self):
        """The inventory is pinned by name.  A deleted law is a hole, not a
        smaller suite, and the only way to remove one is to edit this list -
        which is a decision a reviewer sees."""
        assert sorted(law["name"] for law in _real_laws()) == [
            "preview-ceiling-order-first-output-under-stage-total",
            "preview-deadline-call-sites-resolve-to-table",
            "probe-concurrent-platforms-printout-is-measured",
        ]

    def test_every_checker_exists_and_parses(self):
        for law in _real_laws():
            path = _checker_path(law["cmd"])
            assert path.is_file(), path
            assert path.suffix == ".py", path
            ast.parse(path.read_text(encoding="utf-8"))

    def test_every_red_document_still_applies(self, monkeypatch):
        """No drift.  This is the test that keeps the other laws falsifiable.

        A needle that no longer matches makes the red arm decline, and a
        red arm that declines turns its whole law NO_VERDICT - so a drifted
        red document silently un-measures the law it protects.
        """
        monkeypatch.delenv(lawchecks.ENV_SCRATCH, raising=False)
        for law in _real_laws():
            red = LAWS_DIR / law["red_input"]
            assert red.is_file(), red
            doc = json.loads(red.read_text(encoding="utf-8"))
            rels = sorted({m.get("file") for m in doc.get("mutations", [])})
            assert rels, (law["name"], "declares no mutations")
            tree = lawchecks.load_named_files(REPO_ROOT, rels)
            snapshot = {rel: tree[rel] for rel in rels}
            monkeypatch.setenv(lawchecks.ENV_RED_INPUT, _abs(red))
            mutated = lawchecks.apply_red_input(tree, REPO_ROOT)

            for rel in rels:
                # the image is a copy: the repo is never the red arm's scratch
                assert tree[rel] == snapshot[rel], (law["name"], rel)
                # and the red document really does change its subject
                assert mutated[rel] != snapshot[rel], (law["name"], rel)


# === the fold from the runner into the gate ============================

def _row(verdict="PASS", red="RED_CONFIRMED", expect_rc=0, real_rc=0,
         red_rc=1, name="fixture-law", **extra):
    row = {
        "name": name,
        "description": "a fixture law",
        "cmd": ["python", "checker.py"],
        "verdict": verdict,
        "red_input_verdict": red,
        "expect_rc": expect_rc,
        "real_rc": real_rc,
        "red_rc": red_rc,
        "real_word": "PASS" if real_rc == 0 else "FAIL",
        "red_word": "PASS" if red_rc == 0 else "FAIL",
        "real_cause": None,
        "red_cause": None,
        "red_input": "red.json",
        "stdout_tail": "",
        "stderr_tail": "",
        "red_stdout_tail": "",
        "red_stderr_tail": "",
    }
    row.update(extra)
    return row

class TestFoldLawRow:
    def test_a_real_pass_is_a_pass(self):
        folded = gate.fold_law_row(_row())

        assert folded["check"] == "law.fixture-law"
        assert folded["verdict"] == core.PASS
        assert folded["reason_class"] == "red_reproduced"
        assert "clean exited 0 as expected" in folded["reason"]
        assert folded["n"] == 2 and folded["n_measured"] == 2
        assert folded["pass_count"] == 2
        assert folded["fail_count"] == 0
        assert folded["p50_ms"] is None and folded["p95_ms"] is None

    def test_a_red_law_is_a_failure(self):
        folded = gate.fold_law_row(_row(verdict="RED", red="RED_CONFIRMED",
                                        real_rc=1, real_cause="it broke"))

        assert folded["verdict"] == core.FAIL
        assert folded["reason_class"] == "clean_arm_failed"
        assert "broke on the clean arm" in folded["reason"]
        assert folded["fail_count"] == 2

    def test_a_mismatch_is_a_failure_and_names_why(self):
        folded = gate.fold_law_row(_row(verdict="MISMATCH",
                                        red="RED_NOT_REPRODUCED", red_rc=0))

        assert folded["verdict"] == core.FAIL
        assert folded["reason_class"] == "red_not_reproduced"
        assert "cannot be made to fail" in folded["reason"]
        assert "its green arm proves nothing" in folded["reason"]

    def test_an_unknown_verdict_word_is_never_a_pass(self):
        folded = gate.fold_law_row(_row(verdict="GREEN"))

        # the fail-safe is never PASS. An unknown word is not a "no verdict"
        # either - the runner said something it cannot say - so the row has
        # to name the defect instead of reading as an unmeasured law
        assert folded["verdict"] == core.NOT_MEASURED
        assert folded["verdict"] != core.PASS
        assert folded["pass_count"] == 0
        assert folded["reason_class"] == "unknown_verdict_word"
        assert "unknown verdict word" in folded["reason"]

    def test_no_verdict_is_never_a_pass(self):
        folded = gate.fold_law_row(_row(verdict="NO_VERDICT", red="RED_NOT_EVALUATED",
                                        real_rc=2, real_cause="no verdict line"))

        assert folded["verdict"] == core.NOT_MEASURED
        assert folded["reason_class"] == "no_verdict"
        assert folded["n_measured"] == 0
        assert folded["not_measured_count"] == 2
        assert folded["pass_count"] == 0

    def test_a_pass_word_that_contradicts_its_exit_codes_fails(self):
        folded = gate.fold_law_row(_row(expect_rc=2))

        assert folded["verdict"] == core.FAIL
        assert folded["reason_class"] == "law_word_contradicts_rc"
        assert "is not a verdict" in folded["reason"]

    def test_a_pass_word_without_a_reproduced_red_arm_fails(self):
        folded = gate.fold_law_row(_row(red="RED_NOT_EVALUATED"))

        assert folded["verdict"] == core.FAIL
        assert folded["reason_class"] == "law_word_contradicts_rc"

    def test_a_law_whose_declared_green_is_a_nonzero_exit_is_not_measured(self):
        folded = gate.fold_law_row(_row(expect_rc=1, real_rc=1))

        assert folded["verdict"] == core.NOT_MEASURED
        assert folded["reason_class"] == "law_pass_not_measured"
        assert "an unearned pass is not a pass" in folded["reason"]

    def test_a_pass_word_that_contradicts_its_exit_code_is_a_failure(self):
        folded = gate.fold_law_row(_row(real_rc=2))

        # PASS printed, expect_rc 0, clean exited 2: the word and the numbers
        # disagree, and a disagreement is never a pass
        assert folded["verdict"] == core.FAIL
        assert folded["reason_class"] == "law_word_contradicts_rc"
        assert "is not a verdict" in folded["reason"]

    def test_a_row_without_a_name_is_still_folded(self):
        folded = gate.fold_law_row({"verdict": "NO_VERDICT"})

        assert folded["check"] == "law.unnamed"
        assert folded["verdict"] == core.NOT_MEASURED

    def test_the_verdict_word_map_has_exactly_four_words(self):
        assert gate.LAW_VERDICT_MAP == {
            "PASS": core.PASS,
            "RED": core.FAIL,
            "MISMATCH": core.FAIL,
            "NO_VERDICT": core.NOT_MEASURED,
        }

# === the environment an arm runs in ====================================

def _decl(**kw):
    base = {
        "name": "arm-fixture",
        "description": "a fixture law",
        "cmd": ["<python>", "checker.py"],
        "expect_rc": 0,
        "red_input": Path("red.json"),
        "timeout_s": 60.0,
    }
    base.update(kw)
    return lawrunner.LawDecl(**base)

class TestArmEnv:
    def test_the_four_coordinates_are_always_present(self):
        env = lawrunner._arm_env(_decl(), Path("C:/scratch/clean"),
                                  REPO_ROOT, red_arm=False)

        for key in lawrunner._ARM_ENV_KEYS:
            assert key in env, key
        assert env["VODRIP_LAW_NAME"] == "arm-fixture"
        assert env["VODRIP_LAW_EXPECT_RC"] == "0"
        assert env["VODRIP_LAW_SCRATCH"] == str(Path("C:/scratch/clean"))
        assert env["VODRIP_LAW_CWD"] == str(REPO_ROOT)

    def test_the_parent_environment_is_inherited_not_replaced(self, monkeypatch):
        monkeypatch.setenv("VODRIP_ARMTEST_MARKER", "1")
        monkeypatch.setenv("PATH", "a-deliberately-odd-path")

        env = lawrunner._arm_env(_decl(), Path("s"), REPO_ROOT, red_arm=False)

        assert env.get("VODRIP_ARMTEST_MARKER") == "1"
        assert env["PATH"] == "a-deliberately-odd-path"

    def test_only_the_red_arm_gets_the_red_input(self, monkeypatch):
        """A stale value left by another law must not survive into the clean arm.

        The variable is popped rather than emptied because an empty value
        reads as "an empty document was injected", which apply_red_input
        refuses out of hand - a different failure, wrongly reported.
        """
        monkeypatch.setenv(lawrunner._RED_INPUT_ENV_KEY, "a/stale/value.json")

        clean = lawrunner._arm_env(_decl(), Path("s"), REPO_ROOT, red_arm=False)
        red = lawrunner._arm_env(_decl(), Path("s"), REPO_ROOT, red_arm=True)

        assert lawrunner._RED_INPUT_ENV_KEY not in clean
        assert red[lawrunner._RED_INPUT_ENV_KEY] == str(Path("red.json"))

    def test_the_python_token_is_replaced_exactly(self):
        assert lawrunner._resolve_python_argv(["<python>", "x.py"]) == [sys.executable, "x.py"]

        # A token merely CONTAINING it is a path, not a placeholder, and a
        # silent rewrite of it would run a file nobody declared.
        untouched = ["python", "<python>.bak", "a/<python>/b"]
        assert lawrunner._resolve_python_argv(untouched) == untouched

    def test_the_public_surface_is_what_the_gate_and_this_file_use(self):
        for name in ("Row", "LawDecl", "LawError", "evaluate", "load_law_file",
                    "plan_laws", "admit_laws", "verdict_exit_code", "main",
                    "discover_law_file", "EXIT_NO_VERDICT", "MAX_LAW_TIMEOUT_S",
                    "MAX_LAWS", "ARMS_PER_LAW"):
            assert name in lawrunner.__all__, name

# === how the gate runs the suite (and how it must never fake skipping it) ===

def _args(**kw):
    """A stand-in for the gate's argparse namespace, laws step only."""
    base = {"skip_laws": False, "laws_file": None, "step_timeout_s": 300,
            "strict": False}
    base.update(kw)
    return SimpleNamespace(**base)


def _fixture_registry(tmp_path, monkeypatch, *, name="gate-fixture-law",
                      clean_word="PASS", red_word="FAIL"):
    """A one-law registry that passes both arms: the fold's green path."""
    registry, _red = write_fixture(tmp_path, monkeypatch, name=name,
                                 clean_word=clean_word, red_word=red_word)
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    return registry


class TestGateWiring:
    """run_laws is the seam where a law suite can quietly become a pass.

    Here it is measured, not read: a fixture law is actually spawned twice and
    the folded check it produced is the assertion.
    """

    def test_each_row_is_folded_into_one_check_named_after_the_law(
            self, tmp_path, monkeypatch):
        registry = _fixture_registry(tmp_path, monkeypatch)
        checks = []
        gate.run_laws(_args(laws_file=_abs(registry)), REPO_ROOT, checks)

        appended = [c for c in checks if c.name.startswith("law.")]
        assert len(appended) == 1, [c.name for c in checks]
        folded = appended[0].verdict()
        assert folded["check"] == "law.gate-fixture-law"
        # The fixture's clean arm exits 0 and its red arm reproduces, so PASS
        # - and it says red_reproduced, which is the only reason a law may be
        # green. A fold that said PASS for any other reason would fail here.
        assert folded["verdict"] == core.PASS, folded
        assert folded["reason_class"] == "red_reproduced"

    def test_the_laws_file_reaches_the_runner_itself(self, tmp_path, monkeypatch):
        registry = _fixture_registry(tmp_path, monkeypatch)
        seen = {}

        class Stub:
            def __init__(self, name, repo, report, laws_file=None, run_timeout_s=None):
                seen["laws_file"] = laws_file
                seen["repo"] = repo
                self.detail = {"rows": [lawrunner.Row(
                    name="stub-law", description="stub", cmd=["<python>"],
                    expect_rc=0, verdict="PASS",
                    red_input_verdict="RED_CONFIRMED", real_rc=0, red_rc=1,
                    real_word="PASS", red_word="FAIL").to_json()]}

            def run(self, timeout_s=None):
                pass

            def load(self):
                return self

            @property
            def usable(self):
                return True

        monkeypatch.setattr(gate, "LawsRunnerCheck", Stub)
        checks = []
        gate.run_laws(_args(laws_file=_abs(registry)), REPO_ROOT, checks)
        assert seen["laws_file"] == _abs(registry)
        assert seen["repo"] == REPO_ROOT
        assert [c.name for c in checks] == ["law.stub-law"]
        assert checks[0].verdict()["verdict"] == core.PASS

    def test_an_unusable_runner_names_the_registry_it_actually_ran(
            self, tmp_path, monkeypatch):
        registry = _fixture_registry(tmp_path, monkeypatch)
        check = gate.LawsRunnerCheck("laws", REPO_ROOT,
                                     gate.LOG_DIR / "laws.json",
                                     laws_file=_abs(registry))
        check.exit_code = 3  # the runner's usage refusal, before any spawn
        folded = check.verdict()
        assert folded["verdict"] == core.NOT_MEASURED
        assert folded["reason_class"] == "not_run"
        assert folded["n"] == 0 and folded["n_measured"] == 0
        assert _abs(registry) in folded["reason"]
        assert gate.LAWS_REGISTRY_REL not in folded["reason"], (
            "the reason named the shipped registry, not the one actually run")

    def test_skip_laws_is_a_not_measured_with_the_reason_it_claims(self, tmp_path):
        source = inspect.getsource(gate.run_laws)
        assert "args.skip_laws" in source
        assert "args.fast" not in source, (
            "-Fast must not skip the law suite: it documents itself as skipping"
            " pytest only, and a skipped law is a not_measured law")

        checks = []
        gate.run_laws(_args(skip_laws=True), tmp_path, checks)
        assert len(checks) == 1, [c.name for c in checks]
        folded = checks[0].verdict()
        assert folded["check"] == "laws"
        assert folded["verdict"] == core.NOT_MEASURED
        assert folded["reason_class"] == "skipped"
        assert "--skip-laws" in folded["reason"]
        assert folded["reason"].lower().startswith("not measured")
        assert folded["n"] == 0 and folded["n_measured"] == 0

    def test_a_folded_check_survives_the_verdict_file_contract(
            self, tmp_path, monkeypatch):
        registry = _fixture_registry(tmp_path, monkeypatch)
        checks = []
        gate.run_laws(_args(laws_file=_abs(registry)), REPO_ROOT, checks)
        doc = core.build_verdict([c.verdict() for c in checks],
                                 meta={"strict": False})
        problems = core.validate_verdict(doc)
        assert problems == [], problems
        assert doc["verdict"] == core.PASS
        assert doc["totals"][core.PASS] == 1
        assert doc["not_measured"] == [] and doc["failed"] == []


class TestTheShippedWrapper:
    """verify.ps1 is a wrapper, and a wrapper can silently drop a flag."""

    def test_the_switches_it_declares_are_the_switches_it_passes(self):
        lines = VERIFY_PS1.read_text(encoding="utf-8").splitlines()
        text = "\n".join(lines)
        assert "[switch]$Fast" in text
        assert "[switch]$SkipLaws" in text
        assert "if ($Fast)      { $gateArgs += '--fast' }" in text
        assert "if ($SkipLaws)  { $gateArgs += '--skip-laws' }" in text

    def test_fast_never_carries_the_laws_flag_with_it(self):
        lines = VERIFY_PS1.read_text(encoding="utf-8").splitlines()
        fast_lines = [l for l in lines if "$Fast" in l]
        assert fast_lines, "the wrapper must read its own -Fast switch"
        for line in fast_lines:
            assert "--skip-laws" not in line, line
            assert "SkipLaws" not in line, line

