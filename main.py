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
import json as _json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from azure.identity import DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.resource import ResourceManagementClient
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

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
AZURE_SUBSCRIPTION_ID: str = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
TTL_SECONDS: int = int(os.environ.get("CACHE_TTL_SECONDS", "3600"))
LIVE_CACHE_TTL: int = int(os.environ.get("LIVE_CACHE_TTL_SECONDS", "900"))  # 15 min

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
    # Resource identity (used by Live Resources tab for cost correlation)
    "ResourceId":            "C_RESOURCE_ID",
    "InstanceId":            "C_RESOURCE_ID",
}

REQUIRED_INTERNAL_COLS = {"C_COST", "C_SERVICE", "C_NAME", "C_ACCOUNT", "C_DATE"}

# ---------------------------------------------------------------------------
# Blob discovery helpers
# ---------------------------------------------------------------------------

# Azure Cost Management folder date pattern:  20240101-20240131
_DATE_FOLDER_RE = re.compile(r"^(\d{8})-(\d{8})$")


def _discover_latest_blobs(container_name: str) -> list[str]:
    """Walk the container and return only the most-recently modified export file(s).

    Azure Cost Management export paths look like:
        <export-name>/<YYYYMMDD-YYYYMMDD>/<guid>/<file>.parquet

    Azure daily exports are CUMULATIVE — each new run in the same date-range
    folder contains all data from the start of the billing period up to that
    day.  Reading multiple files from the same folder would double- (or N-times-)
    count costs.

    Strategy:
    1. Find the lexicographically latest YYYYMMDD-YYYYMMDD folder.
    2. Among all data files in that folder, keep only the one with the latest
       last_modified timestamp (i.e. the most recent export run).
    """
    client = get_blob_service_client()
    container_client = client.get_container_client(container_name)

    # Collect blobs with their last_modified so we can pick the newest run
    all_blob_props = list(container_client.list_blobs())
    if not all_blob_props:
        return []

    log.info("Found %d blob(s) in container '%s'", len(all_blob_props), container_name)

    # Find all date segments present in blob paths
    date_segments: set[str] = set()
    for bp in all_blob_props:
        for part in bp.name.split("/"):
            if _DATE_FOLDER_RE.match(part):
                date_segments.add(part)

    _EXPORT_EXTS = (".parquet", ".csv", ".csv.gz")

    if not date_segments:
        log.warning(
            "No date-folder segments (YYYYMMDD-YYYYMMDD) found in blob paths. "
            "Falling back to reading only the newest .parquet/.csv file."
        )
        candidates = [bp for bp in all_blob_props if bp.name.endswith(_EXPORT_EXTS)]
        if not candidates:
            return []
        newest = max(candidates, key=lambda bp: bp.last_modified or 0)
        log.info("Fallback: selected newest blob %s", newest.name)
        return [newest.name]

    latest_segment = sorted(date_segments)[-1]
    log.info("Latest export date folder: %s", latest_segment)

    candidates = [
        bp for bp in all_blob_props
        if latest_segment in bp.name and bp.name.endswith(_EXPORT_EXTS)
    ]
    if not candidates:
        return []

    if len(candidates) == 1:
        log.info("Selected 1 file from folder '%s': %s", latest_segment, candidates[0].name)
        return [candidates[0].name]

    # Multiple files in the same date folder → pick only the most recent export
    # run to avoid cumulative double-counting.
    newest = max(candidates, key=lambda bp: bp.last_modified or 0)
    log.info(
        "Found %d file(s) in folder '%s'; using only the most-recent: %s (last_modified=%s)",
        len(candidates), latest_segment, newest.name, newest.last_modified,
    )
    return [newest.name]


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
    # Build a case-insensitive lookup: lowercase(source) -> internal name
    lower_map = {k.lower(): v for k, v in COLUMN_MAP.items()}

    rename: dict[str, str] = {}
    for raw_col in df.columns:
        internal = lower_map.get(raw_col.lower())
        if internal and internal not in rename.values():
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
    """Return the cached DataFrame, refreshing if expired or absent.

    Returns an empty DataFrame (with expected columns) when no export files
    exist yet — this is normal for a new setup before the first Cost Management
    export runs.
    """
    async with _lock:
        now = time.monotonic()
        loaded_at: float = _cache.get("loaded_at", 0.0)

        if "df" not in _cache or (now - loaded_at) > TTL_SECONDS:
            log.info("Cache miss — loading data from Azure Blob Storage …")
            try:
                df = await asyncio.get_event_loop().run_in_executor(None, _load_dataframe)
            except ValueError as exc:
                # No export files yet — return empty DataFrame so the dashboard
                # renders with zero costs rather than a hard 500 error.
                log.warning("%s — returning empty DataFrame.", exc)
                df = pd.DataFrame(columns=list(REQUIRED_INTERNAL_COLS))
            _cache["df"] = df
            _cache["loaded_at"] = now
            log.info("Cache refreshed at %.0f", now)

        return _cache["df"]


