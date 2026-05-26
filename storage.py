"""
Azure Blob Storage access, column mapping, data loading, and in-memory cache.
"""

import asyncio
import io
import json as _json
import re
import time
from typing import Any

import pandas as pd
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

from config import (
    AZURE_CLIENT_ID,
    AZURE_SUBSCRIPTION_ID,
    COST_TAG_KEY,
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
    # PayG (pre-credit) cost preferred — shows actual consumption even when credits cover it.
    # Falls back to CostInBillingCurrency for export formats that don't include paygCost.
    "PaygCostInBillingCurrency": "C_COST",
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
    # Separate per-tag columns (some export formats) — map project tag directly to C_APP
    "tag_project":           "C_APP",
    "ResourceId":            "C_RESOURCE_ID",
    "InstanceId":            "C_RESOURCE_ID",
    "MeterSubCategory":      "C_SUBCATEGORY",
    "SubCategory":           "C_SUBCATEGORY",
    "Quantity":              "C_QUANTITY",
    "UsageQuantity":         "C_QUANTITY",
}


def _extract_project_tag(tags_val: object) -> str | None:
    """Extract the application tag value from a Cost Management Tags JSON string.

    Uses COST_TAG_KEY (env var, default 'project') for case-insensitive lookup.
    Returns the tag value string, or None if the tag key is absent / unparseable.
    """
    if tags_val is None:
        return None
    s = str(tags_val).strip()
    if s in ("", "{}", "nan", "None"):
        return None
    try:
        tags = _json.loads(s)
        tag_key_lower = COST_TAG_KEY.lower()
        val = next(
            (v for k, v in tags.items() if k.lower() == tag_key_lower),
            None,
        )
        return str(val).strip() if val and str(val).strip() else None
    except Exception:
        return None


def _extract_all_tag_keys(tags_val: object) -> list[str]:
    """Return all tag key names from a Tags JSON blob (for diagnostics)."""
    if tags_val is None:
        return []
    s = str(tags_val).strip()
    if s in ("", "{}", "nan", "None"):
        return []
    try:
        return list(_json.loads(s).keys())
    except Exception:
        return []


def _extract_cluster_app(cluster_name: str) -> str | None:
    """Extract an app/project name from an AKS cluster name.

    Handles two common naming conventions (case-insensitive):
      1. {env}-{app}-aks        e.g. dev-seip-aks      → "seip"
                                     az-llm-aks         → "llm"
      2. aks-{env}-{region}-{app}  e.g. aks-dev-wus2-llm → "llm"

    Returns None if no pattern matches.
    """
    c = cluster_name.lower().strip()
    # Convention 1: ends with -aks, app is the middle segment(s)
    m = re.match(r"[^-]+-(.+)-aks$", c)
    if m:
        return m.group(1)
    # Convention 2: starts with aks-, followed by env and region, app is the last segment
    m = re.match(r"^aks-[^-]+-[^-]+-(.+)$", c)
    if m:
        return m.group(1)
    return None


def _infer_app_from_mc_rg(rg_lower: str) -> str | None:
    """Infer app name from an AKS-managed resource group name.

    AKS creates a managed RG with the format:
        mc_{parent-rg}_{cluster-name}_{region}
    e.g. mc_dev-seip-rg_dev-seip-aks_westeurope → "seip"
         mc_rg-dev-wus2-llm_aks-dev-wus2-llm_westus2 → "llm"

    Returns None if the pattern does not match.
    """
    if not rg_lower.startswith("mc_"):
        return None
    parts = rg_lower.split("_")
    # Expect at least: ["mc", "<parent-rg>", "<cluster-name>", "<region>"]
    if len(parts) < 4:
        return None
    cluster_name = parts[2]  # e.g. "dev-seip-aks" or "aks-dev-wus2-llm"
    return _extract_cluster_app(cluster_name)


