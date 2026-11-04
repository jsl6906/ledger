from datetime import date, timedelta

import pytest
from sqlalchemy import select

from ledger.alerts import monthly
from ledger.alerts import service as alerts
from ledger.db.engine import get_sessionmaker
from ledger.models import AccountBalance, AlertEvent, Anomaly


async def test_mailer_builds_safe_message(monkeypatch):
    from ledger.alerts import mailer
    from ledger.config import Settings

    captured = {}

    async def fake_send(msg, **kwargs):
        captured.update(msg=msg, **kwargs)

    settings = Settings(smtp_host="smtp.example.com", smtp_from="Ledger <ledger@example.com>", smtp_username="u")
    monkeypatch.setattr(mailer, "get_settings", lambda: settings)
    monkeypatch.setattr(mailer.aiosmtplib, "send", fake_send)
    await mailer.send_email(["a@example.com", "b@example.com"], "Budget\r\nBcc: evil@example.com", "body", "<p>body</p>")
    msg = captured["msg"]
    assert msg["Subject"] == "Budget Bcc: evil@example.com" and msg["Bcc"] is None
    assert msg["To"] == "a@example.com, b@example.com"
    assert captured["hostname"] == "smtp.example.com" and captured["start_tls"] is True and captured["use_tls"] is False
    assert msg.get_body(("html",)).get_content().strip() == "<p>body</p>"


async def test_mailer_inlines_logo_and_reply_to(monkeypatch):
    from ledger.alerts import mailer
    from ledger.config import Settings

    captured = {}

    async def fake_send(msg, **kwargs):
        captured["msg"] = msg

    monkeypatch.setattr(mailer, "get_settings", lambda: Settings(smtp_host="smtp.example.com", smtp_from="l@example.com"))
    monkeypatch.setattr(mailer.aiosmtplib, "send", fake_send)
    html_body = alerts.mail.layout(kicker="Test", heading="Hi", body="<p>x</p>", preheader="Preview text")
    await mailer.send_email(["a@example.com"], "Hi", "x", html_body, reply_to="sam@example.com")
    msg = captured["msg"]
    assert msg["Reply-To"] == "sam@example.com"
    [logo] = [p for p in msg.walk() if p.get_content_type() == "image/png"]
    assert logo["Content-ID"] == "<ledger-logo>" and logo.get_content() == mailer.logo_png()
    assert "cid:ledger-logo" in msg.get_body(("html",)).get_content()


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []

    async def fake_send(to, subject, text, html=None):
        sent.append({"to": to, "subject": subject, "text": text, "html": html})

    monkeypatch.setattr(alerts, "send_email", fake_send)
    monkeypatch.setattr(alerts, "smtp_configured", lambda: True)

    async def no_ai(d):
        return alerts.DigestNarrative(summary="A quiet week.")

    async def no_monthly_ai(d):
        return monthly.MonthlyNarrative(
            summary="A steady month.",
            budget="Dining went over.",
            spending=["Most of it went on MRX NEWSHOP."],
            notable=[],
            recommendations=["Hold back on <dining> next month."],
        )

    monkeypatch.setattr(alerts, "_ai_narrative", no_ai)
    monkeypatch.setattr(monthly, "_ai_narrative", no_monthly_ai)
    return sent


async def _events(like: str) -> list[AlertEvent]:
    async with get_sessionmaker()() as s:
        return list((await s.scalars(select(AlertEvent).where(AlertEvent.title.like(f"%{like}%")))).all())


async def test_recipients_and_rules_api(client):
    assert (await client.post("/api/alerts/recipients", json={"email": "not-an-email"})).status_code == 422
    r = await client.post("/api/alerts/recipients", json={"email": "Alerts@Example.com", "name": "Home"})
    assert r.status_code == 201 and r.json()["email"] == "alerts@example.com"
    assert (await client.post("/api/alerts/recipients", json={"email": "alerts@example.com"})).status_code == 409

    rules = {r["kind"]: r for r in (await client.get("/api/alerts/rules")).json()}
    assert set(rules) == {"budget_overspend", "out_of_norm", "large_transaction", "weekly_digest", "monthly_digest"}
    assert rules["large_transaction"]["params"]["threshold"] == 500
    r = await client.put("/api/alerts/rules/large_transaction", json={"threshold": 750, "pace": False})
    assert r.json()["params"] == {"threshold": 750.0}
    await client.put("/api/alerts/rules/large_transaction", json={"threshold": 500})

    status = (await client.get("/api/alerts/status")).json()
    assert status["recipients"] >= 1
    assert (await client.post("/api/alerts/test")).status_code == 409  # SMTP not configured in tests


