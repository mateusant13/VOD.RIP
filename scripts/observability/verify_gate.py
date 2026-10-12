"""VOD.RIP verification runner. ONE command, ONE honest exit code.

    pwsh -NoProfile -File scripts/verify.ps1              # the gate
    pwsh -NoProfile -File scripts/verify.ps1 -Strict      # not_measured is red too
    pwsh -NoProfile -File scripts/verify.ps1 -Fast        # probe + tsc + vitest, no pytest

WHAT IT RUNS
------------
1. ``probe.py --once``        live liveness/latency verdict against real URLs
2. ``tsc --noEmit``           frontend types
3. ``vitest run``             frontend tests
4. the probe's own unit tests
5. the backend pytest suite   ONLY when no other pytest is live
6. the law suite              every declared law is run TWICE by
                               ``scripts/laws/lawrunner.py`` - once clean, once mutated
                               red - and only a law whose red arm reproduces counts
                               green

THE RULES THAT EXIST BECAUSE THIS REPO ALREADY SHIPPED FALSE GREENS
-------------------------------------------------------------------
* **Never pipe a native command when you need its exit code.** Every native
  command here is redirected to a file and re-read with ``$LASTEXITCODE`` /
  ``returncode``. ``cmd | Select-Object -First N`` masks the code and has already
  produced two false "green" results in this repo.
* **``node_modules`` can exist and be EMPTY.** A previous agent reported "884 FE
  tests passed" against an empty tree. The file count is checked before ``tsc`` or
  ``vitest`` is trusted; too few files and both become ``not_measured``.
* **ONE pytest at a time.** If another pytest is live the backend suite is
  ``not_measured`` with the reason "skipped: another pytest is live", and the
  gate does NOT fail for it. Two suites at once is the thing being prevented.
* **An unmeasured check is never a pass.** It is printed as NOT_MEASURED, it is
  listed in the verdict file, and ``-Strict`` makes it exit 3.
* **A law only counts when it can be made to fail.** The runner runs each law
  twice - clean and red - and a green arm alone proves nothing: MISMATCH (the red
  arm passed too) is a FAILED check, not a softer kind of pass.

EXIT CODES
----------
0  every check passed (not_measured may be present and is printed)
1  at least one check FAILED
3  -Strict and at least one check is not_measured
4  the runner itself could not run (usage/internal)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import probe_core as core  # noqa: E402

# Overridable so the unit tests can send their fixture logs somewhere private
# instead of mixing fake step logs (ghost.log, noisy.log) into the real gate's
# log directory, where a reader would reasonably think the gate ran them.
LOG_DIR = Path(os.environ.get("VODRIP_VERIFY_LOG_DIR")
               or (REPO_ROOT / "tmp" / "verify-logs"))
VERDICT_FILE = REPO_ROOT / "tmp" / "verify-verdict.json"

# node_modules must be materially populated before tsc/vitest are believed.
# 10850 files on this box on 2026-10-05; 500 is a wide margin below any real
# install and far above an empty or stub tree.
MIN_NODE_MODULE_FILES = 500

# A probe budget guard for the whole gate, so a wedged check cannot hang CI.
DEFAULT_STEP_TIMEOUT_S = 1800.0

# The law registry that ships, repo-relative.  The unusable-runner reason
# names the file the gate ACTUALLY asked for, because "--laws <fixture>" is a
# different set and a reason that names the shipped one would mislead.
LAWS_REGISTRY_REL = "scripts/laws/laws.vodrip.json"


def pytest_guard_decision(live: list[dict] | None, guard_error: str | None = None,
                          fast: bool = False) -> tuple[str, str, dict]:
    """Decide what the pytest steps do. Pure, so the rule is unit-testable.

    The rule: never two suites at once, never fail the gate for refusing to run
    one, and never treat "I could not check" as "clear to run" - a guard that
    fails open is worse than no guard.
    """
    detail: dict = {}
    if fast:
        return (core.NOT_MEASURED,
                "not measured: --fast skips every pytest step", detail)
    if guard_error:
        return (core.NOT_MEASURED,
                f"not measured: the one-pytest guard could not be evaluated ({guard_error}), "
                f"and a guard that cannot read must not authorise a run", detail)
    if live is None:
        return (core.NOT_MEASURED,
                "not measured: the one-pytest guard returned no answer", detail)
    if live:
        who = ", ".join(f"pid {p.get('pid')}" for p in live[:5] if p.get("pid"))
        detail["live_pytest"] = len(live)
        return (core.NOT_MEASURED,
                f"skipped: another pytest is live ({who}); one suite at a time is the rule, "
                f"and the gate does not fail for this", detail)
    return core.PASS, "guard clear: no other pytest is live", detail


class Check:
    """One gate step and its verdict. Never raises; a crash is not_measured."""

    def __init__(self, name: str, argv: list[str] | None = None, cwd: Path | None = None):
        self.name = name
        self.argv = argv or []
        self.cwd = cwd or REPO_ROOT
        self.log: Path | None = None
        self.exit_code: int | None = None
        # The pid of the child this step spawned. Kept because the pytest
        # house-rule re-check has to exclude the gate's own child by pid.
        self.pid: int | None = None
        self.detail: dict = {}

    def verdict(self) -> dict:
        raise NotImplementedError

    def run(self, timeout_s: float) -> "Check":
        """Run, redirecting ALL output to a file and reading the real code."""
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log = LOG_DIR / f"{self.name}.log"
        t0 = time.monotonic()
        try:
            with self.log.open("wb") as fh:
                # Popen, not subprocess.run: the pid has to survive the call so
                # the caller can tell its own child from a foreign pytest.
                proc = subprocess.Popen(self.argv, cwd=str(self.cwd), stdout=fh,
                                        stderr=subprocess.STDOUT, shell=False)
                self.pid = proc.pid
                try:
                    self.exit_code = proc.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    raise
        except subprocess.TimeoutExpired:
            self.exit_code = None
            self.detail["timeout_s"] = timeout_s
        except (OSError, ValueError) as exc:
            self.exit_code = None
            self.detail["spawn_error"] = f"{type(exc).__name__}: {exc}"
        self.detail["duration_s"] = round(time.monotonic() - t0, 1)
        self.detail["log"] = str(self.log) if self.log else None
        return self


class ExecCheck(Check):
    """A step whose verdict is the process exit code (0 pass, else fail)."""

    def verdict(self) -> dict:
        if self.exit_code is None:
            reason = ("timed out" if "timeout_s" in self.detail
                      else self.detail.get("spawn_error", "the command did not produce an exit code"))
            out = core.summarise(self.name, [], None, window="not run")
            out["verdict"] = core.NOT_MEASURED
            out["reason"] = f"not measured: {self.argv[0] if self.argv else self.name}: {reason}"
            out["reason_class"] = "not_run"
            out["log"] = self.detail.get("log")
            out["argv"] = self.argv
            return out

        ok = self.exit_code == 0
        seconds = self.detail.get("duration_s")
        s = {
            "check": self.name, "ts": core.utc_now_iso(), "epoch": time.time(),
            "ok": ok, "status": 200 if ok else 500, "ms": (seconds or 0.0) * 1000.0,
            "error": None if ok else f"exit {self.exit_code}",
        }
        out = core.summarise(self.name, [s], None, window="this run", nominal_interval_s=0)
        out["exit_code"] = self.exit_code
        out["log"] = self.detail.get("log")
        out["argv"] = self.argv
        out["duration_s"] = seconds
        out["reason_class"] = "exit_0" if ok else "nonzero_exit"
        out["reason"] = (f"exit 0 in {seconds}s" if ok
                         else f"FAILED: exit {self.exit_code} in {seconds}s - "
                              f"see {self.detail.get('log')}")
        return out


class CannedCheck(Check):
    """A check decided by the runner's own preconditions (a guard, not a command)."""

    def __init__(self, name: str, verdict: str, reason: str, **detail):
        super().__init__(name)
        self._verdict = verdict
        self._reason = reason
        self._detail = detail

    def verdict(self) -> dict:
        out = core.summarise(self.name, [], None, window="not run")
        out["verdict"] = self._verdict
        out["reason"] = self._reason
        out["reason_class"] = "guard"
        out.update(self._detail)
        return out


