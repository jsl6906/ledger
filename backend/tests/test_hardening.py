import io
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from ledger.ai.pricing import estimate_cost
from ledger.db.engine import get_sessionmaker
from ledger.maintenance import cleanup
from ledger.models import AiCallLog, Attachment, ChatMessage, ChatSession, Job


async def test_security_headers_and_health(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db"] == "ok" and body["schema_current"] is True
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "script-src 'self'" in r.headers["content-security-policy"]


async def test_cross_site_writes_blocked(client):
    r = await client.post("/api/tags", json={"name": "TH evil"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = await client.post("/api/tags", json={"name": "TH evil2"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = await client.post("/api/tags", json={"name": "TH same-origin"}, headers={"Origin": "http://test"})
    assert r.status_code == 201
    # Reads are not affected
    assert (await client.get("/api/tags", headers={"Origin": "https://evil.example"})).status_code == 200


async def test_export_zip(client):
    r = await client.get("/api/export.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert {"transactions.csv", "accounts.csv", "categories.csv", "budgets.csv"} <= set(zf.namelist())
    header = zf.read("transactions.csv").decode().splitlines()[0]
    assert header.startswith("id,date,posted_date,description")
    assert len(zf.read("categories.csv").decode().splitlines()) > 80


async def test_ai_usage(client):
    async with get_sessionmaker()() as s:
        s.add_all(
            [
                AiCallLog(purpose="th_test", model="m1", input_tokens=100, output_tokens=20, latency_ms=300, ok=True),
                AiCallLog(purpose="th_test", model="m1", ok=False, error="boom", latency_ms=50),
                AiCallLog(
                    purpose="th_cost",
                    model="gemini-3.5-flash-lite",
                    input_tokens=1000,
                    output_tokens=200,
                    thinking_tokens=300,
                    cost_usd=Decimal("0.00155"),
                    ok=True,
                ),
                AiCallLog(
                    purpose="th_cost",
                    model="gemini-3.5-flash-lite",
                    input_tokens=1000,
                    output_tokens=0,
                    cost_usd=Decimal("0.0003"),
                    ok=True,
                ),
            ]
        )
        await s.commit()
    data = (await client.get("/api/ai-usage", params={"days": 1})).json()
    item = next(i for i in data["items"] if i["purpose"] == "th_test")
    assert item["calls"] == 2 and item["errors"] == 1 and item["input_tokens"] == 100
    assert item["cost_usd"] is None and item["unpriced_calls"] == 1
    priced = next(i for i in data["items"] if i["purpose"] == "th_cost")
    assert priced["thinking_tokens"] == 300 and priced["unpriced_calls"] == 0
    assert priced["cost_usd"] == pytest.approx(0.00185)


def test_estimate_cost():
    # flash-lite: 0.30 in / 2.50 out (thinking billed as output) / 0.03 cached per 1M tokens
    assert estimate_cost("gemini-3.5-flash-lite", 1000, 200, 300) == Decimal("0.001550")
    assert estimate_cost("models/gemini-3.5-flash-lite-001", 1_000_000, 0, cached_tokens=400_000) == Decimal("0.192000")
    # flash promo pricing ends with 2026
    assert estimate_cost("gemini-3.8-flash", 1_000_000, 0, on=date(2026, 12, 31)) == Decimal("0.750000")
    assert estimate_cost("gemini-3.8-flash", 1_000_000, 0, on=date(2027, 1, 1)) == Decimal("1.500000")
    # pro bills the whole call at the long-context rate past 200k prompt tokens
    assert estimate_cost("gemini-3.1-pro-preview", 200_000, 1000) == Decimal("0.412000")
    assert estimate_cost("gemini-3.1-pro-preview", 200_001, 1000) == Decimal("0.818004")
    assert estimate_cost("some-other-model", 1000, 1000) is None


async def test_cleanup_prunes_old_rows_only(client):
    old = datetime.now(UTC) - timedelta(days=400)
    async with get_sessionmaker()() as s:
        orphan = Attachment(
            filename="th-orphan.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            sha256="th" + "0" * 62,
            content=b"x",
            created_at=old,
        )
        kept = Attachment(
            filename="th-kept.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            sha256="th" + "1" * 62,
            content=b"y",
            created_at=old,
        )
        fresh = Attachment(
            filename="th-fresh.pdf", mime_type="application/pdf", size_bytes=1, sha256="th" + "2" * 62, content=b"z"
        )
        chat = ChatSession(title="th")
        s.add_all([orphan, kept, fresh, chat])
        await s.flush()
        s.add(ChatMessage(session_id=chat.id, role="user", content="doc", attachment_id=kept.id, queries=[]))
        old_job = Job(type="cleanup", status="succeeded", payload={}, finished_at=old)
        new_job = Job(type="cleanup", status="succeeded", payload={}, finished_at=datetime.now(UTC))
        s.add_all([old_job, new_job, AiCallLog(purpose="th_old", model="m", ok=True, created_at=old)])
        await s.commit()
        ids = {"orphan": orphan.id, "kept": kept.id, "fresh": fresh.id, "old_job": old_job.id, "new_job": new_job.id}

    async with get_sessionmaker()() as s:
        removed = await cleanup(s)
    assert removed["jobs"] >= 1 and removed["ai_calls"] >= 1 and removed["attachments"] >= 1
    async with get_sessionmaker()() as s:
        att_ids = [ids["orphan"], ids["kept"], ids["fresh"]]
        remaining = set((await s.scalars(select(Attachment.id).where(Attachment.id.in_(att_ids)))).all())
        assert remaining == {ids["kept"], ids["fresh"]}
        assert await s.get(Job, ids["old_job"]) is None and await s.get(Job, ids["new_job"]) is not None


def test_quoted_password_hash_is_unquoted():
    from ledger.config import Settings

    assert Settings(app_password_hash="'$argon2id$v=19$abc'").app_password_hash == "$argon2id$v=19$abc"
    assert Settings(app_password_hash="$argon2id$v=19$abc").app_password_hash == "$argon2id$v=19$abc"


def test_serve_refuses_insecure_config(monkeypatch):
    from pydantic import SecretStr

    from ledger import cli
    from ledger.config import get_settings

    assert cli.config_problems() == [] or all("SESSION_SECRET" in p for p in cli.config_problems())
    weak = get_settings().model_copy(update={"session_secret": SecretStr("dev-only-change-me"), "app_password_hash": None})
    monkeypatch.setattr("ledger.config.get_settings", lambda: weak)
    problems = cli.config_problems()
    assert any("SESSION_SECRET" in p for p in problems) and any("APP_PASSWORD_HASH" in p for p in problems)
