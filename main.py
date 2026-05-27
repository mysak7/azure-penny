"""
azure-penny — Azure Cost Management dashboard
FastAPI application that reads Cost Management Parquet exports from Azure Blob
Storage and exposes aggregated cost data via a REST API.
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from config import APP_URL, TELEGRAM_BOT_TOKEN, log
from live_resources import _get_live_data
from shield import shield_check_loop
import telegram_bot

from routers import admin, ai, costs, infra, live, pages
from routers import shield as shield_router

# ---------------------------------------------------------------------------
# App version
# ---------------------------------------------------------------------------

APP_VERSION = "1.5.0"

# Propagate version to page router (used in template context).
pages._APP_VERSION  = APP_VERSION
admin._APP_VERSION  = APP_VERSION

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_: FastAPI):
    asyncio.create_task(_get_live_data())
    asyncio.create_task(shield_check_loop(_get_live_data))
    if TELEGRAM_BOT_TOKEN and APP_URL:
        asyncio.create_task(telegram_bot.register_webhook(APP_URL))
    yield


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="azure-penny",
    description="Azure Cost Management dashboard — reads Cost exports from Blob Storage.",
    version=APP_VERSION,
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------

app.include_router(pages.router)
app.include_router(admin.router)
app.include_router(shield_router.router)
app.include_router(costs.router)
app.include_router(live.router)
app.include_router(infra.router)
app.include_router(ai.router)
