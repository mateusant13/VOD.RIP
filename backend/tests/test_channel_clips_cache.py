"""Clips cache key must include youtube_slug to avoid cross-channel pollution."""

from services.channel_cache import make_channel_cache_key


def test_clips_cache_key_includes_youtube_slug():
    a = make_channel_cache_key("clips", "", "", "YouTube", 10, "YouTube", "channelA")
    b = make_channel_cache_key("clips", "", "", "YouTube", 10, "YouTube", "channelB")
    assert a != b
