"""YouTube preview must not recycle expired googlevideo URLs on refresh."""

from unittest.mock import MagicMock, patch

from services.preview_service import (
    _RESOLVED_STREAM_CACHE,
    invalidate_resolved_stream_cache,
)
from services.ytdlp_hls import (
    StaleGooglevideoUrl,
    _fetch_googlevideo_span_resilient,
    _is_stale_googlevideo_error,
)


def test_invalidate_resolved_stream_cache_drops_entry():
    _RESOLVED_STREAM_CACHE.clear()
    vid = "dQw4w9WgXcQ"
    key = f"{vid}:720:v2"
    _RESOLVED_STREAM_CACHE[key] = (0.0, ("url", {}, "YouTube", [], "hls", {}))
    invalidate_resolved_stream_cache(f"https://www.youtube.com/watch?v={vid}", 720)
    assert key not in _RESOLVED_STREAM_CACHE


def test_invalidate_youtube_resolve_caches_clears_both():
    from services.preview_service import _invalidate_youtube_resolve_caches
    from services.ytdlp_hls import _EXTRACT_INFO_CACHE

    _RESOLVED_STREAM_CACHE.clear()
    vid = "dQw4w9WgXcQ"
    url = f"https://www.youtube.com/watch?v={vid}"
    _RESOLVED_STREAM_CACHE[f"{vid}:720:v2"] = (0.0, ("cached", {}, "YouTube", [], "hls", {}))
    _EXTRACT_INFO_CACHE[f"{url}|[]|False|||"] = (0.0, {})
    with patch(
        "services.ytdlp_hls.invalidate_youtube_extract_cache",
    ) as mock_extract:
        _invalidate_youtube_resolve_caches(url, 720)
    mock_extract.assert_called_once_with(url)
    assert f"{vid}:720:v2" not in _RESOLVED_STREAM_CACHE

def test_stale_googlevideo_error_no_bisect():
    assert _is_stale_googlevideo_error(StaleGooglevideoUrl("403"))
    resp = MagicMock(status_code=403)
    err = type("HTTPError", (Exception,), {"response": resp})()
    assert _is_stale_googlevideo_error(err)


def test_fetch_span_raises_stale_on_403_without_bisect():
    with patch(
        "services.ytdlp_hls._fetch_googlevideo_range_once",
        side_effect=StaleGooglevideoUrl("403"),
    ):
        try:
            _fetch_googlevideo_span_resilient(
                "https://rr1---sn.example.googlevideo.com/videoplayback?id=1",
                (0, 10_000_000),
                {},
                "/tmp/out.bin",
            )
            raised = False
        except StaleGooglevideoUrl:
            raised = True
    assert raised
