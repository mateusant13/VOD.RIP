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
                proc = subprocess.run(self.argv, cwd=str(self.cwd), stdout=fh,
                                      stderr=subprocess.STDOUT, timeout=timeout_s,
                                      shell=False)
            self.exit_code = proc.returncode
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
    nm = repo / "node_modules"
    if not nm.exists():
        return 0, "node_modules does not exist"
    n = count_files(nm, cap=MIN_NODE_MODULE_FILES)
    return n, f"node_modules holds {n} file(s)"


def live_pytest_processes() -> list[dict]:
    """Every python process whose command line mentions pytest.

    This is the ONE-pytest-at-a-time guard. It reads the same CIM query the
    house rule names, and it treats an unreadable query as "unknown" rather than
    as "clear to run" - a guard that fails open is worse than no guard.
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
        out.append({"pid": int(parts[0]), "created": parts[1], "cmdline": parts[2].strip()})
    return out


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="verify.ps1 / verify_gate.py",
        description="VOD.RIP verification gate: live probe verdict, tsc, vitest, "
                    "probe unit tests, and the backend suite when it is safe to run.")
    p.add_argument("--repo", default=str(REPO_ROOT))
    p.add_argument("--verdict", default=str(VERDICT_FILE))
    p.add_argument("--fast", action="store_true", help="skip every pytest step")
    p.add_argument("--strict", action="store_true", help="not_measured exits 3")
    p.add_argument("--api", default="http://127.0.0.1:7897")
    p.add_argument("--health-budget-ms", type=float, default=core.DEFAULT_HEALTH_BUDGET_MS)
    p.add_argument("--preview-budget-ms", type=float, default=core.DEFAULT_PREVIEW_BUDGET_MS)
    p.add_argument("--window-minutes", type=float, default=30.0)
    p.add_argument("--skip-probe", action="store_true",
                   help="do not call the live app (offline gate); probe becomes not_measured")
    p.add_argument("--step-timeout-s", type=float, default=DEFAULT_STEP_TIMEOUT_S)
    return p


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    verdict_path = Path(args.verdict)
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
            checks.append(ExecCheck("pytest_probe_tests",
                                    [sys.executable, "-m", "pytest", "-q",
                                     str(HERE / "tests")], cwd=repo)
                          .run(args.step_timeout_s))
            # Re-check between the two suites: the first one takes minutes, and
            # a lane may have started one in the meantime.
            try:
                live2 = live_pytest_processes()
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

    # 6. roll up ----------------------------------------------------------
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
