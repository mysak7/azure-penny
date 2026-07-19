"""Tests for the opportunity engine (Savings Inbox).

Run on synthetic DataFrames and a monkeypatched blob store — no Azure
credentials or network access required.
"""

import asyncio
from datetime import date, timedelta

import pandas as pd
import pytest

import opportunities as engine

SUB = "Azure subscription 1"
DISK_ID = (
    "/subscriptions/s/resourceGroups/prod-rg/providers"
    "/Microsoft.Compute/disks/orphan-disk"
)


def _make_df(days: int = 30) -> pd.DataFrame:
    """30 days of synthetic export data seeded so every cost signal fires."""
    rows = []
    end = date.today() - timedelta(days=1)
    for i in range(days):
        d = (end - timedelta(days=i)).isoformat()
        # dev-alpha-rg: compute 24 h/day, runs weekends too (offhours + benchmark)
        rows.append(
            {
                "C_DATE": d,
                "C_ACCOUNT": SUB,
                "C_NAME": "dev-alpha-rg",
                "C_SERVICE": "Virtual Machines",
                "C_FAMILY": "Compute",
                "C_PRICING": "OnDemand",
                "C_TYPE": "Usage",
                "C_COST": 30.0,
                "C_QUANTITY": 24.0,
                "C_APP": "alpha",
                "C_RESOURCE_ID": "/vm/alpha-1",
            }
        )
        # dev-beta-rg: compute 8 h/day, weekdays only (the benchmark "best")
        if date.fromisoformat(d).weekday() < 5:
            rows.append(
                {
                    "C_DATE": d,
                    "C_ACCOUNT": SUB,
                    "C_NAME": "dev-beta-rg",
                    "C_SERVICE": "Virtual Machines",
                    "C_FAMILY": "Compute",
                    "C_PRICING": "OnDemand",
                    "C_TYPE": "Usage",
                    "C_COST": 10.0,
                    "C_QUANTITY": 8.0,
                    "C_APP": "beta",
                    "C_RESOURCE_ID": "/vm/beta-1",
                }
            )
        # prod-rg: on-demand compute (drives risp) + untagged storage (tags)
        rows.append(
            {
                "C_DATE": d,
                "C_ACCOUNT": SUB,
                "C_NAME": "prod-rg",
                "C_SERVICE": "Virtual Machines",
                "C_FAMILY": "Compute",
                "C_PRICING": "OnDemand",
                "C_TYPE": "Usage",
                "C_COST": 40.0,
                "C_QUANTITY": 24.0,
                "C_APP": "prodapp",
                "C_RESOURCE_ID": "/vm/prod-1",
            }
        )
        rows.append(
            {
                "C_DATE": d,
                "C_ACCOUNT": SUB,
                "C_NAME": "prod-rg",
                "C_SERVICE": "Storage",
                "C_FAMILY": "Storage",
                "C_PRICING": "OnDemand",
                "C_TYPE": "Usage",
                "C_COST": 20.0,
                "C_QUANTITY": 1.0,
                "C_APP": "Untagged",
                "C_RESOURCE_ID": "/storage/untagged-1",
            }
        )
        # unattached disk billing rows (idle signal cost lookup)
        rows.append(
            {
                "C_DATE": d,
                "C_ACCOUNT": SUB,
                "C_NAME": "prod-rg",
                "C_SERVICE": "Storage",
                "C_FAMILY": "Storage",
                "C_PRICING": "OnDemand",
                "C_TYPE": "Usage",
                "C_COST": 0.2,
                "C_QUANTITY": 1.0,
                "C_APP": "prodapp",
                "C_RESOURCE_ID": DISK_ID,
            }
        )
    return pd.DataFrame(rows)


def _live() -> list[dict]:
    return [
        {
            "id": "/subscriptions/s/resourceGroups/prod-rg/providers"
            "/Microsoft.Compute/virtualMachines/vm-idle",
            "name": "vm-idle",
            "type": "Microsoft.Compute/virtualMachines",
            "category": "vm",
            "resource_group": "prod-rg",
            "monthly_cost": 80.0,
            "cpu_avg": 2.1,
        },
        {
            "id": DISK_ID,
            "name": "orphan-disk",
            "type": "Microsoft.Compute/disks",
            "category": "storage",
            "resource_group": "prod-rg",
            "status": "unattached",
        },
    ]


@pytest.fixture
def fake_store(monkeypatch):
    """In-memory replacement for the blob-backed lifecycle store."""
    import storage

    store: dict = {}
    monkeypatch.setattr(storage, "read_blob_json", lambda name: dict(store))
    monkeypatch.setattr(
        storage, "write_blob_json", lambda name, data: store.update(data)
    )
    engine._store_cache.clear()
    engine._engine_cache.clear()
    yield store
    engine._store_cache.clear()
    engine._engine_cache.clear()


@pytest.fixture
def patched_engine(monkeypatch, fake_store):
    """get_opportunities wired to synthetic data, no Azure calls."""
    import live_resources
    import storage

    df = _make_df()

    async def _df():
        return df

    async def _live_data():
        return _live()

    monkeypatch.setattr(storage, "get_cached_dataframe", _df)
    monkeypatch.setattr(live_resources, "_get_live_data", _live_data)
    monkeypatch.setattr(engine, "_unattached_resources", list)
    monkeypatch.setattr(engine, "_enrich_vm_cpu", lambda live, df30: None)
    return df


# ── signals ────────────────────────────────────────────────────────────────


def test_all_five_signals_fire(patched_engine):
    result = asyncio.run(engine.get_opportunities(refresh=True))
    signals = {o["signal"] for o in result["opportunities"]}
    assert {"risp", "idle", "tags", "offhours", "benchmark"} <= signals


