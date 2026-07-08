"""YouTube warm dedup keys must not split shorts vs watch URLs."""

from services.preview_service import _youtube_warm_inflight_key
from services.youtube_innertube import canonical_youtube_watch_url, extract_video_id
from services.ytdlp_hls import _extract_cache_key


def test_youtube_warm_key_matches_shorts_and_watch():
    shorts = "https://www.youtube.com/shorts/IbkQI11-NZk"
    watch = "https://www.youtube.com/watch?v=IbkQI11-NZk"
    assert extract_video_id(shorts) == extract_video_id(watch) == "IbkQI11-NZk"
    assert _youtube_warm_inflight_key(shorts) == _youtube_warm_inflight_key(watch) == "IbkQI11-NZk"
    assert canonical_youtube_watch_url(shorts) == watch


def test_youtube_extract_cache_key_ignores_url_shape():
    shorts = "https://www.youtube.com/shorts/IbkQI11-NZk"
    watch = "https://www.youtube.com/watch?v=IbkQI11-NZk"
    opts = {"extractor_args": {"youtube": {"player_client": ["ios"]}}}
    assert _extract_cache_key(shorts, opts) == _extract_cache_key(watch, opts)
