"""
routers/admin.py — Health, status, reload, debug, and diagnostics endpoints.
"""

import time

import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from config import COST_TAG_KEY, STORAGE_ACCOUNT_NAME, STORAGE_CONTAINER_NAME, log
from storage import _cache, _extract_all_tag_keys, _lock, get_blob_service_client, get_cached_dataframe

router = APIRouter(tags=["api"])

# Injected by main.py after app is created.
_APP_VERSION = "1.5.0"


# ── Health ────────────────────────────────────────────────────────────────────

@router.get("/health", tags=["health"])
async def health_check() -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "service": "azure-penny",
        "storage_account": STORAGE_ACCOUNT_NAME or "not configured",
        "container": STORAGE_CONTAINER_NAME,
    })


@router.get("/api/me", tags=["api"])
async def api_me(request: Request) -> JSONResponse:
    from auth import _get_user_roles  # noqa: PLC0415
    roles = _get_user_roles(request)
    return JSONResponse({
        "name":     request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME", ""),
        "roles":    roles,
        "is_admin": "penny-admin" in roles,
    })


# ── Status / reload ───────────────────────────────────────────────────────────

@router.get("/api/status", tags=["api"])
async def api_status() -> JSONResponse:
    cached_df: pd.DataFrame | None = _cache.get("df")
    loaded_at: float = _cache.get("loaded_at", 0.0)
    cache_age_s = round(time.monotonic() - loaded_at) if loaded_at else None
    periods: list[str] = []
    if cached_df is not None and "C_DATE" in cached_df.columns:
        dates = cached_df["C_DATE"].dropna().unique().tolist()
        if dates:
            dates.sort()
            periods = [dates[0], dates[-1]]
    return JSONResponse({
        "version":         _APP_VERSION,
        "storage_account": STORAGE_ACCOUNT_NAME or "not configured",
        "container":       STORAGE_CONTAINER_NAME,
        "row_count":       len(cached_df) if cached_df is not None else None,
        "date_range":      periods,
        "cache_age_s":     cache_age_s,
        "no_data":         cached_df is not None and cached_df.empty,
    })


@router.post("/api/reload", tags=["api"])
async def api_reload() -> JSONResponse:
    async with _lock:
        _cache.clear()
        log.info("Cache cleared via /api/reload")
    try:
        df = await get_cached_dataframe()
    except Exception as exc:
        log.exception("Reload failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse({"status": "reloaded", "rows_loaded": len(df)})


# ── Debug / diagnostics ───────────────────────────────────────────────────────

@router.get("/api/debug", tags=["api"])
async def api_debug() -> JSONResponse:
    try:
        client = get_blob_service_client()
        container_client = client.get_container_client(STORAGE_CONTAINER_NAME)
        all_blobs = [b.name for b in container_client.list_blobs()]

        df = await get_cached_dataframe()

        date_range = None
        if "C_DATE" in df.columns:
            dates = df["C_DATE"].dropna()
            if not dates.empty:
                date_range = {"min": dates.min(), "max": dates.max()}

        top_services: list = []
        if "C_SERVICE" in df.columns:
            top_services = df["C_SERVICE"].value_counts().head(20).index.tolist()

        top_rgs: list = []
        if "C_NAME" in df.columns:
            top_rgs = df["C_NAME"].value_counts().head(10).index.tolist()

        total_cost = round(float(df["C_COST"].sum()), 4) if "C_COST" in df.columns else None

        app_dist: list = []
        if "C_APP" in df.columns and "C_COST" in df.columns:
            by_app = (
                df.groupby("C_APP", dropna=False)["C_COST"]
                .sum()
                .sort_values(ascending=False)
                .head(20)
            )
            app_dist = [
                {"app": str(k), "cost": round(float(v), 4)}
                for k, v in by_app.items()
            ]

        tag_key_freq: dict[str, int] = {}
        if "C_TAGS" in df.columns:
            sample = df["C_TAGS"].dropna().head(500)
            for keys in sample.apply(_extract_all_tag_keys):
                for k in keys:
                    tag_key_freq[k] = tag_key_freq.get(k, 0) + 1
        tag_keys_ranked = sorted(tag_key_freq.items(), key=lambda x: -x[1])

        untagged_tags_sample: list = []
        if "C_TAGS" in df.columns and "C_APP" in df.columns:
            untagged_rows = df[df["C_APP"] == "Untagged"]["C_TAGS"].dropna().head(5).tolist()
            untagged_tags_sample = [str(v) for v in untagged_rows]

        return JSONResponse({
            "blob_count":           len(all_blobs),
            "blob_paths_sample":    all_blobs[:20],
            "row_count":            len(df),
            "columns":              list(df.columns),
            "date_range":           date_range,
            "total_cost":           total_cost,
            "top_services":         top_services,
            "top_resource_groups":  top_rgs,
            "cost_tag_key_configured":  COST_TAG_KEY,
            "c_app_distribution":       app_dist,
            "tag_keys_in_data":         [{"key": k, "count": c} for k, c in tag_keys_ranked[:30]],
            "untagged_raw_tags_sample": untagged_tags_sample,
        })
    except Exception as exc:
        log.exception("Debug endpoint failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/diagnostics", tags=["api"])
async def api_diagnostics() -> JSONResponse:
    from live_resources import _get_live_data, _live_cache  # noqa: PLC0415

    now = time.monotonic()
    result: dict = {}

    loaded_at = _cache.get("loaded_at", 0.0)
    result["blob_cache_age_seconds"] = round(now - loaded_at) if loaded_at else None
    df = _cache.get("df")
    if df is not None and not df.empty:
        result["export_row_count"] = len(df)
        if "C_DATE" in df.columns:
            result["export_date_min"]    = str(df["C_DATE"].min())
            result["export_date_max"]    = str(df["C_DATE"].max())
            result["export_unique_days"] = int(df["C_DATE"].nunique())
    else:
        result["export_row_count"] = 0

    live_ts = _live_cache.get("ts", 0.0)
    result["live_cache_age_seconds"] = round(now - live_ts) if live_ts else None
    inv = _live_cache.get("inv")
    result["live_resource_count"] = len(inv) if inv else None

    try:
        resources = await _get_live_data()
        source_counts: dict[str, int] = {}
        for r in resources:
            src = r.get("cost_source") or "none"
            source_counts[src] = source_counts.get(src, 0) + 1
        result["cost_source_breakdown"] = dict(
            sorted(source_counts.items(), key=lambda x: x[1], reverse=True)
        )
    except Exception as exc:
        result["cost_source_breakdown"] = {}
        result["live_error"] = str(exc)

    return JSONResponse(result)


@router.get("/api/spot-price-debug", tags=["api"])
async def api_spot_price_debug(vm_size: str, region: str) -> JSONResponse:
    import asyncio  # noqa: PLC0415
    from live_resources import _fetch_spot_price  # noqa: PLC0415

    try:
        price = await asyncio.get_event_loop().run_in_executor(
            None, _fetch_spot_price, vm_size, region
        )
        if price is None:
            return JSONResponse({"vm_size": vm_size, "region": region, "price": None, "status": "not_found"})
        return JSONResponse({
            "vm_size":     vm_size,
            "region":      region,
            "hourly_usd":  round(price, 4),
            "monthly_usd": round(price * 24 * 30, 2),
            "status":      "found",
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc), "vm_size": vm_size, "region": region}, status_code=500)
