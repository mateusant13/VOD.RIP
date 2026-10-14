"""Law/oracle runner for VOD.RIP - one row per law, never a pass-rate.

PATTERN ADOPTED, NOT INVENTED.  This is a vendored port of
consultgpt/codeintel/lawrunner.py (1065 lines, stdlib-only, no third-party
imports) re-pointed at this repository.  What changed and why:

* the env prefix is VODRIP_LAW_* instead of CODEINTEL_LAW_*;
* the exit contract gains a distinct 4th value, because this repo's gate must
  separate "the law could not be evaluated" from "the law failed" (the
  observability substrate already refuses to turn either into a pass);
* discovery skips the $HOME fallback.  A law file is a property of a checked-out
  tree; a stale one in a home directory silently winning is exactly the surprise
  the committed file exists to remove;
* cwd is the REPO ROOT, not the law file's directory;
* argv element "<python>" is substituted with sys.executable, so no PATH lookup
  can run a law under a different interpreter than the gate used;
* a law must print exactly one "law-verdict:" line, and that line is
  corroborated against the process exit code (see _LAW_VERDICT_RE).  A law that
  prints a verdict its own exit code contradicts is reporting NO_VERDICT: a tool
  that contradicts about itself is not evidence about anything.

The reason this exists at all, in this repo's own words: **a law that cannot
fail is not evidence**, and **an unmeasured check is never a pass**.  The
reference runner's version of the same rule is harsher and better: *"a pass-rate
of 0.95 is the same number whether 19 laws hold and one is broken or every law
is half-true."*  So this runner reports PASS / RED / MISMATCH / NO_VERDICT PER
LAW, and the summary never collapses to a fraction.

Four per-law outcomes, kept distinct and never collapsed:

PASS          the law's command produced expect_rc on the clean arm AND a
              DIFFERENT exit code on its red-input arm.  The law is falsifiable,
              so its green is evidence.
RED           the law's command did not produce expect_rc.  The law is broken
              right now - a named finding.
MISMATCH      the clean arm produced expect_rc and the red-input arm produced it
              too: the law holds but cannot be made to fail, so its green is not
              evidence.  This is itself a finding.
NO_VERDICT    the law could not be evaluated: malformed declaration, missing
              red-input, spawn failure, per-law timeout, run budget spent, or no
              / ambiguous verdict line.  Fail closed - a missing or malformed
              field never defaults to a pass.

EXIT CONTRACT (also in --help and docs/laws.md):

 0  every law PASSed.
 1  at least one law REDed or MISMATCHed (--strict also lands here).
 3  usage error: bad arguments, or the law file is missing / unreadable /
    unparseable / over the law cap / over its DECLARED run cost.  Nothing ran.
 4  at least one law produced NO_VERDICT, or the runner crashed.  4 is kept apart
    from 1 so "I could not measure" can never be read as "it broke".  A crash
    carries real_cause with real_rc=null; a RED carries an integer real_rc.
    Those are two different fields because an rc carries a VERDICT and never a
    CAUSE.

3 > 4 > 1 > 0.  --strict maps NO_VERDICT onto 1 for a caller that must treat an
unmeasured law as a failing one; the gate instead maps it to not_measured
(scripts/observability/verify_gate.py), which is still never a pass.

BOUNDS (all stated, all enforced):

* per-law timeout  - timeout_s per law, default DEFAULT_LAW_TIMEOUT_S (60s),
  hard-clamped to MAX_LAW_TIMEOUT_S (600s).  A law that blows it is killed WITH
  ITS PROCESS TREE (taskkill /T on Windows, the arm's own session on POSIX) and
  reports NO_VERDICT / law_timeout.
* whole-run window - run_timeout_s, default DEFAULT_RUN_TIMEOUT_S (900s),
  hard-clamped to MAX_RUN_TIMEOUT_S (3600s).  Checked BEFORE each law, never
  mid-law: a law reached after the window is spent is never started at all, and
  reports NO_VERDICT / run_window_exhausted.  A law that STARTED inside the
  window is never killed for overrunning it.
* law count        - MAX_LAWS (64).  Over the cap -> exit 3, count named, never
  silently truncated.
* declared cost    - ARMS_PER_LAW x sum(timeout_s) against the run window, BEFORE
  law 1 spawns.  A file declaring more time than the window holds is refused
  with exit 3 and the arithmetic named.  The window is never raised for the
  caller, no law is dropped.

NON-MUTATION: the runner writes nothing outside its own scratch directory and the
file named by --out.  Each arm's stdout/stderr is captured to a file in that arm's
own scratch dir, and only a bounded tail is read back into the report.  Laws are
spawned shell=False from the argv the law file declares.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

__all__ = [
    "ARMS_PER_LAW",
    "DEFAULT_LAW_TIMEOUT_S",
    "DEFAULT_RUN_TIMEOUT_S",
    "EXIT_GREEN",
    "EXIT_NO_VERDICT",
    "EXIT_RED",
    "EXIT_USAGE",
    "MAX_LAWS",
    "MAX_LAW_TIMEOUT_S",
    "MAX_RUN_TIMEOUT_S",
    "SCHEMA_VERSION",
    "VERDICT_MISMATCH",
    "VERDICT_NO_VERDICT",
    "VERDICT_PASS",
    "VERDICT_RED",
    "LawBudgetError",
    "LawDecl",
    "LawError",
    "Row",
    "admit_laws",
    "discover_law_file",
    "evaluate",
    "load_law_file",
    "main",
    "plan_laws",
    "verdict_exit_code",
]

# -- bounds ----------------------------------------------------------------
#: Bumped whenever a field changes meaning; a file declaring a different
#: version is refused (fail closed) rather than read with today's semantics.
SCHEMA_VERSION = 1
#: Hard cap on the number of laws in one file.  Over the cap -> exit 3.
MAX_LAWS = 64
DEFAULT_LAW_TIMEOUT_S = 60.0
MAX_LAW_TIMEOUT_S = 600.0
DEFAULT_RUN_TIMEOUT_S = 900.0
MAX_RUN_TIMEOUT_S = 3600.0

#: Verdicts, in the order the summary counts them.
VERDICT_PASS = "PASS"
VERDICT_RED = "RED"
VERDICT_MISMATCH = "MISMATCH"
VERDICT_NO_VERDICT = "NO_VERDICT"

#: Red-input arm verdicts.
RED_CONFIRMED = "RED_CONFIRMED"
RED_NOT_REPRODUCED = "RED_NOT_REPRODUCED"
RED_NOT_EVALUATED = "RED_NOT_EVALUATED"

#: Exit contract.  0 green, 1 red/mismatch, 3 usage, 4 no-verdict/crash.
EXIT_GREEN = 0
EXIT_RED = 1
EXIT_USAGE = 3
EXIT_NO_VERDICT = 4

#: Arms every law is charged in the declared-cost arithmetic: the clean arm
#: plus the red-input arm.  A law whose red-input is missing on disk burns no
#: arm at run time, but admission still charges it both - a file that exists at
#: admission and is gone before the red arm must be covered, and an under-count
#: here is exactly the defect admission exists to stop.
ARMS_PER_LAW = 2

#: Longest stdout/stderr KEPT IN THE REPORT per arm.  Bounded so a runaway law
#: cannot make the report unbounded.
_OUTPUT_CAP = 2000

#: Longest stdout/stderr RETAINED IN MEMORY per stream, per arm, before the capture
#: is truncated.  A law that prints more than this is not allowed to make this
#: process unbounded; the truncation is RECORDED (never swallowed), and a verdict
#: line that fell outside the retained tail is then reported as unmeasured rather
#: than as absent.
_CAPTURE_CAP = 1 << 20

#: A law prints exactly one verdict line to stdout:
#:     law-verdict: PASS <name> | <reason>
#: PASS        the law holds; the checker MUST exit 0.
#: FAIL        the law is broken; the checker MUST exit 1 - a named finding.
#: NO_VERDICT  the law could not be measured; the checker MUST exit 2.
#: The word and the exit code are corroborated: a printed word that contradicts
#: its process exit code means the law is lying about itself, which is
#: NO_VERDICT, never a pass and never a fail.  ASCII separators only, so the
#: line survives a cp1252 stdout on Windows.
_LAW_VERDICT_RE = re.compile(r"^law-verdict:\s*(PASS|FAIL|NO_VERDICT)\s+(\S+)\s+\|\s+(.*\S)\s*$")
#: Transport codes a checker may use, mapped to the transport it claims.
_RC_TO_WORD = {0: "PASS", 1: "FAIL", 2: "NO_VERDICT"}


class LawError(Exception):
    """Usage-level failure: nothing was evaluated (maps to exit 3)."""


class LawBudgetError(LawError):
    """The law set's DECLARED cost does not fit the run window (exit 3).

    Distinct from a plain LawError so a caller can tell "this file is malformed"
    from "this file is well-formed and unaffordable", while both remain
    catchable as LawError.  Nothing ran when this is raised.
    """


@dataclass(frozen=True)
class LawDecl:
    """One validated law declaration.

    Every field except description/timeout_s is REQUIRED; load_law_file raises
    LawError for a missing or malformed one.  There is no default that can turn
    a broken declaration into a pass.
    """

    name: str
    cmd: list[str]
    expect_rc: int
    red_input: Path
    description: str = ""
    timeout_s: float = DEFAULT_LAW_TIMEOUT_S

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "cmd": list(self.cmd),
            "expect_rc": self.expect_rc,
            "red_input": str(self.red_input),
            "description": self.description,
            "timeout_s": self.timeout_s,
        }


@dataclass
class Row:
    """One row per law.  verdict is derived, never assigned."""

    name: str
    description: str
    cmd: list[str]
    expect_rc: int
    verdict: str
    red_input_verdict: str
    # The CLEAN arm: what the law produced when nothing was injected.
    real_rc: int | None = None
    # The RED arm: what the law produced when handed its red-input document.
    red_rc: int | None = None
    #: A CAUSE, never an rc.  None when the run was clean.  Kept separate from
    #: real_rc so a crash can never be read as a RED.
    real_cause: str | None = None
    red_cause: str | None = None
    #: The word the law printed about itself.  None means it printed none.
    real_word: str | None = None
    red_word: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    #: The red arm's tails.  Carried as fields, not scratch attributes, so the
    #: report can show WHY a red arm failed to reproduce - not just that it did.
    red_stdout_tail: str = ""
    red_stderr_tail: str = ""
    #: True when that stream hit _CAPTURE_CAP and only its tail was retained.  A
    #: truncated stream can hide the verdict line, so the flag travels into the
    #: report and into _verdict_word's refusal text instead of being swallowed.
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    red_stdout_truncated: bool = False
    red_stderr_truncated: bool = False
    red_input: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.name if not self.description else self.description,
            "cmd": list(self.cmd),
            "expect_rc": self.expect_rc,
            "verdict": self.verdict,
            "red_input_verdict": self.red_input_verdict,
            "real_rc": self.real_rc,
            "red_rc": self.red_rc,
            "real_word": self.real_word,
            "red_word": self.red_word,
            "real_cause": self.real_cause,
            "red_cause": self.red_cause,
            "red_input": self.red_input,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "red_stdout_tail": self.red_stdout_tail,
            "red_stderr_tail": self.red_stderr_tail,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "red_stdout_truncated": self.red_stdout_truncated,
            "red_stderr_truncated": self.red_stderr_truncated,
        }


# -- discovery --------------------------------------------------------------


#: Where a law file is looked for, in order.  $HOME is deliberately NOT in this
#: list: see the module docstring.
DISCOVERY_ORDER = ("scripts/laws/laws.vodrip.json",)


def discover_law_file(
    explicit: str | os.PathLike[str] | None = None,
    *,
    repo: Path | None = None,
) -> Path:
    """Return the law file, or raise LawError naming every place tried."""
    if explicit is not None:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise LawError(f"--laws path is not a readable file: {p}")
        return p.resolve()

    tried: list[str] = []
    env = os.environ.get("VODRIP_LAWS_FILE")
    if env:
        p = Path(env).expanduser()
        tried.append(f"$VODRIP_LAWS_FILE={p}")
        if p.is_file():
            return p.resolve()

    root = Path(repo) if repo is not None else Path.cwd()
    for rel in DISCOVERY_ORDER:
        cand = root / rel
        tried.append(str(cand))
        if cand.is_file():
            return cand.resolve()

    raise LawError(
        "no law file found; tried: " + ", ".join(tried) + " (--laws to name one)"
    )


# -- loader (fail closed) ---------------------------------------------------


def _require(raw: dict[str, Any], key: str, where: str) -> Any:
    if key not in raw:
        raise LawError(f"{where}: missing required field {key!r} (no default; fail closed)")
    return raw[key]


def _parse_decl(raw: Any, index: int, base_dir: Path) -> LawDecl:
    where = f"laws[{index}]"
    if not isinstance(raw, dict):
        raise LawError(f"{where}: must be an object, got {type(raw).__name__}")

    name = _require(raw, "name", where)
    if not isinstance(name, str) or not name.strip():
        raise LawError(f"{where}: 'name' must be a non-empty string")
    where = f"laws[{index}] ({name})"

    cmd = _require(raw, "cmd", where)
    if isinstance(cmd, str):
        # A bare string would require a shell; refuse it rather than guess a
        # split.  argv is explicit.
        raise LawError(f"{where}: 'cmd' must be a list of argv strings, not a string")
    if not isinstance(cmd, list) or not cmd:
        raise LawError(f"{where}: 'cmd' must be a non-empty list of strings")
    if not all(isinstance(part, str) and part for part in cmd):
        raise LawError(f"{where}: every 'cmd' element must be a non-empty string")

    expect_rc = _require(raw, "expect_rc", where)
    if isinstance(expect_rc, bool) or not isinstance(expect_rc, int):
        raise LawError(f"{where}: 'expect_rc' must be an integer, got {type(expect_rc).__name__}")
    if not 0 <= expect_rc <= 255:
        raise LawError(f"{where}: 'expect_rc' must be in 0..255, got {expect_rc}")

    red_raw = _require(raw, "red_input", where)
    if not isinstance(red_raw, str) or not red_raw.strip():
        raise LawError(f"{where}: 'red_input' must be a non-empty path string")
    red_path = Path(red_raw).expanduser()
    if not red_path.is_absolute():
        red_path = (base_dir / red_path).resolve()

    timeout_s = raw.get("timeout_s", DEFAULT_LAW_TIMEOUT_S)
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise LawError(f"{where}: 'timeout_s' must be a number, got {type(timeout_s).__name__}")
    timeout_s = float(timeout_s)
    if timeout_s <= 0:
        raise LawError(f"{where}: 'timeout_s' must be > 0, got {timeout_s}")
    timeout_s = min(timeout_s, MAX_LAW_TIMEOUT_S)

    desc = raw.get("description", "")
    if desc is None:
        desc = ""
    if not isinstance(desc, str):
        raise LawError(f"{where}: 'description' must be a string when present")

    return LawDecl(
        name=name.strip(),
        cmd=list(cmd),
        expect_rc=expect_rc,
        red_input=red_path,
        description=desc.strip(),
        timeout_s=timeout_s,
    )


class _DuplicateKey(ValueError):
    """Raised when a law file repeats an object key.  Subclasses ValueError so
    the loader refuses it the same way it refuses any other malformed JSON."""


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """json object_pairs_hook: a law file must have ONE reading per key.

    Without this, a duplicate key is silently resolved to whichever value
    the decoder keeps last, so a declaration that disagrees with itself
    (two expect_rc values, two cmd values) still loads.  That is a
    declaration that cannot fail, which this module refuses."""
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise _DuplicateKey(f"duplicate object key {key!r}")
        seen.add(key)
    return dict(pairs)


def load_law_file(path: Path) -> tuple[list[LawDecl], int, dict[str, Any]]:
    """Load and validate.  Returns (declarations, line_count, raw).

    Raises LawError (exit 3) on: unreadable file, bad JSON, wrong
    schema_version, zero laws, over MAX_LAWS, a missing/malformed field, or a
    duplicate law name.  Nothing partial is ever returned.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LawError(f"cannot read law file {path}: {exc}") from exc

    line_count = text.count("\n") + (0 if text.endswith("\n") or not text else 1)

    try:
        raw = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except (json.JSONDecodeError, _DuplicateKey) as exc:
        raise LawError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise LawError(f"{path}: top level must be an object")

    version = _require(raw, "schema_version", str(path))
    if version != SCHEMA_VERSION:
        raise LawError(
            f"{path}: schema_version {version!r} unsupported; this runner speaks {SCHEMA_VERSION}"
        )

    laws = _require(raw, "laws", str(path))
    if not isinstance(laws, list):
        raise LawError(f"{path}: 'laws' must be a list")
    if not laws:
        raise LawError(f"{path}: 'laws' is empty; there is nothing to enforce")
    if len(laws) > MAX_LAWS:
        raise LawError(f"{path}: declares {len(laws)} laws, over the MAX_LAWS cap of {MAX_LAWS}")

    base_dir = path.parent
    decls: list[LawDecl] = []
    seen: set[str] = set()
    for i, entry in enumerate(laws):
        decl = _parse_decl(entry, i, base_dir)
        if decl.name in seen:
            raise LawError(f"{path}: duplicate law name {decl.name!r}; a row per name must be unique")
        seen.add(decl.name)
        decls.append(decl)
    return decls, line_count, raw


