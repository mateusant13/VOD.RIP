"""The yt-dlp guard is the ONLY place a real ``yt_dlp.YoutubeDL`` may be built.

``services/ytdlp_guard.py:211`` states the contract: "Only supported way to
construct YoutubeDL". Four sites in the tree build the real extractor anyway:

  * ``services/live_capture.py:328``          raw ``yt_dlp.YoutubeDL(...)``
  * ``services/live_capture.py:530``          raw ``yt_dlp.YoutubeDL(...)``
  * ``services/chat_sinks/yt_live.py:502``    raw ``yt_dlp.YoutubeDL(...)``
  * ``services/ytdlp_hls.py:4325``            ``from yt_dlp import YoutubeDL``

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
``ytdlp_hls.py:4325``, which binds the constructor as a bare name — the exact
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
# "path:line". KEEP THIS EMPTY-ISH AND SHRINKING. Converting a site means
# routing it through ytdlp_guard.guarded_youtube_dl(...) and deleting its entry
# here in the same commit; leaving a converted entry behind fails the ratchet so
# the list cannot quietly stop being true.
_KNOWN_UNCONVERTED = frozenset(
    {
        "services/live_capture.py:328",
        "services/live_capture.py:530",
        "services/chat_sinks/yt_live.py:502",
        "services/ytdlp_hls.py:4325",
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


def _direct_constructions(path: Path) -> list[int]:
    """Lines that build the real extractor without the guard.

    Catches both spellings of the escape:
      * ``yt_dlp.YoutubeDL(...)`` — an attribute call on any receiver, so an
        alias (``import yt_dlp as y`` → ``y.YoutubeDL(...)``) is still caught;
      * ``from yt_dlp import YoutubeDL`` — the bare-name binding that a
        grep for ``yt_dlp.YoutubeDL(`` misses entirely.
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    hits: list[int] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "YoutubeDL":
                hits.append(node.lineno)
        if isinstance(node, ast.ImportFrom) and (node.module or "") == "yt_dlp":
            for alias in node.names:
                if alias.name == "YoutubeDL":
                    hits.append(node.lineno)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.name or "").split(".")[-1] == "YoutubeDL":
                    hits.append(node.lineno)
    return sorted(hits)


def _measured() -> set[str]:
    found: set[str] = set()
    for path in _source_files():
        if _rel(path) in _ALLOWED:
            continue
        for lineno in _direct_constructions(path):
            found.add(f"{_rel(path)}:{lineno}")
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
