"""Chat emotes: official Twitch globals + custom (BTTV/FFZ/7TV) merge.

Service tests swap services.chat_emotes._SESSION for a canned fake so no
network is touched; the endpoint tests patch fetch_emotes entirely. The live
GQL/static-cdn path (URLs must return HTTP 200) is the opt-in real test at
the bottom of this file — run with ``pytest -m real``.
"""
import pytest

from services import chat_emotes as ce

_TWITCH_GQL_URL = ce._TWITCH_GQL_URL
_IVR_URL = ce._IVR_USER_URL.format(login="cellbit")
_BTTV_USER_URL = ce._BTTV_USER_BY_ID_URL.format(twitch_id="28579002")
_FFZ_ROOM_URL = ce._FFZ_ROOM_URL.format(twitch_id="28579002")
_7TV_USER_URL = ce._SEVENTV_USER_URL.format(twitch_id="28579002")

# The GQL global-set payload shape the service parses (batch: list of one op).
_TWITCH_GQL_PAYLOAD = [
    {"data": {"emoteSet": {"id": "0", "emotes": [
        {"id": "425618", "token": "LUL"},
        {"id": "25", "token": "Kappa"},
        {"id": "88", "token": "PogChamp"},
    ]}}}
]


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, json_error=None):
        self.status_code = status_code
        self._json_data = json_data
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._json_data


class _FakeSession:
    """Stands in for requests.Session: canned per-URL GETs, one POST reply."""

    def __init__(self):
        self.gets: dict[str, _FakeResponse] = {}
        self.get_calls: list[str] = []
        self.post_calls: list[str] = []
        self.post_response = _FakeResponse(200, _TWITCH_GQL_PAYLOAD)

    def get(self, url, **kwargs):
        self.get_calls.append(url)
        return self.gets.get(url, _FakeResponse(404))

    def post(self, url, **kwargs):
        self.post_calls.append(url)
        return self.post_response


@pytest.fixture(autouse=True)
def _clean_caches():
    """Fresh per-test caches so one test's fetches never satisfy another's."""
    ce._CACHE.clear()
    ce._GLOBAL_CACHE.clear()
    yield
    ce._CACHE.clear()
    ce._GLOBAL_CACHE.clear()


@pytest.fixture
def fake_session(monkeypatch):
    """Hermetic network: ivr/BTTV/FFZ/7TV canned, Twitch GQL canned."""
    fake = _FakeSession()
    fake.gets[_IVR_URL] = _FakeResponse(200, [{"id": "28579002", "login": "cellbit"}])
    fake.gets[_BTTV_USER_URL] = _FakeResponse(200, {
        "id": "x", "channelEmotes": [{"id": "abc", "code": "CELLBIT"}], "sharedEmotes": [],
    })
    fake.gets[_FFZ_ROOM_URL] = _FakeResponse(200, {})
    fake.gets[_7TV_USER_URL] = _FakeResponse(200, {"emote_set": {"emotes": []}})
    fake.gets[ce._BTTV_GLOBAL_URL] = _FakeResponse(200, [])
    fake.gets[ce._FFZ_GLOBAL_URL] = _FakeResponse(200, {"default_sets": [], "sets": {}})
    fake.gets[ce._SEVENTV_GLOBAL_URL] = _FakeResponse(200, {"emotes": []})
    monkeypatch.setattr(ce, "_SESSION", fake)
    return fake


# ---------------------------------------------------------------------------
# Service: globals present, merge priority, URL format, cache, degradation
# ---------------------------------------------------------------------------

def test_globals_ride_along_with_customs(fake_session):
    emotes = ce.fetch_emotes("twitch", "cellbit")
    by_name = {e["name"]: e for e in emotes}
    # Official Twitch globals present with static-cdn v2 urls.
    assert by_name["LUL"] == {
        "name": "LUL",
        "provider": "twitch",
        "url": "https://static-cdn.jtvnw.net/emoticons/v2/425618/default/dark/1.0",
        "global": True,
    }
    assert by_name["Kappa"]["provider"] == "twitch"
    assert by_name["PogChamp"]["provider"] == "twitch"
    # Channel customs untouched.
    assert by_name["CELLBIT"] == {
        "name": "CELLBIT", "provider": "bttv",
        "url": "https://cdn.betterttv.net/emote/abc/2x.webp", "global": False,
    }