# -- admission (declared cost, checked before anything spawns) ---------------


def plan_laws(decls: Sequence[LawDecl], *, run_timeout_s: float) -> dict[str, Any]:
    """What this law set DECLARES it will cost, and whether that fits the window.

    Pure arithmetic: no spawn, no filesystem, no clock.  Safe as a pre-flight,
    and safe to unit-test without running a single oracle.

    The budget is the declared worst case, ARMS_PER_LAW x sum(timeout_s), from
    the per-law timeouts AFTER the MAX_LAW_TIMEOUT_S clamp the loader already
    applied - i.e. the timeouts the run will really use.  The sum is published
    together with its POPULATION (the per-law list) and its WINDOW, because a
    total without them is not a measurement.
    """
    window_s = max(1.0, min(float(run_timeout_s), MAX_RUN_TIMEOUT_S))
    timeouts = [float(d.timeout_s) for d in decls]
    one_arm_s = sum(timeouts)
    arms = ARMS_PER_LAW * len(decls)
    declared_budget_s = one_arm_s * ARMS_PER_LAW
    shortfall_s = declared_budget_s - window_s
    fits = shortfall_s <= 0
    plan: dict[str, Any] = {
        "admitted": fits,
        "fits": fits,
        "verdict": "ADMITTED" if fits else "REFUSED_DECLARED_COST",
        "laws": len(decls),
        "arms_per_law": ARMS_PER_LAW,
        "arms": arms,
        "law_timeouts_s": timeouts,
        "one_arm_declared_s": round(one_arm_s, 3),
        "declared_budget_s": round(declared_budget_s, 3),
        "run_window_s": window_s,
        "run_window_requested_s": float(run_timeout_s),
        "run_window_hard_max_s": MAX_RUN_TIMEOUT_S,
        "shortfall_s": round(max(0.0, shortfall_s), 3),
        "fits_at_hard_max": declared_budget_s <= MAX_RUN_TIMEOUT_S,
        "ratio_declared_over_window": round(declared_budget_s / window_s, 3),
        "refusal": None,
    }
    if not fits:
        plan["refusal"] = _budget_refusal_text(plan)
    return plan


