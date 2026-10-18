import base64
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from ledger.db.engine import get_sessionmaker
from ledger.models import Account, AccountBalance, DuplicatePair, ImportBatch, ImportRow, Source, Transaction
from ledger.services.normalize import normalize_merchant
from ledger.sources import simplefin, tiller
from ledger.sources.jobs import run_sync
from ledger.sources.service import apply_feed

TODAY = date.today()


def _us(d: date) -> str:
    return f"{d.month}/{d.day}/{d.year}"


@pytest.fixture
def no_ai(monkeypatch):
    from ledger.ai import imports as ai_imports

    verdicts: dict[str, float] = {}

    async def fake_adjudicate(pairs):
        out = {}
        for p in pairs:
            prob = verdicts.get(p["b"]["description"])
            if prob is not None:
                out[p["id"]] = ai_imports.PairVerdict(pair_id=p["id"], probability_same=prob, reason="test")
        return out

    async def fake_labels(labels, taxonomy):
        return []

    monkeypatch.setattr(ai_imports, "adjudicate_pairs", fake_adjudicate)
    monkeypatch.setattr(ai_imports, "map_category_labels", fake_labels)
    return verdicts


async def _source(kind: str, **config) -> int:
    async with get_sessionmaker()() as s:
        src = await s.scalar(select(Source).where(Source.kind == kind))
        if src is None:
            src = Source(kind=kind, name=kind.title(), config=config)
            s.add(src)
        await s.commit()
        return src.id


def _tiller_row(d, desc, amount, tid, category="", account="TS Checking", number="xxxx4821"):
    return {
        "Date": _us(d),
        "Description": desc,
        "Category": category,
        "Amount": amount,
        "Account": account,
        "Account #": number,
        "Institution": "TS Bank",
        "Transaction ID": tid,
        "Account ID": f"acct-{account}",
        "Full Description": f"{desc} FULL",
        "Check Number": "",
    }


async def test_tiller_feed_sync(client, no_ai):
    g = (await client.post("/api/category-groups", json={"name": "TS Group"})).json()
    cat = (await client.post("/api/categories", json={"name": "TS Coffee", "group_id": g["id"]})).json()
    d = TODAY - timedelta(days=3)
    rows = [
        _tiller_row(d, "TS BEANERY", -4.5, "tid-1", "TS Coffee"),
        _tiller_row(d, "TS PAYCHECK", 2500, "tid-2"),
        _tiller_row(TODAY - timedelta(days=400), "TS OLD", -1, "tid-old"),
        {"Date": "", "Description": "", "Amount": "", "Transaction ID": ""},
    ]
    balances = [
        {
            "Date": _us(d),
            "Account": "TS Checking",
            "Account #": "xxxx4821",
            "Institution": "TS Bank",
            "Account ID": "acct-TS Checking",
            "Balance": 1834.12,
        },
    ]
    feed = tiller.build_feed(rows, balances, since=TODAY - timedelta(days=60))
    assert [t.external_id for t in feed.transactions] == ["tid-1", "tid-2"]
    assert feed.transactions[0].amount == Decimal("-4.50")

    sid = await _source("tiller", sheet_id="x" * 30)
    async with get_sessionmaker()() as s:
        res = await apply_feed(s, await s.get(Source, sid), feed)
    assert res["new"] == 2 and res["inserted"] == 2, res

    async with get_sessionmaker()() as s:
        acct = await s.scalar(select(Account).where(Account.name == "TS Checking"))
        assert acct.external_refs["tiller"] == "acct-TS Checking" and acct.mask == "4821"
        txns = (await s.scalars(select(Transaction).where(Transaction.external_id.in_(["tid-1", "tid-2"])))).unique().all()
        assert {t.source_type for t in txns} == {"tiller"}
        assert next(t for t in txns if t.external_id == "tid-1").category_id == cat["id"]
        assert (await s.scalar(select(AccountBalance.balance).where(AccountBalance.account_id == acct.id))) == Decimal(
            "1834.12"
        )

    # Re-sync is idempotent; a category assigned later in Tiller fills the uncategorized row
    rows[1]["Category"] = "TS Coffee"
    async with get_sessionmaker()() as s:
        res = await apply_feed(s, await s.get(Source, sid), tiller.build_feed(rows, balances, None))
    assert res["new"] == 1 and res["categories_filled"] == 1  # only the >60-day-old row is new when not limited

    bal = (await client.get("/api/balances")).json()
    assert any(a["account"] == "TS Checking" and a["balance"] == 1834.12 for a in bal["accounts"])
    trend = (await client.get("/api/balances/trend")).json()
    assert trend[str(acct.id)] == [{"as_of": d.isoformat(), "balance": 1834.12}]

    # A retired feed account is skipped entirely: no account, balances or transactions.
    retired = [_tiller_row(d, "TS RETIRED", -3, "tid-retired", account="TS Old Card", number="9911")]
    old_bal = [{**balances[0], "Account": "TS Old Card", "Account ID": "acct-TS Old Card", "Account #": "9911"}]
    async with get_sessionmaker()() as s:
        src = await s.get(Source, sid)
        src.config = {**src.config, "ignored_accounts": ["acct-TS Old Card"]}
        await s.commit()
        res = await apply_feed(s, await s.get(Source, sid), tiller.build_feed(retired, old_bal, None))
    assert res["accounts"] == 0 and res["new"] == 0
    async with get_sessionmaker()() as s:
        assert await s.scalar(select(Account).where(Account.name == "TS Old Card")) is None


