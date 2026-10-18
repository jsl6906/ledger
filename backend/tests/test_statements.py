from ledger.jobs.worker import JobContext


async def _no_progress(self, fraction, message=None):
    return None


async def test_bill_upload_suggest_and_approve(client, monkeypatch):
    import ledger.jobs.worker as worker
    from ledger.ai import statements as ai_st
    from ledger.statements.jobs import extract_bill_job

    monkeypatch.setattr(worker.JobContext, "progress", _no_progress)
    acct = (await client.post("/api/accounts", json={"name": "Bills Checking"})).json()
    pay = (
        await client.post(
            "/api/transactions",
            json={
                "txn_date": "2026-09-25",
                "description": "CITY OF SPRINGFIELD WATER AUTOPAY",
                "amount": "-84.12",
                "account_id": acct["id"],
            },
        )
    ).json()
    decoy = (
        await client.post(
            "/api/transactions",
            json={"txn_date": "2026-09-20", "description": "SHELL OIL", "amount": "-84.12", "account_id": acct["id"]},
        )
    ).json()

    async def fake_extract(data, mime_type, filename):
        return ai_st.BillExtraction(
            document_type="utility_bill",
            vendor="City of Springfield Water",
            service_type="Water",
            account_last4="0042",
            statement_date="2026-09-15",
            period_start="2026-08-14",
            period_end="2026-09-13",
            due_date="2026-09-30",
            amount_due=84.12,
            usage=[
                ai_st.UsageMetric(metric="water_usage", value=5380, unit="gal", is_primary=True),
                ai_st.UsageMetric(metric="sewer_usage", value=5000, unit="gal", is_primary=False),
            ],
            summary="City water bill Aug-Sep 2026",
        )

    monkeypatch.setattr(ai_st, "extract_bill", fake_extract)

    r = await client.post("/api/statements", files={"file": ("CityWater_2026-09.pdf", b"%PDF-1.7 water", "application/pdf")})
    assert r.status_code == 201, r.text
    st = r.json()
    assert st["status"] == "processing"
    await extract_bill_job(JobContext(0, "extract_bill", {"statement_id": st["id"]}))

    st = (await client.get(f"/api/statements/{st['id']}")).json()
    assert st["status"] == "suggested"
    sug = st["suggestion"]
    assert sug["series_id"] is None and sug["new_series_name"] == "Water"
    assert sug["new_series_category_id"] is not None  # seeded "Water" category
    assert sug["candidates"][0]["transaction_id"] == pay["id"]
    assert sug["transaction_ids"] == [pay["id"]]
    assert decoy["id"] in [c["transaction_id"] for c in sug["candidates"]]

    r = await client.post(f"/api/statements/{st['id']}/approve", json={"transaction_ids": sug["transaction_ids"]})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["status"] == "approved" and st["series_name"] == "Water"
    assert [t["id"] for t in st["transactions"]] == [pay["id"]]

    t = (await client.get(f"/api/transactions/{pay['id']}")).json()
    assert t["has_statement"] is True
    assert "Water" in [tg["name"] for tg in t["tags"]]
    assert t["category_name"] == "Water"

    series = next(s for s in (await client.get("/api/statement-series")).json() if s["name"] == "Water")
    assert series["statement_count"] == 1 and series["primary_metric"] == "water_usage" and series["unit"] == "gal"
    hist = (await client.get(f"/api/statement-series/{series['id']}/history")).json()
    assert hist[0]["usage_value"] == "5380.000" and hist[0]["cost_per_unit"] == "0.0156"

    page = (await client.get("/api/transactions", params={"status": "with_statement"})).json()
    with_statement = [i["id"] for i in page["items"]]
    assert pay["id"] in with_statement and decoy["id"] not in with_statement
    linked = (await client.get(f"/api/statements/for-transaction/{pay['id']}")).json()
    assert linked[0]["id"] == st["id"]

    # Next month's bill is matched to the existing series
    async def fake_extract2(data, mime_type, filename):
        return ai_st.BillExtraction(
            document_type="utility_bill",
            vendor="City of Springfield Water",
            service_type="Water",
            statement_date="2026-10-15",
            due_date="2026-10-30",
            amount_due=81.00,
            usage=[],
            summary="Oct bill",
        )

    monkeypatch.setattr(ai_st, "extract_bill", fake_extract2)
    r = await client.post("/api/statements", files={"file": ("CityWater_2026-10.pdf", b"%PDF-1.7 oct", "application/pdf")})
    await extract_bill_job(JobContext(0, "extract_bill", {"statement_id": r.json()["id"]}))
    st2 = (await client.get(f"/api/statements/{r.json()['id']}")).json()
    assert st2["suggestion"]["series_id"] == series["id"]
    st2 = (await client.post(f"/api/statements/{st2['id']}/approve", json={"transaction_ids": []})).json()
    assert st2["series_id"] == series["id"]