def _budget_refusal_text(plan: dict[str, Any]) -> str:
    """The refusal, naming the arithmetic that failed.  Never a bare "too slow"."""
    tosum = " + ".join(f"{t:g}" for t in plan["law_timeouts_s"]) or "0"
    lines = [
        "run admission REFUSED: the declared cost of this law file does not fit the "
        "run window, so NOTHING ran.",
        f"  population          = {plan['laws']} laws, {plan['arms_per_law']} arms each "
        f"= {plan['arms']} arms",
        f"  declared per-law timeout_s ({plan['laws']} values) = {tosum} "
        f"= {plan['one_arm_declared_s']:g}s (one arm)",
        f"  arithmetic          = {plan['one_arm_declared_s']:g}s x "
        f"{plan['arms_per_law']} arms = {plan['declared_budget_s']:g}s declared budget",
        f"  run window          = {plan['run_window_s']:g}s (requested "
        f"{plan['run_window_requested_s']:g}s, hard clamp "
        f"{plan['run_window_hard_max_s']:g}s)",
        f"  FAILED              = {plan['declared_budget_s']:g}s declared > "
        f"{plan['run_window_s']:g}s window: short by {plan['shortfall_s']:g}s "
        f"({plan['ratio_declared_over_window']:g}x the window)",
    ]
    if not plan["fits_at_hard_max"]:
        lines.append(
            f"  also over the clamp  = {plan['declared_budget_s']:g}s > "
            f"{plan['run_window_hard_max_s']:g}s hard max: this file cannot fit the clamp "
            "as declared either, so raising --run-timeout alone cannot admit it"
        )
    lines.append(
        "  remedy (NEVER taken automatically) = shard the law file into sets that each "
        f"declare < {plan['run_window_hard_max_s']:g}s, or lower the per-law timeout_s "
        "declarations.  The runner does not raise the window, drop laws, or truncate."
    )
    return "\n".join(lines)