async def test_feed_duplicates_against_manual_entries(client, no_ai):
    acct = (
        await client.post("/api/accounts", json={"name": "TS Card", "account_type": "credit_card", "mask": "6173"})
    ).json()
    d = TODAY - timedelta(days=2)
    for desc, amt in (("TS GROCER", "-52.10"), ("TS HARDWARE STORE", "-19.99")):
        r = await client.post(
            "/api/transactions",
            json={"txn_date": d.isoformat(), "description": desc, "amount": amt, "account_id": acct["id"]},
        )
        assert r.status_code == 201
    # Same amount and day in another account (the other leg of a transfer) is not a duplicate of a synced row.
    other = (await client.post("/api/accounts", json={"name": "TS Other Checking"})).json()
    r = await client.post(
        "/api/transactions",
        json={"txn_date": d.isoformat(), "description": "TS FRESH", "amount": "-8.37", "account_id": other["id"]},
    )
    assert r.status_code == 201
    sid = await _source("simplefin")
    feed = tiller.build_feed(
        [
            _tiller_row(d, "TS GROCER", -52.10, "dup-exact", account="Visa 6173", number="6173"),
            _tiller_row(d, "HARDWARE #22 TS", -19.99, "dup-fuzzy", account="Visa 6173", number="6173"),
            _tiller_row(d, "TS FRESH", -8.37, "new-1", account="Visa 6173", number="6173"),
        ],
        [],
        None,
    )
    async with get_sessionmaker()() as s:
        res = await apply_feed(s, await s.get(Source, sid), feed)
    # Matched to the existing card by last-4; the exact duplicate is skipped, the fuzzy one needs a person
    assert res["needs_review"] == 1 and res["inserted"] == 0
    async with get_sessionmaker()() as s:
        linked = await s.get(Account, acct["id"])
        assert linked.external_refs.get("simplefin") == "acct-Visa 6173"
        batch = await s.get(ImportBatch, res["batch_id"])
        assert batch.status == "review" and batch.origin == "simplefin"

    # The pending batch's rows aren't re-queued on the next sync; with a confident AI verdict it auto-commits
    async with get_sessionmaker()() as s:
        again = await apply_feed(s, await s.get(Source, sid), feed)
    assert again["new"] == 0
    await client.delete(f"/api/imports/{res['batch_id']}")
    no_ai["HARDWARE #22 TS"] = 0.97
    async with get_sessionmaker()() as s:
        res = await apply_feed(s, await s.get(Source, sid), feed)
    assert res["inserted"] == 1 and res["skipped_duplicates"] == 2