async def test_budget_overspend_alerts_once(client, outbox):
    g = (await client.post("/api/category-groups", json={"name": "TAL Group"})).json()
    cat = (await client.post("/api/categories", json={"name": "TAL Hobbies", "group_id": g["id"]})).json()
    r = await client.post("/api/budgets", json={"category_id": cat["id"], "period_type": "month", "amount": "100"})
    assert r.status_code == 201, r.text
    today = date.today()
    await client.post(
        "/api/transactions",
        json={"txn_date": today.isoformat(), "description": "TAL KITS", "amount": "-160.00", "category_id": cat["id"]},
    )
    await client.post("/api/alerts/recipients", json={"email": "budget@example.com"})

    async with get_sessionmaker()() as s:
        res = await alerts.evaluate(s, today)
    assert res["new"] >= 1
    events = await _events("TAL Hobbies")
    assert len(events) == 1 and events[0].kind == "budget_overspend" and events[0].status == "sent"
    assert "budget@example.com" in events[0].recipients
    assert any("TAL Hobbies" in m["text"] for m in outbox)

    async with get_sessionmaker()() as s:
        await alerts.evaluate(s, today)
    assert len(await _events("TAL Hobbies")) == 1


async def test_anomaly_alerts_respect_rules(client, outbox):
    async with get_sessionmaker()() as s:
        s.add(
            Anomaly(
                kind="category_spike",
                subject_key="test:tal-spike",
                period=date.today().replace(day=1),
                amount=420,
                baseline=100,
                score=5,
                title="TAL Spike",
                detail="TAL Spike is 4.2x its norm.",
            )
        )
        await s.commit()

    await client.put("/api/alerts/rules/out_of_norm", json={"enabled": False})
    async with get_sessionmaker()() as s:
        await alerts.evaluate(s)
    assert await _events("TAL Spike") == []

    await client.put("/api/alerts/rules/out_of_norm", json={"enabled": True})
    async with get_sessionmaker()() as s:
        await alerts.evaluate(s)
    [event] = await _events("TAL Spike")
    assert event.kind == "out_of_norm" and event.status == "sent"
    assert event.title.startswith("Category spike · TAL Spike")

    listed = (await client.get("/api/alerts/events")).json()
    assert any(e["id"] == event.id and e["finding_status"] == "open" for e in listed)


async def test_undelivered_without_smtp(client):
    async with get_sessionmaker()() as s:
        s.add(
            Anomaly(
                kind="new_merchant",
                subject_key="test:tal-new",
                period=date.today().replace(day=1),
                amount=300,
                score=1.5,
                title="TAL Newshop",
                detail="First purchase.",
            )
        )
        await s.commit()
        await alerts.evaluate(s)
    [event] = await _events("TAL Newshop")
    assert event.status == "skipped" and "SMTP" in event.error


async def test_weekly_digest(client, outbox):
    preview = (await client.get("/api/alerts/digest/preview")).json()
    assert preview["subject"].startswith("Ledger weekly digest")
    assert "The week" in preview["text"] and "<h3" in preview["html"]

    async with get_sessionmaker()() as s:
        d = await alerts.render_digest(s, date.today())
    assert d["summary"] == "A quiet week." and d["text"].startswith("A quiet week.")
    # Highlights only: the in-depth sections are in the monthly report.
    assert not any(h in d["text"] for h in ("Top merchants", "Top categories", "Trends", "Recommendations"))
    assert "Weekly highlights" in d["html"]

    async with get_sessionmaker()() as s:
        week = await alerts.digest_data(s, date(2031, 3, 7))
    assert (week["start"], week["end"]) == (date(2031, 2, 24), date(2031, 3, 2))  # Friday send -> prior Mon-Sun

    async with get_sessionmaker()() as s:
        first = await alerts.send_digest(s, date(2031, 3, 3))
        again = await alerts.send_digest(s, date(2031, 3, 5))
    assert first["status"] == "sent" and again["status"] == "already_sent"
    assert outbox[-1]["subject"].startswith("Ledger weekly digest")