def admit_laws(decls: Sequence[LawDecl], *, run_timeout_s: float) -> dict[str, Any]:
    """Return the admission plan, or raise LawBudgetError naming the shortfall.

    This is the cost-axis twin of the MAX_LAWS refusal: a file declaring too many
    laws is refused up front with exit 3 and the count named, and a file
    declaring too much TIME is refused the same way, before law 1 runs.
    """
    plan = plan_laws(decls, run_timeout_s=run_timeout_s)
    if not plan["admitted"]:
        raise LawBudgetError(plan["refusal"])
    return plan



# -- arm environment --------------------------------------------------------

#: Variables every arm receives.  A law is handed its own identity, its expected
#: exit code, a scratch dir and the repo root, so no law has to guess, hardcode
#: an absolute path, or reach outside its own inputs.
_ARM_ENV_KEYS = ("VODRIP_LAW_NAME", "VODRIP_LAW_EXPECT_RC", "VODRIP_LAW_SCRATCH", "VODRIP_LAW_CWD")
#: The red-input variable is present ONLY on the red arm.  A checker reading it
#: on the clean arm must treat it as absent, never as "nothing was injected"
#: - which is why it is popped, not emptied: an empty value would be
#: indistinguishable from "injected an empty document".
_RED_INPUT_ENV_KEY = "VODRIP_LAW_RED_INPUT"


def _resolve_python_argv(cmd: Sequence[str]) -> list[str]:
    """Substitute the literal argv element "<python>" with sys.executable.

    A checked-in law file cannot know which interpreter will run it, and a
    "python" on PATH can be a different install (or none at all, which is how
    consultgpt's runner died with "Could not determine home directory" under a
    bare execSync).  The token is checked exactly, so an argv element merely
    CONTAINING "<python>" is left alone.
    """
    return [sys.executable if part == "<python>" else part for part in cmd]


def _arm_env(decl: LawDecl, scratch: Path, cwd: Path, *, red_arm: bool) -> dict[str, str]:
    """The environment for one arm: inherited, plus the law's own coordinates.

    PATH is inherited (a checker may need it); every variable the runner itself
    reads is set explicitly here so an arm cannot inherit a stale one from the
    caller's shell and shift a verdict.
    """
    env = dict(os.environ)
    env["VODRIP_LAW_NAME"] = decl.name
    env["VODRIP_LAW_EXPECT_RC"] = str(decl.expect_rc)
    env["VODRIP_LAW_SCRATCH"] = str(scratch)
    env["VODRIP_LAW_CWD"] = str(cwd)
    if red_arm:
        env[_RED_INPUT_ENV_KEY] = str(decl.red_input)
    else:
        env.pop(_RED_INPUT_ENV_KEY, None)
    return env


def _read_tail(path: Path, cap: int | None = None) -> tuple[str, bool]:
    """Read back the TAIL of a captured stream.  Returns (text, truncated).

    Only the last cap bytes are read, so a law that prints a gigabyte costs the
    runner a megabyte.  When the stream was cut, the first (partial) line of the
    tail is dropped: a half-line must never be read as if the law had printed it.

    cap defaults to the module constant at CALL time, not at def time, so a test
    can shrink the cap and measure the truncation path without printing megabytes.
    """
    if cap is None:
        cap = _CAPTURE_CAP
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > cap:
                fh.seek(size - cap)
                raw = fh.read()
                nl = raw.find(b"\n")
                if nl >= 0:
                    raw = raw[nl + 1:]
                truncated = True
            else:
                raw = fh.read()
                truncated = False
    except OSError:
        return "", False
    return raw.decode("utf-8", errors="replace"), truncated


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the arm AND its descendants.

    subprocess.run(timeout=...) kills only the DIRECT child.  A checker that
    spawns a worker would leave that worker running past the law: the runner
    would report a timeout while the law's own processes kept writing, and the
    next law would race them.  On Windows taskkill /T is what reaches the tree;
    on POSIX the arm is started in its own session, so its whole group can be
    signalled.  Every failure here is swallowed on purpose - this runs on the
    timeout path, and a containment failure must not replace the timeout cause
    the caller is told about.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), getattr(signal, "SIGKILL", 9))
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _spawn(
    cmd: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
    capture_dir: Path,
    deadline: float | None = None,
    now: Callable[[], float] = time.monotonic,
) -> tuple[int | None, str | None, str, str, bool, bool]:
    """Run one arm.  Returns (rc, cause, stdout, stderr, out_cut, err_cut).

    rc is None only when cause is set.  These are the causes, and each one is a
    NO_VERDICT rather than a RED, because none of them is an observation about
    the LAW: the process never ran, or never finished.

    window_closed: the run window had already closed, so this arm was never
                   started.  The check is HERE, immediately before the capture
                   directory is created and the process is spawned, and not only
                   in the caller's loop: between the loop's check and this spawn
                   the caller reads the red input, builds two environments and
                   creates three directories, so a law that arrived just inside
                   the window could still be started outside it.  The window is
                   then decorative for exactly the laws it exists to refuse, and
                   a refusal the runner never measured is not a refusal.

    law_timeout:   killed after Ns, together with its process tree.  The law
                   timed out.
    spawn_failed:  the executable does not exist.
    spawn_denied:  it exists but is not executable by this user.
    spawn_os_error: any other OS-level failure to start it.

    Output is redirected to files inside a runner-private capture directory and
    only a bounded tail is read back, so a law that prints without end cannot make
    the runner's memory unbounded.  The capture directory is deliberately NOT the
    arm's scratch directory: the law owns that one and may clear it, and it must
    not be able to delete the file the runner is capturing its output into.  The
    cut flags are RETURNED, never swallowed: the caller must be able to tell "the
    law printed no verdict line" from "the law printed more than was retained".
    """
    if deadline is not None and now() >= deadline:
        # Refused BEFORE any side effect: no capture directory, no process.  An
        # arm that never started must leave nothing behind that the other arm,
        # or the next run, could read as its own output.
        return (None, "window_closed: the run window closed before this arm started",
                "", "", False, False)
    try:
        capture_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"spawn_os_error: capture dir uncreatable: {exc}", "", "", False, False
    out_path = capture_dir / "_arm_stdout.txt"
    err_path = capture_dir / "_arm_stderr.txt"
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        with open(out_path, "wb") as out_fh, open(err_path, "wb") as err_fh:
            # Re-checked HERE, immediately before the process is created, and not
            # only before the capture directory: mkdir and the two opens take real
            # time, so a check that ran before them is not a check on this spawn.
            # No userspace check can be atomic with process creation -- the OS is
            # free to be slower than any check -- so the race is narrowed to the
            # few instructions between this line and Popen and nothing wider, and
            # that is the strongest statement this code can honestly make.
            if deadline is not None and now() >= deadline:
                return (None, "window_closed: the run window closed before this arm started",
                        "", "", False, False)
            proc = subprocess.Popen(
                list(cmd),
                cwd=str(cwd),
                env=env,
                shell=False,
                stdout=out_fh,
                stderr=err_fh,
                **kwargs,
            )
            try:
                rc: int | None = proc.wait(timeout=timeout_s)
                cause: str | None = None
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
                rc = None
                cause = f"law_timeout: killed after {timeout_s:g}s"
    except FileNotFoundError:
        return None, "spawn_failed: executable not found", "", "", False, False
    except PermissionError:
        return None, "spawn_denied: not executable by this user", "", "", False, False
    except OSError as exc:
        return None, f"spawn_os_error: {exc}", "", "", False, False

    out, out_cut = _read_tail(out_path)
    err, err_cut = _read_tail(err_path)
    return rc, cause, out, err, out_cut, err_cut


