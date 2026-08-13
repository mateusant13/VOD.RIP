"""Autostart (Windows Run key) + quiet background pacing.

Pure-logic tests: the registry helper is validated against a fake winreg
module (no real HKCU writes); the pacing sites read VODRIP_BACKGROUND and
must scale down deterministically.
"""

from __future__ import annotations

import sys
import types

from services import archive_scheduler, archive_transcribe as at


# --- autostart registry helper ---------------------------------------------

def _fake_winreg():
    """In-memory HKCU\\Run key: values dict keyed by name."""
    values: dict = {}

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeWinreg:
        HKEY_CURRENT_USER = "hkcu"
        KEY_SET_VALUE = 1
        REG_SZ = 1

        class _Writable(_Key):
            pass

        class _Readable(_Key):
            pass

        def OpenKey(self, _root, _path, _reserved=0, access=0):
            if access:
                return _FakeWinreg._Writable()
            return _FakeWinreg._Readable()

        def SetValueEx(self, key, name, _reserved, _type, value):
            values[name] = value

        def DeleteValue(self, key, name):
            values.pop(name, None)

        def QueryValueEx(self, key, name):
            if name not in values:
                raise FileNotFoundError(name)
            return values[name], _FakeWinreg.REG_SZ

    return _FakeWinreg(), values


def test_set_autostart_writes_and_removes_run_key(monkeypatch):
    from services import autostart

    fake, values = _fake_winreg()
    monkeypatch.setattr(autostart, "winreg", fake)
    # Swap the WHOLE os/sys module refs — patching the real os.name would
    # corrupt pathlib's flavour decision during pytest teardown.
    monkeypatch.setattr(autostart, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(autostart, "sys", types.SimpleNamespace(frozen=True, executable=r"C:\Apps\VOD-RIP.exe"))

    assert autostart.set_windows_autostart(True) is True
    assert values["VOD.RIP"] == r'"C:\Apps\VOD-RIP.exe" --autostart'
    assert autostart.windows_autostart_enabled() is True

    assert autostart.set_windows_autostart(False) is True
    assert "VOD.RIP" not in values
    assert autostart.windows_autostart_enabled() is False


def test_set_autostart_noop_outside_windows_or_dev(monkeypatch):
    from services import autostart

    fake, values = _fake_winreg()
    monkeypatch.setattr(autostart, "winreg", fake)
    monkeypatch.setattr(autostart, "os", types.SimpleNamespace(name="nt"))
    # dev build (not frozen) — never touch the registry
    monkeypatch.setattr(autostart, "sys", types.SimpleNamespace(frozen=False, executable=r"C:\Python\python.exe"))
    assert autostart.set_windows_autostart(True) is True
    assert values == {}
    # non-Windows — no-op
    monkeypatch.setattr(autostart, "os", types.SimpleNamespace(name="posix"))
    monkeypatch.setattr(autostart, "sys", types.SimpleNamespace(frozen=True, executable=r"/usr/bin/vodrip"))
    assert autostart.set_windows_autostart(True) is True
    assert values == {}


# --- quiet background pacing ------------------------------------------------

def test_background_mode_environment(monkeypatch):
    from services import autostart

    monkeypatch.delenv("VODRIP_BACKGROUND", raising=False)
    assert autostart.background_mode() is False
    monkeypatch.setenv("VODRIP_BACKGROUND", "1")
    assert autostart.background_mode() is True


def test_background_caps_cpu_lanes(monkeypatch):
    """Even a 64-thread box gets 2 lanes in background mode — the threads
    belong to the user's other boot work, not extra model copies."""
    monkeypatch.setenv("VODRIP_BACKGROUND", "1")
    monkeypatch.setattr("os.cpu_count", lambda: 64)
    assert at._cpu_auto_workers() == 2
    monkeypatch.delenv("VODRIP_BACKGROUND")
    assert at._cpu_auto_workers() == 4  # interactive: ladder unchanged


def test_background_widens_youtube_chat_pacing(monkeypatch):
    """The quota-sensitive YouTube chat fetch backs off ~2.5x in background."""
    monkeypatch.setattr(at.archive_db, "worker_live", lambda *a, **k: False)
    monkeypatch.delenv("VODRIP_BACKGROUND", raising=False)
    assert at._youtube_chat_interval() == at._YOUTUBE_CHAT_QUIET_INTERVAL_S
    monkeypatch.setenv("VODRIP_BACKGROUND", "1")
    assert at._youtube_chat_interval() == at._YOUTUBE_CHAT_QUIET_INTERVAL_S * 2.5
    # active-lane case (user at the keyboard): 30 -> 75
    monkeypatch.setattr(at.archive_db, "worker_live", lambda *a, **k: True)
    assert at._youtube_chat_interval() == at._YOUTUBE_CHAT_ACTIVE_INTERVAL_S * 2.5


def test_background_scheduler_budgets_and_cadence(monkeypatch):
    """Quiet mode: 1 YT ingest + 1 transcribe per pass, 6-min cadence."""
    monkeypatch.delenv("VODRIP_BACKGROUND", raising=False)
    assert archive_scheduler._yt_ingest_budget() == archive_scheduler.YOUTUBE_INGEST_PER_PASS
    assert archive_scheduler._transcribe_budget() == archive_scheduler.TRANSCRIBE_QUEUE_PER_PASS
    assert archive_scheduler._pass_interval() == archive_scheduler.PASS_INTERVAL_SEC

    monkeypatch.setenv("VODRIP_BACKGROUND", "1")
    assert archive_scheduler._yt_ingest_budget() == 1
    assert archive_scheduler._transcribe_budget() == 1
    assert archive_scheduler._pass_interval() == 360.0