def test_channel_custom_wins_over_twitch_global(fake_session):
    """A channel BTTV emote named like an official global shadows it."""
    fake_session.gets[_BTTV_USER_URL] = _FakeResponse(200, {
        "id": "x", "channelEmotes": [{"id": "custom-id", "code": "Kappa"}], "sharedEmotes": [],
    })
    by_name = {e["name"]: e for e in ce.fetch_emotes("twitch", "cellbit")}
    assert by_name["Kappa"] == {
        "name": "Kappa", "provider": "bttv",
        "url": "https://cdn.betterttv.net/emote/custom-id/2x.webp", "global": False,
    }


def test_twitch_global_url_uses_v2_template(fake_session):
    """Every official global maps to static-cdn.jtvnw.net/emoticons/v2/{id}/..."""
    urls = {
        e["name"]: e["url"]
        for e in ce._fetch_twitch_globals()
    }
    assert urls["LUL"] == "https://static-cdn.jtvnw.net/emoticons/v2/425618/default/dark/1.0"
    assert urls["Kappa"] == "https://static-cdn.jtvnw.net/emoticons/v2/25/default/dark/1.0"
    assert all(u.startswith("https://static-cdn.jtvnw.net/emoticons/v2/") for u in urls.values())


def test_globals_cached_second_call(fake_session):
    """Channel (15min) and global (1h) caches serve the second call — no refetch."""
    ce.fetch_emotes("twitch", "cellbit")
    first_gql = fake_session.post_calls.count(_TWITCH_GQL_URL)
    first_ivr = fake_session.get_calls.count(_IVR_URL)
    assert first_gql == 1 and first_ivr == 1

    ce.fetch_emotes("twitch", "cellbit")
    assert fake_session.post_calls.count(_TWITCH_GQL_URL) == first_gql == 1
    assert fake_session.get_calls.count(_IVR_URL) == first_ivr == 1
    # And the merged result is stable across calls.
    first = {e["name"] for e in ce.fetch_emotes("twitch", "cellbit")}
    assert "LUL" in first


def test_gql_failure_degrades_to_customs(fake_session):
    """A dead GQL endpoint drops only the official globals — customs survive."""
    fake_session.post_response = _FakeResponse(500)
    by_name = {e["name"]: e for e in ce.fetch_emotes("twitch", "cellbit")}
    assert "LUL" not in by_name
    assert by_name["CELLBIT"]["provider"] == "bttv"


def test_gql_bad_body_degrades(fake_session):
    fake_session.post_response = _FakeResponse(200, json_error=ValueError("bad json"))
    by_name = {e["name"]: e for e in ce.fetch_emotes("twitch", "cellbit")}
    assert "LUL" not in by_name
    assert "CELLBIT" in by_name


# ---------------------------------------------------------------------------
# Endpoint contract: shape unchanged, slug still required
# ---------------------------------------------------------------------------

@pytest.fixture
async def client():
    from httpx import ASGITransport, AsyncClient

    from app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_endpoint_still_requires_slug(client):
    resp = await client.get("/api/chat/emotes")
    assert resp.status_code == 400
    resp = await client.get("/api/chat/emotes", params={"platform": "twitch"})
    assert resp.status_code == 400
    resp = await client.get("/api/chat/emotes", params={"slug": "cellbit"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_endpoint_shape_unchanged(client, monkeypatch):
    def fake_fetch(platform, slug):
        return [
            {"name": "CELLBIT", "provider": "bttv", "url": "u1", "global": False},
            {"name": "LUL", "provider": "twitch", "url": "u2", "global": True},
        ]

    monkeypatch.setattr(ce, "fetch_emotes", fake_fetch)
    resp = await client.get("/api/chat/emotes", params={"platform": "twitch", "slug": "cellbit"})
    assert resp.status_code == 200
    assert resp.json() == {"emotes": [
        {"name": "CELLBIT", "provider": "bttv", "url": "u1", "global": False},
        {"name": "LUL", "provider": "twitch", "url": "u2", "global": True},
    ]}


# ---------------------------------------------------------------------------
# Live guard (opt-in): the real GQL set resolves and static-cdn urls serve.
# ---------------------------------------------------------------------------

@pytest.mark.real
def test_twitch_global_urls_serve_live_real():
    """LUL at minimum must resolve with a URL that returns HTTP 200."""
    import requests

    globals_ = ce._fetch_twitch_globals()
    by_name = {e["name"]: e for e in globals_}
    lul = by_name.get("LUL")
    assert lul is not None and lul["provider"] == "twitch"
    assert lul["url"] == "https://static-cdn.jtvnw.net/emoticons/v2/425618/default/dark/1.0"
    for e in [lul, by_name["Kappa"], by_name["Kreygasm"]]:
        resp = requests.get(e["url"], timeout=10)
        assert resp.status_code == 200, f"{e['name']} url {e['url']} -> HTTP {resp.status_code}"
