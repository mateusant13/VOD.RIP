"""Oracle tests for the lock-contract lint (backend/scripts/lock_lint.py).

The lint runs on any revision of download_manager.py as a standalone module
(pure stdlib ast — no project imports), so here we drive it as a subprocess
against two fixtures:

  * the current tip  -> must be clean (exit 0, 0 hard N1/N2/N4), with N3 soft
                        reports present but never exit-fatal;
  * the pre-fix blob (tests/fixtures/dm_e136310.py, `git show
    e136310:backend/services/download_manager.py`) -> must exit 1 naming
    exactly three hard sites: the N1 nested-acquire at :733, the N2
    lock-taking-call at :650, and the N4 shadowed handler at :691.

The e136310 fixture is the untouched historical blob, so its line numbers are
the oracle's: 733 (nested `with self._lock:`), 650 (a `_notify_sse` call made
while the worker already holds the non-reentrant lock) and 691 (a dead
duplicate `except Exception:` arm shadowed by an earlier `except Exception`).
Exit 1 is driven by ANY of N1/N2/N4 being hard-fatal.
"""
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
LINT = BACKEND / "scripts" / "lock_lint.py"
TIP = BACKEND / "services" / "download_manager.py"
FIXTURE = BACKEND / "tests" / "fixtures" / "dm_e136310.py"


def _run_lint(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(LINT), str(path)],
        capture_output=True,
        text=True,
        cwd=str(BACKEND),
        timeout=60,
    )


@pytest.mark.parametrize("fixture_missing", [False])
def test_lint_fixtures_exist(fixture_missing):
    assert LINT.is_file(), f"lint missing: {LINT}"
    assert TIP.is_file(), f"tip missing: {TIP}"
    assert FIXTURE.is_file(), (
        f"oracle fixture missing: {FIXTURE} (generate via "
        "`git show e136310:backend/services/download_manager.py > "
        "tests/fixtures/dm_e136310.py`)"
    )


def test_tip_is_clean():
    """Current download_manager.py must have zero hard (N1/N2/N4) violations.

    N4 (dead duplicate except arm) is also gone at tip — the F1 dead-arm
    deletion removes it entirely, so it must not appear at all.
    """
    r = _run_lint(TIP)
    assert r.returncode == 0, (
        f"tip lint failed (rc={r.returncode}):\n{r.stdout}\n{r.stderr}"
    )
    assert "hard violations (N1+N2+N4): 0" in r.stdout
    assert "N4 shadowed handler" not in r.stdout, (
        "the F1 dead-arm deletion should have removed the only N4 at tip"
    )
    # N3 is soft/informational only: it must be reported but never be fatal.
    assert "N3 IO in holder" in r.stdout, (
        "tip still performs _db IO under _lock and the lint should say so "
        "(soft ratchet)"
    )


def test_e136310_blob_reports_exactly_oracle_sites():
    """The pre-fix blob must exit 1 naming exactly three hard sites:
    :733 (N1) + :650 (N2) + :691 (N4).

    This is the falsifiable oracle from the research doc: before the nested
    acquire, the in-holder _notify_sse call, and the duplicate except arm were
    fixed, the lint flags those three sites and nothing else among the hard
    rules.
    """
    r = _run_lint(FIXTURE)
    assert r.returncode == 1, (
        f"oracle blob lint should fail with hard violations "
        f"(rc={r.returncode}):\n{r.stdout}"
    )
    assert "N1 nested acquire" in r.stdout and ":733:" in r.stdout, (
        "oracle must report the N1 nested acquire at :733, got:\n" + r.stdout
    )
    assert "N2 lock-taking call in holder" in r.stdout and ":650:" in r.stdout, (
        "oracle must report the N2 in-holder call at :650, got:\n" + r.stdout
    )
    assert "N4 shadowed handler" in r.stdout and ":691:" in r.stdout, (
        "oracle must report the N4 shadowed handler at :691 (dead arm must be "
        "hard, not soft):\n" + r.stdout
    )
    hard = [
        ln for ln in r.stdout.splitlines()
        if " N1 " in ln or " N2 " in ln or " N4 " in ln
    ]
    assert len(hard) == 3, (
        "oracle must report EXACTLY three hard sites (N1 :733 + N2 :650 + "
        "N4 :691), got:\n" + "\n".join(hard)
    )


def test_n3_is_soft_not_fatal_on_blob():
    """N3 (IO under _lock) is reported on the pre-fix blob but never exit-fatal.

    exit code 1 is driven only by hard N1/N2/N4; N3 must only appear in the
    counts and the listing.
    """
    r = _run_lint(FIXTURE)
    hard = r.stdout.split("hard violations (N1+N2+N4):")[1].split(";")[0].strip()
    assert int(hard) == 3
    assert "N3 IO in holder" in r.stdout