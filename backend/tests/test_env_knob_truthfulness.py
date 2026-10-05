"""Env knobs: a doc may not advertise a knob the code never reads.

The defect this pins, confirmed in-tree: ``VODRIP_TRANSCRIBE_GPU_COPIES`` was
retired (multi-copy ASR was removed) and the CODE says so —
``services/archive_transcribe.py:115`` carries the constant annotated
``# DEPRECATED, IGNORED`` and nothing reads it outside the module self-check.
The DOCS did not follow: ``todo.md`` listed it twice as a live dial with a
default ("min(VODRIP_TRANSCRIBE_GPU_COPIES (default 1), ...)" and "Env:
VODRIP_TRANSCRIBE_GPU_COPIES (default 1), VODRIP_TRANSCRIBE_WORKERS
(default 2)"). A knob documented as functional that does nothing is worse than
an undocumented one: it makes a broken path look configured.

Why the pre-existing guard did not catch it: the docstring check in
``test_asr_device_reporting.py::test_docs_do_not_advertise_gpu_copies_as_a_
working_knob`` reads three docstrings off the imported module. It cannot see a
markdown file at all, so the ``todo.md`` lie was structurally invisible to it.
The scan below is the missing half — repository prose, not Python docstrings.

A knob is RETIRED here only when the code proves it: the constant is annotated
dead AND no read of it exists outside the module self-check. Both halves are
asserted, so this file cannot be satisfied by editing prose alone.

No import of ``services.archive_transcribe`` here on purpose — it is a heavy
module; every fact below is read off the AST, which is also what makes the
"never reintroduce a live read" check possible.

Run from backend/: python -m pytest tests/test_env_knob_truthfulness.py
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND_ROOT.parent
_TRANSCRIBE = _BACKEND_ROOT / "services" / "archive_transcribe.py"

# Knob name -> the constant that carries it in the code.
RETIRED_KNOBS = {
    "VODRIP_TRANSCRIBE_GPU_COPIES": "GPU_COPIES_ENV",
}

# The functions that are ALLOWED to read a retired knob: the module self-check
# asserts "this env var does not change the result", which is the opposite of
# consuming it as a dial. The self-check is split in two (a thin wrapper plus a
# body) and has been before, so the allowed region is every function whose name
# starts with this prefix rather than one hardcoded def that a refactor would
# silently invalidate.
_SELFCHECK_PREFIX = "_run_module_selfcheck"

# A prose mention of a retired knob is only honest next to one of these.
# Deliberately generous on casing and phrasing: the point is that a reader sees
# "this does nothing", not that the wording matches a fixed string.
_DEAD_MARKERS = (
    "retired",
    "deprecated",
    "ignored",
    "removed",
    "dead knob",
    "not read",
    "never read",
    "superseded",
    "does nothing",
)

# Lines of context a mention may sit away from its marker. A markdown bullet
# wraps; the marker does not have to share the line.
_WINDOW = 4


def _markdown_files() -> list[Path]:
    """Every prose file a human reads instructions out of.

    Scoped to the top level + docs/ + .github/: the prose that ships. Code
    comments and test docstrings are covered by the checks below or by
    test_asr_device_reporting.py.
    """
    found: list[Path] = []
    for pattern in ("*.md", "docs/*.md", ".github/*.md", "*.txt"):
        found.extend(_REPO_ROOT.glob(pattern))
    return sorted(p for p in found if p.is_file())


def _constant_assignment(tree: ast.AST, const: str) -> ast.Assign | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == const:
                    return node
    return None


def _env_reads_of(tree: ast.AST, const: str) -> list[int]:
    """Line numbers of every ``os.environ`` read keyed on `const`.

    Both spellings count: ``os.environ.get(CONST, ...)`` and
    ``os.environ[CONST]``. A ``from os import environ`` alias would be missed,
    which is why :func:`test_retired_knob_is_not_reachable_through_an_env_alias`
    closes that hole separately.
    """
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # os.environ.get(CONST, ...)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "environ"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == const
        ):
            hits.append(node.lineno)
            continue
        # os.environ[CONST]
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "environ"
            and isinstance(node.slice, ast.Name)
            and node.slice.id == const
        ):
            hits.append(node.lineno)
    return sorted(hits)


# --- 1. the code still says the knob is dead --------------------------------

def test_retired_knob_constant_is_annotated_dead():
    """The code half. If this stops holding, the knob is live and the doc fix
    above it is a lie in the other direction."""
    text = _TRANSCRIBE.read_text(encoding="utf-8")
    lines = text.splitlines()
    tree = ast.parse(text)
    for knob, const in RETIRED_KNOBS.items():
        node = _constant_assignment(tree, const)
        assert node is not None, (
            f"{const} vanished from archive_transcribe.py - {knob} is no longer "
            f"even named as retired. Either restore the annotated constant or "
            f"remove {knob} from RETIRED_KNOBS with a reason."
        )
        # The annotation is a COMMENT, and a trailing comment is not part of an
        # ast node's source segment - read the physical line, not get_source_segment.
        line = lines[node.lineno - 1]
        assert knob in line, (
            f"{const} no longer names {knob}; the retired-knob mapping drifted"
        )
        comment = line.split("#", 1)[1].lower() if "#" in line else ""
        assert "deprecated" in comment and "ignored" in comment, (
            f"{const} lost its '# DEPRECATED, IGNORED' annotation ({comment!r}); "
            f"if {knob} became live again, delete it from RETIRED_KNOBS instead "
            f"of re-labelling it dead"
        )


# --- 2. and no live read path exists ----------------------------------------

def test_retired_knob_is_not_read_outside_the_module_selfcheck():
    """The knob must stay inert. Every read is confined to the self-check, the
    one place that PROVES the env var is ignored.

    This is the assertion that fails if someone reintroduces the dial as a live
    path, and it is AST-based on purpose: the reintroduction would be a single
    ``os.environ.get(GPU_COPIES_ENV, "1")``, invisible to any test that only
    exercised behaviour.
    """
    src = _TRANSCRIBE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    selfchecks = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name.startswith(_SELFCHECK_PREFIX)
    ]
    assert selfchecks, (
        f"no function named {_SELFCHECK_PREFIX}* in archive_transcribe.py - the "
        f"retired-knob scan has no allowed region left and cannot judge any read"
    )
    # The allowed region is the UNION of the self-check functions' line spans.
    allowed = [(n.lineno, n.end_lineno) for n in selfchecks]
    for knob, const in RETIRED_KNOBS.items():
        reads = _env_reads_of(tree, const)
        assert reads, (
            f"{const} is not read anywhere, not even in {_SELFCHECK_PREFIX}* - the "
            f"proves-it-is-ignored self-check was deleted, so nothing now "
            f"holds {knob} inert"
        )
        escaped = [
            ln for ln in reads if not any(lo <= ln <= hi for lo, hi in allowed)
        ]
        assert escaped == [], (
            f"{knob} is read at archive_transcribe.py:{escaped}, OUTSIDE "
            f"{_SELFCHECK_PREFIX}* (lines {sorted(allowed)}) - the retired knob is "
            f"a live dial again. Revert the reintroduction with: "
            f"`git checkout 54663e1 -- backend/services/archive_transcribe.py`"
        )


def test_retired_knob_is_not_reachable_through_an_env_alias():
    """Closes the alias hole in :func:`_env_reads_of`.

    ``from os import environ`` then ``environ.get(GPU_COPIES_ENV)`` — or
    ``environ = os.environ`` under another local name — is the same read wearing
    a different hat. Assert the module never aliases ``environ``, so the
    constant-name scan above cannot be walked around.
    """
    src = _TRANSCRIBE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    aliases: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "") == "os":
            for alias in node.names:
                if alias.name == "environ":
                    aliases.append(node.lineno)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "environ":
                    aliases.append(node.lineno)
    assert aliases == [], (
        f"archive_transcribe.py aliases os.environ at lines {aliases}; the "
        f"retired-knob read scan keys on the literal name and would miss a "
        f"reintroduced read through the alias"
    )


# --- 3. and no doc advertises it as working --------------------------------

def test_no_prose_advertises_a_retired_knob_as_a_dial():
    """The defect itself, and the test that fails if it comes back.

    Every prose mention of a retired knob must sit next to a "this does
    nothing" marker. A mention with no marker is the lying case: a reader sets
    the variable, sees no effect, and concludes something else is broken.
    """
    offenders: list[str] = []
    for path in _markdown_files():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel = path.relative_to(_REPO_ROOT).as_posix()
        for idx, line in enumerate(lines):
            for knob in RETIRED_KNOBS:
                if knob not in line:
                    continue
                window = "\n".join(
                    lines[max(0, idx - _WINDOW) : idx + _WINDOW + 1]
                ).lower()
                if not any(m in window for m in _DEAD_MARKERS):
                    offenders.append(f"{rel}:{idx + 1}")
    assert offenders == [], (
        f"retired knob advertised as a working dial at {offenders}; say it is "
        f"RETIRED/IGNORED and what replaced it, or the owner sets a variable "
        f"that does nothing"
    )


def test_the_documented_knobs_are_the_ones_the_code_reads():
    """Guards the OTHER direction of drift: a doc that lists a knob with a
    default is a claim the code must honour.

    Scoped to the retired-knob neighbourhood in todo.md's "Contracts" list, so
    it stays a real assertion rather than a repo-wide lint: a contract line that
    names a VODRIP_* env var must name a constant the code actually reads.
    """
    contracts = _REPO_ROOT / "todo.md"
    assert contracts.is_file(), "todo.md moved — repoint this scan"
    text = contracts.read_text(encoding="utf-8", errors="replace")
    src = _TRANSCRIBE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    # The ENV VAR NAME is the string VALUE of the assignment, not the constant's
    # identifier: `WORKERS_ENV = "VODRIP_TRANSCRIBE_WORKERS"` documents the knob
    # VODRIP_TRANSCRIBE_WORKERS. Comparing against the identifier name is the
    # bug this comment exists to prevent.
    live_knobs = {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and node.value.value.startswith("VODRIP_")
    }
    claimed = set(re.findall(r"VODRIP_[A-Z0-9_]+", text))
    unknown = claimed - live_knobs - set(RETIRED_KNOBS)
    # todo.md is a historical design + backlog doc and also names knobs owned by
    # other modules (VODRIP_APP_DATA, VODRIP_ARCHIVE_DB) and by out-of-tree
    # bench scripts (VODRIP_BENCH_BACKEND, read by G:/vodrip-bench/bench_one.py).
    # Assert instead that nothing UNKNOWN slips in beyond that recorded set, so
    # a new invented knob in the contracts list is caught.
    known_external = {
        "VODRIP_APP_DATA",
        "VODRIP_ARCHIVE_DB",
        "VODRIP_DATA_DIR",
        "VODRIP_BENCH_BACKEND",
    }
    assert unknown <= known_external, (
        f"todo.md names env knobs this repo's code never reads: "
        f"{sorted(unknown - known_external)}"
    )


# --- 4. meta: the seam guard this file's sibling depends on must exist -------

def test_the_ast_seam_guard_file_still_exists_with_its_ast_checks():
    """Fails if the seam AST guard is deleted or gutted.

    ``tests/test_guard_binding_seam.py`` is the only thing standing between a
    function-local re-import of the yt-dlp guard and a unit test issuing a LIVE
    network request — the escape that already happened twice. A guard that can
    be quietly removed has no value, so its removal is itself an assertion.
    """
    guard = _BACKEND_ROOT / "tests" / "test_guard_binding_seam.py"
    assert guard.is_file(), (
        f"{guard.name} is gone — the repo-wide AST scan that stops a second "
        f"binding of guarded_youtube_dl from reaching the real network"
    )
    src = guard.read_text(encoding="utf-8")
    for needed in (
        "def test_no_new_module_binds_a_guard_name",  # the repo-wide half
        "def test_consumer_does_not_bind_the_guard_name",  # the per-module half
        "ast.parse",  # the mechanism both halves rely on
        "_RealExtractorReached",  # the runtime tripwire
    ):
        assert needed in src, (
            f"{guard.name} no longer contains {needed!r}; the seam guard was "
            f"reduced and the escape it prevents is unguarded again"
        )
