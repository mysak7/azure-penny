"""
routers/ai.py — AI chat endpoints and Telegram webhook for azure-penny.
"""

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ai_chat import (
    _CHAT_TOOLS,
    _TOOL_LABELS,
    _chat_headers,
    _execute_chat_tool,
    _run_ai_chat,
)
from ai_chat import _CHAT_MODEL, _CHAT_SYSTEM_PROMPT
from config import (
    CF_ACCESS_CLIENT_ID,
    CF_ACCESS_CLIENT_SECRET,
    TELEGRAM_WEBHOOK_SECRET,
    VERTEX_PROXY_API_KEY,
    VERTEX_PROXY_URL,
    log,
)
import telegram_bot

router = APIRouter(tags=["api"])


# ── Simple AI (single-shot, no tool use) ─────────────────────────────────────


@router.post("/api/ai")
async def api_ai(request: Request) -> JSONResponse:
    """Pošle dotaz na vertex-proxy (Gemini přes CF Tunnel + CF Access)."""
    if not VERTEX_PROXY_URL or not VERTEX_PROXY_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Vertex proxy not configured (VERTEX_PROXY_URL / VERTEX_PROXY_API_KEY missing)",
        )

    body = await request.json()
    question = body.get("question", "")
    model = body.get("model", "gemini-2.5-flash")
    if not question:
        raise HTTPException(status_code=400, detail="'question' field required")

    headers = {
        "Authorization": f"Bearer {VERTEX_PROXY_API_KEY}",
        "Content-Type": "application/json",
    }
    if CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET:
        headers["CF-Access-Client-Id"] = CF_ACCESS_CLIENT_ID
        headers["CF-Access-Client-Secret"] = CF_ACCESS_CLIENT_SECRET

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": question}],
    }

    try:
        import httpx  # noqa: PLC0415

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{VERTEX_PROXY_URL}/v1/chat/completions",
                headers=headers,
                json=payload,
            )
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"]
        return JSONResponse({"answer": answer, "model": model})
    except Exception as exc:
        log.exception("AI call failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Agentic AI chat with SSE streaming ───────────────────────────────────────


@router.post("/api/ai/chat")
async def api_ai_chat(request: Request) -> StreamingResponse:
    """AI chat with agentic tool-use loop; streams tokens via SSE."""

    def _sse(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    if not VERTEX_PROXY_URL or not VERTEX_PROXY_API_KEY:

        async def _cfg_err():
            yield _sse({"type": "error", "text": "Vertex proxy not configured"})

        return StreamingResponse(_cfg_err(), media_type="text/event-stream")

    body = await request.json()
    question = (body.get("question") or "").strip()
    history = (body.get("history") or [])[-20:]

    if not question:

        async def _empty():
            yield _sse({"type": "error", "text": "Empty question"})

        return StreamingResponse(_empty(), media_type="text/event-stream")

    import httpx  # noqa: PLC0415

    async def event_stream():
        messages: list[dict] = [{"role": "system", "content": _CHAT_SYSTEM_PROMPT}]
        messages += list(history)
        messages.append({"role": "user", "content": question})

        final_text = ""

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                for _iteration in range(5):
                    resp = await client.post(
                        f"{VERTEX_PROXY_URL}/v1/chat/completions",
                        headers=_chat_headers(),
                        json={
                            "model": _CHAT_MODEL,
                            "messages": messages,
                            "tools": _CHAT_TOOLS,
                            "tool_choice": "auto",
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    msg = data["choices"][0]["message"]
                    tc_list = msg.get("tool_calls") or []

                    if not tc_list:
                        final_text = msg.get("content") or ""
                        break

                    messages.append(
                        {
                            "role": "assistant",
                            "content": msg.get("content"),
                            "tool_calls": tc_list,
                        }
                    )

                    for tc in tc_list:
                        tool_name = tc["function"]["name"]
                        label = _TOOL_LABELS.get(tool_name, tool_name)
                        yield _sse({"type": "tool", "name": tool_name, "label": label})
                        await asyncio.sleep(0)

                        try:
                            tool_args = json.loads(
                                tc["function"].get("arguments", "{}")
                            )
                        except json.JSONDecodeError:
                            tool_args = {}

                        result = await _execute_chat_tool(tool_name, tool_args)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": result,
                            }
                        )

                # Final no-tools call if loop exhausted without a text answer.
                if not final_text:
                    resp = await client.post(
                        f"{VERTEX_PROXY_URL}/v1/chat/completions",
                        headers=_chat_headers(),
                        json={"model": _CHAT_MODEL, "messages": messages},
                    )
                    resp.raise_for_status()
                    final_text = (
                        resp.json()["choices"][0]["message"].get("content") or ""
                    )

        except Exception as exc:
            log.exception("AI chat agentic loop failed")
            yield _sse({"type": "error", "text": str(exc)})
            return

        # Stream the final answer word-by-word (typewriter effect).
        words = final_text.split(" ")
        for i, word in enumerate(words):
            chunk = word + (" " if i < len(words) - 1 else "")
            yield _sse({"type": "token", "text": chunk})
            await asyncio.sleep(0.022)

        yield _sse({"type": "done"})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── Telegram webhook ──────────────────────────────────────────────────────────


@router.post("/telegram/webhook", include_in_schema=False)
async def telegram_webhook(request: Request) -> JSONResponse:
    """Telegram Bot API webhook — receives updates and dispatches to the AI chat loop."""
    if TELEGRAM_WEBHOOK_SECRET:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token != TELEGRAM_WEBHOOK_SECRET:
            log.warning("Telegram webhook: rejected request with invalid secret token")
            return JSONResponse({"ok": False}, status_code=403)

    try:
        update = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    asyncio.create_task(telegram_bot.handle_update(update, _run_ai_chat))
    return JSONResponse({"ok": True})