def test_risp_flags_uncovered_compute():
    df = _make_df()
    opps = engine._signal_risp(df, {SUB: SUB})
    assert len(opps) == 1
    o = opps[0]
    assert o["account_name"] == SUB
    assert o["evidence"]["coverage_pct"] == 0.0
    assert o["monthly_impact_usd"] > 0


def test_risp_quiet_when_covered():
    df = _make_df()
    df["C_PRICING"] = "Reservation"
    assert engine._signal_risp(df, {SUB: SUB}) == []


def test_idle_finds_low_cpu_and_unattached():
    df = _make_df()
    opps = engine._signal_idle(df, _live(), {SUB: SUB})
    titles = " | ".join(o["title"] for o in opps)
    assert "vm-idle" in titles and "2.1 % CPU" in titles
    assert "Unattached" in titles and "orphan-disk" in titles
    disk = next(o for o in opps if "orphan-disk" in o["title"])
    assert disk["monthly_impact_usd"] == pytest.approx(6.0, abs=0.1)


def test_tags_flags_untagged_spend():
    df = _make_df()
    opps = engine._signal_tags(df, {SUB: SUB})
    assert len(opps) == 1
    assert opps[0]["evidence"]["untagged_monthly_usd"] == pytest.approx(600, rel=0.05)


def test_offhours_flags_weekend_dev_rg():
    df = _make_df()
    opps = engine._signal_offhours(df, {SUB: SUB})
    rgs = {o["account_name"] for o in opps}
    assert "dev-alpha-rg" in rgs  # runs weekends
    assert "dev-beta-rg" not in rgs  # weekdays only


def test_benchmark_compares_dev_rgs():
    df = _make_df()
    opps = engine._signal_benchmark(df, {SUB: SUB})
    assert len(opps) == 1
    o = opps[0]
    assert o["account_name"] == "dev-alpha-rg"
    assert o["evidence"]["best_rg"] == "dev-beta-rg"


# ── lifecycle + ranking ────────────────────────────────────────────────────


def test_open_opportunities_ranked_by_impact(patched_engine):
    result = asyncio.run(engine.get_opportunities(refresh=True))
    open_opps = [
        o for o in result["opportunities"] if o["status"] in ("new", "in_progress")
    ]
    impacts = [o["monthly_impact_usd"] for o in open_opps]
    assert impacts == sorted(impacts, reverse=True)


def test_status_lifecycle_roundtrip(patched_engine):
    result = asyncio.run(engine.get_opportunities(refresh=True))
    target = result["opportunities"][0]
    asyncio.run(
        engine.update_status(
            target["id"], "in_progress", snapshot=target, note="FIN-42"
        )
    )
    refreshed = asyncio.run(engine.get_opportunities(refresh=True))
    updated = next(o for o in refreshed["opportunities"] if o["id"] == target["id"])
    assert updated["status"] == "in_progress"
    assert updated["note"] == "FIN-42"


def test_update_status_rejects_invalid():
    with pytest.raises(ValueError):
        asyncio.run(engine.update_status("x", "bogus"))


def test_resolved_history_survives_when_finding_disappears(patched_engine, fake_store):
    fake_store["idle:/gone/resource"] = {
        "signal": "idle",
        "title": "Idle VM 'gone-vm'",
        "account_id": SUB,
        "account_name": "prod-rg",
        "estimated_monthly_usd": 55.0,
        "status": "resolved",
        "resolved_date": (date.today() - timedelta(days=10)).isoformat(),
        "scope": {"account": SUB, "resource_id": "/gone/resource"},
    }
    result = asyncio.run(engine.get_opportunities(refresh=True))
    hist = next(o for o in result["opportunities"] if o["id"] == "idle:/gone/resource")
    assert hist["status"] == "resolved"
    assert result["summary"]["resolved_count"] >= 1


# ── realized savings verification ──────────────────────────────────────────


def test_realized_savings_before_vs_after():
    end = date.today() - timedelta(days=1)
    resolved = (end - timedelta(days=14)).isoformat()
    rows = []
    for i in range(56):
        d = (end - timedelta(days=i)).isoformat()
        cost = 0.0 if d >= resolved else 10.0  # resource removed on resolution
        rows.append(
            {
                "C_DATE": d,
                "C_ACCOUNT": SUB,
                "C_NAME": "prod-rg",
                "C_COST": cost,
                "C_RESOURCE_ID": "/vm/removed",
            }
        )
    df = pd.DataFrame(rows)
    entry = {
        "resolved_date": resolved,
        "scope": {"account": SUB, "resource_id": "/vm/removed"},
    }
    realized = engine._realized_savings(df, entry)
    assert realized is not None
    assert realized["before_monthly_usd"] == pytest.approx(300, rel=0.01)
    assert realized["realized_monthly_usd"] == pytest.approx(300, rel=0.01)


# ── API ────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(patched_engine):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routers import opportunities as opp_router

    app = FastAPI()
    app.include_router(opp_router.router)
    with TestClient(app) as c:
        yield c


def test_api_opportunities_filters(client):
    data = client.get("/api/opportunities?signal=idle&refresh=true").json()
    assert data["opportunities"]
    assert all(o["signal"] == "idle" for o in data["opportunities"])

    data = client.get("/api/opportunities?account=" + SUB).json()
    assert all(
        o["account_id"] == SUB or o["account_name"] == SUB
        for o in data["opportunities"]
    )


def test_api_accounts_lists_subscriptions(client):
    data = client.get("/api/accounts").json()
    assert [a["name"] for a in data["accounts"]] == [SUB]


def test_api_status_rejects_invalid(client):
    r = client.post("/api/opportunities/x/status", json={"status": "bogus"})
    assert r.status_code == 400
