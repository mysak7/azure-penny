"""
azure-penny — Azure Cost Management dashboard
FastAPI application that reads Cost Management Parquet exports from Azure Blob
Storage and exposes aggregated cost data via a REST API.
"""

import asyncio
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from config import AZURE_SUBSCRIPTION_ID, STORAGE_ACCOUNT_NAME, STORAGE_CONTAINER_NAME, log
from live_resources import _get_live_data, _live_cache, _live_lock
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
    return templates.TemplateResponse("live.html", {"request": request})


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["health"])
async def health_check() -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "service": "azure-penny",
        "storage_account": STORAGE_ACCOUNT_NAME or "not configured",
        "container": STORAGE_CONTAINER_NAME,
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
        if df.empty or "C_NAME" not in df.columns or "C_COST" not in df.columns:
            return JSONResponse({"resource_groups": []})
        by_rg = df.groupby("C_NAME", dropna=False)["C_COST"].sum().sort_values(ascending=False)
        rgs = [
            {"name": str(k), "total_cost": round(float(v), 2)}
            for k, v in by_rg.items()
            if v > 0 and str(k).strip()
        ]
        return JSONResponse({"resource_groups": rgs})
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


# ── Infrastructure mutation ───────────────────────────────────────────────────

@app.delete("/api/resource", tags=["infrastructure"])
async def api_delete_resource(resource_id: str) -> StreamingResponse:
    """Delete a live Azure resource by its full ARM resource ID."""
    if not resource_id.lower().startswith("/subscriptions/"):
        raise HTTPException(status_code=400, detail="resource_id must be a full ARM resource ID")

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
async def api_delete_resource_group(resource_group_name: str) -> StreamingResponse:
    """Delete all resources in an Azure resource group by deleting the group itself."""
    if not resource_group_name or not resource_group_name.strip():
        raise HTTPException(status_code=400, detail="resource_group_name is required")

    rg = resource_group_name.strip()
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
async def api_delete_all_resource_groups(resource_groups: list[str] = Body(...)) -> StreamingResponse:
    """Delete multiple Azure resource groups sequentially."""
    if not resource_groups:
        raise HTTPException(status_code=400, detail="resource_groups list is empty")

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
