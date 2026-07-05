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
