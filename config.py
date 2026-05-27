import logging
import os

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("azure-penny")

STORAGE_ACCOUNT_NAME: str = os.environ.get("STORAGE_ACCOUNT_NAME", "")
STORAGE_CONTAINER_NAME: str = os.environ.get("STORAGE_CONTAINER_NAME", "cost-exports")
AZURE_CLIENT_ID: str | None = os.environ.get("AZURE_CLIENT_ID")
AZURE_SUBSCRIPTION_ID: str = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
TTL_SECONDS: int = int(os.environ.get("CACHE_TTL_SECONDS", "3600"))

# Tag key used to identify the application/project.
# Change this to match whatever tag key you use in Azure (case-insensitive match).
# E.g. "project", "Application", "app", "system", "workload"
COST_TAG_KEY: str = os.environ.get("COST_TAG_KEY", "project")
LIVE_CACHE_TTL: int = int(os.environ.get("LIVE_CACHE_TTL_SECONDS", "900"))
PROTECTED_RGS: set[str] = {
    rg.strip().lower() for rg in os.environ.get("PROTECTED_RGS", "").split(",") if rg.strip()
}

# ── Vertex proxy (LLM) ────────────────────────────────────────────────────────
VERTEX_PROXY_URL: str = os.environ.get("VERTEX_PROXY_URL", "")
VERTEX_PROXY_API_KEY: str = os.environ.get("VERTEX_PROXY_API_KEY", "")
CF_ACCESS_CLIENT_ID: str = os.environ.get("CF_ACCESS_CLIENT_ID", "")
CF_ACCESS_CLIENT_SECRET: str = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")

# ── Shield (cost alert) ───────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")
# Monthly budget threshold in EUR/USD — alert when projected monthly spend exceeds this.
# Set to 0 to disable Shield alerts.
SHIELD_MONTHLY_THRESHOLD: float = float(os.environ.get("SHIELD_MONTHLY_THRESHOLD", "0"))
# How often (seconds) the background task checks the threshold. Default: 900 = 15 min.
SHIELD_CHECK_INTERVAL: int = int(os.environ.get("SHIELD_CHECK_INTERVAL", "900"))
# Cooldown (seconds) between repeated alerts for the same breach. Default: 7200 = 2 h.
SHIELD_ALERT_COOLDOWN: int = int(os.environ.get("SHIELD_ALERT_COOLDOWN", "7200"))
