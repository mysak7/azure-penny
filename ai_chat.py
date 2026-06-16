"""
ai_chat.py — Agentic AI chat engine for azure-penny.

Provides the shared `_run_ai_chat` coroutine used by both the web SSE
endpoint (GET /api/ai/chat) and the Telegram webhook handler.
"""

import json
from datetime import date, timedelta

from config import (
    CF_ACCESS_CLIENT_ID,
    CF_ACCESS_CLIENT_SECRET,
    VERTEX_PROXY_API_KEY,
    VERTEX_PROXY_URL,
    log,
)
from cost_filters import (
    _bucket_key,
    _build_snapshot,
    _cost_by,
    _filter_app,
    _filter_period,
    _filter_rg,
    _period_days,
)
from storage import get_cached_dataframe

# ---------------------------------------------------------------------------
# Model & system prompt
# ---------------------------------------------------------------------------

_CHAT_MODEL = "gemini-3.5-flash"

_CHAT_SYSTEM_PROMPT = (
    "You are azure-penny, an AI assistant specialized in analyzing Microsoft Azure cloud costs. "
    "You have access to tools that provide real-time data from Azure Cost Management exports. "
    "ALWAYS call the relevant tools to get current data before answering questions about costs or resources. "
    "Be concise, precise, and actionable. Format currency values in USD with 2 decimal places. "
    "When you identify cost issues or anomalies, suggest concrete optimizations. "
    "Never suggest or perform any resource deletion or infrastructure changes. "
    "Detect the language of the user's question and always respond in the same language "
    "(Czech question → Czech answer, English question → English answer)."
)

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

_CHAT_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_cost_summary",
            "description": (
                "Get total Azure cost and top services/resource-groups for a time period. "
                "Use this to answer questions about total spend, biggest cost drivers, or overview."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["day", "week", "month", "year"],
                        "description": "Time period for cost analysis (default: week)",
                    },
                    "rg": {
                        "type": "string",
                        "description": "Filter by resource group name (optional)",
                    },
                    "app": {
                        "type": "string",
                        "description": "Filter by app/project tag (optional)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_anomalies",
            "description": (
                "Get week-over-week cost anomalies: spikes (>50% increase), new services, "
                "disappeared services, and untagged resources with their costs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rg": {
                        "type": "string",
                        "description": "Filter by resource group (optional)",
                    },
                    "app": {
                        "type": "string",
                        "description": "Filter by app/project tag (optional)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_resource_groups",
            "description": "Get Azure costs broken down by resource group for a given time period.",
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["day", "week", "month", "year"],
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_breakdown",
            "description": (
                "Get time-series cost breakdown with daily/weekly/monthly buckets. "
                "Use for trend analysis, forecasting questions, or category-level drill-down."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["day", "week", "month", "year"],
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "all",
                            "compute",
                            "storage",
                            "network",
                            "database",
                            "monitoring",
                            "other",
                        ],
                    },
                    "granularity": {
                        "type": "string",
                        "enum": ["days", "weeks", "months"],
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_live_resources",
            "description": (
                "Get current live Azure resources with their monthly cost estimates. "
                "Use for questions about running resources, spot price savings, or resource inventory."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_daily_costs",
            "description": "Get daily cost time series for the past N days. Use for trend or day-by-day analysis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of past days (default 30)",
                    },
                    "rg": {"type": "string"},
                    "app": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_page_snapshot",
            "description": (
                "Get a comprehensive all-in-one cost snapshot: total spend, breakdown by category, "
                "top services, top resource groups, application-tag breakdown, and week-over-week "
                "anomalies — all in a single call. "
                "Use when the user asks for a general overview, wants to understand the full picture, "
                "or asks broad questions like 'what's going on with my costs' or 'give me a summary'. "
                "More complete than get_cost_summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["day", "week", "month", "year"],
                        "description": "Time period (default: week)",
                    },
                    "rg": {
                        "type": "string",
                        "description": "Filter by resource group (optional)",
                    },
                    "app": {
                        "type": "string",
                        "description": "Filter by app/project tag (optional)",
                    },
                },
            },
        },
    },
]

_TOOL_LABELS: dict[str, str] = {
    "get_cost_summary": "přehled nákladů",
    "get_anomalies": "anomálie",
    "get_resource_groups": "resource groups",
    "get_breakdown": "detail kategorií",
    "get_live_resources": "živé zdroje",
    "get_daily_costs": "denní data",
    "get_page_snapshot": "přehled stránky",
}

# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


