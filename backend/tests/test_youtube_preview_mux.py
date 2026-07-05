"""YouTube preview muxed-stream policy — no network."""
from services.preview_service import _deduped_progressive_variants, _is_muxed_format
from services.youtube_innertube import _formats_from_adaptive, _streaming_url_formats


def test_adaptive_video_only_has_no_audio_codec():
    streaming = {
        "adaptiveFormats": [{
            "url": "https://cdn.example/v.mp4",
            "mimeType": "video/mp4; codecs=\"avc1.4D401E\"",
            "height": 720,
            "itag": 136,
        }],
    }
    fmt = _formats_from_adaptive(streaming)[0]
    assert fmt["acodec"] == "none"
    assert not _is_muxed_format(fmt)


def test_abr_streaming_url_not_exposed():
    """serverAbrStreamingUrl is SABR — must not be offered as a stream format."""
    from services.youtube_innertube import _is_sabr_stream_url, _streaming_url_formats

    streaming = {
        "serverAbrStreamingUrl": "https://rr1---sn.example.googlevideo.com/videoplayback?itag=18&sabr=1",
        "hlsManifestUrl": "https://manifest.googlevideo.com/api/manifest/hls_playlist/expire/1/master.m3u8",
    }
    assert _is_sabr_stream_url(streaming["serverAbrStreamingUrl"]) is True
    out = _streaming_url_formats(streaming)
    assert len(out) == 1
    assert out[0]["format_id"] == "hls-master"
    assert not _is_muxed_format({"format_id": "abr-muxed", "url": streaming["serverAbrStreamingUrl"], "acodec": "mp4a"})


def test_progressive_variants_skip_video_only():
    info = {
        "formats": [
            {"url": "https://x/v-only.mp4", "protocol": "https", "height": 1080, "vcodec": "avc1", "acodec": "none"},
            {"url": "https://x/mux.mp4", "protocol": "https", "height": 720, "vcodec": "avc1", "acodec": "mp4a", "format_id": "progressive-18"},
        ],
    }
    prog = _deduped_progressive_variants(info)
    assert len(prog) == 1
    assert prog[0]["format_id"] == "progressive-18"


def test_innertube_info_keeps_video_only_for_multi_height_preview():
    from services.youtube_innertube import _info_from_player_data

    adaptive = [{
        "url": "https://cdn.example/v-only.mp4",
        "format_id": "137",
        "protocol": "https",
        "height": 1080,
        "vcodec": "avc1",
        "acodec": "none",
    }]
    info = _info_from_player_data({}, "abc", None, None, adaptive_formats=adaptive)
    assert info is not None
    assert info["formats"][0]["height"] == 1080


def test_find_progressive_skips_video_only():
    from services.ytdlp_hls import _find_progressive_format

    info = {
        "formats": [
            {"url": "https://x/v.mp4", "protocol": "https", "height": 1080, "vcodec": "avc1", "acodec": "none"},
            {"url": "https://x/mux.mp4", "protocol": "https", "height": 720, "vcodec": "avc1", "acodec": "mp4a"},
        ],
    }
    fmt = _find_progressive_format(info)
    assert fmt["height"] == 720
