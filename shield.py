"""
shield.py — Cost Shield for azure-penny.

Background task that checks live resource costs against a monthly threshold
and sends Telegram alerts when the threshold is exceeded (repeat every 2 h
while breach persists).
"""

import asyncio
import json
import time
from pathlib import Path

import httpx

from config import (
    SHIELD_ALERT_COOLDOWN,
    SHIELD_CHECK_INTERVAL,
    SHIELD_MONTHLY_THRESHOLD,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    log,
)

# Config file — lives next to this module
_CONFIG_PATH = Path(__file__).parent / "shield_config.json"


# ── Config persistence ────────────────────────────────────────────────────────

def load_shield_config() -> dict:
    """Load config from JSON file, env vars as fallback for initial values."""
    cfg: dict = {
        "monthly_threshold": SHIELD_MONTHLY_THRESHOLD,
        "telegram_chat_id": TELEGRAM_CHAT_ID,
        "last_alerted_at": 0.0,
    }
    if _CONFIG_PATH.exists():
        try:
            saved = json.loads(_CONFIG_PATH.read_text())
            cfg.update(saved)
        except Exception as exc:
            log.warning("Shield: failed to read shield_config.json: %s", exc)
    return cfg


def save_shield_config(cfg: dict) -> None:
    """Persist config to JSON file."""
    try:
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except Exception as exc:
        log.error("Shield: failed to write shield_config.json: %s", exc)


# ── Telegram ──────────────────────────────────────────────────────────────────

async def send_telegram_message(chat_id: str, text: str) -> bool:
    """POST a message via Telegram Bot API. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        log.warning("Shield: Telegram not configured (missing token or chat_id)")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
            if resp.status_code == 200:
                return True
            log.error("Shield: Telegram API error %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.error("Shield: failed to send Telegram message: %s", exc)
    return False


def _build_alert_text(
    live_monthly_total: float,
    threshold: float,
    top_resources: list[dict],
    is_repeat: bool,
) -> str:
    pct_over = round((live_monthly_total - threshold) / threshold * 100, 1)
    header = "🔴 <b>Azure Cost Alert — STILL ACTIVE</b>" if is_repeat else "🚨 <b>Azure Cost Alert</b>"
    lines = [
        header,
        "",
        f"💰 Projected monthly:  <b>${live_monthly_total:.2f}</b>",
        f"🎯 Threshold:          <b>${threshold:.2f}</b>",
        f"📈 Exceeded by:        <b>${live_monthly_total - threshold:.2f} (+{pct_over}%)</b>",
        "",
        "Top cost consumers:",
    ]
    for i, r in enumerate(top_resources, 1):
        name = r.get("name") or r.get("resource_group") or "unknown"
        cost = r.get("monthly_cost") or 0.0
        lines.append(f"  {i}. {name} — <b>${cost:.2f}/mo</b>")
    lines.append("")
    lines.append("→ az-penny.mysak.fun/shield")
    return "\n".join(lines)


# ── Shield check ──────────────────────────────────────────────────────────────

async def run_shield_check(get_live_data_fn) -> dict:
    """
    One shield check cycle.

    Args:
        get_live_data_fn: async callable → list[dict] of live resources
    Returns status dict with keys: status, live_monthly_total, threshold, top_resources
    """
    cfg = load_shield_config()
    threshold = float(cfg.get("monthly_threshold") or 0)
    chat_id = str(cfg.get("telegram_chat_id") or TELEGRAM_CHAT_ID or "")

    if threshold <= 0:
        return {"status": "disabled", "threshold": 0, "live_monthly_total": 0, "top_resources": []}

    resources = await get_live_data_fn()
    live_monthly_total = sum(r.get("monthly_cost") or 0.0 for r in resources)

    top_resources = sorted(
        [r for r in resources if (r.get("monthly_cost") or 0.0) > 0],
        key=lambda r: r.get("monthly_cost") or 0.0,
        reverse=True,
    )[:10]

    base = {
        "live_monthly_total": round(live_monthly_total, 2),
        "threshold": threshold,
        "top_resources": top_resources,
    }

    if live_monthly_total <= threshold:
        return {**base, "status": "ok"}

    # Breach — check cooldown
    now = time.time()
    last_alerted = float(cfg.get("last_alerted_at") or 0)

    if now - last_alerted < SHIELD_ALERT_COOLDOWN:
        remaining = int(SHIELD_ALERT_COOLDOWN - (now - last_alerted))
        return {**base, "status": "breach_cooldown", "cooldown_remaining_s": remaining}

    # Send alert
    is_repeat = last_alerted > 0
    msg = _build_alert_text(live_monthly_total, threshold, top_resources, is_repeat)
    sent = await send_telegram_message(chat_id, msg)

    if sent:
        cfg["last_alerted_at"] = now
        save_shield_config(cfg)
        log.warning(
            "🛡 Shield alert sent (repeat=%s): $%.2f > $%.2f",
            is_repeat, live_monthly_total, threshold,
        )

    return {**base, "status": "breach_alerted" if sent else "breach_alert_failed"}


async def shield_check_loop(get_live_data_fn) -> None:
    """Infinite asyncio background loop. Runs a check every SHIELD_CHECK_INTERVAL seconds."""
    log.info("🛡 Shield loop started (interval=%ds, cooldown=%ds)", SHIELD_CHECK_INTERVAL, SHIELD_ALERT_COOLDOWN)
    while True:
        await asyncio.sleep(SHIELD_CHECK_INTERVAL)
        try:
            result = await run_shield_check(get_live_data_fn)
            log.debug("🛡 Shield check → %s (monthly=$%.2f)", result["status"], result["live_monthly_total"])
        except Exception as exc:
            log.exception("Shield check loop error: %s", exc)