# ---------------------------------------------------------------------------
# Azure Resource Management clients (Live Resources tab)
# ---------------------------------------------------------------------------

_resource_mgmt_client: ResourceManagementClient | None = None
_compute_mgmt_client: ComputeManagementClient | None = None


def _get_resource_mgmt_client() -> ResourceManagementClient:
    global _resource_mgmt_client
    if _resource_mgmt_client is None:
        if not AZURE_SUBSCRIPTION_ID:
            raise RuntimeError("AZURE_SUBSCRIPTION_ID environment variable is not set.")
        credential = DefaultAzureCredential(managed_identity_client_id=AZURE_CLIENT_ID or None)
        _resource_mgmt_client = ResourceManagementClient(credential, AZURE_SUBSCRIPTION_ID)
    return _resource_mgmt_client


def _get_compute_mgmt_client() -> ComputeManagementClient:
    global _compute_mgmt_client
    if _compute_mgmt_client is None:
        if not AZURE_SUBSCRIPTION_ID:
            raise RuntimeError("AZURE_SUBSCRIPTION_ID environment variable is not set.")
        credential = DefaultAzureCredential(managed_identity_client_id=AZURE_CLIENT_ID or None)
        _compute_mgmt_client = ComputeManagementClient(credential, AZURE_SUBSCRIPTION_ID)
    return _compute_mgmt_client


# Resource type (lowercase ARM type string) → UI category
_RTYPE_CATEGORY: dict[str, str] = {
    "microsoft.compute/virtualmachines":            "vm",
    "microsoft.compute/virtualmachinescalesets":    "vm",
    "microsoft.compute/disks":                      "storage",
    "microsoft.compute/snapshots":                  "storage",
    "microsoft.storage/storageaccounts":            "storage",
    "microsoft.netapp/netappaccounts":              "storage",
    "microsoft.network/virtualnetworks":            "network",
    "microsoft.network/publicipaddresses":          "network",
    "microsoft.network/loadbalancers":              "network",
    "microsoft.network/applicationgateways":        "network",
    "microsoft.network/bastionhosts":               "network",
    "microsoft.network/azurefirewalls":             "network",
    "microsoft.network/vpngateways":                "network",
    "microsoft.network/dnszones":                   "network",
    "microsoft.network/privatednszones":            "network",
    "microsoft.network/trafficmanagerprofiles":     "network",
    "microsoft.network/frontdoors":                 "network",
    "microsoft.sql/servers":                        "database",
    "microsoft.sql/managedinstances":               "database",
    "microsoft.documentdb/databaseaccounts":        "database",
    "microsoft.cache/redis":                        "database",
    "microsoft.dbformysql/servers":                 "database",
    "microsoft.dbformysql/flexibleservers":         "database",
    "microsoft.dbforpostgresql/servers":            "database",
    "microsoft.dbforpostgresql/flexibleservers":    "database",
    "microsoft.synapse/workspaces":                 "database",
    "microsoft.containerregistry/registries":       "container",
    "microsoft.app/containerapps":                  "container",
    "microsoft.app/managedenvironments":            "container",
    "microsoft.containerservice/managedclusters":   "container",
    "microsoft.web/sites":                          "container",
    "microsoft.web/serverfarms":                    "container",
}