# A law that can still be MADE to fail is evidence; MISMATCH is not a softer pass.

LAW_VERDICT_MAP = {
    # The law runner speaks four verdict words; the gate speaks three. The map is
    # the whole translation, and it is deliberately lossy in one direction only:
    #   PASS       -> PASS          the law holds AND its red arm reproduces,
    #   RED        -> FAIL          the law broke on the clean arm,
    #   MISMATCH   -> FAIL          the law holds but cannot be made to fail, so
    #                             its green is not evidence,
    #   NO_VERDICT -> NOT_MEASURED  the runner declined to measure, and an
    #                             unmeasured check is never a pass,
    # An unrecognised word maps to NOT_MEASURED through .get() below. It must
    # never map to PASS: a word this file has never heard cannot be a green.
    "PASS": core.PASS,
    "RED": core.FAIL,
    "MISMATCH": core.FAIL,
    "NO_VERDICT": core.NOT_MEASURED,
}

# Keys the law runner owns. Overriding one would let the gate disagree with the
# runner about the same law, so the folded row keeps them out of the detail update.
_LAW_ROW_KEYS = {
    "name", "description", "cmd", "expect_rc", "verdict", "red_input_verdict",
    "real_rc", "red_rc", "real_word", "red_word", "real_cause", "red_cause",
    "red_input", "stdout_tail", "stderr_tail", "red_stdout_tail", "red_stderr_tail",
}

# The keys summarise always produces, so the folded law rows keep exactly the
# shape every probe row already has: one shape, one validator.
LAW_ROW_ZERO_KEYS = (
    "over_budget_count", "unreachable_count", "http_error_count", "restarts",
    "failures_truncated", "unreachable_windows",
)

_SAFE_TEXT = 400


