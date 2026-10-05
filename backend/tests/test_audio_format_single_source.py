"""One audio-only format spec, and no caller may re-inline its own.

SIBLING, NOT A DUPLICATE. ``test_audio_only_format_spec.py`` owns the
BEHAVIOUR: it runs the app's real opts through yt-dlp's real format selector
over a format list captured from a real production extract, and it covers
``archive_ytdlp.download_bestaudio``. This file owns the SOURCE: that the
three audio call sites all read one definition and cannot quietly drift back
to the unguarded spelling. The two sites the sibling does not reach —
the HLS audio section and the UI "audio only" download — are exactly the
ones that still carried ``bestaudio/best`` when this was written.

THE DEFECT THIS PINS. Three call sites wanted audio and each spelled the
selector itself. Two of them used the raw ``bestaudio/best``, whose ``/`` is a
fallback CHAIN ending in ``best`` = "best format containing BOTH video and
audio". So when an extract returns no audio-only itag, those two silently
downloaded a muxed progressive stream and fed the video bitrate to a speech
recogniser.

MEASURED on this box over 10 real YouTube extracts: 0 of 10 offered any
audio-only itag, and ffprobe on what was actually fetched showed h264
640x360 @ 579,939 bps next to aac @ 48,022 bps — about 12x the bytes for a
speech-to-text job, with no error and no warning. A fix applied to one caller
therefore left the other two still doing it, which is why the selector now
lives in one module.

These tests scan the source rather than importing the constants on purpose: an
import-based test would keep passing if someone re-inlined the literal into a
caller and left the shared module unused, which is exactly the drift that
produced the original split.
"""
from __future__ import annotations

import ast
from pathlib import Path

from services.audio_format import AUDIO_ONLY_FORMAT_SPEC

_SERVICES = Path(__file__).resolve().parents[1] / "services"
#: Every module that must obtain the selector from the shared module.
_CONSUMERS = (
    "archive_ytdlp.py",
    "ytdlp_hls.py",
    "ytdlp_download.py",
)


# --- the selector itself ----------------------------------------------------

def test_the_tail_cannot_accept_video():
    """`/best` at the tail is the whole defect: it accepts a muxed stream.

    yt-dlp reads `/` as a fallback chain, so a trailing bare `best` means the
    chain still succeeds on a video-bearing format. The tail must be
    constrained to no-video AND real-audio, so a list without audio raises and
    the worker requeues instead of paying 12x for a transcript.
    """
    head, _, tail = AUDIO_ONLY_FORMAT_SPEC.partition("/")
    assert head == "bestaudio"
    assert tail != "best", "a bare '/best' tail re-admits muxed video"
    assert "vcodec=none" in tail, "the tail must refuse any video codec"
    assert "acodec!=none" in tail, (
        "a vcodec=none-only tail could hand the transcriber an sb0-sb3 mhtml "
        "storyboard, which is also vcodec=none"
    )


def test_the_spec_is_exactly_the_one_measured() -> None:
    """Pin the literal, so a well-meaning edit has to be argued for."""
    assert AUDIO_ONLY_FORMAT_SPEC == "bestaudio/best[vcodec=none][acodec!=none]"


# --- no caller may re-inline it ---------------------------------------------

def _string_literals(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


def test_no_service_module_re_inlines_the_unguarded_selector() -> None:
    """The drift guard: no `"bestaudio/best"` string literal anywhere.

    This is the assertion that would have caught the original split. Scanning
    source text is deliberate — an import-based check would still pass while a
    caller carried its own unguarded literal.
    """
    offenders: list[str] = []
    for path in sorted(_SERVICES.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "audio_format.py":
            continue
        for literal in _string_literals(path):
            # Only the exact unguarded chain. `bestvideo+bestaudio/best` is a
            # different, correct selector: it asks for video on purpose.
            if literal.strip() == "bestaudio/best":
                offenders.append(f"{path.relative_to(_SERVICES.parent).as_posix()}")
    assert offenders == [], (
        f"these modules spell the unguarded selector themselves: {offenders}. "
        f"Import AUDIO_ONLY_FORMAT_SPEC from services.audio_format instead — a "
        f"bare '/best' tail falls through to a muxed video stream."
    )


def test_every_consumer_imports_the_shared_spec() -> None:
    """The three audio call sites must all read the one definition."""
    missing: list[str] = []
    for name in _CONSUMERS:
        path = _SERVICES / name
        assert path.is_file(), f"expected consumer {name} to exist"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "services.audio_format":
                if any(a.name == "AUDIO_ONLY_FORMAT_SPEC" for a in node.names):
                    imported = True
        if not imported:
            missing.append(name)
    assert missing == [], (
        f"{missing} do not import AUDIO_ONLY_FORMAT_SPEC. A caller that spells "
        f"its own selector is how the unguarded '/best' chain came back."
    )


def test_the_shared_module_is_importable_without_pulling_in_yt_dlp() -> None:
    """It is a leaf so all three can import it without an import cycle.

    `services.audio_format` must stay dependency-free: it is imported by
    `archive_ytdlp`, `ytdlp_hls` and `ytdlp_download`, and a future import of
    any of those here would close a cycle that is awkward to debug.
    """
    tree = ast.parse((_SERVICES / "audio_format.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(("services", "yt_dlp")), (
                    f"audio_format.py must stay a leaf, found import {alias.name}"
                )
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("services"), (
                f"audio_format.py must stay a leaf, found import from {node.module}"
            )
