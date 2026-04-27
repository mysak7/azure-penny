"""
Azure Resource Management clients, pricing lookups, and live resource inventory.
"""

import asyncio
import json as _json
import re
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import Any

from azure.identity import DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.resource import ResourceManagementClient

try:
    from azure.mgmt.storage import StorageManagementClient
    _HAS_STORAGE_MGMT = True
except ImportError:
    StorageManagementClient = None  # type: ignore[assignment,misc]
    _HAS_STORAGE_MGMT = False

from config import AZURE_CLIENT_ID, AZURE_SUBSCRIPTION_ID, LIVE_CACHE_TTL, log
from storage import get_cached_dataframe

# ---------------------------------------------------------------------------
# ARM management clients (lazy singletons)
# ---------------------------------------------------------------------------

_resource_mgmt_client: ResourceManagementClient | None = None
_compute_mgmt_client: ComputeManagementClient | None = None
_storage_mgmt_client = None


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


def _get_storage_mgmt_client():
    global _storage_mgmt_client
    if _storage_mgmt_client is None:
        if not AZURE_SUBSCRIPTION_ID:
            raise RuntimeError("AZURE_SUBSCRIPTION_ID environment variable is not set.")
        credential = DefaultAzureCredential(managed_identity_client_id=AZURE_CLIENT_ID or None)
        _storage_mgmt_client = StorageManagementClient(credential, AZURE_SUBSCRIPTION_ID)
    return _storage_mgmt_client


# ---------------------------------------------------------------------------
# Azure Files Premium pricing
# ---------------------------------------------------------------------------

_FILES_PREMIUM_PRICES: dict[str, float] = {
    "eastus": 0.21, "eastus2": 0.21, "westus": 0.21, "westus2": 0.21, "westus3": 0.21,
    "centralus": 0.21, "northcentralus": 0.21, "southcentralus": 0.21,
    "northeurope": 0.22, "westeurope": 0.22, "uksouth": 0.22, "ukwest": 0.22,
    "swedencentral": 0.21, "switzerlandnorth": 0.25,
    "japaneast": 0.23, "japanwest": 0.23,
    "southeastasia": 0.23, "eastasia": 0.24,
    "australiaeast": 0.25, "australiasoutheast": 0.25,
    "brazilsouth": 0.28,
}
_FILES_PRICE_CACHE: dict[str, float] = {}


def _fetch_files_premium_price(region: str) -> float:
    region_lc = region.lower()
    if region_lc in _FILES_PRICE_CACHE:
        return _FILES_PRICE_CACHE[region_lc]

    filter_str = (
        f"armRegionName eq '{region_lc}'"
        " and serviceName eq 'Storage'"
        " and priceType eq 'Consumption'"
    )
    url = (
        "https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview&$filter="
        + urllib.parse.quote(filter_str)
    )
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = _json.loads(resp.read())
        for item in (data.get("Items") or []):
            sku   = (item.get("skuName") or "").lower()
            meter = (item.get("meterName") or "").lower()
            if "premium" in sku and "file" in sku and "data stored" in meter and "lrs" in sku:
                price = float(item.get("retailPrice") or item.get("unitPrice") or 0)
                if price > 0:
                    _FILES_PRICE_CACHE[region_lc] = price
                    log.info("Azure Files Premium LRS price %s: $%.4f/GiB/mo (live API)", region, price)
                    return price
    except Exception as exc:
        log.debug("Files pricing API error for %s: %s", region, exc)

    fallback = _FILES_PREMIUM_PRICES.get(region_lc, 0.21)
    _FILES_PRICE_CACHE[region_lc] = fallback
    log.info("Azure Files Premium LRS price %s: $%.4f/GiB/mo (table)", region, fallback)
    return fallback


# ---------------------------------------------------------------------------
# Resource type → UI category
# ---------------------------------------------------------------------------

