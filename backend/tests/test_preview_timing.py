"""Preview timing log helper."""
from services.preview_timing import log_preview_timing, _platform_label


def test_platform_label():
    assert _platform_label("youtube") == "YouTube"
    assert _platform_label("Kick") == "Kick"


def test_log_preview_timing_smoke(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="VOD.RIP.preview_timing")
    log_preview_timing(
        platform="youtube",
        surface="main",
        event="first_playable",
        open_ms=1234.5,
        session_id="abcd1234efgh5678",
        detail="test",
    )
    assert any("PREVIEW_TIMING" in r.message for r in caplog.records)
    assert any("open_ms=1234" in r.message for r in caplog.records)


def test_safe_stdout_write_handles_non_encodable_chars():
    """The console writer must not raise on chars the Windows cp1252 console
    can't encode (e.g. the U+2192 arrow), which previously crashed /api/preview/timing."""
    from services.preview_timing import _safe_stdout_write

    # Should not raise even though '→' is unencodable on a cp1252 stdout.
    _safe_stdout_write("PREVIEW_TIMING event=seek_ms=12 → arrow")
    _safe_stdout_write("plain line")

