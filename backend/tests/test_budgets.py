from datetime import date
from decimal import Decimal

import pytest


@pytest.fixture(scope="module")
async def cats(client):
    g = (await client.post("/api/category-groups", json={"name": "TB Group", "type": "expense"})).json()
    groc = (await client.post("/api/categories", json={"name": "TB Groceries", "group_id": g["id"]})).json()
    ins = (await client.post("/api/categories", json={"name": "TB Insurance", "group_id": g["id"]})).json()
    for d, desc, amt, cat, spread in (
        ("2025-03-02", "TB MARKET", "-150.00", groc, None),
        ("2025-03-09", "TB MARKET", "-120.00", groc, None),
        ("2025-03-10", "TB MARKET REFUND", "20.00", groc, None),
        ("2025-03-05", "TB BULK WAREHOUSE", "-600.00", groc, 3),
        ("2025-01-10", "TB STATE FARM ANNUAL", "-1200.00", ins, None),
    ):
        body = {"txn_date": d, "description": desc, "amount": amt, "category_id": cat["id"], "budget_spread_months": spread}
        assert (await client.post("/api/transactions", json=body)).status_code == 201
    return {"group": g, "groc": groc, "ins": ins}


async def test_budget_status_with_spreading(client, cats):
    r = await client.post(
        "/api/spread-rules",
        json={"name": "Annual insurance", "category_id": cats["ins"]["id"], "min_amount": "500", "months": 12},
    )
    assert r.status_code == 201, r.text
    assert r.json()["matches_12m"] >= 0
    assert (await client.post("/api/spread-rules", json={"name": "bad"})).status_code == 422

    b1 = (await client.post("/api/budgets", json={"category_id": cats["groc"]["id"], "amount": "400"})).json()
    await client.post("/api/budgets", json={"category_id": cats["ins"]["id"], "period_type": "year", "amount": "1200"})
    assert (await client.post("/api/budgets", json={"category_id": cats["groc"]["id"], "amount": "1"})).status_code == 409
    both = {"category_id": cats["groc"]["id"], "group_id": cats["group"]["id"], "amount": "1"}
    assert (await client.post("/api/budgets", json=both)).status_code == 422

    s = (await client.get("/api/budgets/status", params={"period": "month", "on": "2025-03-15"})).json()
    assert s["period"]["label"] == "March 2025" and s["period"]["elapsed"] == 1.0
    rows = {r["name"]: r for r in s["rows"]}
    assert rows["TB Groceries"]["actual"] == 450  # 150 + 120 - 20 + 600/3
    assert rows["TB Groceries"]["status"] == "over"
    assert rows["TB Insurance"]["budget"] == 100 and rows["TB Insurance"]["actual"] == 100
    assert rows["TB Insurance"]["spread_amount"] == 100

    q = (await client.get("/api/budgets/status", params={"period": "quarter", "on": "2025-02-01"})).json()
    rows = {r["name"]: r for r in q["rows"]}
    assert q["period"]["label"] == "Q1 2025"
    # The 3-month bulk buy spreads over its calendar quarter, so all 600 lands in Q1
    assert rows["TB Groceries"]["budget"] == 1200 and rows["TB Groceries"]["actual"] == 850
    assert rows["TB Insurance"]["actual"] == 300

    # Mid-period, spread shares count as already-known spending rather than being extrapolated by pace
    from ledger.budgets.service import budget_status, period_for
    from ledger.db.engine import get_sessionmaker

    async with get_sessionmaker()() as session:
        mid = await budget_status(session, period_for("month", date(2025, 3, 1)), date(2025, 3, 15))
    ins = {r["name"]: r for r in mid["rows"]}["TB Insurance"]
    assert ins["projected"] == 100 and ins["status"] == "ok"

    # The annual spread carries into the next quarter; the quarterly one stops at the quarter's end
    apr = (await client.get("/api/budgets/status", params={"period": "month", "on": "2025-04-01"})).json()
    rows = {r["name"]: r for r in apr["rows"]}
    assert rows["TB Groceries"]["actual"] == 0 and rows["TB Insurance"]["actual"] == 100

    # Group budget covers both categories; per-category budgets removed so totals don't double count
    await client.delete(f"/api/budgets/{b1['id']}")
    gb = (await client.post("/api/budgets", json={"group_id": cats["group"]["id"], "amount": "500"})).json()
    assert gb["name"] == "TB Group"
    s = (await client.get("/api/budgets/status", params={"period": "month", "on": "2025-03-15"})).json()
    rows = {r["name"]: r for r in s["rows"]}
    # Insurance (100/mo) still has its own budget, so the group row is the remainder
    g_row = rows["TB Group · everything else"]
    assert g_row["budget"] == 400 and g_row["allocated"] == 100 and g_row["total_budget"] == 500
    assert g_row["actual"] == 450  # 550 group spend minus insurance's 100
    assert g_row["category_names"] == ["TB Groceries"]
    assert rows["TB Insurance"]["actual"] == 100 and rows["TB Insurance"]["category_names"] == []
    # Hovering a budget bar lists the period's own transactions behind its categories (spread shares aren't rows)
    period = {"start": s["period"]["start"], "end": s["period"]["end"]}
    tip = (
        await client.get("/api/analytics/contributors", params={**period, "basis": "budget", "ids": g_row["category_ids"]})
    ).json()
    assert tip["count"] == 4 and tip["out"] == 870.0 and tip["in"] == 20.0

    # Overall budget: net of the group budget (which already contains the insurance budget)
    ob = (await client.post("/api/budgets", json={"amount": "1000"})).json()
    assert ob["name"] == "All spending" and ob["category_id"] is None and ob["group_id"] is None
    assert (await client.post("/api/budgets", json={"amount": "5"})).status_code == 409
    s = (await client.get("/api/budgets/status", params={"period": "month", "on": "2025-03-15"})).json()
    rows = {r["name"]: r for r in s["rows"]}
    rest = rows["Everything else"]
    # Other test modules may have added budgets; the group budget (500) must be in the allocation, insurance not twice
    assert rest["scope"] == "overall" and rest["total_budget"] == 1000 and rest["allocated"] >= 500
    assert rest["budget"] == 1000 - rest["allocated"]
    assert [r["scope"] for r in s["rows"] if r["kind"] == "expense"][-1] == "overall"
    assert s["total"]["budget"] == 1000 and s["unbudgeted"] == []
    assert (await client.get("/api/budgets")).json()[-1]["name"] == "All spending"
    await client.delete(f"/api/budgets/{ob['id']}")


