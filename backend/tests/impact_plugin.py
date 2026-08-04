"""Opt-in pytest plugin: skip test modules that would pass anyway.

Test impact analysis (TIA) without coverage.py or a daemon:

  * changed set = ``git diff --name-only HEAD`` + untracked files, run at the
    repo root (override: ``VODRIP_CHANGED_FILES``, space/newline separated);
  * for each collected test module we compute its transitive import closure
    (AST ``import`` / ``from ... import`` resolved to repo-relative files);
  * a module is skipped iff NONE of its closure files is in the changed set
    AND the cache says it passed the previous run with identical
    module + closure hashes.

Safety is the priority: every failure mode (git unavailable, repo root not
found, corrupt cache) degrades to "run everything", and a changed file inside
the closure always forces a run. Enable with ``--impact`` or ``VODRIP_IMPACT=1``.
Cache: ``backend/.impact-cache.json`` (override: ``VODRIP_IMPACT_CACHE``).
Stdlib only — no new dependencies.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


# ---------------------------------------------------------------------------
# repo / git plumbing
# ---------------------------------------------------------------------------

def _find_repo_root() -> Path | None:
    """Walk up from this plugin file to the first dir containing ``.git``."""
    here = Path(__file__).resolve().parent
    for d in (here, *here.parents):
        if (d / ".git").exists():
            return d
    return None


def _normalize(p: str) -> str:
    p = p.replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p


def _changed_files(root: Path) -> set[str] | None:
    """Repo-relative changed paths (git diff + untracked). None = unknown -> no-op."""
    env = os.environ.get("VODRIP_CHANGED_FILES", "")
    if env.strip():
        return {_normalize(p) for p in env.replace("\n", " ").split() if p.strip()}
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"], cwd=root,
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        if diff.returncode != 0:
            return None
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"], cwd=root,
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        if untracked.returncode != 0:
            return None
        names = diff.stdout.splitlines() + untracked.stdout.splitlines()
        return {_normalize(n) for n in names if n.strip()}
    except Exception:  # git missing, timeout, ... -> conservative no-op
        return None


# ---------------------------------------------------------------------------
# AST import closure
# ---------------------------------------------------------------------------

def _import_targets(path: Path) -> list[tuple[Path | None, str]]:
    """(base, dotted) import targets; base=None means absolute import.

    ``from services import archive_db`` yields both ``services`` and
    ``services.archive_db`` so submodule imports are never missed; stdlib /
    third-party names simply resolve to nothing and become leaves.
    """
    try:
        tree = ast.parse(
            path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except (OSError, SyntaxError):
        return []
    targets: list[tuple[Path | None, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                targets.append((None, a.name))
        elif isinstance(node, ast.ImportFrom):
            prefix = node.module or ""
            if node.level == 0:
                base: Path | None = None
            else:
                base = path.parent
                for _ in range(node.level - 1):
                    base = base.parent
            if prefix:
                targets.append((base, prefix))
            for a in node.names:
                if a.name == "*":
                    continue
                dotted = f"{prefix}.{a.name}" if prefix else a.name
                targets.append((base, dotted))
    return targets


def _resolve(dotted: str, bases: list[Path]) -> Path | None:
    """First existing repo file under any base for a dotted module name."""
    parts = dotted.split(".")
    for base in bases:
        p = base.joinpath(*parts)
        if p.with_suffix(".py").is_file():
            return p.with_suffix(".py")
        if (p / "__init__.py").is_file():
            return p / "__init__.py"
    return None


def _closure(path: Path, backend: Path) -> list[Path]:
    """Transitive repo-file closure of ``path`` (the module itself included)."""
    root = backend.parent
    seen: dict[Path, None] = {}
    stack = [path]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen[cur] = None
        for base, dotted in _import_targets(cur):
            cand = _resolve(dotted, [backend, cur.parent])
            if cand is not None and cand.is_relative_to(root):
                stack.append(cand)
    return list(seen)


def closure_info(path: Path, backend: Path) -> tuple[str, str, list[str]]:
    """(module_sha, closure_sha, sorted repo-relative closure paths).

    module_sha  = SHA-256 of the test module file bytes;
    closure_sha = SHA-256 of the sorted repo-relative path list. Structure
    changes bust it; content changes are caught by the changed-set check
    (ponytail: env overrides must list EVERY changed file — git-driven runs
    always do, which is the primary path).
    """
    files = sorted(_closure(path, backend))
    module_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    rel = [str(f.relative_to(backend.parent)).replace("\\", "/") for f in files]
    closure_sha = hashlib.sha256("\n".join(rel).encode("utf-8")).hexdigest()
    return module_sha, closure_sha, rel


def should_skip(path: Path, changed: set[str], cache: dict, backend: Path) -> bool:
    """True iff ``path`` is unaffected by ``changed`` and cached-passing unchanged.

    Changed paths are matched in both repo-relative ("backend/services/x.py",
    as git emits) and backend-relative ("services/x.py", as VODRIP_CHANGED_FILES
    examples use) forms.
    """
    module_sha, closure_sha, rel = closure_info(path, backend)
    module_rel = str(path.relative_to(backend.parent)).replace("\\", "/")
    entry = cache.get(module_rel)
    if not entry or entry.get("pass_hashes") != [module_sha, closure_sha]:
        return False
    rel_set = set(rel)
    rel_set |= {r[len("backend/"):] for r in rel if r.startswith("backend/")}
    return not bool(changed & rel_set)


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def _load_cache(path: Path) -> dict:
    """Corrupt/missing cache -> empty (run everything)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    for key, entry in data.items():
        if not (isinstance(key, str) and isinstance(entry, dict)):
            continue
        h = entry.get("pass_hashes")
        if (isinstance(h, list) and len(h) == 2
                and all(isinstance(x, str) and len(x) == 64 for x in h)):
            out[key] = {"pass_hashes": h}
    return out