def _resource_category(rtype: str) -> str:
    return _RTYPE_CATEGORY.get(rtype.lower(), "other")


# ---------------------------------------------------------------------------
# Spot price lookup — hardcoded for common VMs (approx as of Apr 2026)
# Falls back to 40% of on-demand rate if VM not in table
# ---------------------------------------------------------------------------

_SPOT_PRICE_CACHE: dict[str, float] = {}  # "{vm_size}:{region}" → $/hr

# Approximate spot prices ($/hr) for common VM families, by region
# Data source: Azure Retail Prices API (cached locally since container can't reach external APIs)
# These are representative Sept 2025–Apr 2026 spot rates; actual rates vary
_SPOT_PRICES: dict[str, dict[str, float]] = {
    "Standard_D2s_v3": {
        "eastus": 0.0380, "westus": 0.0380, "centralus": 0.0360,
        "northeurope": 0.0420, "westeurope": 0.0410,
    },
    "Standard_D4s_v3": {
        "eastus": 0.0760, "westus": 0.0760, "centralus": 0.0720,
        "northeurope": 0.0840, "westeurope": 0.0820,
    },
    "Standard_D8s_v3": {
        "eastus": 0.1520, "westus": 0.1520, "centralus": 0.1440,
        "northeurope": 0.1680, "westeurope": 0.1640,
    },
    "Standard_E4s_v3": {
        "eastus": 0.0852, "westus": 0.0852, "centralus": 0.0808,
        "northeurope": 0.0945, "westeurope": 0.0924,
    },
}

# On-demand rates for fallback when spot not listed ($/hr, Compute)
_ONDEMAND_PRICES: dict[str, dict[str, float]] = {
    "Standard_D2s_v3": {
        "eastus": 0.0960, "westus": 0.0960, "centralus": 0.0960,
        "northeurope": 0.1056, "westeurope": 0.1056,
    },
    "Standard_D4s_v3": {
        "eastus": 0.1920, "westus": 0.1920, "centralus": 0.1920,
        "northeurope": 0.2112, "westeurope": 0.2112,
    },
    "Standard_D8s_v3": {
        "eastus": 0.3840, "westus": 0.3840, "centralus": 0.3840,
        "northeurope": 0.4224, "westeurope": 0.4224,
    },
    "Standard_E4s_v3": {
        "eastus": 0.2133, "westus": 0.2133, "centralus": 0.2133,
        "northeurope": 0.2346, "westeurope": 0.2346,
    },
}


def _fetch_retail_price(vm_size: str, region: str, price_type: str = "Consumption") -> float | None:
    """Query the Azure Retail Prices API for a VM price.

    price_type: 'Consumption' for on-demand, 'DevTestConsumption' for dev/test,
                'Spot' for spot pricing.
    Returns $/hr or None if not found / unreachable.
    """
    filter_str = (
        f"armRegionName eq '{region.lower()}'"
        f" and armSkuName eq '{vm_size}'"
        f" and priceType eq '{price_type}'"
        " and serviceName eq 'Virtual Machines'"
    )
    url = (
        "https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview&$filter="
        + urllib.parse.quote(filter_str)
    )
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = _json.loads(resp.read())
        items = data.get("Items") or []
        # Prefer non-Windows, non-Low Priority rows
        for item in items:
            sku = (item.get("skuName") or "").lower()
            if "windows" in sku or "low priority" in sku:
                continue
            price = item.get("retailPrice") or item.get("unitPrice")
            if price:
                return float(price)
        # Fallback: return first available price
        if items:
            return float(items[0].get("retailPrice") or items[0].get("unitPrice") or 0) or None
    except Exception as exc:
        log.debug("Retail Prices API unavailable for %s/%s: %s", vm_size, region, exc)
    return None


