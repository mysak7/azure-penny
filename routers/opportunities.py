"""
routers/opportunities.py — savings-opportunity endpoints for azure-penny.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import opportunities as engine
from config import log

router = APIRouter(tags=["opportunities"])


@router.get("/api/opportunities")
async def api_opportunities(
    account: str = "", signal: str = "", status: str = "", refresh: bool = False
) -> JSONResponse:
    """Ranked savings opportunities (open first, by monthly impact)."""
    try:
        result = await engine.get_opportunities(refresh=refresh)
        opps = result["opportunities"]
        if account:
            opps = [
                o
                for o in opps
                if o["account_id"] == account or o["account_name"] == account
            ]
        if signal:
            opps = [o for o in opps if o["signal"] == signal]
        if status:
            opps = [o for o in opps if o["status"] == status]
        return JSONResponse({"opportunities": opps, "summary": result["summary"]})
    except Exception as exc:
        log.exception("Opportunities endpoint failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/accounts")
async def api_accounts(period: str = "month") -> JSONResponse:
    """Distinct subscriptions with cost over the period — feeds the Sub filter."""
    from cost_filters import _filter_period, _period_days
    from storage import get_cached_dataframe

    try:
        df = await get_cached_dataframe()
        if df.empty or "C_ACCOUNT" not in df.columns:
            return JSONResponse({"accounts": []})
        filtered = _filter_period(df, _period_days(period))
        if filtered.empty:
            filtered = df
        grp = filtered.groupby("C_ACCOUNT")["C_COST"].sum()
        accounts = [
            {"id": str(acct), "name": str(acct), "cost_usd": round(float(cost), 2)}
            for acct, cost in grp.items()
            if cost > 0
        ]
        accounts.sort(key=lambda a: -a["cost_usd"])
        return JSONResponse({"accounts": accounts, "count": len(accounts)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


class StatusUpdate(BaseModel):
    status: str
    note: str = ""


@router.post("/api/opportunities/{opp_id:path}/status")
async def api_opportunity_status(opp_id: str, body: StatusUpdate) -> JSONResponse:
    """Move an opportunity through its lifecycle (new → in_progress → resolved/dismissed)."""
    if body.status not in engine.VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of {sorted(engine.VALID_STATUSES)}",
        )
    try:
        result = await engine.get_opportunities()
        snapshot = next((o for o in result["opportunities"] if o["id"] == opp_id), None)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="unknown opportunity id")
        entry = await engine.update_status(
            opp_id, body.status, snapshot=snapshot, note=body.note
        )
        refreshed = await engine.get_opportunities(refresh=True)
        return JSONResponse(
            {"id": opp_id, "entry": entry, "summary": refreshed["summary"]}
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Opportunity status update failed")
        return JSONResponse({"error": str(exc)}, status_code=500)
