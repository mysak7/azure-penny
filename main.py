"""
azure-penny — Azure Cost Management dashboard
FastAPI application that reads Cost Management Parquet exports from Azure Blob
Storage and exposes aggregated cost data via a REST API.
"""

import asyncio
import base64
import calendar
import json
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from config import AZURE_SUBSCRIPTION_ID, PROTECTED_RGS, STORAGE_ACCOUNT_NAME, STORAGE_CONTAINER_NAME, log
from live_resources import _get_live_data, _live_cache, _live_lock, fetch_resource_metrics, list_resource_groups
from storage import _cache, _lock, get_blob_service_client, get_cached_dataframe

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(
    title="azure-penny",
    description="Azure Cost Management dashboard — reads Cost exports from Blob Storage.",
    version="1.0.0",
)

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# ---------------------------------------------------------------------------
# Role-based access (ACA Easy Auth injects X-MS-CLIENT-PRINCIPAL)
# ---------------------------------------------------------------------------

def _get_user_roles(request: Request) -> list[str]:
    """Return Entra app role values from the ACA Easy Auth principal header."""
    header = request.headers.get("X-MS-CLIENT-PRINCIPAL")
    if not header:
        return []  # no Easy Auth (local dev) — caller decides how to handle
    try:
        principal = json.loads(base64.b64decode(header + "=="))
        return [c["val"] for c in principal.get("claims", []) if c.get("typ") == "roles"]
    except Exception:
        return []


def _require_admin(request: Request) -> None:
    """FastAPI dependency — blocks non-admin users when Easy Auth is active."""
    header = request.headers.get("X-MS-CLIENT-PRINCIPAL")
    if not header:
        return  # local dev: no Easy Auth header, allow through
    if "penny-admin" not in _get_user_roles(request):
        raise HTTPException(status_code=403, detail="penny-admin role required")


# ---------------------------------------------------------------------------
# Azure service categories  (MeterCategory values)
# ---------------------------------------------------------------------------

CAT_COMPUTE = {
    "Virtual Machines", "App Service", "Container Apps", "Azure Functions",
    "Container Instances", "Azure Kubernetes Service", "Batch", "Cloud Services",
}
CAT_STORAGE = {
    "Storage", "Azure Data Lake Storage", "Backup", "StorSimple",
    "Azure NetApp Files", "Managed Disks",
}
CAT_NETWORK = {
    "Virtual Network", "Load Balancer", "Application Gateway", "Azure DNS",
    "Azure Front Door", "Bandwidth", "VPN Gateway", "Azure Bastion",
    "Azure Firewall", "Network Watcher", "Traffic Manager",
}
CAT_DATABASE = {
    "SQL Database", "Azure Cosmos DB", "Azure Cache for Redis",
    "Azure Database for MySQL", "Azure Database for PostgreSQL",
    "Azure SQL Managed Instance", "Azure Synapse Analytics",
}
CAT_MONITORING = {
    "Azure Grafana Service", "Log Analytics", "Azure Monitor",
    "Application Insights",
}

# ---------------------------------------------------------------------------
# Filtering / aggregation helpers
# ---------------------------------------------------------------------------

_TRANSFER_KEYWORDS = frozenset([
    "bandwidth", "transfer", "egress", "geo-redundant replication",
    "data retrieval", "replication",
])


def _period_days(period: str) -> int:
    return {"day": 1, "week": 7, "month": 30}.get(period, 7)


def _filter_period(df: pd.DataFrame, days: int) -> pd.DataFrame:
    if "C_DATE" not in df.columns:
        return df
    cutoff = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    return df[df["C_DATE"] >= cutoff]


def _filter_services(df: pd.DataFrame, services: set[str]) -> pd.DataFrame:
    if "C_SERVICE" not in df.columns:
        return df
    lower = {s.lower() for s in services}
    return df[df["C_SERVICE"].str.lower().isin(lower)]


def _filter_rg(df: pd.DataFrame, rg: str) -> pd.DataFrame:
    if not rg or "C_NAME" not in df.columns:
        return df
    return df[df["C_NAME"].str.lower() == rg.lower()]


def _cost_by(df: pd.DataFrame, col: str) -> dict[str, float]:
    if col not in df.columns or "C_COST" not in df.columns:
        return {}
    grp = df.groupby(col)["C_COST"].sum()
    return grp[grp > 0].sort_values(ascending=False).round(6).to_dict()


