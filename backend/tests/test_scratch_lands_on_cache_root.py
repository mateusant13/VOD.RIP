"""Scratch staging must land on cache_root(), never on a full system temp.

G:\ is 100% full on this box (0.00 GB free), and tempfile.mkdtemp() puts the
fragment/mux staging there. Downloads died mid-flight with
"[Errno 28] No space left on device" until _tmpdir() started pinning scratch to
cache_root() - the drive the app already picks for throwaway data because
"headroom beats speed".

RED: revert _tmpdir to a bare tempfile.mkdtemp(prefix) and test_pins_scratch_to_the_cache_root
fails, because the temp file lands on the system temp again.
"""
import ast
import tempfile
from pathlib import Path

from services import ytdlp_hls

BACKEND = Path(__file__).resolve().parents[1]


def test_there_are_no_bare_mkdtemp_calls_left():
    """RED: any tempfile.mkdtemp( left outside the helper is a disk-full landmine."""
    src = (BACKEND / "services" / "ytdlp_hls.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "mkdtemp":
            # allowed only inside _tmpdir itself (the fallback path)
            enc = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_tmpdir"][0]
            assert any(n is node for n in ast.walk(enc)), (
                "bare tempfile.mkdtemp at line %d - scratch would land on the"
                " system temp (G: is full on this box)" % node.lineno
            )


def test_pins_scratch_to_the_cache_root(monkeypatch):
    """RED: revert _tmpdir to a bare mkdtemp and this fails."""
    # NOTE: no tmp_path - the system temp is on a FULL drive on this box, so
    # pytest's own fixture cannot allocate. Scratch goes under the repo tmp/.
    root = BACKEND.parent / "tmp" / "scratch-test-cache"
    if root.exists():
        import shutil

        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    monkeypatch.setattr("services.settings.cache_root", lambda: root)
    d = ytdlp_hls._tmpdir("probe_")
    try:
        assert Path(d).parent == root, "scratch went to %s, not %s" % (d, root)
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


def test_falls_back_to_the_system_temp_when_there_is_no_cache_root(monkeypatch):
    """The fallback must still work on a host with no cache root."""
    monkeypatch.setattr("services.settings.cache_root", lambda: None)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(BACKEND.parent / "tmp"))
    d = ytdlp_hls._tmpdir("probe_")
    try:
        assert Path(d).exists()
        assert Path(d).parent == BACKEND.parent / "tmp"
    finally:
        import shutil

        shutil.rmtree(d, ignore_errors=True)


def test_explicit_dir_wins_over_the_cache_root(monkeypatch):
    """The HLS clip path passes dir= so a cancel can wipe its staging.

    RED: drop the ``dir`` parameter (the shape that shipped before) and the
    call site raises ``TypeError: _tmpdir() got an unexpected keyword
    argument 'dir'`` — which is exactly how the instant-preview path stayed
    disabled while every channel logged a skip.
    """
    import shutil

    cache = BACKEND.parent / "tmp" / "scratch-test-cache-root"
    out = BACKEND.parent / "tmp" / "scratch-test-output"
    for p in (cache, out):
        shutil.rmtree(p, ignore_errors=True)
        p.mkdir(parents=True)
    monkeypatch.setattr("services.settings.cache_root", lambda: cache)
    d = ytdlp_hls._tmpdir("hls_clip_", dir=str(out))
    try:
        assert Path(d).parent == out, "dir= was ignored: %s (cache root %s)" % (d, cache)
    finally:
        shutil.rmtree(out, ignore_errors=True)
        shutil.rmtree(cache, ignore_errors=True)