async def test_budget_suggestions(client, cats):
    from ledger.budgets.service import suggest_budgets
    from ledger.db.engine import get_sessionmaker

    async with get_sessionmaker()() as session:
        out = await suggest_budgets(session, date(2025, 4, 15), months=3)
    by = {r["name"]: r for r in out}
    # Groceries now has no category budget (deleted above); insurance still does and is excluded.
    # Jan-Mar: 250 in March plus the whole 600 quarterly spread
    assert by["TB Groceries"]["monthly_average"] == Decimal("283.33") and by["TB Groceries"]["suggested"] == 290
    assert "TB Insurance" not in by


async def test_spreads_align_to_calendar_blocks(client):
    from ledger.budgets.service import actuals
    from ledger.db.engine import get_sessionmaker

    g = (await client.post("/api/category-groups", json={"name": "TB Spread Group", "type": "expense"})).json()
    tax = (await client.post("/api/categories", json={"name": "TB Car Tax", "group_id": g["id"]})).json()
    pol = (await client.post("/api/categories", json={"name": "TB Policy", "group_id": g["id"]})).json()
    for d, amt, cat, n in (
        ("2025-10-15", "-1200.00", tax, 12),
        ("2026-10-07", "-600.00", tax, 12),
        ("2025-08-01", "-600.00", pol, 6),
        ("2025-08-20", "-500.00", pol, 5),
    ):
        body = {
            "txn_date": d,
            "description": "TB SPREAD",
            "amount": amt,
            "category_id": cat["id"],
            "budget_spread_months": n,
        }
        assert (await client.post("/api/transactions", json=body)).status_code == 201

    async with get_sessionmaker()() as session:

        async def net(cat: dict, start: str, end: str) -> Decimal:
            act = await actuals(session, date.fromisoformat(start), date.fromisoformat(end))
            return act.get(cat["id"], (Decimal(0), Decimal(0)))[0]

        # Annual bills cover their own calendar year, so a year shows exactly that year's bill
        assert await net(tax, "2026-01-01", "2026-12-31") == -600
        assert await net(tax, "2025-01-01", "2025-01-31") == -100
        # 6 months: Jul-Dec half-year; 5 months doesn't tile the year so it runs Aug-Dec from the charge
        assert await net(pol, "2025-07-01", "2025-07-31") == -100
        assert await net(pol, "2025-12-01", "2025-12-31") == -200
        assert await net(pol, "2026-01-01", "2026-01-31") == 0


async def test_recurring_bill_projection(client):
    from ledger.budgets.service import budget_status, period_for
    from ledger.db.engine import get_sessionmaker

    g = (await client.post("/api/category-groups", json={"name": "TB Bills", "type": "expense"})).json()
    phone = (await client.post("/api/categories", json={"name": "TB Phone", "group_id": g["id"]})).json()
    for m in ("2022-07", "2022-08", "2022-09", "2022-10", "2022-11", "2022-12", "2023-01"):
        body = {"txn_date": f"{m}-03", "description": "TB MOBILE", "amount": "-80.00", "category_id": phone["id"]}
        assert (await client.post("/api/transactions", json=body)).status_code == 201
    b = (await client.post("/api/budgets", json={"category_id": phone["id"], "amount": "85"})).json()
    try:
        async with get_sessionmaker()() as session:
            paid = await budget_status(session, period_for("month", date(2023, 1, 1)), date(2023, 1, 10))
            due = await budget_status(session, period_for("month", date(2023, 2, 1)), date(2023, 2, 10))
            quarter = await budget_status(session, period_for("quarter", date(2023, 1, 1)), date(2023, 1, 10))
        # Paid on the 3rd: a third of the way in it is done for the month, not on pace for 3x
        row = {r["name"]: r for r in paid["rows"]}["TB Phone"]
        assert row["recurring"] and row["bills_due"] == 0 and row["projected"] == 80 and row["status"] == "ok"
        # Before this month's charge lands, the usual amount is still expected
        row = {r["name"]: r for r in due["rows"]}["TB Phone"]
        assert row["actual"] == 0 and row["bills_due"] == 80 and row["projected"] == 80
        # Later months in a quarter each expect one more charge
        row = {r["name"]: r for r in quarter["rows"]}["TB Phone"]
        assert row["bills_due"] == 160 and row["projected"] == 240 and row["status"] == "ok"
    finally:
        await client.delete(f"/api/budgets/{b['id']}")
