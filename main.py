"""
azure-penny — Azure Cost Management dashboard
FastAPI application that reads Cost Management Parquet exports from Azure Blob
Storage and exposes aggregated cost data via a REST API.

Expected blob path structure written by Azure Cost Management scheduled exports:
    {export-name}/{YYYYMMDD-YYYYMMDD}/{guid}/{filename}.parquet
    {export-name}/{YYYYMMDD-YYYYMMDD}/{guid}/{filename}.csv   (legacy)

The app discovers the most-recent date folder, reads every Parquet (or
CSV/CSV.GZ) file it finds there, and merges them into a single DataFrame.
Results are cached in memory for TTL_SECONDS (default 3 600 s / 1 hour).
"""

import asyncio
import io
import logging
import os
import re
import time
from typing import Any

import pandas as pd
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()  # harmless in production; picks up .env in local dev

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("azure-penny")

# ---------------------------------------------------------------------------
# Configuration (environment variables)
# ---------------------------------------------------------------------------

STORAGE_ACCOUNT_NAME: str = os.environ.get("STORAGE_ACCOUNT_NAME", "")
STORAGE_CONTAINER_NAME: str = os.environ.get("STORAGE_CONTAINER_NAME", "cost-exports")
AZURE_CLIENT_ID: str | None = os.environ.get("AZURE_CLIENT_ID")  # optional
TTL_SECONDS: int = int(os.environ.get("CACHE_TTL_SECONDS", "3600"))

# ---------------------------------------------------------------------------
# Azure Blob Storage client (constructed lazily; reused across requests)
# ---------------------------------------------------------------------------

_blob_client: BlobServiceClient | None = None


def get_blob_service_client() -> BlobServiceClient:
    """Return a cached BlobServiceClient authenticated via DefaultAzureCredential.

    DefaultAzureCredential works transparently in:
    - Azure Container Apps (user-assigned managed identity via AZURE_CLIENT_ID)
    - Local dev (Azure CLI / VS Code credential)
    - CI/CD (service principal via env vars)
    """
    global _blob_client
    if _blob_client is None:
        if not STORAGE_ACCOUNT_NAME:
            raise RuntimeError(
                "STORAGE_ACCOUNT_NAME environment variable is not set."
            )
        credential = DefaultAzureCredential(
            managed_identity_client_id=AZURE_CLIENT_ID or None
        )
        account_url = f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net"
        _blob_client = BlobServiceClient(
            account_url=account_url, credential=credential
        )
        log.info("BlobServiceClient initialised for account: %s", STORAGE_ACCOUNT_NAME)
    return _blob_client


# ---------------------------------------------------------------------------
# Column mapping  (Azure Cost Management → internal names)
# ---------------------------------------------------------------------------

# Azure exports different column names depending on the export type and
# agreement (EA, MCA, CSP).  The mapping below covers the most common columns.
# Extend as needed for your specific agreement type.
COLUMN_MAP: dict[str, str] = {
    # Cost
    "CostInBillingCurrency": "C_COST",
    "Cost":                  "C_COST",        # fallback in some export types
    "PreTaxCost":            "C_COST",        # EA legacy
    # Service / meter
    "MeterCategory":         "C_SERVICE",
    "ServiceName":           "C_SERVICE",     # MCA alternative
    # Resource group
    "ResourceGroup":         "C_NAME",
    "ResourceGroupName":     "C_NAME",
    # Subscription
    "SubscriptionName":      "C_ACCOUNT",
    "SubscriptionId":        "C_ACCOUNT",     # fallback
    # Date
    "Date":                  "C_DATE",
    "UsageDate":             "C_DATE",
    # Tags
    "Tags":                  "C_TAGS",
    "tag_":                  "C_TAGS",        # prefix match handled in code
}

REQUIRED_INTERNAL_COLS = {"C_COST", "C_SERVICE", "C_NAME", "C_ACCOUNT", "C_DATE"}

# ---------------------------------------------------------------------------
# Blob discovery helpers
# ---------------------------------------------------------------------------

# Azure Cost Management folder date pattern:  20240101-20240131
_DATE_FOLDER_RE = re.compile(r"^(\d{8})-(\d{8})$")


def _discover_latest_blobs(container_name: str) -> list[str]:
    """Walk the container and return blob names in the most-recent date folder.

    Azure Cost Management export paths look like:
        <export-name>/<YYYYMMDD-YYYYMMDD>/<guid>/<file>.parquet

    We collect every unique YYYYMMDD-YYYYMMDD segment across all blobs,
    choose the lexicographically largest (most-recent), then return all blob
    names whose path contains that segment.
    """
    client = get_blob_service_client()
    container_client = client.get_container_client(container_name)

    all_blobs: list[str] = [b.name for b in container_client.list_blobs()]
    if not all_blobs:
        return []

    log.info("Found %d blob(s) in container '%s'", len(all_blobs), container_name)

    # Find all date segments present in blob paths
    date_segments: set[str] = set()
    for name in all_blobs:
        for part in name.split("/"):
            if _DATE_FOLDER_RE.match(part):
                date_segments.add(part)

    if not date_segments:
        log.warning(
            "No date-folder segments (YYYYMMDD-YYYYMMDD) found in blob paths. "
            "Falling back to reading all .parquet/.csv files."
        )
        return [
            n for n in all_blobs
            if n.endswith(".parquet") or n.endswith(".csv") or n.endswith(".csv.gz")
        ]

    latest_segment = sorted(date_segments)[-1]
    log.info("Latest export date folder: %s", latest_segment)

    selected = [
        n for n in all_blobs
        if latest_segment in n
        and (
            n.endswith(".parquet")
            or n.endswith(".csv")
            or n.endswith(".csv.gz")
        )
    ]
    log.info("Selected %d file(s) from folder '%s'", len(selected), latest_segment)
    return selected


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _read_blob_to_bytes(container_name: str, blob_name: str) -> bytes:
    client = get_blob_service_client()
    blob_client = client.get_blob_client(container=container_name, blob=blob_name)
    stream = blob_client.download_blob()
    return stream.readall()