def _clip(text) -> str:
    """Bound a log tail, so one runaway law cannot write the verdict file huge."""
    s = "" if text is None else str(text)
    return s if len(s) <= _SAFE_TEXT else s[-_SAFE_TEXT:]


def law_check_shape(name, verdict, reason, reason_class, n, n_measured, **detail):
    """One gate check, in the shape summarise produces, for a non-latency row.

    Law outcomes are booleans over two arms; they have no latency distribution
    and summarise must not be asked to invent one. So the key set is copied here
    instead: same keys, so the verdict document keeps one shape, but p50_ms and
    p95_ms stay None because nobody measured a distribution. A record that claims
    a 95th percentile it never sampled is a lie the validator would otherwise
    wave through.
    """
    sample_ms = detail.pop("duration_s", None)
    budget = detail.pop("budget_ms", None)
    out: dict = {
        "check": name,
        "verdict": verdict,
        "reason": reason,
        "reason_class": reason_class,
        "budget_ms": budget,
        "window": "this run",
        "n": n,
        "n_measured": n_measured,
        "pass_count": detail.pop("pass_count", n if verdict == core.PASS else 0),
        "fail_count": detail.pop("fail_count", n if verdict == core.FAIL else 0),
        "not_measured_count": detail.pop("not_measured_count",
                                        n if verdict == core.NOT_MEASURED else 0),
        "p50_ms": None,
        "p95_ms": None,
        "max_ms": None,
        "over_budget_count": 0,
        "unreachable_count": 0,
        "http_error_count": 0,
        "restarts": 0,
        "unreachable_windows": [],
        "failures": [],
        "failures_truncated": 0,
        "single_sample": n == 1,
        "duration_s": sample_ms,
    }
    for key in LAW_ROW_ZERO_KEYS:
        out[key] = 0
    out["unreachable_windows"] = []
    # Evidence goes last and never overrides the verdict itself.
    for key, value in detail.items():
        if key in ("check", "verdict", "reason", "reason_class", "n", "n_measured"):
            continue
        out[key] = _clip(value) if key.endswith("_tail") and isinstance(value, str) else value
    return out


def fold_law_row(row: dict) -> dict:
    """Fold one lawrunner report row into one gate check.

    The gate reuses the runner's verdict word, never re-derives it, so the two
    can never disagree about the same law. PASS requires the clean arm to have
    exited expect_rc AND the red arm to have reproduced - both are defensive: a
    row whose real_rc disagrees with a PASS word is a contradiction, and a
    contradiction is not a pass.
    """
    name = "law." + str(row.get("name") or "unnamed")
    word = str(row.get("verdict") or "")
    red_word = str(row.get("red_input_verdict") or "")
    verdict = LAW_VERDICT_MAP.get(word, core.NOT_MEASURED)
    if word not in LAW_VERDICT_MAP:
        # An unknown word is not "no verdict": the runner said something it
        # cannot say, which is a defect in the REPORT rather than a
        # measurement of the law. The fail-safe stays (never PASS), but the
        # row has to name the defect or a drifted report reads as a law that
        # was merely unmeasured.
        reason_class = "unknown_verdict_word"
    elif verdict == core.PASS:
        reason_class = "red_reproduced"
    elif word == "RED":
        reason_class = "clean_arm_failed"
    elif word == "MISMATCH":
        reason_class = "red_not_reproduced"
    else:
        reason_class = "no_verdict"

    if verdict == core.PASS:
        real_rc = row.get("real_rc")
        if real_rc != row.get("expect_rc") or red_word != "RED_CONFIRMED":
            # The word says PASS but the numbers do not. Report the numbers.
            return law_check_shape(
                name, core.FAIL,
                f"FAILED: the law runner called {name} PASS but clean exited "
                f"{real_rc} (expect_rc {row.get('expect_rc')}) and the red arm said "
                f"{red_word}; a verdict word that contradicts its own exit codes "
                f"is not a verdict",
                "law_word_contradicts_rc", 2, 2,
                law_verdict=word, red_input_verdict=red_word,
                expect_rc=row.get("expect_rc"), clean_rc=real_rc,
                red_rc=row.get("red_rc"))
        if real_rc != 0:
            # A law that printed PASS while its clean arm exited nonzero is
            # not_measured: nothing here earned the call.
            return law_check_shape(
                name, core.NOT_MEASURED,
                f"not measured: the law runner called {name} PASS while the clean "
                f"arm exited {real_rc}; an unearned pass is not a pass",
                "law_pass_not_measured", 2, 0,
                law_verdict=word, red_input_verdict=red_word,
                expect_rc=row.get("expect_rc"), clean_rc=real_rc,
                red_rc=row.get("red_rc"))
        reason = (f"law {row.get('name')} passed: clean exited {real_rc} as "
                  f"expected and the red arm reproduced ({red_word})")
    elif verdict == core.FAIL:
        if word == "RED":
            reason = (f"FAILED: the law {row.get('name')} broke on the clean "
                      f"arm: {row.get('real_cause') or 'see the log'}")
        elif word == "MISMATCH":
            reason = (f"FAILED: the law {row.get('name')} cannot be made to "
                      f"fail ({red_word}), so its green arm proves nothing: "
                      f"{row.get('red_cause') or 'see the log'}")
        else:
            # unreachable while the map above sends unknown words to
            # NOT_MEASURED; kept so that if one ever reaches here it is
            # still a loud failure rather than a pass
            reason = (f"FAILED: the law runner returned the unknown verdict word "
                      f"{word!r} for {row.get('name')}; an unknown verdict is not "
                      f"a pass")
    else:
        parts = [f"not measured: the law {row.get('name')} got no verdict"]
        if row.get("real_cause"):
            parts.append(f"clean arm: {row.get('real_cause')}")
        if row.get("red_cause"):
            parts.append(f"red arm: {row.get('red_cause')}")
        if word not in LAW_VERDICT_MAP:
            parts.append(f"(unknown verdict word {word!r})")
        reason = "; ".join(parts)

    return law_check_shape(
        name, verdict, reason, reason_class, 2,
        0 if verdict == core.NOT_MEASURED else 2,
        law_verdict=word,
        red_input_verdict=red_word,
        expect_rc=row.get("expect_rc"),
        clean_rc=row.get("real_rc"),
        red_rc=row.get("red_rc"),
        clean_word=row.get("real_word"),
        red_word=row.get("red_word"),
        red_input=row.get("red_input"),
        clean_cause=row.get("real_cause"),
        red_cause=row.get("red_cause"),
        clean_tail=_clip(row.get("stdout_tail")),
        clean_err_tail=_clip(row.get("stderr_tail")),
        red_tail=_clip(row.get("red_stdout_tail")),
        red_err_tail=_clip(row.get("red_stderr_tail")),
        law_description=row.get("description"),
        law_cmd=row.get("cmd"),
    )