def _fetch_spot_price(vm_size: str, region: str) -> float | None:
    """Return spot price $/hr for a VM. Tries live API, falls back to table."""
    cache_key = f"spot:{vm_size.lower()}:{region.lower()}"
    if cache_key in _SPOT_PRICE_CACHE:
        return _SPOT_PRICE_CACHE[cache_key]

    # Try live API first
    price = _fetch_retail_price(vm_size, region, price_type="Spot")
    if price is not None:
        _SPOT_PRICE_CACHE[cache_key] = price
        log.info("✓ Spot price %s (%s): $%.4f/hr (live API)", vm_size, region, price)
        return price

    # Fall back to hardcoded table
    region_norm = region.lower()
    if vm_size in _SPOT_PRICES and region_norm in _SPOT_PRICES[vm_size]:
        price = _SPOT_PRICES[vm_size][region_norm]
        _SPOT_PRICE_CACHE[cache_key] = price
        log.info("✓ Spot price %s (%s): $%.4f/hr (table)", vm_size, region_norm, price)
        return price

    if vm_size in _ONDEMAND_PRICES and region_norm in _ONDEMAND_PRICES[vm_size]:
        price = round(_ONDEMAND_PRICES[vm_size][region_norm] * 0.4, 4)
        _SPOT_PRICE_CACHE[cache_key] = price
        log.info("✓ Spot price %s (%s): $%.4f/hr (40%% on-demand table)", vm_size, region_norm, price)
        return price

    log.warning("✗ No spot price found for %s in %s", vm_size, region)
    return None


def _fetch_ondemand_price(vm_size: str, region: str) -> float | None:
    """Return on-demand price $/hr for a VM. Tries live API, falls back to table."""
    cache_key = f"ondemand:{vm_size.lower()}:{region.lower()}"
    if cache_key in _SPOT_PRICE_CACHE:
        return _SPOT_PRICE_CACHE[cache_key]

    # Try live API first
    price = _fetch_retail_price(vm_size, region, price_type="Consumption")
    if price is not None:
        _SPOT_PRICE_CACHE[cache_key] = price
        log.info("✓ On-demand price %s (%s): $%.4f/hr (live API)", vm_size, region, price)
        return price

    # Fall back to hardcoded table
    region_norm = region.lower()
    if vm_size in _ONDEMAND_PRICES and region_norm in _ONDEMAND_PRICES[vm_size]:
        price = _ONDEMAND_PRICES[vm_size][region_norm]
        _SPOT_PRICE_CACHE[cache_key] = price
        log.info("✓ On-demand price %s (%s): $%.4f/hr (table)", vm_size, region_norm, price)
        return price

    log.warning("✗ No on-demand price found for %s in %s", vm_size, region)
    return None


