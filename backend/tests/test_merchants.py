import pytest


@pytest.fixture(scope="module")
async def tm(client):
    cat = (await client.get("/api/categories")).json()[0]

    async def add(desc, amt="-5.00", d="2025-02-01"):
        r = await client.post("/api/transactions", json={"txn_date": d, "description": desc, "amount": amt})
        assert r.status_code == 201, r.text
        return r.json()

    a = await add("TM COFFEE #123 SPRINGFIELD IL")
    b = await add("TM COFFEE SHOP 99999", d="2025-02-03")
    # A rule on the source merchant should follow the merge.
    r = await client.patch(f"/api/transactions/{b['id']}", json={"category_id": cat["id"]})
    assert r.status_code == 200
    r = await client.post("/api/rules", json={"pattern": "tm coffee shop", "category_id": cat["id"]})
    assert r.status_code == 201, r.text
    return {"a": a, "b": b, "cat": cat, "add": add}


async def test_keys_and_merge(client, tm):
    a, b = tm["a"], tm["b"]
    assert a["merchant"] == "tm coffee springfield" and a["merchant_name"] == "Tm Coffee Springfield"
    assert b["merchant"] == "tm coffee shop" and a["merchant_source"] is None

    r = await client.post("/api/merchants/merge", json={"source": "tm coffee shop", "target": "tm coffee springfield"})
    assert r.json() == {"key": "tm coffee springfield", "moved": 1}
    assert (await client.get(f"/api/transactions/{b['id']}")).json()["merchant"] == "tm coffee springfield"

    d = (await client.get("/api/merchants/detail", params={"key": "tm coffee shop"})).json()
    assert d["key"] == "tm coffee springfield" and d["aliases"] == ["tm coffee shop"] and d["stats"]["count"] == 2
    assert d["rule"]["category_id"] == tm["cat"]["id"]
    assert {x["description"] for x in d["descriptions"]} == {"TM COFFEE #123 SPRINGFIELD IL", "TM COFFEE SHOP 99999"}

    c = await tm["add"]("TM COFFEE SHOP 5555")
    assert c["merchant"] == "tm coffee springfield" and c["category_id"] == tm["cat"]["id"]

    same = {"source": "tm coffee springfield", "target": "tm coffee springfield"}
    assert (await client.post("/api/merchants/merge", json=same)).status_code == 422

    r = await client.put("/api/merchants/name", json={"key": "tm coffee springfield", "display_name": "TM Coffee"})
    assert r.json()["name"] == "TM Coffee"
    assert (await client.get(f"/api/transactions/{a['id']}")).json()["merchant_name"] == "TM Coffee"
    found = (await client.get("/api/merchants/search", params={"q": "tm coffee"})).json()
    assert [(m["key"], m["name"], m["count"]) for m in found] == [("tm coffee springfield", "TM Coffee", 3)]

    r = await client.post("/api/merchants/unmerge", json={"key": "tm coffee shop"})
    assert r.json()["moved"] == 2
    assert (await client.get(f"/api/transactions/{c['id']}")).json()["merchant"] == "tm coffee shop"


async def test_transaction_override(client, tm):
    a = tm["a"]
    r = await client.patch(f"/api/transactions/{a['id']}", json={"merchant_name": "TM Beans & Co"})
    t = r.json()
    assert (t["merchant"], t["merchant_name"], t["merchant_source"]) == ("tm beans & co", "TM Beans & Co", "user")

    t = (await client.patch(f"/api/transactions/{a['id']}", json={"description": "TM COFFEE #9 SHELBYVILLE"})).json()
    assert t["merchant"] == "tm beans & co"

    from ledger.db.engine import get_sessionmaker
    from ledger.maintenance import renormalize

    async with get_sessionmaker()() as session:
        await renormalize(session)
    assert (await client.get(f"/api/transactions/{a['id']}")).json()["merchant"] == "tm beans & co"

    # Typing an existing display name picks that merchant rather than a new key.
    t = (await client.patch(f"/api/transactions/{a['id']}", json={"merchant_name": "tm coffee"})).json()
    assert t["merchant"] == "tm coffee springfield" and t["merchant_source"] == "user"

    t = (await client.patch(f"/api/transactions/{a['id']}", json={"merchant_name": ""})).json()
    assert (t["merchant"], t["merchant_source"]) == ("tm coffee shelbyville", None)

    body = {"txn_date": "2025-03-01", "description": "SQ *TM TRUCK 12", "amount": "-3.00", "merchant_name": "TM Taco Truck"}
    n = await client.post("/api/transactions", json=body)
    assert n.json()["merchant"] == "tm taco truck" and n.json()["merchant_source"] == "user"


