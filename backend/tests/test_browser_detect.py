"""browser_detect: default HTTPS app, recency fallbacks, auto-install pick."""
import time
from pathlib import Path

from services import browser_detect as bd


def test_progid_mapping():
    assert bd._progid_to_name("FirefoxURL") == "firefox"
    assert bd._progid_to_name("OperaStable") == "opera"
    assert bd._progid_to_name("OperaGXStable") == "opera_gx"
    assert bd._progid_to_name("ChromeHTML") == "chrome"
    assert bd._progid_to_name("MSEdgeHTM") == "msedge"
    assert bd._progid_to_name("BraveHTML") == "brave"


def test_command_exe_extracts_quoted_path(tmp_path):
    exe = tmp_path / "firefox.exe"
    exe.write_bytes(b"")
    assert bd._command_exe(f'"{exe}" -osint -url "%1"') == exe


def test_stale_profile_falls_back_to_hot_root(monkeypatch, tmp_path):
    cold = tmp_path / "cold"
    hot = tmp_path / "hot"
    (cold / "Default").mkdir(parents=True)
    (hot / "Default" / "Network").mkdir(parents=True)
    (cold / "Default" / "History").write_text("old")
    cookie = hot / "Default" / "Network" / "Cookies"
    cookie.write_text("new")
    old = time.time() - (120 * 24 * 3600)
    import os
    os.utime(cold / "Default" / "History", (old, old))

    monkeypatch.setattr(bd, "_chromium_profile_roots", lambda name: [cold, hot] if name == "chrome" else [])
    prof, age = bd.profile_recency("chrome")
    assert prof == hot
    assert age is not None and age < bd.PROFILE_HOT_SEC


def test_score_prefers_default_then_hot():
    default_stale = bd._score(bd.PROFILE_WARM_SEC + 10, is_default=True, is_running=False)
    other_hot = bd._score(60.0, is_default=False, is_running=True)
    assert default_stale > other_hot


def test_pick_uses_highest_score(monkeypatch, tmp_path):
    exe = tmp_path / "opera.exe"
    exe.write_bytes(b"")
    hit = bd.BrowserHit(name="opera", exe=exe, source="default", recency_sec=10, score=130)
    monkeypatch.setattr(bd, "detect_browsers", lambda: [
        bd.BrowserHit(name="chrome", exe=tmp_path / "c.exe", source="install", recency_sec=10, score=10),
        hit,
    ])
    picked = bd.pick_auto_install_browser()
    assert picked and picked.name == "opera"


def test_firefox_ext_src_patches_manifest(tmp_path):
    from routers.cookie_bridge import _firefox_ext_src
    src = tmp_path / "VOD.RIP-cookies"
    src.mkdir()
    (src / "manifest.json").write_text(
        '{"name":"VOD RIP Get Cookies","version":"0.8.32","manifest_version":3,'
        '"background":{"service_worker":"background.js","type":"module"}}',
        encoding="utf-8",
    )
    (src / "background.js").write_text("void 0;", encoding="utf-8")
    (src / "background.html").write_text('<script type="module" src="background.js"></script>', encoding="utf-8")
    dest = _firefox_ext_src(src)
    import json
    man = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert man["background"]["page"] == "background.html"
    assert man["browser_specific_settings"]["gecko"]["id"] == "vodrip-cookies@vod.rip"
