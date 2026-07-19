"""
opportunities.py — FinOps savings signals and opportunity lifecycle.

Five signals computed from the Cost Management export DataFrame and the live
inventory (Azure port of the aws-penny Technician 3.0 opportunity engine):

  risp       — Reservation / Savings Plan coverage per subscription
  idle       — idle or unattached resources (live CPU / disk state × costs)
  tags       — unallocated (untagged) spend per subscription
  offhours   — non-prod resource groups running nights and weekends
  benchmark  — dev resource groups compared on compute run-hours per day

Each finding is an *opportunity* with a lifecycle (new → in_progress →
resolved/dismissed). State is persisted as JSON in the cost-export blob
container (opportunity_state.json).

For resolved opportunities the engine verifies the outcome: it compares spend
in the scope 28 days before the resolution date against the latest 28 days
and reports the difference as realized monthly savings.

Amounts follow the repo-wide `_usd`-suffixed field convention; values are in
the billing currency (EUR) like every other azure-penny endpoint.
"""

import asyncio
import time
from datetime import date, timedelta
from typing import Any

import pandas as pd

from config import log

RESERVATION_DISCOUNT_EST = 0.30  # typical 1-yr reservation discount vs PAYG
COVERAGE_TARGET = 0.60  # flag subscriptions below this reservation/SP coverage
IDLE_CPU_PCT = 5.0
TAGS_PCT_MIN = 0.15
TAGS_EUR_MIN = 300.0
WEEKEND_RATIO_MIN = 0.6
BENCH_HOURS_FACTOR = 1.4

OPPORTUNITY_STATE_KEY = "opportunity_state.json"

VALID_STATUSES = {"new", "in_progress", "resolved", "dismissed"}

# Fallback compute filter when the export lacks a ServiceFamily column
_COMPUTE_SERVICES = r"Virtual Machines|Container|Kubernetes|App Service|Functions"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _last_n_days(df: pd.DataFrame, n: int, end: str | None = None) -> pd.DataFrame:
    if df.empty or "C_DATE" not in df.columns:
        return df
    end_day = end or str(df["C_DATE"].dropna().max())
    start_day = (date.fromisoformat(end_day) - timedelta(days=n - 1)).isoformat()
    return df[(df["C_DATE"] >= start_day) & (df["C_DATE"] <= end_day)]


def _subscription_names(df: pd.DataFrame) -> dict[str, str]:
    """C_ACCOUNT already holds the subscription display name on Azure."""
    if "C_ACCOUNT" not in df.columns:
        return {}
    return {str(a): str(a) for a in df["C_ACCOUNT"].dropna().unique()}


def _is_nonprod(name: str) -> bool:
    n = name.lower()
    return any(t in n for t in ("dev", "test", "stage", "staging", "sandbox", "qa"))


def _compute_rows(df: pd.DataFrame) -> pd.DataFrame:
    if "C_FAMILY" in df.columns:
        return df[df["C_FAMILY"] == "Compute"]
    if "C_SERVICE" not in df.columns:
        return df.iloc[0:0]
    return df[df["C_SERVICE"].str.contains(_COMPUTE_SERVICES, na=False, regex=True)]


def _monthly(cost_30d: float, days: int = 30) -> float:
    return round(cost_30d / max(days, 1) * 30, 2)


def _resource_monthly_cost(df30: pd.DataFrame, resource_id: str) -> float:
    if df30.empty or "C_RESOURCE_ID" not in df30.columns:
        return 0.0
    rows = df30[df30["C_RESOURCE_ID"].str.lower() == resource_id.lower()]
    if rows.empty:
        return 0.0
    ndays = rows["C_DATE"].nunique() if "C_DATE" in rows.columns else 30
    return _monthly(float(rows["C_COST"].sum()), ndays)


def _dominant_account(grp: pd.DataFrame) -> str:
    if "C_ACCOUNT" not in grp.columns or grp["C_ACCOUNT"].dropna().empty:
        return ""
    return str(grp["C_ACCOUNT"].mode().iloc[0])


