"""YouTube size estimates from googlevideo clen + video-only tiers."""
from services.size_estimate import size_by_quality_from_formats


def test_clen_video_only_includes_audio_and_matches_download():
    formats = [{
        "url": (
            "https://rr1---sn-x.googlevideo.com/videoplayback"
            "?clen=21000000&itag=136&mime=video%2Fmp4"
        ),
        "height": 720,
        "vcodec": "avc1",
        "acodec": "none",
        "format_id": "adaptive-136",
        "protocol": "https",
    }]
    sizes = size_by_quality_from_formats(formats, 180.0)
    assert "720p" in sizes
    assert sizes["720p"] >= 21_000_000


def test_muxed_360_not_shrunk_when_clen_present():
    formats = [{
        "url": "https://x.googlevideo.com/videoplayback?clen=5000000&itag=18",
        "height": 360,
        "vcodec": "avc1",
        "acodec": "mp4a",
        "format_id": "progressive-18",
        "protocol": "https",
    }]
    sizes = size_by_quality_from_formats(formats, 60.0)
    assert sizes["360p"] == 5_000_000


def test_googlevideo_tbr_used_at_face_value_plus_audio():
    """Video-only DASH without clen: tbr is the stream average (YouTube itag
    bandwidth) — used at face value with the audio addition. Guards the old
    0.55 manifest-shrink that halved it."""
    formats = [{
        "url": "https://rr1---sn-x.googlevideo.com/videoplayback?mime=video%2Fmp4&itag=137",
        "height": 1080,
        "vcodec": "avc1",
        "acodec": "none",
        "tbr": 8000.0,
        "format_id": "adaptive-137",
        "protocol": "https",
    }]
    sizes = size_by_quality_from_formats(formats, 360.0)
    assert sizes["1080p"] == int((8000.0 + 160.0) * 1000.0 / 8.0 * 360.0)