def _verdict_word(text: str, *, truncated: bool = False) -> tuple[str | None, str]:
    """Parse the one verdict line a law must print.

    Returns (word, problem).  Zero lines -> (None, problem); more than one ->
    (None, problem); exactly one -> (word, "").  The word is what the law
    CLAIMS; corroboration against its exit code happens in the caller, so a law
    that claims PASS and exits 1 is reported as lying rather than as failing.

    A truncated stream is NOT the same observation as a short one: the verdict
    line may have been printed and then cut away.  The refusal text says which
    of the two happened, because "the law printed no verdict line" would be a
    claim the runner cannot make about bytes it never read.
    """
    hits: list[str] = []
    for line in (text or "").splitlines():
        m = _LAW_VERDICT_RE.match(line.strip())
        if m:
            hits.append(m.group(1))
    if not hits:
        if truncated:
            return None, (
                "output_truncated: the law printed more than the runner retained, "
                "and no law-verdict: line survived in the retained tail"
            )
        return None, "no_verdict_word: the law printed no law-verdict: line"
    if len(hits) > 1:
        return None, "ambiguous_verdict_word: the law printed " + str(len(hits)) + " law-verdict: lines"
    return hits[0], ""


def _word_for_rc(rc: int | None) -> str | None:
    return _RC_TO_WORD.get(rc)


def _cap(text: str) -> str:
    return (text or "")[-_OUTPUT_CAP:]


# -- per-law evaluation -----------------------------------------------------


def evaluate_declaration(
    decl: LawDecl,
    *,
    cwd: Path,
    scratch_root: Path,
    deadline: float | None = None,
    now: Callable[[], float] = time.monotonic,
) -> Row:
    """Evaluate one law across both arms and derive its verdict.

    The red-input document is READ here, and the read failure is a NO_VERDICT
    rather than a RED: a red input that cannot be read means the law cannot be
    made to fail, and "the law holds" is then unearned, not broken.

    `deadline` is the absolute end of the run window on the same clock as
    `now`, which defaults to the monotonic clock evaluate() uses.  Both arms
    refuse to start once it has passed; see _spawn's window_closed.
    """
    row = Row(
        name=decl.name,
        description=decl.description,
        cmd=_resolve_python_argv(decl.cmd),
        expect_rc=decl.expect_rc,
        verdict=VERDICT_NO_VERDICT,
        red_input_verdict=RED_NOT_EVALUATED,
        red_input=str(decl.red_input),
    )

    if not decl.red_input.is_file():
        row.real_cause = f"red_input_missing: {decl.red_input}"
        row.stdout_tail = "law not evaluated: its red-input arm is absent"
        return row
    try:
        row.red_input = decl.red_input.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        row.real_cause = f"red_input_unreadable: {exc}"
        return row

    # Two independent scratch dirs, one per arm, so a checker that writes a
    # report into its scratch cannot have it read back by the other arm.
    clean_scratch = scratch_root / f"{decl.name}.clean"
    red_scratch = scratch_root / f"{decl.name}.red"
    # Runner-private, one per arm: see _spawn's docstring for why this is not the
    # arm's own scratch directory.
    capture_root = scratch_root / "_capture"
    for path in (clean_scratch, red_scratch, capture_root):
        path.mkdir(parents=True, exist_ok=True)

    env_clean = _arm_env(decl, clean_scratch, cwd, red_arm=False)
    env_red = _arm_env(decl, red_scratch, cwd, red_arm=True)

    # Clean arm.  Capture is bounded and its cut flags are carried, because a
    # truncated stream can hide the verdict line: "no verdict line" and "no
    # verdict line in the part that was retained" are different observations.
    clean_rc, clean_cause, clean_out, clean_err, clean_out_cut, clean_err_cut = _spawn(
        row.cmd, cwd=cwd, env=env_clean, timeout_s=decl.timeout_s,
        capture_dir=capture_root / f"{decl.name}.clean",
        deadline=deadline, now=now,
    )
    row.real_rc = clean_rc
    row.real_cause = clean_cause
    row.stdout_tail = _cap(clean_out)
    row.stderr_tail = _cap(clean_err)
    row.stdout_truncated = clean_out_cut
    row.stderr_truncated = clean_err_cut

    # Red arm.
    red_rc, red_cause, red_out, red_err, red_out_cut, red_err_cut = _spawn(
        row.cmd, cwd=cwd, env=env_red, timeout_s=decl.timeout_s,
        capture_dir=capture_root / f"{decl.name}.red",
        deadline=deadline, now=now,
    )
    row.red_rc = red_rc
    row.red_cause = red_cause
    row.red_stdout_tail = _cap(red_out)
    row.red_stderr_tail = _cap(red_err)
    row.red_stdout_truncated = red_out_cut
    row.red_stderr_truncated = red_err_cut

    # Verbatim from the reference runner: an rc carries a VERDICT, never a CAUSE,
    # so "None" must fail closed, not default to a pass.
    if row.real_rc is None:
        row.verdict = VERDICT_NO_VERDICT
        return row
    if row.red_rc is None:
        # A red arm that never finished is NOT a mismatch: MISMATCH is a claim
        # about a completed evaluation that failed to reproduce the red tree.
        # A timeout or a spawn failure is the runner failing to measure, which
        # is NO_VERDICT - the same word the clean arm gets one branch above.
        # Measured before this fix: a red-only hang reported MISMATCH, i.e. the
        # runner asserted "the law cannot fail" about an arm it never observed.
        row.verdict = VERDICT_NO_VERDICT
        row.red_input_verdict = RED_NOT_EVALUATED
        return row

    clean_word, clean_problem = _verdict_word(clean_out, truncated=clean_out_cut)
    red_word, red_problem = _verdict_word(red_out, truncated=red_out_cut)
    row.real_word = clean_word
    row.red_word = red_word

    # The law must claim, in print, exactly what its exit code says.  A
    # contradiction is the law reporting about itself, so it is NO_VERDICT on
    # both arms and never a pass and never a fail.
    if clean_word != _word_for_rc(row.real_rc):
        row.real_cause = clean_problem or (
            f"verdict_word_contradicts_rc: printed {clean_word!r}, exited {row.real_rc}"
        )
        row.verdict = VERDICT_NO_VERDICT
        return row
    if red_word != _word_for_rc(row.red_rc):
        # NO is not a value here: a red arm that printed NO line, TWO lines, or
        # an unparseable one has not corroborated anything, and the comparison
        # must fail closed on it.  Measured before this fix: with red_word None
        # this branch was skipped, the next block saw red_rc != expect_rc and
        # stamped RED_CONFIRMED, and the law came out PASS on the strength of a
        # red arm that had printed nothing at all.  That is the exact false
        # green this runner exists to refuse.
        row.red_cause = red_problem or (
            f"verdict_word_contradicts_rc: printed {red_word!r}, exited {row.red_rc}"
        )
        row.verdict = VERDICT_NO_VERDICT
        row.red_input_verdict = RED_NOT_EVALUATED
        return row

    # The red arm reproduced: it said FAIL while the clean arm said PASS.
    row.red_input_verdict = RED_CONFIRMED if row.red_rc != row.expect_rc else RED_NOT_REPRODUCED
    if row.real_rc != row.expect_rc:
        row.verdict = VERDICT_RED
    elif row.red_input_verdict == RED_CONFIRMED:
        row.verdict = VERDICT_PASS
    else:
        # Red not reproduced: the law is UNFALSIFIABLE.  Its green is not
        # evidence.  This is the verdict that stops a law from being decorative.
        row.verdict = VERDICT_MISMATCH
    # VOD delta (not in the reference runner).  A red arm that reports
    # NO_VERDICT has DECLINED TO MEASURE - its red input no longer applies, its
    # scratch is unreadable, it crashed before finishing.  Its exit code is then
    # different from expect_rc for a reason that has nothing to do with the
    # subject: it did not judge anything.  Counting that as RED_CONFIRMED would
    # print PASS for a law whose red arm never reproduced the red tree, which is
    # a green nobody earned.  Measured: a red doc whose 'expect' pin drifted to
    # 99 exits 2 with "red-input mutation[0] no longer applies" and the
    # reference verdict reports PASS / RED_CONFIRMED / red_rc=2.  So here the red
    # arm must say FAIL for the law to be PASS, and anything else leaves this law
    # unmeasured - never a pass, and never a lie about itself.
    if row.red_word == VERDICT_NO_VERDICT:
        row.red_input_verdict = RED_NOT_EVALUATED
        row.red_cause = red_problem or (
            "red_arm_no_verdict: the red arm declined to measure, "
            + "so this law's green is unearned"
        )
        row.verdict = VERDICT_NO_VERDICT
    return row