async def test_feed_resolves_certain_matches_and_remembers_skips(client, no_ai, monkeypatch):
    from ledger.ai import imports as ai_imports

    acct = (
        await client.post("/api/accounts", json={"name": "TS Amex", "account_type": "credit_card", "mask": "3005"})
    ).json()
    d = TODAY - timedelta(days=2)
    async with get_sessionmaker()() as s:
        for day, desc, merchant, amount, source in (
            # Another feed's copy of the same charges, with its own description format.
            (d, "TS DELI 4417 SPRINGFIELD VA", "TS DELI", "-7.25", "tiller"),
            (d - timedelta(days=1), "TS BOOKS ONLINE", "TS BOOKS", "-12.00", "tiller"),
            # An earlier charge from this same feed: a same-day twin may be a repeat purchase.
            (d, "TS CAFE 22", "TS CAFE", "-3.10", "simplefin"),
        ):
            s.add(
                Transaction(
                    account_id=acct["id"],
                    txn_date=day,
                    amount=Decimal(amount),
                    description=desc,
                    merchant=normalize_merchant(merchant),
                    source_type=source,
                )
            )
        await s.commit()

    sent: list[dict] = []

    async def capture(pairs):
        sent.extend(pairs)
        return {}

    monkeypatch.setattr(ai_imports, "adjudicate_pairs", capture)
    sid = await _source("simplefin")
    feed = tiller.build_feed(
        [
            _tiller_row(d, "TS DELI", -7.25, "sf-deli", account="Amex 3005", number="3005"),
            _tiller_row(d, "TS BOOKS", -12.00, "sf-books", account="Amex 3005", number="3005"),
            _tiller_row(d, "TS CAFE", -3.10, "sf-cafe", account="Amex 3005", number="3005"),
        ],
        [],
        None,
    )
    async with get_sessionmaker()() as s:
        res = await apply_feed(s, await s.get(Source, sid), feed)
    batch_id = res["batch_id"]
    async with get_sessionmaker()() as s:
        rows = {r.external_id: r for r in (await s.scalars(select(ImportRow).where(ImportRow.batch_id == batch_id))).all()}
        scores = dict(
            (
                await s.execute(
                    select(ImportRow.external_id, DuplicatePair.score)
                    .join(DuplicatePair, DuplicatePair.import_row_id == ImportRow.id)
                    .where(ImportRow.batch_id == batch_id)
                )
            ).all()
        )
    # Near-certain cross-feed match is skipped without AI; the same-feed twin and the weaker match wait for a person.
    assert scores["sf-deli"] >= Decimal("0.95") and scores["sf-cafe"] >= Decimal("0.95"), scores
    assert rows["sf-deli"].decision == "skip_duplicate"
    assert rows["sf-cafe"].decision == "pending" and rows["sf-books"].decision == "pending"
    # Only the weaker match went to the AI, with each side's source.
    assert [(p["a"]["source"], p["b"]["source"]) for p in sent] == [("tiller", "simplefin")]
    assert "source: simplefin" in ai_imports._side(sent[0]["b"])

    r = await client.post(
        f"/api/imports/{batch_id}/rows/decisions",
        json={"row_ids": [rows["sf-cafe"].id, rows["sf-books"].id], "decision": "skip_duplicate"},
    )
    assert r.status_code == 200, r.text
    async with get_sessionmaker()() as s:
        res = await apply_feed(s, await s.get(Source, sid), feed)
    assert res["new"] == 0 and res["skipped_duplicates"] == 3
    async with get_sessionmaker()() as s:
        assert (await s.get(ImportBatch, batch_id)).status == "committed"
    # Rows the committed batch skipped are remembered, not fetched and reviewed again on every sync.
    async with get_sessionmaker()() as s:
        res = await apply_feed(s, await s.get(Source, sid), feed)
    assert res["new"] == 0 and "batch_id" not in res


async def _held_feed_batch(client, mask: str, held_desc: str, rows: list[tuple[str, str]]) -> tuple[int, int, date]:
    """A SimpleFIN batch kept in review by one ambiguous row, plus `rows` (description, amount) set to be inserted."""
    acct = (await client.post("/api/accounts", json={"name": f"TS Savings {mask}", "mask": mask})).json()
    d = TODAY - timedelta(days=2)
    r = await client.post(
        "/api/transactions",
        json={
            "txn_date": (d - timedelta(days=1)).isoformat(),
            "description": held_desc,
            "amount": "-40.00",
            "account_id": acct["id"],
        },
    )
    assert r.status_code == 201
    feed = tiller.build_feed(
        [
            _tiller_row(d, held_desc, -40.00, f"sf-held-{mask}", account=f"Sav {mask}", number=mask),
            *(
                _tiller_row(d, desc, float(amt), f"sf-{mask}-{i}", account=f"Sav {mask}", number=mask)
                for i, (desc, amt) in enumerate(rows)
            ),
        ],
        [],
        None,
    )
    async with get_sessionmaker()() as s:
        res = await apply_feed(s, await s.get(Source, await _source("simplefin")), feed)
    assert res["needs_review"] == 1, res
    return res["batch_id"], acct["id"], d


