"""ONE seam for the yt-dlp guard, and no second binding anywhere in the path.

The defect this pins: ``guarded_youtube_dl`` was bound TWO ways in the same
codebase — ``from services.ytdlp_guard import guarded_youtube_dl`` at module
level in ``archive_ytdlp``, and the SAME import again INSIDE a function body in
``youtube_service`` and inside ``download_bestaudio``. That published a
second, plausible-looking attribute (``archive_ytdlp.guarded_youtube_dl``)
which is not the seam. A test that patched it looked correct — the name is
right there in the import block — but it only intercepts calls resolving
through the module global, so the function-local re-import bypassed it and the
real extractor issued a LIVE NETWORK REQUEST from a unit test. It happened
twice, and the escape is invisible at the call site: both lines read
identically.

Note the asymmetry, or the rule gets applied backwards. Patching
``services.ytdlp_guard`` intercepts a function-local re-import TOO, because the
re-import reads the current module attribute. The canonical seam is safe
against both binding styles; it is the *consumer* attribute that is the trap.

The contract, enforced here:

  1. every consumer reaches the guard through the guard MODULE
     (``ytdlp_guard.guarded_youtube_dl(...)``), never by binding the name;
  2. therefore ``monkeypatch.setattr(ytdlp_guard, "guarded_youtube_dl", stub)``
     is the single seam for the whole process, and one patch intercepts every
     egress — including ones added later;
  3. a patch there really does intercept: driving each production entry point
     with the seam stubbed must reach the stub and must NEVER construct a real
     ``yt_dlp.YoutubeDL`` (which is what reaching YouTube looks like).

Points 1-2 are what fail if someone reintroduces a binding — they are AST
checks, because the reintroduction is invisible to any runtime probe (the
canonical patch still intercepts it, which is exactly why the bug survived
twice). Point 3 is the runtime half: the tripwire in ``seam`` turns a bypassed
seam into a named failure instead of a silent HTTP request, so this file fails
loudly rather than quietly reaching the internet.

Run from backend/: python -m pytest tests/test_guard_binding_seam.py
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from services import (
    archive_ytdlp,
    twitch_gql_service,
    ytdlp_download,
    ytdlp_guard,
    youtube_service,
)
from routers import subtitles as subtitles_router

_GUARD_NAMES = ("guarded_youtube_dl", "guarded_youtube_dl_channel")

# Every module that reaches the guard. They must carry ZERO name bindings.
# The last three were converted from the allowlist below — twitch_gql_service
# was the function-local one, i.e. a live instance of the exact escape this
# file exists to prevent.
_CONSUMED = (
    archive_ytdlp,
    youtube_service,
    ytdlp_download,
    twitch_gql_service,
    subtitles_router,
)

# Known escapes, by path. EMPTY ON PURPOSE: the three entries that used to be
# here (services/twitch_gql_service.py, services/ytdlp_download.py,
# routers/subtitles.py) have been converted, and every converted module is now
# enforced directly in _CONSUMED above.
#
# Keep it empty. A path here is a hole in the repo-wide scan below: that scan is
# what stops a FIFTH offender from appearing in a file nobody was watching, and
# a binding is only allowlisted until someone converts it. Adding an entry here
# needs the AST checks to be run against that module first — which is what the
# conversion did.
_KNOWN_NAME_BINDINGS: set[str] = set()

_BACKEND_ROOT = Path(archive_ytdlp.__file__).resolve().parents[1]


def _rel(path: Path) -> str:
    return path.resolve().relative_to(_BACKEND_ROOT).as_posix()


def _name_bindings(tree: ast.AST) -> list[ast.ImportFrom]:
    """Every `from ...ytdlp_guard import guarded_youtube_dl*`, at ANY scope.

    Deliberately not filtered by scope: a module-level import and a
    function-local one are the same hazard, and the function-local one is the
    one that actually escaped.
    """
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").split(".")[-1] == "ytdlp_guard"
        and any(alias.name in _GUARD_NAMES for alias in node.names)
    ]


# --- 1. no second binding, in the modules this lane unified -----------------

@pytest.mark.parametrize("mod", _CONSUMED, ids=lambda m: m.__name__)
def test_consumer_does_not_bind_the_guard_name(mod):
    """A name binding is a second, invisible seam. None may remain."""
    hits = _name_bindings(ast.parse(Path(mod.__file__).read_text(encoding="utf-8")))
    assert hits == [], (
        f"{mod.__name__} binds a guard name at {[h.lineno for h in hits]}; "
        f"import the module (`from services import ytdlp_guard`) and call "
        f"`ytdlp_guard.<name>(...)` so one patch intercepts it"
    )


@pytest.mark.parametrize("mod", _CONSUMED, ids=lambda m: m.__name__)
def test_every_guard_reference_resolves_through_the_module(mod):
    """Each use must be an ATTRIBUTE read on the module object.

    This is the property that makes the patch work: a module attribute is
    looked up at call time, so a monkeypatch is seen. A bare name is a binding
    captured at import time and is not.
    """
    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    bare = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id in _GUARD_NAMES
    ]
    assert bare == [], (
        f"{mod.__name__} uses a bare guard name at {bare}; those resolve to a "
        f"module-local binding that a patch on services.ytdlp_guard misses"
    )
    attrs = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr in _GUARD_NAMES
    ]
    assert attrs, f"{mod.__name__} is expected to call the guard at least once"
    wrong = [
        n.lineno
        for n in attrs
        if not (isinstance(n.value, ast.Name) and n.value.id == "ytdlp_guard")
    ]
    assert wrong == [], (
        f"{mod.__name__} calls the guard on something other than the module at "
        f"{wrong}; it must be `ytdlp_guard.<name>(...)`"
    )


def test_all_consumers_share_one_seam_object():
    """Same module object everywhere — so one patch covers every consumer."""
    from services import ytdlp_hls

    for mod in (
        archive_ytdlp, youtube_service, ytdlp_hls,
        ytdlp_download, twitch_gql_service, subtitles_router,
    ):
        assert mod.ytdlp_guard is ytdlp_guard, (
            f"{mod.__name__} holds a different guard module"
        )


# --- 2. no NEW name binding anywhere in the backend -------------------------

def test_no_new_module_binds_a_guard_name():
    """Repo-wide: any module outside the known set that binds a guard name
    fails here. This is what stops the escape from being reintroduced by the
    next contributor, in a file nobody was watching."""
    offenders: dict[str, list[int]] = {}
    for path in sorted(_BACKEND_ROOT.rglob("*.py")):
        rel = _rel(path)
        if "tests" in rel.split("/") or rel in _KNOWN_NAME_BINDINGS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - not expected in-tree
            continue
        hits = _name_bindings(tree)
        if hits:
            offenders[rel] = [h.lineno for h in hits]
    assert offenders == {}, (
        f"guard name bound in {offenders}; use `from services import "
        f"ytdlp_guard` + `ytdlp_guard.<name>(...)` so the single seam holds"
    )


# --- 3. the seam really intercepts (no module can reach the network) --------

class _RealExtractorReached(AssertionError):
    """The real yt_dlp.YoutubeDL was constructed — the network was live."""


@pytest.fixture()
def seam(monkeypatch):
    """Patch the ONE seam, and arm a tripwire on the real constructor.

    Any code path that bypasses the seam reaches the real
    ``guarded_youtube_dl``, which constructs ``yt_dlp.YoutubeDL`` — the exact
    line that starts an HTTP request. The tripwire makes that a named failure
    in this process instead of a request to YouTube.
    """
    yt_dlp = pytest.importorskip("yt_dlp")
    seen: list[tuple[str, dict]] = []
    reached: list[str] = []

    class _FakeYdl:
        def extract_info(self, url, download=False):
            return {"id": "VzuPKrGl0z8", "title": "t", "entries": []}

    def _recorder(kind):
        import contextlib

        @contextlib.contextmanager
        def _cm(opts):
            seen.append((kind, opts))
            yield _FakeYdl()

        return _cm

    for name in _GUARD_NAMES:
        monkeypatch.setattr(ytdlp_guard, name, _recorder(name))

    def _boom(self, *a, **kw):
        reached.append("yt_dlp.YoutubeDL")
        raise _RealExtractorReached(
            "the real yt-dlp was constructed — this test reached the network"
        )

    monkeypatch.setattr(yt_dlp.YoutubeDL, "__init__", _boom)
    # The governor has its own test file; silence it here so this file tests
    # the SEAM and nothing else.
    monkeypatch.setattr(archive_ytdlp, "_governor_admit_ytdlp", lambda *a, **k: None)
    monkeypatch.setattr(archive_ytdlp, "_yt_opts", lambda outdir, video_id=None: {})
    monkeypatch.setattr(archive_ytdlp, "_apply_youtube_session", lambda *a, **kw: None)
    return type("Seam", (), {"seen": seen, "reached": reached})()


def _attempt(fn, *a, **kw):
    """Call a production entry point, tolerating its own error handling.

    The point is the SIDE EFFECT (the stub was reached), not the return value:
    several of these deliberately swallow failures, so the recorder — not an
    exception — is the evidence.
    """
    try:
        fn(*a, **kw)
    except Exception:  # noqa: BLE001 — the function's own contract, not ours
        pass


def test_extract_seam_is_reached(seam):
    _attempt(archive_ytdlp.ingest_video, "VzuPKrGl0z8")
    assert seam.seen, "ingest_video never reached the guard seam"
    assert seam.reached == [], "the real extractor was constructed"


def test_bestaudio_seam_is_reached(seam, tmp_path, monkeypatch):
    """The exact call that escaped to YouTube before: download_bestaudio."""
    monkeypatch.setattr(archive_ytdlp, "_audio_resume_dir", lambda vid: tmp_path)
    _attempt(archive_ytdlp.download_bestaudio, "VzuPKrGl0z8", tmp_path)
    assert seam.seen, "download_bestaudio never reached the guard seam"
    assert seam.reached == [], "the real extractor was constructed"


def test_channel_list_seam_is_reached(seam):
    _attempt(archive_ytdlp.list_channel_videos, "https://youtube.com/@chan", limit=3)
    assert seam.seen, "list_channel_videos never reached the guard seam"
    assert seam.reached == [], "the real extractor was constructed"


def test_display_name_seam_is_reached(seam, monkeypatch):
    monkeypatch.setattr(
        archive_ytdlp.archive_db, "youtube_chat_user_ids_without_display_name",
        lambda limit: ["UCaaa"],
    )
    _attempt(archive_ytdlp.resolve_youtube_display_names, limit=1)
    assert seam.seen, "resolve_youtube_display_names never reached the guard seam"
    assert seam.reached == [], "the real extractor was constructed"


def test_a_deliberate_call_attempt_is_intercepted(seam):
    """The guarantee, stated directly: with the seam patched, a call attempt
    is INTERCEPTED — the stub runs and no real extractor is ever built."""
    before = len(seam.seen)
    with ytdlp_guard.guarded_youtube_dl({}) as ydl:
        ydl.extract_info("https://youtu.be/VzuPKrGl0z8", download=False)
    assert len(seam.seen) == before + 1
    assert seam.reached == [], "the real extractor was constructed"