# -- whole-run evaluation ---------------------------------------------------


def _blank_row(decl: LawDecl) -> Row:
    """A row for a law that was never evaluated, in the fail-closed default."""
    return Row(
        name=decl.name,
        description=decl.description,
        cmd=_resolve_python_argv(decl.cmd),
        expect_rc=decl.expect_rc,
        verdict=VERDICT_NO_VERDICT,
        red_input_verdict=RED_NOT_EVALUATED,
        red_input=str(decl.red_input),
    )


def _window_exhausted_row(decl: LawDecl, window_s: float) -> Row:
    """The row for a law the run window never allowed to start.

    This is NOT a pass and NOT a fail: the law was never run, so nothing about it
    was observed.  It travels as a full row rather than as a silent omission,
    because "3 of 5 laws ran" must never be readable as "5 laws hold".
    """
    row = _blank_row(decl)
    row.real_cause = (
        f"run_window_exhausted: the {window_s:g}s run window was already spent "
        "when this law was reached, so it was never started"
    )
    row.stdout_tail = "law not evaluated: the run window was exhausted before it started"
    return row


def _raised_row(decl: LawDecl, exc: BaseException) -> Row:
    """The row for a law whose evaluation raised.

    The exception is reported as a cause, never as a verdict: an exception is the
    runner failing, not the law failing, so this is NO_VERDICT.
    """
    row = _blank_row(decl)
    row.real_cause = f"law_raised: {type(exc).__name__}: {exc}"
    row.stdout_tail = "law not evaluated: evaluating it raised"
    return row


