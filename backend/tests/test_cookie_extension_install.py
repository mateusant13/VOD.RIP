"""Cookie extension drag-and-drop install flow (source materializer + launcher)."""
import io
import json
import os
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from routers.cookie_bridge import (
    _ext_src_dir,
    _find_browser,
    _materialize_ext_src,
    _open_extension_manager,
    _ext_version,
)


def _fake_crx(tmp_path, *, name="ext.crx") -> "object":
    """Build a CRX3-shaped file: Cr24 header + zip payload (real appdata crx is the same)."""
    manifest = {"name": "Get cookies.txt LOCALLY", "version": "0.7.2", "manifest_version": 3}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("background.mjs", "export {}")
        z.writestr("_metadata/computed_hashes.json", '{"skip":"me"}')
        z.writestr("popup.html", "<html></html>")
    zip_bytes = buf.getvalue()
    crx = tmp_path / name
    crx.write_bytes(b"Cr24" + (3).to_bytes(4, "little") + (0).to_bytes(4, "little") + zip_bytes)
    return crx


def test_materialize_extracts_unpacked_source(monkeypatch, tmp_path):
    crx = _fake_crx(tmp_path)
    monkeypatch.setenv("VODRIP_EXT_CRX", str(crx))
    src = _materialize_ext_src()
    assert (src / "manifest.json").exists()
    assert (src / "background.mjs").exists()
    # _metadata is Chrome bookkeeping and must not ship into an unpacked load
    assert not (src / "_metadata").exists()
    assert _ext_version() == "0.7.2"


def test_materialize_is_idempotent_and_skips_zip_slip(monkeypatch, tmp_path):
    crx = _fake_crx(tmp_path)
    monkeypatch.setenv("VODRIP_EXT_CRX", str(crx))
    first = _materialize_ext_src()
    # rebuild the crx with a zip-slip entry — extractor must drop it
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("manifest.json", json.dumps({"version": "0.7.2"}))
        z.writestr("../../escape.txt", "nope")
    crx.write_bytes(b"Cr24" + (3).to_bytes(4, "little") + (0).to_bytes(4, "little") + inner.getvalue())
    second = _materialize_ext_src()
    assert second == first
    assert not (second.parent.parent / "escape.txt").exists()
    assert not (second / "escape.txt").exists()


def test_source_dir_under_appdata(tmp_path):
    # conftest pins VODRIP_APP_DATA to a scratch root — src lives under it
    assert str(_ext_src_dir()).startswith(os.environ["VODRIP_APP_DATA"])
    assert _ext_src_dir().name == "VOD.RIP-cookies"
    assert _ext_src_dir().parent.name == "cookie-extension"


def test_materialize_writes_drag_note(monkeypatch, tmp_path):
    crx = _fake_crx(tmp_path)
    monkeypatch.setenv("VODRIP_EXT_CRX", str(crx))
    src = _materialize_ext_src()
    note = src.parent / "drag this folder above.txt"
    assert note.exists()
    text = note.read_text(encoding="utf-8")
    assert "VOD.RIP-cookies" in text and "drag" in text.lower()
    # idempotent — a second materialize keeps the note
    assert _materialize_ext_src() == src
    assert note.exists()


def test_reveal_opens_parent_folder(monkeypatch, tmp_path):
    import asyncio

    from routers import cookie_bridge as cb

    crx = _fake_crx(tmp_path)
    monkeypatch.setenv("VODRIP_EXT_CRX", str(crx))
    opened = []
    monkeypatch.setattr(cb.os, "startfile", lambda p: opened.append(p))
    res = asyncio.run(cb.session_cookies_extension_reveal())
    parent = cb._materialize_ext_src().parent
    assert res["ok"] is True
    assert opened == [str(parent)]
    assert res["extension_dir"] == str(parent)


def test_find_browser_prefers_program_files(monkeypatch, tmp_path):
    pf = tmp_path / "PF"
    (pf / "Google" / "Chrome" / "Application").mkdir(parents=True)
    exe = pf / "Google" / "Chrome" / "Application" / "chrome.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv("ProgramFiles", str(pf))
    assert _find_browser("chrome") == exe
    assert _find_browser("definitely-not-a-browser") is None


def test_open_extension_manager_launches_chrome(monkeypatch):
    from routers import cookie_bridge as cb

    fake = type("FakePath", (), {"__str__": lambda self: "C:/chrome.exe"})()
    monkeypatch.setattr(cb, "_find_browser", lambda name: fake if name == "chrome" else None)
    monkeypatch.setattr(cb, "_activate_extension_tab", lambda: None)
    launched = {}
    with patch.object(subprocess, "Popen") as popen:
        res = _open_extension_manager()
    assert res["launched"] is True
    assert res["browser"] == "chrome"
    assert res["url"] == "chrome://extensions/"
    assert res["reused"] is False
    popen.assert_called_once()


