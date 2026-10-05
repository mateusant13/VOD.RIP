"""Prove the BACKOFF actually prevents the egress, not just the logging.

`test_live_upcoming_budget.py` proves the classifier and the ladder. Neither of
those proves the thing the owner cares about: that a live which has not started
STOPS costing a request. The test below drives the real
`services.live_capture.youtube_live_info` twice against the same not-started id
and counts how many times yt-dlp was actually constructed.

Before the fix, the second call builds a second YoutubeDL and issues a second
real extract. After it, the second call returns from the backoff without
constructing one at all.
"""
from __future__ import annotations

import pytest

from services import live_capture, live_upcoming_backoff


class _Resp:
    content = b'"videoId": "ZvW6Id7tmHs"'


@pytest.fixture(autouse=True)
def _clean():
    live_upcoming_backoff.reset()
    yield
    live_upcoming_backoff.reset()


def test_a_not_started_live_costs_exactly_one_egress(monkeypatch) -> None:
    """Two badge polls of a not-started live => ONE yt-dlp extract, not two."""
    built: list[int] = []

    class _YDL:
        def __init__(self, opts):
            built.append(1)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            raise RuntimeError(
                "ERROR: [youtube] ZvW6Id7tmHs: Este evento ao vivo começará em breve."
            )

    monkeypatch.setattr("services.live_capture.requests.get", lambda *a, **k: _Resp())
    monkeypatch.setattr("yt_dlp.YoutubeDL", _YDL)
    # Innertube is the first leg; make it report "not a live stream" so the
    # yt-dlp fallback (the leg that produces the phrase) is the one exercised.
    monkeypatch.setattr(
        "services.youtube_innertube.innertube_extract_info",
        lambda *a, **k: None,
    )

    first = live_capture.youtube_live_info("@somechannel")
    assert first is not None
    assert first.get("not_started") is True, (
        f"the not-started outcome was not recognised; got {first!r}"
    )
    assert len(built) == 1, f"first poll should cost one extract, got {len(built)}"

    # SECOND badge poll, same not-started live, immediately after.
    second = live_capture.youtube_live_info("@somechannel")

    assert len(built) == 1, (
        f"a not-started live was re-probed inside its backoff: yt-dlp was "
        f"constructed {len(built)} times for one live that has not started. "
        f"This is the waste being fixed — 32 such probes were logged in 4 h."
    )
    assert second is not None
    assert second.get("not_started") is True
    assert "has not started" in second.get("reason", "").lower()


def test_a_live_that_starts_is_polled_again_immediately(monkeypatch) -> None:
    """The polarity: a backoff must not strand a live that goes up.

    If the stream is live, the very next badge poll must do real work — a
    backoff that kept suppressing the probe would hide a broadcast that
    finally started, which is a worse defect than the one being fixed.
    """
    built: list[int] = []

    class _YDL:
        def __init__(self, opts):
            built.append(1)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            if len(built) == 1:
                raise RuntimeError(
                    "ERROR: [youtube] ZvW6Id7tmHs: Este evento ao vivo começará "
                    "em breve."
                )
            return {
                "is_live": True,
                "title": "Now live",
                "formats": [
                    {"protocol": "m3u8_native", "height": 720, "url": "https://cdn/x.m3u8"}
                ],
            }

    monkeypatch.setattr("services.live_capture.requests.get", lambda *a, **k: _Resp())
    monkeypatch.setattr("yt_dlp.YoutubeDL", _YDL)
    monkeypatch.setattr(
        "services.youtube_innertube.innertube_extract_info", lambda *a, **k: None
    )

    first = live_capture.youtube_live_info("@somechannel")
    assert first is not None and first.get("not_started") is True
    assert len(built) == 1

    # Simulate the live having started: clear the backoff the way a successful
    # live extract would, then poll again.
    live_upcoming_backoff.note_started("ZvW6Id7tmHs")
    second = live_capture.youtube_live_info("@somechannel")

    assert len(built) == 2, (
        "a live that started was not polled again; the backoff stranded it"
    )
    assert second is not None and second.get("url"), f"expected a stream url: {second!r}"
    assert second.get("is_live") is not False