def evaluate(
    decls: Sequence[LawDecl],
    *,
    run_timeout_s: float,
    cwd: Path,
    scratch: Path,
    now: Callable[[], float] = time.monotonic,
) -> tuple[list[Row], dict[str, Any]]:
    """Run every law, streaming one LAW-PLAN, LAW-REFUSED and LAW-VERDICT each.

    Returns (rows, window-info).  Never raises: any unexpected failure per law is
    caught and becomes that law's NO_VERDICT row, so one bad law cannot take the
    whole run down - and cannot take other laws' verdicts with it.
    """
    # cwd and scratch are handed to every law (VODRIP_LAW_CWD / VODRIP_LAW_SCRATCH)
    # and used as subprocess cwds.  A RELATIVE one is a different directory to
    # each reader - the runner, the checker, and the checker's own children all
    # resolve it against a different cwd - so they are made absolute here, at the
    # one seam that hands them out.  Measured: see main().
    cwd = Path(cwd).resolve()
    scratch = Path(scratch).resolve()

    started = now()
    window_s = max(1.0, min(float(run_timeout_s), MAX_RUN_TIMEOUT_S))
    deadline = started + window_s

    rows: list[Row] = []
    for decl in decls:
        # The budget is re-checked BEFORE each law, never mid-law: a law that
        # STARTED inside the window is never killed for overrunning it, and a law
        # that would START outside it is never started at all.  This is a real
        # check, not a comment: before this fix the loop computed `remaining` for
        # the printout only, so a law reached after the window had closed was
        # spawned anyway and the window was decorative.
        #
        # The same deadline is handed to evaluate_declaration, which hands it to
        # each _spawn, because this check and that spawn are not adjacent: the
        # red input is read, two environments are built and three directories are
        # created in between.  A law that passes this line just inside the window
        # could still start outside it, so the window needs the second check to
        # be a window rather than a decoration.
        if now() >= deadline:
            row = _window_exhausted_row(decl, window_s)
            rows.append(row)
            print(f"LAW-REFUSED {decl.name}\n{row.real_cause}", flush=True)
        else:
            try:
                row = evaluate_declaration(decl, cwd=cwd, scratch_root=scratch,
                                           deadline=deadline, now=now)
            except Exception as exc:  # noqa: BLE001 - see the docstring above
                # "Never raises ... one bad law cannot take the whole run down"
                # is a promise the loop has to keep itself: without this catch an
                # unexpected failure here aborted the run and discarded the
                # verdicts of every law already evaluated.
                row = _raised_row(decl, exc)
                print(f"LAW-REFUSED {decl.name}\n{row.real_cause}", flush=True)
            rows.append(row)
        # Both figures come from one raw reading of the clock.  Deriving
        # `remaining` from the ROUNDED `elapsed` made the printout able to say
        # "remaining=0.0" a moment before the window closed, and the two numbers
        # could disagree with each other by up to a millisecond.  The clamp is on
        # the raw difference, so a closed window is 0.0 and never negative.
        delta = now() - started
        elapsed = round(delta, 3)
        remaining = max(0.0, round(window_s - delta, 3))
        print(
            f"LAW-VERDICT {row.verdict} {row.name} | red={row.red_input_verdict} "
            f"clean_rc={row.real_rc} red_rc={row.red_rc} elapsed={elapsed}s remaining={remaining}s",
            flush=True,
        )

    # ONE final reading for both figures.  Two now() calls a moment apart let the
    # pair disagree with each other and with the per-row lines above by whatever
    # time passed between them, which reads as a clock that lost time rather than
    # as a report of one.
    end_delta = now() - started
    window = {
        "run_window_s": window_s,
        "run_window_requested_s": float(run_timeout_s),
        "run_window_hard_max_s": MAX_RUN_TIMEOUT_S,
        "elapsed_s": round(end_delta, 3),
        "remaining_s_at_end": max(0.0, round(window_s - end_delta, 3)),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return rows, window


def verdict_exit_code(rows: Sequence[Row], *, strict: bool = False) -> int:
    """Fold per-law verdicts into the runner's exit code.

    Precedence (3 > 4 > 1 > 0), stated so a caller cannot quietly invent a 5th:

    any NO_VERDICT  -> 4 (strict: 1)  could not measure.  Never a pass, and kept
                                      apart from RED so "I could not measure"
                                      can never read as "it broke".
    any RED/MISMATCH-> 1              a named finding.  A red must not hide
                                      behind a not-measured.
    else            -> 0              every law PASSed, and every pass was
                                      earned by a reproduced red arm.
    """
    if any(r.verdict == VERDICT_NO_VERDICT for r in rows):
        return EXIT_RED if strict else EXIT_NO_VERDICT
    if any(r.verdict in (VERDICT_RED, VERDICT_MISMATCH) for r in rows):
        return EXIT_RED
    return EXIT_GREEN


# -- reporting ---------------------------------------------------------------


def build_report(
    rows: Sequence[Row],
    *,
    laws_file: Path,
    laws_file_lines: int,
    run_timeout_s: float,
    admission: dict[str, Any],
    window: dict[str, Any],
    schema_version: int = SCHEMA_VERSION,
    exit_code: int | None = None,
) -> dict[str, Any]:
    """Assemble the whole-run report.  Population and counts travel together.

    The summary carries the POPULATION (per-law verdicts), never only counts:
    "19 pass, 1 mismatch" without "which one is the mismatch" is exactly the
    0.95 pass-rate this module exists to replace.
    """
    population = len(rows)
    counts = {
        VERDICT_PASS: sum(1 for r in rows if r.verdict == VERDICT_PASS),
        VERDICT_RED: sum(1 for r in rows if r.verdict == VERDICT_RED),
        VERDICT_MISMATCH: sum(1 for r in rows if r.verdict == VERDICT_MISMATCH),
        VERDICT_NO_VERDICT: sum(1 for r in rows if r.verdict == VERDICT_NO_VERDICT),
    }
    failing = [r.name for r in rows if r.verdict in (VERDICT_RED, VERDICT_MISMATCH)]
    unearned = [r.name for r in rows if r.verdict == VERDICT_MISMATCH]
    no_verdict = [r.name for r in rows if r.verdict == VERDICT_NO_VERDICT]
    if no_verdict:
        overall = VERDICT_NO_VERDICT
    elif failing:
        overall = VERDICT_RED
    else:
        overall = VERDICT_PASS
    return {
        "schema_version": schema_version,
        "laws_file": str(laws_file),
        "laws_file_lines": laws_file_lines,
        "run_timeout_s": run_timeout_s,
        "admission": admission,
        "window": window,
        "rows": [r.to_json() for r in rows],
        "summary": {
            "population": population,
            "counts": counts,
            "overall": overall,
            "failing_laws": failing,
            "mismatch_laws_unearned": unearned,
            "no_verdict_laws": no_verdict,
            "green_laws": [r.name for r in rows if r.verdict == VERDICT_PASS],
            "red_input_findings": [
                {"law": r.name, "red_input_verdict": r.red_input_verdict}
                for r in rows
                if r.red_input_verdict != RED_CONFIRMED
            ],
        },
        "bounds": {
            "max_laws": MAX_LAWS,
            "arms_per_law": ARMS_PER_LAW,
            "max_law_timeout_s": MAX_LAW_TIMEOUT_S,
            "max_run_timeout_s": MAX_RUN_TIMEOUT_S,
            "output_cap": _OUTPUT_CAP,
            "capture_cap_bytes": _CAPTURE_CAP,
            "exit_contract": {EXIT_GREEN: "green", EXIT_RED: "red_or_mismatch", EXIT_USAGE: "usage", EXIT_NO_VERDICT: "no_verdict"},
        },
        "exit_code": exit_code,
    }


def _write_report(target: Path, payload: str) -> None:
    """Publish the report atomically: a temp file in the target dir, then replace.

    Atomic on purpose: a half-written report is worse than no report, because a
    caller that reads it would treat a truncated tail as a finished one.  Any
    failure unlinks the temp file and re-raises, so a failed write never leaves a
    partial report where a complete one is expected.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _render_text(report: dict[str, Any]) -> str:
    """Human-readable render.  One line per law, and the arithmetic, always."""
    s = report["summary"]
    counts = s["counts"]
    lines = [
        f"VOD.RIP LAW RUNNER  laws_file={report['laws_file']} ({report['laws_file_lines']} lines)",
        f"population={s['population']} laws  PASS={counts[VERDICT_PASS]} "
        f"RED={counts[VERDICT_RED]} MISMATCH={counts[VERDICT_MISMATCH]} "
        f"NO_VERDICT={counts[VERDICT_NO_VERDICT]}",
        f"ADMISSION  verdict={report['admission']['verdict']} declared_budget_s="
        f"{report['admission']['declared_budget_s']:g}s window_s={report['window']['run_window_s']:g}s",
    ]
    lines.append("")
    for row in report["rows"]:
        lines.append(
            f"  {row['verdict']:<11} {row['name']}  red={row['red_input_verdict']} "
            f"clean_rc={row['real_rc']} red_rc={row['red_rc']}"
        )
        if row["real_cause"]:
            lines.append(f"      cause: {row['real_cause']}")
        if row["red_cause"]:
            lines.append(f"      red cause: {row['red_cause']}")
        if row["stdout_tail"].strip():
            tail = " | ".join(row["stdout_tail"].strip().splitlines()[-3:])
            lines.append(f"      stdout: {tail}")
        if row["red_stdout_tail"].strip() and row["verdict"] != VERDICT_PASS:
            tail = " | ".join(row["red_stdout_tail"].strip().splitlines()[-3:])
            lines.append(f"      red stdout: {tail}")
    lines.append("")
    if s["failing_laws"]:
        lines.append("FAILING (a named finding, not a fraction): " + ", ".join(s["failing_laws"]))
    if s["no_verdict_laws"]:
        lines.append("NO_VERDICT (could not be measured): " + ", ".join(s["no_verdict_laws"]))
    if s["green_laws"]:
        lines.append("PASS (each earned by a reproduced red arm): " + ", ".join(s["green_laws"]))
    lines.append(f"OVERALL {s['overall']}  exit {report['exit_code']}")
    return "\n".join(lines)


# -- CLI --------------------------------------------------------------------

USAGE = """usage: lawrunner.py [--laws PATH] [--json] [--out PATH] [--strict]
                      [--run-timeout SECONDS] [--scratch DIR] [--repo DIR]

Runs every law declared in a law file twice: once on the tree as it stands
(the clean arm), once with a red-input document injected (the red arm).  A law
only PASSes when its clean arm produces expect_rc AND its red arm produces a
different exit code - i.e. when the law is provable to fail.

Four per-law verdicts, never aggregated into a fraction:

  PASS          clean arm produced expect_rc; red arm did not.  Evidence.
  RED           clean arm did not produce expect_rc.  A named finding.
  MISMATCH      both arms produced expect_rc.  The law holds but cannot be
                made to fail, so its green is not evidence.  Also a finding.
  NO_VERDICT    the law could not be evaluated (missing red-input, spawn
                failure, timeout, budget exhausted, no verdict line, or a
                verdict line contradicting its own exit code).

Exit contract:

  0  every law PASSed
  1  at least one law REDed or MISMATCHed (--strict also lands here)
  3  usage error: bad arguments, or the law file is missing / unreadable /
     unparseable / over the MAX_LAWS cap / over its DECLARED run cost.
     Nothing ran.
  4  at least one law produced NO_VERDICT, or the runner crashed.  Kept
     apart from 1 so "I could not measure" can never read as "it broke".

Env: VODRIP_LAWS_FILE names a law file when --laws is absent.
     In every arm: VODRIP_LAW_NAME, VODRIP_LAW_EXPECT_RC, VODRIP_LAW_SCRATCH,
     VODRIP_LAW_CWD.  On the red arm only: VODRIP_LAW_RED_INPUT.
     An argv element of exactly "<python>" is replaced with sys.executable.
"""


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point.  Never raises a traceback at the caller: every failure is
    folded into an exit code the caller already knows how to read (3 or 4)."""
    args = list(sys.argv[1:] if argv is None else argv)
    laws_path: str | None = None
    as_json = False
    out_path: str | None = None
    strict = False
    run_timeout_s = DEFAULT_RUN_TIMEOUT_S
    scratch_dir: str | None = None
    repo_dir: Path = Path(__file__).resolve().parents[2]

    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            sys.stdout.write(USAGE)
            return EXIT_GREEN
        if a == "--laws":
            if i + 1 >= len(args):
                sys.stderr.write("lawrunner: --laws needs a path\n")
                return EXIT_USAGE
            laws_path = args[i + 1]
            i += 2
            continue
        if a == "--json":
            as_json = True
            i += 1
            continue
        if a == "--out":
            if i + 1 >= len(args):
                sys.stderr.write("lawrunner: --out needs a path\n")
                return EXIT_USAGE
            out_path = args[i + 1]
            i += 2
            continue
        if a == "--strict":
            strict = True
            i += 1
            continue
        if a == "--run-timeout":
            if i + 1 >= len(args):
                sys.stderr.write("lawrunner: --run-timeout needs a number of seconds\n")
                return EXIT_USAGE
            try:
                run_timeout_s = float(args[i + 1])
            except ValueError:
                sys.stderr.write(f"lawrunner: --run-timeout must be a number, got {args[i + 1]!r}\n")
                return EXIT_USAGE
            i += 2
            continue
        if a == "--scratch":
            if i + 1 >= len(args):
                sys.stderr.write("lawrunner: --scratch needs a directory\n")
                return EXIT_USAGE
            scratch_dir = args[i + 1]
            i += 2
            continue
        if a == "--repo":
            if i + 1 >= len(args):
                sys.stderr.write("lawrunner: --repo needs a directory\n")
                return EXIT_USAGE
            # Absolute immediately: this becomes the arm cwd and VODRIP_LAW_CWD.
            repo_dir = Path(args[i + 1]).resolve()
            i += 2
            continue
        sys.stderr.write(f"lawrunner: unknown argument {a!r} (see --help)\n")
        return EXIT_USAGE

    try:
        path = discover_law_file(laws_path, repo=repo_dir)
    except LawError as exc:
        sys.stderr.write(f"lawrunner: {exc}\n")
        return EXIT_USAGE

    # The LAW-PLAN line is emitted BEFORE the first spawn, and the LAW-REFUSED
    # line is the failure path's twin - so a caller reading a truncated log can
    # always tell "nothing ran" from "everything ran".
    try:
        decls, line_count, _raw = load_law_file(path)
    except LawError as exc:
        sys.stderr.write(f"lawrunner: {exc}\n")
        return EXIT_USAGE

    plan = plan_laws(decls, run_timeout_s=run_timeout_s)
    print(
        f"LAW-PLAN laws={plan['laws']} arms={plan['arms']} "
        f"one_arm_declared_s={plan['one_arm_declared_s']:g} "
        f"declared_budget_s={plan['declared_budget_s']:g} "
        f"run_window_s={plan['run_window_s']:g} verdict={plan['verdict']}",
        flush=True,
    )
    if not plan["admitted"]:
        # LAW-REFUSED on stdout: the log itself must carry the arithmetic, since
        # a caller that only captures one stream must not lose the refusal.
        print(f"LAW-REFUSED run_admission_refused\n{plan['refusal']}", flush=True)
        sys.stderr.write(f"lawrunner: {plan['refusal']}\n")
        return EXIT_USAGE

    # Absolute before anything touches it.  This path is both a subprocess cwd
    # and the value of VODRIP_LAW_SCRATCH, and a relative path means a DIFFERENT
    # directory to each of those readers.  Measured with a relative --scratch:
    # the arm materialised its files under <arm>/<relative>/<arm>/... while the
    # probe was launched with the same string relative to THAT cwd, so the probe
    # exited 2 on a path that did not exist and the law reported NO_VERDICT - the
    # runner's own path handling wearing a law's verdict.  A scratch that means
    # two things measures nothing.
    scratch = Path(scratch_dir) if scratch_dir else Path(tempfile.mkdtemp(prefix="vodrip-laws-"))
    scratch = scratch.resolve()
    scratch.mkdir(parents=True, exist_ok=True)

    try:
        rows, window = evaluate(decls, run_timeout_s=run_timeout_s, cwd=repo_dir, scratch=scratch)
    except Exception as exc:  # pragma: no cover - the runner must never crash a gate
        sys.stderr.write(f"lawrunner: unexpected failure: {type(exc).__name__}: {exc}\n")
        return EXIT_NO_VERDICT

    code = verdict_exit_code(rows, strict=strict)
    report = build_report(
        rows,
        laws_file=path,
        laws_file_lines=line_count,
        run_timeout_s=run_timeout_s,
        admission=plan,
        window=window,
        exit_code=code,
    )

    payload = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if out_path:
        try:
            _write_report(Path(out_path), payload)
        except (OSError, ValueError) as exc:
            # The report IS the interface.  A run whose report cannot be written
            # has not been measured, so it must say that in words instead of
            # escaping as a traceback - a traceback reads as "the runner
            # crashed", and a caller parsing stderr for LAW lines would see
            # nothing at all.  This path used to sit outside the try around
            # evaluate() and did exactly that.
            cause = f"report_unwritable: {out_path}: {type(exc).__name__}: {exc}"
            print(f"LAW-REFUSED report_not_written\n{cause}", flush=True)
            sys.stderr.write(f"lawrunner: {cause}\n")
            return EXIT_NO_VERDICT
    if as_json:
        sys.stdout.write(payload)
    else:
        print(_render_text(report), flush=True)

    sys.stderr.write(
        f"LAW RUNNER EXIT {code}: "
        + (", ".join(f"{r.verdict} {r.name}" for r in rows) or "no laws")
        + "\n"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