async def _execute_chat_tool(name: str, args: dict) -> str:
    """Execute a read-only chat tool and return a JSON string result."""
    # Import here to avoid circular imports at module load time.
    from live_resources import _get_live_data  # noqa: PLC0415

    try:
        if name == "get_cost_summary":
            period = args.get("period", "week")
            rg = args.get("rg", "")
            app = args.get("app", "")
            full_df = await get_cached_dataframe()
            df = _filter_app(_filter_rg(full_df, rg), app)
            filtered = _filter_period(df, _period_days(period))
            total = (
                round(float(filtered["C_COST"].sum()), 2)
                if "C_COST" in filtered.columns
                else 0
            )
            top_svcs = _cost_by(filtered, "C_SERVICE")
            top_rgs = _cost_by(filtered, "C_NAME")
            data_as_of = None
            if not filtered.empty and "C_DATE" in filtered.columns:
                data_as_of = str(filtered["C_DATE"].dropna().max())
            return json.dumps(
                {
                    "period": period,
                    "total_usd": total,
                    "top_services": [
                        {"service": k, "cost_usd": v}
                        for k, v in list(top_svcs.items())[:10]
                    ],
                    "top_resource_groups": [
                        {"name": k, "cost_usd": v}
                        for k, v in list(top_rgs.items())[:10]
                    ],
                    "data_as_of": data_as_of,
                    "filters": {"rg": rg, "app": app},
                }
            )

        if name == "get_anomalies":
            rg = args.get("rg", "")
            app = args.get("app", "")
            full_df = await get_cached_dataframe()
            df = _filter_app(_filter_rg(full_df, rg), app)
            today = date.today()
            curr_start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
            prev_start = (today - timedelta(days=14)).strftime("%Y-%m-%d")
            import pandas as pd  # noqa: PLC0415

            has_date = "C_DATE" in df.columns
            curr_df = df[df["C_DATE"] >= curr_start] if has_date else df
            prev_df = (
                df[(df["C_DATE"] >= prev_start) & (df["C_DATE"] < curr_start)]
                if has_date
                else pd.DataFrame(columns=df.columns)
            )

            def _deltas(col: str) -> list[dict]:
                if col not in df.columns or "C_COST" not in df.columns:
                    return []
                c = (
                    curr_df.groupby(col)["C_COST"].sum()
                    if not curr_df.empty
                    else pd.Series(dtype=float)
                )
                p = (
                    prev_df.groupby(col)["C_COST"].sum()
                    if not prev_df.empty
                    else pd.Series(dtype=float)
                )
                rows = []
                for k in set(c.index) | set(p.index):
                    cv, pv = float(c.get(k, 0)), float(p.get(k, 0))
                    if cv == 0 and pv == 0:
                        continue
                    dp = round((cv - pv) / pv * 100, 1) if pv > 0 else None
                    rows.append(
                        {
                            col: str(k),
                            "current_week_usd": round(cv, 2),
                            "prior_week_usd": round(pv, 2),
                            "delta_pct": dp,
                            "is_new": pv == 0 and cv > 0,
                            "vanished": cv == 0 and pv > 0,
                        }
                    )
                return rows

            svc_rows = _deltas("C_SERVICE")
            untagged_cost, untagged_count = 0.0, 0
            if "C_TAGS" in df.columns and "C_COST" in df.columns:
                mask = df["C_TAGS"].isna() | df["C_TAGS"].astype(str).str.strip().isin(
                    ["", "{}", "nan", "None"]
                )
                utdf = df[mask]
                untagged_cost = round(float(utdf["C_COST"].sum()), 2)
                untagged_count = (
                    int(utdf["C_RESOURCE_ID"].nunique())
                    if "C_RESOURCE_ID" in utdf.columns
                    else len(utdf)
                )
            return json.dumps(
                {
                    "spikes": sorted(
                        [s for s in svc_rows if (s.get("delta_pct") or 0) > 50],
                        key=lambda x: x["delta_pct"],
                        reverse=True,
                    )[:5],
                    "new_services": sorted(
                        [
                            s
                            for s in svc_rows
                            if s["is_new"] and s["current_week_usd"] > 1
                        ],
                        key=lambda x: x["current_week_usd"],
                        reverse=True,
                    )[:5],
                    "vanished_services": sorted(
                        [
                            s
                            for s in svc_rows
                            if s["vanished"] and s["prior_week_usd"] > 1
                        ],
                        key=lambda x: x["prior_week_usd"],
                        reverse=True,
                    )[:5],
                    "untagged_cost_usd": untagged_cost,
                    "untagged_resource_count": untagged_count,
                }
            )

        if name == "get_resource_groups":
            period = args.get("period", "week")
            full_df = await get_cached_dataframe()
            filtered = _filter_period(full_df, _period_days(period))
            by_rg = _cost_by(filtered, "C_NAME")
            return json.dumps(
                {
                    "period": period,
                    "resource_groups": [
                        {"name": k, "cost_usd": v} for k, v in list(by_rg.items())[:20]
                    ],
                    "total_usd": round(sum(by_rg.values()), 2),
                }
            )

        if name == "get_breakdown":
            period = args.get("period", "week")
            granularity = args.get("granularity", "days")
            import pandas as pd  # noqa: PLC0415

            full_df = await get_cached_dataframe()
            df = _filter_period(full_df, _period_days(period))
            if df.empty or "C_DATE" not in df.columns or "C_COST" not in df.columns:
                return json.dumps({"buckets": [], "period": period})
            df = df.copy()
            df["_bucket"] = df["C_DATE"].apply(
                lambda x: _bucket_key(str(x), granularity)
            )
            grp = df.groupby("_bucket")["C_COST"].sum().sort_index()
            return json.dumps(
                {
                    "period": period,
                    "granularity": granularity,
                    "buckets": [
                        {"period": k, "cost_usd": round(float(v), 2)}
                        for k, v in grp.items()
                    ][:30],
                    "total_usd": round(float(grp.sum()), 2),
                }
            )

        if name == "get_live_resources":
            resources = await _get_live_data()
            with_cost = sorted(
                [r for r in resources if (r.get("monthly_cost") or 0) > 0],
                key=lambda r: r.get("monthly_cost") or 0,
                reverse=True,
            )
            total = sum(r.get("monthly_cost") or 0 for r in with_cost)
            return json.dumps(
                {
                    "count": len(resources),
                    "with_cost_count": len(with_cost),
                    "total_monthly_usd": round(total, 2),
                    "top_resources": [
                        {
                            "name": r.get("name", ""),
                            "type": r.get("type", ""),
                            "resource_group": r.get("resource_group", ""),
                            "monthly_cost_usd": round(r.get("monthly_cost") or 0, 2),
                            "location": r.get("location", ""),
                        }
                        for r in with_cost[:20]
                    ],
                }
            )

        if name == "get_daily_costs":
            days_n = int(args.get("days", 30))
            rg = args.get("rg", "")
            app = args.get("app", "")
            df = await get_cached_dataframe()
            df = _filter_app(_filter_rg(df, rg), app)
            if "C_DATE" not in df.columns or "C_COST" not in df.columns:
                return json.dumps({"points": [], "total_usd": 0})
            cutoff = (date.today() - timedelta(days=days_n)).strftime("%Y-%m-%d")
            filtered = df[df["C_DATE"] >= cutoff]
            daily = filtered.groupby("C_DATE")["C_COST"].sum().sort_index()
            points = [
                {"date": str(d), "cost_usd": round(float(v), 2)}
                for d, v in daily.items()
            ]
            total = round(float(daily.sum()), 2)
            return json.dumps(
                {
                    "days": days_n,
                    "points": points,
                    "total_usd": total,
                    "avg_daily_usd": round(total / max(len(points), 1), 2),
                }
            )

        if name == "get_page_snapshot":
            period = args.get("period", "week")
            rg = args.get("rg", "")
            app = args.get("app", "")
            snap = await _build_snapshot(period, rg, app)
            return json.dumps({k: snap[k] for k in snap if k != "markdown"})

        return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as exc:
        log.exception("Chat tool '%s' failed", name)
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _chat_headers() -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {VERTEX_PROXY_API_KEY}",
        "Content-Type": "application/json",
    }
    if CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET:
        h["CF-Access-Client-Id"] = CF_ACCESS_CLIENT_ID
        h["CF-Access-Client-Secret"] = CF_ACCESS_CLIENT_SECRET
    return h


