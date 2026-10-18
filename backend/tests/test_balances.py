from datetime import date
from decimal import Decimal as D

from ledger.analytics.balances import account_series, from_purchase, month_points
from ledger.db.engine import get_sessionmaker
from ledger.models import AccountBalance, ImportBatch, StatementCheck


def test_month_points_end_at_today():
    assert month_points(date(2024, 1, 15), date(2024, 6, 30), today=date(2024, 3, 10)) == [
        date(2024, 1, 31),
        date(2024, 2, 29),
        date(2024, 3, 10),
    ]


def test_account_series_follows_ledger_around_snapshot():
    points = [date(2023, 12, 31), date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31), date(2024, 4, 30)]
    anchors = [(date(2024, 3, 15), D("1000"))]
    flows = [
        (date(2024, 1, 10), D("500")),
        (date(2024, 2, 5), D("200")),
        (date(2024, 3, 1), D("-100")),
        (date(2024, 3, 20), D("50")),
        (date(2024, 4, 2), D("-30")),
    ]
    assert account_series(points, anchors, flows) == [
        (None, False),  # before the account's first transaction
        (D("900"), True),
        (D("1100"), True),
        (D("1050"), False),
        (D("1020"), False),
    ]
    # Without ledger flows a snapshot is only carried forward.
    assert account_series(points, anchors) == [(None, False)] * 3 + [(D("1000"), False)] * 2
    # A closed account drops out after its end.
    assert account_series(points, anchors, flows, until=date(2024, 4, 2))[-1] == (None, False)
    assert account_series(points, []) == [(None, False)] * 5


def test_from_purchase_interpolates_to_first_value():
    points = [date(2019, 12, 31), date(2020, 1, 31), date(2020, 2, 29), date(2020, 3, 31)]
    series = [(None, False), (None, False), (None, False), (D("20000"), False)]
    first = (date(2020, 3, 1), D("20000"))
    assert from_purchase(points, series, (date(2020, 1, 1), D("30000")), first) == [
        (None, False),  # before the purchase
        (D("30000") - D("10000") * 30 / 60, False),
        (D("30000") - D("10000") * 59 / 60, False),
        (D("20000"), False),
    ]
    # A purchase recorded after the first value changes nothing.
    assert from_purchase(points, series, (date(2020, 6, 1), D("1")), first) == series


async def test_monthly_balances_endpoint(client):
    async def account(name, kind):
        r = await client.post("/api/accounts", json={"name": name, "account_type": kind})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def txn(acct, d, amount):
        r = await client.post(
            "/api/transactions", json={"account_id": acct, "txn_date": d, "description": "BL TXN", "amount": amount}
        )
        assert r.status_code == 201, r.text

    chk = await account("BL Checking", "checking")
    sav = await account("BL Savings", "savings")
    card = await account("BL Card", "credit_card")
    brk = await account("BL Brokerage", "investment")
    for d, amt in (("2019-01-05", "1000.00"), ("2019-02-10", "-200.00"), ("2019-03-03", "-50.00")):
        await txn(chk, d, amt)
    await txn(sav, "2019-02-15", "25.00")

    async with get_sessionmaker()() as s:
        snapshots = ((chk, date(2019, 3, 15), "1750"), (card, date(2019, 2, 20), "300"), (brk, date(2019, 3, 31), "10000"))
        for acct, d, bal in snapshots:
            s.add(AccountBalance(account_id=acct, as_of=d, balance=D(bal), source="manual"))
        for trusted, end, closing in ((True, date(2019, 1, 31), 5000.0), (False, date(2019, 2, 28), 9999.0)):
            batch = ImportBatch(
                source_type="document",
                status="committed",
                doc_meta={"accounts": [{"ref": "4321", "closing_balance": closing}], "closing_balance": closing},
            )
            s.add(batch)
            await s.flush()
            s.add(
                StatementCheck(
                    import_batch_id=batch.id,
                    account_ref="4321",
                    account_id=sav,
                    period_end=end,
                    status="ok",
                    trusted=trusted,
                    detail={"period_source": "statement"},
                )
            )
        await s.commit()

    r = await client.get("/api/analytics/balances", params={"start": "2018-12-01", "end": "2019-04-30"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["months"] == ["2018-12-01", "2019-01-01", "2019-02-01", "2019-03-01", "2019-04-01"]
    rows = {a["id"]: a for a in out["accounts"]}
    assert rows[chk]["values"] == [None, 2000.0, 1800.0, 1750.0, 1750.0]
    assert rows[chk]["estimated"] == [False, True, True, False, False]
    # The untrusted statement's closing balance is ignored.
    assert rows[sav]["values"] == [None, 5000.0, 5025.0, 5025.0, 5025.0]
    assert rows[card]["values"] == [None, None, -300.0, -300.0, -300.0] and rows[card]["liability"]
    assert rows[brk]["values"] == [None, None, None, 10000.0, 10000.0]

    def total(pick):
        return [round(sum(a["values"][i] or 0 for a in out["accounts"] if pick(a)), 2) for i in range(5)]

    assert out["net_worth"] == total(lambda a: True)
    assert out["liabilities"] == [-v for v in total(lambda a: a["liability"])]
    assert out["cash"] == total(lambda a: a["account_type"] in ("checking", "savings"))