async def test_monthly_report_waits_for_accounts_to_load(client, outbox):
    card = (await client.post("/api/accounts", json={"name": "MRX Card", "account_type": "credit_card"})).json()
    checking = (await client.post("/api/accounts", json={"name": "MRX Checking"})).json()

    async def txn(acct: dict, day: str, desc: str, amount: str) -> None:
        body = {"txn_date": day, "description": desc, "amount": amount, "account_id": acct["id"]}
        assert (await client.post("/api/transactions", json=body)).status_code == 201

    await txn(card, "2035-04-10", "MRX DINER", "-50.00")
    await txn(card, "2035-05-12", "MRX DINER", "-120.00")
    await txn(card, "2035-05-20", "MRX NEWSHOP", "-800.00")
    await txn(checking, "2035-05-15", "MRX PAYROLL", "3000.00")
    may = date(2035, 5, 1)

    async with get_sessionmaker()() as s:
        r = await monthly.month_readiness(s, may)
        assert not r["ready"] and r["missing"] == ["MRX Card", "MRX Checking"]
        assert r["send_by"] == date(2035, 6, 8) and r["needed_through"] == date(2035, 6, 3)
        assert (await monthly.send_monthly(s, date(2035, 6, 4)))["status"] == "waiting"

    # Checking: transactions reach the settle days past month end. Card: a statement closes at month end.
    await txn(checking, "2035-06-03", "MRX COFFEE", "-4.00")
    async with get_sessionmaker()() as s:
        s.add(AccountBalance(account_id=card["id"], as_of=date(2035, 5, 31), balance=920, source="statement"))
        await s.commit()
        r = await monthly.month_readiness(s, may)
        assert r["ready"] and {a["name"] for a in r["accounts"]} == {"MRX Card", "MRX Checking"}
        sent = await monthly.send_monthly(s, date(2035, 6, 4))
        again = await monthly.send_monthly(s, date(2035, 6, 5))
    assert sent["status"] == "sent" and again["status"] == "already_sent"
    mail = outbox[-1]
    assert mail["subject"] == "Ledger monthly report · May 2035"
    assert mail["text"].startswith("A steady month.") and "hadn’t loaded" not in mail["text"]
    assert "New merchants\n- MRX NEWSHOP: $800.00 (1)" in mail["text"]
    assert "Income $3,000.00, spending $920.00" in mail["text"] and "All 2 active accounts had loaded" in mail["text"]
    assert "Recommendations\n- Hold back on <dining>" in mail["text"]
    assert "<li" in mail["html"] and "&lt;dining&gt;" in mail["html"]

    # June never loads on the card: the report waits until the cap day, then names the account.
    await txn(card, "2035-06-10", "MRX DINER", "-30.00")
    await txn(checking, "2035-07-04", "MRX COFFEE", "-4.00")
    async with get_sessionmaker()() as s:
        assert (await monthly.send_monthly(s, date(2035, 7, 7)))["missing"] == ["MRX Card"]
        assert (await monthly.send_monthly(s, date(2035, 7, 8)))["status"] == "sent"
    assert outbox[-1]["subject"] == "Ledger monthly report · June 2035"
    assert "Some accounts hadn’t loaded June 2035 yet (MRX Card)" in outbox[-1]["text"]
    assert "MRX Card: not loaded (data through 10 Jun)" in outbox[-1]["text"]

    preview = (await client.get("/api/alerts/digest/preview", params={"kind": "monthly"})).json()
    assert preview["subject"].startswith("Ledger monthly report")
    assert (await client.get("/api/alerts/digest/monthly/readiness")).status_code == 200


