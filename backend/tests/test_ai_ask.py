"""Tests for the experimental AI ask-about-channel feature.

Covers: write-only API key + toggle validation in /api/settings, the
POST /api/ai/ask guards, RAG context assembly (scope + days window + chat
author prefix), and the LLM error mapping (invalid key → 401, network → 502).

The LLM HTTP call is stubbed at the router boundary (`routers.ai._ask_llm` /
`routers.ai._post_chat_completion`) so the tests never hit the network.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

_TMP = Path(tempfile.mkdtemp(prefix="ai-ask-tests-"))
_DB = _TMP / "archive.db"

# Must be set before any services.archive_db / app import: the module-level
# self-check binds the shared connection at import time.
os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)

from app import app  # noqa: E402
from deps import settings_mgr  # noqa: E402
from httpx import AsyncClient, ASGITransport  # noqa: E402
from models.schemas import AppSettings  # noqa: E402
from routers import ai as ai_router  # noqa: E402
from services import archive_db  # noqa: E402

CHANNEL = "cellbit"
PLATFORM = "twitch"


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


@pytest.fixture(autouse=True)
def _reset_settings():
    """Clean settings + scratch settings.json before each test."""
    original_file = settings_mgr._settings_file
    original_dir = original_file.parent
    original_dir.mkdir(parents=True, exist_ok=True)
    temp_file = original_dir / f"settings_ai_test_{os.getpid()}.json"
    settings_mgr._settings_file = temp_file
    settings_mgr._settings = AppSettings()
    yield
    settings_mgr._settings_file = original_file
    if temp_file.exists():
        temp_file.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _clean_archive_db():
    """Drop all seeded rows (FTS entries follow via the external-content
    triggers) so each test starts from an empty archive."""
    archive_db.execute("DELETE FROM messages")
    archive_db.execute("DELETE FROM transcripts")
    archive_db.execute("DELETE FROM videos")
    yield


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _enable(api_key: str = "sk-test-1234") -> None:
    """Turn the experimental AI feature on with a key, through the real API."""
    s = settings_mgr.get()
    s.experimental_ai_enabled = True
    s.ai_api_key = api_key
    s.ai_api_key_set = True
    settings_mgr.save(s)


def _seed_video(video_id: str, title: str, days_ago: int) -> None:
    archive_db.upsert_video({
        "platform": PLATFORM,
        "video_id": video_id,
        "channel": CHANNEL,
        "title": title,
        "kind": "vod",
        "started_at": _iso(days_ago),
        "ended_at": _iso(days_ago),
        "duration_sec": 3600,
        "status": "known",
    })


def _seed_chat(video_id: str, lines: list[tuple[str, str]]) -> None:
    archive_db.insert_messages(PLATFORM, video_id, [
        {"offset_sec": float(i), "username": user, "text": text, "badges": [], "emotes": []}
        for i, (user, text) in enumerate(lines)
    ])


def _seed_transcript(video_id: str, lines: list[str]) -> None:
    archive_db.insert_transcript(PLATFORM, video_id, [
        {"seg_idx": i, "start_sec": float(i * 10), "end_sec": float(i * 10 + 9), "text": text}
        for i, text in enumerate(lines)
    ])


def _seed_default_archive() -> None:
    """Two recent videos with chat + transcript mentions, one old video with
    a chat mention (outside a 7-day window)."""
    _seed_video("recent-1", "Recomendação de builds 01", 1)
    _seed_chat("recent-1", [
        ("cellbit", "hoje eu recomendei a build do xerath"),
        ("cellbit", "a build do xerath é forte demais"),
        ("viewer1", "qual build ele recomendou?"),
    ])
    _seed_transcript("recent-1", [
        "então gente a build do xerath fica assim",
        "recomendo testar essa build hoje",
    ])
    _seed_video("recent-2", "Builds da semana 02", 2)
    _seed_chat("recent-2", [
        ("cellbit", "recomendei a build do xerath de novo"),
    ])
    _seed_transcript("recent-2", [
        "a build do xerath voltou a ser boa",
    ])
    _seed_video("old-1", "Stream antiga 03", 30)
    _seed_chat("old-1", [
        ("cellbit", "lá atrás eu recomendei a build do xerath também"),
    ])


# ── Settings round-trip ─────────────────────────────────────────────────────

class TestAiSettings:
    @pytest.mark.asyncio
    async def test_toggle_on_without_key_rejected(self, client):
        resp = await client.post("/api/settings", json={"experimental_ai_enabled": True})
        assert resp.status_code == 400
        assert "API key" in resp.json()["detail"]
        # Nothing persisted.
        assert settings_mgr.get().experimental_ai_enabled is False

    @pytest.mark.asyncio
    async def test_key_never_returned_by_get(self, client):
        resp = await client.post("/api/settings", json={"ai_api_key": "sk-secret-42"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ai_api_key_set"] is True
        assert body.get("ai_api_key") == ""

        resp = await client.get("/api/settings")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ai_api_key_set"] is True
        assert body.get("ai_api_key") == ""
        assert "sk-secret-42" not in resp.text

    @pytest.mark.asyncio
    async def test_toggle_on_after_key_persists(self, client):
        await client.post("/api/settings", json={"ai_api_key": "sk-secret-42"})
        resp = await client.post("/api/settings", json={"experimental_ai_enabled": True})
        assert resp.status_code == 200
        assert resp.json()["experimental_ai_enabled"] is True
        stored = settings_mgr.get()
        assert stored.experimental_ai_enabled is True
        assert stored.ai_api_key == "sk-secret-42"

    @pytest.mark.asyncio
    async def test_empty_key_clears(self, client):
        await client.post("/api/settings", json={"ai_api_key": "sk-secret-42"})
        resp = await client.post("/api/settings", json={"ai_api_key": ""})
        assert resp.status_code == 200
        assert resp.json()["ai_api_key_set"] is False
        assert settings_mgr.get().ai_api_key == ""


# ── Endpoint guards ─────────────────────────────────────────────────────────

class TestAiAskGuards:
    @pytest.mark.asyncio
    async def test_disabled_toggle_forbidden(self, client):
        resp = await client.post("/api/ai/ask", json={
            "channel": CHANNEL, "platform": PLATFORM, "question": "quantas vezes?",
        })
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_key_bad_request(self, client):
        s = settings_mgr.get()
        s.experimental_ai_enabled = True  # inconsistent state: toggle on, no key
        settings_mgr.save(s)
        resp = await client.post("/api/ai/ask", json={
            "channel": CHANNEL, "platform": PLATFORM, "question": "quantas vezes?",
        })
        assert resp.status_code == 400
        assert "API key" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_bad_platform(self, client):
        _enable()
        resp = await client.post("/api/ai/ask", json={
            "channel": CHANNEL, "platform": "myspace", "question": "quantas vezes?",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_question_length_bound(self, client):
        _enable()
        resp = await client.post("/api/ai/ask", json={
            "channel": CHANNEL, "platform": PLATFORM, "question": "x" * 501,
        })
        assert resp.status_code == 400
        assert "too long" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_bad_scope_and_days(self, client):
        _enable()
        resp = await client.post("/api/ai/ask", json={
            "channel": CHANNEL, "platform": PLATFORM, "question": "quantas vezes?",
            "scope": "transcricoes",
        })
        assert resp.status_code == 400
        resp = await client.post("/api/ai/ask", json={
            "channel": CHANNEL, "platform": PLATFORM, "question": "quantas vezes?",
            "days": 0,
        })
        assert resp.status_code == 400


# ── RAG happy path ──────────────────────────────────────────────────────────

class TestAiAskRag:
    @pytest.mark.asyncio
    async def test_answer_cites_right_videos(self, client, monkeypatch):
        _enable()
        _seed_default_archive()
        captured: dict = {}

        async def fake_llm(api_key, question, context_text):
            captured["api_key"] = api_key
            captured["question"] = question
            captured["context"] = context_text
            return "Ele recomendou a build do xerath 2 vezes, em Builds da semana 02."

        monkeypatch.setattr(ai_router, "_ask_llm", fake_llm)
        resp = await client.post("/api/ai/ask", json={
            "channel": CHANNEL, "platform": PLATFORM,
            "question": "quantas vezes ele recomendou a build do xerath?",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["answer"] == "Ele recomendou a build do xerath 2 vezes, em Builds da semana 02."
        # LLM received the user's own key + the question.
        assert captured["api_key"] == "sk-test-1234"
        assert captured["question"] == "quantas vezes ele recomendou a build do xerath?"
        # Context is tagged with video title + date and chat rows are
        # 'user: message' prefixed.
        assert "Recomendação de builds 01" in captured["context"]
        assert "Builds da semana 02" in captured["context"]
        assert "cellbit: hoje eu recomendei a build do xerath" in captured["context"]
        # Sources echo video titles + matched text.
        titles = {s["video_title"] for s in body["sources"]}
        assert "Recomendação de builds 01" in titles
        assert "Builds da semana 02" in titles
        assert all(s["created_at"] for s in body["sources"])
        assert any("xerath" in s["matched_text"] for s in body["sources"])

    @pytest.mark.asyncio
    async def test_scope_filters_searched_table(self, client, monkeypatch):
        _enable()
        _seed_default_archive()
        captured: dict = {}

        async def fake_llm(api_key, question, context_text):
            captured["context"] = context_text
            return "ok"

        monkeypatch.setattr(ai_router, "_ask_llm", fake_llm)

        await client.post("/api/ai/ask", json={
            "channel": CHANNEL, "platform": PLATFORM,
            "question": "build do xerath", "scope": "chat",
        })
        # Chat-only context: 'user:' rows present, transcript-only lines absent.
        assert "cellbit: hoje eu recomendei a build do xerath" in captured["context"]
        assert "então gente a build do xerath fica assim" not in captured["context"]
        assert "recomendo testar essa build hoje" not in captured["context"]

        await client.post("/api/ai/ask", json={
            "channel": CHANNEL, "platform": PLATFORM,
            "question": "build do xerath", "scope": "transcript",
        })
        # Transcript-only context: caption lines present, no 'user:' rows.
        assert "então gente a build do xerath fica assim" in captured["context"]
        assert "cellbit: hoje eu recomendei a build do xerath" not in captured["context"]

    @pytest.mark.asyncio
    async def test_days_window_filters_by_video_date(self, client, monkeypatch):
        _enable()
        _seed_default_archive()
        captured: dict = {}

        async def fake_llm(api_key, question, context_text):
            captured["context"] = context_text
            return "ok"

        monkeypatch.setattr(ai_router, "_ask_llm", fake_llm)

        # 7-day window: only the two recent videos are cited.
        resp = await client.post("/api/ai/ask", json={
            "channel": CHANNEL, "platform": PLATFORM,
            "question": "build do xerath", "days": 7,
        })
        assert resp.status_code == 200
        assert "Stream antiga 03" not in captured["context"]
        assert "Recomendação de builds 01" in captured["context"]
        assert all(s["video_title"] != "Stream antiga 03" for s in resp.json()["sources"])

        # No window: the old video's mention is included.
        await client.post("/api/ai/ask", json={
            "channel": CHANNEL, "platform": PLATFORM,
            "question": "build do xerath",
        })
        assert "Stream antiga 03" in captured["context"]

    @pytest.mark.asyncio
    async def test_no_matches_short_circuits_without_llm(self, client, monkeypatch):
        _enable()
        called = {"n": 0}

        async def fake_llm(*args):
            called["n"] += 1
            return "should not be called"

        monkeypatch.setattr(ai_router, "_ask_llm", fake_llm)
        resp = await client.post("/api/ai/ask", json={
            "channel": "canal-sem-archivo", "platform": PLATFORM,
            "question": "build do xerath",
        })
        assert resp.status_code == 200
        assert called["n"] == 0
        assert resp.json()["sources"] == []
        assert "no matching" in resp.json()["answer"]


# ── LLM error mapping ───────────────────────────────────────────────────────

class TestAiAskLlmErrors:
    @pytest.mark.asyncio
    async def test_invalid_key_maps_to_401(self, client, monkeypatch):
        _enable()
        _seed_default_archive()

        class FakeResp:
            status_code = 401

        async def fake_post(api_key, payload):
            return FakeResp()

        monkeypatch.setattr(ai_router, "_post_chat_completion", fake_post)
        resp = await client.post("/api/ai/ask", json={
            "channel": CHANNEL, "platform": PLATFORM, "question": "build do xerath",
        })
        assert resp.status_code == 401
        assert "API key rejected" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_network_error_maps_to_502(self, client, monkeypatch):
        _enable()
        _seed_default_archive()

        async def fake_post(api_key, payload):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(ai_router, "_post_chat_completion", fake_post)
        resp = await client.post("/api/ai/ask", json={
            "channel": CHANNEL, "platform": PLATFORM, "question": "build do xerath",
        })
        assert resp.status_code == 502
        assert "could not be reached" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_provider_5xx_maps_to_502(self, client, monkeypatch):
        _enable()
        _seed_default_archive()

        class FakeResp:
            status_code = 500

        async def fake_post(api_key, payload):
            return FakeResp()

        monkeypatch.setattr(ai_router, "_post_chat_completion", fake_post)
        resp = await client.post("/api/ai/ask", json={
            "channel": CHANNEL, "platform": PLATFORM, "question": "build do xerath",
        })
        assert resp.status_code == 502
        assert "HTTP 500" in resp.json()["detail"]
