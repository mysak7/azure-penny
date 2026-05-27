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

from config import (AZURE_SUBSCRIPTION_ID, CF_ACCESS_CLIENT_ID, CF_ACCESS_CLIENT_SECRET,
                    COST_TAG_KEY, PROTECTED_RGS, STORAGE_ACCOUNT_NAME, STORAGE_CONTAINER_NAME,
                    VERTEX_PROXY_API_KEY, VERTEX_PROXY_URL, log)
from live_resources import _get_live_data, _live_cache, _live_lock, fetch_resource_metrics, list_resource_groups
from storage import _cache, _extract_all_tag_keys, _lock, get_blob_service_client, get_cached_dataframe

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).parent / "templates"

_rg_names_cache: dict = {}   # {"names": set[str], "ts": float}
_RG_NAMES_TTL = 900          # 15 min

async def _get_arm_rg_names() -> set[str]:
    """Return ARM resource group names, cached for 15 min."""
    now = time.monotonic()
    if "names" in _rg_names_cache and (now - _rg_names_cache.get("ts", 0)) < _RG_NAMES_TTL:
        return _rg_names_cache["names"]
    # Prefer live inventory cache if already populated (no extra call)
    async with _live_lock:
        inv = _live_cache.get("inv")
    if inv is not None:
        names = {r.get("resource_group", "") for r in inv if r.get("resource_group")}
    else:
        names = set(await asyncio.get_event_loop().run_in_executor(None, list_resource_groups))
    _rg_names_cache["names"] = names
    _rg_names_cache["ts"] = now
    return names

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(_: FastAPI):
    asyncio.create_task(_get_live_data())
    yield

APP_VERSION = "1.3.0"

app = FastAPI(
    title="azure-penny",
    description="Azure Cost Management dashboard — reads Cost exports from Blob Storage.",
    version=APP_VERSION,
    lifespan=lifespan,
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
# Both bare names and "Azure …" prefixed variants are listed so the mapping
# works regardless of which export format Azure Cost Management produces.
# ---------------------------------------------------------------------------

CAT_COMPUTE = {
    "Virtual Machines", "Azure Virtual Machines",
    "App Service", "Azure App Service",
    "Container Apps", "Azure Container Apps",          # ← ACA billing name
    "Azure Functions", "Functions",
    "Container Instances", "Azure Container Instances",
    "Azure Kubernetes Service", "Kubernetes Service",
    "Container Registry", "Azure Container Registry",  # ← ACR
    "Batch", "Azure Batch",
    "Cloud Services",
    "Azure Spring Apps", "Spring Apps",
    "Azure Red Hat OpenShift",
}
CAT_STORAGE = {
    "Storage", "Azure Blob Storage", "Azure Files",
    "Azure Data Lake Storage", "Azure Data Lake Storage Gen2",
    "Backup", "Azure Backup",
    "StorSimple",
    "Azure NetApp Files",
    "Managed Disks", "Azure Managed Disks",
    "Azure Queue Storage",
    "Azure Table Storage",
}
CAT_NETWORK = {
    "Virtual Network", "Azure Virtual Network",
    "Load Balancer", "Azure Load Balancer",
    "Application Gateway", "Azure Application Gateway",
    "Azure DNS", "DNS",
    "Azure Front Door", "Front Door",
    "Bandwidth",
    "Content Delivery Network", "Azure CDN",
    "VPN Gateway", "Azure VPN Gateway",
    "Azure Bastion",
    "Azure Firewall",
    "Network Watcher", "Azure Network Watcher",
    "Traffic Manager", "Azure Traffic Manager",
    "ExpressRoute", "Azure ExpressRoute",
    "NAT Gateway", "Azure NAT Gateway",
    "Private Link", "Azure Private Link",
    "Azure DDoS Protection",
    "Azure Virtual WAN",
}
CAT_DATABASE = {
    "SQL Database", "Azure SQL Database",
    "Azure Cosmos DB", "Cosmos DB",
    "Azure Cache for Redis", "Cache for Redis",
    "Azure Database for MySQL", "Database for MySQL",
    "Azure Database for PostgreSQL", "Database for PostgreSQL",
    "Azure SQL Managed Instance", "SQL Managed Instance",
    "Azure Synapse Analytics", "Synapse Analytics",
    "Azure Database for MariaDB",
    "Azure SQL",
}
CAT_MONITORING = {
    "Azure Grafana Service", "Grafana",
    "Log Analytics", "Azure Log Analytics",
    "Azure Monitor",
    "Application Insights", "Azure Application Insights",
    "Microsoft Sentinel", "Azure Sentinel",
    "Azure Advisor",
    "Microsoft Defender for Cloud", "Azure Security Center",
    "Key Vault", "Azure Key Vault",           # secrets management / security
}

# Union of all explicitly mapped services — used to identify "Other" spend.
ALL_KNOWN_SERVICES: frozenset[str] = frozenset(
    CAT_COMPUTE | CAT_STORAGE | CAT_NETWORK | CAT_DATABASE | CAT_MONITORING
)

# ---------------------------------------------------------------------------
# Filtering / aggregation helpers
# ---------------------------------------------------------------------------

_TRANSFER_KEYWORDS = frozenset([
    "bandwidth", "transfer", "egress", "geo-redundant replication",
    "data retrieval", "replication",
])


def _period_days(period: str) -> int:
    return {"day": 1, "week": 7, "month": 30, "year": 365}.get(period, 7)


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


def _filter_app(df: pd.DataFrame, app: str) -> pd.DataFrame:
    if not app or "C_APP" not in df.columns:
        return df
    return df[df["C_APP"].str.lower() == app.lower()]


def _cost_by(df: pd.DataFrame, col: str) -> dict[str, float]:
    if col not in df.columns or "C_COST" not in df.columns:
        return {}
    grp = df.groupby(col)["C_COST"].sum()
    return grp[grp > 0].sort_values(ascending=False).round(6).to_dict()


async def _category_api(period: str, services: set[str], rg: str = "", app: str = "") -> dict:
    full_df = await get_cached_dataframe()
    df = _filter_app(_filter_rg(full_df, rg), app)
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
# Page routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request, "version": APP_VERSION})


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request, "version": APP_VERSION})


@app.get("/manager", response_class=HTMLResponse, include_in_schema=False)
async def manager(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request, "version": APP_VERSION})


@app.get("/technician", response_class=HTMLResponse, include_in_schema=False)
async def technician(request: Request):
    return templates.TemplateResponse("technician.html", {"request": request, "version": APP_VERSION})