async def _tiller_adds(mask: str, rows: list[tuple[date, str, str]]) -> None:
    """The other feed inserts the same charges while the SimpleFIN batch waits."""
    feed = tiller.build_feed(
        [
            _tiller_row(day, desc, float(amt), f"t-{mask}-{i}", account=f"Sav {mask}", number=mask)
            for i, (day, desc, amt) in enumerate(rows)
        ],
        [],
        None,
    )
    async with get_sessionmaker()() as s:
        res = await apply_feed(s, await s.get(Source, await _source("tiller", sheet_id="z" * 30)), feed)
    assert res["inserted"] == len(rows), res


async def _decisions(batch_id: int) -> dict[str, str]:
    async with get_sessionmaker()() as s:
        rows = (await s.scalars(select(ImportRow).where(ImportRow.batch_id == batch_id))).all()
    return {r.description: r.decision for r in rows}


async def test_commit_rechecks_rows_against_transactions_added_while_waiting(client, no_ai):
    batch_id, acct_id, d = await _held_feed_batch(
        client, "7781", "TS GYM", [("TS PENNYMAC", "-3135.86"), ("TS GROCER BEE", "-53.67")]
    )
    assert await _decisions(batch_id) == {"TS GYM": "pending", "TS PENNYMAC": "insert", "TS GROCER BEE": "insert"}
    await _tiller_adds("7781", [(d, "TS PENNYMAC", "-3135.86"), (d + timedelta(days=1), "TS GROCER BEE", "-53.67")])

    # The certain match is skipped; the weaker one is refused until a person decides it.
    r = await client.post(f"/api/imports/{batch_id}/commit", json={"pending_as": "skip"})
    assert r.status_code == 409 and "1 row now matches" in r.json()["detail"], r.text
    assert await _decisions(batch_id) == {"TS GYM": "pending", "TS PENNYMAC": "skip_duplicate", "TS GROCER BEE": "pending"}
    pairs = (await client.get(f"/api/imports/{batch_id}/duplicates")).json()
    assert {p["row"]["description"] for p in pairs if p["status"] == "pending"} == {"TS GYM", "TS GROCER BEE"}

    r = await client.post(f"/api/imports/{batch_id}/commit", json={"pending_as": "skip"})
    assert r.status_code == 200, r.text
    assert r.json()["stats"]["inserted"] == 0
    async with get_sessionmaker()() as s:
        copies = (
            await s.scalars(
                select(Transaction.source_type).where(
                    Transaction.account_id == acct_id, Transaction.amount == Decimal("-3135.86")
                )
            )
        ).all()
    assert copies == ["tiller"]


async def test_sources_review_reflects_live_batches(client, no_ai):
    from ledger.sources.service import record_run

    batch_id, _, _ = await _held_feed_batch(client, "7783", "TS SPA", [("TS CAFE", "-6.10"), ("TS BOOKS", "-19.99")])
    async with get_sessionmaker()() as s:
        src = await s.get(Source, await _source("simplefin"))
        await record_run(s, src, {"needs_review": 1, "batch_id": batch_id, "new": 3}, None)

    async def simplefin_out():
        return next(x for x in (await client.get("/api/sources")).json()["sources"] if x["kind"] == "simplefin")

    sf = await simplefin_out()
    assert sf["last_status"] == "needs_review" and sf["review"]["pending"] >= 1 and sf["review"]["waiting"] >= 2

    # Committed on the import screen: the sync's saved result no longer holds the pane in review.
    r = await client.post(f"/api/imports/{batch_id}/commit", json={"pending_as": "skip"})
    assert r.status_code == 200, r.text
    sf = await simplefin_out()
    if sf["review"] is None:
        assert sf["last_status"] == "ok"
    else:  # another test's feed batch still waiting
        assert sf["review"]["batch_id"] != batch_id and sf["last_status"] == "needs_review"