async def _category_api(period: str, services: set[str], rg: str = "") -> dict:
    full_df = await get_cached_dataframe()
    df = _filter_rg(full_df, rg)
    days = _period_days(period)
    filtered = _filter_services(_filter_period(df, days), services)

    fallback = False
    if period == "day" and filtered.empty and "C_DATE" in full_df.columns:
        df_svc = _filter_services(df, services)
        if not df_svc.empty:
            last_date = full_df["C_DATE"].dropna().max()
            filtered = df_svc[df_svc["C_DATE"] == last_date]
            fallback = True

    by_svc = _cost_by(filtered, "C_SERVICE")
    data_as_of = None
    if not filtered.empty and "C_DATE" in filtered.columns:
        data_as_of = filtered["C_DATE"].dropna().max()

    return {
        "period": period,
        "source": "Cost Management / Blob Storage",
        "services": [{"service": k, "cost_usd": v} for k, v in by_svc.items()],
        "total_usd": round(sum(by_svc.values()), 4),
        "data_as_of": data_as_of,
        "fallback": fallback,
    }


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------

async def _build_forecast(rg: str = "") -> dict:
    today = date.today()
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    month_str = today.strftime("%Y-%m")

    full_df = await get_cached_dataframe()
    df = _filter_rg(full_df, rg)
    actual_points: list[dict] = []
    spent_so_far = 0.0
    per_rg_actual: dict[str, float] = {}

    if not df.empty and "C_DATE" in df.columns and "C_COST" in df.columns:
        this_month = df[df["C_DATE"].str.startswith(month_str)]
        if not this_month.empty:
            daily = this_month.groupby("C_DATE")["C_COST"].sum().sort_index()
            actual_points = [{"date": d, "cost_usd": round(float(v), 4)} for d, v in daily.items()]
            spent_so_far = round(float(daily.sum()), 4)
            if "C_NAME" in this_month.columns:
                rg_agg = this_month.groupby("C_NAME")["C_COST"].sum()
                per_rg_actual = {
                    str(k): float(v) for k, v in rg_agg.items()
                    if v > 0 and str(k).strip()
                }

    # billing_days = how many days of billing data we actually have (lags by 1-3 days).
    # days_elapsed = calendar position in the month (always today.day).
    billing_days = len(actual_points) if actual_points else today.day
    days_elapsed = today.day
    days_remaining = days_in_month - days_elapsed

    # Both global and per-RG forecasts use live ARM pricing when available —
    # billing history can include shared/pass-through costs that inflate per-RG baselines.
    data_source = "linear"
    live_daily_rate = 0.0
    per_rg_live_monthly: dict[str, float] = {}
    try:
        all_resources = await _get_live_data()
        filtered_resources = all_resources if not rg else [
            r for r in all_resources if (r.get("resource_group") or "").lower() == rg.lower()
        ]
        live_monthly = sum(
            (r.get("monthly_cost") or 0.0) for r in filtered_resources if (r.get("monthly_cost") or 0.0) > 0
        )
        live_daily_rate = live_monthly / 30
        if live_daily_rate > 0:
            data_source = "live"
            for r in filtered_resources:
                rg_name = (r.get("resource_group") or "").strip()
                monthly = r.get("monthly_cost") or 0.0
                if rg_name and monthly > 0:
                    per_rg_live_monthly[rg_name] = per_rg_live_monthly.get(rg_name, 0.0) + monthly
    except Exception:
        pass

    if data_source == "live":
        daily_fwd = live_daily_rate
    elif rg and live_daily_rate == 0:
        # Specific RG selected, no live ARM resources found — nothing is running,
        # so don't project future spend beyond what's already been billed.
        daily_fwd = 0.0
    else:
        # Spread known spend over full elapsed calendar days to avoid inflated rates
        # when billing lags (e.g. only 2 billing data points 11 days into the month).
        daily_fwd = (spent_so_far / days_elapsed) if days_elapsed > 0 else 0.0

    last_date = (
        date.fromisoformat(actual_points[-1]["date"]) if actual_points
        else today.replace(day=1) - timedelta(days=1)
    )
    # Fill billing-lag gap (days after last billed date up to today) with $0 actual points
    # so the chart always spans the full elapsed period without fake orange bars over the past.
    if actual_points:
        gap_day = last_date + timedelta(days=1)
        while gap_day <= today and gap_day.month == today.month:
            actual_points.append({"date": gap_day.strftime("%Y-%m-%d"), "cost_usd": 0.0})
            gap_day += timedelta(days=1)

    # Project future days (tomorrow onwards) — orange bars never appear over past dates.
    # Always generate even when daily_fwd == 0 so the chart spans the full month.
    projected_points: list[dict] = []
    d = today + timedelta(days=1)
    while d.month == today.month:
        projected_points.append({"date": d.strftime("%Y-%m-%d"), "cost_usd": round(daily_fwd, 4)})
        d += timedelta(days=1)

    # end_of_month projection covers billing lag + future (full window from last billed day).
    full_remaining_days = (date(today.year, today.month, days_in_month) - last_date).days
    end_of_month = round(spent_so_far + daily_fwd * full_remaining_days, 2)

    # Per-RG projections also use full_remaining_days so the totals stay consistent.
    combined: dict[str, float] = {}
    if data_source == "live" and per_rg_live_monthly:
        for rg_name, live_monthly_rg in per_rg_live_monthly.items():
            actual_so_far = per_rg_actual.get(rg_name, 0.0)
            combined[rg_name] = actual_so_far + (live_monthly_rg / 30) * full_remaining_days
    elif days_elapsed > 0:
        daily_per_rg = {name: v / days_elapsed for name, v in per_rg_actual.items()}
        for name, actual_so_far in per_rg_actual.items():
            combined[name] = actual_so_far + daily_per_rg[name] * full_remaining_days

    top_rgs = sorted(
        [
            {"name": rg, "projected_usd": round(cost, 2)}
            for rg, cost in combined.items()
            if cost > 0 and rg.strip()
        ],
        key=lambda x: x["projected_usd"],
        reverse=True,
    )[:3]

    return {
        "actual_points": actual_points,
        "projected_points": projected_points,
        "end_of_month_usd": end_of_month,
        "spent_so_far_usd": spent_so_far,
        "live_daily_rate_usd": round(live_daily_rate, 4),
        "calibration_factor": None,
        "data_source": data_source,
        "top_rgs": top_rgs,
        "days_remaining": days_remaining,
        "days_elapsed": days_elapsed,
        "month": today.strftime("%B %Y"),
    }


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/manager", response_class=HTMLResponse, include_in_schema=False)
async def manager(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/technician", response_class=HTMLResponse, include_in_schema=False)
async def technician(request: Request):
    return templates.TemplateResponse("technician.html", {"request": request})


@app.get("/live", response_class=HTMLResponse, include_in_schema=False)
async def live_view(request: Request):
    is_admin = "penny-admin" in _get_user_roles(request)
    return templates.TemplateResponse("live.html", {"request": request, "is_admin": is_admin})


@app.get("/guide", response_class=HTMLResponse, include_in_schema=False)
async def guide_view(request: Request):
    return templates.TemplateResponse("guide.html", {"request": request})


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["health"])
async def health_check() -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "service": "azure-penny",
        "storage_account": STORAGE_ACCOUNT_NAME or "not configured",
        "container": STORAGE_CONTAINER_NAME,
    })