async def test_renormalize_carries_merchants_forward_when_keys_change(client):
    from sqlalchemy import select, text

    from ledger.db.engine import get_sessionmaker
    from ledger.maintenance import renormalize
    from ledger.models import CategoryRule, MerchantProfile

    cat = (await client.get("/api/categories")).json()[0]

    async def add(desc, d):
        r = await client.post("/api/transactions", json={"txn_date": d, "description": desc, "amount": "-7.00"})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    d1 = await add("RNTEST MOBILE 04/24 DEPOSIT", "2025-04-24")
    d2 = await add("RNTEST MOBILE 05/02 DEPOSIT", "2025-05-02")
    await add("RNFEE SERVICE", "2025-04-01")
    f2 = await add("RNFEE SERVICE CHARGE 04/30", "2025-04-30")
    # Another dated key for the same charge that was never merged: it should join "rnfee service" too.
    f3 = await add("RNFEE SERVICE CHARGE 05/31", "2025-05-31")

    # Recreate what older key rules produced: per-date deposit keys (one named, with a rule), and fee keys, one of
    # which had been merged into "rnfee service".
    old = {
        d1: "rntest mobile 04 24 deposit",
        d2: "rntest mobile 05 02 deposit",
        f2: "rnfee service",
        f3: "rnfee service charge 05 31",
    }
    async with get_sessionmaker()() as s:
        for tid, key in old.items():
            await s.execute(text('UPDATE "transaction" SET merchant = :m WHERE id = :i'), {"m": key, "i": tid})
        s.add_all(
            [
                MerchantProfile(key="rntest mobile 04 24 deposit", display_name="RN Mobile Deposit"),
                MerchantProfile(key="rnfee service", display_name="RN Fee"),
                MerchantProfile(key="rnfee service charge 04 30", alias_of="rnfee service"),
                CategoryRule(pattern="rntest mobile 04 24 deposit", category_id=cat["id"]),
            ]
        )
        await s.commit()

    async with get_sessionmaker()() as s:
        preview = await renormalize(s, dry_run=True)
    assert preview["merged"] >= 3 and preview["aliased"] >= 1
    assert (await client.get(f"/api/transactions/{d1}")).json()["merchant"] == "rntest mobile 04 24 deposit"

    async with get_sessionmaker()() as s:
        await renormalize(s)
        rule = await s.scalar(
            select(CategoryRule.pattern).where(CategoryRule.category_id == cat["id"], CategoryRule.pattern.like("rntest%"))
        )
        alias = await s.scalar(select(MerchantProfile.alias_of).where(MerchantProfile.key == "rnfee service charge"))
    for tid in (d1, d2):
        t = (await client.get(f"/api/transactions/{tid}")).json()
        assert (t["merchant"], t["merchant_name"]) == ("rntest mobile deposit", "RN Mobile Deposit")
    assert rule == "rntest mobile deposit"
    assert alias == "rnfee service"
    assert (await client.get(f"/api/transactions/{f2}")).json()["merchant_name"] == "RN Fee"
    assert (await client.get(f"/api/transactions/{f3}")).json()["merchant"] == "rnfee service"
    later = await add("RNFEE SERVICE CHARGE 06/30", "2025-06-30")
    assert (await client.get(f"/api/transactions/{later}")).json()["merchant"] == "rnfee service"