class LawCheck(Check):
    """One declared law, held as the runner's row and folded on demand.

    The runner owns the verdict; this class only decides where it goes in the
    verdict document. Keeping the fold separate from the run is what stops the
    gate from re-implementing (and so re-inventing) the runner's rules.
    """

    def __init__(self, name: str, row: dict):
        super().__init__(name)
        self.row = dict(row)
        self.exit_code = 0
        self.detail = {"row": self.row}

    def verdict(self) -> dict:
        return fold_law_row(self.row)


class LawsRunnerCheck(Check):
    """Step 6: run the law runner once and fold every row it reports.

    The runner runs each law twice in per-arm scratch directories, so this is
    the one gate step whose cost is 2 x the declared law count. Its exit code
    is the runner's exit code, which is 0/1/4 for a verdict and 3 for a usage
    error the gate treats as the runner failing to run.
    """

    USABLE_EXIT_CODES = (0, 1, 4)

    def __init__(self, name, repo: Path, report: Path,
                 laws_file: str | None = None, run_timeout_s: float | None = None):
        argv = [sys.executable, str(REPO_ROOT / "scripts" / "laws" / "lawrunner.py"),
                "--repo", str(repo), "--json", "--out", str(report)]
        if laws_file:
            argv += ["--laws", str(laws_file)]
        if run_timeout_s:
            argv += ["--run-timeout", str(int(run_timeout_s))]
        super().__init__(name, argv, cwd=repo)
        self.report_path = Path(report)
        # The registry the gate actually asked for.  The unusable reason names
        # it, because "--laws R/tmp/x.json" is not the registry that ships and a
        # reason that says otherwise is a reason that misleads.
        self.laws_file = laws_file

    def load(self) -> "LawsRunnerCheck":
        """Read the report the runner wrote; tolerate a missing or broken file.

        Never raises: a broken report is evidence about the runner, not a crash
        in the gate.
        """
        doc = None
        try:
            doc = json.loads(self.report_path.read_text(encoding="utf-8",
                                                      errors="replace"))
        except (OSError, ValueError) as exc:
            self.detail["report_error"] = f"{type(exc).__name__}: {exc}"
            doc = None
        rows = doc.get("rows") if isinstance(doc, dict) else None
        self.detail["rows"] = [r for r in rows if isinstance(r, dict)]
        self.detail["exit_code"] = self.exit_code
        summary = doc.get("summary") if isinstance(doc, dict) else None
        if isinstance(summary, dict):
            self.detail["law_summary"] = summary
        self.detail["report"] = str(self.report_path)
        return self

    @property
    def usable(self) -> bool:
        """True when the runner reported a verdict about at least one law.

        Exit 3 (usage) or a missing/broken report means the runner failed to run
        at all, and the gate must not fold rows it cannot see.
        """
        if self.exit_code not in self.USABLE_EXIT_CODES:
            return False
        rows = self.detail.get("rows")
        if "report_error" in self.detail:
            return False
        if not isinstance(rows, list) or not rows:
            return False
        return all(isinstance(r.get("name"), str) and r.get("name")
                   for r in rows)

    def verdict(self) -> dict:
        """The runner-level check: usable means fold the rows, else not measured.

        This is what the gate records when the runner itself could not run. It is
        NOT_MEASURED - a law suite that never ran is not a green one - and the
        reason names the runner's exit code, because 3 and 4 mean different
        things and only one of them is the runner's own refusal.
        """
        detail = {k: self.detail.get(k) for k in (
            "report", "report_error", "law_summary")}

        return law_check_shape(
            self.name, core.NOT_MEASURED,
            f"not measured: the law runner produced no usable report (exit "
            f"{self.exit_code}); every law in {self.laws_file or LAWS_REGISTRY_REL} "
            f"was left unmeasured - see {self.detail.get('log')}",
            "not_run", 0, 0, **detail)



