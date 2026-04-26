"""
Azure Blob Storage access, column mapping, data loading, and in-memory cache.
"""

import asyncio
import io
import re
import time
from typing import Any

import pandas as pd
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

from config import (
    AZURE_CLIENT_ID,
    STORAGE_ACCOUNT_NAME,
    STORAGE_CONTAINER_NAME,
    TTL_SECONDS,
    log,
)

# ---------------------------------------------------------------------------
# Azure Blob Storage client (lazy singleton)
# ---------------------------------------------------------------------------

_blob_client: BlobServiceClient | None = None


def get_blob_service_client() -> BlobServiceClient:
    """Return a cached BlobServiceClient authenticated via DefaultAzureCredential."""
    global _blob_client
    if _blob_client is None:
        if not STORAGE_ACCOUNT_NAME:
            raise RuntimeError("STORAGE_ACCOUNT_NAME environment variable is not set.")
        credential = DefaultAzureCredential(managed_identity_client_id=AZURE_CLIENT_ID or None)
        account_url = f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net"
        _blob_client = BlobServiceClient(account_url=account_url, credential=credential)
        log.info("BlobServiceClient initialised for account: %s", STORAGE_ACCOUNT_NAME)
    return _blob_client


# ---------------------------------------------------------------------------
# Column mapping  (Azure Cost Management → internal names)
# ---------------------------------------------------------------------------

COLUMN_MAP: dict[str, str] = {
    "CostInBillingCurrency": "C_COST",
    "Cost":                  "C_COST",
    "PreTaxCost":            "C_COST",
    "MeterCategory":         "C_SERVICE",
    "ServiceName":           "C_SERVICE",
    "ResourceGroup":         "C_NAME",
    "ResourceGroupName":     "C_NAME",
    "SubscriptionName":      "C_ACCOUNT",
    "SubscriptionId":        "C_ACCOUNT",
    "Date":                  "C_DATE",
    "UsageDate":             "C_DATE",
    "Tags":                  "C_TAGS",
    "tag_":                  "C_TAGS",
    "ResourceId":            "C_RESOURCE_ID",
    "InstanceId":            "C_RESOURCE_ID",
    "MeterSubCategory":      "C_SUBCATEGORY",
    "SubCategory":           "C_SUBCATEGORY",
    "Quantity":              "C_QUANTITY",
    "UsageQuantity":         "C_QUANTITY",
}

REQUIRED_INTERNAL_COLS = {"C_COST", "C_SERVICE", "C_NAME", "C_ACCOUNT", "C_DATE"}

# ---------------------------------------------------------------------------
# Blob discovery
# ---------------------------------------------------------------------------

_DATE_FOLDER_RE = re.compile(r"^(\d{8})-(\d{8})$")
_EXPORT_EXTS = (".parquet", ".csv", ".csv.gz")


def _discover_latest_blobs(container_name: str) -> list[str]:
    """Return only the most-recently modified export file(s) from the container.

    Azure daily exports are cumulative — each new run contains all data from
    the start of the billing period.  We pick only the newest file in the
    latest date-range folder to avoid double-counting.
    """
    client = get_blob_service_client()
    container_client = client.get_container_client(container_name)

    all_blob_props = list(container_client.list_blobs())
    if not all_blob_props:
        return []

    log.info("Found %d blob(s) in container '%s'", len(all_blob_props), container_name)

    date_segments: set[str] = set()
    for bp in all_blob_props:
        for part in bp.name.split("/"):
            if _DATE_FOLDER_RE.match(part):
                date_segments.add(part)

    if not date_segments:
        log.warning(
            "No date-folder segments (YYYYMMDD-YYYYMMDD) found in blob paths. "
            "Falling back to newest .parquet/.csv file."
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
    return blob_client.download_blob().readall()


def _blob_to_dataframe(raw: bytes, blob_name: str) -> pd.DataFrame:
    buf = io.BytesIO(raw)
    if blob_name.endswith(".parquet"):
        return pd.read_parquet(buf, engine="pyarrow")
    if blob_name.endswith(".csv.gz"):
        return pd.read_csv(buf, compression="gzip")
    return pd.read_csv(buf)


def _apply_column_map(df: pd.DataFrame) -> pd.DataFrame:
    lower_map = {k.lower(): v for k, v in COLUMN_MAP.items()}

    rename: dict[str, str] = {}
    for raw_col in df.columns:
        internal = lower_map.get(raw_col.lower())
        if internal and internal not in rename.values():
            rename[raw_col] = internal
        elif raw_col.lower().startswith("tag_") and "C_TAGS" not in rename.values():
            rename[raw_col] = "C_TAGS"

    df = df.rename(columns=rename)

    if "C_COST" in df.columns:
        df["C_COST"] = pd.to_numeric(df["C_COST"], errors="coerce").fillna(0.0)

    if "C_DATE" in df.columns:
        df["C_DATE"] = pd.to_datetime(df["C_DATE"], errors="coerce").dt.strftime("%Y-%m-%d")

    if "C_NAME" in df.columns:
        df["C_NAME"] = df["C_NAME"].str.lower()

    return df


def _load_dataframe() -> pd.DataFrame:
    blob_names = _discover_latest_blobs(STORAGE_CONTAINER_NAME)
    if not blob_names:
        raise ValueError(f"No cost export files found in container '{STORAGE_CONTAINER_NAME}'.")

    frames: list[pd.DataFrame] = []
    for name in blob_names:
        log.info("Loading blob: %s", name)
        raw = _read_blob_to_bytes(STORAGE_CONTAINER_NAME, name)
        frames.append(_blob_to_dataframe(raw, name))

    merged = pd.concat(frames, ignore_index=True)
    merged = _apply_column_map(merged)

    missing = REQUIRED_INTERNAL_COLS - set(merged.columns)
    if missing:
        log.warning(
            "Expected columns not found after mapping: %s. Check COLUMN_MAP.", missing
        )

    log.info("DataFrame loaded: %d rows, %d columns", len(merged), len(merged.columns))
    return merged


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------

_lock: asyncio.Lock = asyncio.Lock()
_cache: dict[str, Any] = {}


async def get_cached_dataframe() -> pd.DataFrame:
    """Return the cached DataFrame, refreshing if expired or absent."""
    async with _lock:
        now = time.monotonic()
        loaded_at: float = _cache.get("loaded_at", 0.0)

        if "df" not in _cache or (now - loaded_at) > TTL_SECONDS:
            log.info("Cache miss — loading data from Azure Blob Storage …")
            try:
                df = await asyncio.get_event_loop().run_in_executor(None, _load_dataframe)
            except ValueError as exc:
                log.warning("%s — returning empty DataFrame.", exc)
                df = pd.DataFrame(columns=list(REQUIRED_INTERNAL_COLS))
            _cache["df"] = df
            _cache["loaded_at"] = now
            log.info("Cache refreshed at %.0f", now)

        return _cache["df"]
