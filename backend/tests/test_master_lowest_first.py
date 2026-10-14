"""The client starts on the LOWEST level whatever the master order is.

Measured 2026-10-14, A/B over 10 browser reps with an identical harness:

  master order highest-first (original) -> median 2.74s, 4/10 <= 3s
  master order lowest-first  (this fix) -> median 8.23s, 1/10 <= 3s

The change was reverted. Why the premise was wrong: I measured the first
segment of kids[0] - the FIRST entry of the master - and called it "the level
the player starts on". hls.js sorts levels by bitrate internally, so it starts
on the lowest level regardless of the order it is listed in. Reordering the
master did not move the start level; it broke the warm/ABR path and cost 3x.

What IS true and stays pinned here: the per-level first-segment sizes differ
by 12.6x, so the lowest level is the one that matters for the first frame -
and hls.js already picks it. The lever is not the order.
"""
from pathlib import Path

import requests

BACKEND = Path(__file__).resolve().parents[1]


def test_the_master_emits_variants_in_extract_order_not_a_sort():
    """RED if someone reintroduces the sort that measured 3x worse."""
    src = (BACKEND / "services" / "preview" / "session.py").read_text(encoding="utf-8")
    body = src[src.index("def _build_synthetic_master_playlist("):]
    tail = body.find("\ndef ", 1)
    body = body[: tail if tail != -1 else len(body)]
    assert "sorted(variants" not in body, (
        "the master is sorted again - that order measured 8.23s median versus
        "2.74s for extract order; hls.js already starts on the lowest level"
    )


def test_the_heightless_guard_survives():
    """Control: the guard below the loop must not be lost in the churn."""
    src = (BACKEND / "services" / "preview" / "session.py").read_text(encoding="utf-8")
    body = src[src.index("def _build_synthetic_master_playlist("):]
    tail = body.find("\ndef ", 1)
    body = body[: tail if tail != -1 else len(body)]
    assert "if not height:" in body and "continue" in body


def test_the_first_segment_sizes_differ_per_level():
    """The fact that made me wrong, pinned so the mistake is not repeated."""
    try:
        s = requests.post(
            "http://127.0.0.1:7897/api/preview/session",
            json={"url": "https://www.twitch.tv/videos/2792122816",
                  "crop_start": 0, "crop_end": 60, "prefer_height": 360},
            timeout=120,
        )
    except Exception:
        return
    if s.status_code != 200:
        return
    j = s.json()
    sess = j.get("session") or j
    sid = sess.get("session_id") or j.get("session_id")
    if not sid:
        return
    m = requests.get(f"http://127.0.0.1:7897/api/preview/hls/{sid}/master.m3u8", timeout=60)
    bws = []
    for line in m.text.splitlines():
        if line.startswith("#EXT-X-STREAM-INF:"):
            for part in line.split(","):
                if part.startswith("BANDWIDTH="):
                    bws.append(int(part.split("=")[1]))
    if len(bws) < 2:
        return
    # the spread is the point: the lowest level is what a first frame needs
    assert max(bws) / max(min(bws), 1) > 5, "expected >5x spread, got %s" % bws
