"""App-level security configuration tests."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.middleware.cors import CORSMiddleware

from app import app


def _cors_kwargs() -> dict:
    for middleware in app.user_middleware:
        if middleware.cls is CORSMiddleware:
            return middleware.kwargs
    raise AssertionError("CORSMiddleware not mounted")


def test_cors_localhost_only_origins():
    cfg = _cors_kwargs()
    assert "*" not in cfg["allow_origins"]
    assert "http://localhost" in cfg["allow_origins"]
    assert "http://127.0.0.1" in cfg["allow_origins"]
    assert "http://[::1]" in cfg["allow_origins"]
    assert cfg["allow_origin_regex"] == r"^http://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_cors_allows_localhost_origin(client):
    resp = await client.options(
        "/api/settings",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"



async def test_cors_allows_ipv6_localhost_origin(client):
    resp = await client.options(
        "/api/settings",
        headers={
            "Origin": "http://[::1]:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.headers.get("access-control-allow-origin") == "http://[::1]:5173"


async def test_cors_blocks_untrusted_origin(client):
    resp = await client.options(
        "/api/settings",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.headers.get("access-control-allow-origin") is None
