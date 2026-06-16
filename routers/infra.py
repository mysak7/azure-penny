"""
routers/infra.py — Infrastructure mutation (DELETE) endpoints for azure-penny.

All destructive endpoints require the penny-admin role (enforced via _require_admin
FastAPI dependency).
"""

import asyncio

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import StreamingResponse

from auth import _require_admin
from config import PROTECTED_RGS, log

router = APIRouter(tags=["infrastructure"])


# ---------------------------------------------------------------------------
# API version lookup helper
# ---------------------------------------------------------------------------


def _get_api_version(client, namespace: str, rtype: str) -> str:
    FALLBACKS: dict[str, str] = {
        "microsoft.compute": "2024-03-01",
        "microsoft.storage": "2023-05-01",
        "microsoft.network": "2024-01-01",
        "microsoft.sql": "2024-01-01",
        "microsoft.documentdb": "2023-11-15",
        "microsoft.app": "2024-03-01",
        "microsoft.containerregistry": "2023-07-01",
        "microsoft.operationalinsights": "2022-10-01",
        "microsoft.insights": "2022-06-15",
        "microsoft.keyvault": "2023-07-01",
        "microsoft.web": "2023-12-01",
        "microsoft.cache": "2023-08-01",
        "microsoft.dbforpostgresql": "2024-03-01-preview",
        "microsoft.dbformysql": "2023-12-30",
    }
    try:
        provider = client.providers.get(namespace)
        for rt in provider.resource_types or []:
            if rt.resource_type and rt.resource_type.lower() == rtype.lower():
                stable = [
                    v for v in (rt.api_versions or []) if "preview" not in v.lower()
                ]
                return (
                    stable[0]
                    if stable
                    else (
                        rt.api_versions
                        or [FALLBACKS.get(namespace.lower(), "2024-01-01")]
                    )[0]
                )
    except Exception:
        pass
    return FALLBACKS.get(namespace.lower(), "2024-01-01")


# ---------------------------------------------------------------------------
# Delete single resource
# ---------------------------------------------------------------------------


@router.delete("/api/resource")
async def api_delete_resource(
    resource_id: str,
    _: None = Depends(_require_admin),
) -> StreamingResponse:
    """Delete a live Azure resource by its full ARM resource ID."""
    if not resource_id.lower().startswith("/subscriptions/"):
        raise HTTPException(
            status_code=400, detail="resource_id must be a full ARM resource ID"
        )

    _parts = [p.lower() for p in resource_id.strip("/").split("/")]
    try:
        _rg = _parts[_parts.index("resourcegroups") + 1]
        if _rg in PROTECTED_RGS:
            raise HTTPException(
                status_code=403, detail=f"Resource group '{_rg}' is protected."
            )
    except (ValueError, IndexError):
        pass

    log.warning("⚠️  DELETE resource initiated: %s", resource_id)

    from live_resources import _get_resource_mgmt_client  # noqa: PLC0415

    async def log_streamer():
        try:
            yield f"[INFO] Resource: {resource_id}\n"

            parts = resource_id.strip("/").split("/")
            try:
                prov_idx = [p.lower() for p in parts].index("providers")
                namespace = parts[prov_idx + 1]
                rtype = parts[prov_idx + 2]
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
                None,
                lambda: client.resources.begin_delete_by_id(resource_id, api_version),
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


# ---------------------------------------------------------------------------
# Delete single resource group
# ---------------------------------------------------------------------------


@router.delete("/api/resource-group")
async def api_delete_resource_group(
    resource_group_name: str,
    _: None = Depends(_require_admin),
) -> StreamingResponse:
    """Delete all resources in an Azure resource group by deleting the group itself."""
    if not resource_group_name or not resource_group_name.strip():
        raise HTTPException(status_code=400, detail="resource_group_name is required")

    rg = resource_group_name.strip()
    if rg.lower() in PROTECTED_RGS:
        raise HTTPException(
            status_code=403, detail=f"Resource group '{rg}' is protected."
        )

    log.warning("⚠️  DELETE resource group initiated: %s", rg)

    from live_resources import _get_resource_mgmt_client  # noqa: PLC0415

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


# ---------------------------------------------------------------------------
# Bulk delete resource groups
# ---------------------------------------------------------------------------


@router.delete("/api/resource-groups/all")
async def api_delete_all_resource_groups(
    resource_groups: list[str] = Body(...),
    _: None = Depends(_require_admin),
) -> StreamingResponse:
    """Delete multiple Azure resource groups sequentially."""
    if not resource_groups:
        raise HTTPException(status_code=400, detail="resource_groups list is empty")

    blocked = [r for r in resource_groups if r.strip().lower() in PROTECTED_RGS]
    if blocked:
        raise HTTPException(
            status_code=403,
            detail=f"Protected resource groups cannot be deleted: {blocked}",
        )

    log.warning("⚠️  BULK DELETE resource groups initiated: %s", resource_groups)

    from live_resources import _get_resource_mgmt_client  # noqa: PLC0415

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
