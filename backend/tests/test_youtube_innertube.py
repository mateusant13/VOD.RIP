"""InnerTube fast-path helpers."""
from unittest.mock import MagicMock, patch

from services.youtube_innertube import (
    _classify_http,
    _classify_playability,
    _CLIENT_PROFILES,
    extract_video_id,
    innertube_extract_info,
    _parse_hls_variants,
)
from services.youtube_session import YouTubeSession


def test_extract_video_id_watch():
    assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_parse_hls_variants_dedupes_heights():
    text = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=854x480\n"
        "tier480.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\n"
        "tier720.m3u8\n"
    )
    formats = _parse_hls_variants(text, "https://cdn.example/master.m3u8")
    heights = sorted(f["height"] for f in formats)
    assert heights == [480, 720]


def test_classify_http_retry_on_403():
    assert _classify_http(403) == "retry"
    assert _classify_http(404) == "fatal"


def test_classify_playability_login_retries():
    assert _classify_playability("LOGIN_REQUIRED", None) == "retry"
    assert _classify_playability("LIVE_STREAM_OFFLINE", None) == "fatal"


def test_innertube_falls_through_clients_on_403():
    master = (
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720\n"
        "https://cdn.example/720.m3u8\n"
    )
    ok_player = {
        "playabilityStatus": {"status": "OK"},
        "streamingData": {"hlsManifestUrl": "https://cdn.example/master.m3u8"},
        "videoDetails": {"title": "t", "lengthSeconds": "60", "author": "a"},
    }
    post_resp_fail = MagicMock(status_code=403, raise_for_status=MagicMock())
    post_resp_ok = MagicMock(status_code=200)
    post_resp_ok.json.return_value = ok_player
    post_resp_ok.raise_for_status = MagicMock()

    get_resp = MagicMock(status_code=200, text=master)
    get_resp.raise_for_status = MagicMock()

    mock_http = MagicMock()
    mock_http.post.side_effect = [post_resp_fail, post_resp_ok, post_resp_ok]
    mock_http.get.return_value = get_resp
    session = YouTubeSession(visitor_data="test-visitor", cookie_header="YSC=test")

    with patch("services.youtube_innertube._http_for", return_value=mock_http):
        info = innertube_extract_info("dQw4w9WgXcQ", session=session)
    assert info is not None
    assert info["title"] == "t"
    assert mock_http.post.call_count >= 2
    assert len(_CLIENT_PROFILES) >= 2