async def test_emails_link_into_the_app(client, outbox, monkeypatch):
    settings = alerts.get_settings().model_copy(update={"app_base_url": "http://homeserver:8470/"})
    monkeypatch.setattr(alerts, "get_settings", lambda: settings)
    base = "http://homeserver:8470"
    g = (await client.post("/api/category-groups", json={"name": "TAL Link Group"})).json()
    cat = (await client.post("/api/categories", json={"name": "TAL Linked", "group_id": g["id"]})).json()
    await client.post("/api/budgets", json={"category_id": cat["id"], "period_type": "month", "amount": "50"})
    today = date.today()
    await client.post(
        "/api/transactions",
        json={"txn_date": today.isoformat(), "description": "TAL LINKSHOP", "amount": "-90.00", "category_id": cat["id"]},
    )
    async with get_sessionmaker()() as s:
        s.add(
            Anomaly(
                kind="category_spike",
                subject_key="test:tal-link-spike",
                period=today.replace(day=1),
                category_id=cat["id"],
                amount=90,
                baseline=10,
                score=5,
                title="TAL Link Spike",
                detail="Spike.",
            )
        )
        await s.commit()
        await alerts.evaluate(s, today)

    month = alerts.period_for("month", today)
    spike_url = f"{base}/transactions?category={cat['id']}&start={month.start}&end={month.end}"
    [budget_event] = await _events("TAL Linked")
    assert budget_event.link == f"/transactions?category={cat['id']}&start={month.start}&end={month.end}"
    sent = next(m for m in outbox if "TAL Link Spike" in m["text"])
    assert spike_url in sent["text"]
    assert f'href="{spike_url.replace("&", "&amp;")}"' in sent["html"]
    assert f'href="{base}/alerts"' in sent["html"]

    async with get_sessionmaker()() as s:
        d = await alerts.render_digest(s, today + timedelta(days=1 + settings.digest_lag_days), ai=False)
    assert f"{base}/transactions?start=" in d["text"]
    async with get_sessionmaker()() as s:
        m = await monthly.render_monthly(s, today.replace(day=1), ai=False)
    assert f"{base}/transactions?start=" in m["text"] and f'href="{base}/merchants?key=' in m["html"]


async def test_ask_about_transactions(client, monkeypatch):
    from ledger.alerts import questions

    sent: list[dict] = []

    async def fake_send(to, subject, text, html=None, reply_to=None, message_id=None):
        sent.append({"to": to, "subject": subject, "text": text, "html": html, "reply_to": reply_to})

    monkeypatch.setattr(questions, "send_email", fake_send)
    monkeypatch.setattr(questions, "smtp_configured", lambda: True)
    await client.post("/api/members", json={"name": "TQ Sam", "initials": "TQS", "email": "TQ-Sam@Example.com"})
    await client.post("/api/alerts/recipients", json={"email": "tq-alex@example.com", "name": "TQ Alex"})
    people = {p["email"]: p["name"] for p in (await client.get("/api/alerts/people")).json()}
    assert people["tq-sam@example.com"] == "TQ Sam" and people["tq-alex@example.com"] == "TQ Alex"

    ids = [
        (await client.post("/api/transactions", json={"txn_date": d, "description": desc, "amount": amt})).json()["id"]
        for d, desc, amt in (("2031-02-01", "TQ <b>GADGETS</b>", "-42.50"), ("2031-02-03", "TQ REFUNDCO", "12.00"))
    ]
    body = {"transaction_ids": ids, "to": ["tq-sam@example.com"], "message": "What were <these>?"}
    assert (await client.post("/api/alerts/questions", json={**body, "to": ["stranger@example.com"]})).status_code == 422
    assert (await client.post("/api/alerts/questions", json={**body, "reply_to": "x@example.com"})).status_code == 422
    assert sent == []

    r = await client.post("/api/alerts/questions", json={**body, "reply_to": "tq-alex@example.com"})
    assert r.status_code == 200, r.text
    [mail] = sent
    assert mail["to"] == ["tq-sam@example.com"] and mail["reply_to"] == "tq-alex@example.com"
    assert mail["subject"] == "Question about 2 transactions"
    assert "TQ Alex has a question about 2 transactions" in mail["html"]
    assert "What were &lt;these&gt;?" in mail["html"] and "TQ &lt;b&gt;GADGETS&lt;/b&gt;" in mail["html"]
    assert "-$30.50" in mail["html"] and "What were <these>?" in mail["text"]

    notes = (await client.get(f"/api/transactions/{ids[0]}/notes")).json()
    assert [(n["source"], n["body"]) for n in notes] == [("question", "Asked TQ Sam: What were <these>?")]
    event = next(e for e in (await client.get("/api/alerts/events")).json() if e["id"] == r.json()["event_id"])
    assert event["kind"] == "question" and event["status"] == "sent" and event["recipients"] == ["tq-sam@example.com"]

    one = await client.post("/api/alerts/questions", json={**body, "transaction_ids": ids[:1], "message": "Hm?"})
    assert sent[-1]["subject"] == "Question: TQ <b>GADGETS</b> -$42.50" and sent[-1]["reply_to"] is None
    assert one.json()["sent_to"] == ["tq-sam@example.com"]