async def test_one_off_receipt_skips_series(client, monkeypatch):
    import ledger.jobs.worker as worker
    from ledger.ai import statements as ai_st
    from ledger.statements.jobs import extract_bill_job

    monkeypatch.setattr(worker.JobContext, "progress", _no_progress)
    acct = (await client.post("/api/accounts", json={"name": "Repairs Checking"})).json()
    pay = (
        await client.post(
            "/api/transactions",
            json={"txn_date": "2026-05-02", "description": "ACME ROOFING", "amount": "-950.00", "account_id": acct["id"]},
        )
    ).json()

    async def fake_extract(data, mime_type, filename):
        return ai_st.BillExtraction(
            document_type="receipt",
            vendor="Acme Roofing",
            service_type=None,
            statement_date="2026-05-01",
            amount_due=950.0,
            usage=[],
            summary="Roof repair invoice",
        )

    monkeypatch.setattr(ai_st, "extract_bill", fake_extract)
    r = await client.post("/api/statements", files={"file": ("roof.pdf", b"%PDF-1.7 roof", "application/pdf")})
    await extract_bill_job(JobContext(0, "extract_bill", {"statement_id": r.json()["id"]}))
    st = (await client.get(f"/api/statements/{r.json()['id']}")).json()
    assert st["suggestion"]["one_off"] is True
    assert st["suggestion"]["transaction_ids"] == [pay["id"]]

    st = (await client.post(f"/api/statements/{st['id']}/approve", json={"transaction_ids": [pay["id"]]})).json()
    assert st["status"] == "approved" and st["series_id"] is None
    assert [t["id"] for t in st["transactions"]] == [pay["id"]]
    assert "Acme Roofing" not in [s["name"] for s in (await client.get("/api/statement-series")).json()]
    t = (await client.get(f"/api/transactions/{pay['id']}")).json()
    assert t["has_statement"] is True and t["tags"] == []

    # Choosing a series overrides the one-off suggestion
    st = (
        await client.post(
            f"/api/statements/{st['id']}/approve", json={"new_series_name": "Roofing", "transaction_ids": [pay["id"]]}
        )
    ).json()
    assert st["series_name"] == "Roofing"


