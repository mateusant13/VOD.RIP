"""The window-HLS eviction must match the extension the writer emits.

Measured 2026-10-12: _enforce_window_hls_cache_budget globbed
seg_[0-9][0-9][0-9].ts while the writer emits .m4s (USE_FMP4 defaults ON), so
the glob matched nothing, the function returned on its second line, and the
500 MB WINDOW_HLS_CACHE_MB budget never evicted a byte. The guard existed and
could not fail - the same defect class as a guard nobody calls.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.preview import session as session_mod  # noqa: E402


class _FakeSession:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir


def _writer_ext() -> str:
    """The extension the muxer actually writes, read from the module itself."""
    return ".m4s" if session_mod.USE_FMP4 else ".ts"


def _evict(tmp_path, ext: str, count: int = 4):
    d = tmp_path / "window_hls"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (d / f"seg_{i:03d}{ext}").write_bytes(b"x" * 4096)
    original = session_mod.WINDOW_HLS_CACHE_MB
    session_mod.WINDOW_HLS_CACHE_MB = 0  # any byte on disk exceeds the budget
    try:
        session_mod._enforce_window_hls_cache_budget(_FakeSession(tmp_path))
    finally:
        session_mod.WINDOW_HLS_CACHE_MB = original
    return sorted(p.name for p in d.glob(f"seg_*{ext}"))


def test_the_budget_evicts_the_extension_the_writer_emits(tmp_path):
    """RED on the pre-fix code: the .ts-only glob left every file behind."""
    ext = _writer_ext()
    remaining = _evict(tmp_path, ext)
    assert remaining == [], (
        f"the budget did not evict a single {ext} segment, so the eviction glob "
        f"does not match what the writer emits (USE_FMP4={session_mod.USE_FMP4})"
    )


def test_the_budget_still_evicts_legacy_ts_segments(tmp_path):
    """The control: the fix must not trade one blind glob for another."""
    remaining = _evict(tmp_path, ".ts")
    assert remaining == [], "legacy .ts segments are no longer evicted"


def test_the_evictor_and_the_writer_agree_on_the_extension():
    """The invariant itself, stated once: evictor suffix set contains the writer's."""
    import re

    src = Path(session_mod.__file__).read_text(encoding="utf-8", errors="replace")
    start = src.index("def _enforce_window_hls_cache_budget")
    body = src[start : src.index("def _window_hls_seek_chunk_sec", start)]
    m = re.search(r"p\.suffix in \(([^)]*)\)", body)
    assert m, "the evictor no longer filters by suffix - re-derive this test"
    matched = {s.strip().strip('"') for s in m.group(1).split(",")}
    assert _writer_ext() in matched, (
        f"the writer emits {_writer_ext()} but the evictor only matches {matched}"
    )