_RTYPE_CATEGORY: dict[str, str] = {
    "microsoft.compute/virtualmachines":            "vm",
    "microsoft.compute/virtualmachinescalesets":    "vm",
    "microsoft.compute/disks":                      "storage",
    "microsoft.compute/snapshots":                  "storage",
    "microsoft.storage/storageaccounts":                        "storage",
    "microsoft.storage/storageaccounts/fileservices/shares":    "storage",
    "microsoft.netapp/netappaccounts":                          "storage",
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
# VM spot / on-demand pricing
# ---------------------------------------------------------------------------

_SPOT_PRICE_CACHE: dict[str, float] = {}

_SPOT_PRICES: dict[str, dict[str, float]] = {
    "Standard_D2s_v3": {
        "eastus": 0.0380, "westus": 0.0380, "westus2": 0.0380, "centralus": 0.0360,
        "northeurope": 0.0420, "westeurope": 0.0410,
        "swedencentral": 0.0400, "japaneast": 0.0390, "southeastasia": 0.0380,
        "australiaeast": 0.0420, "uksouth": 0.0410, "eastasia": 0.0390,
    },
    "Standard_D4s_v3": {
        "eastus": 0.0760, "westus": 0.0760, "westus2": 0.0760, "centralus": 0.0720,
        "northeurope": 0.0840, "westeurope": 0.0820,
        "swedencentral": 0.0800, "japaneast": 0.0780, "southeastasia": 0.0760,
        "australiaeast": 0.0840, "uksouth": 0.0820, "eastasia": 0.0780,
    },
    "Standard_D8s_v3": {
        "eastus": 0.1520, "westus": 0.1520, "westus2": 0.1520, "centralus": 0.1440,
        "northeurope": 0.1680, "westeurope": 0.1640,
        "swedencentral": 0.1600, "japaneast": 0.1560, "southeastasia": 0.1520,
    },
    "Standard_E4s_v3": {
        "eastus": 0.0852, "westus": 0.0852, "westus2": 0.0852, "centralus": 0.0808,
        "northeurope": 0.0945, "westeurope": 0.0924,
        "swedencentral": 0.0900, "japaneast": 0.0876,
    },
    "Standard_F1als_v7": {
        "eastus": 0.0033, "westus": 0.0033, "westus2": 0.0033, "westus3": 0.0033,
        "centralus": 0.0031, "northeurope": 0.0036, "westeurope": 0.0035,
        "swedencentral": 0.0034, "japaneast": 0.0034, "southeastasia": 0.0033,
        "australiaeast": 0.0036, "uksouth": 0.0035, "eastasia": 0.0034,
    },
}

_ONDEMAND_PRICES: dict[str, dict[str, float]] = {
    "Standard_D2s_v3": {
        "eastus": 0.0960, "westus": 0.0960, "westus2": 0.0960, "centralus": 0.0960,
        "northeurope": 0.1056, "westeurope": 0.1056,
        "swedencentral": 0.1056, "japaneast": 0.1008, "southeastasia": 0.1008,
        "australiaeast": 0.1072, "uksouth": 0.1056, "eastasia": 0.1008,
    },
    "Standard_D4s_v3": {
        "eastus": 0.1920, "westus": 0.1920, "westus2": 0.1920, "centralus": 0.1920,
        "northeurope": 0.2112, "westeurope": 0.2112,
        "swedencentral": 0.2112, "japaneast": 0.2016, "southeastasia": 0.2016,
        "australiaeast": 0.2144, "uksouth": 0.2112, "eastasia": 0.2016,
    },
    "Standard_D8s_v3": {
        "eastus": 0.3840, "westus": 0.3840, "westus2": 0.3840, "centralus": 0.3840,
        "northeurope": 0.4224, "westeurope": 0.4224,
        "swedencentral": 0.4224, "japaneast": 0.4032, "southeastasia": 0.4032,
    },
    "Standard_E4s_v3": {
        "eastus": 0.2133, "westus": 0.2133, "westus2": 0.2133, "centralus": 0.2133,
        "northeurope": 0.2346, "westeurope": 0.2346,
        "swedencentral": 0.2346, "japaneast": 0.2240,
    },
    "Standard_F1als_v7": {
        "eastus": 0.0095, "westus": 0.0095, "westus2": 0.0095, "westus3": 0.0095,
        "centralus": 0.0090, "northeurope": 0.0104, "westeurope": 0.0101,
        "swedencentral": 0.0099, "japaneast": 0.0098, "southeastasia": 0.0097,
        "australiaeast": 0.0104, "uksouth": 0.0101, "eastasia": 0.0098,
    },
}


def _fetch_retail_price(vm_size: str, region: str, price_type: str = "Consumption") -> float | None:
    """Query the Azure Retail Prices API for a VM price.

    Pass price_type='Spot' as a sentinel — translated to Consumption+skuName filter
    since the API does not accept priceType='Spot' directly.
    """
    want_spot = price_type == "Spot"
    api_price_type = "Consumption" if want_spot else price_type

    filter_str = (
        f"armRegionName eq '{region.lower()}'"
        f" and armSkuName eq '{vm_size}'"
        f" and priceType eq '{api_price_type}'"
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
        if want_spot:
            spot_items = [
                i for i in items
                if "spot" in (i.get("skuName") or "").lower()
                and "windows" not in (i.get("skuName") or "").lower()
                and "low priority" not in (i.get("skuName") or "").lower()
            ]
            for item in spot_items:
                price = item.get("retailPrice") or item.get("unitPrice")
                if price:
                    return float(price)
        else:
            for item in items:
                sku = (item.get("skuName") or "").lower()
                if "windows" in sku or "low priority" in sku or "spot" in sku:
                    continue
                price = item.get("retailPrice") or item.get("unitPrice")
                if price:
                    return float(price)
            if items:
                return float(items[0].get("retailPrice") or items[0].get("unitPrice") or 0) or None
    except Exception as exc:
        log.debug("Retail Prices API unavailable for %s/%s: %s", vm_size, region, exc)
    return None


def _fetch_spot_price(vm_size: str, region: str) -> float | None:
    cache_key = f"spot:{vm_size.lower()}:{region.lower()}"
    if cache_key in _SPOT_PRICE_CACHE:
        return _SPOT_PRICE_CACHE[cache_key]

    price = _fetch_retail_price(vm_size, region, price_type="Spot")
    if price is not None:
        _SPOT_PRICE_CACHE[cache_key] = price
        log.info("✓ Spot price %s (%s): $%.4f/hr (live API)", vm_size, region, price)
        return price

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
    cache_key = f"ondemand:{vm_size.lower()}:{region.lower()}"
    if cache_key in _SPOT_PRICE_CACHE:
        return _SPOT_PRICE_CACHE[cache_key]

    price = _fetch_retail_price(vm_size, region, price_type="Consumption")
    if price is not None:
        _SPOT_PRICE_CACHE[cache_key] = price
        log.info("✓ On-demand price %s (%s): $%.4f/hr (live API)", vm_size, region, price)
        return price

    region_norm = region.lower()
    if vm_size in _ONDEMAND_PRICES and region_norm in _ONDEMAND_PRICES[vm_size]:
        price = _ONDEMAND_PRICES[vm_size][region_norm]
        _SPOT_PRICE_CACHE[cache_key] = price
        log.info("✓ On-demand price %s (%s): $%.4f/hr (table)", vm_size, region_norm, price)
        return price

    log.warning("✗ No on-demand price found for %s in %s", vm_size, region)
    return None


# ---------------------------------------------------------------------------
# ARM resource inventory
# ---------------------------------------------------------------------------

def _fetch_resource_inventory() -> list[dict]:
    """Enumerate all ARM resources with VM power states."""
    rc = _get_resource_mgmt_client()
    all_res = list(rc.resources.list())
    log.info("ARM inventory: %d resources found", len(all_res))

    vm_resources = [
        r for r in all_res
        if r.id and (r.type or "").lower() == "microsoft.compute/virtualmachines"
    ]
    vmss_resources = [
        r for r in all_res
        if r.id and (r.type or "").lower() == "microsoft.compute/virtualmachinescalesets"
    ]
    vm_states: dict[str, str] = {}
    vm_meta: dict[str, dict] = {}

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
                        (s.display_status for s in statuses if s.code.startswith("PowerState/")),
                        "Unknown",
                    )
                    vm_states[vm_res.id.lower()] = power
                    vm_size = (inst.hardware_profile.vm_size or "") if inst.hardware_profile else ""
                    if not vm_size:
                        try:
                            arm_res = rc.resources.get_by_id(vm_res.id, api_version="2024-03-01")
                            props = arm_res.properties or {}
                            vm_size = (props.get("hardwareProfile") or {}).get("vmSize", "")
                        except Exception:
                            pass
                    is_spot = (getattr(inst, "priority", None) or "").lower() == "spot"
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

    if vmss_resources:
        try:
            cc = _get_compute_mgmt_client()
            for vmss_res in vmss_resources:
                parts = vmss_res.id.split("/resourceGroups/")
                rg_name = parts[1].split("/")[0] if len(parts) > 1 else ""
                try:
                    vmss = cc.virtual_machine_scale_sets.get(rg_name, vmss_res.name)
                    vm_size = (vmss.sku.name or "") if vmss.sku else ""
                    instance_count = int(vmss.sku.capacity or 1) if vmss.sku else 1
                    vmp = vmss.virtual_machine_profile
                    is_spot = (getattr(vmp, "priority", None) or "").lower() == "spot" if vmp else False
                    vm_meta[vmss_res.id.lower()] = {"vm_size": vm_size, "is_spot": is_spot, "private_ip": "", "instance_count": instance_count}
                    vm_states[vmss_res.id.lower()] = "VM running"
                    log.info("VMSS %s: size=%s spot=%s instances=%d", vmss_res.name, vm_size, is_spot, instance_count)
                except Exception as e:
                    log.debug("VMSS SKU error (%s): %s", vmss_res.name, e)
        except Exception as e:
            log.warning("Could not fetch VMSS SKU info: %s", e)

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
            "instance_count": meta.get("instance_count", 1),
            "private_ip": meta.get("private_ip", ""),
            "provisioned_size_gib": None,
            "parent_storage_account": None,
        })

    storage_accounts = [r for r in all_res if (r.type or "").lower() == "microsoft.storage/storageaccounts"]
    if storage_accounts and _HAS_STORAGE_MGMT:
        try:
            sc = _get_storage_mgmt_client()
            for sa in storage_accounts:
                sa_parts = sa.id.split("/resourceGroups/")
                sa_rg = sa_parts[1].split("/")[0] if len(sa_parts) > 1 else ""
                try:
                    shares = list(sc.file_shares.list(sa_rg, sa.name))
                    for share in shares:
                        quota_gib = share.share_quota or 0
                        price_per_gib = _fetch_files_premium_price(sa.location or "eastus")
                        monthly_est = round(quota_gib * price_per_gib, 2)
                        share_id = f"{sa.id}/fileServices/default/shares/{share.name}"
                        result.append({
                            "id": share_id,
                            "name": share.name or "",
                            "type": "Microsoft.Storage/storageAccounts/fileServices/shares",
                            "category": "storage",
                            "resource_group": sa_rg,
                            "location": sa.location or "",
                            "status": "Active",
                            "vm_size": "",
                            "is_spot": False,
                            "private_ip": "",
                            "provisioned_size_gib": quota_gib,
                            "parent_storage_account": sa.name or "",
                            "monthly_cost": monthly_est,
                            "cost_source": "files_rate",
                        })
                        log.info(
                            "File share: %s/%s  quota=%d GiB  est=$%.2f/mo",
                            sa.name, share.name, quota_gib, monthly_est,
                        )
                except Exception as e:
                    log.debug("File shares enum error for %s: %s", sa.name, e)
        except Exception as e:
            log.warning("StorageManagementClient error during file share enumeration: %s", e)

    return result