# ---------------------------------------------------------------------------
# Shared agentic loop
# ---------------------------------------------------------------------------


async def _run_ai_chat(question: str, history: list[dict]) -> str:
    """Run the agentic AI chat loop and return the final text answer (no streaming).

    Shared by the web SSE endpoint and the Telegram webhook handler so both
    use exactly the same model, tools, and system prompt.
    """
    if not VERTEX_PROXY_URL or not VERTEX_PROXY_API_KEY:
        return "❌ AI chat není nakonfigurován (chybí VERTEX_PROXY_URL / VERTEX_PROXY_API_KEY)."

    import httpx  # noqa: PLC0415

    messages: list[dict] = [{"role": "system", "content": _CHAT_SYSTEM_PROMPT}]
    messages += list(history)
    messages.append({"role": "user", "content": question})

    final_text = ""

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            for _ in range(5):
                resp = await client.post(
                    f"{VERTEX_PROXY_URL}/v1/chat/completions",
                    headers=_chat_headers(),
                    json={
                        "model": _CHAT_MODEL,
                        "messages": messages,
                        "tools": _CHAT_TOOLS,
                        "tool_choice": "auto",
                    },
                )
                resp.raise_for_status()
                msg = resp.json()["choices"][0]["message"]
                tc_list = msg.get("tool_calls") or []

                if not tc_list:
                    final_text = msg.get("content") or ""
                    break

                messages.append(
                    {
                        "role": "assistant",
                        "content": msg.get("content"),
                        "tool_calls": tc_list,
                    }
                )
                for tc in tc_list:
                    try:
                        tool_args = json.loads(tc["function"].get("arguments", "{}"))
                    except json.JSONDecodeError:
                        tool_args = {}
                    result = await _execute_chat_tool(tc["function"]["name"], tool_args)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result,
                        }
                    )

            # If loop exhausted without a text answer, force one no-tools call.
            if not final_text:
                resp = await client.post(
                    f"{VERTEX_PROXY_URL}/v1/chat/completions",
                    headers=_chat_headers(),
                    json={"model": _CHAT_MODEL, "messages": messages},
                )
                resp.raise_for_status()
                final_text = resp.json()["choices"][0]["message"].get("content") or ""
    except Exception:
        log.exception("_run_ai_chat failed")
        raise

    return final_text