@app.get("/api/me", tags=["api"])
async def api_me(request: Request) -> JSONResponse:
    roles = _get_user_roles(request)
    return JSONResponse({
        "name": request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME", ""),
        "roles": roles,
        "is_admin": "penny-admin" in roles,
    })


# ── Status / reload ───────────────────────────────────────────────────────────

@app.get("/api/status", tags=["api"])
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
        "storage_account": STORAGE_ACCOUNT_NAME or "not configured",
        "container": STORAGE_CONTAINER_NAME,
        "row_count": len(cached_df) if cached_df is not None else None,
        "date_range": periods,
        "cache_age_s": cache_age_s,
        "no_data": cached_df is not None and cached_df.empty,
    })


@app.post("/api/reload", tags=["api"])
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


# ── Category endpoints ────────────────────────────────────────────────────────

@app.get("/api/compute", tags=["api"])
async def api_compute(period: str = "week", rg: str = "") -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_COMPUTE, rg))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/compute/machines", tags=["api"])
async def api_compute_machines(period: str = "week", rg: str = "") -> JSONResponse:
    try:
        full_df = await get_cached_dataframe()
        df = _filter_rg(full_df, rg)
        days = _period_days(period)
        filtered = _filter_services(_filter_period(df, days), CAT_COMPUTE)

        fallback = False
        if period == "day" and filtered.empty and "C_DATE" in full_df.columns:
            df_svc = _filter_services(df, CAT_COMPUTE)
            if not df_svc.empty:
                last_date = full_df["C_DATE"].dropna().max()
                filtered = df_svc[df_svc["C_DATE"] == last_date]
                fallback = True

        by_machine = _cost_by(filtered, "C_NAME")
        data_as_of = None
        if not filtered.empty and "C_DATE" in filtered.columns:
            data_as_of = filtered["C_DATE"].dropna().max()

        return JSONResponse({
            "period": period,
            "source": "Cost Management / Blob Storage",
            "machines": [{"name": k, "cost_usd": v} for k, v in by_machine.items()],
            "total_usd": round(sum(by_machine.values()), 4),
            "data_as_of": data_as_of,
            "fallback": fallback,
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/storage", tags=["api"])
async def api_storage(period: str = "week", rg: str = "") -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_STORAGE, rg))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/storage/breakdown", tags=["api"])
async def api_storage_breakdown(period: str = "week", rg: str = "") -> JSONResponse:
    try:
        full_df = await get_cached_dataframe()
        df = _filter_rg(full_df, rg)
        days = _period_days(period)

        all_storage_svcs = CAT_STORAGE | {"Bandwidth"}
        filtered = _filter_services(_filter_period(df, days), all_storage_svcs)

        fallback = False
        if period == "day" and filtered.empty and "C_DATE" in full_df.columns:
            df_svc = _filter_services(df, all_storage_svcs)
            if not df_svc.empty:
                last_date = full_df["C_DATE"].dropna().max()
                filtered = df_svc[df_svc["C_DATE"] == last_date]
                fallback = True

        if filtered.empty or "C_COST" not in filtered.columns:
            return JSONResponse({
                "period": period, "total_usd": 0,
                "space_usd": 0, "transfer_usd": 0,
                "by_service": [], "fallback": fallback,
            })

        filtered = filtered.copy()
        has_sub = "C_SUBCATEGORY" in filtered.columns

        def _row_type(svc: str, sub: str) -> str:
            combined = (svc + " " + sub).lower()
            return "transfer" if any(k in combined for k in _TRANSFER_KEYWORDS) else "space"

        if has_sub:
            filtered["_type"] = filtered.apply(
                lambda r: _row_type(str(r.get("C_SERVICE", "")), str(r.get("C_SUBCATEGORY", ""))),
                axis=1,
            )
        else:
            filtered["_type"] = filtered["C_SERVICE"].apply(lambda s: _row_type(str(s), ""))

        space_total = float(filtered[filtered["_type"] == "space"]["C_COST"].sum())
        transfer_total = float(filtered[filtered["_type"] == "transfer"]["C_COST"].sum())
        by_svc = _cost_by(filtered, "C_SERVICE")
        data_as_of = None
        if not filtered.empty and "C_DATE" in filtered.columns:
            data_as_of = filtered["C_DATE"].dropna().max()

        return JSONResponse({
            "period": period,
            "total_usd": round(space_total + transfer_total, 4),
            "space_usd": round(space_total, 4),
            "transfer_usd": round(transfer_total, 4),
            "by_service": [{"service": k, "cost_usd": v} for k, v in by_svc.items()],
            "data_as_of": data_as_of,
            "fallback": fallback,
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/network", tags=["api"])
async def api_network(period: str = "week", rg: str = "") -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_NETWORK, rg))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/database", tags=["api"])
async def api_database(period: str = "week", rg: str = "") -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_DATABASE, rg))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/monitoring", tags=["api"])
async def api_monitoring(period: str = "week", rg: str = "") -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_MONITORING, rg))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Raw costs (JSON) ──────────────────────────────────────────────────────────

