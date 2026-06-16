"""
cost_filters.py — Filtering, aggregation, and snapshot helpers for azure-penny.

All pure data-manipulation functions that work on the cached Pandas DataFrame.
"""

from datetime import date, timedelta

import pandas as pd

from cost_categories import (
    ALL_KNOWN_SERVICES,
    _CAT_KEY_MAP,
)
from storage import get_cached_dataframe

# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------


def _period_days(period: str) -> int:
    return {"day": 1, "week": 7, "month": 30, "year": 365}.get(period, 7)


# ---------------------------------------------------------------------------
# DataFrame filter helpers
# ---------------------------------------------------------------------------


def _filter_period(df: pd.DataFrame, days: int) -> pd.DataFrame:
    if "C_DATE" not in df.columns:
        return df
    cutoff = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    today_str = date.today().strftime("%Y-%m-%d")
    return df[(df["C_DATE"] >= cutoff) & (df["C_DATE"] < today_str)]


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


# ---------------------------------------------------------------------------
# Category API helper
# ---------------------------------------------------------------------------


async def _category_api(
    period: str, services: set[str], rg: str = "", app: str = ""
) -> dict:
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


# ---------------------------------------------------------------------------
# Breakdown bucket helpers
# ---------------------------------------------------------------------------


def _bucket_key(date_str: str, granularity: str) -> str:
    """Convert a YYYY-MM-DD date string to a time-bucket key."""
    try:
        d = date.fromisoformat(str(date_str))
    except (ValueError, TypeError):
        return str(date_str)
    if granularity == "weeks":
        return d.strftime("%G-W%V")
    if granularity == "months":
        return str(date_str)[:7]
    return str(date_str)


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
    try:
        d = date.fromisoformat(key)
        return f"{d.day}.{d.month}"
    except Exception:
        return key


# ---------------------------------------------------------------------------
# Snapshot builder (used by GET /api/snapshot and AI chat tool)
# ---------------------------------------------------------------------------


async def _build_snapshot(period: str, rg: str, app: str) -> dict:
    """Assemble a comprehensive cost snapshot dict (data + markdown).

    Used by both GET /api/snapshot and the get_page_snapshot chat tool so
    the two always return the same information.
    """
    # Import here to avoid circular dependency at module load time.

    full_df = await get_cached_dataframe()
    df = _filter_app(_filter_rg(full_df, rg), app)
    days = _period_days(period)
    filtered = _filter_period(df, days)

    today = date.today()

    total = (
        round(float(filtered["C_COST"].sum()), 2)
        if "C_COST" in filtered.columns
        else 0.0
    )

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
        cat_total = (
            round(float(cat_df["C_COST"].sum()), 2)
            if ("C_COST" in cat_df.columns and not cat_df.empty)
            else 0.0
        )
        svc_count = (
            int(cat_df["C_SERVICE"].nunique())
            if ("C_SERVICE" in cat_df.columns and not cat_df.empty)
            else 0
        )
        if cat_total > 0:
            categories.append(
                {
                    "category": cat_name.capitalize(),
                    "total_usd": cat_total,
                    "service_count": svc_count,
                }
            )
    categories.sort(key=lambda x: x["total_usd"], reverse=True)

    # ── Top services / RGs / apps ─────────────────────────────────────────────
    top_services = [
        {"service": k, "cost_usd": v}
        for k, v in list(_cost_by(filtered, "C_SERVICE").items())[:10]
    ]
    top_rgs = [
        {"name": k, "cost_usd": v}
        for k, v in list(_cost_by(filtered, "C_NAME").items())[:10]
    ]

    top_apps: list[dict] = []
    if "C_APP" in filtered.columns and "C_COST" in filtered.columns:
        by_app = (
            filtered.groupby("C_APP", dropna=False)["C_COST"]
            .sum()
            .sort_values(ascending=False)
        )
        top_apps = [
            {"app": str(k), "cost_usd": round(float(v), 2)}
            for k, v in by_app.items()
            if v > 0
        ][:10]

    # ── Week-over-week anomalies ───────────────────────────────────────────────
    spikes, new_svcs, gone_svcs = [], [], []
    if "C_DATE" in df.columns and "C_SERVICE" in df.columns and "C_COST" in df.columns:
        curr_start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        prev_start = (today - timedelta(days=14)).strftime("%Y-%m-%d")
        curr_df = df[df["C_DATE"] >= curr_start]
        prev_df = df[(df["C_DATE"] >= prev_start) & (df["C_DATE"] < curr_start)]
        c = (
            curr_df.groupby("C_SERVICE")["C_COST"].sum()
            if not curr_df.empty
            else pd.Series(dtype=float)
        )
        p = (
            prev_df.groupby("C_SERVICE")["C_COST"].sum()
            if not prev_df.empty
            else pd.Series(dtype=float)
        )
        for svc in set(c.index) | set(p.index):
            cv, pv = float(c.get(svc, 0)), float(p.get(svc, 0))
            if cv == 0 and pv == 0:
                continue
            dp = round((cv - pv) / pv * 100, 1) if pv > 0 else None
            entry = {
                "service": str(svc),
                "current_week_usd": round(cv, 2),
                "prior_week_usd": round(pv, 2),
                "delta_pct": dp,
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
    if rg:
        filter_parts.append(f"RG: {rg}")
    if app:
        filter_parts.append(f"App: {app}")
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
            md.append(
                f"- {c_item['category']}: ${c_item['total_usd']:.2f} ({n} service{'s' if n != 1 else ''})"
            )
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
            md.append(
                f"⚠️  {s['service']}: +{s['delta_pct']}% (${s['prior_week_usd']:.2f} → ${s['current_week_usd']:.2f})"
            )
        for s in new_svcs[:3]:
            md.append(f"🆕 New: {s['service']} (${s['current_week_usd']:.2f})")
        for s in gone_svcs[:3]:
            md.append(f"👻 Gone: {s['service']} (was ${s['prior_week_usd']:.2f})")
        md.append("")

    return {
        "period": period,
        "filters": {"rg": rg, "app": app},
        "generated_at": str(today),
        "summary": {"total_usd": total, "date_range": [date_range_start, data_as_of]},
        "by_category": categories,
        "top_services": top_services,
        "top_resource_groups": top_rgs,
        "top_apps": top_apps,
        "anomalies": {
            "spikes": spikes,
            "new_services": new_svcs,
            "vanished_services": gone_svcs,
        },
        "markdown": "\n".join(md),
    }