def _opp(
    signal: str,
    key: str,
    title: str,
    detail: str,
    account_id: str,
    account_name: str,
    impact: float,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": f"{signal}:{key}",
        "signal": signal,
        "title": title,
        "detail": detail,
        "account_id": account_id,
        "account_name": account_name,
        "monthly_impact_usd": round(impact, 2),
        "evidence": evidence,
        "status": "new",
    }


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def _signal_risp(df30: pd.DataFrame, names: dict[str, str]) -> list[dict]:
    """Reservation / Savings Plan coverage per subscription over compute usage.

    Spot rows are excluded entirely — spot capacity needs no commitment and is
    already discounted.
    """
    out: list[dict] = []
    rows = _compute_rows(df30)
    if rows.empty or "C_PRICING" not in rows.columns or "C_ACCOUNT" not in rows.columns:
        return out
    if "C_TYPE" in rows.columns:
        rows = rows[rows["C_TYPE"] == "Usage"]
    rows = rows[rows["C_PRICING"] != "Spot"]
    if rows.empty:
        return out
    ndays = rows["C_DATE"].nunique() if "C_DATE" in rows.columns else 30
    for acct, grp in rows.groupby("C_ACCOUNT"):
        total = float(grp["C_COST"].sum())
        covered = float(
            grp.loc[
                grp["C_PRICING"].isin(["Reservation", "SavingsPlan"]), "C_COST"
            ].sum()
        )
        if total <= 0:
            continue
        coverage = covered / total
        uncovered_monthly = _monthly(total - covered, ndays)
        if coverage >= COVERAGE_TARGET or uncovered_monthly < 500:
            continue
        name = names.get(str(acct), str(acct))
        impact = uncovered_monthly * RESERVATION_DISCOUNT_EST
        out.append(
            _opp(
                "risp",
                str(acct),
                f"Low reservation coverage in {name} ({coverage:.0%})",
                f"€{uncovered_monthly:,.0f}/mo of compute runs pay-as-you-go. "
                f"A reservation or savings plan at ~{RESERVATION_DISCOUNT_EST:.0%} "
                f"discount saves ~€{impact:,.0f}/mo at current usage.",
                str(acct),
                name,
                impact,
                {
                    "coverage_pct": round(coverage * 100, 1),
                    "covered_monthly_usd": _monthly(covered, ndays),
                    "uncovered_monthly_usd": uncovered_monthly,
                },
            )
        )
    return out


def _signal_idle(
    df30: pd.DataFrame, live: list[dict], names: dict[str, str]
) -> list[dict]:
    """Idle (low-CPU) and unattached resources.

    `live` entries may carry `cpu_avg` (filled by `_enrich_vm_cpu`) and
    `status: "unattached"` (unattached managed disks / public IPs appended by
    `_unattached_resources`).
    """
    out: list[dict] = []
    default_account = next(iter(names.values()), "")

    for r in live:
        rid = r.get("id") or ""
        if not rid:
            continue
        cost = r.get("monthly_cost")
        if cost is None:
            cost = _resource_monthly_cost(df30, rid)
        rg = r.get("resource_group", "")
        acct = r.get("account_id") or default_account
        cpu = r.get("cpu_avg")
        rtype_short = (r.get("type") or "resource").split("/")[-1]
        if cpu is not None and cpu < IDLE_CPU_PCT and cost >= 25:
            out.append(
                _opp(
                    "idle",
                    rid,
                    f"Idle {rtype_short} '{r.get('name')}' at {cpu:.1f} % CPU",
                    f"Average CPU {cpu:.1f} % over 24 h while billing "
                    f"€{cost:,.0f}/mo. Stop, downsize, or delete.",
                    acct,
                    rg or acct,
                    cost,
                    {
                        "cpu_avg": cpu,
                        "resource_id": rid,
                        "resource_type": r.get("type"),
                        "rg": rg,
                    },
                )
            )
        elif r.get("status") == "unattached" and cost > 0:
            out.append(
                _opp(
                    "idle",
                    rid,
                    f"Unattached {rtype_short} '{r.get('name')}'",
                    f"Not associated with any resource — pure waste at "
                    f"€{cost:,.2f}/mo. Delete it.",
                    acct,
                    rg or acct,
                    cost,
                    {"resource_id": rid, "resource_type": r.get("type"), "rg": rg},
                )
            )
    return out