def _fetch_resource_inventory() -> list[dict]:
    """Enumerate all ARM resources with VM power states (runs in executor)."""
    rc = _get_resource_mgmt_client()
    all_res = list(rc.resources.list())
    log.info("ARM inventory: %d resources found", len(all_res))

    # Collect VMs for individual power-state queries
    vm_resources = [
        r for r in all_res
        if r.id and (r.type or "").lower() == "microsoft.compute/virtualmachines"
    ]
    vm_states: dict[str, str] = {}
    vm_meta: dict[str, dict] = {}  # id.lower() → {"vm_size": str, "is_spot": bool, "private_ip": str}
    if vm_resources:
        try:
            cc = _get_compute_mgmt_client()
            for vm_res in vm_resources:
                parts = vm_res.id.split("/resourceGroups/")
                rg = parts[1].split("/")[0] if len(parts) > 1 else ""
                try:
                    inst = cc.virtual_machines.get(rg, vm_res.name, expand="instanceView")
                    statuses = inst.instance_view.statuses if inst.instance_view else []
                    power = next(
                        (s.display_status for s in statuses
                         if s.code.startswith("PowerState/")),
                        "Unknown",
                    )
                    vm_states[vm_res.id.lower()] = power
                    vm_size = (inst.hardware_profile.vm_size or "") if inst.hardware_profile else ""
                    # Fallback: read vm_size from ARM resource properties if Compute didn't return it
                    if not vm_size:
                        try:
                            arm_res = rc.resources.get_by_id(vm_res.id, api_version="2024-03-01")
                            props = arm_res.properties or {}
                            vm_size = (props.get("hardwareProfile") or {}).get("vmSize", "")
                        except Exception:
                            pass
                    # Use ARM priority field only — name heuristic is unreliable
                    is_spot = (getattr(inst, "priority", None) or "").lower() == "spot"
                    # Resolve private IP from the first NIC
                    private_ip = ""
                    try:
                        nics = (inst.network_profile.network_interfaces or []) if inst.network_profile else []
                        if nics:
                            nic_res = rc.resources.get_by_id(nics[0].id, api_version="2024-05-01")
                            nic_props = nic_res.properties or {}
                            ip_cfgs = nic_props.get("ipConfigurations") or []
                            if ip_cfgs:
                                private_ip = (ip_cfgs[0].get("properties") or {}).get("privateIPAddress", "")
                    except Exception:
                        pass
                    vm_meta[vm_res.id.lower()] = {"vm_size": vm_size, "is_spot": is_spot, "private_ip": private_ip}
                except Exception as e:
                    log.debug("VM power state error (%s): %s", vm_res.name, e)
                    vm_states[vm_res.id.lower()] = "Unknown"
        except Exception as e:
            log.warning("Could not fetch VM power states: %s", e)

    result: list[dict] = []
    for r in all_res:
        if not r.id:
            continue
        rtype = r.type or ""
        cat = _resource_category(rtype)
        parts = r.id.split("/resourceGroups/")
        rg = parts[1].split("/")[0] if len(parts) > 1 else ""
        status = vm_states.get(r.id.lower(), "Active") if cat == "vm" else "Active"
        if status.lower() in ("deallocated", "vm deallocated", "stopped", "vm stopped"):
            continue
        meta = vm_meta.get(r.id.lower(), {})
        result.append({
            "id": r.id,
            "name": r.name or "",
            "type": rtype,
            "category": cat,
            "resource_group": rg,
            "location": r.location or "",
            "status": status,
            "vm_size": meta.get("vm_size", ""),
            "is_spot": meta.get("is_spot", False),
            "private_ip": meta.get("private_ip", ""),
        })
    return result


_live_lock: asyncio.Lock = asyncio.Lock()
_live_cache: dict[str, Any] = {}


