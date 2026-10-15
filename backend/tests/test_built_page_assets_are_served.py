"""Every asset the built index.html asks for must actually be served.

Measured 2026-10-15 on the INSTALLED frozen bundle: every launch logged two
404s - /assets/icon.ico and /assets/icon.png - because build-copy copied only
dist/index.html into backend/static, and the backend mounted /static but had
nothing at /assets. Vite serves public/ at the root in dev, so the defect was
invisible until the frozen app was opened.

It is cosmetic (the favicon) and it is still a defect: two failed requests on
every cold open, for a file the build already produced.

RED ARM: remove the /assets mount from app.py and test_the_backend_serves_assets
fails; remove the cpSync from build-copy and test_build_copy_ships_assets fails.
"""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "backend" / "app.py"
PKG = REPO / "package.json"
HTML = REPO / "index.html"


def test_the_backend_serves_assets():
    """RED: without the mount, /assets/icon.ico is a 404 on every launch."""
    text = APP.read_text(encoding="utf-8")
    assert 'app.mount("/assets"' in text, (
        "app.py must mount /assets from static/assets - index.html asks for"
        " /assets/icon.ico and /assets/icon.png, and the frozen app has no Vite"
        " to serve public/ at the root"
    )


def test_build_copy_ships_assets():
    """RED: copying only index.html drops the icons the page references."""
    scripts = json.loads(PKG.read_text(encoding="utf-8"))["scripts"]
    copy = scripts["build-copy"]
    assert "cpSync" in copy and "dist/assets" in copy, (
        "build-copy must copy dist/assets alongside dist/index.html"
    )


def test_the_page_references_match_what_is_served():
    """The link hrefs and the served prefix agree - no silent path drift."""
    html = HTML.read_text(encoding="utf-8")
    assert '/assets/icon.ico' in html
    assert '/assets/icon.png' in html
    text = APP.read_text(encoding="utf-8")
    assert 'app.mount("/assets"' in text


def test_the_icons_exist_in_public():
    """The build reads these from public/; absent means the copy is a no-op."""
    for name in ("icon.ico", "icon.png"):
        assert (REPO / "public" / "assets" / name).is_file(), (
            "public/assets/%s is what Vite copies into dist/assets" % name
        )


def test_the_spec_ships_the_whole_static_dir():
    """RED: pinning index.html alone drops assets/ at the PyInstaller stage.

    This is the link that was actually broken. build-copy DID produce
    backend/static/assets/, app.py DID mount /assets, and the installed bundle
    still had no icons - because vod-rip.spec listed
    (STATIC_DIR / "index.html", "static") and PyInstaller shipped exactly that
    one file. Fixing the copy and the mount without this changed nothing
    observable, which is what the second build proved.
    """
    spec = (REPO / "vod-rip.spec").read_text(encoding="utf-8")
    assert '(str(H.STATIC_DIR), "static")' in spec, (
        "the spec must ship the whole static dir, not just index.html"
    )
    assert 'H.STATIC_DIR / "index.html"' not in spec, (
        "the single-file entry is the bug: assets/ never reaches the bundle"
    )