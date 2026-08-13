"""Self-check for the --impact test-selection plugin (tests/impact_plugin.py).

Proves the plugin end to end, hermetically — no real git diff, no network, no
torch/transformers:

  1. closure mapping against the REAL repo files: test_archive_watchdog.py
     imports both services.archive_db and services.archive_watchdog, so a
     change to either marks it affected (the spec's draft scenario assumed
     archive_db.py would NOT affect it — it does, and the plugin must not
     skip it); test_archive_phonetic_search.py imports services.archive_db;
  2. subprocess runs with a pre-seeded "passed" cache: with
     VODRIP_CHANGED_FILES=services/archive_db.py, --impact skips the module
     whose closure does NOT contain it (seed_clean) and runs the ones that do
     (seed_db directly, seed_aff transitively through archive_watchdog's
     function-local ``from services import archive_db``);
  3. flipping VODRIP_CHANGED_FILES to services/archive_watchdog.py flips the
     skip set (seed_db + seed_clean skipped, seed_aff runs);
  4. without --impact everything runs and the plugin is inert;
  5. broken git (GIT_DIR pointing nowhere) degrades to a no-op that runs
     everything.

At import time this module redirects VODRIP_IMPACT_CACHE to a fresh temp file
(collection reads it lazily) so this file's own --impact runs are never
skipped and never touch backend/.impact-cache.json.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.impact_plugin import _find_repo_root, closure_info, should_skip

os.environ["VODRIP_IMPACT_CACHE"] = str(
    Path(tempfile.mkdtemp(prefix="impact-selfcheck-")) / "cache.json")

_ROOT = _find_repo_root()
_BACKEND = _ROOT / "backend"
_SCRATCH = _BACKEND / "tests" / "_impact_scratch"
_SELF = Path(__file__).resolve()

_FILES = {
    "seed_clean.py": (
        '"""Impact selfcheck scratch — no repo imports (always skippable)."""\n'
        "import os\n\n\n"
        "def test_seed_clean():\n"
        "    assert os.name  # trivial: proves the module ran\n"),
    "seed_aff.py": (
        '"""Impact selfcheck scratch — depends on the archive watchdog service."""\n'
        "from services import archive_watchdog  # noqa: E402\n\n\n"
        "def test_seed_aff():\n"
        "    assert callable(archive_watchdog.start_archive_watchdog)\n"),
    "seed_db.py": (
        '"""Impact selfcheck scratch — depends on the archive DB service."""\n'
        "from services import archive_db  # noqa: E402\n\n\n"
        "def test_seed_db():\n"
        "    assert callable(archive_db.upsert_video)\n"),
}


def _rel(path: Path) -> str:
    return str(path.relative_to(_ROOT)).replace("\\", "/")


def _seed_cache(cache_path: Path) -> None:
    """Seed a cache that marks every scratch module as previously passing."""
    cache = {}
    for name in _FILES:
        module_sha, closure_sha, _rel_paths = closure_info(_SCRATCH / name, _BACKEND)
        cache[_rel(_SCRATCH / name)] = {"pass_hashes": [module_sha, closure_sha]}
    cache_path.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")


def _pytest(args: list[str], changed: str | None = None,
            impact: bool = True, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if changed is not None:
        env["VODRIP_CHANGED_FILES"] = changed
    else:
        env.pop("VODRIP_CHANGED_FILES", None)  # fall through to real git
    if extra_env:
        env.update(extra_env)
    cmd = [sys.executable, "-m", "pytest", *args]
    if impact:
        cmd.insert(3, "--impact")
    return subprocess.run(cmd, cwd=_BACKEND, env=env,
                          capture_output=True, text=True, timeout=240)


@pytest.fixture(scope="module", autouse=True)
def _scratch_dir():
    _SCRATCH.mkdir(parents=True, exist_ok=True)
    for name, src in _FILES.items():
        (_SCRATCH / name).write_text(src, encoding="utf-8")
    yield
    shutil.rmtree(_SCRATCH, ignore_errors=True)


# ---------------------------------------------------------------------------
# in-process unit checks (fast, no subprocess)
# ---------------------------------------------------------------------------

def test_closure_mapping_real_files():
    _m, _c, rel = closure_info(_BACKEND / "tests" / "test_archive_watchdog.py", _BACKEND)
    for expected in ("backend/tests/test_archive_watchdog.py",
                     "backend/services/archive_db.py",
                     "backend/services/archive_watchdog.py",
                     "backend/services/chat_sinks/base.py"):
        assert expected in rel, expected
    _m, _c, rel_phon = closure_info(
        _BACKEND / "tests" / "test_archive_phonetic_search.py", _BACKEND)
    assert "backend/services/archive_db.py" in rel_phon
    # A plugin change must re-run this selfcheck.
    _m, _c, rel_self = closure_info(_SELF, _BACKEND)
    assert "backend/tests/impact_plugin.py" in rel_self


def test_real_watchdog_is_affected_by_archive_db_change():
    """archive_db.py is INSIDE test_archive_watchdog's closure (it imports it),
    so changing archive_db.py must NOT skip it — a false skip here would hide
    real breakage."""
    wd = _BACKEND / "tests" / "test_archive_watchdog.py"
    cache = {}
    module_sha, closure_sha, _rel_paths = closure_info(wd, _BACKEND)
    cache[_rel(wd)] = {"pass_hashes": [module_sha, closure_sha]}
    assert should_skip(wd, {"services/archive_db.py"}, cache, _BACKEND) is False
    # A changed file OUTSIDE its closure + cached-passing -> skippable.
    # (archive_retention is now IN the closure: archive_watchdog ->
    #  chat_sinks.kick_pusher -> archive_kick -> archive_retention — a
    #  retention change legitimately must not be skipped.)
    _m, _c, rel = closure_info(wd, _BACKEND)
    assert "backend/services/app_lifecycle.py" not in rel
    assert should_skip(wd, {"services/app_lifecycle.py"}, cache, _BACKEND) is True


# ---------------------------------------------------------------------------
# subprocess runs through the real pytest hooks
# ---------------------------------------------------------------------------

def test_impact_skips_only_unaffected_modules():
    _seed_cache(Path(os.environ["VODRIP_IMPACT_CACHE"]))
    scratch = [f"tests/_impact_scratch/{n}" for n in _FILES]
    r = _pytest([*scratch, "-v"], changed="services/archive_db.py", impact=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "impact: skipped 1 modules (unchanged closure), running 2" in r.stdout, r.stdout
    assert "test_seed_db PASSED" in r.stdout, r.stdout
    assert "test_seed_aff PASSED" in r.stdout, r.stdout
    assert "test_seed_clean" not in r.stdout, r.stdout
    assert "2 passed" in r.stdout, r.stdout


def test_impact_flip_changed_file_flips_skip_set():
    _seed_cache(Path(os.environ["VODRIP_IMPACT_CACHE"]))
    scratch = [f"tests/_impact_scratch/{n}" for n in _FILES]
    r = _pytest([*scratch, "-v"], changed="services/archive_watchdog.py", impact=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "impact: skipped 2 modules (unchanged closure), running 1" in r.stdout, r.stdout
    assert "test_seed_aff PASSED" in r.stdout, r.stdout
    assert "test_seed_db" not in r.stdout, r.stdout
    assert "test_seed_clean" not in r.stdout, r.stdout
    assert "1 passed" in r.stdout, r.stdout


def test_without_impact_plugin_is_inert():
    scratch = [f"tests/_impact_scratch/{n}" for n in _FILES]
    r = _pytest([*scratch, "-v"], changed="services/archive_db.py", impact=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "impact:" not in r.stdout, r.stdout
    assert "3 passed" in r.stdout, r.stdout
    for name in _FILES:
        assert f"test_{name[:-3]} PASSED" in r.stdout, r.stdout


def test_broken_git_degrades_to_noop():
    scratch = [f"tests/_impact_scratch/{n}" for n in _FILES]
    r = _pytest([*scratch, "-v"], changed=None, impact=True,
                extra_env={"GIT_DIR": str(_SCRATCH / "no-such-git")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "impact:" not in r.stdout, r.stdout
    assert "3 passed" in r.stdout, r.stdout