async def _get_live_data() -> list[dict]:
    """Live resource inventory merged with 30-day cost data (cached)."""
    async with _live_lock:
        now = time.monotonic()
        if "inv" not in _live_cache or (now - _live_cache.get("ts", 0.0)) > LIVE_CACHE_TTL:
            log.info("Live inventory cache miss — fetching from ARM…")
            inv = await asyncio.get_event_loop().run_in_executor(
                None, _fetch_resource_inventory
            )
            _live_cache["inv"] = inv
            _live_cache["ts"] = now
        inv: list[dict] = _live_cache["inv"]

    # Cost correlation (outside lock — uses existing cost cache)
    df = await get_cached_dataframe()
    cost_by_id: dict[str, float] = {}
    if not df.empty and "C_RESOURCE_ID" in df.columns and "C_COST" in df.columns:
        cutoff = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
        mdf = df[df["C_DATE"] >= cutoff] if "C_DATE" in df.columns else df
        # Normalize ResourceId to lowercase BEFORE aggregation (case differences in cost export)
        mdf = mdf.copy()
        mdf["C_RESOURCE_ID"] = mdf["C_RESOURCE_ID"].str.lower()
        agg = mdf.groupby("C_RESOURCE_ID")["C_COST"].sum()
        cost_by_id = {
            str(k): round(float(v), 4)
            for k, v in agg.items()
            if v > 0
        }

    enriched: list[dict] = []
    for r in inv:
        export_cost = cost_by_id.get(r["id"].lower())
        entry: dict = {**r, "monthly_cost": export_cost, "cost_source": "export" if export_cost is not None else None}

        # For VMs, always compute projected monthly from pricing API (hourly_rate × 24 × 30).
        # Export data shows accumulated billing-period cost (could be just 1-2 days), which
        # is misleading in a "Monthly Cost" column. We keep export_cost for reference.
        # Deallocated/stopped VMs don't accrue compute charges — skip pricing lookup.
        vm_is_active = (r.get("status") or "").lower() not in ("deallocated", "stopped", "vm deallocated", "vm stopped")
        if r.get("vm_size") and r.get("location") and vm_is_active:
            vm_size_norm = r["vm_size"]
            region_norm = r["location"].lower()
            if export_cost is not None:
                entry["export_cost"] = export_cost  # preserve actual billing data
            if r.get("is_spot"):
                spot_price = await asyncio.get_event_loop().run_in_executor(
                    None, _fetch_spot_price, vm_size_norm, region_norm
                )
                if spot_price is not None:
                    entry["monthly_cost"] = round(spot_price * 24 * 30, 2)
                    entry["spot_price_per_hour"] = round(spot_price, 4)
                    entry["cost_source"] = "spot_rate"
            else:
                ondemand = await asyncio.get_event_loop().run_in_executor(
                    None, _fetch_ondemand_price, vm_size_norm, region_norm
                )
                if ondemand is not None:
                    entry["monthly_cost"] = round(ondemand * 24 * 30, 2)
                    entry["cost_source"] = "price_table"

        enriched.append(entry)
    return enriched


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

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
# Category filtering helpers
# ---------------------------------------------------------------------------


def _period_days(period: str) -> int:
    return {"day": 1, "week": 7, "month": 30}.get(period, 7)


def _filter_period(df: pd.DataFrame, days: int) -> pd.DataFrame:
    """Filter rows to the last *days* calendar days using C_DATE."""
    if "C_DATE" not in df.columns:
        return df
    cutoff = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    return df[df["C_DATE"] >= cutoff]


def _filter_services(df: pd.DataFrame, services: set[str]) -> pd.DataFrame:
    """Keep only rows whose C_SERVICE matches the given set (case-insensitive)."""
    if "C_SERVICE" not in df.columns:
        return df
    lower = {s.lower() for s in services}
    return df[df["C_SERVICE"].str.lower().isin(lower)]


def _cost_by(df: pd.DataFrame, col: str) -> dict[str, float]:
    if col not in df.columns or "C_COST" not in df.columns:
        return {}
    grp = df.groupby(col)["C_COST"].sum()
    return grp[grp > 0].sort_values(ascending=False).round(6).to_dict()


