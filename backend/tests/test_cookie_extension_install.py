"""Cookie extension drag-and-drop install flow (source materializer + launcher)."""
import io
import json
import os
import subprocess
import zipfile
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
    assert _ext_src_dir().name == "src"
    assert _ext_src_dir().parent.name == "cookie-extension"


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
    launched = {}
    with patch.object(subprocess, "Popen") as popen:
        res = _open_extension_manager()
    assert res["launched"] is True
    assert res["browser"] == "chrome"
    assert res["url"] == "chrome://extensions/"
    popen.assert_called_once()


def test_open_extension_manager_no_browser_returns_false(monkeypatch):
    from routers import cookie_bridge as cb

    monkeypatch.setattr(cb, "_find_browser", lambda name: None)
    res = _open_extension_manager()
    assert res["launched"] is False
    assert res["url"] is None