def _signal_tags(df30: pd.DataFrame, names: dict[str, str]) -> list[dict]:
    """Unallocated (untagged) spend per subscription — cost-allocation coverage.

    'Shared/Unattributed' rows (subscription-scoped charges that cannot carry a
    tag) are legitimate shared costs, not a tagging gap — they don't count.
    """
    out: list[dict] = []
    if df30.empty or "C_ACCOUNT" not in df30.columns or "C_APP" not in df30.columns:
        return out
    ndays = df30["C_DATE"].nunique() if "C_DATE" in df30.columns else 30
    for acct, grp in df30.groupby("C_ACCOUNT"):
        total = float(grp["C_COST"].sum())
        untagged = float(grp.loc[grp["C_APP"] == "Untagged", "C_COST"].sum())
        if total <= 0:
            continue
        pct = untagged / total
        untagged_monthly = _monthly(untagged, ndays)
        if pct < TAGS_PCT_MIN or untagged_monthly < TAGS_EUR_MIN:
            continue
        name = names.get(str(acct), str(acct))
        out.append(
            _opp(
                "tags",
                str(acct),
                f"{pct:.0%} of {name} spend is unallocated",
                f"€{untagged_monthly:,.0f}/mo has no project tag — it cannot "
                f"be charged back to a team. Enforce tagging via Azure Policy.",
                str(acct),
                name,
                untagged_monthly,
                {
                    "untagged_pct": round(pct * 100, 1),
                    "untagged_monthly_usd": untagged_monthly,
                    "allocation_kind": "unallocated_spend",
                },
            )
        )
    return out


def _signal_offhours(df30: pd.DataFrame, names: dict[str, str]) -> list[dict]:
    """Non-prod resource groups whose weekend compute spend matches weekdays."""
    out: list[dict] = []
    rows = _compute_rows(df30)
    if rows.empty or "C_DATE" not in rows.columns or "C_NAME" not in rows.columns:
        return out
    rows = rows.assign(_weekend=pd.to_datetime(rows["C_DATE"]).dt.dayofweek >= 5)
    for rg, grp in rows.groupby("C_NAME"):
        rg = str(rg)
        if not _is_nonprod(rg):
            continue
        wd = grp[~grp["_weekend"]]
        we = grp[grp["_weekend"]]
        wd_days = wd["C_DATE"].nunique()
        we_days = we["C_DATE"].nunique()
        if not wd_days or not we_days:
            continue
        wd_daily = float(wd["C_COST"].sum()) / wd_days
        we_daily = float(we["C_COST"].sum()) / we_days
        if wd_daily <= 0:
            continue
        ratio = we_daily / wd_daily
        weekend_monthly = round(we_daily * 8.7, 2)  # ~8.7 weekend days/month
        if ratio < WEEKEND_RATIO_MIN or weekend_monthly < 100:
            continue
        impact = weekend_monthly * 0.85  # assume ~85 % can be stopped
        acct = _dominant_account(grp)
        out.append(
            _opp(
                "offhours",
                rg,
                f"{rg} compute runs through weekends",
                f"Weekend spend is {ratio:.0%} of a weekday (€{we_daily:,.0f} vs "
                f"€{wd_daily:,.0f} per day). Scheduled shutdown saves "
                f"~€{impact:,.0f}/mo.",
                acct,
                rg,
                impact,
                {
                    "weekend_ratio_pct": round(ratio * 100, 1),
                    "weekday_daily_usd": round(wd_daily, 2),
                    "weekend_daily_usd": round(we_daily, 2),
                    "rg": rg,
                },
            )
        )
    return out