async def _category_api(period: str, services: set[str]) -> dict:
    df = await get_cached_dataframe()
    days = _period_days(period)
    filtered = _filter_services(_filter_period(df, days), services)

    # Fallback: if day period is empty, use latest available day
    fallback = False
    if period == "day" and filtered.empty and "C_DATE" in df.columns:
        df_svc = _filter_services(df, services)
        if not df_svc.empty:
            last_date = df_svc["C_DATE"].dropna().max()
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
# FastAPI application
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(
    title="azure-penny",
    description="Azure Cost Management dashboard — reads Cost exports from Blob Storage.",
    version="1.0.0",
)

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


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
    return JSONResponse(
        {
            "status": "ok",
            "service": "azure-penny",
            "storage_account": STORAGE_ACCOUNT_NAME or "not configured",
            "container": STORAGE_CONTAINER_NAME,
        }
    )


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
    return JSONResponse(
        {
            "storage_account": STORAGE_ACCOUNT_NAME or "not configured",
            "container": STORAGE_CONTAINER_NAME,
            "row_count": len(cached_df) if cached_df is not None else None,
            "date_range": periods,
            "cache_age_s": cache_age_s,
            "no_data": cached_df is not None and cached_df.empty,
        }
    )


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
async def api_compute(period: str = "week") -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_COMPUTE))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/compute/machines", tags=["api"])
async def api_compute_machines(period: str = "week") -> JSONResponse:
    """Cost breakdown per individual machine (resource name) for compute services."""
    try:
        df = await get_cached_dataframe()
        days = _period_days(period)
        filtered = _filter_services(_filter_period(df, days), CAT_COMPUTE)

        fallback = False
        if period == "day" and filtered.empty and "C_DATE" in df.columns:
            df_svc = _filter_services(df, CAT_COMPUTE)
            if not df_svc.empty:
                last_date = df_svc["C_DATE"].dropna().max()
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
async def api_storage(period: str = "week") -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_STORAGE))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/network", tags=["api"])
async def api_network(period: str = "week") -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_NETWORK))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/database", tags=["api"])
async def api_database(period: str = "week") -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_DATABASE))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Raw costs (JSON) ──────────────────────────────────────────────────────────

@app.get("/costs", tags=["costs"])
async def get_costs() -> JSONResponse:
    """Aggregated cost data grouped by service, resource group, and date."""
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
    """Total cost, top-5 services, and top-5 resource groups."""
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
    """Clear the cache and reload from Azure Blob Storage."""
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
    """Diagnostic endpoint: shows raw columns, actual service names, and date range."""
    try:
        client = get_blob_service_client()
        container_client = client.get_container_client(STORAGE_CONTAINER_NAME)
        all_blobs = [b.name for b in container_client.list_blobs()]
        blob_paths = all_blobs[:20]  # first 20 only

        df = await get_cached_dataframe()
        raw_cols = list(df.columns)

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
            "blob_paths_sample": blob_paths,
            "row_count": len(df),
            "columns": raw_cols,
            "date_range": date_range,
            "total_cost": total_cost,
            "top_services": top_services,
            "top_resource_groups": top_rgs,
        })
    except Exception as exc:
        log.exception("Debug endpoint failed")
        return JSONResponse({"error": str(exc)}, status_code=500)



# ── Technician API endpoints ──────────────────────────────────────────────────

@app.get("/api/services", tags=["api"])
async def api_services(period: str = "week") -> JSONResponse:
    """All services grouped by MeterCategory for the given period."""
    try:
        df = await get_cached_dataframe()
        days = _period_days(period)
        filtered = _filter_period(df, days)

        fallback = False
        if period == "day" and filtered.empty and "C_DATE" in df.columns and not df.empty:
            last_date = df["C_DATE"].dropna().max()
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
    """Service-level costs grouped by MeterCategory for the last 30 days.

    These are aggregate summaries shown in the Services tab. Individual
    resource costs are shown per ARM resource in /api/live-resources.
    """
    try:
        df = await get_cached_dataframe()
        if df.empty or "C_SERVICE" not in df.columns or "C_COST" not in df.columns:
            return JSONResponse({"services": [], "total_usd": 0})

        cutoff = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
        mdf = df[df["C_DATE"] >= cutoff] if "C_DATE" in df.columns else df

        # Group by service and sum costs
        by_svc = mdf.groupby("C_SERVICE")["C_COST"].sum().sort_values(ascending=False)

        services = [
            {
                "name": str(svc),
                "monthly_cost": round(float(cost), 2),
                "category": "service",
            }
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
    """Search cost export for rows matching a resource name pattern."""
    try:
        df = await get_cached_dataframe()
        if df.empty or "C_NAME" not in df.columns:
            return JSONResponse({"results": [], "count": 0})

        # Search by resource name (case-insensitive substring match)
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
            ]
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/resource-groups", tags=["api"])
async def api_resource_groups(period: str = "week") -> JSONResponse:
    """Cost grouped by resource group (C_NAME) for the given period."""
    try:
        df = await get_cached_dataframe()
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


