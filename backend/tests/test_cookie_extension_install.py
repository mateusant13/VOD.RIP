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


def _fake_run(returncode: int, stdout: str = ""):
    return lambda cmd, **kwargs: type("R", (), {"returncode": returncode, "stdout": stdout.encode()})


def test_open_extension_manager_drives_new_tab(monkeypatch):
    from routers import cookie_bridge as cb

    monkeypatch.setattr(cb.subprocess, "run", _fake_run(0, "chrome\n"))
    with patch.object(subprocess, "Popen") as popen:
        res = _open_extension_manager()
    assert res == {"launched": True, "browser": "chrome", "url": "chrome://extensions/"}
    # the ps1 drives the running browser — no process spawn here
    popen.assert_not_called()


def test_open_extension_manager_spawns_fresh_instance_when_none_running(monkeypatch):
    from routers import cookie_bridge as cb

    fake = type("FakePath", (), {"__str__": lambda self: "C:/Program Files/Google/Chrome/Application/chrome.exe"})()
    monkeypatch.setattr(cb, "_find_browser", lambda name: fake if name == "chrome" else None)
    monkeypatch.setattr(cb.subprocess, "run", _fake_run(1, "none\n"))
    with patch.object(subprocess, "Popen") as popen:
        res = _open_extension_manager()
    assert res == {"launched": True, "browser": "chrome", "url": "chrome://extensions/"}
    # no singleton to drop the URL when nothing is running — plain spawn opens it
    assert popen.call_args.args[0] == ["C:/Program Files/Google/Chrome/Application/chrome.exe", "chrome://extensions/"]
    popen.assert_called_once()


def test_open_extension_manager_blocked_when_drive_fails(monkeypatch):
    from routers import cookie_bridge as cb

    monkeypatch.setattr(cb.subprocess, "run", _fake_run(2, "chrome\n"))
    with patch.object(subprocess, "Popen") as popen:
        res = _open_extension_manager()
    assert res == {"launched": False, "browser": None, "url": None, "blocked": True}
    popen.assert_not_called()


def test_open_extension_manager_falls_through_to_next_browser(monkeypatch):
    from routers import cookie_bridge as cb

    paths = {"chrome": "C:/chrome.exe", "msedge": "C:/msedge.exe"}
    monkeypatch.setattr(cb, "_find_browser", lambda name: paths.get(name))
    monkeypatch.setattr(cb.subprocess, "run", _fake_run(1, "none\n"))
    launched = []

    def fake_popen(cmd, **kwargs):
        if cmd[0] == "C:/chrome.exe":
            raise OSError("boom")
        launched.append(cmd)
        return object()

    with patch.object(subprocess, "Popen", side_effect=fake_popen) as popen:
        res = _open_extension_manager()
    assert res == {"launched": True, "browser": "msedge", "url": "edge://extensions/"}
    assert launched == [["C:/msedge.exe", "edge://extensions/"]]


def test_open_extension_manager_no_browser_returns_false(monkeypatch):
    from routers import cookie_bridge as cb

    monkeypatch.setattr(cb, "_find_browser", lambda name: None)
    monkeypatch.setattr(cb.subprocess, "run", _fake_run(1, "none\n"))
    res = _open_extension_manager()
    assert res == {"launched": False, "browser": None, "url": None}