def _infer_app_from_aks_tags(tags_val: object) -> str | None:
    """Infer app name from AKS-managed system tags on node pool VMs / Karpenter nodes.

    AKS-managed VMs and Karpenter spot nodes carry system tags but NOT a user
    'project' tag.  This function reads two well-known AKS tags to find the
    cluster name and then derives the app from it:

      karpenter.azure.com_cluster  — set on every Karpenter-provisioned node
      aks-managed-cluster-name     — set on AKS managed node pool VMs

    Returns None if neither tag is present or cluster name cannot be parsed.
    """
    if tags_val is None:
        return None
    s = str(tags_val).strip()
    if s in ("", "{}", "nan", "None"):
        return None
    try:
        tags = _json.loads(s)
        cluster = (
            tags.get("karpenter.azure.com_cluster")
            or tags.get("aks-managed-cluster-name")
        )
        if cluster:
            return _extract_cluster_app(str(cluster))
    except Exception:
        pass
    return None

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

    for segment in sorted(date_segments, reverse=True):
        candidates = [
            bp for bp in all_blob_props
            if segment in bp.name and bp.name.endswith(_EXPORT_EXTS)
        ]
        if not candidates:
            log.info("Folder '%s' has no export files, trying previous period.", segment)
            continue

        if len(candidates) == 1:
            log.info("Selected 1 file from folder '%s': %s", segment, candidates[0].name)
            return [candidates[0].name]

        newest = max(candidates, key=lambda bp: bp.last_modified or 0)
        log.info(
            "Found %d file(s) in folder '%s'; using only the most-recent: %s (last_modified=%s)",
            len(candidates), segment, newest.name, newest.last_modified,
        )
        return [newest.name]

    return []


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
    # Build priority-ordered list: COLUMN_MAP order determines which source column wins
    # when multiple CSV columns map to the same internal name.
    priority_map: dict[str, str] = {}  # internal_name -> preferred raw_col
    lower_map = {k.lower(): v for k, v in COLUMN_MAP.items()}
    for map_key_lower, internal in lower_map.items():
        for raw_col in df.columns:
            if raw_col.lower() == map_key_lower:
                if internal not in priority_map:
                    priority_map[internal] = raw_col
                break

    rename: dict[str, str] = {raw: internal for internal, raw in priority_map.items()}

    for raw_col in df.columns:
        if raw_col.lower().startswith("tag_") and "C_TAGS" not in rename.values():
            rename[raw_col] = "C_TAGS"

    df = df.rename(columns=rename)

    if "C_COST" in df.columns:
        df["C_COST"] = pd.to_numeric(df["C_COST"], errors="coerce").fillna(0.0)

    if "C_DATE" in df.columns:
        df["C_DATE"] = pd.to_datetime(df["C_DATE"], errors="coerce").dt.strftime("%Y-%m-%d")

    if "C_NAME" in df.columns:
        df["C_NAME"] = df["C_NAME"].str.lower()

    # Derive C_APP (application / project tag).
    # Priority: explicit tag_project column (already renamed above) → JSON parse of C_TAGS.
    if "C_APP" not in df.columns:
        if "C_TAGS" in df.columns:
            # _extract_project_tag returns str | None; None → filled as "Untagged" below
            df["C_APP"] = df["C_TAGS"].apply(_extract_project_tag)
        else:
            df["C_APP"] = None
    # Normalise: empty / None → "Untagged"
    df["C_APP"] = df["C_APP"].fillna("Untagged")
    df["C_APP"] = df["C_APP"].replace("", "Untagged")

    # Fallback 1: infer app from AKS-managed resource group name (mc_*).
    # Resources in mc_* RGs don't inherit tags from the AKS cluster, so costs
    # would otherwise appear as "Untagged".  Extract the app from the cluster
    # name embedded in the managed RG name.
    if "C_NAME" in df.columns:
        untagged_mask = df["C_APP"] == "Untagged"
        if untagged_mask.any():
            inferred = df.loc[untagged_mask, "C_NAME"].apply(_infer_app_from_mc_rg)
            filled = inferred.notna()
            if filled.any():
                df.loc[untagged_mask & filled, "C_APP"] = inferred[filled]
                log.debug(
                    "Inferred C_APP from AKS managed RG for %d rows", filled.sum()
                )

    # Fallback 2: infer app from AKS system tags on Karpenter / managed node VMs.
    # These carry tags like karpenter.azure.com_cluster or aks-managed-cluster-name
    # but NOT a user 'project' tag.  Parse the cluster name to get the app.
    if "C_TAGS" in df.columns:
        untagged_mask = df["C_APP"] == "Untagged"
        if untagged_mask.any():
            inferred = df.loc[untagged_mask, "C_TAGS"].apply(_infer_app_from_aks_tags)
            filled = inferred.notna()
            if filled.any():
                df.loc[untagged_mask & filled, "C_APP"] = inferred[filled]
                log.debug(
                    "Inferred C_APP from AKS system tags (Karpenter/node pool) for %d rows",
                    filled.sum(),
                )

    # Subscription-scoped charges (bandwidth, AAD, Azure Monitor…) have no ResourceGroup
    # and cannot carry a resource-level tag.  Label them explicitly instead of "Untagged"
    # so it's clear these are legitimately unattributable shared costs, not a tagging gap.
    if "C_RESOURCE_ID" in df.columns and "C_NAME" in df.columns:
        untagged_mask = df["C_APP"] == "Untagged"
        if untagged_mask.any():
            no_rg = (
                df["C_RESOURCE_ID"].fillna("").str.strip() == ""
            ) | (
                df["C_NAME"].fillna("").str.strip() == ""
            )
            subscription_scoped = untagged_mask & no_rg
            if subscription_scoped.any():
                df.loc[subscription_scoped, "C_APP"] = "Shared/Unattributed"
                log.debug(
                    "Labelled %d subscription-scoped rows as 'Shared/Unattributed'",
                    subscription_scoped.sum(),
                )

    return df


