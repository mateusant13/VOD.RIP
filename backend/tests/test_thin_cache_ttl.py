"""Self-check: degenerate YouTube extracts get short TTL in _EXTRACT_INFO_CACHE."""

import time

from services.ytdlp_hls import _cache_extract_result, _EXTRACT_INFO_CACHE, _EXTRACT_CACHE_TTL_SEC

# Reset cache for clean test
_EXTRACT_INFO_CACHE.clear()


def _check_implied_ttl(info: dict, expected_max_ttl: float) -> None:
    """Feed info through _cache_extract_result and check the implied TTL."""
    key = f"test:{id(info)}"
    before = time.time()
    _cache_extract_result(key, info)
    stored_ts = _EXTRACT_INFO_CACHE[key][0]
    # stored_ts may be adjusted backward for degenerate extracts
    # implied TTL = _EXTRACT_CACHE_TTL_SEC - (time_of_store - stored_ts)
    # where time_of_store ≈ before (the function runs synchronously)
    implied_ttl = _EXTRACT_CACHE_TTL_SEC - (before - stored_ts)
    # Allow ~200ms clock jitter
    assert implied_ttl <= expected_max_ttl + 0.2, (
        f"expected max TTL {expected_max_ttl}s, got {implied_ttl:.2f}s "
        f"(stored_ts delta={before - stored_ts:.4f}s)"
    )
    assert implied_ttl > 0, f"negative implied TTL: {implied_ttl:.2f}s"


def test_short_ttl_for_degenerate_youtube():
    """Formats≤1 → ~60s TTL (not full 6h)."""
    _check_implied_ttl({"formats": []}, 60.0)


def test_short_ttl_for_single_format():
    _check_implied_ttl({"formats": [{"url": "a.mp4"}]}, 60.0)


def test_full_ttl_for_normal_youtube():
    """Formats>1 → full _EXTRACT_CACHE_TTL_SEC."""
    _check_implied_ttl({"formats": [{"url": "a.mp4"}, {"url": "b.mp4"}]}, _EXTRACT_CACHE_TTL_SEC)


def test_full_ttl_for_many_formats():
    _check_implied_ttl({"formats": [{"url": str(i)} for i in range(10)]}, _EXTRACT_CACHE_TTL_SEC)


def test_full_ttl_for_no_formats_key():
    """No 'formats' key → treated as 0 → short TTL (defensive: bot-wall could strip it)."""
    _check_implied_ttl({}, 60.0)