def run_laws(args, repo: Path, checks: list) -> None:
    """Step 6: the law suite, then a fold of every row into its own gate check.

    --fast does NOT skip this step on purpose: --fast is documented as
    "skip every pytest step", and the laws are not pytest. The whole suite
    measured 13s, which is cheap enough that skipping it would buy nothing
    and cost the only step that can catch a guard that can no longer fail.
    --skip-laws is the only skip, and it is recorded as NOT_MEASURED with a
    reason that says so, because a skipped law is not a passed law.

    The runner's rows are folded into one gate check each, which is why this
    returns nothing: it appends its checks to the caller's list the way every
    other step does.
    """
    if args.skip_laws:
        checks.append(CannedCheck(
            "laws", core.NOT_MEASURED,
            "not measured: --skip-laws was passed, so no law was evaluated "
            "and nothing about the law suite was learned",
            reason_class="skipped"))
        print("law runner                   NOT_MEASURED (--skip-laws)", flush=True)
        return

    runner = LawsRunnerCheck("laws", repo, LOG_DIR / "laws.json",
                            laws_file=args.laws_file)
    runner.run(args.step_timeout_s)
    runner.load()
    detail = dict(runner.detail)
    detail.pop("rows", None)
    runner.duration_s = round(detail.get("duration_s") or 0.0, 1)
    if runner.usable:
        folded = 0
        good = bad = unearned = 0
        for row in runner.detail["rows"]:
            folded_row = LawCheck("law." + str(row["name"]), row)
            checks.append(folded_row)
            folded += 1
            # never print PASS for a row that failed or was never earned:
            # count the FOLDED verdict, which is the one the gate reports
            folded_verdict = folded_row.verdict()["verdict"]
            if folded_verdict == core.PASS:
                good += 1
            elif folded_verdict == core.FAIL:
                bad += 1
            else:
                unearned += 1
        print(f"law runner                       {folded} law(s) folded: "
              f"{good} pass, {bad} failed, {unearned} not_measured",
              flush=True)
        return
    verdict = runner.verdict()
    verdict.setdefault("duration_s", runner.duration_s)
    verdict["log"] = str(runner.log) if runner.log else None
    checks.append(_FoldedCheck("laws", verdict))
    print("law runner                   NOT_MEASURED (no usable report)",
          flush=True)


class _FoldedCheck(Check):
    """A check that is already a verdict dict, not a command.

    The roll-up calls verdict() on every step uniformly; this wraps one already
    computed dict so the unusable-law-runner path needs no special case there.
    """

    def __init__(self, name: str, verdict_dict: dict):
        super().__init__(name)
        self._verdict_dict = dict(verdict_dict)
        self.exit_code = None
        self.detail = {}

    def verdict(self) -> dict:
        return dict(self._verdict_dict)