@app.get("/costs", tags=["costs"])
async def get_costs() -> JSONResponse:
    try:
        df = await get_cached_dataframe()
    except Exception as exc:
        log.exception("Failed to load cost data")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    group_cols = [c for c in ["C_SERVICE", "C_NAME", "C_DATE"] if c in df.columns]
    if not group_cols or "C_COST" not in df.columns:
        raise HTTPException(
            status_code=500,
            detail="Required columns (C_COST + at least one grouping column) not found.",
        )

    aggregated = (
        df.groupby(group_cols, dropna=False)["C_COST"]
        .sum()
        .reset_index()
        .sort_values("C_COST", ascending=False)
    )
    aggregated["C_COST"] = aggregated["C_COST"].round(4)
    return JSONResponse({"data": aggregated.to_dict(orient="records")})


@app.get("/costs/summary", tags=["costs"])
async def get_costs_summary() -> JSONResponse:
    try:
        df = await get_cached_dataframe()
    except Exception as exc:
        log.exception("Failed to load cost data")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if "C_COST" not in df.columns:
        raise HTTPException(status_code=500, detail="C_COST column not found.")

    total = round(float(df["C_COST"].sum()), 4)

    def top5(col: str) -> list[dict]:
        if col not in df.columns:
            return []
        return (
            df.groupby(col, dropna=False)["C_COST"]
            .sum()
            .sort_values(ascending=False)
            .head(5)
            .round(4)
            .reset_index()
            .rename(columns={col: "name", "C_COST": "cost"})
            .to_dict(orient="records")
        )

    return JSONResponse({
        "total_cost": total,
        "top_services": top5("C_SERVICE"),
        "top_resource_groups": top5("C_NAME"),
    })


