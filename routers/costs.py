"""
routers/costs.py — All Azure cost data API endpoints for azure-penny.
"""

import asyncio
import calendar
import time
from datetime import date, timedelta

import pandas as pd
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from config import log
from cost_categories import (
    ALL_KNOWN_SERVICES,
    CAT_COMPUTE,
    CAT_DATABASE,
    CAT_MONITORING,
    CAT_NETWORK,
    CAT_STORAGE,
    _CAT_KEY_MAP,
    _ORDERED_CATS,
    _TRANSFER_KEYWORDS,
)
from cost_filters import (
    _bucket_key,
    _bucket_label,
    _build_snapshot,
    _category_api,
    _cost_by,
    _filter_app,
    _filter_period,
    _filter_rg,
    _filter_services,
    _other_category_api,
    _period_days,
)
from storage import _cache, _lock, get_cached_dataframe

router = APIRouter(tags=["api"])


# ── Category endpoints ────────────────────────────────────────────────────────


@router.get("/api/compute")
async def api_compute(
    period: str = "week", rg: str = "", app: str = ""
) -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_COMPUTE, rg, app))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/compute/machines")
async def api_compute_machines(
    period: str = "week", rg: str = "", app: str = ""
) -> JSONResponse:
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

        return JSONResponse(
            {
                "period": period,
                "source": "Cost Management / Blob Storage",
                "machines": [{"name": k, "cost_usd": v} for k, v in by_machine.items()],
                "total_usd": round(sum(by_machine.values()), 4),
                "data_as_of": data_as_of,
                "fallback": fallback,
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/storage")
async def api_storage(
    period: str = "week", rg: str = "", app: str = ""
) -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_STORAGE, rg, app))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/storage/breakdown")
async def api_storage_breakdown(
    period: str = "week", rg: str = "", app: str = ""
) -> JSONResponse:
    try:
        full_df = await get_cached_dataframe()
        df = _filter_app(_filter_rg(full_df, rg), app)
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
            return JSONResponse(
                {
                    "period": period,
                    "total_usd": 0,
                    "space_usd": 0,
                    "transfer_usd": 0,
                    "by_service": [],
                    "fallback": fallback,
                }
            )

        filtered = filtered.copy()
        has_sub = "C_SUBCATEGORY" in filtered.columns

        def _row_type(svc: str, sub: str) -> str:
            combined = (svc + " " + sub).lower()
            return (
                "transfer"
                if any(k in combined for k in _TRANSFER_KEYWORDS)
                else "space"
            )

        if has_sub:
            filtered["_type"] = filtered.apply(
                lambda r: _row_type(
                    str(r.get("C_SERVICE", "")), str(r.get("C_SUBCATEGORY", ""))
                ),
                axis=1,
            )
        else:
            filtered["_type"] = filtered["C_SERVICE"].apply(
                lambda s: _row_type(str(s), "")
            )

        space_total = float(filtered[filtered["_type"] == "space"]["C_COST"].sum())
        transfer_total = float(
            filtered[filtered["_type"] == "transfer"]["C_COST"].sum()
        )
        by_svc = _cost_by(filtered, "C_SERVICE")
        data_as_of = None
        if not filtered.empty and "C_DATE" in filtered.columns:
            data_as_of = filtered["C_DATE"].dropna().max()

        return JSONResponse(
            {
                "period": period,
                "total_usd": round(space_total + transfer_total, 4),
                "space_usd": round(space_total, 4),
                "transfer_usd": round(transfer_total, 4),
                "by_service": [
                    {"service": k, "cost_usd": v} for k, v in by_svc.items()
                ],
                "data_as_of": data_as_of,
                "fallback": fallback,
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/network")
async def api_network(
    period: str = "week", rg: str = "", app: str = ""
) -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_NETWORK, rg, app))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/database")
async def api_database(
    period: str = "week", rg: str = "", app: str = ""
) -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_DATABASE, rg, app))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/monitoring")
async def api_monitoring(
    period: str = "week", rg: str = "", app: str = ""
) -> JSONResponse:
    try:
        return JSONResponse(await _category_api(period, CAT_MONITORING, rg, app))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/other")
async def api_other(period: str = "week", rg: str = "", app: str = "") -> JSONResponse:
    """Services that don't fall into any standard category."""
    try:
        return JSONResponse(await _other_category_api(period, rg, app))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Application (project tag) endpoints ──────────────────────────────────────


