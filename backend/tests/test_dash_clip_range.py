"""YouTube DASH clip byte-range helpers."""
from services.ytdlp_hls import _googlevideo_byte_range, _dash_video_needs_transcode


def test_googlevideo_byte_range_trim_start():
    url = "https://x.googlevideo.com/videoplayback?clen=1000000&dur=100.0"
    b0, b1 = _googlevideo_byte_range(url, 0.0, 30.0)
    assert b0 == 0
    assert 25000 < b1 < 400000


def test_dash_transcode_from_format_not_url():
    fmt = {"ext": "webm", "vcodec": "vp9", "url": "https://x/v?mime=video%2Fmp4"}
    assert _dash_video_needs_transcode(fmt["url"], fmt) is True
    fmt2 = {"ext": "mp4", "vcodec": "avc1", "url": "https://x/v?mime=video%2Fmp4"}
    assert _dash_video_needs_transcode(fmt2["url"], fmt2) is False