async def test_sync_rechecks_waiting_batch_before_committing(client, no_ai):
    batch_id, _, d = await _held_feed_batch(client, "7782", "TS POOL", [("TS LENDER", "-900.00"), ("TS BAKERY", "-8.25")])
    await _tiller_adds("7782", [(d, "TS LENDER", "-900.00"), (d + timedelta(days=1), "TS BAKERY", "-8.25")])

    no_ai["TS BAKERY"] = 0.97
    async with get_sessionmaker()() as s:
        res = await apply_feed(s, await s.get(Source, await _source("simplefin")), tiller.build_feed([], [], None))
    # The new match went to the AI and was skipped; the original ambiguous row still needs a person.
    assert res.get("needs_review") == 1 and res["batch_id"] == batch_id, res
    assert await _decisions(batch_id) == {"TS POOL": "pending", "TS LENDER": "skip_duplicate", "TS BAKERY": "skip_duplicate"}
    r = await client.post(f"/api/imports/{batch_id}/commit", json={"pending_as": "skip"})
    assert r.status_code == 200 and r.json()["stats"]["inserted"] == 0, r.text


def test_tiller_rows_without_ids_get_stable_ids():
    d = date(2012, 5, 4)
    live = _tiller_row(TODAY, "TS NEW", -1.0, "tid-live")
    manual = {**_tiller_row(d, "TS DINER", -12.5, ""), "Account ID": "", "Account #": ""}
    twin = dict(manual)
    feed = tiller.build_feed([live, manual, twin], [], None)
    ids = [t.external_id for t in feed.transactions]
    assert ids[0] == "tid-live" and ids[1].startswith("tiller:") and ids[1] != ids[2]
    assert ids == [t.external_id for t in tiller.build_feed([live, manual, twin], [], None).transactions]
    # The hand-entered rows share the account id Tiller assigned to the same account
    assert {t.account_key for t in feed.transactions} == {"acct-TS Checking"}
    assert feed.synthetic_ids == 2 and feed.warnings == []


def test_simplefin_token_and_feed():
    token = base64.b64encode(b"https://bridge.example.org/simplefin/claim/abc").decode()
    assert simplefin.claim_url(token) == "https://bridge.example.org/simplefin/claim/abc"
    with pytest.raises(simplefin.SimpleFinError):
        simplefin.claim_url(base64.b64encode(b"http://insecure.example.org/claim").decode())

    payload = {
        "errors": ["Connection to Old Bank needs attention"],
        "accounts": [
            {
                "org": {"name": "SF Brokerage", "domain": "sfb.example"},
                "id": "ACT-1",
                "name": "Individual (5555)",
                "currency": "USD",
                "balance": "10250.55",
                "available-balance": "0",
                "balance-date": 1790000000,
                "transactions": [
                    {"id": "T1", "posted": 1789900000, "amount": "-25.00", "description": "SF FEE", "payee": ""},
                    {"id": "T2", "posted": 0, "amount": "-1.00", "description": "PENDING", "pending": True},
                ],
                "holdings": [
                    {
                        "id": "H1",
                        "symbol": "VTI",
                        "description": "Vanguard Total",
                        "shares": "10",
                        "market_value": "2800.10",
                        "cost_basis": "2000",
                    },
                ],
            }
        ],
    }
    feed = simplefin.build_feed(payload)
    assert feed.warnings == ["Connection to Old Bank needs attention"]
    assert [t.external_id for t in feed.transactions] == ["sfin:ACT-1:T1"]
    assert feed.balances[0].balance == Decimal("10250.55")
    assert feed.holdings[0].symbol == "VTI" and feed.holdings[0].shares == Decimal("10")
    assert feed.accounts["ACT-1"].institution == "SF Brokerage"

    v2 = simplefin.build_feed(
        {
            "errlist": [{"code": "con.auth", "msg": "Authentication failed for My Bank", "conn_id": "C1"}],
            "connections": [{"conn_id": "C1", "name": "My Bank - Jo", "org_name": "My Bank", "org_id": "O1"}],
            "accounts": [
                {
                    "id": "A",
                    "conn_id": "C1",
                    "name": "Checking",
                    "balance": "1",
                    "balance-date": 1790000000,
                    "transactions": [{"id": "X", "posted": 1789900000, "amount": "1.00", "description": "D"}],
                },
            ],
        }
    )
    assert v2.warnings == ["Authentication failed for My Bank"]
    assert v2.accounts["C1/A"].institution == "My Bank"
    assert v2.transactions[0].external_id == "sfin:C1/A:X"