@app.get("/api/live-resources", tags=["api"])
async def api_live_resources() -> JSONResponse:
    """All live Azure resources with 30-day cost from Cost Management exports."""
    try:
        resources = await _get_live_data()

        # Sum the actual billed export cost for matched ARM resources.
        # VMs store it in "export_cost" (monthly_cost is overridden by pricing API);
        # everything else stores it directly in "monthly_cost" when cost_source=="export".
        matched_export_usd = 0.0
        for r in resources:
            ec = r.get("export_cost")
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
    """Clear the live resource cache and re-fetch from ARM."""
    async with _live_lock:
        _live_cache.clear()
    try:
        resources = await _get_live_data()
        return JSONResponse({"status": "reloaded", "count": len(resources)})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/resource", tags=["infrastructure"])
async def api_delete_resource(resource_id: str) -> StreamingResponse:
    """Delete a live Azure resource by its full ARM resource ID.

    Streams status log lines as plain text while the operation runs.
    resource_id example:
        /subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Compute/virtualMachines/myvm
    """
    if not resource_id.lower().startswith("/subscriptions/"):
        raise HTTPException(status_code=400, detail="resource_id must be a full ARM resource ID")

    log.warning("⚠️  DELETE resource initiated: %s", resource_id)

    def _get_api_version(client: ResourceManagementClient, namespace: str, rtype: str) -> str:
        """Return the latest non-preview API version for a resource type."""
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

            # Parse provider namespace + resource type from the ARM ID
            # /subscriptions/{s}/resourceGroups/{rg}/providers/{ns}/{type}/{name}[/...]
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

            # Poll every 5 s and stream status
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
    """Delete all resources in an Azure resource group by deleting the group itself.

    Streams status log lines as plain text while the operation runs.
    """
    if not resource_group_name or not resource_group_name.strip():
        raise HTTPException(status_code=400, detail="resource_group_name is required")

    rg = resource_group_name.strip()
    log.warning("⚠️  DELETE resource group initiated: %s", rg)

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
async def api_delete_all_resource_groups(resource_groups: list[str]) -> StreamingResponse:
    """Delete multiple Azure resource groups sequentially.

    Accepts a JSON array of resource group names in the request body.
    Streams progress for each group as plain text.
    """
    if not resource_groups:
        raise HTTPException(status_code=400, detail="resource_groups list is empty")

    log.warning("⚠️  BULK DELETE resource groups initiated: %s", resource_groups)

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


@app.get("/api/spot-price-debug", tags=["api"])
async def api_spot_price_debug(vm_size: str, region: str) -> JSONResponse:
    """Debug endpoint: test spot price lookup for a given VM size + region."""
    try:
        price = await asyncio.get_event_loop().run_in_executor(
            None, _fetch_spot_price, vm_size, region
        )
        if price is None:
            return JSONResponse({"vm_size": vm_size, "region": region, "price": None, "status": "not_found"})
        monthly = round(price * 24 * 30, 2)
        return JSONResponse({
            "vm_size": vm_size,
            "region": region,
            "hourly_usd": round(price, 4),
            "monthly_usd": monthly,
            "status": "found",
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc), "vm_size": vm_size, "region": region}, status_code=500)


@app.get("/api/daily", tags=["api"])
async def api_daily(days: int = 30) -> JSONResponse:
    """Daily spend totals for the last N days."""
    try:
        df = await get_cached_dataframe()
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