@app.get("/live", response_class=HTMLResponse, include_in_schema=False)
async def live_view(request: Request):
    is_admin = "penny-admin" in _get_user_roles(request)
    return templates.TemplateResponse("live.html", {"request": request, "is_admin": is_admin, "version": APP_VERSION})


@app.get("/guide", response_class=HTMLResponse, include_in_schema=False)
async def guide_view(request: Request):
    return templates.TemplateResponse("guide.html", {"request": request, "version": APP_VERSION})


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
        "version": APP_VERSION,
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
async def api_compute(period: str = "week", rg: str = "", app: str = "") -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_COMPUTE, rg, app))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/compute/machines", tags=["api"])
async def api_compute_machines(period: str = "week", rg: str = "", app: str = "") -> JSONResponse:
    try:
        full_df = await get_cached_dataframe()
        df = _filter_app(_filter_rg(full_df, rg), app)
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
async def api_storage(period: str = "week", rg: str = "", app: str = "") -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_STORAGE, rg, app))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/storage/breakdown", tags=["api"])
async def api_storage_breakdown(period: str = "week", rg: str = "", app: str = "") -> JSONResponse:
    try:
        full_df = await get_cached_dataframe()
        df = _filter_app(_filter_rg(full_df, rg), app)
        days = _period_days(period)

        all_storage_svcs = CAT_STORAGE | {"Bandwidth"}
        filtered = _filter_services(_filter_period(df, days), all_storage_svcs)

        fallback = False
        if period == "day" and filtered.empty and "C_DATE" in full_df.columns:
            df_svc = _filter_services(df, all_storage_svcs)  # df already has app filter applied
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
async def api_network(period: str = "week", rg: str = "", app: str = "") -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_NETWORK, rg, app))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/database", tags=["api"])
async def api_database(period: str = "week", rg: str = "", app: str = "") -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_DATABASE, rg, app))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/monitoring", tags=["api"])
async def api_monitoring(period: str = "week", rg: str = "", app: str = "") -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_MONITORING, rg, app))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def _other_category_api(period: str, rg: str = "", app: str = "") -> dict:
    """Costs for services NOT covered by any standard category (catch-all)."""
    full_df = await get_cached_dataframe()
    df = _filter_app(_filter_rg(full_df, rg), app)
    days = _period_days(period)
    filtered = _filter_period(df, days)

    fallback = False
    if period == "day" and filtered.empty and "C_DATE" in full_df.columns:
        df_rg = _filter_app(_filter_rg(full_df, rg), app)
        if not df_rg.empty:
            last_date = full_df["C_DATE"].dropna().max()
            filtered = df_rg[df_rg["C_DATE"] == last_date]
            fallback = True

    if "C_SERVICE" in filtered.columns:
        lower_known = {s.lower() for s in ALL_KNOWN_SERVICES}
        filtered = filtered[~filtered["C_SERVICE"].str.lower().isin(lower_known)]

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


@app.get("/api/other", tags=["api"])
async def api_other(period: str = "week", rg: str = "", app: str = "") -> JSONResponse:
    """Services that don't fall into any standard category."""
    try:
        return JSONResponse(await _other_category_api(period, rg, app))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Application (project tag) endpoints ──────────────────────────────────────

