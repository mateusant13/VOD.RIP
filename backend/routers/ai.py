"""Experimental AI ask-about-channel — POST /api/ai/ask.

Single-turn RAG over the LOCAL archive (no agents): (1) FTS/keyword search
the channel's archived chat + transcripts via the same machinery the archive
search UI uses (services.archive_db.search), (2) assemble the top matching
segments as context, each tagged with video title/date, (3) one call to an
OpenAI-compatible chat-completions endpoint with the user's own API key
(Settings > Experimental, write-only).

The system prompt restricts the model to the provided context (no prompt
injection surface beyond the archive content itself) and requires citations.
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, HTTPException

from deps import settings_mgr
from models.schemas import AiAskRequest, AiAskResponse, AiSource
from services import archive_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ai"])

# OpenAI-compatible chat-completions base. Overridable for local providers
# (Ollama/LM Studio etc.) via env — the frontend stays unchanged.
AI_BASE_URL = "https://api.openai.com/v1"
AI_MODEL = "gpt-4o-mini"
AI_TIMEOUT_S = 30.0
# Guard rails: bounded question + bounded context, so the prompt can never
# grow unbounded and the LLM bill stays predictable.
AI_MAX_QUESTION_CHARS = 500
AI_CONTEXT_SEGMENTS = 20
AI_MAX_DAYS = 3650
# Search fetch size: > 30 lifts the per-video hit cap (3 → 60), so counting
# questions ("quantas vezes ele recomendou build x?") surface every mention
# of one video instead of its top 3; the top AI_CONTEXT_SEGMENTS feed the LLM.
AI_SEARCH_LIMIT = 60

_AI_SYSTEM_PROMPT = (
    "You are a research assistant for a streamer's VOD archive. Answer the "
    "user's question using ONLY the archive context provided below. Cite "
    "which video(s) and date(s) your answer comes from (video title + date). "
    "If the context contains no information relevant to the question, say so "
    "plainly and do not invent or extrapolate. Be concise."
)


async def _post_chat_completion(api_key: str, payload: dict) -> httpx.Response:
    """One chat-completions call. Separated from _ask_llm so tests can stub
    the HTTP boundary (status mapping) without touching httpx globally."""
    async with httpx.AsyncClient(timeout=AI_TIMEOUT_S) as client:
        return await client.post(
            f"{AI_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )


async def _ask_llm(api_key: str, question: str, context_text: str) -> str:
    """One LLM call; maps provider failures to user-facing HTTP errors."""
    payload = {
        "model": AI_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _AI_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Question: {question}\n\nArchive context:\n{context_text}",
            },
        ],
    }
    try:
        resp = await _post_chat_completion(api_key, payload)
    except httpx.HTTPError as exc:
        logger.warning("ai/ask: provider unreachable: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="The AI provider could not be reached — check your connection and try again.",
        ) from exc
    if resp.status_code in (401, 403):
        raise HTTPException(
            status_code=401,
            detail="AI API key rejected by the provider (HTTP 401/403) — check the key in Settings.",
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"AI provider error (HTTP {resp.status_code}).",
        )
    try:
        data = resp.json()
        answer = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("ai/ask: unexpected provider payload: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="AI provider returned an unexpected response.",
        ) from exc
    return str(answer).strip()


@router.post("/api/ai/ask", response_model=AiAskResponse)
async def ai_ask(body: AiAskRequest):
    settings = settings_mgr.get()
    if not getattr(settings, "experimental_ai_enabled", False):
        raise HTTPException(
            status_code=403,
            detail="Experimental AI is disabled — enable it in Settings (Experimental) and add an API key.",
        )
    api_key = (getattr(settings, "ai_api_key", "") or "").strip()
    if not api_key:
        # Defensive: the settings toggle cannot turn on without a key.
        raise HTTPException(
            status_code=400,
            detail="No AI API key configured — add one in Settings (Experimental).",
        )

    platform = (body.platform or "").strip().lower()
    if platform not in archive_db.PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail=f"platform must be one of {archive_db.PLATFORMS}",
        )
    channel = (body.channel or "").strip()
    if not channel:
        raise HTTPException(status_code=400, detail="channel required")
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question required")
    if len(question) > AI_MAX_QUESTION_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"question too long (max {AI_MAX_QUESTION_CHARS} characters)",
        )
    scope = (body.scope or "all").strip().lower()
    if scope not in ("chat", "transcript", "all"):
        raise HTTPException(
            status_code=400,
            detail="scope must be one of chat, transcript, all",
        )
    if body.days is not None and not (1 <= body.days <= AI_MAX_DAYS):
        raise HTTPException(
            status_code=400,
            detail=f"days must be between 1 and {AI_MAX_DAYS}, or null for the entire history",
        )

    # Days window → inclusive date bounds on the owning video's started_at
    # (the same filter the archive search UI passes as date_from/date_to).
    date_from = date_to = None
    if body.days is not None:
        today = datetime.now(timezone.utc).date()
        date_from = (today - timedelta(days=body.days)).isoformat()
        date_to = today.isoformat()

    hits = archive_db.search(
        question,
        platform=platform,
        channel=channel,
        source="chat" if scope == "chat" else ("transcript" if scope == "transcript" else "both"),
        date_from=date_from,
        date_to=date_to,
        limit=AI_SEARCH_LIMIT,
        mode="broad",
    )

    context_lines: list[str] = []
    sources: list[AiSource] = []
    for hit in hits[:AI_CONTEXT_SEGMENTS]:
        title = hit.get("title") or ""
        date = hit.get("date") or ""
        text = str(hit.get("text") or "")
        # Chat rows carry the author ('user: message'); transcript rows are
        # bare caption text.
        if hit.get("kind") == "message":
            author = hit.get("author") or "user"
            line = f"{author}: {text}"
        else:
            line = text
        tag = title if title else "(unknown video)"
        if date:
            tag = f"{tag} · {date}"
        context_lines.append(f"[{tag}] {line}")
        sources.append(AiSource(
            video_title=title or "(unknown video)",
            created_at=date or None,
            matched_text=text,
        ))

    if not context_lines:
        return AiAskResponse(
            answer="The local archive has no matching chat or transcript content for this question.",
            sources=[],
        )

    answer = await _ask_llm(api_key, question, "\n".join(context_lines))
    return AiAskResponse(answer=answer, sources=sources)