def force_utf8_stdio() -> None:
    """Make stdout/stderr UTF-8 and lossy, before anything is printed.

    Measured failure this prevents, 2026-10-05: a full gate run crashed with
    `UnicodeEncodeError: 'charmap' codec can't encode character '\\u276f'` while
    printing the tail of the vitest log. vitest marks failed files with U+276F,
    and this console is cp1252. The gate therefore crashed *at the moment it was
    reporting a failure* - and the exit code 1 that followed came from the crash,
    not from the gate's own verdict. A reporter that dies while reporting is
    worse than no reporter, so this runs first and `errors="replace"` guarantees
    it can never die on a stray glyph again.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def safe_text(text: str, limit: int = 4000) -> str:
    """Fold anything a console might refuse into ASCII, then bound it."""
    out = text.encode("ascii", "replace").decode("ascii")
    return out if len(out) <= limit else out[:limit] + f"... (+{len(out) - limit} chars)"


def log_tail(path: Path | None, lines: int = 20) -> str:
    if not path or not path.exists():
        return "(no log)"
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"(log unreadable: {exc})"
    return safe_text("\n".join(text[-lines:]))


# --------------------------------------------------------------------------
# preconditions
# --------------------------------------------------------------------------
def count_files(path: Path, cap: int | None = None) -> int:
    """Count files under ``path``, FOLLOWING directory junctions.

    Junctions matter here and the first version got this wrong. A git worktree's
    ``node_modules`` is a junction into the main checkout, and ``os.walk`` with
    its default ``followlinks=False`` refuses to descend into it. Measured
    consequence of that bug: PowerShell reported 10,850 files while this
    function reported 709 for the same directory on the same second - and 709
    sits uncomfortably close to the 500-file floor, so the two counters could
    disagree across a safety threshold.

    Junctions can also point at an ancestor, so every realpath is remembered
    and a directory already visited is never entered twice. That keeps the walk
    terminating on a self-referential junction instead of hanging the gate.
    """
    n = 0
    seen: set[str] = set()
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            real = os.path.realpath(current)
        except OSError:
            real = current
        key = real.lower() if os.name == "nt" else real
        if key in seen:
            continue
        seen.add(key)
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=True):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=True):
                    n += 1
            except OSError:
                continue
            if cap is not None and n >= cap:
                return n
    return n


def node_modules_state(repo: Path) -> tuple[int, str]:
    """The node_modules POPULATION, counted EXACTLY.

    No cap here, deliberately. ``count_files`` still takes a ``cap`` for callers
    that want an early exit, but this function's number is printed to the
    operator AND written into the verdict file as ``node_module_files``, so it
    is read as a total by every lane that inspects a gate run.

    Measured defect this fixes (2026-10-06, this repo): this function called
    ``count_files(nm, cap=MIN_NODE_MODULE_FILES)``, so the gate published
    ``node_modules holds 500 file(s)`` for a directory that holds 10,848 -
    a floor wearing a total's label. 500 also sits exactly ON the trust floor,
    which is the worst possible place to under-report: a reader could not
    distinguish "exactly at the threshold" from "ten thousand files over it".

    It also silently disabled the cross-check that justified the junction fix
    in ``count_files``: STATE.md records that PowerShell and this counter "now
    agree at 10,850", but a capped counter can never report more than 500 and
    so could never have agreed. The fix cost is measured, not assumed: the
    uncapped walk of 10,848 files takes ~1.05 s versus ~0.12 s capped.
    """
    nm = repo / "node_modules"
    if not nm.exists():
        return 0, "node_modules does not exist"
    n = count_files(nm)
    return n, f"node_modules holds {n} file(s)"


def live_pytest_processes(exclude_pids: set[int] | None = None) -> list[dict]:
    """Every python process whose command line mentions pytest.

    This is the ONE-pytest-at-a-time guard. It reads the same CIM query the
    house rule names, and it treats an unreadable query as "unknown" rather than
    as "clear to run" - a guard that fails open is worse than no guard.

    exclude_pids is how the gate excludes ITS OWN children: they are spawned as
    "python -m pytest -q <tests>", which carries no verify_gate.py for the
    command-line self-exclusion below to match, and CIM can still list a child
    for a moment after it exits (measured: gate run 3 reported "a pytest appeared
    after the probe tests started" and skipped the whole backend suite).
    """
    ps_cmd = [
        "powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
        "Where-Object { $_.CommandLine -match 'pytest' } | "
        "ForEach-Object { \"$($_.ProcessId)`t$($_.CreationDate)`t$($_.CommandLine)\" }",
    ]
    if shutil.which("powershell") is None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            raise RuntimeError("neither powershell nor pwsh is on PATH; cannot run the pytest guard")
        ps_cmd[0] = pwsh
    try:
        proc = subprocess.run(ps_cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"pytest guard query failed: {type(exc).__name__}: {exc}")
    out: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3 or not parts[0].strip().isdigit():
            continue
        if "verify_gate.py" in parts[2] or "verify.ps1" in parts[2]:
            continue  # never count ourselves
        pid = int(parts[0])
        if exclude_pids and pid in exclude_pids:
            continue  # our own child, by pid: not "another pytest"
        out.append({"pid": pid, "created": parts[1], "cmdline": parts[2].strip()})
    return out


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="verify.ps1 / verify_gate.py",
        description="VOD.RIP verification gate: live probe verdict, tsc, vitest, "
                    "probe unit tests, the law suite, and the backend suite when "
                    "it is safe to run. Every step reports what it measured; a "
                    "skipped step is never a passed one.")
    p.add_argument("--repo", default=str(REPO_ROOT))
    p.add_argument("--verdict", default=str(VERDICT_FILE))
    p.add_argument("--fast", action="store_true", help="skip every pytest step")
    p.add_argument("--strict", action="store_true", help="not_measured exits 3")
    p.add_argument("--api", default="http://127.0.0.1:7897")
    p.add_argument("--health-budget-ms", type=float, default=core.DEFAULT_HEALTH_BUDGET_MS)
    p.add_argument("--preview-budget-ms", type=float, default=core.DEFAULT_PREVIEW_BUDGET_MS)
    # 1 minute: this gate takes ONE fresh sample per check and grades the window,
    # so a 30-minute window folds in samples from previous runs - measured as a
    # 4282ms sample (box busy) failing a quiet run 20 minutes later, plus a
    # phantom "unreachable window" spanning the gap between the two runs. The
    # daemon keeps its 30-minute default; the gate grades what it just measured.
    p.add_argument("--window-minutes", type=float, default=1.0)
    p.add_argument("--skip-probe", action="store_true",
                   help="do not call the live app (offline gate); probe becomes not_measured")
    p.add_argument("--skip-laws", action="store_true",
                   help="do not run the law suite; laws becomes not_measured, never pass")
    p.add_argument("--laws-file", default=None,
                   help="alternate laws registry (default: scripts/laws/laws.vodrip.json)")
    p.add_argument("--step-timeout-s", type=float, default=DEFAULT_STEP_TIMEOUT_S)
    return p


def _clear_stale_verdict(verdict_path: Path) -> str | None:
    """
    Remove the previous run's verdict file. Returns None on success.

    The probe's verdict file is the authority for the probe's grade, so a file
    left behind by an earlier run is an answer this run never earned. Measured
    failure mode: the probe fails to start, or exits without writing, and the
    gate then reads a healthy document produced before the last edit and grades
    the app green off a run that no longer describes the tree. A stale file that
    cannot be removed is worse than a missing one, because the gate could not
    then tell which run the verdict describes -- so that case is refused, not
    ignored.
    """
    try:
        verdict_path.unlink()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot remove the previous verdict file {verdict_path}: {exc}"
    return None


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    verdict_path = Path(args.verdict)
    if not verdict_path.is_absolute():
        # The probe is spawned with cwd=repo, so a relative --verdict means
        # <repo>/<verdict> to the writer. The gate must clear and read the same
        # file the writer will touch, whatever directory the gate was started in.
        verdict_path = (repo / verdict_path).resolve()
    checks: list[Check] = []
    started = time.monotonic()

    print(f"VOD.RIP verification gate")
    print(f"  repo    {repo}")
    print(f"  verdict {verdict_path}")
    print(f"  strict  {args.strict}")
    print("")

    # 1. live probe, real URLs, real video id -----------------------------
    if args.skip_probe:
        checks.append(CannedCheck("probe", core.NOT_MEASURED,
                                  "not measured: --skip-probe was passed, so no live endpoint "
                                  "was called and nothing about the live app was learned",
                                  reason_class="skipped"))
        print("  [probe] skipped by flag (NOT_MEASURED, not a pass)")
    else:
        # The previous run's verdict must not survive into this one: the file,
        # not the exit code, is what the gate grades the probe from below.
        stale = _clear_stale_verdict(verdict_path)
        if stale is not None:
            checks.append(CannedCheck("probe", core.NOT_MEASURED,
                                      f"not measured: {stale}",
                                      reason_class="stale_verdict_guard"))
            print("  [probe] refused: the previous verdict file could not be removed",
                  flush=True)
        else:
            print("  [probe] calling the live app ...", flush=True)
            probe = ExecCheck("probe", [sys.executable, str(HERE / "probe.py"), "--once",
                                        "--api", args.api,
                                        "--verdict", verdict_path,
                                        "--health-budget-ms", str(args.health_budget_ms),
                                        "--preview-budget-ms", str(args.preview_budget_ms),
                                        "--window-minutes", str(args.window_minutes)],
                             cwd=repo)
            probe.run(args.step_timeout_s)
            # The probe's own verdict file is the authority, not its exit code: the
            # probe exits 0 whenever it managed to grade, which is not the same as
            # the app being healthy.
            doc = core.load_verdict(verdict_path)
            if doc is None:
                checks.append(CannedCheck("probe", core.NOT_MEASURED,
                                          f"not measured: the probe wrote no verdict file at "
                                          f"{verdict_path}", log=probe.detail.get("log")))
            else:
                for c in doc.get("checks", []):
                    sub = ExecCheck(f"probe.{c['check']}")
                    sub.detail = {"verdict_file": str(verdict_path)}
                    sub._from_doc = c  # type: ignore[attr-defined]
                    checks.append(sub)
            print(f"  [probe] wrote {verdict_path}", flush=True)

    # 2 + 3. frontend: node_modules must be materially populated ----------
    nm_files, nm_note = node_modules_state(repo)
    tsc_js = repo / "node_modules" / "typescript" / "bin" / "tsc"
    vitest_js = repo / "node_modules" / "vitest" / "vitest.mjs"
    node = shutil.which("node")

    if node is None:
        for nm in ("tsc", "vitest"):
            checks.append(CannedCheck(nm, core.NOT_MEASURED,
                                      "not measured: node is not on PATH", node_module_files=nm_files))
    elif nm_files < MIN_NODE_MODULE_FILES:
        for nm, tool in (("tsc", tsc_js), ("vitest", vitest_js)):
            checks.append(CannedCheck(nm, core.NOT_MEASURED,
                                      f"not measured: {nm_note} (need >= {MIN_NODE_MODULE_FILES}); "
                                      f"an empty node_modules has produced a false 'FE tests passed' "
                                      f"in this repo before", node_module_files=nm_files))
    else:
        print(f"  [tsc] node_modules has {nm_files} files (>= {MIN_NODE_MODULE_FILES})", flush=True)
        checks.append(ExecCheck("tsc", [node, str(tsc_js), "--noEmit"], cwd=repo)
                      .run(args.step_timeout_s))
        print("  [vitest] running ...", flush=True)
        checks.append(ExecCheck("vitest", [node, str(vitest_js), "run"], cwd=repo)
                      .run(args.step_timeout_s))

    # 4 + 5. pytest: ONE at a time, or not_measured with the reason --------
    if args.fast:
        checks.append(CannedCheck("pytest_probe_tests", core.NOT_MEASURED,
                                  "not measured: --fast skips every pytest step"))
        checks.append(CannedCheck("pytest_backend", core.NOT_MEASURED,
                                  "not measured: --fast skips every pytest step"))
    else:
        try:
            live = live_pytest_processes()
            guard_error = None
        except RuntimeError as exc:
            live, guard_error = None, str(exc)
        verdict, reason, detail = pytest_guard_decision(live, guard_error, fast=False)
        if verdict == core.NOT_MEASURED:
            for nm in ("pytest_probe_tests", "pytest_backend"):
                checks.append(CannedCheck(nm, core.NOT_MEASURED, reason, **detail))
            print(f"  [pytest] NOT MEASURED: {reason}", flush=True)
        else:
            print("  [pytest_probe_tests] running ...", flush=True)
            probe_check = ExecCheck("pytest_probe_tests",
                                    [sys.executable, "-m", "pytest", "-q",
                                     str(HERE / "tests")], cwd=repo).run(args.step_timeout_s)
            checks.append(probe_check)
            # Re-check between the two suites: the first one takes minutes, and
            # a lane may have started one in the meantime. The gate own child is
            # excluded BY PID - its command line is "python -m pytest -q <tests>",
            # so the command-line self-exclusion cannot see it, and CIM can still
            # list it right after it exits.
            own = {probe_check.pid} if probe_check.pid else None
            try:
                live2 = live_pytest_processes(exclude_pids=own)
            except RuntimeError:
                live2 = [{}]
            if live2:
                checks.append(CannedCheck("pytest_backend", core.NOT_MEASURED,
                                          "skipped: a pytest appeared after the probe tests "
                                          "started; one suite at a time",
                                          live_pytest=len(live2)))
            else:
                print("  [pytest_backend] running (this is the 2000-test suite) ...", flush=True)
                checks.append(ExecCheck("pytest_backend",
                                        [sys.executable, "-m", "pytest", "-q", "--timeout=600"],
                                        cwd=repo / "backend")
                              .run(args.step_timeout_s))

    # 6. the law suite ----------------------------------------------------
    # AFTER the pytest steps on purpose: one-pytest-at-a-time must not be
    # disturbed.  --fast does NOT skip this step: --fast means "skip every
    # pytest step" and the laws are not pytest, and they measured 13s, which
    # is cheap next to the only step that can catch a guard that can no
    # longer fail.  --skip-laws is the only skip, and it is a NOT_MEASURED.
    run_laws(args, repo, checks)

    # 7. roll up ----------------------------------------------------------
    results: list[dict] = []
    for c in checks:
        from_doc = getattr(c, "_from_doc", None)
        if from_doc is not None:
            results.append(dict(from_doc))
        else:
            results.append(c.verdict())

    doc = core.build_verdict(results, {
        "repo": str(repo),
        "gate": "scripts/verify.ps1",
        "command": " ".join([sys.executable.split("\\")[-1], "scripts/verify.ps1"] + sys.argv[1:]),
        "fast": bool(args.fast),
        "strict": bool(args.strict),
        "skip_laws": bool(getattr(args, "skip_laws", False)),
        "laws_file": getattr(args, "laws_file", None),
        "node_module_files": nm_files,
        "log_dir": str(LOG_DIR),
        "duration_s": round(time.monotonic() - started, 1),
    })
    core.write_verdict(verdict_path, doc)

    print("\n" + "=" * 78)
    for r in results:
        mark = {core.PASS: "PASS", core.FAIL: "FAIL", core.NOT_MEASURED: "NOT_MEASURED"}[r["verdict"]]
        print(f"  {mark:<14} {r['check']:<20} n={r['n']:<3} {r['reason']}")
    print("=" * 78)
    print(f"  GATE VERDICT: {doc['verdict'].upper()}   exit {doc['exit_code']}")
    if doc["not_measured"]:
        print(f"  NOT MEASURED (never a pass): {', '.join(doc['not_measured'])}")
    if doc["failed"]:
        print(f"  FAILED: {', '.join(doc['failed'])}")
        for r in results:
            if r["verdict"] == core.FAIL and r.get("log"):
                print(f"\n  --- tail of {r['log']} ---")
                print(log_tail(Path(r["log"]), 15))
    print(f"  verdict file: {verdict_path}")
    problems = core.validate_verdict(doc)
    if problems:
        print(f"\n  VERDICT CONTRACT VIOLATIONS (the gate is lying about its own output):")
        for p in problems:
            print(f"    - {p}")
        return 4
    return doc["exit_code"]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("verify_gate: interrupted", file=sys.stderr)
        sys.exit(130)