@app.get("/api/apps", tags=["api"])
async def api_apps(period: str = "week", rg: str = "") -> JSONResponse:
    """List distinct applications (project tags) with their total cost for the given period."""
    try:
        full_df = await get_cached_dataframe()
        df = _filter_rg(full_df, rg)
        days = _period_days(period)
        filtered = _filter_period(df, days)

        if filtered.empty or "C_APP" not in filtered.columns or "C_COST" not in filtered.columns:
            return JSONResponse({"apps": [], "period": period})

        by_app = (
            filtered.groupby("C_APP", dropna=False)["C_COST"]
            .sum()
            .sort_values(ascending=False)
        )
        apps = [
            {"name": str(k), "cost_usd": round(float(v), 4)}
            for k, v in by_app.items()
        ]
        return JSONResponse({"apps": apps, "period": period})
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

        # ── Tag diagnostics ───────────────────────────────────────────────────
        # Show C_APP distribution and (if C_TAGS present) actual tag keys in
        # the raw data — helps diagnose mismatched COST_TAG_KEY.
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

        # Collect all unique tag key names from a sample of C_TAGS rows
        tag_key_freq: dict[str, int] = {}
        if "C_TAGS" in df.columns:
            sample = df["C_TAGS"].dropna().head(500)
            for keys in sample.apply(_extract_all_tag_keys):
                for k in keys:
                    tag_key_freq[k] = tag_key_freq.get(k, 0) + 1
        # Sort by frequency
        tag_keys_ranked = sorted(tag_key_freq.items(), key=lambda x: -x[1])

        # Sample raw C_TAGS values for 5 "Untagged" rows (to inspect manually)
        untagged_tags_sample: list = []
        if "C_TAGS" in df.columns and "C_APP" in df.columns:
            untagged_rows = df[df["C_APP"] == "Untagged"]["C_TAGS"].dropna().head(5).tolist()
            untagged_tags_sample = [str(v) for v in untagged_rows]

        return JSONResponse({
            "blob_count": len(all_blobs),
            "blob_paths_sample": all_blobs[:20],
            "row_count": len(df),
            "columns": list(df.columns),
            "date_range": date_range,
            "total_cost": total_cost,
            "top_services": top_services,
            "top_resource_groups": top_rgs,
            # Tag diagnostics
            "cost_tag_key_configured": COST_TAG_KEY,
            "c_app_distribution": app_dist,
            "tag_keys_in_data": [{"key": k, "count": c} for k, c in tag_keys_ranked[:30]],
            "untagged_raw_tags_sample": untagged_tags_sample,
        })
    except Exception as exc:
        log.exception("Debug endpoint failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Technician / services ─────────────────────────────────────────────────────

@app.get("/api/services", tags=["api"])
async def api_services(period: str = "week", rg: str = "", app: str = "") -> JSONResponse:
    try:
        full_df = await get_cached_dataframe()
        df = _filter_app(_filter_rg(full_df, rg), app)
        days = _period_days(period)
        filtered = _filter_period(df, days)

        fallback = False
        if period == "day" and filtered.empty and "C_DATE" in full_df.columns and not df.empty:
            last_date = full_df["C_DATE"].dropna().max()
            filtered = df[df["C_DATE"] == last_date]  # df already has rg+app filter applied
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
async def api_resource_groups(period: str = "week", rg: str = "", app: str = "") -> JSONResponse:
    try:
        df = await get_cached_dataframe()
        df = _filter_app(_filter_rg(df, rg), app)
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
async def api_resource_groups_list(app_filter: str = "") -> JSONResponse:
    """List resource groups, optionally pre-filtered to only those that have data for a given app tag."""
    try:
        df = await get_cached_dataframe()

        # When app filter is active: return only RGs present in that app's billing data.
        # ARM-only RGs are excluded because they carry no app-tag billing rows.
        if app_filter:
            filtered = _filter_app(df, app_filter)
            if not filtered.empty and "C_NAME" in filtered.columns and "C_COST" in filtered.columns:
                by_rg = filtered.groupby("C_NAME", dropna=False)["C_COST"].sum()
                rgs = [
                    {"name": str(k), "total_cost": round(float(v), 2), "in_arm": True}
                    for k, v in by_rg.items()
                    if str(k).strip() and v > 0
                ]
                rgs.sort(key=lambda r: -(r["total_cost"] or 0))
            else:
                rgs = []
            return JSONResponse({"resource_groups": rgs})

        # No app filter — original behaviour: billing data merged with ARM list.
        arm_rg_names = await _get_arm_rg_names()

        if not df.empty and "C_NAME" in df.columns and "C_COST" in df.columns:
            by_rg = df.groupby("C_NAME", dropna=False)["C_COST"].sum()
            cost_rgs: dict[str, float] = {
                str(k): round(float(v), 2)
                for k, v in by_rg.items()
                if str(k).strip()
            }
            rgs = [{"name": n, "total_cost": c, "in_arm": n in arm_rg_names} for n, c in cost_rgs.items()]
            # Append ARM-only RGs (exist in ARM but never appeared in cost export)
            for name in arm_rg_names:
                if name not in cost_rgs:
                    rgs.append({"name": name, "total_cost": None, "in_arm": True})
            rgs.sort(key=lambda r: (r["total_cost"] is None, -(r["total_cost"] or 0)))
            return JSONResponse({"resource_groups": rgs})

        # No cost export data yet — fall back to ARM resource group list
        names = await asyncio.get_event_loop().run_in_executor(None, list_resource_groups)
        rgs = [{"name": n, "total_cost": None, "in_arm": True} for n in sorted(names) if n.strip()]
        return JSONResponse({"resource_groups": rgs, "source": "arm"})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Anomalies ────────────────────────────────────────────────────────────────

@app.get("/api/anomalies", tags=["api"])
async def api_anomalies(rg: str = "", app: str = "") -> JSONResponse:
    """Week-over-week cost anomalies and untagged resource report from billing CSV."""
    try:
        full_df = await get_cached_dataframe()
        df = _filter_app(_filter_rg(full_df, rg), app)

        today = date.today()
        curr_start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        prev_start = (today - timedelta(days=14)).strftime("%Y-%m-%d")

        has_date = "C_DATE" in df.columns
        curr_df = df[df["C_DATE"] >= curr_start] if has_date else df
        prev_df = df[(df["C_DATE"] >= prev_start) & (df["C_DATE"] < curr_start)] if has_date else pd.DataFrame(columns=df.columns)

        def _week_deltas(key_col: str) -> tuple[list[dict], dict[str, dict]]:
            if key_col not in df.columns or "C_COST" not in df.columns:
                return [], {}
            c = curr_df.groupby(key_col)["C_COST"].sum() if not curr_df.empty else pd.Series(dtype=float)
            p = prev_df.groupby(key_col)["C_COST"].sum() if not prev_df.empty else pd.Series(dtype=float)
            rows, index = [], {}
            for k in set(c.index) | set(p.index):
                cv, pv = float(c.get(k, 0)), float(p.get(k, 0))
                if cv == 0 and pv == 0:
                    continue
                delta_pct = round((cv - pv) / pv * 100, 1) if pv > 0 else None
                entry = {
                    key_col: str(k),
                    "current_week_usd": round(cv, 4),
                    "prior_week_usd": round(pv, 4),
                    "delta_pct": delta_pct,
                    "is_new": pv == 0 and cv > 0,
                    "vanished": cv == 0 and pv > 0,
                }
                rows.append(entry)
                index[str(k)] = entry
            return rows, index

        svc_rows, svc_index = _week_deltas("C_SERVICE")
        rg_rows,  rg_index  = _week_deltas("C_NAME")

        spikes    = sorted([s for s in svc_rows if s["delta_pct"] is not None and s["delta_pct"] > 50],
                           key=lambda x: x["delta_pct"], reverse=True)
        new_svcs  = sorted([s for s in svc_rows if s["is_new"] and s["current_week_usd"] > 1],
                           key=lambda x: x["current_week_usd"], reverse=True)
        gone_svcs = sorted([s for s in svc_rows if s["vanished"] and s["prior_week_usd"] > 1],
                           key=lambda x: x["prior_week_usd"], reverse=True)
        rg_spikes = sorted([r for r in rg_rows if r["delta_pct"] is not None and r["delta_pct"] > 50],
                           key=lambda x: x["delta_pct"], reverse=True)

        untagged: list[dict] = []
        untagged_cost = 0.0
        if "C_TAGS" in df.columns and "C_RESOURCE_ID" in df.columns and "C_COST" in df.columns:
            mask = (
                df["C_TAGS"].isna() |
                df["C_TAGS"].astype(str).str.strip().isin(["", "{}", "nan", "None"])
            )
            utdf = df[mask]
            if not utdf.empty:
                agg = utdf.groupby("C_RESOURCE_ID")["C_COST"].sum().sort_values(ascending=False)
                untagged = [
                    {"resource_id": str(rid), "cost_usd": round(float(c), 4)}
                    for rid, c in agg.items() if c > 0
                ][:50]
                untagged_cost = round(sum(u["cost_usd"] for u in untagged), 4)

        return JSONResponse({
            "spikes": spikes,
            "new_services": new_svcs,
            "vanished_services": gone_svcs,
            "rg_spikes": rg_spikes,
            "service_deltas": svc_index,
            "rg_deltas": rg_index,
            "untagged_cost_usd": untagged_cost,
            "untagged_resources": untagged,
            "total_untagged_resources": len(untagged),
        })
    except Exception as exc:
        log.exception("Anomalies endpoint failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Year overview ─────────────────────────────────────────────────────────────

@app.get("/api/year", tags=["api"])
async def api_year(rg: str = "", app: str = "") -> JSONResponse:
    try:
        full_df = await get_cached_dataframe()
        df = _filter_app(_filter_rg(full_df, rg), app)

        if df.empty or "C_DATE" not in df.columns or "C_COST" not in df.columns:
            return JSONResponse({"months": [], "projected_months": [], "ytd_usd": 0, "monthly_avg_usd": 0})

        today = date.today()
        cutoff = date(today.year - 1, today.month, 1).strftime("%Y-%m-%d")
        df = df[df["C_DATE"] >= cutoff].copy()

        df["_month"] = df["C_DATE"].str[:7]
        monthly = df.groupby("_month")["C_COST"].sum().sort_index()
        months = [{"month": m, "cost_usd": round(float(v), 2)} for m, v in monthly.items()]

        ytd_prefix = today.strftime("%Y")
        ytd_usd = round(float(df[df["C_DATE"].str.startswith(ytd_prefix)]["C_COST"].sum()), 2)
        monthly_avg = round(float(monthly.mean()), 2) if len(monthly) > 0 else 0.0

        daily_rate = 0.0
        try:
            all_resources = await _get_live_data()
            live_monthly = sum((r.get("monthly_cost") or 0.0) for r in all_resources if (r.get("monthly_cost") or 0.0) > 0)
            if live_monthly > 0:
                daily_rate = live_monthly / 30
        except Exception:
            pass
        if daily_rate == 0 and today.day > 0:
            month_str = today.strftime("%Y-%m")
            this_month_df = df[df["C_DATE"].str.startswith(month_str)]
            if not this_month_df.empty:
                daily_rate = float(this_month_df["C_COST"].sum()) / today.day

        projected_months: list[dict] = []
        if daily_rate > 0:
            m = today.month + 1
            y = today.year
            while m <= 12:
                days_in = calendar.monthrange(y, m)[1]
                projected_months.append({
                    "month": f"{y}-{m:02d}",
                    "cost_usd": round(daily_rate * days_in, 2),
                })
                m += 1

        return JSONResponse({
            "months":           months,
            "projected_months": projected_months,
            "ytd_usd":          ytd_usd,
            "monthly_avg_usd":  monthly_avg,
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Breakdown chart ───────────────────────────────────────────────────────────

_ORDERED_CATS = ["Compute", "Storage", "Network", "Database", "Monitoring", "Other"]
# None sentinel = "everything NOT in any of the known CAT_* sets"
_CAT_KEY_MAP: dict[str, set[str] | None] = {
    "compute":    CAT_COMPUTE,
    "storage":    CAT_STORAGE,
    "network":    CAT_NETWORK,
    "database":   CAT_DATABASE,
    "monitoring": CAT_MONITORING,
    "other":      None,
}


def _bucket_key(date_str: str, granularity: str) -> str:
    """Convert a YYYY-MM-DD date string to a time-bucket key."""
    try:
        d = date.fromisoformat(str(date_str))
    except (ValueError, TypeError):
        return str(date_str)
    if granularity == "weeks":
        return d.strftime("%G-W%V")   # ISO year-week, e.g. "2025-W21"
    if granularity == "months":
        return str(date_str)[:7]      # "2025-05"
    return str(date_str)              # days: "2025-05-19"


def _bucket_label(key: str, granularity: str) -> str:
    """Short human-readable label for a bucket key."""
    if granularity == "weeks":
        parts = key.split("-W")
        return f"W{parts[1]}" if len(parts) == 2 else key
    if granularity == "months":
        try:
            y, m = key.split("-")
            return date(int(y), int(m), 1).strftime("%b %y")
        except Exception:
            return key
    # days
    try:
        d = date.fromisoformat(key)
        return f"{d.day}.{d.month}"
    except Exception:
        return key


@app.get("/api/breakdown", tags=["api"])
async def api_breakdown(
    period: str = "week",
    granularity: str = "days",
    category: str = "all",
    rg: str = "",
    app: str = "",
) -> JSONResponse:
    """Time-bucketed stacked cost breakdown."""
    try:
        full_df = await get_cached_dataframe()
        df = _filter_app(_filter_rg(full_df, rg), app)
        days = _period_days(period)
        df = _filter_period(df, days)

        if df.empty or "C_DATE" not in df.columns or "C_COST" not in df.columns:
            return JSONResponse({"buckets": [], "stack_keys": []})

        df = df.copy()
        df["_bucket"] = df["C_DATE"].apply(lambda x: _bucket_key(str(x), granularity))

        result_buckets: dict[str, dict[str, float]] = {}
        stack_keys: list[str] = []

        lower_known = {s.lower() for s in ALL_KNOWN_SERVICES}

        def _get_cat_df(services: set[str] | None) -> pd.DataFrame:
            """Return rows for a category; None = 'Other' (not in any known set)."""
            if services is None:
                if "C_SERVICE" not in df.columns:
                    return df.iloc[0:0]
                return df[~df["C_SERVICE"].str.lower().isin(lower_known)]
            return _filter_services(df, services)

        if category.lower() == "all":
            for cat_name, cat_services in _CAT_KEY_MAP.items():
                cat_df = _get_cat_df(cat_services)
                if cat_df.empty:
                    continue
                grp = cat_df.groupby("_bucket")["C_COST"].sum()
                for bucket, cost in grp.items():
                    result_buckets.setdefault(str(bucket), {})[cat_name.capitalize()] = round(float(cost), 4)
            stack_keys = list(_ORDERED_CATS)  # always show all categories, zero for missing ones
        else:
            cat_services = _CAT_KEY_MAP.get(category.lower(), set())
            cat_df = _get_cat_df(cat_services)
            # Seed every bucket that exists in the full df so days with zero
            # category spend still appear in the chart (not silently dropped)
            for bk in df["_bucket"].astype(str).unique():
                result_buckets.setdefault(str(bk), {})
            if not cat_df.empty and "C_SERVICE" in cat_df.columns:
                grp2 = cat_df.groupby(["_bucket", "C_SERVICE"])["C_COST"].sum()
                for (bucket, svc), cost in grp2.items():
                    result_buckets.setdefault(str(bucket), {})[str(svc)] = round(float(cost), 4)
            svc_totals: dict[str, float] = {}
            for bdata in result_buckets.values():
                for svc, cost in bdata.items():
                    svc_totals[svc] = svc_totals.get(svc, 0) + cost
            stack_keys = sorted(svc_totals, key=lambda x: svc_totals[x], reverse=True)[:8]

        buckets = [
            {"key": bk, "label": _bucket_label(bk, granularity),
             **{k: result_buckets[bk].get(k, 0) for k in stack_keys}}
            for bk in sorted(result_buckets)
        ]

        return JSONResponse({"buckets": buckets, "stack_keys": stack_keys})

    except Exception as exc:
        log.exception("Breakdown endpoint failed")
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
    _rg_names_cache.clear()
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
async def api_daily(days: int = 30, rg: str = "", app: str = "") -> JSONResponse:
    try:
        df = await get_cached_dataframe()
        df = _filter_app(_filter_rg(df, rg), app)
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


# ── Monthly Forecast ─────────────────────────────────────────────────────────

@app.get("/api/forecast", tags=["api"])
async def api_forecast(rg: str = "", app: str = "") -> JSONResponse:
    """Monthly forecast: actual cumulative daily costs + two projections.

    Returns:
        actual: [{date, cumulative}] — day-by-day cumulative spend this month
        today_total: cumulative spend up to and including today
        avg_end: projected month-end total using historical daily average
        live_end: projected month-end total using live-resources monthly_cost sum
        avg_daily_rate / live_daily_rate: daily rates used for each projection
        today / end_of_month: ISO date strings
        days_elapsed / days_remaining / days_in_month
    """
    try:
        today = date.today()
        month_start = today.replace(day=1)
        last_day = calendar.monthrange(today.year, today.month)[1]
        end_of_month = today.replace(day=last_day)
        days_elapsed = (today - month_start).days + 1
        days_remaining = (end_of_month - today).days
        days_in_month = last_day

        # ── Export: daily costs this month ───────────────────────────────────
        full_df = await get_cached_dataframe()
        df = _filter_app(_filter_rg(full_df, rg), app)

        actual_points: list[dict] = []
        today_total = 0.0

        if "C_DATE" in df.columns and "C_COST" in df.columns and not df.empty:
            df = df.copy()
            df["_date"] = pd.to_datetime(df["C_DATE"]).dt.date
            month_df = df[(df["_date"] >= month_start) & (df["_date"] <= today)]
            daily = month_df.groupby("_date")["C_COST"].sum().sort_index()
            cumulative = 0.0
            for d, cost in daily.items():
                cumulative += float(cost)
                actual_points.append({"date": str(d), "cumulative": round(cumulative, 4)})
            today_total = cumulative

        avg_daily_rate = today_total / max(days_elapsed, 1)
        avg_end = round(today_total + avg_daily_rate * days_remaining, 2)

        # ── Live: sum monthly_cost, filter by rg / app ───────────────────────
        live_resources = await _get_live_data()
        if rg:
            live_resources = [
                r for r in live_resources
                if (r.get("resource_group") or "").lower() == rg.lower()
            ]
        if app:
            live_resources = [
                r for r in live_resources
                if (r.get("app") or "") == app
            ]
        live_monthly_total = sum(r.get("monthly_cost") or 0.0 for r in live_resources)
        live_daily_rate = live_monthly_total / days_in_month
        live_end = round(today_total + live_daily_rate * days_remaining, 2)

        return JSONResponse({
            "actual": actual_points,
            "today": str(today),
            "end_of_month": str(end_of_month),
            "today_total": round(today_total, 2),
            "days_elapsed": days_elapsed,
            "days_remaining": days_remaining,
            "days_in_month": days_in_month,
            "avg_daily_rate": round(avg_daily_rate, 4),
            "avg_end": avg_end,
            "live_daily_rate": round(live_daily_rate, 4),
            "live_end": live_end,
            "live_monthly_total": round(live_monthly_total, 2),
        })
    except Exception as exc:
        log.exception("Forecast endpoint failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Snapshot (LLM context chunk) ─────────────────────────────────────────────

async def _build_snapshot(period: str, rg: str, app: str) -> dict:
    """Assemble a comprehensive cost snapshot dict (data + markdown).

    Used by both GET /api/snapshot and the get_page_snapshot chat tool so
    the two always return the same information.
    """
    full_df = await get_cached_dataframe()
    df = _filter_app(_filter_rg(full_df, rg), app)
    days = _period_days(period)
    filtered = _filter_period(df, days)

    today = date.today()

    total = round(float(filtered["C_COST"].sum()), 2) if "C_COST" in filtered.columns else 0.0

    data_as_of = None
    date_range_start = None
    if not filtered.empty and "C_DATE" in filtered.columns:
        dates = filtered["C_DATE"].dropna()
        if not dates.empty:
            data_as_of = str(dates.max())
            date_range_start = str(dates.min())

    # ── By category ──────────────────────────────────────────────────────────
    lower_known = {s.lower() for s in ALL_KNOWN_SERVICES}
    categories: list[dict] = []
    for cat_name, cat_services in _CAT_KEY_MAP.items():
        if cat_services is None:
            cat_df = (
                filtered[~filtered["C_SERVICE"].str.lower().isin(lower_known)]
                if "C_SERVICE" in filtered.columns
                else filtered.iloc[0:0]
            )
        else:
            cat_df = _filter_services(filtered, cat_services)
        cat_total = round(float(cat_df["C_COST"].sum()), 2) if ("C_COST" in cat_df.columns and not cat_df.empty) else 0.0
        svc_count = int(cat_df["C_SERVICE"].nunique()) if ("C_SERVICE" in cat_df.columns and not cat_df.empty) else 0
        if cat_total > 0:
            categories.append({
                "category": cat_name.capitalize(),
                "total_usd": cat_total,
                "service_count": svc_count,
            })
    categories.sort(key=lambda x: x["total_usd"], reverse=True)

    # ── Top services / RGs / apps ─────────────────────────────────────────────
    top_services = [{"service": k, "cost_usd": v} for k, v in list(_cost_by(filtered, "C_SERVICE").items())[:10]]
    top_rgs      = [{"name":    k, "cost_usd": v} for k, v in list(_cost_by(filtered, "C_NAME").items())[:10]]

    top_apps: list[dict] = []
    if "C_APP" in filtered.columns and "C_COST" in filtered.columns:
        by_app = filtered.groupby("C_APP", dropna=False)["C_COST"].sum().sort_values(ascending=False)
        top_apps = [
            {"app": str(k), "cost_usd": round(float(v), 2)}
            for k, v in by_app.items() if v > 0
        ][:10]

    # ── Week-over-week anomalies ───────────────────────────────────────────────
    spikes, new_svcs, gone_svcs = [], [], []
    if "C_DATE" in df.columns and "C_SERVICE" in df.columns and "C_COST" in df.columns:
        curr_start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        prev_start = (today - timedelta(days=14)).strftime("%Y-%m-%d")
        curr_df = df[df["C_DATE"] >= curr_start]
        prev_df = df[(df["C_DATE"] >= prev_start) & (df["C_DATE"] < curr_start)]
        c = curr_df.groupby("C_SERVICE")["C_COST"].sum() if not curr_df.empty else pd.Series(dtype=float)
        p = prev_df.groupby("C_SERVICE")["C_COST"].sum() if not prev_df.empty else pd.Series(dtype=float)
        for svc in set(c.index) | set(p.index):
            cv, pv = float(c.get(svc, 0)), float(p.get(svc, 0))
            if cv == 0 and pv == 0:
                continue
            dp = round((cv - pv) / pv * 100, 1) if pv > 0 else None
            entry = {
                "service": str(svc),
                "current_week_usd": round(cv, 2),
                "prior_week_usd":   round(pv, 2),
                "delta_pct":        dp,
            }
            if dp is not None and dp > 50:
                spikes.append(entry)
            elif pv == 0 and cv > 1:
                new_svcs.append(entry)
            elif cv == 0 and pv > 1:
                gone_svcs.append(entry)
        spikes.sort(key=lambda x: x["delta_pct"], reverse=True)
        new_svcs.sort(key=lambda x: x["current_week_usd"], reverse=True)

    # ── Markdown ──────────────────────────────────────────────────────────────
    filter_parts = []
    if rg:  filter_parts.append(f"RG: {rg}")
    if app: filter_parts.append(f"App: {app}")
    filter_label = " · ".join(filter_parts) if filter_parts else "all resources"
    date_str = (
        f"{date_range_start} → {data_as_of}"
        if date_range_start and data_as_of
        else "n/a"
    )

    md: list[str] = [
        f"# Azure Cost Snapshot — {period} · {filter_label}",
        f"Generated: {today} | Data: {date_str}",
        "",
        "## Summary",
        f"**Total: ${total:.2f}** over {days} days",
        "",
    ]
    if categories:
        md.append("## By Category")
        for c_item in categories:
            n = c_item["service_count"]
            md.append(f"- {c_item['category']}: ${c_item['total_usd']:.2f} ({n} service{'s' if n != 1 else ''})")
        md.append("")
    if top_services:
        md.append("## Top Services")
        for i, s in enumerate(top_services, 1):
            md.append(f"{i}. {s['service']} — ${s['cost_usd']:.2f}")
        md.append("")
    if top_rgs:
        md.append("## Top Resource Groups")
        for i, r in enumerate(top_rgs[:5], 1):
            md.append(f"{i}. {r['name']} — ${r['cost_usd']:.2f}")
        md.append("")
    if len(top_apps) > 1:
        md.append("## By Application Tag")
        for i, a in enumerate(top_apps[:5], 1):
            md.append(f"{i}. {a['app']} — ${a['cost_usd']:.2f}")
        md.append("")
    if spikes or new_svcs or gone_svcs:
        md.append("## Anomalies (week-over-week)")
        for s in spikes[:3]:
            md.append(f"⚠️  {s['service']}: +{s['delta_pct']}% (${s['prior_week_usd']:.2f} → ${s['current_week_usd']:.2f})")
        for s in new_svcs[:3]:
            md.append(f"🆕 New: {s['service']} (${s['current_week_usd']:.2f})")
        for s in gone_svcs[:3]:
            md.append(f"👻 Gone: {s['service']} (was ${s['prior_week_usd']:.2f})")
        md.append("")

    return {
        "period":           period,
        "filters":          {"rg": rg, "app": app},
        "generated_at":     str(today),
        "summary":          {"total_usd": total, "date_range": [date_range_start, data_as_of]},
        "by_category":      categories,
        "top_services":     top_services,
        "top_resource_groups": top_rgs,
        "top_apps":         top_apps,
        "anomalies":        {"spikes": spikes, "new_services": new_svcs, "vanished_services": gone_svcs},
        "markdown":         "\n".join(md),
    }


@app.get("/api/snapshot", tags=["api"])
async def api_snapshot(period: str = "week", rg: str = "", app: str = "") -> JSONResponse:
    """Comprehensive cost snapshot — structured data + markdown optimised for LLM consumption.

    The markdown field is ready to paste into any external LLM (ChatGPT, Claude, etc.).
    The same data is available to the on-page AI agent via the get_page_snapshot tool.
    """
    try:
        return JSONResponse(await _build_snapshot(period, rg, app))
    except Exception as exc:
        log.exception("Snapshot endpoint failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── AI / LLM (vertex-proxy) ───────────────────────────────────────────────────

@app.post("/api/ai", tags=["api"])
async def api_ai(request: Request) -> JSONResponse:
    """Pošle dotaz na vertex-proxy (Gemini přes CF Tunnel + CF Access)."""
    if not VERTEX_PROXY_URL or not VERTEX_PROXY_API_KEY:
        raise HTTPException(status_code=503, detail="Vertex proxy not configured (VERTEX_PROXY_URL / VERTEX_PROXY_API_KEY missing)")

    body = await request.json()
    question = body.get("question", "")
    model    = body.get("model", "gemini-2.5-flash")
    if not question:
        raise HTTPException(status_code=400, detail="'question' field required")

    headers = {
        "Authorization":  f"Bearer {VERTEX_PROXY_API_KEY}",
        "Content-Type":   "application/json",
    }
    if CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET:
        headers["CF-Access-Client-Id"]     = CF_ACCESS_CLIENT_ID
        headers["CF-Access-Client-Secret"] = CF_ACCESS_CLIENT_SECRET

    payload = {
        "model":    model,
        "messages": [{"role": "user", "content": question}],
    }

    try:
        import httpx
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


# ── AI Chat — agentic loop with tool use + SSE streaming ─────────────────────

_CHAT_MODEL = "gemini-3.5-flash"

_CHAT_SYSTEM_PROMPT = (
    "You are azure-penny, an AI assistant specialized in analyzing Microsoft Azure cloud costs. "
    "You have access to tools that provide real-time data from Azure Cost Management exports. "
    "ALWAYS call the relevant tools to get current data before answering questions about costs or resources. "
    "Be concise, precise, and actionable. Format currency values in USD with 2 decimal places. "
    "When you identify cost issues or anomalies, suggest concrete optimizations. "
    "Never suggest or perform any resource deletion or infrastructure changes. "
    "Detect the language of the user's question and always respond in the same language "
    "(Czech question → Czech answer, English question → English answer)."
)

_CHAT_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_cost_summary",
            "description": (
                "Get total Azure cost and top services/resource-groups for a time period. "
                "Use this to answer questions about total spend, biggest cost drivers, or overview."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["day", "week", "month", "year"],
                        "description": "Time period for cost analysis (default: week)",
                    },
                    "rg": {"type": "string", "description": "Filter by resource group name (optional)"},
                    "app": {"type": "string", "description": "Filter by app/project tag (optional)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_anomalies",
            "description": (
                "Get week-over-week cost anomalies: spikes (>50% increase), new services, "
                "disappeared services, and untagged resources with their costs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rg": {"type": "string", "description": "Filter by resource group (optional)"},
                    "app": {"type": "string", "description": "Filter by app/project tag (optional)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_resource_groups",
            "description": "Get Azure costs broken down by resource group for a given time period.",
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["day", "week", "month", "year"],
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_breakdown",
            "description": (
                "Get time-series cost breakdown with daily/weekly/monthly buckets. "
                "Use for trend analysis, forecasting questions, or category-level drill-down."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {"type": "string", "enum": ["day", "week", "month", "year"]},
                    "category": {
                        "type": "string",
                        "enum": ["all", "compute", "storage", "network", "database", "monitoring", "other"],
                    },
                    "granularity": {"type": "string", "enum": ["days", "weeks", "months"]},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_live_resources",
            "description": (
                "Get current live Azure resources with their monthly cost estimates. "
                "Use for questions about running resources, spot price savings, or resource inventory."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_daily_costs",
            "description": "Get daily cost time series for the past N days. Use for trend or day-by-day analysis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Number of past days (default 30)"},
                    "rg": {"type": "string"},
                    "app": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_page_snapshot",
            "description": (
                "Get a comprehensive all-in-one cost snapshot: total spend, breakdown by category, "
                "top services, top resource groups, application-tag breakdown, and week-over-week "
                "anomalies — all in a single call. "
                "Use when the user asks for a general overview, wants to understand the full picture, "
                "or asks broad questions like 'what's going on with my costs' or 'give me a summary'. "
                "More complete than get_cost_summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["day", "week", "month", "year"],
                        "description": "Time period (default: week)",
                    },
                    "rg":  {"type": "string", "description": "Filter by resource group (optional)"},
                    "app": {"type": "string", "description": "Filter by app/project tag (optional)"},
                },
            },
        },
    },
]

_TOOL_LABELS: dict[str, str] = {
    "get_cost_summary":    "přehled nákladů",
    "get_anomalies":       "anomálie",
    "get_resource_groups": "resource groups",
    "get_breakdown":       "detail kategorií",
    "get_live_resources":  "živé zdroje",
    "get_daily_costs":     "denní data",
    "get_page_snapshot":   "přehled stránky",
}


async def _execute_chat_tool(name: str, args: dict) -> str:
    """Execute a read-only chat tool and return a JSON string result."""
    try:
        if name == "get_cost_summary":
            period = args.get("period", "week")
            rg = args.get("rg", "")
            app = args.get("app", "")
            full_df = await get_cached_dataframe()
            df = _filter_app(_filter_rg(full_df, rg), app)
            filtered = _filter_period(df, _period_days(period))
            total = round(float(filtered["C_COST"].sum()), 2) if "C_COST" in filtered.columns else 0
            top_svcs = _cost_by(filtered, "C_SERVICE")
            top_rgs  = _cost_by(filtered, "C_NAME")
            data_as_of = None
            if not filtered.empty and "C_DATE" in filtered.columns:
                data_as_of = str(filtered["C_DATE"].dropna().max())
            return json.dumps({
                "period": period, "total_usd": total,
                "top_services":        [{"service": k, "cost_usd": v} for k, v in list(top_svcs.items())[:10]],
                "top_resource_groups": [{"name": k,    "cost_usd": v} for k, v in list(top_rgs.items())[:10]],
                "data_as_of": data_as_of,
                "filters": {"rg": rg, "app": app},
            })

        if name == "get_anomalies":
            rg = args.get("rg", "")
            app = args.get("app", "")
            full_df = await get_cached_dataframe()
            df = _filter_app(_filter_rg(full_df, rg), app)
            today = date.today()
            curr_start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
            prev_start = (today - timedelta(days=14)).strftime("%Y-%m-%d")
            has_date = "C_DATE" in df.columns
            curr_df = df[df["C_DATE"] >= curr_start] if has_date else df
            prev_df = (
                df[(df["C_DATE"] >= prev_start) & (df["C_DATE"] < curr_start)]
                if has_date else pd.DataFrame(columns=df.columns)
            )

            def _deltas(col: str) -> list[dict]:
                if col not in df.columns or "C_COST" not in df.columns:
                    return []
                c = curr_df.groupby(col)["C_COST"].sum() if not curr_df.empty else pd.Series(dtype=float)
                p = prev_df.groupby(col)["C_COST"].sum() if not prev_df.empty else pd.Series(dtype=float)
                rows = []
                for k in set(c.index) | set(p.index):
                    cv, pv = float(c.get(k, 0)), float(p.get(k, 0))
                    if cv == 0 and pv == 0:
                        continue
                    dp = round((cv - pv) / pv * 100, 1) if pv > 0 else None
                    rows.append({
                        col: str(k), "current_week_usd": round(cv, 2),
                        "prior_week_usd": round(pv, 2), "delta_pct": dp,
                        "is_new": pv == 0 and cv > 0, "vanished": cv == 0 and pv > 0,
                    })
                return rows

            svc_rows = _deltas("C_SERVICE")
            untagged_cost, untagged_count = 0.0, 0
            if "C_TAGS" in df.columns and "C_COST" in df.columns:
                mask = df["C_TAGS"].isna() | df["C_TAGS"].astype(str).str.strip().isin(["", "{}", "nan", "None"])
                utdf = df[mask]
                untagged_cost = round(float(utdf["C_COST"].sum()), 2)
                untagged_count = int(utdf["C_RESOURCE_ID"].nunique()) if "C_RESOURCE_ID" in utdf.columns else len(utdf)
            return json.dumps({
                "spikes":           sorted([s for s in svc_rows if (s.get("delta_pct") or 0) > 50],  key=lambda x: x["delta_pct"], reverse=True)[:5],
                "new_services":     sorted([s for s in svc_rows if s["is_new"] and s["current_week_usd"] > 1], key=lambda x: x["current_week_usd"], reverse=True)[:5],
                "vanished_services": sorted([s for s in svc_rows if s["vanished"] and s["prior_week_usd"] > 1], key=lambda x: x["prior_week_usd"], reverse=True)[:5],
                "untagged_cost_usd": untagged_cost, "untagged_resource_count": untagged_count,
            })

        if name == "get_resource_groups":
            period = args.get("period", "week")
            full_df = await get_cached_dataframe()
            filtered = _filter_period(full_df, _period_days(period))
            by_rg = _cost_by(filtered, "C_NAME")
            return json.dumps({
                "period": period,
                "resource_groups": [{"name": k, "cost_usd": v} for k, v in list(by_rg.items())[:20]],
                "total_usd": round(sum(by_rg.values()), 2),
            })

        if name == "get_breakdown":
            period      = args.get("period", "week")
            granularity = args.get("granularity", "days")
            full_df = await get_cached_dataframe()
            df = _filter_period(full_df, _period_days(period))
            if df.empty or "C_DATE" not in df.columns or "C_COST" not in df.columns:
                return json.dumps({"buckets": [], "period": period})
            df = df.copy()
            df["_bucket"] = df["C_DATE"].apply(lambda x: _bucket_key(str(x), granularity))
            grp = df.groupby("_bucket")["C_COST"].sum().sort_index()
            return json.dumps({
                "period": period, "granularity": granularity,
                "buckets": [{"period": k, "cost_usd": round(float(v), 2)} for k, v in grp.items()][:30],
                "total_usd": round(float(grp.sum()), 2),
            })

        if name == "get_live_resources":
            resources = await _get_live_data()
            with_cost = sorted(
                [r for r in resources if (r.get("monthly_cost") or 0) > 0],
                key=lambda r: r.get("monthly_cost") or 0, reverse=True,
            )
            total = sum(r.get("monthly_cost") or 0 for r in with_cost)
            return json.dumps({
                "count": len(resources), "with_cost_count": len(with_cost),
                "total_monthly_usd": round(total, 2),
                "top_resources": [
                    {
                        "name": r.get("name", ""), "type": r.get("type", ""),
                        "resource_group": r.get("resource_group", ""),
                        "monthly_cost_usd": round(r.get("monthly_cost") or 0, 2),
                        "location": r.get("location", ""),
                    }
                    for r in with_cost[:20]
                ],
            })

        if name == "get_daily_costs":
            days_n = int(args.get("days", 30))
            rg  = args.get("rg", "")
            app = args.get("app", "")
            df = await get_cached_dataframe()
            df = _filter_app(_filter_rg(df, rg), app)
            if "C_DATE" not in df.columns or "C_COST" not in df.columns:
                return json.dumps({"points": [], "total_usd": 0})
            cutoff = (date.today() - timedelta(days=days_n)).strftime("%Y-%m-%d")
            filtered = df[df["C_DATE"] >= cutoff]
            daily = filtered.groupby("C_DATE")["C_COST"].sum().sort_index()
            points = [{"date": str(d), "cost_usd": round(float(v), 2)} for d, v in daily.items()]
            total = round(float(daily.sum()), 2)
            return json.dumps({
                "days": days_n, "points": points, "total_usd": total,
                "avg_daily_usd": round(total / max(len(points), 1), 2),
            })

        if name == "get_page_snapshot":
            period = args.get("period", "week")
            rg     = args.get("rg", "")
            app    = args.get("app", "")
            snap   = await _build_snapshot(period, rg, app)
            # Return the structured fields (not markdown) so the LLM reasons on data, not prose
            return json.dumps({k: snap[k] for k in snap if k != "markdown"})

        return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as exc:
        log.exception("Chat tool '%s' failed", name)
        return json.dumps({"error": str(exc)})


def _chat_headers() -> dict[str, str]:
    h = {"Authorization": f"Bearer {VERTEX_PROXY_API_KEY}", "Content-Type": "application/json"}
    if CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET:
        h["CF-Access-Client-Id"]     = CF_ACCESS_CLIENT_ID
        h["CF-Access-Client-Secret"] = CF_ACCESS_CLIENT_SECRET
    return h


@app.post("/api/ai/chat", tags=["api"])
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
    history  = (body.get("history") or [])[-20:]   # last 10 turns (user+assistant pairs)

    if not question:
        async def _empty():
            yield _sse({"type": "error", "text": "Empty question"})
        return StreamingResponse(_empty(), media_type="text/event-stream")

    import httpx as _httpx

    async def event_stream():
        messages: list[dict] = [{"role": "system", "content": _CHAT_SYSTEM_PROMPT}]
        messages += list(history)
        messages.append({"role": "user", "content": question})

        final_text = ""

        try:
            async with _httpx.AsyncClient(timeout=60) as client:
                for _iteration in range(5):
                    resp = await client.post(
                        f"{VERTEX_PROXY_URL}/v1/chat/completions",
                        headers=_chat_headers(),
                        json={
                            "model":       _CHAT_MODEL,
                            "messages":    messages,
                            "tools":       _CHAT_TOOLS,
                            "tool_choice": "auto",
                        },
                    )
                    resp.raise_for_status()
                    data   = resp.json()
                    msg    = data["choices"][0]["message"]
                    tc_list = msg.get("tool_calls") or []

                    if not tc_list:
                        final_text = msg.get("content") or ""
                        break

                    # Append assistant message with tool calls
                    messages.append({
                        "role": "assistant",
                        "content": msg.get("content"),
                        "tool_calls": tc_list,
                    })

                    # Execute each tool
                    for tc in tc_list:
                        tool_name = tc["function"]["name"]
                        label = _TOOL_LABELS.get(tool_name, tool_name)
                        yield _sse({"type": "tool", "name": tool_name, "label": label})
                        await asyncio.sleep(0)   # yield event loop

                        try:
                            tool_args = json.loads(tc["function"].get("arguments", "{}"))
                        except json.JSONDecodeError:
                            tool_args = {}

                        result = await _execute_chat_tool(tool_name, tool_args)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result,
                        })

                # If loop exhausted without a text answer, make one final no-tools call
                if not final_text:
                    resp = await client.post(
                        f"{VERTEX_PROXY_URL}/v1/chat/completions",
                        headers=_chat_headers(),
                        json={"model": _CHAT_MODEL, "messages": messages},
                    )
                    resp.raise_for_status()
                    final_text = resp.json()["choices"][0]["message"].get("content") or ""

        except Exception as exc:
            log.exception("AI chat agentic loop failed")
            yield _sse({"type": "error", "text": str(exc)})
            return

        # Stream the final answer word-by-word (typewriter effect)
        words = final_text.split(" ")
        for i, word in enumerate(words):
            chunk = word + (" " if i < len(words) - 1 else "")
            yield _sse({"type": "token", "text": chunk})
            await asyncio.sleep(0.022)

        yield _sse({"type": "done"})

    return StreamingResponse(event_stream(), media_type="text/event-stream")
