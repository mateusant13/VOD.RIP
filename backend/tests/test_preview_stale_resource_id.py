"""Stale synthetic-master resource IDs must self-heal ONLY when the session's own
master text names them (gpt finding 2026-10-12, HIGH).
"""
import pytest

from services.preview.session import (
    PreviewSession,
    _current_variant_for_session,
    _resource_id,
    _is_synthetic_resource_id,
)


def _session(master_lines, entries):
    sess = PreviewSession(
        session_id="a" * 32,
        vod_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        master_url="/api/preview/hls/%s/master.m3u8" % ("a" * 32),
        entry_url="https://rr2---sn.googlevideo.com/videoplayback?x=1",
        platform="YouTube",
        kind="hls",
        prefer_height=360,
    )
    sess.custom_master = NL.join(master_lines) + NL
    sess.variant_entries = list(entries)
    sess.resource_map = {}
    return sess


NL = "\n"


def test_unknown_hex_id_is_not_served():
    """RED arm: an ID the master never names must NOT buy media."""
    sess = _session(
        [
            "#EXTM3U",
            "#EXT-X-STREAM-INF:BANDWIDTH=100,RESOLUTION=640x360",
            "/api/preview/hls/%s/resource?id=%s" % ("a" * 32, "b" * 16),
        ],
        [(360, "https://current-360")],
    )
    assert _is_synthetic_resource_id("f" * 16)
    assert _current_variant_for_session(sess, "f" * 16) is None


def test_master_named_stale_id_resolves_current_url():
    """GREEN arm: the height the master promised, at the CURRENT URL."""
    sess = _session(
        [
            "#EXTM3U",
            "#EXT-X-STREAM-INF:BANDWIDTH=100,RESOLUTION=640x360",
            "/api/preview/hls/%s/resource?id=%s" % ("a" * 32, "c" * 16),
            "#EXT-X-STREAM-INF:BANDWIDTH=9999,RESOLUTION=3840x2160",
            "/api/preview/hls/%s/resource?id=%s" % ("a" * 32, "d" * 16),
        ],
        [(360, "https://current-360"), (2160, "https://current-2160")],
    )
    assert _current_variant_for_session(sess, "c" * 16) == "https://current-360"
    assert _current_variant_for_session(sess, "d" * 16) == "https://current-2160"


def test_promised_height_gone_falls_back_to_nearest_current():
    sess = _session(
        [
            "#EXTM3U",
            "#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1280x720",
            "/api/preview/hls/%s/resource?id=%s" % ("a" * 32, "e" * 16),
        ],
        [(360, "https://current-360"), (1080, "https://current-1080")],
    )
    got = _current_variant_for_session(sess, "e" * 16)
    assert got in ("https://current-360", "https://current-1080")


def test_no_master_text_means_no_healing():
    sess = _session(["#EXTM3U"], [(360, "https://current-360")])
    assert _current_variant_for_session(sess, "c" * 16) is None


def test_resource_id_is_a_stable_digest_of_the_url():
    sess = _session(["#EXTM3U"], [])
    first = _resource_id(sess, "https://googlevideo/videoplayback?x=1")
    assert first == _resource_id(sess, "https://googlevideo/videoplayback?x=1")
    assert len(first) == 16