def _signal_benchmark(df30: pd.DataFrame, names: dict[str, str]) -> list[dict]:
    """Compare dev resource groups on compute run-hours per resource per day."""
    out: list[dict] = []
    rows = _compute_rows(df30)
    if rows.empty or "C_QUANTITY" not in rows.columns or "C_NAME" not in rows.columns:
        return out

    stats: dict[str, dict] = {}
    accts: dict[str, str] = {}
    for rg, grp in rows.groupby("C_NAME"):
        rg = str(rg)
        if not _is_nonprod(rg):
            continue
        n_res = grp["C_RESOURCE_ID"].nunique() if "C_RESOURCE_ID" in grp.columns else 1
        n_days = grp["C_DATE"].nunique() if "C_DATE" in grp.columns else 30
        hours = float(pd.to_numeric(grp["C_QUANTITY"], errors="coerce").fillna(0).sum())
        per_day = hours / max(n_res, 1) / max(n_days, 1)
        stats[rg] = {
            "rg": rg,
            "hours_per_day": round(per_day, 1),
            "compute_monthly_usd": _monthly(float(grp["C_COST"].sum()), n_days),
        }
        accts[rg] = _dominant_account(grp)

    if len(stats) < 2:
        return out
    best = min(stats.values(), key=lambda s: s["hours_per_day"])
    if best["hours_per_day"] <= 0:
        return out
    table = sorted(stats.values(), key=lambda s: s["hours_per_day"])
    for s in stats.values():
        if s["hours_per_day"] <= best["hours_per_day"] * BENCH_HOURS_FACTOR:
            continue
        impact = s["compute_monthly_usd"] * (
            1 - best["hours_per_day"] / s["hours_per_day"]
        )
        if impact < 100:
            continue
        out.append(
            _opp(
                "benchmark",
                s["rg"],
                f"{s['rg']} runs {s['hours_per_day']:.0f} h/day — "
                f"{best['rg']} manages {best['hours_per_day']:.0f}",
                f"Matching {best['rg']}'s schedule "
                f"({best['hours_per_day']:.0f} h/day vs "
                f"{s['hours_per_day']:.0f}) would save ~€{impact:,.0f}/mo.",
                accts.get(s["rg"], ""),
                s["rg"],
                impact,
                {
                    "benchmark": table,
                    "best_rg": best["rg"],
                    "hours_per_day": s["hours_per_day"],
                    "rg": s["rg"],
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Live-inventory enrichment (CPU averages, unattached disks / public IPs)
# ---------------------------------------------------------------------------

_CPU_MONTHLY_MIN = 25.0  # only query metrics for resources worth flagging


def _enrich_vm_cpu(live: list[dict], df30: pd.DataFrame) -> None:
    """Fill `cpu_avg` (24 h average) on VM/VMSS live entries via Azure Monitor."""
    from live_resources import fetch_resource_metrics

    for r in live:
        if r.get("category") != "vm" or r.get("cpu_avg") is not None:
            continue
        cost = r.get("monthly_cost")
        if cost is None:
            cost = _resource_monthly_cost(df30, r.get("id") or "")
            r["monthly_cost"] = cost
        if cost < _CPU_MONTHLY_MIN:
            continue
        data = fetch_resource_metrics(r["id"], hours=24)
        if data.get("error"):
            continue
        cpu = next(
            (m for m in data.get("metrics", []) if m.get("label") == "CPU"), None
        )
        if not cpu:
            continue
        vals = [p["v"] for p in cpu.get("data", []) if p.get("v") is not None]
        if vals:
            r["cpu_avg"] = round(sum(vals) / len(vals), 1)


def _unattached_resources() -> list[dict]:
    """Unattached managed disks and public IPs as live-style entries."""
    import json as _json
    import urllib.request

    from azure.identity import DefaultAzureCredential

    from config import AZURE_CLIENT_ID, AZURE_SUBSCRIPTION_ID
    from live_resources import _get_compute_mgmt_client

    out: list[dict] = []

    cc = _get_compute_mgmt_client()
    for disk in cc.disks.list():
        if (disk.disk_state or "").lower() != "unattached":
            continue
        parts = (disk.id or "").split("/resourceGroups/")
        rg = parts[1].split("/")[0].lower() if len(parts) > 1 else ""
        out.append(
            {
                "id": disk.id,
                "name": disk.name or "",
                "type": "Microsoft.Compute/disks",
                "category": "storage",
                "resource_group": rg,
                "status": "unattached",
            }
        )

    # Public IPs without an ipConfiguration (generic ARM list lacks properties,
    # so query the network provider directly).
    cred = DefaultAzureCredential(managed_identity_client_id=AZURE_CLIENT_ID or None)
    token = cred.get_token("https://management.azure.com/.default").token
    url = (
        f"https://management.azure.com/subscriptions/{AZURE_SUBSCRIPTION_ID}"
        "/providers/Microsoft.Network/publicIPAddresses?api-version=2023-09-01"
    )
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req) as resp:
            data = _json.loads(resp.read())
        for ip in data.get("value", []):
            props = ip.get("properties", {})
            if props.get("ipConfiguration") or props.get("natGateway"):
                continue
            rid = ip.get("id", "")
            parts = rid.split("/resourceGroups/")
            rg = parts[1].split("/")[0].lower() if len(parts) > 1 else ""
            out.append(
                {
                    "id": rid,
                    "name": ip.get("name", ""),
                    "type": "Microsoft.Network/publicIPAddresses",
                    "category": "network",
                    "resource_group": rg,
                    "status": "unattached",
                }
            )
        url = data.get("nextLink")

    return out


# ---------------------------------------------------------------------------
# Lifecycle store
# ---------------------------------------------------------------------------

_store_lock = asyncio.Lock()
_store_cache: dict[str, Any] = {}


def _load_state() -> dict[str, dict]:
    if "state" in _store_cache:
        return _store_cache["state"]
    from storage import read_blob_json

    state = read_blob_json(OPPORTUNITY_STATE_KEY)
    _store_cache["state"] = state
    return state


def _save_state(state: dict[str, dict]) -> None:
    _store_cache["state"] = state
    from storage import write_blob_json

    write_blob_json(OPPORTUNITY_STATE_KEY, state)


async def update_status(
    opp_id: str, status: str, snapshot: dict | None = None, note: str = ""
) -> dict:
    """Set lifecycle status for an opportunity; snapshot preserves title/scope/
    impact so resolved items survive after the finding disappears."""
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status '{status}'")
    async with _store_lock:
        state = dict(_load_state())
        entry = dict(state.get(opp_id, {}))
        if snapshot:
            entry.update(
                {
                    "signal": snapshot.get("signal"),
                    "title": snapshot.get("title"),
                    "account_id": snapshot.get("account_id"),
                    "account_name": snapshot.get("account_name"),
                    "estimated_monthly_usd": snapshot.get("monthly_impact_usd"),
                    "scope": snapshot.get("scope") or _scope_from(snapshot),
                }
            )
        entry["status"] = status
        if note:
            entry["note"] = note
        if status == "resolved" and not entry.get("resolved_date"):
            entry["resolved_date"] = date.today().isoformat()
        state[opp_id] = entry
        _save_state(state)
        return entry


def _scope_from(opp: dict) -> dict:
    scope: dict[str, Any] = {"account": opp.get("account_id")}
    evidence = opp.get("evidence") or {}
    if evidence.get("rg"):
        scope["rg"] = evidence["rg"]
    if evidence.get("resource_id"):
        scope["resource_id"] = evidence["resource_id"]
    return scope


# ---------------------------------------------------------------------------
# Realized savings verification
# ---------------------------------------------------------------------------


def _realized_savings(df: pd.DataFrame, entry: dict) -> dict | None:
    """Spend in scope: 28 days before resolution vs the latest 28 days."""
    scope = entry.get("scope") or {}
    resolved = entry.get("resolved_date")
    if not resolved or df.empty or "C_DATE" not in df.columns:
        return None

    sub = df
    if scope.get("account") and "C_ACCOUNT" in sub.columns:
        sub = sub[sub["C_ACCOUNT"] == scope["account"]]
    if scope.get("rg") and "C_NAME" in sub.columns:
        sub = sub[sub["C_NAME"] == str(scope["rg"]).lower()]
    if scope.get("resource_id") and "C_RESOURCE_ID" in sub.columns:
        sub = sub[sub["C_RESOURCE_ID"].str.lower() == str(scope["resource_id"]).lower()]
    if sub.empty:
        return None

    res_day = date.fromisoformat(resolved)
    before_start = (res_day - timedelta(days=28)).isoformat()
    before = sub[(sub["C_DATE"] >= before_start) & (sub["C_DATE"] < resolved)]

    b_days = before["C_DATE"].nunique()
    dataset_max = str(df["C_DATE"].dropna().max())
    if not b_days or dataset_max < (res_day + timedelta(days=7)).isoformat():
        return None  # too little data on either side of the resolution

    after = sub[sub["C_DATE"] >= resolved]
    after = _last_n_days(after, 28) if not after.empty else after
    a_days = after["C_DATE"].nunique()

    before_daily = float(before["C_COST"].sum()) / b_days
    # No rows after resolution = the resource is gone; spend in scope is 0.
    after_daily = float(after["C_COST"].sum()) / a_days if a_days else 0.0
    realized = max((before_daily - after_daily) * 30, 0)
    return {
        "before_monthly_usd": round(before_daily * 30, 2),
        "after_monthly_usd": round(after_daily * 30, 2),
        "realized_monthly_usd": round(realized, 2),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_engine_cache: dict[str, Any] = {}
_ENGINE_TTL = 300  # signals are pure functions of df+live; cache briefly


async def get_opportunities(refresh: bool = False) -> dict[str, Any]:
    """Compute all signals, merge lifecycle state, verify resolved savings."""
    from live_resources import _get_live_data
    from storage import get_cached_dataframe

    now = time.monotonic()
    if (
        not refresh
        and "result" in _engine_cache
        and now - _engine_cache.get("ts", 0) < _ENGINE_TTL
    ):
        return _engine_cache["result"]

    df = await get_cached_dataframe()
    try:
        live = list(await _get_live_data())
    except Exception as exc:  # live inventory is optional for cost-only signals
        log.warning("Opportunities: live inventory unavailable: %s", exc)
        live = []

    names = _subscription_names(df)
    df30 = _last_n_days(df, 30)

    loop = asyncio.get_event_loop()
    try:
        live += await loop.run_in_executor(None, _unattached_resources)
    except Exception as exc:
        log.warning("Opportunities: unattached-resource scan failed: %s", exc)
    try:
        await loop.run_in_executor(None, _enrich_vm_cpu, live, df30)
    except Exception as exc:
        log.warning("Opportunities: VM CPU enrichment failed: %s", exc)

    found: list[dict] = []
    for fn in (
        lambda: _signal_risp(df30, names),
        lambda: _signal_idle(df30, live, names),
        lambda: _signal_tags(df30, names),
        lambda: _signal_offhours(df30, names),
        lambda: _signal_benchmark(df30, names),
    ):
        try:
            found.extend(fn())
        except Exception:
            log.exception("Opportunity signal failed")

    state = _load_state()
    found_ids = set()
    for opp in found:
        entry = state.get(opp["id"])
        if entry:
            opp["status"] = entry.get("status", "new")
            if entry.get("note"):
                opp["note"] = entry["note"]
        found_ids.add(opp["id"])

    # Store-only entries (typically resolved/dismissed history) + verification
    for opp_id, entry in state.items():
        status = entry.get("status", "new")
        if opp_id in found_ids:
            if status == "resolved":
                for opp in found:
                    if opp["id"] == opp_id:
                        opp["realized"] = _realized_savings(df, entry)
            continue
        if status not in ("resolved", "dismissed"):
            continue  # stale open state for a finding that no longer fires
        hist = {
            "id": opp_id,
            "signal": entry.get("signal", opp_id.split(":", 1)[0]),
            "title": entry.get("title", opp_id),
            "detail": "",  # note is rendered separately — don't duplicate it
            "account_id": entry.get("account_id", ""),
            "account_name": entry.get("account_name", ""),
            "monthly_impact_usd": entry.get("estimated_monthly_usd") or 0,
            "evidence": {},
            "status": status,
            "note": entry.get("note", ""),
            "resolved_date": entry.get("resolved_date"),
        }
        if status == "resolved":
            hist["realized"] = _realized_savings(df, entry)
        found.append(hist)

    open_opps = [o for o in found if o["status"] in ("new", "in_progress")]
    resolved = [o for o in found if o["status"] == "resolved"]
    realized_total = sum(
        (o.get("realized") or {}).get("realized_monthly_usd", 0) for o in resolved
    )

    def _rank(o: dict) -> tuple:
        # new and in_progress are both "open" — rank them together by impact
        order = {"new": 0, "in_progress": 0, "resolved": 2, "dismissed": 3}
        return (order.get(o["status"], 9), -o["monthly_impact_usd"])

    result = {
        "opportunities": sorted(found, key=_rank),
        "summary": {
            "open_count": len(open_opps),
            "open_monthly_usd": round(
                sum(o["monthly_impact_usd"] for o in open_opps), 2
            ),
            "resolved_count": len(resolved),
            "realized_monthly_usd": round(realized_total, 2),
            "accounts": sorted(names.values()),
        },
    }
    _engine_cache["result"] = result
    _engine_cache["ts"] = now
    return result
