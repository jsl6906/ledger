from datetime import date
from decimal import Decimal as D

import pytest

from ledger.analytics.balances import account_series, month_points
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


def test_account_series_interpolates_between_anchors():
    points = [date(2019, 12, 31), date(2020, 1, 31), date(2020, 2, 29), date(2020, 3, 31)]
    anchors = [(date(2020, 1, 1), D("30000")), (date(2020, 3, 1), D("20000"))]
    assert account_series(points, anchors, interpolate=True) == [
        (None, False),  # before the first known value
        (D("30000") - D("10000") * 30 / 60, False),
        (D("30000") - D("10000") * 59 / 60, False),
        (D("20000"), False),  # held after the last
    ]


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
    car = await account("BL Car Loan", "loan")
    ira = await account("BL IRA", "retirement")
    for d, amt in (("2019-01-05", "1000.00"), ("2019-02-10", "-200.00"), ("2019-03-03", "-50.00")):
        await txn(chk, d, amt)
    await txn(sav, "2019-02-15", "25.00")

    # A home sold in March, with its mortgage paid off at the sale; both closed.
    loan = await account("BL Old Mortgage", "mortgage")
    r = await client.post(
        "/api/assets",
        json={
            "name": "BL Old Home",
            "account_type": "property",
            "purchase_date": "2019-01-01",
            "purchase_price": "300000",
            "loan_account_id": loan,
        },
    )
    assert r.status_code == 201, r.text
    home = r.json()["account_id"]
    r = await client.post(f"/api/assets/{home}/valuations", json={"value": "330000", "as_of": "2019-03-01"})
    assert r.status_code == 200, r.text
    for acct, name, kind in ((home, "BL Old Home", "property"), (loan, "BL Old Mortgage", "mortgage")):
        r = await client.put(f"/api/accounts/{acct}", json={"name": name, "account_type": kind, "is_closed": True})
        assert r.status_code == 200, r.text

    async with get_sessionmaker()() as s:
        snapshots = ((chk, date(2019, 3, 15), "1750"), (card, date(2019, 2, 20), "300"), (brk, date(2019, 3, 31), "10000"))
        snapshots += ((loan, date(2019, 1, 1), "-240000"), (loan, date(2019, 3, 1), "-230000"))
        for acct, d, bal in snapshots:
            s.add(AccountBalance(account_id=acct, as_of=d, balance=D(bal), source="manual"))
        # The IRA's feed starts mid-March; balances read off statements only count before that.
        for d, bal, source in (
            (date(2019, 3, 15), "9000", "tiller"),
            (date(2019, 2, 28), "8500", "statement"),
            (date(2019, 4, 15), "1", "statement"),
        ):
            s.add(AccountBalance(account_id=ira, as_of=d, balance=D(bal), source=source))
        closings = [
            (sav, "4321", True, date(2019, 1, 31), 5000.0),
            (sav, "4321", False, date(2019, 2, 28), 9999.0),
            (sav, "4321", True, "2019-02-30", 7777.0),  # unreadable period end
            # Loan rows never reconcile (interest), so their statements count however that check came out; signs vary.
            (car, "8290", None, date(2019, 1, 31), 12000.0),
            (car, "8290", False, date(2019, 3, 31), -11000.0),
            (ira, "5413", None, date(2019, 1, 31), 8000.0),
            (ira, "5413", None, date(2019, 3, 31), 1.0),
        ]
        for acct, ref, trusted, end, closing in closings:
            printed = end if isinstance(end, str) else end.isoformat()
            batch = ImportBatch(
                source_type="document",
                status="committed",
                doc_meta={
                    "period_end": printed,
                    "accounts": [{"ref": ref, "closing_balance": closing}],
                    "closing_balance": closing,
                },
            )
            s.add(batch)
            await s.flush()
            s.add(
                StatementCheck(
                    import_batch_id=batch.id,
                    account_ref=ref,
                    account_id=acct,
                    period_end=date(2019, 2, 28) if isinstance(end, str) else end,
                    status="ok" if trusted else "unverified",
                    trusted=trusted,
                    detail={"period_source": "rows"},
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
    # Straight lines from purchase to sale (59 days), gone once sold.
    assert rows[home]["values"] == pytest.approx([None, 300000 + 30000 * 30 / 59, 300000 + 30000 * 58 / 59, None, None])
    assert rows[loan]["values"] == pytest.approx([None, -240000 + 10000 * 30 / 59, -240000 + 10000 * 58 / 59, None, None])
    assert rows[car]["values"] == pytest.approx([None, -12000, -(12000 - 1000 * 28 / 59), -11000, -11000], abs=0.01)
    assert rows[ira]["values"] == [None, 8000.0, 8500.0, 9000.0, 9000.0]

    def total(pick):
        return [round(sum(a["values"][i] or 0 for a in out["accounts"] if pick(a)), 2) for i in range(5)]

    assert out["net_worth"] == total(lambda a: True)
    assert out["liabilities"] == [-v for v in total(lambda a: a["liability"])]
    assert out["cash"] == total(lambda a: a["account_type"] in ("checking", "savings"))
