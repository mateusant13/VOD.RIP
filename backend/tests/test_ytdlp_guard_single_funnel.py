"""The yt-dlp guard is the ONLY place a real ``yt_dlp.YoutubeDL`` may be built.

``services/ytdlp_guard.py:211`` states the contract: "Only supported way to
construct YoutubeDL". Four sites in the tree build the real extractor anyway:

  * ``services/live_capture.py::_twitch_live_info_ytdlp_fallback``  raw ``yt_dlp.YoutubeDL(...)``
  * ``services/live_capture.py::youtube_live_info``                 raw ``yt_dlp.YoutubeDL(...)``
  * ``services/chat_sinks/yt_live.py::_make_ydl``                   raw ``yt_dlp.YoutubeDL(...)``
  * ``services/ytdlp_hls.py::ytdlp_section_mux_to_ts``             ``from yt_dlp import YoutubeDL``

Each one skips all four things the guard does on the way in:
``assert_ytdlp_safe()`` (the getpot_wpc plugin block), ``sanitize_ytdlp_opts``,
the process-wide extract lock, and ``rl_counter.count_request("youtube")`` —
the last of which is the number the YouTube rate-limit history in ``yt_gate``
reports and any future adaptive throttle would calibrate on, so a bypass makes
that history UNDERCOUNT.

Why ``test_guard_binding_seam.py`` cannot see any of this: its repo-wide scan
looks for a binding of the two ``guarded_youtube_dl*`` NAMES. Bypassing the
guard does not bind those names — it skips the module entirely and imports
``yt_dlp`` itself. That is the vacuity hole, and it had already admitted four
live offenders. This file is the missing property.

It is a RATCHET, not a reprieve: the four sites are pinned as
``_KNOWN_UNCONVERTED`` rather than deleted, so a FIFTH fails immediately and a
converted site has to be removed from the list in the same commit. Nothing here
is skipped, xfailed or weakened.

Detection is deliberately not spelling-specific. The naive grep for
``yt_dlp.YoutubeDL(`` finds three of the four and MISSES
``ytdlp_hls.py::ytdlp_section_mux_to_ts``, which binds the constructor as a bare
name — the exact
"a name that looks unused in one grep is not proof" trap. The scan below accepts
ANY call to a ``YoutubeDL`` attribute plus any import of the name, so an alias
(``import yt_dlp as y``) is caught too.

Run from backend/: python -m pytest tests/test_ytdlp_guard_single_funnel.py
"""
from __future__ import annotations

import ast
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_GUARD_MODULE = "services/ytdlp_guard.py"

# The four sites that build the real extractor outside the guard, as
# "path::enclosing_function". KEEP THIS EMPTY-ISH AND SHRINKING. Converting a
# site means routing it through ytdlp_guard.guarded_youtube_dl(...) and
# deleting its entry here in the same commit; leaving a converted entry behind
# fails the ratchet so the list cannot quietly stop being true.
#
# WHY NOT "path:line" — measured, and it made the ratchet lie in both
# directions. Merging the priority gate shifted three of these sites without
# converting any of them (live_capture.py 328->330 and 530->572,
# ytdlp_hls.py 4325->4329), and a line key reported that as THREE stale
# entries plus THREE brand-new offenders. Nothing had actually changed. A
# ratchet that fires on unrelated edits is a ratchet a lane learns to
# wave through, and the first thing waved through would be a real fifth
# escape. The enclosing function survives any edit above it, and a genuine
# new construction still fails wherever it lands.
_KNOWN_UNCONVERTED = frozenset(
    {
        "services/chat_sinks/yt_live.py::_make_ydl",
        "services/live_capture.py::_twitch_live_info_ytdlp_fallback",
        "services/live_capture.py::youtube_live_info",
        "services/ytdlp_hls.py::ytdlp_section_mux_to_ts",
    }
)

# Where a direct construction is allowed: the guard itself, and nothing else.
_ALLOWED = frozenset({_GUARD_MODULE})


def _source_files() -> list[Path]:
    """Production modules only.

    Deliberately ``services/`` + ``routers/`` + the backend top level rather
    than a recursive walk of the whole backend: ``backend/models`` and
    ``backend/tests`` hold no production call sites, and a full walk of
    ``backend/`` also has to cross the model/data trees, which is slow and
    irrelevant to this contract.
    """
    out: list[Path] = []
    for sub in ("services", "routers"):
        out.extend(p for p in (_BACKEND_ROOT / sub).rglob("*.py") if "__pycache__" not in p.parts)
    out.extend(_BACKEND_ROOT.glob("*.py"))
    return sorted(out)


