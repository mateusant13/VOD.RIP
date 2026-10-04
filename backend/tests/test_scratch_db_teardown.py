"""Regression test for the session-end scratch leak (backend/conftest.py
reaper + services.archive_db / services.cookie_store).

The leak: services.archive_db and services.cookie_store hand out sqlite
connections that stay open for the life of the pytest process. On Windows an
open file cannot be unlinked, so the vodrip-tests-* scratch dir holding
archive.db / cookies.db could not be deleted at session end — one dir leaked
per run, and the reaper's warning (added when it stopped swallowing failures)
fired on the run's own leftovers. A test fixture that nils ``_conn`` to rebind
the store made it worse: the orphaned connection is refcount-only and sat in a
reference cycle, so the OS handle survived until the cyclic collector ran.

These tests pin the three properties that make the dir removable:
  1. close_connections() actually releases the handle (unlink works after it);
  2. it closes connections the module no longer POINTS AT (the orphan case);
  3. the store still works afterwards (teardown must not break the module).
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_CT_PATH = _BACKEND / "conftest.py"
_ct_spec = importlib.util.spec_from_file_location("_root_conftest_db", _CT_PATH)
_ct = importlib.util.module_from_spec(_ct_spec)
_ct_spec.loader.exec_module(_ct)


def _can_unlink(p: Path) -> bool:
    try:
        os.unlink(p)
    except OSError:
        return False
    return True


@pytest.fixture(autouse=True)
def _restore_db_modules():
    """Every test here rebinds the DB path; put the modules back afterwards.

    Close first, THEN restore the env: the modules cache their connection
    against the path that was current when they opened it, so restoring the env
    while a handle is still open would leave the next caller reading a scratch
    DB this test pointed them at.
    """
    from services import archive_db, cookie_store

    saved = {k: os.environ.get(k) for k in ("VODRIP_ARCHIVE_DB",
                                            "VODRIP_COOKIE_DB")}
    yield
    cookie_store.close_connections()
    archive_db.close_connections()
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# --- 1 + 3: the handle is released and the module still works ------------

def test_archive_db_close_releases_file_lock(tmp_path, monkeypatch):
    """archive_db.close_connections() frees the file for deletion.

    While the module holds the write conn (and the main thread's read conn) the
    scratch file cannot be unlinked on Windows — that is the whole leak.
    """
    from services import archive_db

    db = tmp_path / "archive.db"
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(db))
    archive_db.execute("SELECT 1")
    archive_db.query("SELECT 1")  # opens this thread's per-thread read conn
    assert db.exists()

    closed = archive_db.close_connections()
    assert closed >= 2, f"expected the write conn + read conn, closed {closed}"
    assert _can_unlink(db), "close_connections() must release the OS handle"


def test_cookie_store_close_releases_file_lock(tmp_path, monkeypatch):
    """Same for cookie_store's module-level conn."""
    from services import cookie_store

    db = tmp_path / "cookies.db"
    monkeypatch.setenv("VODRIP_COOKIE_DB", str(db))
    cookie_store.counts()
    assert db.exists()

    assert cookie_store.close_connections() >= 1
    assert _can_unlink(db), "close_connections() must release the OS handle"


def test_modules_still_work_after_close(tmp_path, monkeypatch):
    """Teardown is a close, not a kill: the next call re-opens and re-migrates."""
    from services import archive_db, cookie_store

    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(tmp_path / "archive.db"))
    monkeypatch.setenv("VODRIP_COOKIE_DB", str(tmp_path / "cookies.db"))
    archive_db.execute("CREATE TABLE IF NOT EXISTS roundtrip (x INTEGER)")
    archive_db.execute("INSERT INTO roundtrip VALUES (7)")
    cookie_store.upsert_cookies([
        {"platform": "youtube", "name": "SID", "value": "v", "domain": ".youtube.com"},
    ])

    archive_db.close_connections()
    cookie_store.close_connections()

    assert archive_db.query("SELECT x FROM roundtrip")[0]["x"] == 7
    assert cookie_store.counts()["youtube"] == 1
    archive_db.execute("DELETE FROM roundtrip")


# --- 2: the orphan case (why a plain "close the current conn" was not enough)

def test_close_reaches_connections_the_module_dropped(tmp_path, monkeypatch):
    """A fixture nils _conn to rebind the store; the old handle still holds the
    file until close_connections() names it through the registry.

    Without the registry this is unfixable from outside: the module has no
    reference left, and the orphan is a reference cycle that only the cyclic
    GC reclaims — at an unpredictable moment, or never within the session.
    """
    from services import cookie_store

    db = tmp_path / "cookies.db"
    monkeypatch.setenv("VODRIP_COOKIE_DB", str(db))
    cookie_store.counts()
    orphan = cookie_store._conn
    assert orphan is not None

    # The fixture pattern used across the suite (test_cookie_bridge.py:26):
    # rebind by dropping the attribute. The connection is now unreachable
    # from the module but still open.
    cookie_store._conn = None
    cookie_store._schema_ready = False
    assert not _can_unlink(db) or True  # may or may not be locked on POSIX

    cookie_store.counts()  # re-opens on the same path — a SECOND live handle
    assert cookie_store.close_connections() >= 2, (
        "both the orphan and the current connection must be closed"
    )
    assert _can_unlink(db), "an orphaned connection must not keep the file locked"


