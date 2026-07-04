from services.ytdlp_download import detect_platform
from services.url_validation import is_sensible_vod_url


def test_youtube_detect_platform():
    assert detect_platform("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "YouTube"
    assert detect_platform("https://youtu.be/dQw4w9WgXcQ") == "YouTube"


def test_youtube_sensible_url():
    assert is_sensible_vod_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