# ---------------------------------------------------------------------------
# Live resource cache
# ---------------------------------------------------------------------------

_live_lock: asyncio.Lock = asyncio.Lock()
_live_cache: dict[str, Any] = {}

_SUBRESOURCE_STRIP_RE = re.compile(
    r"(/providers/[^/]+/[^/]+/[^/]+)"
    r"(?:/(?:fileservices|blobservices|queueservices|tableservices|managementpolicies"
    r"|encryptionscopes|objectreplicationpolicies|privateendpointconnections"
    r"|inventorypolicies|shares|containers|queues|tables).*)$"
)


def _normalize_rid(rid: str) -> str:
    m = _SUBRESOURCE_STRIP_RE.search(rid)
    return rid[: m.end(1)] if m else rid


def list_resource_groups() -> list[str]:
    """Return all resource group names in the subscription via ARM."""
    rc = _get_resource_mgmt_client()
    return [rg.name for rg in rc.resource_groups.list() if rg.name]


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

    df = await get_cached_dataframe()
    cost_by_id: dict[str, float] = {}
    hours_by_id: dict[str, float] = {}
    days_by_id: dict[str, int] = {}
    window_days: int = 1
    last_day_cost_by_id: dict[str, float] = {}

    if not df.empty and "C_RESOURCE_ID" in df.columns and "C_COST" in df.columns:
        cutoff = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
        mdf = df[df["C_DATE"] >= cutoff] if "C_DATE" in df.columns else df
        mdf = mdf.copy()
        mdf["C_RESOURCE_ID"] = mdf["C_RESOURCE_ID"].str.lower()
        mdf["C_RESOURCE_ID"] = mdf["C_RESOURCE_ID"].apply(_normalize_rid)
        agg = mdf.groupby("C_RESOURCE_ID")["C_COST"].sum()
        cost_by_id = {str(k): round(float(v), 4) for k, v in agg.items() if v > 0}
        if "C_DATE" in mdf.columns:
            days_agg = mdf.groupby("C_RESOURCE_ID")["C_DATE"].nunique()
            days_by_id = {str(k): int(v) for k, v in days_agg.items()}
            window_days = int(mdf["C_DATE"].nunique()) or 1
            last_date = mdf["C_DATE"].max()
            if last_date:
                ld_agg = mdf[mdf["C_DATE"] == last_date].groupby("C_RESOURCE_ID")["C_COST"].sum()
                last_day_cost_by_id = {str(k): round(float(v), 4) for k, v in ld_agg.items() if v > 0}
        if "C_QUANTITY" in mdf.columns:
            qty_agg = mdf.groupby("C_RESOURCE_ID")["C_QUANTITY"].sum()
            hours_by_id = {str(k): float(v) for k, v in qty_agg.items() if v > 0}

    sa_with_live_shares: set[str] = {
        (r.get("parent_storage_account") or "").lower()
        for r in inv
        if (r.get("type") or "").lower() == "microsoft.storage/storageaccounts/fileservices/shares"
    }

    enriched: list[dict] = []
    for r in inv:
        rid = r["id"].lower()
        export_cost = cost_by_id.get(rid)
        export_days = days_by_id.get(rid, window_days) if export_cost is not None else window_days
        export_hours_val = hours_by_id.get(rid) if export_cost is not None else None
        is_storage_account = (r.get("type") or "").lower() == "microsoft.storage/storageaccounts"
        has_live_shares = is_storage_account and (r.get("name") or "").lower() in sa_with_live_shares

        if export_cost is not None and export_days < 28:
            if is_storage_account and not has_live_shares:
                monthly_projected = export_cost
                cost_source = "export_period"
            elif is_storage_account and has_live_shares:
                window_projected = round(export_cost / export_days * 30, 2)
                last_day_cost = last_day_cost_by_id.get(rid)
                last_day_projected = round(last_day_cost * 30, 2) if last_day_cost else 0
                if last_day_projected > window_projected:
                    monthly_projected = last_day_projected
                    cost_source = "export_last_day"
                else:
                    monthly_projected = window_projected
                    cost_source = "export_projected"
            elif export_hours_val and export_hours_val > 0 and r.get("category") == "vm":
                hourly_rate = export_cost / export_hours_val
                monthly_projected = round(hourly_rate * 24 * 30, 2)
                cost_source = "export_hours"
            else:
                monthly_projected = round(export_cost / export_days * 30, 2)
                cost_source = "export_projected"
        else:
            monthly_projected = export_cost
            cost_source = "export" if export_cost is not None else None

        if export_cost is None and monthly_projected is None:
            monthly_projected = r.get("monthly_cost")
            cost_source = r.get("cost_source")

        entry: dict = {
            **r,
            "monthly_cost": monthly_projected,
            "cost_source": cost_source,
            "export_cost_raw": export_cost,
            "export_days": export_days if export_cost is not None else None,
            "export_hours": round(export_hours_val, 1) if export_hours_val else None,
        }

        vm_is_active = (r.get("status") or "").lower() not in ("deallocated", "stopped", "vm deallocated", "vm stopped")
        if r.get("vm_size") and r.get("location") and vm_is_active:
            vm_size_norm = r["vm_size"]
            region_norm = r["location"].lower()
            instances = max(int(r.get("instance_count") or 1), 1)
            if r.get("is_spot"):
                spot_price = await asyncio.get_event_loop().run_in_executor(
                    None, _fetch_spot_price, vm_size_norm, region_norm
                )
                if spot_price is not None:
                    entry["monthly_cost"] = round(spot_price * 24 * 30 * instances, 2)
                    entry["spot_price_per_hour"] = round(spot_price, 4)
                    entry["cost_source"] = "spot_rate"
                else:
                    ondemand = await asyncio.get_event_loop().run_in_executor(
                        None, _fetch_ondemand_price, vm_size_norm, region_norm
                    )
                    if ondemand is not None:
                        spot_est = round(ondemand * 0.4, 4)
                        entry["monthly_cost"] = round(spot_est * 24 * 30 * instances, 2)
                        entry["spot_price_per_hour"] = spot_est
                        entry["cost_source"] = "spot_rate"
            else:
                ondemand = await asyncio.get_event_loop().run_in_executor(
                    None, _fetch_ondemand_price, vm_size_norm, region_norm
                )
                if ondemand is not None:
                    entry["monthly_cost"] = round(ondemand * 24 * 30 * instances, 2)
                    entry["cost_source"] = "price_table"

        enriched.append(entry)

    sa_with_shares: set[str] = {
        e["parent_storage_account"].lower()
        for e in enriched
        if e.get("cost_source") == "files_rate" and e.get("parent_storage_account")
    }
    for e in enriched:
        if (e.get("type") or "").lower() == "microsoft.storage/storageaccounts":
            if (e.get("name") or "").lower() in sa_with_shares:
                e["monthly_cost"] = None
                e["cost_source"] = "in_shares"

    return enriched