@router.get("/api/apps")
async def api_apps(period: str = "week", rg: str = "") -> JSONResponse:
    """List distinct applications (project tags) with their total cost for the given period."""
    try:
        full_df = await get_cached_dataframe()
        df = _filter_rg(full_df, rg)
        days = _period_days(period)
        filtered = _filter_period(df, days)

        if (
            filtered.empty
            or "C_APP" not in filtered.columns
            or "C_COST" not in filtered.columns
        ):
            return JSONResponse({"apps": [], "period": period})

        by_app = (
            filtered.groupby("C_APP", dropna=False)["C_COST"]
            .sum()
            .sort_values(ascending=False)
        )
        apps = [
            {"name": str(k), "cost_usd": round(float(v), 4)} for k, v in by_app.items()
        ]
        return JSONResponse({"apps": apps, "period": period})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Raw costs (JSON) ──────────────────────────────────────────────────────────


@router.get("/costs", tags=["costs"])
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


@router.get("/costs/summary", tags=["costs"])
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

    return JSONResponse(
        {
            "total_cost": total,
            "top_services": top5("C_SERVICE"),
            "top_resource_groups": top5("C_NAME"),
        }
    )


@router.get("/costs/refresh", tags=["costs"])
async def refresh_cache() -> JSONResponse:
    async with _lock:
        _cache.clear()
    try:
        df = await get_cached_dataframe()
    except Exception as exc:
        log.exception("Reload failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse(
        {"status": "refreshed", "rows_loaded": len(df), "columns": list(df.columns)}
    )


# ── Technician / services ─────────────────────────────────────────────────────


