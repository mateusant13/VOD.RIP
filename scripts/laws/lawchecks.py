"""Shared plumbing for VOD.RIP law checkers.

Every checker is a plain process that prints exactly ONE verdict line to stdout:

    law-verdict: PASS|FAIL|NO_VERDICT <law-name> | <reason>

and exits 0 / 1 / 2 respectively (3 is reserved for a usage error, which
lawrunner.py detects on its own).  The runner corroborates the printed word
against this process exit code, so a contradiction becomes NO_VERDICT rather
than a pass.  ASCII separators only: a cp1252 stdout must not eat the line.

The red-input mechanism is IN MEMORY.  A red-input document is a set of
exact-text find/replace mutations with pinned expected counts; the checker
applies them to its own image of the tree and then runs the same invariant over
the mutated image.  The working tree is never written to, so no law run can
leave the repo broken - and a red document that no longer applies (a count that
does not match) is a NO_VERDICT, never a pass, because a law that cannot be made
to fail is not evidence.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

#: Verdict words a checker may print.  Deliberately tiny: a checker that wants a
#: new word is asking for a new outcome, and that is a runner change, not a typo.
WORD_PASS = "PASS"
WORD_FAIL = "FAIL"
WORD_NO_VERDICT = "NO_VERDICT"

#: Exit codes.  0 pass, 1 fail (a named finding), 2 could not measure, 3 usage.
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_NO_VERDICT = 2
EXIT_USAGE = 3

#: Environments the runner guarantees a checker.  Missing ones are a NO_VERDICT:
#: a checker that cannot read its own coordinates must not authorise a finding.
ENV_NAME = "VODRIP_LAW_NAME"
ENV_RED_INPUT = "VODRIP_LAW_RED_INPUT"
ENV_SCRATCH = "VODRIP_LAW_SCRATCH"
ENV_CWD = "VODRIP_LAW_CWD"

#: Source extensions counted as part of the front end for source-shape laws.
SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs")


class NoVerdict(Exception):
    """Raised by a checker when it cannot measure.  Always becomes exit 2."""


#: Set by finish().  run() reads it to refuse a checker that returns WITHOUT
#: having printed anything: a silent exit 0 is a checker that judged nothing,
#: and a shell has no way to tell that apart from a pass.
_FINISHED = False


def law_name(default: str = "unnamed") -> str:
    """The law's own name, as the runner declared it.

    Under the runner the declared env var is the only source and it is always
    present.  Run by a human with no runner, the first caller's default becomes
    the name - and it is written back into the environment, so every later
    finish() prints that same name instead of falling back to "unnamed".
    """
    name = os.environ.get(ENV_NAME)
    if name is None or not name.strip():
        os.environ[ENV_NAME] = (default or "").strip() or "unnamed"
    return os.environ[ENV_NAME].strip()


def repo_root() -> Path:
    """The repository root the runner handed us, resolved to an absolute path."""
    cwd = os.environ.get(ENV_CWD)
    if not cwd or not cwd.strip():
        raise NoVerdict(f"{ENV_CWD} is not set; the law cannot locate the tree")
    root = Path(cwd.strip()).resolve()
    if not root.is_dir():
        raise NoVerdict(f"{ENV_CWD} is not a directory: {root}")
    return root


def finish(word: str, reason: str) -> int:
    """Print the one verdict line and return the matching exit code.

    Exactly one line, ever.  A checker that prints two is arguing with itself.

    An unknown word is refused BEFORE anything is printed.  It used to be looked
    up after the print, so a typo emitted a line the runner cannot read and then
    a traceback whose handler emitted a SECOND line - and two lines is exactly
    what "the law is arguing with itself" looks like from the runner's side.
    """
    global _FINISHED
    code = {WORD_PASS: EXIT_PASS, WORD_FAIL: EXIT_FAIL, WORD_NO_VERDICT: EXIT_NO_VERDICT}.get(word)
    if code is None:
        raise ValueError(
            f"not a verdict word: {word!r} "
            f"(one of {WORD_PASS}/{WORD_FAIL}/{WORD_NO_VERDICT})"
        )
    if _FINISHED:
        # "Exactly one line, ever" has to be true by construction, not by the
        # good manners of checkers.  A second call - a direct second finish(),
        # or run() catching an exception raised AFTER a verdict was printed -
        # would emit a second, possibly contradictory line, and the runner reads
        # two lines as no verdict at all.  Nothing more is printed; the answer
        # is the exit code for "no single verdict", so the process exits with the
        # first line standing and a code that disagrees with it (the runner
        # turns that disagreement into NO_VERDICT rather than a green).
        return EXIT_NO_VERDICT
    name = os.environ.get(ENV_NAME) or "unnamed"
    reason = " ".join((reason or "").split()) or "no reason given"
    print(f"law-verdict: {word} {name} | {reason}", flush=True)
    _FINISHED = True
    return code


def run(main_fn) -> int:
    """Wrap a checker's entry point so NoVerdict/usage errors cannot escape.

    A checker never raises at the shell: every exit is an exit code the runner
    already interprets.
    """
    global _FINISHED
    # Reset the flag per invocation.  It is module-level, so a second run() in
    # the same interpreter would otherwise inherit the FIRST run's verdict and
    # read a checker that printed nothing as one that had already finished.
    _FINISHED = False
    try:
        rc = main_fn()
    except NoVerdict as exc:
        return finish(WORD_NO_VERDICT, f"could not measure: {exc}")
    except FileNotFoundError as exc:
        return finish(WORD_NO_VERDICT, f"could not measure: {exc}")
    except Exception as exc:  # noqa: BLE001 - a crash is NO_VERDICT, never a pass
        return finish(WORD_NO_VERDICT, f"checker crashed: {type(exc).__name__}: {exc}")
    if not _FINISHED:
        # The runner already refuses an arm with no verdict word
        # (no_verdict_word -> NO_VERDICT), but a checker run by hand would exit 0
        # and read as a pass.  A checker that returns without printing judged
        # nothing, and this says so in the one place a human looks.
        return finish(WORD_NO_VERDICT,
                      f"checker returned {rc!r} without printing a verdict line")
    return rc


def iter_source_files(root: Path, subdir: str = "src") -> list[Path]:
    """Every source file under root/subdir, sorted for a stable report."""
    base = root / subdir
    if not base.is_dir():
        raise NoVerdict(f"{subdir}/ not found under {root}")
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in {"node_modules", "dist", ".git"})
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.suffix.lower() in SOURCE_SUFFIXES:
                out.append(p)
    return out


def relpaths(root: Path, files: Iterable[Path]) -> list[str]:
    """Repo-relative, forward-slashed paths - the form a finding must use."""
    return sorted(str(Path(f).resolve().relative_to(root.resolve())).replace(os.sep, "/") for f in files)


def load_tree(root: Path, subdir: str = "src") -> dict[str, str]:
    """Read every source file into {relpath: text}.  One pass, no lazy reads."""
    tree: dict[str, str] = {}
    for f in iter_source_files(root, subdir):
        rel = str(f.relative_to(root)).replace(os.sep, "/")
        try:
            tree[rel] = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise NoVerdict(f"cannot read {rel}: {exc}") from exc
    if not tree:
        raise NoVerdict(f"no source files found under {root / subdir}")
    return tree


def lines_with(text: str, needle: str) -> list[int]:
    """Public form of the line finder: 1-based lines containing needle."""
    return [i for i, line in enumerate(text.splitlines(), start=1) if needle in line]


def load_named_files(root: Path, relpaths) -> dict[str, str]:
    """Read NAMED repo-relative files into {relpath: text}.

    ``load_tree`` is scoped to ``src/`` because a frontend law about call sites
    wants the whole tree.  A law about the tooling - the probe, the gate - is
    the mirror case, and it used to have no way to open its subject at all,
    which is how the parts that judge everything stay unjudged.  Every path
    must exist: a law that silently loads an empty tree can only ever pass.
    """
    out: dict[str, str] = {}
    for rel in relpaths:
        if not isinstance(rel, str) or not rel.strip():
            raise NoVerdict("a path passed to load_named_files is empty")
        rel = rel.strip().replace("\\", "/")
        p = (root / rel).resolve()
        try:
            p.relative_to(root.resolve())
        except ValueError:
            raise NoVerdict(f"not under the repo root: {rel}") from None
        if p.is_dir() or not p.is_file():
            raise NoVerdict(f"not a file under the repo: {rel}")
        try:
            out[rel] = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise NoVerdict(f"cannot read {rel}: {exc}") from exc
    if not out:
        raise NoVerdict("load_named_files was given no paths")
    return out

def apply_red_input(tree: dict[str, str], root: Path) -> dict[str, str] | None:
    """Apply the red-input document to an in-memory copy of the tree.

    Returns the MUTATED tree, or None when there is no red input (clean arm).
    Raises NoVerdict when the document is unreadable, malformed, or any
    mutation's exact-text count does not match its pinned 'expect' - because a
    red document that no longer applies means this law can no longer be made to
    fail, and 'the law holds' is then unearned rather than measured.
    """
    doc_path = os.environ.get(ENV_RED_INPUT)
    if not doc_path or not doc_path.strip():
        return None

    path = Path(doc_path.strip())
    if not path.is_file():
        raise NoVerdict(f"red-input document not found: {path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise NoVerdict(f"red-input document is unreadable: {exc}") from exc
    if not isinstance(doc, dict):
        raise NoVerdict("red-input document must be a JSON object")
    mutations = doc.get("mutations")
    if not isinstance(mutations, list) or not mutations:
        raise NoVerdict("red-input document declares no mutations")

    mutated = dict(tree)
    for i, m in enumerate(mutations):
        if not isinstance(m, dict):
            raise NoVerdict(f"red-input mutation[{i}] must be an object")
        rel = m.get("file")
        find = m.get("find")
        put = m.get("put")
        expect = m.get("expect")
        if not isinstance(rel, str) or not rel.strip():
            raise NoVerdict(f"red-input mutation[{i}] has no 'file'")
        if not isinstance(find, str) or not find:
            raise NoVerdict(f"red-input mutation[{i}] has no 'find'")
        if not isinstance(put, str):
            raise NoVerdict(f"red-input mutation[{i}] has no 'put'")
        if isinstance(expect, bool) or not isinstance(expect, int) or expect < 1:
            raise NoVerdict(f"red-input mutation[{i}] needs an integer 'expect' >= 1")
        if find == put:
            # A mutation that replaces text with itself cannot change anything,
            # so the red arm would measure the CLEAN tree.  The runner does
            # refuse that as MISMATCH (red_rc == expect_rc -> RED_NOT_REPRODUCED),
            # which is honest but generic; this names the actual mistake, in the
            # document, at the moment it is read.
            raise NoVerdict(
                f"red-input mutation[{i}] replaces {find!r} with itself, which "
                f"cannot change anything"
            )
        if rel not in mutated:
            raise NoVerdict(f"red-input mutation[{i}] names a file not in the tree: {rel}")
        text = mutated[rel]
        got = text.count(find)
        if got != expect:
            raise NoVerdict(
                f"red-input mutation[{i}] no longer applies to {rel}: "
                f"expected exactly {expect} occurrence(s) of {find!r}, found {got}"
            )
        mutated[rel] = text.replace(find, put)
    return mutated


def count_lines(text: str, needle: str) -> int:
    """Number of LINES containing needle (a file with 3 hits on one line counts 1)."""
    return len(_find_lines(text, needle))


def findings_by_file(tree: dict[str, str], needles: tuple[str, ...]) -> dict[str, list[int]]:
    """{relpath: [lines]} for every source line containing any needle."""
    out: dict[str, list[int]] = {}
    for rel, text in tree.items():
        hits: list[int] = []
        for needle in needles:
            hits.extend(lines_with(text, needle))
        if hits:
            out[rel] = sorted(set(hits))
    return out


def fmt_findings(findings: dict[str, list[int]], limit: int = 12) -> str:
    """A finding rendered so a reader can go straight to the line."""
    if not findings:
        return "none"
    items = sorted(findings.items())
    parts = [f"{rel}:{','.join(str(n) for n in lines)}" for rel, lines in items]
    if len(parts) > limit:
        shown = "; ".join(parts[:limit])
        return f"{shown}; ... and {len(parts) - limit} more file(s)"
    return "; ".join(parts)