async def test_statement_rejects_spreadsheets(client):
    r = await client.post("/api/statements", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 422


def _receipt(vendor: str, day: str, amount: float):
    from ledger.ai import statements as ai_st

    async def fake_extract(data, mime_type, filename):
        return ai_st.BillExtraction(
            document_type="receipt",
            vendor=vendor,
            service_type=None,
            statement_date=day,
            amount_due=amount,
            usage=[],
            summary=f"{vendor} receipt",
        )

    return fake_extract


async def _upload(client, name: str, data: bytes, **form) -> dict:
    from ledger.statements.jobs import extract_bill_job

    r = await client.post("/api/statements", files={"file": (name, data, "application/pdf")}, data=form)
    assert r.status_code == 201, r.text
    if r.json()["status"] == "processing":
        await extract_bill_job(JobContext(0, "extract_bill", {"statement_id": r.json()["id"]}))
    return (await client.get(f"/api/statements/{r.json()['id']}")).json()


async def test_receipt_matched_when_payment_is_imported_later(client, monkeypatch):
    import ledger.jobs.worker as worker
    from ledger.ai import statements as ai_st
    from ledger.imports.jobs import prepare_import_job

    monkeypatch.setattr(worker.JobContext, "progress", _no_progress)
    monkeypatch.setattr(ai_st, "extract_bill", _receipt("Corner Hardware", "2026-10-17", 37.41))
    acct = (await client.post("/api/accounts", json={"name": "Later Card"})).json()

    st = await _upload(client, "hardware.pdf", b"%PDF-1.7 hardware")
    assert st["status"] == "suggested"
    assert st["suggestion"]["candidates"] == [] and st["suggestion"]["transaction_ids"] == []

    # The card's feed delivers the charge a day later.
    csv = "Date,Description,Amount\n10/18/2026,CORNER HARDWARE #12,-37.41\n"
    b = (await client.post("/api/imports", files={"file": ("card.csv", csv.encode(), "text/csv")})).json()
    r = await client.post(
        f"/api/imports/{b['id']}/prepare", json={"mapping": b["mapping"], "defaults": {"account_id": acct["id"]}}
    )
    assert r.status_code == 200, r.text
    await prepare_import_job(JobContext(0, "prepare_import", {"batch_id": b["id"]}))
    assert (await client.post(f"/api/imports/{b['id']}/commit", json={})).json()["status"] == "committed"
    pay = (await client.get("/api/transactions", params={"import_batch_id": b["id"]})).json()["items"][0]

    st = (await client.get(f"/api/statements/{st['id']}")).json()
    assert st["status"] == "suggested"
    assert st["suggestion"]["candidates"][0]["transaction_id"] == pay["id"]
    assert st["suggestion"]["transaction_ids"] == [pay["id"]]
    assert st["suggestion"]["refreshed_at"]

    st = (await client.post(f"/api/statements/{st['id']}/approve", json={"transaction_ids": [pay["id"]]})).json()
    assert [t["id"] for t in st["transactions"]] == [pay["id"]]


async def test_unlinked_receipt_refresh_and_link(client, monkeypatch):
    import ledger.jobs.worker as worker
    from ledger.ai import statements as ai_st

    monkeypatch.setattr(worker.JobContext, "progress", _no_progress)
    monkeypatch.setattr(ai_st, "extract_bill", _receipt("Garden Center", "2026-10-10", 63.05))
    acct = (await client.post("/api/accounts", json={"name": "Garden Checking"})).json()

    st = await _upload(client, "garden.pdf", b"%PDF-1.7 garden")
    st = (await client.post(f"/api/statements/{st['id']}/approve", json={"one_off": True, "transaction_ids": []})).json()
    assert st["status"] == "approved" and st["transactions"] == []
    unlinked = (await client.get("/api/statements", params={"status": "approved", "unlinked": True})).json()
    assert st["id"] in [s["id"] for s in unlinked]

    pay = (
        await client.post(
            "/api/transactions",
            json={"txn_date": "2026-10-12", "description": "GARDEN CENTER", "amount": "-63.05", "account_id": acct["id"]},
        )
    ).json()
    # Approved statements get fresh candidates but are never linked behind the user's back.
    st = (await client.get(f"/api/statements/{st['id']}")).json()
    assert st["transactions"] == []
    assert st["suggestion"]["candidates"][0]["transaction_id"] == pay["id"]

    r = await client.post(f"/api/statements/{st['id']}/refresh-links")
    assert r.status_code == 200 and r.json()["suggestion"]["candidates"][0]["transaction_id"] == pay["id"]

    assert (await client.post(f"/api/statements/{st['id']}/transactions", json={"transaction_ids": [0]})).status_code == 422
    st = (await client.post(f"/api/statements/{st['id']}/transactions", json={"transaction_ids": [pay["id"]]})).json()
    assert [t["id"] for t in st["transactions"]] == [pay["id"]]
    unlinked = (await client.get("/api/statements", params={"status": "approved", "unlinked": True})).json()
    assert st["id"] not in [s["id"] for s in unlinked]
    assert [s["id"] for s in (await client.get(f"/api/statements/for-transaction/{pay['id']}")).json()] == [st["id"]]

    dismissed = (await client.post(f"/api/statements/{st['id']}/dismiss")).json()
    assert (await client.post(f"/api/statements/{dismissed['id']}/refresh-links")).status_code == 409


async def test_reupload_from_transaction_reuses_statement(client, monkeypatch):
    import ledger.jobs.worker as worker
    from ledger.ai import statements as ai_st

    monkeypatch.setattr(worker.JobContext, "progress", _no_progress)
    monkeypatch.setattr(ai_st, "extract_bill", _receipt("Lakeside Diner", "2026-10-05", 48.20))
    acct = (await client.post("/api/accounts", json={"name": "Diner Card"})).json()
    guess, real = [
        (
            await client.post(
                "/api/transactions",
                json={"txn_date": day, "description": "LAKESIDE DINER", "amount": "-48.20", "account_id": acct["id"]},
            )
        ).json()
        for day in ("2026-10-05", "2026-10-06")
    ]

    first = await _upload(client, "diner.pdf", b"%PDF-1.7 diner")
    assert first["suggestion"]["transaction_ids"] == [guess["id"]]

    # Attaching the same file from the right transaction links it to the existing statement instead of a copy.
    again = await _upload(client, "diner.pdf", b"%PDF-1.7 diner", transaction_ids=str(real["id"]))
    assert again["id"] == first["id"]
    assert again["suggestion"]["transaction_ids"] == [real["id"]]
    assert again["suggestion"]["candidates"][0]["transaction_id"] == real["id"]
    assert sum(s["filename"] == "diner.pdf" for s in (await client.get("/api/statements")).json()) == 1

    # A refresh keeps the user's choice rather than reverting to the automatic guess.
    st = (await client.post(f"/api/statements/{first['id']}/refresh-links")).json()
    assert st["suggestion"]["transaction_ids"] == [real["id"]]
