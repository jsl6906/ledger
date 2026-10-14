from datetime import date

import pytest


@pytest.fixture(scope="module")
async def data(client):
    checking = (await client.post("/api/accounts", json={"name": "TT Checking"})).json()
    savings = (await client.post("/api/accounts", json={"name": "TT Savings", "account_type": "savings"})).json()
    hidden = (await client.post("/api/accounts", json={"name": "TT Hidden", "is_hidden": True})).json()
    cats = {c["name"]: c for c in (await client.get("/api/categories")).json()}
    g = (await client.post("/api/category-groups", json={"name": "TT Group"})).json()
    misc = (await client.post("/api/categories", json={"name": "TT Misc", "group_id": g["id"]})).json()
    transfer = cats["Transfer"]["id"]

    async def add(d, desc, amt, acct, cat=None):
        body = {"txn_date": d, "description": desc, "amount": amt, "account_id": acct["id"], "category_id": cat}
        r = await client.post("/api/transactions", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    rows = {
        "out1": await add("2019-02-05", "TT MOVE 731", "-731.00", checking, transfer),
        "in1": await add("2019-02-07", "TT MOVE IN 731", "731.00", savings),
        "out2": await add("2019-02-10", "TT MOVE 312", "-312.00", checking, transfer),
        "in2": await add("2019-02-10", "TT MOVE IN 312", "312.00", savings, transfer),
        "gap": await add("2019-02-15", "TT GAP 457", "-457.00", checking, transfer),
        "hint": await add("2019-02-20", "TT HINT 623", "-623.00", checking, transfer),
        "hint_other": await add("2019-02-21", "TT HINT OTHER 623", "623.00", savings, misc["id"]),
        "dup_a": await add("2019-02-24", "TT DUP 811", "-811.00", checking, transfer),
        "dup_b": await add("2019-02-26", "TT DUP 811", "-811.00", checking, transfer),
        "dup_in": await add("2019-02-26", "TT DUP IN 811", "811.00", savings),
        "to_hidden": await add("2019-02-12", "TT TO HIDDEN 205", "-205.00", checking, transfer),
        "in_hidden": await add("2019-02-12", "TT IN HIDDEN 205", "205.00", hidden),
        "promo_out": await add("2019-02-14", "TT PROMO MOVED 88", "-88.00", checking, transfer),
        "promo_in": await add("2019-02-14", "TT PROMO ENDED 88", "88.00", checking, transfer),
    }
    return rows


async def _feb(client) -> dict:
    return (await client.get("/api/analytics/cashflow", params={"start": "2019-02-01", "end": "2019-02-28"})).json()[0]


async def test_transfer_matching(client, data):
    from sqlalchemy import select, text

    from ledger.analytics.anomalies import detect
    from ledger.db.engine import get_sessionmaker
    from ledger.models import Transaction

    before = await _feb(client)
    async with get_sessionmaker()() as session:
        await detect(session, date(2019, 2, 1))
        ids = [r["id"] for r in data.values()]
        pairs = dict(
            (
                await session.execute(select(Transaction.id, Transaction.transfer_match_id).where(Transaction.id.in_(ids)))
            ).all()
        )
    d = {k: v["id"] for k, v in data.items()}
    assert pairs[d["out1"]] == d["in1"] and pairs[d["in1"]] == d["out1"]
    assert pairs[d["out2"]] == d["in2"] and pairs[d["dup_b"]] == d["dup_in"]
    assert pairs[d["gap"]] is None and pairs[d["hint"]] is None and pairs[d["hint_other"]] is None
    assert pairs[d["dup_a"]] is None
    assert pairs[d["to_hidden"]] == d["in_hidden"]
    assert pairs[d["promo_out"]] == d["promo_in"]  # same-day offset within one account

    # Pairs between visible accounts drop out of reports; unmatched transfers and pairs with a hidden account count by sign.
    after = await _feb(client)
    assert before["income"] - after["income"] == pytest.approx(731 + 312 + 811 + 88)
    assert before["expenses"] - after["expenses"] == pytest.approx(731 + 312 + 811 + 88)

    def listed(page: dict) -> set[int]:
        return {t["id"] for t in page["items"]}

    feb = {"start": "2019-02-01", "end": "2019-02-28", "q": "TT ", "limit": 1000}
    shown = listed((await client.get("/api/transactions", params=feb)).json())
    internal = {d[k] for k in ("out1", "in1", "out2", "in2", "dup_b", "dup_in", "promo_out", "promo_in")}
    assert not shown & internal
    assert {d[k] for k in ("gap", "hint", "dup_a", "to_hidden")} <= shown
    assert d["in_hidden"] not in shown
    everything = listed((await client.get("/api/transactions", params=feb | {"include_hidden": True})).json())
    assert internal | {d["in_hidden"]} <= everything

    # The chat views flag the same rows; hidden-account rows are never reportable.
    async with get_sessionmaker()() as session:
        sql = text("SELECT id, reportable FROM v_transactions WHERE id = ANY(:ids)")
        reportable = dict((await session.execute(sql, {"ids": ids})).all())
    assert {i for i, r in reportable.items() if not r} == internal | {d["in_hidden"]}

    flagged = [
        a
        for a in (await client.get("/api/anomalies")).json()
        if a["period"] == "2019-02-01" and a["kind"] == "unmatched_transfer"
    ]
    by_id = {a["transaction_id"]: a for a in flagged}
    assert data["gap"]["id"] in by_id and "No opposite $457.00" in by_id[data["gap"]["id"]]["detail"]
    assert data["hint"]["id"] in by_id and "TT Misc" in by_id[data["hint"]["id"]]["detail"]
    assert data["dup_b"]["id"] not in by_id  # closest-date leg wins
    assert data["dup_a"]["id"] in by_id
    for k in ("out1", "in1", "out2", "in2"):
        assert data[k]["id"] not in by_id
    assert len(by_id) == 3

    # Recategorizing the other side as a transfer pairs it and withdraws the finding.
    cats = {c["name"]: c for c in (await client.get("/api/categories")).json()}
    r = await client.patch(f"/api/transactions/{data['hint_other']['id']}", json={"category_id": cats["Transfer"]["id"]})
    assert r.status_code == 200, r.text
    async with get_sessionmaker()() as session:
        res = await detect(session, date(2019, 2, 1))
    assert res["withdrawn"] == 1