def _rel(path: Path) -> str:
    return path.relative_to(_BACKEND_ROOT).as_posix()


_MODULE_SCOPE = "<module>"


def _enclosing_qualname(path: Path, tree: ast.Module, lineno: int) -> str:
    """The dotted name of the scope a line sits in, outermost first.

    A construction is identified by WHERE IT LIVES, not by the line it happens
    to occupy. A line key is invalidated by any edit above it, which is what
    made this ratchet report three converted-looking entries and three
    phantom new offenders when the priority-gate merge moved three sites
    without touching a single one of them.
    """
    scopes: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = node.end_lineno or node.lineno
            if node.lineno <= lineno <= end:
                scopes.append((node.lineno, node.name))
    if not scopes:
        return _MODULE_SCOPE
    scopes.sort()
    return ".".join(name for _, name in scopes)


def _direct_constructions(path: Path) -> set[str]:
    """Scopes that build the real extractor without the guard.

    Catches both spellings of the escape:
      * ``yt_dlp.YoutubeDL(...)`` — an attribute on any receiver, so an
        alias (``import yt_dlp as y`` → ``y.YoutubeDL(...)``) is still caught;
      * ``from yt_dlp import YoutubeDL`` — the bare-name binding that a
        grep for ``yt_dlp.YoutubeDL(`` misses entirely.

    It matches the ATTRIBUTE, not just a call of it, on purpose. A name
    bound first and called later (``Ctor = yt_dlp.YoutubeDL`` … ``Ctor(opts)``)
    constructs the extractor while every ``ast.Call`` in the file has a plain
    ``Name`` callee — undetectable by the call-shaped scan. Measured on this
    tree: zero such bindings exist today, so catching them costs no false
    positives and closes a hole rather than widening a contract.
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    linenos: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "YoutubeDL":
            linenos.add(node.lineno)
        if isinstance(node, ast.ImportFrom) and (node.module or "") == "yt_dlp":
            for alias in node.names:
                if alias.name == "YoutubeDL":
                    linenos.add(node.lineno)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.name or "").split(".")[-1] == "YoutubeDL":
                    linenos.add(node.lineno)
    return {_enclosing_qualname(path, tree, ln) for ln in linenos}


def _measured() -> set[str]:
    found: set[str] = set()
    for path in _source_files():
        if _rel(path) in _ALLOWED:
            continue
        for qualname in _direct_constructions(path):
            found.add(f"{_rel(path)}::{qualname}")
    return found


def test_only_the_guard_constructs_the_real_extractor():
    """A FIFTH direct construction fails here.

    The message names the remedy because the honest options are narrow: route
    the call through ``ytdlp_guard.guarded_youtube_dl(...)`` (which also gets it
    the plugin check, the option sanitiser, the extract lock and the
    ``rl_counter`` increment), or — if the site genuinely cannot take the lock —
    say so in a comment AND add the entry to ``_KNOWN_UNCONVERTED`` so it stays
    visible instead of invisible.
    """
    measured = _measured()
    new_offenders = measured - _KNOWN_UNCONVERTED
    assert new_offenders == set(), (
        f"yt-dlp constructed outside the guard at {sorted(new_offenders)}; "
        f"{_GUARD_MODULE} is the only supported constructor (it also runs "
        f"assert_ytdlp_safe(), sanitize_ytdlp_opts, the extract lock and the "
        f"rl_counter increment). Route it through "
        f"ytdlp_guard.guarded_youtube_dl(...) or add it to "
        f"_KNOWN_UNCONVERTED with a reason."
    )


def test_the_known_unconverted_list_is_not_stale():
    """The other half of the ratchet, so the list cannot rot into fiction.

    A site that has been converted must be REMOVED from
    ``_KNOWN_UNCONVERTED`` in the same commit. Otherwise the escape stops being
    reported and the list silently becomes a list of things nobody fixed.
    """
    measured = _measured()
    stale = _KNOWN_UNCONVERTED - measured
    assert stale == set(), (
        f"_KNOWN_UNCONVERTED lists {sorted(stale)}, which no longer construct "
        f"the extractor directly — they were converted. Delete those entries; "
        f"a stale entry hides a real escape."
    )


def test_the_pinned_count_matches_the_documented_four():
    """A tripwire on the list itself, so a blanket-allowlist edit is visible.

    If someone empties ``_KNOWN_UNCONVERTED`` to make the suite green, the four
    real escapes come back and the count check fires. Cheap, and it turns
    "someone widened the allowlist" from invisible into a failure.
    """
    assert len(_KNOWN_UNCONVERTED) == 4, (
        f"_KNOWN_UNCONVERTED has {len(_KNOWN_UNCONVERTED)} entries, expected 4 "
        f"({sorted(_KNOWN_UNCONVERTED)}). Changing the count is a real change "
        f"to the guard's coverage — convert a site, or document why a new one "
        f"is acceptable, and say so in the commit."
    )


# --- the ratchet tests ITSELF, so a "fix" that weakens it is visible --------

def test_the_identity_of_a_site_survives_unrelated_edits_above_it(tmp_path):
    """A line key made this ratchet lie; prove the function key does not.

    This is the exact failure that had to be repaired: inserting unrelated
    lines ABOVE a construction used to change its identity, so the ratchet
    reported the known site as stale AND a new offender at the same time. A
    lane cannot read that as "a real escape appeared", and a ratchet that
    cries wolf on every merge is a ratchet that stops being read.
    """
    body = "def _make_ydl(opts):\n    return yt_dlp.YoutubeDL(opts)\n"
    short = tmp_path / "short.py"
    short.write_text("import yt_dlp\n" + body, encoding="utf-8")

    padded = tmp_path / "padded.py"
    padded.write_text(
        "import yt_dlp\n"
        + "# an unrelated comment\n" * 40
        + "X = 1\n" * 20
        + body,
        encoding="utf-8",
    )

    assert _direct_constructions(short) == _direct_constructions(padded) == {"_make_ydl"}
    # And the two really are at different lines, so this is not vacuous.
    short_line = next(
        n.lineno
        for n in ast.walk(ast.parse(short.read_text(encoding="utf-8")))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "YoutubeDL"
    )
    padded_line = next(
        n.lineno
        for n in ast.walk(ast.parse(padded.read_text(encoding="utf-8")))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "YoutubeDL"
    )
    assert short_line != padded_line


def test_a_new_construction_in_a_new_function_is_still_caught(tmp_path):
    """Re-keying on the function must not soften detection.

    The whole point of the ratchet is that a FIFTH escape fails. If the new
    key accidentally collapsed distinct sites into one, or dropped sites at
    module scope, that protection would be gone while the suite stayed green.
    """
    src = tmp_path / "mod.py"
    src.write_text(
        "import yt_dlp\n"
        "def known():\n"
        "    return yt_dlp.YoutubeDL({})\n"
        "def brand_new():\n"
        "    return yt_dlp.YoutubeDL({})\n"
        "LEAK = yt_dlp.YoutubeDL\n"
        "def deferred():\n"
        "    return LEAK({})\n",
        encoding="utf-8",
    )
    found = _direct_constructions(src)
    assert found == {"known", "brand_new", _MODULE_SCOPE}, (
        f"expected one identity per distinct scope, got {sorted(found)}"
    )
    # `deferred` must NOT appear: it constructs through a plain Name callee and
    # is invisible to a call-shaped scan. It is the hole this test pins shut —
    # if the scan regressed to calls-only, the module-scope binding above
    # would still be caught but this deferred construction would not.
    assert "deferred" not in found
    assert _direct_constructions(src) == {
        "known",
        "brand_new",
        _MODULE_SCOPE,
    }, "the module-scope binding that defeats a call-shaped scan is missed"


def test_a_nested_method_is_identified_by_its_qualified_name(tmp_path):
    """Renaming or wrapping a site is a real change the ratchet must see."""
    src = tmp_path / "mod.py"
    src.write_text(
        "import yt_dlp\n"
        "class Extractor:\n"
        "    def build(self, opts):\n"
        "        return yt_dlp.YoutubeDL(opts)\n",
        encoding="utf-8",
    )
    assert _direct_constructions(src) == {"Extractor.build"}