async def test_simplefin_connect_and_sync(client, no_ai, monkeypatch):
    async def fake_claim(token):
        return "https://user:pass@bridge.example.org/simplefin"

    seen = {}

    async def fake_fetch(access_url, config, full=False):
        seen["url"] = access_url
        return simplefin.build_feed(
            {
                "accounts": [
                    {
                        "org": {"name": "SF Bank"},
                        "id": "ACT-9",
                        "name": "SF Savings (7070)",
                        "balance": "500.00",
                        "balance-date": 1790000000,
                        "transactions": [
                            {"id": "SF-T1", "posted": 1789990000, "amount": "12.00", "description": "INTEREST"}
                        ],
                        "holdings": [{"id": "HX", "symbol": "CASH", "market_value": "500"}],
                    }
                ]
            }
        )

    monkeypatch.setattr(simplefin, "claim", fake_claim)
    monkeypatch.setattr(simplefin, "fetch", fake_fetch)
    r = await client.post("/api/sources/simplefin/claim", json={"setup_token": "x" * 40})
    assert r.status_code == 200, r.text
    src = r.json()
    assert src["connected"] and "secret" not in src

    async with get_sessionmaker()() as s:
        stored = await s.get(Source, src["id"])
        assert "pass" not in stored.secret  # encrypted at rest
        res = await run_sync(s, src["id"])
    assert seen["url"] == "https://user:pass@bridge.example.org/simplefin"
    assert res["inserted"] == 1 and res["holdings"] == 1

    listed = (await client.get("/api/sources")).json()["sources"]
    sf = next(x for x in listed if x["kind"] == "simplefin")
    assert sf["last_status"] == "ok" and sf["linked_accounts"] >= 1
    holdings = (await client.get("/api/holdings")).json()
    assert any(h["symbol"] == "CASH" and h["account"] == "SF Savings (7070)" for h in holdings)


async def test_stale_accounts_listed(client, no_ai):
    from datetime import UTC, datetime

    sid = await _source("tiller", sheet_id="z" * 30)
    synced = date(2026, 3, 20)
    balances = [
        {
            "Date": _us(d),
            "Account": name,
            "Account #": num,
            "Institution": "ST Bank",
            "Account ID": f"st-{name}",
            "Balance": 10,
        }
        for name, num, d in (
            ("ST Fresh", "1111", synced - timedelta(days=1)),
            ("ST Lagging", "2222", synced - timedelta(days=20)),
            ("ST Closed", "3333", synced - timedelta(days=40)),
        )
    ]
    async with get_sessionmaker()() as s:
        src = await s.get(Source, sid)
        await apply_feed(s, src, tiller.build_feed([], balances, None))
        src.last_sync_at = datetime(2026, 3, 20, 16, tzinfo=UTC)
        await s.execute(update(Account).where(Account.name == "ST Closed").values(is_closed=True))
        await s.commit()

    listed = (await client.get("/api/sources")).json()["sources"]
    stale = next(x for x in listed if x["kind"] == "tiller")["stale_accounts"]
    st = [a for a in stale if a["name"].startswith("ST ")]
    assert [(a["name"], a["mask"], a["institution"], a["last_refreshed"]) for a in st] == [
        ("ST Lagging", "2222", "ST Bank", (synced - timedelta(days=20)).isoformat())
    ]


async def test_sync_failure_recorded(client, monkeypatch):
    async def boom(config, full=False):
        raise tiller.TillerError("Google returned 403: share the sheet")

    monkeypatch.setattr(tiller, "fetch", boom)
    sid = await _source("tiller", sheet_id="y" * 30)
    async with get_sessionmaker()() as s:
        with pytest.raises(tiller.TillerError):
            await run_sync(s, sid)
    async with get_sessionmaker()() as s:
        src = await s.get(Source, sid)
        assert src.last_status == "failed" and "403" in src.last_error