@router.get("/api/services")
async def api_services(
    period: str = "week", rg: str = "", app: str = ""
) -> JSONResponse:
    try:
        full_df = await get_cached_dataframe()
        df = _filter_app(_filter_rg(full_df, rg), app)
        days = _period_days(period)
        filtered = _filter_period(df, days)

        fallback = False
        if (
            period == "day"
            and filtered.empty
            and "C_DATE" in full_df.columns
            and not df.empty
        ):
            last_date = full_df["C_DATE"].dropna().max()
            filtered = df[df["C_DATE"] == last_date]
            fallback = True

        by_svc = _cost_by(filtered, "C_SERVICE")
        data_as_of = None
        if not filtered.empty and "C_DATE" in filtered.columns:
            data_as_of = filtered["C_DATE"].dropna().max()

        return JSONResponse(
            {
                "period": period,
                "services": [{"service": k, "cost_usd": v} for k, v in by_svc.items()],
                "total_usd": round(sum(by_svc.values()), 4),
                "data_as_of": data_as_of,
                "fallback": fallback,
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/live-services")
async def api_live_services() -> JSONResponse:
    try:
        df = await get_cached_dataframe()
        if df.empty or "C_SERVICE" not in df.columns or "C_COST" not in df.columns:
            return JSONResponse({"services": [], "total_usd": 0})

        cutoff = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
        mdf = df[df["C_DATE"] >= cutoff] if "C_DATE" in df.columns else df

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

        return JSONResponse(
            {
                "services": services,
                "count": len(services),
                "total_usd": round(float(by_svc.sum()), 2),
            }
        )
    except Exception as exc:
        log.exception("Live services endpoint failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/cost-search")
async def api_cost_search(q: str) -> JSONResponse:
    try:
        df = await get_cached_dataframe()
        if df.empty or "C_NAME" not in df.columns:
            return JSONResponse({"results": [], "count": 0})

        mask = df["C_NAME"].str.contains(q, case=False, na=False)
        matches = (
            df[mask][["C_NAME", "C_RESOURCE_ID", "C_COST", "C_DATE"]]
            .drop_duplicates(subset=["C_RESOURCE_ID"])
            .to_dict("records")
        )

        return JSONResponse(
            {
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
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Resource groups ───────────────────────────────────────────────────────────


@router.get("/api/resource-groups")
async def api_resource_groups(
    period: str = "week", rg: str = "", app: str = ""
) -> JSONResponse:
    try:
        df = await get_cached_dataframe()
        df = _filter_app(_filter_rg(df, rg), app)
        days = _period_days(period)
        filtered = _filter_period(df, days)

        by_rg = _cost_by(filtered, "C_NAME")
        data_as_of = None
        if not filtered.empty and "C_DATE" in filtered.columns:
            data_as_of = filtered["C_DATE"].dropna().max()

        return JSONResponse(
            {
                "period": period,
                "resource_groups": [
                    {"name": k, "cost_usd": v} for k, v in by_rg.items()
                ],
                "total_usd": round(sum(by_rg.values()), 4),
                "data_as_of": data_as_of,
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/resource-groups-list")
async def api_resource_groups_list(app_filter: str = "") -> JSONResponse:
    """List resource groups, optionally pre-filtered by app tag."""
    from live_resources import list_resource_groups  # noqa: PLC0415

    try:
        df = await get_cached_dataframe()

        if app_filter:
            filtered = _filter_app(df, app_filter)
            if (
                not filtered.empty
                and "C_NAME" in filtered.columns
                and "C_COST" in filtered.columns
            ):
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

        # No app filter — billing data merged with ARM list.
        arm_rg_names = await _get_arm_rg_names()

        if not df.empty and "C_NAME" in df.columns and "C_COST" in df.columns:
            by_rg = df.groupby("C_NAME", dropna=False)["C_COST"].sum()
            cost_rgs = {
                str(k): round(float(v), 2) for k, v in by_rg.items() if str(k).strip()
            }
            rgs = [
                {"name": n, "total_cost": c, "in_arm": n in arm_rg_names}
                for n, c in cost_rgs.items()
            ]
            for name in arm_rg_names:
                if name not in cost_rgs:
                    rgs.append({"name": name, "total_cost": None, "in_arm": True})
            rgs.sort(key=lambda r: (r["total_cost"] is None, -(r["total_cost"] or 0)))
            return JSONResponse({"resource_groups": rgs})

        names = await asyncio.get_event_loop().run_in_executor(
            None, list_resource_groups
        )
        rgs = [
            {"name": n, "total_cost": None, "in_arm": True}
            for n in sorted(names)
            if n.strip()
        ]
        return JSONResponse({"resource_groups": rgs, "source": "arm"})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── ARM RG names cache (used by resource-groups-list) ────────────────────────

_rg_names_cache: dict = {}
_RG_NAMES_TTL = 900  # 15 min


async def _get_arm_rg_names() -> set[str]:
    """Return ARM resource group names, cached for 15 min."""
    from live_resources import _live_cache, _live_lock, list_resource_groups  # noqa: PLC0415

    now = time.monotonic()
    if (
        "names" in _rg_names_cache
        and (now - _rg_names_cache.get("ts", 0)) < _RG_NAMES_TTL
    ):
        return _rg_names_cache["names"]
    async with _live_lock:
        inv = _live_cache.get("inv")
    if inv is not None:
        names = {r.get("resource_group", "") for r in inv if r.get("resource_group")}
    else:
        names = set(
            await asyncio.get_event_loop().run_in_executor(None, list_resource_groups)
        )
    _rg_names_cache["names"] = names
    _rg_names_cache["ts"] = now
    return names


# ── Anomalies ────────────────────────────────────────────────────────────────


@router.get("/api/anomalies")
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
        prev_df = (
            df[(df["C_DATE"] >= prev_start) & (df["C_DATE"] < curr_start)]
            if has_date
            else pd.DataFrame(columns=df.columns)
        )

        def _week_deltas(key_col: str) -> tuple[list[dict], dict[str, dict]]:
            if key_col not in df.columns or "C_COST" not in df.columns:
                return [], {}
            c = (
                curr_df.groupby(key_col)["C_COST"].sum()
                if not curr_df.empty
                else pd.Series(dtype=float)
            )
            p = (
                prev_df.groupby(key_col)["C_COST"].sum()
                if not prev_df.empty
                else pd.Series(dtype=float)
            )
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
        rg_rows, rg_index = _week_deltas("C_NAME")

        spikes = sorted(
            [s for s in svc_rows if s["delta_pct"] is not None and s["delta_pct"] > 50],
            key=lambda x: x["delta_pct"],
            reverse=True,
        )
        new_svcs = sorted(
            [s for s in svc_rows if s["is_new"] and s["current_week_usd"] > 1],
            key=lambda x: x["current_week_usd"],
            reverse=True,
        )
        gone_svcs = sorted(
            [s for s in svc_rows if s["vanished"] and s["prior_week_usd"] > 1],
            key=lambda x: x["prior_week_usd"],
            reverse=True,
        )
        rg_spikes = sorted(
            [r for r in rg_rows if r["delta_pct"] is not None and r["delta_pct"] > 50],
            key=lambda x: x["delta_pct"],
            reverse=True,
        )

        untagged: list[dict] = []
        untagged_cost = 0.0
        if (
            "C_TAGS" in df.columns
            and "C_RESOURCE_ID" in df.columns
            and "C_COST" in df.columns
        ):
            mask = df["C_TAGS"].isna() | df["C_TAGS"].astype(str).str.strip().isin(
                ["", "{}", "nan", "None"]
            )
            utdf = df[mask]
            if not utdf.empty:
                agg = (
                    utdf.groupby("C_RESOURCE_ID")["C_COST"]
                    .sum()
                    .sort_values(ascending=False)
                )
                untagged = [
                    {"resource_id": str(rid), "cost_usd": round(float(c), 4)}
                    for rid, c in agg.items()
                    if c > 0
                ][:50]
                untagged_cost = round(sum(u["cost_usd"] for u in untagged), 4)

        return JSONResponse(
            {
                "spikes": spikes,
                "new_services": new_svcs,
                "vanished_services": gone_svcs,
                "rg_spikes": rg_spikes,
                "service_deltas": svc_index,
                "rg_deltas": rg_index,
                "untagged_cost_usd": untagged_cost,
                "untagged_resources": untagged,
                "total_untagged_resources": len(untagged),
            }
        )
    except Exception as exc:
        log.exception("Anomalies endpoint failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Year overview ─────────────────────────────────────────────────────────────


@router.get("/api/year")
async def api_year(rg: str = "", app: str = "") -> JSONResponse:
    try:
        full_df = await get_cached_dataframe()
        df = _filter_app(_filter_rg(full_df, rg), app)

        if df.empty or "C_DATE" not in df.columns or "C_COST" not in df.columns:
            return JSONResponse(
                {
                    "months": [],
                    "projected_months": [],
                    "ytd_usd": 0,
                    "monthly_avg_usd": 0,
                }
            )

        from live_resources import _get_live_data  # noqa: PLC0415

        today = date.today()
        cutoff = date(today.year - 1, today.month, 1).strftime("%Y-%m-%d")
        df = df[df["C_DATE"] >= cutoff].copy()

        df["_month"] = df["C_DATE"].str[:7]
        monthly = df.groupby("_month")["C_COST"].sum().sort_index()
        months = [
            {"month": m, "cost_usd": round(float(v), 2)} for m, v in monthly.items()
        ]

        ytd_prefix = today.strftime("%Y")
        ytd_usd = round(
            float(df[df["C_DATE"].str.startswith(ytd_prefix)]["C_COST"].sum()), 2
        )
        monthly_avg = round(float(monthly.mean()), 2) if len(monthly) > 0 else 0.0

        daily_rate = 0.0
        try:
            all_resources = await _get_live_data()
            live_monthly = sum(
                (r.get("monthly_cost") or 0.0)
                for r in all_resources
                if (r.get("monthly_cost") or 0.0) > 0
            )
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
            m, y = today.month + 1, today.year
            while m <= 12:
                days_in = calendar.monthrange(y, m)[1]
                projected_months.append(
                    {
                        "month": f"{y}-{m:02d}",
                        "cost_usd": round(daily_rate * days_in, 2),
                    }
                )
                m += 1

        return JSONResponse(
            {
                "months": months,
                "projected_months": projected_months,
                "ytd_usd": ytd_usd,
                "monthly_avg_usd": monthly_avg,
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Breakdown chart ───────────────────────────────────────────────────────────


@router.get("/api/breakdown")
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
                    result_buckets.setdefault(str(bucket), {})[
                        cat_name.capitalize()
                    ] = round(float(cost), 4)
            stack_keys = list(_ORDERED_CATS)
        else:
            cat_services = _CAT_KEY_MAP.get(category.lower(), set())
            cat_df = _get_cat_df(cat_services)
            for bk in df["_bucket"].astype(str).unique():
                result_buckets.setdefault(str(bk), {})
            if not cat_df.empty and "C_SERVICE" in cat_df.columns:
                grp2 = cat_df.groupby(["_bucket", "C_SERVICE"])["C_COST"].sum()
                for (bucket, svc), cost in grp2.items():
                    result_buckets.setdefault(str(bucket), {})[str(svc)] = round(
                        float(cost), 4
                    )
            svc_totals: dict[str, float] = {}
            for bdata in result_buckets.values():
                for svc, cost in bdata.items():
                    svc_totals[svc] = svc_totals.get(svc, 0) + cost
            stack_keys = sorted(svc_totals, key=lambda x: svc_totals[x], reverse=True)[
                :8
            ]

        buckets = [
            {
                "key": bk,
                "label": _bucket_label(bk, granularity),
                **{k: result_buckets[bk].get(k, 0) for k in stack_keys},
            }
            for bk in sorted(result_buckets)
        ]

        return JSONResponse({"buckets": buckets, "stack_keys": stack_keys})

    except Exception as exc:
        log.exception("Breakdown endpoint failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Daily ─────────────────────────────────────────────────────────────────────


@router.get("/api/daily")
async def api_daily(days: int = 30, rg: str = "", app: str = "") -> JSONResponse:
    try:
        df = await get_cached_dataframe()
        df = _filter_app(_filter_rg(df, rg), app)
        if "C_DATE" not in df.columns or "C_COST" not in df.columns:
            return JSONResponse({"days": days, "points": [], "total_usd": 0})

        cutoff = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
        filtered = df[df["C_DATE"] >= cutoff]
        daily = filtered.groupby("C_DATE")["C_COST"].sum().sort_index()
        points = [
            {"date": str(d), "cost_usd": round(float(v), 6)} for d, v in daily.items()
        ]

        return JSONResponse(
            {
                "days": days,
                "points": points,
                "total_usd": round(float(daily.sum()), 4),
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Monthly Forecast ─────────────────────────────────────────────────────────


@router.get("/api/forecast")
async def api_forecast(rg: str = "", app: str = "") -> JSONResponse:
    """Monthly forecast: actual cumulative daily costs + two projections."""
    try:
        from live_resources import _get_live_data  # noqa: PLC0415

        today = date.today()
        month_start = today.replace(day=1)
        last_day = calendar.monthrange(today.year, today.month)[1]
        end_of_month = today.replace(day=last_day)
        days_elapsed = (today - month_start).days + 1
        days_remaining = (end_of_month - today).days
        days_in_month = last_day

        full_df = await get_cached_dataframe()
        df = _filter_app(_filter_rg(full_df, rg), app)

        actual_points: list[dict] = []
        today_total = 0.0
        data_days_elapsed = days_elapsed
        daily = None

        if "C_DATE" in df.columns and "C_COST" in df.columns and not df.empty:
            df = df.copy()
            df["_date"] = pd.to_datetime(df["C_DATE"]).dt.date
            month_df = df[(df["_date"] >= month_start) & (df["_date"] <= today)]
            daily = month_df.groupby("_date")["C_COST"].sum().sort_index()
            cumulative = 0.0
            for d, cost in daily.items():
                cumulative += float(cost)
                actual_points.append(
                    {"date": str(d), "cumulative": round(cumulative, 4)}
                )
            today_total = cumulative

        # Cost Management exports regenerate intra-day — today's data is partial.
        # Use only complete days (up to yesterday) for avg rate.
        spent_complete = today_total
        if daily is not None and not daily.empty:
            complete_daily = daily[daily.index < today]
            if not complete_daily.empty:
                data_days_elapsed = max(
                    (complete_daily.index[-1] - complete_daily.index[0]).days + 1, 1
                )
                spent_complete = float(complete_daily.sum())
        avg_daily_rate = spent_complete / max(data_days_elapsed, 1)
        avg_end = round(today_total + avg_daily_rate * days_remaining, 2)

        live_resources = await _get_live_data()
        if rg:
            live_resources = [
                r
                for r in live_resources
                if (r.get("resource_group") or "").lower() == rg.lower()
            ]
        if app:
            live_resources = [
                r for r in live_resources if (r.get("app") or "").lower() == app.lower()
            ]
        live_monthly_total = sum(r.get("monthly_cost") or 0.0 for r in live_resources)
        live_daily_rate = live_monthly_total / days_in_month
        live_end = round(today_total + live_daily_rate * days_remaining, 2)

        return JSONResponse(
            {
                "actual": actual_points,
                "today": str(today),
                "end_of_month": str(end_of_month),
                "today_total": round(today_total, 2),
                "days_elapsed": days_elapsed,
                "data_days_elapsed": data_days_elapsed,
                "days_remaining": days_remaining,
                "days_in_month": days_in_month,
                "avg_daily_rate": round(avg_daily_rate, 4),
                "avg_end": avg_end,
                "live_daily_rate": round(live_daily_rate, 4),
                "live_end": live_end,
                "live_monthly_total": round(live_monthly_total, 2),
            }
        )
    except Exception as exc:
        log.exception("Forecast endpoint failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Snapshot ─────────────────────────────────────────────────────────────────


@router.get("/api/snapshot")
async def api_snapshot(
    period: str = "week", rg: str = "", app: str = ""
) -> JSONResponse:
    """Comprehensive cost snapshot — structured data + markdown optimised for LLM consumption."""
    try:
        return JSONResponse(await _build_snapshot(period, rg, app))
    except Exception as exc:
        log.exception("Snapshot endpoint failed")
        return JSONResponse({"error": str(exc)}, status_code=500)
