"""
telegram_bot.py — Telegram webhook handler for azure-penny chat.

Receives Telegram updates via POST webhook, runs the shared AI agentic loop,
and sends the formatted answer back to the same chat.
"""

import html
import re
from typing import Awaitable, Callable

import httpx

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, log

# ---------------------------------------------------------------------------
# Per-chat conversation history (in-process; cleared on restart)
# ---------------------------------------------------------------------------

_tg_history: dict[int, list[dict]] = {}
_HISTORY_LIMIT = 20  # total messages kept per chat_id (user+assistant pairs)

# ---------------------------------------------------------------------------
# Telegram Bot API helpers
# ---------------------------------------------------------------------------


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


async def _tg_send(chat_id: int, text: str) -> None:
    """Send an HTML-mode message, splitting at Telegram's 4096-char limit."""
    if not TELEGRAM_BOT_TOKEN:
        return
    # Guarantee at least one chunk even for empty string
    chunks = [text[i : i + 4096] for i in range(0, max(len(text), 1), 4096)]
    async with httpx.AsyncClient(timeout=15) as client:
        for chunk in chunks:
            try:
                await client.post(
                    _api_url("sendMessage"),
                    json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"},
                )
            except Exception:
                log.exception("Telegram sendMessage failed for chat_id=%s", chat_id)


async def _tg_typing(chat_id: int) -> None:
    """Send the 'typing…' chat action (best-effort, errors silently ignored)."""
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                _api_url("sendChatAction"),
                json={"chat_id": chat_id, "action": "typing"},
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Markdown → Telegram HTML converter
# ---------------------------------------------------------------------------


def _md_to_html(text: str) -> str:
    """Convert LLM markdown to Telegram's HTML subset.

    Supported: ```code blocks```, **bold**, *italic*, `inline code`, # headers,
    - / * bullet lists.  Everything else is HTML-escaped so special chars
    (< > & etc.) never break the message.
    """
    lines = text.split("\n")
    result: list[str] = []
    in_code = False
    code_buf: list[str] = []

    for line in lines:
        # ── Code fence (``` … ```) ────────────────────────────────────────────
        if line.strip().startswith("```"):
            if not in_code:
                in_code = True
                code_buf = []
            else:
                in_code = False
                result.append(
                    "<pre><code>" + html.escape("\n".join(code_buf)) + "</code></pre>"
                )
                code_buf = []
            continue

        if in_code:
            code_buf.append(line)
            continue

        # ── Regular line: HTML-escape first, then apply inline markup ─────────
        line = html.escape(line)

        # Bullet list: leading "- " or "* " → "• "
        line = re.sub(r"^[-*]\s+", "• ", line)

        # Headers (##, ###, #) → bold
        line = re.sub(r"^#{1,3}\s+(.+)$", r"<b>\1</b>", line)

        # **bold** (must come before *italic* so we don't mis-match the stars)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)

        # *italic*
        line = re.sub(r"\*(.+?)\*", r"<i>\1</i>", line)

        # `inline code`
        line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)

        result.append(line)

    # Flush an unclosed code block
    if in_code and code_buf:
        result.append(
            "<pre><code>" + html.escape("\n".join(code_buf)) + "</code></pre>"
        )

    return "\n".join(result)


# ---------------------------------------------------------------------------
# Webhook registration
# ---------------------------------------------------------------------------


async def register_webhook(app_url: str) -> None:
    """Register this app as the Telegram bot's webhook.

    Idempotent — safe to call on every startup.
    Skips silently when TELEGRAM_BOT_TOKEN or app_url is not set.
    """
    from config import (
        TELEGRAM_WEBHOOK_SECRET,
    )  # imported here to keep module-level clean

    if not TELEGRAM_BOT_TOKEN or not app_url:
        log.warning(
            "Telegram webhook registration skipped (TELEGRAM_BOT_TOKEN=%s, APP_URL=%s)",
            bool(TELEGRAM_BOT_TOKEN),
            bool(app_url),
        )
        return

    webhook_url = app_url.rstrip("/") + "/telegram/webhook"
    payload: dict = {"url": webhook_url, "max_connections": 10}
    if TELEGRAM_WEBHOOK_SECRET:
        payload["secret_token"] = TELEGRAM_WEBHOOK_SECRET

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(_api_url("setWebhook"), json=payload)
            data = resp.json()
        if data.get("ok"):
            log.info("Telegram webhook registered → %s", webhook_url)
        else:
            log.warning("Telegram webhook registration failed: %s", data)
    except Exception as exc:
        log.warning("Telegram webhook registration error: %s", exc)


# ---------------------------------------------------------------------------
# Update handler
# ---------------------------------------------------------------------------

_RunAiChat = Callable[[str, list], Awaitable[str]]


async def handle_update(update: dict, run_ai_chat: _RunAiChat) -> None:
    """Process one Telegram update object.

    Args:
        update:       Raw JSON dict from Telegram's Bot API.
        run_ai_chat:  Async callable ``(question: str, history: list) → str``.
    """
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat_id: int | None = (message.get("chat") or {}).get("id")
    text: str = (message.get("text") or "").strip()

    if not chat_id or not text:
        return

    # ── Whitelist ─────────────────────────────────────────────────────────────
    if TELEGRAM_CHAT_ID and str(chat_id) != str(TELEGRAM_CHAT_ID):
        log.debug(
            "Telegram: ignoring message from chat_id=%s (not whitelisted)", chat_id
        )
        return

    # ── /start → greeting ─────────────────────────────────────────────────────
    if text.lower() == "/start":
        await _tg_send(
            chat_id,
            (
                "👋 Ahoj! Jsem <b>azure-penny</b> — tvůj Azure cost asistent.\n\n"
                "Zeptej se mě na cokoliv ohledně nákladů. Například:\n"
                "• <i>Kolik mě stojí Azure tento týden?</i>\n"
                "• <i>Jaké jsou největší náklady?</i>\n"
                "• <i>Jsou nějaké cost anomálie?</i>\n\n"
                "Konverzace si pamatuji — můžeš se ptát v kontextu."
            ),
        )
        return

    # Skip other slash commands
    if text.startswith("/"):
        return

    # ── Conversation history ──────────────────────────────────────────────────
    history = _tg_history.get(chat_id, [])

    # ── Typing indicator ──────────────────────────────────────────────────────
    await _tg_typing(chat_id)

    # ── AI chat loop ──────────────────────────────────────────────────────────
    try:
        answer = await run_ai_chat(text, history)
    except Exception as exc:
        log.exception("Telegram AI chat failed for chat_id=%s", chat_id)
        await _tg_send(chat_id, f"❌ Chyba při zpracování: {html.escape(str(exc))}")
        return

    # ── Persist history (cap at limit) ───────────────────────────────────────
    _tg_history[chat_id] = (
        history
        + [{"role": "user", "content": text}, {"role": "assistant", "content": answer}]
    )[-_HISTORY_LIMIT:]

    # ── Send formatted response ───────────────────────────────────────────────
    await _tg_send(chat_id, _md_to_html(answer))
