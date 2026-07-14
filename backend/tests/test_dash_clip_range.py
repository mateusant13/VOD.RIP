"""YouTube DASH clip byte-range helpers."""
import os
import tempfile

from services.ytdlp_hls import (
    _googlevideo_byte_range,
    _resolve_googlevideo_clen,
    _resolve_googlevideo_dur,
    _dash_video_needs_transcode,
    _local_dash_slice_valid,
)


def test_googlevideo_byte_range_trim_start():
    url = "https://x.googlevideo.com/videoplayback?clen=1000000&dur=100.0"
    b0, b1 = _googlevideo_byte_range(url, 0.0, 30.0)
    assert b0 == 0
    assert 25000 < b1 < 400000


def test_googlevideo_byte_range_fmt_fallback_without_url_clen_dur():
    """Refreshed googlevideo URLs may omit clen=/dur= — use format + vod_duration."""
    url = "https://x.googlevideo.com/videoplayback?itag=136"
    fmt = {"filesize": 2_000_000, "duration": 100.0, "tbr": 1600}
    assert _resolve_googlevideo_clen(url, fmt, 100.0) == 2_000_000
    assert _resolve_googlevideo_dur(url, fmt, 99.0) == 100.0
    br = _googlevideo_byte_range(url, 0.0, 30.0, fmt=fmt, vod_duration=100.0)
    assert br is not None
    b0, b1 = br
    assert b0 == 0
    assert b1 > 100_000


def test_googlevideo_byte_range_tbr_estimate_when_no_filesize():
    url = "https://x.googlevideo.com/videoplayback?itag=140"
    fmt = {"tbr": 128, "duration": 200.0}
    clen = _resolve_googlevideo_clen(url, fmt, 200.0)
    assert clen and clen > 1_000_000
    br = _googlevideo_byte_range(url, 10.0, 40.0, fmt=fmt, vod_duration=200.0)
    assert br is not None


def test_local_dash_slice_accepts_midfile_fragment():
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(b"\x00" * 70000)
        path = tmp.name
    try:
        assert _local_dash_slice_valid(path) is True
    finally:
        os.unlink(path)


def test_dash_transcode_from_format_not_url():
    fmt = {"ext": "webm", "vcodec": "vp9", "url": "https://x/v?mime=video%2Fmp4"}
    assert _dash_video_needs_transcode(fmt["url"], fmt) is True
    fmt2 = {"ext": "mp4", "vcodec": "avc1", "url": "https://x/v?mime=video%2Fmp4"}
    assert _dash_video_needs_transcode(fmt2["url"], fmt2) is False