def _blob_to_dataframe(raw: bytes, blob_name: str) -> pd.DataFrame:
    """Parse raw bytes into a DataFrame, supporting Parquet and CSV formats."""
    buf = io.BytesIO(raw)
    if blob_name.endswith(".parquet"):
        return pd.read_parquet(buf, engine="pyarrow")
    if blob_name.endswith(".csv.gz"):
        return pd.read_csv(buf, compression="gzip")
    # plain .csv
    return pd.read_csv(buf)


def _apply_column_map(df: pd.DataFrame) -> pd.DataFrame:
    """Rename raw Azure Cost Management columns to internal C_* names."""
    rename: dict[str, str] = {}
    for raw_col in df.columns:
        if raw_col in COLUMN_MAP:
            internal = COLUMN_MAP[raw_col]
            # Only add to rename dict if we haven't already mapped this target
            if internal not in rename.values():
                rename[raw_col] = internal
        # Handle tag_ prefix (Azure sometimes flattens tags as tag_<key> columns)
        elif raw_col.lower().startswith("tag_") and "C_TAGS" not in rename.values():
            rename[raw_col] = "C_TAGS"

    df = df.rename(columns=rename)

    # Ensure C_COST is numeric
    if "C_COST" in df.columns:
        df["C_COST"] = pd.to_numeric(df["C_COST"], errors="coerce").fillna(0.0)

    # Normalise C_DATE to string (YYYY-MM-DD)
    if "C_DATE" in df.columns:
        df["C_DATE"] = pd.to_datetime(df["C_DATE"], errors="coerce").dt.strftime(
            "%Y-%m-%d"
        )

    return df


def _load_dataframe() -> pd.DataFrame:
    """Download and merge all Parquet/CSV files from the latest export folder."""
    blob_names = _discover_latest_blobs(STORAGE_CONTAINER_NAME)
    if not blob_names:
        raise ValueError(
            f"No cost export files found in container '{STORAGE_CONTAINER_NAME}'."
        )

    frames: list[pd.DataFrame] = []
    for name in blob_names:
        log.info("Loading blob: %s", name)
        raw = _read_blob_to_bytes(STORAGE_CONTAINER_NAME, name)
        df = _blob_to_dataframe(raw, name)
        frames.append(df)

    merged = pd.concat(frames, ignore_index=True)
    merged = _apply_column_map(merged)

    missing = REQUIRED_INTERNAL_COLS - set(merged.columns)
    if missing:
        log.warning(
            "The following expected columns were not found after mapping: %s. "
            "Check COLUMN_MAP against your export schema.",
            missing,
        )

    log.info(
        "DataFrame loaded: %d rows, %d columns", len(merged), len(merged.columns)
    )
    return merged


# ---------------------------------------------------------------------------
# In-memory cache with async lock
# ---------------------------------------------------------------------------

_lock: asyncio.Lock = asyncio.Lock()
_cache: dict[str, Any] = {}  # keys: "df", "loaded_at"


async def get_cached_dataframe() -> pd.DataFrame:
    """Return the cached DataFrame, refreshing if expired or absent."""
    async with _lock:
        now = time.monotonic()
        loaded_at: float = _cache.get("loaded_at", 0.0)

        if "df" not in _cache or (now - loaded_at) > TTL_SECONDS:
            log.info("Cache miss — loading data from Azure Blob Storage …")
            df = await asyncio.get_event_loop().run_in_executor(None, _load_dataframe)
            _cache["df"] = df
            _cache["loaded_at"] = now
            log.info("Cache refreshed at %.0f", now)

        return _cache["df"]


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="azure-penny",
    description="Azure Cost Management dashboard — reads Cost exports from Blob Storage.",
    version="1.0.0",
)


@app.get("/", tags=["health"])
async def health_check() -> JSONResponse:
    """Health / readiness probe."""
    return JSONResponse(
        {
            "status": "ok",
            "service": "azure-penny",
            "storage_account": STORAGE_ACCOUNT_NAME or "not configured",
            "container": STORAGE_CONTAINER_NAME,
        }
    )


@app.get("/costs", tags=["costs"])
async def get_costs() -> JSONResponse:
    """Return aggregated cost data grouped by service, resource group, and date."""
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
    """Return total cost, top-5 services, and top-5 resource groups."""
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

    return JSONResponse(
        {
            "total_cost": total,
            "top_services": top5("C_SERVICE"),
            "top_resource_groups": top5("C_NAME"),
        }
    )


@app.get("/costs/refresh", tags=["costs"])
async def refresh_cache() -> JSONResponse:
    """Clear the in-memory cache and reload from Azure Blob Storage immediately."""
    async with _lock:
        _cache.clear()
        log.info("Cache cleared via /costs/refresh")

    try:
        df = await get_cached_dataframe()
    except Exception as exc:
        log.exception("Failed to reload cost data after cache clear")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse(
        {
            "status": "refreshed",
            "rows_loaded": len(df),
            "columns": list(df.columns),
        }
    )
