"""The Kick language clue lives under livestream, not under user.

Measured 2026-10-16 against the live API for six real channels:

  gaules   livestream.language = 'Portuguese'   user.language = None
  gaveta   livestream.language = None            user.language = None
  (four more offline channels: both None)

The parser read `user.language or data.language`, so it returned None for EVERY
channel - the clue never produced a value while costing 0.5-1.4 s of network per
channel fetch on the critical path.

RED ARM: drop `_ls.get("language")` from the chain and
test_reads_language_from_livestream fails, because the payload below is shaped
exactly like the real one.
"""
def test_reads_language_from_livestream(monkeypatch):
    from services import kick_api_service as K
    from services import channel_cache
    # a cache hit would mask the parse, so make the lookup miss
    monkeypatch.setattr(channel_cache, "get_cached", lambda *a, **k: None)
    monkeypatch.setattr(channel_cache, "set_cached", lambda *a, **k: None)
    # the real shape: language under livestream, nothing under user
    payload = {
        "id": 1,
        "slug": "gaules",
        "user": {"id": 2, "username": "gaules"},
        "livestream": {"id": 3, "is_live": True, "language": "Portuguese"},
    }
    monkeypatch.setattr(K, "_get_json", lambda *a, **k: payload)
    assert K.get_channel_language_sync("gaules") == "Portuguese"


def test_offline_channel_is_still_none(monkeypatch):
    """An offline channel has an empty livestream - None stays None."""
    from services import kick_api_service as K
    from services import channel_cache
    monkeypatch.setattr(channel_cache, "get_cached", lambda *a, **k: None)
    monkeypatch.setattr(channel_cache, "set_cached", lambda *a, **k: None)
    payload = {"id": 1, "slug": "gaveta", "user": {"id": 2}, "livestream": {}}
    monkeypatch.setattr(K, "_get_json", lambda *a, **k: payload)
    assert K.get_channel_language_sync("gaveta") is None