def _load_from_cost_management_api() -> pd.DataFrame:
    """Query the Azure Cost Management API directly (fallback when no blob exports exist).

    Groups by ResourceId + MeterCategory so that the live-page resource matching
    (which joins on C_RESOURCE_ID) works correctly.  ResourceGroup (C_NAME) is
    derived from the ResourceId after loading.
    """
    import json
    import urllib.error
    import urllib.request
    from datetime import date, timedelta

    if not AZURE_SUBSCRIPTION_ID:
        raise ValueError("AZURE_SUBSCRIPTION_ID is not set; cannot query Cost Management API.")

    cred = DefaultAzureCredential(managed_identity_client_id=AZURE_CLIENT_ID or None)
    token = cred.get_token("https://management.azure.com/.default").token
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    start = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")
    end = date.today().strftime("%Y-%m-%d")
    body = json.dumps({
        "type": "Usage",
        "timeframe": "Custom",
        "timePeriod": {"from": f"{start}T00:00:00+00:00", "to": f"{end}T23:59:59+00:00"},
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [
                {"type": "Dimension", "name": "ResourceId"},
                {"type": "Dimension", "name": "MeterCategory"},
                {"type": "TagKey", "name": "project"},
            ],
        },
    }).encode()

    base_url = (
        f"https://management.azure.com/subscriptions/{AZURE_SUBSCRIPTION_ID}"
        "/providers/Microsoft.CostManagement/query?api-version=2023-11-01"
    )

    all_rows: list = []
    col_names: list[str] = []
    url: str | None = base_url

    while url:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Cost Management API error {exc.code}: {exc.read().decode()}") from exc

        props = data.get("properties", {})
        if not col_names:
            col_names = [c["name"] for c in props.get("columns", [])]
        all_rows.extend(props.get("rows", []))
        url = props.get("nextLink")

    if not all_rows:
        log.warning("Cost Management API returned 0 rows.")
        return pd.DataFrame(columns=list(REQUIRED_INTERNAL_COLS))

    df = pd.DataFrame(all_rows, columns=col_names)

    # The TagKey grouping returns a column named "project" — rename it so that
    # _apply_column_map picks it up via the tag_project → C_APP mapping.
    if "project" in df.columns:
        df = df.rename(columns={"project": "tag_project"})

    # UsageDate comes back as an integer YYYYMMDD
    if "UsageDate" in df.columns:
        df["UsageDate"] = pd.to_datetime(df["UsageDate"].astype(str), format="%Y%m%d").dt.strftime("%Y-%m-%d")

    # Derive ResourceGroup from ResourceId so C_NAME is available for dashboards.
    # The ARM resource ID format is: /subscriptions/{sub}/resourceGroups/{rg}/...
    if "ResourceId" in df.columns:
        df["ResourceGroupName"] = (
            df["ResourceId"]
            .str.extract(r"/resourceGroups/([^/]+)", flags=re.IGNORECASE)[0]
            .fillna("")
            .str.lower()
        )

    log.info("Cost Management API returned %d rows (90-day window).", len(df))
    return df


def _load_dataframe() -> pd.DataFrame:
    blob_names = _discover_latest_blobs(STORAGE_CONTAINER_NAME)
    if not blob_names:
        if AZURE_SUBSCRIPTION_ID:
            log.info("No blob exports found — falling back to Cost Management API.")
            df = _load_from_cost_management_api()
            df = _apply_column_map(df)
            # The API grouping omits subscription; fill C_ACCOUNT so required cols are met.
            if "C_ACCOUNT" not in df.columns:
                df["C_ACCOUNT"] = AZURE_SUBSCRIPTION_ID
            return df
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