@app.get("/costs/refresh", tags=["costs"])
async def refresh_cache() -> JSONResponse:
    async with _lock:
        _cache.clear()
    try:
        df = await get_cached_dataframe()
    except Exception as exc:
        log.exception("Reload failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse({"status": "refreshed", "rows_loaded": len(df), "columns": list(df.columns)})


@app.get("/api/debug", tags=["api"])
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

        return JSONResponse({
            "blob_count": len(all_blobs),
            "blob_paths_sample": all_blobs[:20],
            "row_count": len(df),
            "columns": list(df.columns),
            "date_range": date_range,
            "total_cost": total_cost,
            "top_services": top_services,
            "top_resource_groups": top_rgs,
        })
    except Exception as exc:
        log.exception("Debug endpoint failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Technician / services ─────────────────────────────────────────────────────

@app.get("/api/services", tags=["api"])
async def api_services(period: str = "week", rg: str = "") -> JSONResponse:
    try:
        full_df = await get_cached_dataframe()
        df = _filter_rg(full_df, rg)
        days = _period_days(period)
        filtered = _filter_period(df, days)

        fallback = False
        if period == "day" and filtered.empty and "C_DATE" in full_df.columns and not df.empty:
            last_date = full_df["C_DATE"].dropna().max()
            filtered = df[df["C_DATE"] == last_date]
            fallback = True

        by_svc = _cost_by(filtered, "C_SERVICE")
        data_as_of = None
        if not filtered.empty and "C_DATE" in filtered.columns:
            data_as_of = filtered["C_DATE"].dropna().max()

        return JSONResponse({
            "period": period,
            "services": [{"service": k, "cost_usd": v} for k, v in by_svc.items()],
            "total_usd": round(sum(by_svc.values()), 4),
            "data_as_of": data_as_of,
            "fallback": fallback,
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/live-services", tags=["api"])
async def api_live_services() -> JSONResponse:
    try:
        df = await get_cached_dataframe()
        if df.empty or "C_SERVICE" not in df.columns or "C_COST" not in df.columns:
            return JSONResponse({"services": [], "total_usd": 0})

        cutoff = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
        mdf = df[df["C_DATE"] >= cutoff] if "C_DATE" in df.columns else df

        by_svc = mdf.groupby("C_SERVICE")["C_COST"].sum().sort_values(ascending=False)
        services = [
            {"name": str(svc), "monthly_cost": round(float(cost), 2), "category": "service"}
            for svc, cost in by_svc.items()
            if cost > 0
        ]

        return JSONResponse({
            "services": services,
            "count": len(services),
            "total_usd": round(float(by_svc.sum()), 2),
        })
    except Exception as exc:
        log.exception("Live services endpoint failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/cost-search", tags=["api"])
async def api_cost_search(q: str) -> JSONResponse:
    try:
        df = await get_cached_dataframe()
        if df.empty or "C_NAME" not in df.columns:
            return JSONResponse({"results": [], "count": 0})

        mask = df["C_NAME"].str.contains(q, case=False, na=False)
        matches = df[mask][["C_NAME", "C_RESOURCE_ID", "C_COST", "C_DATE"]].drop_duplicates(subset=["C_RESOURCE_ID"]).to_dict("records")

        return JSONResponse({
            "query": q,
            "count": len(matches),
            "results": [
                {
                    "name": str(r.get("C_NAME", "")),
                    "resource_id": str(r.get("C_RESOURCE_ID", "")),
                    "last_cost": round(float(r.get("C_COST", 0)), 4),
                    "date": str(r.get("C_DATE", "")),
                }
                for r in matches
            ],
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/resource-groups", tags=["api"])
async def api_resource_groups(period: str = "week", rg: str = "") -> JSONResponse:
    try:
        df = await get_cached_dataframe()
        df = _filter_rg(df, rg)
        days = _period_days(period)
        filtered = _filter_period(df, days)

        by_rg = _cost_by(filtered, "C_NAME")
        data_as_of = None
        if not filtered.empty and "C_DATE" in filtered.columns:
            data_as_of = filtered["C_DATE"].dropna().max()

        return JSONResponse({
            "period": period,
            "resource_groups": [{"name": k, "cost_usd": v} for k, v in by_rg.items()],
            "total_usd": round(sum(by_rg.values()), 4),
            "data_as_of": data_as_of,
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/resource-groups-list", tags=["api"])
async def api_resource_groups_list() -> JSONResponse:
    try:
        df = await get_cached_dataframe()
        if not df.empty and "C_NAME" in df.columns and "C_COST" in df.columns:
            by_rg = df.groupby("C_NAME", dropna=False)["C_COST"].sum().sort_values(ascending=False)
            rgs = [
                {"name": str(k), "total_cost": round(float(v), 2)}
                for k, v in by_rg.items()
                if str(k).strip()
            ]
            return JSONResponse({"resource_groups": rgs})

        # No cost export data yet — fall back to ARM resource group list
        names = await asyncio.get_event_loop().run_in_executor(None, list_resource_groups)
        rgs = [{"name": n, "total_cost": None} for n in sorted(names) if n.strip()]
        return JSONResponse({"resource_groups": rgs, "source": "arm"})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Live resources ────────────────────────────────────────────────────────────

@app.get("/api/live-resources", tags=["api"])
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
            "resources": resources,
            "count": len(resources),
            "subscription_id": AZURE_SUBSCRIPTION_ID or "not configured",
            "matched_export_usd": round(matched_export_usd, 2),
        })
    except Exception as exc:
        log.exception("Live resources endpoint failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/live-reload", tags=["api"])
async def api_live_reload() -> JSONResponse:
    async with _live_lock:
        _live_cache.clear()
    try:
        resources = await _get_live_data()
        return JSONResponse({"status": "reloaded", "count": len(resources)})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/resource-metrics", tags=["api"])
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


# ── Infrastructure mutation ───────────────────────────────────────────────────

@app.delete("/api/resource", tags=["infrastructure"])
async def api_delete_resource(resource_id: str, _: None = Depends(_require_admin)) -> StreamingResponse:
    """Delete a live Azure resource by its full ARM resource ID."""
    if not resource_id.lower().startswith("/subscriptions/"):
        raise HTTPException(status_code=400, detail="resource_id must be a full ARM resource ID")

    _parts = [p.lower() for p in resource_id.strip("/").split("/")]
    try:
        _rg = _parts[_parts.index("resourcegroups") + 1]
        if _rg in PROTECTED_RGS:
            raise HTTPException(status_code=403, detail=f"Resource group '{_rg}' is protected.")
    except (ValueError, IndexError):
        pass

    log.warning("⚠️  DELETE resource initiated: %s", resource_id)

    from live_resources import _get_resource_mgmt_client
    from azure.mgmt.resource import ResourceManagementClient as _RMC

    def _get_api_version(client: _RMC, namespace: str, rtype: str) -> str:
        FALLBACKS: dict[str, str] = {
            "microsoft.compute":              "2024-03-01",
            "microsoft.storage":              "2023-05-01",
            "microsoft.network":              "2024-01-01",
            "microsoft.sql":                  "2024-01-01",
            "microsoft.documentdb":           "2023-11-15",
            "microsoft.app":                  "2024-03-01",
            "microsoft.containerregistry":    "2023-07-01",
            "microsoft.operationalinsights":  "2022-10-01",
            "microsoft.insights":             "2022-06-15",
            "microsoft.keyvault":             "2023-07-01",
            "microsoft.web":                  "2023-12-01",
            "microsoft.cache":                "2023-08-01",
            "microsoft.dbforpostgresql":      "2024-03-01-preview",
            "microsoft.dbformysql":           "2023-12-30",
        }
        try:
            provider = client.providers.get(namespace)
            for rt in provider.resource_types or []:
                if rt.resource_type and rt.resource_type.lower() == rtype.lower():
                    stable = [v for v in (rt.api_versions or []) if "preview" not in v.lower()]
                    return stable[0] if stable else (rt.api_versions or [FALLBACKS.get(namespace.lower(), "2024-01-01")])[0]
        except Exception:
            pass
        return FALLBACKS.get(namespace.lower(), "2024-01-01")

    async def log_streamer():
        try:
            yield f"[INFO] Resource: {resource_id}\n"

            parts = resource_id.strip("/").split("/")
            try:
                prov_idx = [p.lower() for p in parts].index("providers")
                namespace = parts[prov_idx + 1]
                rtype     = parts[prov_idx + 2]
            except (ValueError, IndexError):
                yield "❌ ERROR: Cannot parse provider namespace from resource ID\n"
                return

            client = _get_resource_mgmt_client()

            yield f"[INFO] Looking up API version for {namespace}/{rtype}...\n"
            api_version = await asyncio.get_event_loop().run_in_executor(
                None, _get_api_version, client, namespace, rtype
            )
            yield f"[INFO] Using API version: {api_version}\n"
            yield "[INFO] Sending delete request to Azure Resource Manager...\n"

            poller = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.resources.begin_delete_by_id(resource_id, api_version)
            )
            yield "[INFO] Delete operation accepted. Waiting for completion...\n"

            elapsed = 0
            while not poller.done():
                await asyncio.sleep(5)
                elapsed += 5
                yield f"[{elapsed:>4}s] status: {poller.status()}\n"

            await asyncio.get_event_loop().run_in_executor(None, poller.result)
            log.warning("✅ Resource deleted: %s", resource_id)
            yield f"\n✅ Resource deleted successfully ({elapsed}s total)\n"

        except Exception as exc:
            log.exception("Resource delete failed for %s", resource_id)
            yield f"\n❌ ERROR: {exc}\n"

    return StreamingResponse(log_streamer(), media_type="text/plain")


@app.delete("/api/resource-group", tags=["infrastructure"])
async def api_delete_resource_group(resource_group_name: str, _: None = Depends(_require_admin)) -> StreamingResponse:
    """Delete all resources in an Azure resource group by deleting the group itself."""
    if not resource_group_name or not resource_group_name.strip():
        raise HTTPException(status_code=400, detail="resource_group_name is required")

    rg = resource_group_name.strip()
    if rg.lower() in PROTECTED_RGS:
        raise HTTPException(status_code=403, detail=f"Resource group '{rg}' is protected.")

    log.warning("⚠️  DELETE resource group initiated: %s", rg)

    from live_resources import _get_resource_mgmt_client

    async def log_streamer():
        try:
            yield f"[INFO] Resource group: {rg}\n"
            yield "[INFO] This will delete ALL resources inside the group.\n"

            client = _get_resource_mgmt_client()
            yield "[INFO] Sending delete request to Azure Resource Manager...\n"

            poller = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.resource_groups.begin_delete(rg)
            )
            yield "[INFO] Delete operation accepted. Waiting for completion...\n"

            elapsed = 0
            while not poller.done():
                await asyncio.sleep(5)
                elapsed += 5
                yield f"[{elapsed:>4}s] status: {poller.status()}\n"

            await asyncio.get_event_loop().run_in_executor(None, poller.result)
            log.warning("✅ Resource group deleted: %s", rg)
            yield f"\n✅ Resource group '{rg}' deleted successfully ({elapsed}s total)\n"

        except Exception as exc:
            log.exception("Resource group delete failed for %s", rg)
            yield f"\n❌ ERROR: {exc}\n"

    return StreamingResponse(log_streamer(), media_type="text/plain")


@app.delete("/api/resource-groups/all", tags=["infrastructure"])
async def api_delete_all_resource_groups(resource_groups: list[str] = Body(...), _: None = Depends(_require_admin)) -> StreamingResponse:
    """Delete multiple Azure resource groups sequentially."""
    if not resource_groups:
        raise HTTPException(status_code=400, detail="resource_groups list is empty")

    blocked = [r for r in resource_groups if r.strip().lower() in PROTECTED_RGS]
    if blocked:
        raise HTTPException(status_code=403, detail=f"Protected resource groups cannot be deleted: {blocked}")

    log.warning("⚠️  BULK DELETE resource groups initiated: %s", resource_groups)

    from live_resources import _get_resource_mgmt_client

    async def log_streamer():
        client = _get_resource_mgmt_client()
        total = len(resource_groups)
        succeeded = 0
        failed = 0

        for i, rg in enumerate(resource_groups, 1):
            rg = rg.strip()
            yield f"\n[{i}/{total}] Deleting resource group: {rg}\n"
            try:
                poller = await asyncio.get_event_loop().run_in_executor(
                    None, lambda r=rg: client.resource_groups.begin_delete(r)
                )
                elapsed = 0
                while not poller.done():
                    await asyncio.sleep(5)
                    elapsed += 5
                    yield f"  [{elapsed:>4}s] {rg}: {poller.status()}\n"

                await asyncio.get_event_loop().run_in_executor(None, poller.result)
                log.warning("✅ Resource group deleted: %s", rg)
                yield f"  ✅ {rg} deleted ({elapsed}s)\n"
                succeeded += 1
            except Exception as exc:
                log.exception("Resource group delete failed for %s", rg)
                yield f"  ❌ {rg} FAILED: {exc}\n"
                failed += 1

        yield f"\n{'✅' if failed == 0 else '⚠️'} Done — {succeeded}/{total} deleted"
        if failed:
            yield f", {failed} failed"
        yield "\n"

    return StreamingResponse(log_streamer(), media_type="text/plain")


# ── Debug / diagnostics ───────────────────────────────────────────────────────

@app.get("/api/spot-price-debug", tags=["api"])
async def api_spot_price_debug(vm_size: str, region: str) -> JSONResponse:
    from live_resources import _fetch_spot_price
    try:
        price = await asyncio.get_event_loop().run_in_executor(
            None, _fetch_spot_price, vm_size, region
        )
        if price is None:
            return JSONResponse({"vm_size": vm_size, "region": region, "price": None, "status": "not_found"})
        return JSONResponse({
            "vm_size": vm_size,
            "region": region,
            "hourly_usd": round(price, 4),
            "monthly_usd": round(price * 24 * 30, 2),
            "status": "found",
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc), "vm_size": vm_size, "region": region}, status_code=500)


@app.get("/api/forecast", tags=["api"])
async def api_forecast(rg: str = "") -> JSONResponse:
    try:
        return JSONResponse(await _build_forecast(rg))
    except Exception as exc:
        log.exception("Forecast endpoint failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/diagnostics", tags=["api"])
async def api_diagnostics() -> JSONResponse:
    now = time.monotonic()
    result: dict = {}

    # Export (blob/API) cache state
    loaded_at = _cache.get("loaded_at", 0.0)
    result["blob_cache_age_seconds"] = round(now - loaded_at) if loaded_at else None
    df = _cache.get("df")
    if df is not None and not df.empty:
        result["export_row_count"] = len(df)
        if "C_DATE" in df.columns:
            result["export_date_min"] = str(df["C_DATE"].min())
            result["export_date_max"] = str(df["C_DATE"].max())
            result["export_unique_days"] = int(df["C_DATE"].nunique())
    else:
        result["export_row_count"] = 0

    # Live inventory cache state
    live_ts = _live_cache.get("ts", 0.0)
    result["live_cache_age_seconds"] = round(now - live_ts) if live_ts else None
    inv = _live_cache.get("inv")
    result["live_resource_count"] = len(inv) if inv else None

    # Forecast calibration state (uses cached data — fast if warm)
    try:
        fc = await _build_forecast("")
        result["calibration_factor"] = fc["calibration_factor"]
        result["forecast_data_source"] = fc["data_source"]
        result["live_daily_rate_usd"] = fc["live_daily_rate_usd"]
    except Exception as exc:
        result["calibration_factor"] = None
        result["forecast_error"] = str(exc)

    # Cost source tag breakdown from live enriched resources
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


@app.get("/api/daily", tags=["api"])
async def api_daily(days: int = 30, rg: str = "") -> JSONResponse:
    try:
        df = await get_cached_dataframe()
        df = _filter_rg(df, rg)
        if "C_DATE" not in df.columns or "C_COST" not in df.columns:
            return JSONResponse({"days": days, "points": [], "total_usd": 0})

        cutoff = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
        filtered = df[df["C_DATE"] >= cutoff]
        daily = filtered.groupby("C_DATE")["C_COST"].sum().sort_index()
        points = [{"date": str(d), "cost_usd": round(float(v), 6)} for d, v in daily.items()]

        return JSONResponse({
            "days": days,
            "points": points,
            "total_usd": round(float(daily.sum()), 4),
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
