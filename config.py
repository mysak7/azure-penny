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