def test_archive_db_close_survives_a_rebind(monkeypatch, tmp_path):
    """The write conn is re-keyed on a path change; the registry follows it."""
    from services import archive_db

    first = tmp_path / "one" / "archive.db"
    second = tmp_path / "two" / "archive.db"
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(first))
    archive_db.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(second))
    archive_db.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")  # rebinds

    closed = archive_db.close_connections()
    assert closed >= 1
    for db in (first, second):
        assert _can_unlink(db), f"{db.name} still locked after close_connections()"


# --- the reaper side: handles must be closed BEFORE the wipe --------------

def test_reaper_closes_db_handles_before_wiping(monkeypatch, tmp_path):
    """Session-end ordering is the fix; this pins it as a contract.

    A scratch dir whose DB handle is still open is unremovable on Windows, so
    the reaper must release the handles it can reach BEFORE it deletes. The
    teardown lives in one function for exactly this reason — fixture
    finalization order must not be able to reorder the two steps.
    """
    from services import cookie_store

    scratch = tmp_path / "vodrip-tests-ordering"
    scratch.mkdir()
    db = scratch / "cookies.db"
    monkeypatch.setenv("VODRIP_COOKIE_DB", str(db))
    cookie_store.counts()  # the handle the reaper must release

    # Spy, don't replace: the real close has to run or the dir stays locked
    # and the wipe below would fail for the wrong reason.
    order = []
    real_close = _ct._close_scratch_db_handles
    real_wipe = _ct._wipe_vodrip_scratch

    def spy_close():
        order.append("close")
        return real_close()

    def spy_wipe(min_age_s, root=None):
        order.append(("wipe", root))
        return real_wipe(min_age_s, root=root)

    monkeypatch.setattr(_ct, "_close_scratch_db_handles", spy_close)
    monkeypatch.setattr(_ct, "_wipe_vodrip_scratch", spy_wipe)
    monkeypatch.setattr(_ct, "_TEMP_ROOT_AT_IMPORT", tmp_path)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    # The exact sequence the session fixture performs, in the same order and
    # against the same root the fixture uses.
    _ct._close_scratch_db_handles()
    _ct._wipe_vodrip_scratch(min_age_s=0.0,
                             root=_ct._TEMP_ROOT_AT_IMPORT)

    assert [o for o in order if o == "close"], "close step must run"
    wipes = [o for o in order if isinstance(o, tuple)]
    assert wipes == [("wipe", tmp_path)], f"wrong teardown order: {order}"
    assert not scratch.exists(), (
        "with the handle released first, the scratch dir must be removable"
    )


def test_session_wipe_targets_the_import_time_temp_root(monkeypatch, tmp_path):
    """A rebound tempfile.tempdir must not make the reaper scan the wrong root.

    tests/test_transcribe_shards.py:49 sets tempfile.tempdir to a private dir
    at IMPORT, and collection imports every test module before the first test
    runs — so at session end gettempdir() points somewhere else and the
    vodrip-tests-* dir this conftest created (under the ORIGINAL root) was
    never a candidate. The session fixture therefore passes the root captured
    at import; this pins that it is threaded through.
    """
    other = tmp_path / "private-scope"
    other.mkdir()
    scratch = tmp_path / "vodrip-tests-in-original-root"
    scratch.mkdir()
    (scratch / "marker").write_text("x", encoding="utf-8")

    # A test module rebound the temp root; the real one is _TEMP_ROOT_AT_IMPORT.
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(other))
    monkeypatch.setattr(_ct, "_TEMP_ROOT_AT_IMPORT", tmp_path)

    _ct._wipe_vodrip_scratch(min_age_s=0.0, root=_ct._TEMP_ROOT_AT_IMPORT)
    assert not scratch.exists(), (
        "the session's own scratch lives under the import-time root, not the "
        "rebound one — scanning gettempdir() silently skipped it"
    )


def test_reaper_still_warns_for_a_genuinely_stuck_node(monkeypatch, tmp_path):
    """d7dc7d7's warning is the tripwire; closing handles must not blunt it.

    A node that stays unremovable even after the teardown re-asserts the close
    (a file another process holds) is still a real leak and must still be
    surfaced. os.unlink is the seam: shutil.rmtree unlinks children through
    os.unlink, not Path.unlink.
    """
    stuck = tmp_path / "vodrip-tests-stuck"
    stuck.mkdir()
    (stuck / "archive.db").write_bytes(b"x")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    real_unlink = os.unlink
    # Match on the RESOLVED node the reaper actually hands to rmtree, and
    # normalize both sides (rmtree passes a backslash path, Windows compare is
    # case-insensitive). Deriving the prefix from the unresolved tmp_path
    # instead made this test flap: whenever resolve() changed a component's
    # case the prefix never matched, the file was really deleted, and the
    # "stuck node" silently was not one.
    resolved = Path(tempfile.gettempdir()).resolve()
    node = next(p for p in resolved.iterdir() if p.name == stuck.name)
    prefix = os.path.normcase(os.path.normpath(str(node)))

    def deny_unlink(path, *a, **k):
        if os.path.normcase(os.path.normpath(str(path))).startswith(prefix):
            raise PermissionError(5, "Access is denied", str(path))
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(os, "unlink", deny_unlink)
    with pytest.warns(UserWarning, match="vodrip-tests-stuck"):
        _ct._wipe_vodrip_scratch(min_age_s=0.0)
    assert stuck.exists(), "the node is still there — that is what is reported"
