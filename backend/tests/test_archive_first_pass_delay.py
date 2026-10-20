"""The archive scheduler's first pass must not start in the app's first minutes.

Measured 2026-10-20 on the live app, after a dev restart:

    20:37:46  API up (boot complete)
    20:38:06  archive scheduler ingest begins - Twitch GQL + yt-dlp for 39
              channels, YouTube bot-gate cooldowns, chat backfill
    20:44:xx  the owner opens the UI; requests reach :7897 and stall
    20:45:0x  /api/health silent for 71 s -> dev-all restarts the API under the
              open page; the page keeps its empty state because every /api call
              it made returned 500

The scheduler was started with ``delay=_BOOT_WARM_GRACE_SEC`` (8 s) - a value
that exists for other warm work, not for a pass that is minutes of yt-dlp. A
pass now waits ``_ARCHIVE_FIRST_PASS_DELAY_SEC`` (150 s) so opening the app is
never the moment the archive starts extracting.

These pins are deliberately about the WIRING, not the number: the delay the app
passes must be the archive-specific one, and it must be long enough to cover an
open-and-look window. A channel add still kicks a pass immediately through
``kick_scheduler_pass``, which is why the delay cannot simply be "the cadence".
"""
from __future__ import annotations

import pathlib
import re

APP_PY = pathlib.Path(__file__).resolve().parents[1] / "app.py"


def _app_source() -> str:
    return APP_PY.read_text(encoding="utf-8")


def test_the_archive_boot_delay_is_an_open_and_look_window_not_a_warm_grace():
    from app import _ARCHIVE_FIRST_PASS_DELAY_SEC

    assert _ARCHIVE_FIRST_PASS_DELAY_SEC >= 60.0, (
        "the first archive pass is minutes of yt-dlp/ASR; 8 s puts it in the "
        "same window as the owner opening the app (measured 2026-10-20)"
    )


def test_the_scheduler_is_started_with_its_own_delay_not_the_warm_grace():
    src = _app_source()
    call = re.search(r"start_archive_scheduler\(([^)]*)\)", src)
    assert call, "the archive scheduler start call vanished - re-check the boot path"
    assert "_ARCHIVE_FIRST_PASS_DELAY_SEC" in call.group(1), (
        "the scheduler must use the archive-specific boot delay; passing "
        "_BOOT_WARM_GRACE_SEC (8 s) is the bug this pins"
    )


def test_the_delay_can_be_reverted_by_env(monkeypatch):
    """A knob nobody can turn off is a hard rule; this one stays reversible."""
    src = _app_source()
    assert "VODRIP_ARCHIVE_FIRST_PASS_DELAY" in src
