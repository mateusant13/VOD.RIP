"""The per-session rewritten-playlist cache is bounded.

This cache used to be unbounded: every distinct playlist URL kept its body for
the life of the session, and nothing stopped a NON-playlist body (an MP4 served
from a master URL) from being pinned there as well. Measured on this box: a
GET /master.m3u8 answered with a 92 MB MP4 body - the shape of a resident set
that grows with sessions, which is how an RSS reaches multi-GB.

Two bounds and one refusal, all pinned here:

  * at most _MAX_PLAYLIST_CACHE_ENTRIES entries (oldest evicted first);
  * at most _MAX_PLAYLIST_CACHE_BYTES bytes resident;
  * a body over the byte budget is REFUSED, because a body that big is not a
    playlist and caching it is the bug.
"""
from __future__ import annotations

from pathlib import Path

from services.preview import session as S


def _session(tmp_path: Path) -> S.PreviewSession:
    sess = S.PreviewSession(
        session_id="cachetest01",
        vod_url="https://www.youtube.com/watch?v=x",
        platform="YouTube",
        master_url="",
        entry_url="https://example.com/v",
        cache_dir=tmp_path,
        crop_start=0,
        crop_end=25,
    )
    return sess


def test_the_entry_count_is_bounded_and_evicts_oldest(tmp_path):
    sess = _session(tmp_path)
    limit = S._MAX_PLAYLIST_CACHE_ENTRIES

    for i in range(limit + 5):
        S._cache_playlist(sess, f"https://cdn/{i}.m3u8", b"#EXTM3U\n", float(i))

    cache = S._playlist_cache(sess)
    assert len(cache) == limit
    assert "https://cdn/0.m3u8" not in cache, "oldest must go first"
    assert f"https://cdn/{limit + 4}.m3u8" in cache


def test_a_body_over_the_budget_is_refused_not_pinned(tmp_path):
    sess = _session(tmp_path)
    huge = b"x" * (S._MAX_PLAYLIST_CACHE_BYTES + 1)

    stored = S._cache_playlist(sess, "https://cdn/master.m3u8", huge, 0.0)

    assert stored is False
    assert S._playlist_cache(sess) == {}


def test_the_byte_budget_is_respected(tmp_path):
    sess = _session(tmp_path)
    chunk = b"#EXTM3U\n" + b"#" * (S._MAX_PLAYLIST_CACHE_BYTES // 2)

    for i in range(6):
        S._cache_playlist(sess, f"https://cdn/{i}.m3u8", chunk, float(i))

    cache = S._playlist_cache(sess)
    total = sum(len(body) for body, _ts in cache.values())
    assert total <= S._MAX_PLAYLIST_CACHE_BYTES
    assert len(cache) >= 1, "never evict down to an empty cache"


def test_reusing_a_url_moves_it_to_the_fresh_end(tmp_path):
    sess = _session(tmp_path)
    limit = S._MAX_PLAYLIST_CACHE_ENTRIES

    for i in range(limit):
        S._cache_playlist(sess, f"https://cdn/{i}.m3u8", b"#EXTM3U\n", float(i))
    # touch the oldest, then overflow
    S._cache_playlist(sess, "https://cdn/0.m3u8", b"#EXTM3U\n", 99.0)
    S._cache_playlist(sess, "https://cdn/new.m3u8", b"#EXTM3U\n", 100.0)

    cache = S._playlist_cache(sess)
    assert "https://cdn/0.m3u8" in cache, "a just-used entry must survive the sweep"
    assert "https://cdn/1.m3u8" not in cache


def test_empty_body_is_never_stored(tmp_path):
    sess = _session(tmp_path)

    assert S._cache_playlist(sess, "https://cdn/x.m3u8", b"", 0.0) is False
    assert S._playlist_cache(sess) == {}
