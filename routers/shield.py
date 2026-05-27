"""
routers/shield.py — Cost Shield API endpoints for azure-penny.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import SHIELD_ALERT_COOLDOWN, SHIELD_CHECK_INTERVAL, TELEGRAM_BOT_TOKEN, log
from shield import load_shield_config, run_shield_check, save_shield_config, send_telegram_message

router = APIRouter(tags=["api"])


@router.get("/api/shield/status")
async def api_shield_status() -> JSONResponse:
    """Current Shield status: threshold, live monthly total, breach flag, top 10 resources."""
    from config import TELEGRAM_CHAT_ID  # noqa: PLC0415
    from live_resources import _get_live_data  # noqa: PLC0415

    try:
        cfg = load_shield_config()
        resources = await _get_live_data()
        live_monthly_total = sum(r.get("monthly_cost") or 0.0 for r in resources)

        top_resources = sorted(
            [r for r in resources if (r.get("monthly_cost") or 0.0) > 0],
            key=lambda r: r.get("monthly_cost") or 0.0,
            reverse=True,
        )[:10]

        threshold      = float(cfg.get("monthly_threshold") or 0)
        last_alerted_at = float(cfg.get("last_alerted_at") or 0)
        chat_id        = str(cfg.get("telegram_chat_id") or "")

        breach   = threshold > 0 and live_monthly_total > threshold
        pct_used = round(live_monthly_total / threshold * 100, 1) if threshold > 0 else 0

        return JSONResponse({
            "threshold":             threshold,
            "live_monthly_total":    round(live_monthly_total, 2),
            "breach":                breach,
            "pct_used":              pct_used,
            "last_alerted_at":       last_alerted_at,
            "check_interval_s":      SHIELD_CHECK_INTERVAL,
            "cooldown_s":            SHIELD_ALERT_COOLDOWN,
            "telegram_configured":   bool(TELEGRAM_BOT_TOKEN and chat_id),
            "telegram_chat_id_set":  bool(chat_id),
            "top_resources": [
                {
                    "name":           r.get("name") or r.get("resource_group") or "unknown",
                    "type":           r.get("type", ""),
                    "resource_group": r.get("resource_group", ""),
                    "monthly_cost":   round(r.get("monthly_cost") or 0.0, 2),
                }
                for r in top_resources
            ],
        })
    except Exception as exc:
        log.exception("Shield status endpoint failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/shield/config")
async def api_shield_config_save(request: Request) -> JSONResponse:
    """Save Shield configuration (threshold, telegram_chat_id)."""
    try:
        body = await request.json()
        cfg  = load_shield_config()

        if "monthly_threshold" in body:
            cfg["monthly_threshold"] = float(body["monthly_threshold"])
        if "telegram_chat_id" in body:
            cfg["telegram_chat_id"] = str(body["telegram_chat_id"]).strip()

        save_shield_config(cfg)
        log.info("Shield config saved: threshold=%.2f", cfg.get("monthly_threshold", 0))
        return JSONResponse({"status": "saved", "config": {
            "monthly_threshold": cfg.get("monthly_threshold"),
            "telegram_chat_id":  cfg.get("telegram_chat_id"),
        }})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/shield/test")
async def api_shield_test() -> JSONResponse:
    """Send a test Telegram notification to verify configuration."""
    from config import TELEGRAM_CHAT_ID  # noqa: PLC0415
    from live_resources import _get_live_data  # noqa: PLC0415

    try:
        cfg     = load_shield_config()
        chat_id = str(cfg.get("telegram_chat_id") or TELEGRAM_CHAT_ID or "")

        if not TELEGRAM_BOT_TOKEN or not chat_id:
            return JSONResponse({"ok": False, "error": "Telegram not configured (missing token or chat_id)"})

        resources          = await _get_live_data()
        live_monthly_total = sum(r.get("monthly_cost") or 0.0 for r in resources)

        msg = (
            "🧪 <b>azure-penny Shield — Test Notification</b>\n\n"
            "✅ Telegram is working!\n"
            f"💰 Current projected monthly: <b>${live_monthly_total:.2f}</b>\n"
            f"🎯 Threshold: <b>${float(cfg.get('monthly_threshold') or 0):.2f}</b>\n\n"
            "→ az-penny.mysak.fun/shield"
        )
        sent = await send_telegram_message(chat_id, msg)
        return JSONResponse({"ok": sent, "chat_id": chat_id})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
