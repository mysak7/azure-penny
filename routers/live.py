"""
routers/live.py — Live Azure resource endpoints for azure-penny.
"""

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from config import AZURE_SUBSCRIPTION_ID, log
from live_resources import _get_live_data, _live_cache, _live_lock, fetch_resource_metrics, list_resource_groups

router = APIRouter(tags=["api"])


@router.get("/api/live-resources")
async def api_live_resources() -> JSONResponse:
    try:
        resources = await _get_live_data()

        matched_export_usd = 0.0
        for r in resources:
            ec = r.get("export_cost") or r.get("export_cost_raw")
            if ec is not None:
                matched_export_usd += ec
            elif r.get("cost_source") == "export":
                matched_export_usd += r.get("monthly_cost") or 0

        return JSONResponse({
            "resources":           resources,
            "count":               len(resources),
            "subscription_id":     AZURE_SUBSCRIPTION_ID or "not configured",
            "matched_export_usd":  round(matched_export_usd, 2),
        })
    except Exception as exc:
        log.exception("Live resources endpoint failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/live-reload")
async def api_live_reload() -> JSONResponse:
    async with _live_lock:
        _live_cache.clear()
    try:
        resources = await _get_live_data()
        return JSONResponse({"status": "reloaded", "count": len(resources)})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/resource-metrics")
async def api_resource_metrics(resource_id: str, hours: int = 24) -> JSONResponse:
    """Fetch Azure Monitor metrics for a single ARM resource."""
    if not resource_id.lower().startswith("/subscriptions/"):
        raise HTTPException(status_code=400, detail="resource_id must be a full ARM resource ID")
    try:
        data = await asyncio.get_event_loop().run_in_executor(
            None, fetch_resource_metrics, resource_id, hours
        )
        return JSONResponse(data)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