def test_open_extension_manager_reuses_open_tab(monkeypatch):
    from routers import cookie_bridge as cb

    monkeypatch.setattr(cb, "_activate_extension_tab", lambda: ("reused", None))
    with patch.object(subprocess, "Popen") as popen:
        res = _open_extension_manager()
    assert res["launched"] is True
    assert res["reused"] is True
    assert res["url"] is None
    popen.assert_not_called()


def test_open_extension_manager_drives_running_browser(monkeypatch):
    from routers import cookie_bridge as cb

    monkeypatch.setattr(cb, "_activate_extension_tab", lambda: ("chrome", "chrome://extensions/"))
    with patch.object(subprocess, "Popen") as popen:
        res = _open_extension_manager()
    assert res["launched"] is True
    assert res["reused"] is False
    assert res["browser"] == "chrome"
    assert res["url"] == "chrome://extensions/"
    popen.assert_not_called()


def test_open_extension_manager_no_browser_returns_false(monkeypatch):
    from routers import cookie_bridge as cb

    monkeypatch.setattr(cb, "_find_browser", lambda name: None)
    monkeypatch.setattr(cb, "_activate_extension_tab", lambda: None)
    res = _open_extension_manager()
    assert res["launched"] is False
    assert res["url"] is None
    assert res["reused"] is False


def test_activate_extension_tab_runs_bundled_script(monkeypatch, tmp_path):
    from routers import cookie_bridge as cb

    # The bundled ps1 must exist — it is the whole point of the helper.
    script = Path(__file__).resolve().parent.parent / "scripts" / "focus_extension_tab.ps1"
    assert script.is_file()

    calls = {}
    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["timeout"] = kwargs.get("timeout")
        return type("R", (), {"returncode": 0, "stdout": b""})()
    monkeypatch.setattr(cb.subprocess, "run", fake_run)
    assert cb._activate_extension_tab() == ("reused", None)
    assert calls["cmd"][-2:] == ["-File", str(script)]
    assert calls["timeout"] == 25

    # exit 2 = the active tab was driven in the reported browser
    monkeypatch.setattr(cb.subprocess, "run",
                        lambda cmd, **kwargs: type("R", (), {"returncode": 2, "stdout": b"chrome\n"})())
    assert cb._activate_extension_tab() == ("chrome", "chrome://extensions/")

    # unknown browser on drive -> unusable, caller must not spawn
    monkeypatch.setattr(cb.subprocess, "run",
                        lambda cmd, **kwargs: type("R", (), {"returncode": 2, "stdout": b"weird\n"})())
    assert cb._activate_extension_tab() == ("blocked", None)

    # exit 1 = no browser running at all -> fresh spawn is safe
    def fake_run_fail(cmd, **kwargs):
        return type("R", (), {"returncode": 1, "stdout": b""})()
    monkeypatch.setattr(cb.subprocess, "run", fake_run_fail)
    assert cb._activate_extension_tab() == ("none", None)

    # exit 3 = browser running but undrivable -> blocked, NEVER spawn
    monkeypatch.setattr(cb.subprocess, "run",
                        lambda cmd, **kwargs: type("R", (), {"returncode": 3, "stdout": b""})())
    assert cb._activate_extension_tab() == ("blocked", None)

    # a transient exit-3 is retried once before giving up
    seq = [type("R", (), {"returncode": 3, "stdout": b""})(),
           type("R", (), {"returncode": 2, "stdout": b"chrome\n"})()]
    monkeypatch.setattr(cb.subprocess, "run", lambda cmd, **kwargs: seq.pop(0))
    assert cb._activate_extension_tab() == ("chrome", "chrome://extensions/")

    monkeypatch.setattr(cb.subprocess, "run", lambda cmd, **kwargs: (_ for _ in ()).throw(OSError()))
    assert cb._activate_extension_tab() == ("blocked", None)


def test_open_extension_manager_never_spawns_into_running_browser(monkeypatch):
    from routers import cookie_bridge as cb

    # Browser running but undrivable: the button must report failure instead
    # of spawning chrome.exe (which yields a stray new-tab page, never the URL).
    monkeypatch.setattr(cb, "_find_browser", lambda name: "C:/chrome.exe" if name == "chrome" else None)
    monkeypatch.setattr(cb, "_activate_extension_tab", lambda: ("blocked", None))
    with patch.object(subprocess, "Popen") as popen:
        res = _open_extension_manager()
    assert res["launched"] is False
    assert res["url"] is None
    popen.assert_not_called()


def test_activate_extension_tab_missing_script_returns_none(monkeypatch, tmp_path):
    from routers import cookie_bridge as cb

    monkeypatch.setattr(Path, "is_file", lambda self: False)
    assert cb._activate_extension_tab() is None