def _write_cache(path: Path, cache: dict) -> None:
    """Atomic replace; cache is advisory so a write failure never fails the run."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(cache, sort_keys=True, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# state + hooks
# ---------------------------------------------------------------------------

# One pytest session per process, so module-level state is safe (xdist workers
# are separate processes). TestReport carries no .config in pytest 9.x, hence
# this instead of a config attribute.
_STATE: dict = {
    "enabled": False,
    "ready": False,
    "root": None,
    "backend": None,
    "changed": None,
    "cache_path": None,
    "module_stats": {},
    "closure_cache": {},
}


def _ensure_ready() -> None:
    st = _STATE
    if not st["enabled"] or st["ready"]:
        return
    st["ready"] = True
    root = _find_repo_root()
    if root is None:
        return
    backend = root / "backend"
    if not backend.is_dir():
        return
    changed = _changed_files(root)
    if changed is None:
        return
    st["root"] = root
    st["backend"] = backend
    st["changed"] = changed
    # Read lazily (not in pytest_configure) so a test module can relocate the
    # cache at import time, before collection finishes.
    cache_env = os.environ.get("VODRIP_IMPACT_CACHE", "")
    st["cache_path"] = Path(cache_env) if cache_env else (backend / ".impact-cache.json")


@pytest.hookimpl
def pytest_addoption(parser):
    group = parser.getgroup("test impact")
    group.addoption(
        "--impact", action="store_true", default=False,
        help="skip test modules whose dependency closure is untouched by the "
             "working-tree diff and that passed unchanged last run")


@pytest.hookimpl
def pytest_configure(config):
    _STATE["enabled"] = bool(config.getoption("--impact")) \
        or os.environ.get("VODRIP_IMPACT") == "1"


@pytest.hookimpl
def pytest_collection_modifyitems(session, config, items):
    _ensure_ready()
    st = _STATE
    cache_path = st.get("cache_path")
    if cache_path is None:
        return  # disabled or degraded -> run everything
    cache = _load_cache(cache_path)
    changed = st["changed"]
    backend = st["backend"]
    closure_cache = st["closure_cache"]
    keep: list = []
    skipped_modules: set[str] = set()
    for item in items:
        mod = getattr(item, "module", None)
        f = getattr(mod, "__file__", None) if mod is not None else None
        if not f:
            p = getattr(item, "path", None)
            f = str(p) if p is not None else None
        if not f:
            keep.append(item)
            continue
        path = Path(f).resolve()
        try:
            info = closure_cache.get(path)
            if info is None:
                info = closure_info(path, backend)
                closure_cache[path] = info
            module_sha, closure_sha, rel = info
            module_rel = str(path.relative_to(backend.parent)).replace("\\", "/")
        except ValueError:
            keep.append(item)  # module outside the repo -> never skip
            continue
        entry = cache.get(module_rel)
        if not entry or entry.get("pass_hashes") != [module_sha, closure_sha]:
            keep.append(item)
            continue
        rel_set = set(rel)
        rel_set |= {r[len("backend/"):] for r in rel if r.startswith("backend/")}
        if changed & rel_set:
            keep.append(item)
            continue
        skipped_modules.add(module_rel)
    if skipped_modules:
        items[:] = keep
        print(f"impact: skipped {len(skipped_modules)} modules (unchanged closure), "
              f"running {len(keep)}")


@pytest.hookimpl
def pytest_runtest_logreport(report):
    st = _STATE
    if not st.get("cache_path"):
        return
    module_part = report.nodeid.split("::")[0]
    if not module_part:
        return
    m = st["module_stats"].setdefault(
        module_part, {"ran": False, "failed": False, "skipped": False})
    if report.when == "call":
        m["ran"] = True
    if report.failed:
        m["failed"] = True
    if report.skipped:
        m["skipped"] = True


@pytest.hookimpl
def pytest_sessionfinish(session, exitstatus):
    st = _STATE
    cache_path = st.get("cache_path")
    backend = st.get("backend")
    root = st.get("root")
    if cache_path is None or backend is None or root is None:
        return
    if not st["module_stats"]:
        return  # nothing ran (e.g. --collect-only) -> leave the cache untouched
    cache = _load_cache(cache_path)
    closure_cache = st["closure_cache"]
    modified = False
    for module_part, m in st["module_stats"].items():
        mod_path = (backend / module_part).resolve()
        try:
            module_rel = str(mod_path.relative_to(root)).replace("\\", "/")
        except ValueError:
            continue
        if not (m["ran"] and not m["failed"] and not m["skipped"]):
            # Ran but not cleanly passed (or skipped) -> drop any stale pass.
            cache.pop(module_rel, None)
            modified = True
            continue
        info = closure_cache.get(mod_path)
        if info is None:
            info = closure_info(mod_path, backend)
        module_sha, closure_sha, _rel = info
        cache[module_rel] = {"pass_hashes": [module_sha, closure_sha]}
        modified = True
    if modified:
        _write_cache(cache_path, cache)
