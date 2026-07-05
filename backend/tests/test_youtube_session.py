"""YouTube session helpers."""
import threading

from services.youtube_session import (
    bootstrap_anonymous_session,
    invalidate_anonymous_session,
    youtube_session_from_values,
)


def test_bootstrap_anonymous_session():
    vd, cookies, cookie_file = bootstrap_anonymous_session()
    assert vd is None or len(vd) > 8
    assert cookies is None or "YSC" in cookies or "VISITOR" in cookies
    if cookie_file:
        assert __import__("pathlib").Path(cookie_file).is_file()


def test_parallel_bootstrap_single_flight():
    invalidate_anonymous_session()
    results: list[tuple] = []
    barrier = threading.Barrier(4)

    def _worker():
        barrier.wait()
        results.append(bootstrap_anonymous_session(force=True))

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=45)
    assert len(results) == 4
    cookies = [r[1] for r in results if r[1]]
    assert cookies and all(c == cookies[0] for c in cookies)


def test_session_defaults_anonymous():
    s = youtube_session_from_values()
    assert s.visitor_data is None or len(s.visitor_data) > 8
