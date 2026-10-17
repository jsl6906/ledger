"""Email alerts: budget overspend, out-of-norm spending, large transactions and a weekly digest.

Each condition becomes an AlertEvent with a stable subject_key, so it is emailed once. New events from one
evaluation run are batched into a single email.
"""

import json
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import quote, urlencode

from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.alerts import email_html as mail
from ledger.alerts.mailer import send_email, smtp_configured
from ledger.analytics.reports import EXPENSE, FROM, INCOME, REPORTABLE, category_breakdown, top_merchants
from ledger.budgets.service import budget_status, period_for
from ledger.config import get_settings
from ledger.models import AlertEvent, AlertRecipient, AlertRule, Anomaly, Budget, DuplicatePair, Statement, Transaction
from ledger.models.analytics import ANOMALY_LABELS

log = logging.getLogger(__name__)

OUT_OF_NORM_KINDS = ("category_spike", "large_for_merchant", "bill_increase", "new_merchant", "unmatched_transfer")
ANOMALY_LOOKBACK_DAYS = 7
PACE_MIN_ELAPSED = 0.33
PACE_MARGIN = Decimal("1.10")


def money(v) -> str:
    return f"${Decimal(str(v)):,.2f}"


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


async def rules(session: AsyncSession) -> dict[str, AlertRule]:
    return {r.kind: r for r in (await session.scalars(select(AlertRule))).all()}


async def large_txn_threshold(session: AsyncSession) -> Decimal:
    rule = await session.get(AlertRule, "large_transaction")
    value = (rule.params or {}).get("threshold") if rule else None
    return Decimal(str(value if value else get_settings().large_txn_threshold))


def txns_link(**params) -> str:
    """App path of the Transactions page filtered like links.ts txnsPath."""
    return "/transactions?" + urlencode({k: str(v) for k, v in params.items() if v not in (None, "")})


def budget_link(row: dict, period) -> str:
    if not row["category_ids"]:
        return "/budgets"
    if row["category_id"]:
        target = {"category": row["category_id"]}
    else:
        target = {"categories": ",".join(map(str, row["category_ids"])), "label": f"{row['name']} · {period.label}"}
    return txns_link(**target, start=period.start, end=period.end)


def anomaly_link(a: Anomaly) -> str:
    if a.transaction_id:
        return f"/transactions/{a.transaction_id}"
    if a.series_id:
        return f"/bills?series={a.series_id}"
    if a.category_id:
        month = period_for("month", a.period)
        return txns_link(category=a.category_id, start=month.start, end=month.end)
    return f"/findings?kind={a.kind}"


async def _budget_candidates(session: AsyncSession, rule: AlertRule, today: date) -> list[dict]:
    out = []
    kinds = set((await session.scalars(select(Budget.period_type).distinct())).all())
    for kind in kinds:
        period = period_for(kind, today)
        status = await budget_status(session, period, today)
        elapsed = status["period"]["elapsed"]
        for r in status["rows"]:
            if r["kind"] != "expense" or r["period_type"] != kind or r["budget"] <= 0:
                continue
            key = f"budget:{r['budget_id']}:{period.start:%Y-%m-%d}"
            if r["actual"] > r["budget"]:
                out.append(
                    {
                        "kind": "budget_overspend",
                        "subject_key": f"{key}:over",
                        "title": f"Budget · {r['name']} {r['pct']:.0f}% of {period.label}",
                        "body": f"{r['name']} has reached {money(r['actual'])} against a {money(r['budget'])} budget "
                        f"for {period.label} ({money(-r['left'])} over).",
                        "link": budget_link(r, period),
                    }
                )
            elif (
                (rule.params or {}).get("pace", True)
                and elapsed >= PACE_MIN_ELAPSED
                and r["projected"] > r["budget"] * PACE_MARGIN
            ):
                out.append(
                    {
                        "kind": "budget_overspend",
                        "subject_key": f"{key}:pace",
                        "title": f"Budget · {r['name']} on pace for {money(r['projected'])}",
                        "body": f"{r['name']} is at {money(r['actual'])} of {money(r['budget'])} with "
                        f"{(1 - elapsed) * 100:.0f}% of {period.label} left; at this rate it ends near "
                        f"{money(r['projected'])}.",
                        "link": budget_link(r, period),
                    }
                )
    return out


async def _anomaly_candidates(session: AsyncSession, kinds: tuple[str, ...], rule_kind: str, today: date) -> list[dict]:
    since = datetime.combine(today - timedelta(days=ANOMALY_LOOKBACK_DAYS), datetime.min.time(), UTC)
    rows = (
        await session.scalars(
            select(Anomaly)
            .where(Anomaly.kind.in_(kinds), Anomaly.status == "open", Anomaly.created_at >= since)
            .order_by(Anomaly.score.desc())
        )
    ).all()
    return [
        {
            "kind": rule_kind,
            "subject_key": f"anomaly:{a.id}",
            "title": f"{ANOMALY_LABELS.get(a.kind, 'Out-of-norm')} · {a.title} {money(a.amount)}",
            "body": a.detail + (f"\n{a.ai_note}" if a.ai_note else ""),
            "link": anomaly_link(a),
        }
        for a in rows
    ]


async def evaluate(session: AsyncSession, today: date | None = None) -> dict:
    """Record new alert events for enabled rules and email them in one batch."""
    today = today or date.today()
    enabled = {k: r for k, r in (await rules(session)).items() if r.enabled}
    candidates: list[dict] = []
    if "budget_overspend" in enabled:
        candidates += await _budget_candidates(session, enabled["budget_overspend"], today)
    if "out_of_norm" in enabled:
        candidates += await _anomaly_candidates(session, OUT_OF_NORM_KINDS, "out_of_norm", today)
    if "large_transaction" in enabled:
        candidates += await _anomaly_candidates(session, ("large_transaction",), "large_transaction", today)

    new: list[AlertEvent] = []
    for c in candidates:
        event_id = await session.scalar(
            insert(AlertEvent)
            .values(**c)
            .on_conflict_do_nothing(index_elements=[AlertEvent.subject_key])
            .returning(AlertEvent.id)
        )
        if event_id:
            new.append(await session.get(AlertEvent, event_id))
    await session.commit()
    if new:
        subject = f"Ledger: {new[0].title}" if len(new) == 1 else f"Ledger: {len(new)} new alerts"
        await deliver(session, new, subject, *render_alerts(new))
    return {"candidates": len(candidates), "new": len(new), "status": new[0].status if new else None}


def _url(link: str | None) -> str | None:
    base = get_settings().app_base_url
    return f"{base.rstrip('/')}{link}" if base and link else None


def _notice(e: AlertEvent) -> tuple[str, str, str]:
    """(badge label, tone, title without its "Kind · " prefix) for an alert event."""
    prefix, _, rest = e.title.partition(" · ")
    title = rest or e.title
    if e.kind == "budget_overspend":
        pace = e.subject_key.endswith(":pace")
        return ("On pace to overspend", "pace", title) if pace else ("Over budget", "over", title)
    if e.kind == "large_transaction":
        return "Large transaction", "pace", title
    return (prefix if rest else "Out of norm"), "info", title


def _preview(parts: list[str]) -> str:
    s = " · ".join(parts)
    return s if len(s) <= 140 else s[:139] + "…"


def footer(extra: str = "") -> str:
    alerts_page = mail.link("Alerts page", _url("/alerts"), mail.MUTED)
    return f"{extra}Sent by Ledger. Manage rules and recipients on the {alerts_page}."


def render_alerts(events: list[AlertEvent]) -> tuple[str, str]:
    lines, cards, titles = [], [], []
    for e in events:
        url = _url(e.link)
        label, tone, title = _notice(e)
        titles.append(title)
        lines.append(f"* {e.title}\n  {e.body.replace(chr(10), chr(10) + '  ')}" + (f"\n  {url}" if url else ""))
        cards.append(mail.alert_card(label, tone, title, e.body, url))
    heading = "New alert" if len(events) == 1 else f"{len(events)} new alerts"
    body = "".join(cards) + mail.button("Open Ledger", _url("/"))
    html_body = mail.layout(kicker="Alert", heading=heading, body=body, preheader=_preview(titles), footer=footer())
    return "\n\n".join(lines) + "\n", html_body


async def active_recipients(session: AsyncSession) -> list[str]:
    return list((await session.scalars(select(AlertRecipient.email).where(AlertRecipient.enabled))).all())


async def deliver(session: AsyncSession, events: list[AlertEvent], subject: str, body: str, html_body: str) -> str:
    to = await active_recipients(session)
    status, error = "sent", None
    if not smtp_configured():
        status, error = "skipped", "SMTP is not configured"
    elif not to:
        status, error = "skipped", "No alert recipients"
    else:
        try:
            await send_email(to, subject, body, html_body)
        except Exception as exc:  # delivery problems are recorded, not raised
            log.exception("Alert email failed")
            status, error = "failed", str(exc)[:1000]
    now = datetime.now(UTC)
    for e in events:
        e.status, e.error, e.recipients = status, error, to if status == "sent" else []
        e.sent_at = now if status == "sent" else None
    await session.commit()
    return status


# ---- weekly digest ----
TREND_WEEKS = 4
TREND_MIN = 75.0


class DigestNarrative(BaseModel):
    summary: str = Field(description="2-3 sentence headline of the week.")
    budget: str = Field(description="2-3 sentences on where this month's budgets stand.")
    notable: list[str] = Field(description="Up to 4 short bullets on charges worth a look.")
    trends: list[str] = Field(description="Up to 4 short bullets on meaningful spending changes.")
    recommendations: list[str] = Field(description="Up to 3 concrete, actionable suggestions.")


async def _notable_charges(session: AsyncSession, start: date, end: date, limit: int = 5) -> list[dict]:
    sql = text(
        f"""--sql
        SELECT t.id, t.txn_date, coalesce(mp.display_name, t.description) AS name,
               coalesce(c.name, 'Uncategorized') AS category, {EXPENSE} AS amount
        {FROM}
        LEFT JOIN merchant_profile mp ON mp.key = t.merchant
        WHERE {REPORTABLE} AND t.txn_date BETWEEN :start AND :end AND {EXPENSE} > 0
        ORDER BY amount DESC, t.id LIMIT :limit
        """
    )
    return [
        {"id": r.id, "date": r.txn_date, "name": r.name, "category": r.category, "amount": float(r.amount)}
        for r in await session.execute(sql, {"start": start, "end": end, "limit": limit})
    ]


async def _category_trends(session: AsyncSession, end: date, limit: int = 6) -> list[dict]:
    """Expense categories whose last 4 weeks differ most from the average 4 weeks over the 12 before."""
    recent_start = end - timedelta(weeks=TREND_WEEKS)
    sql = text(
        f"""--sql
        SELECT c.id AS category_id, coalesce(c.name, 'Uncategorized') AS category,
               sum(CASE WHEN t.txn_date > :recent_start THEN {EXPENSE} ELSE 0 END) AS recent,
               sum(CASE WHEN t.txn_date <= :recent_start THEN {EXPENSE} ELSE 0 END) / 3 AS baseline
        {FROM}
        WHERE {REPORTABLE} AND t.txn_date > :base_start AND t.txn_date <= :end
        GROUP BY 1, 2
        """
    )
    params = {"recent_start": recent_start, "base_start": recent_start - timedelta(weeks=3 * TREND_WEEKS), "end": end}
    out = []
    for r in await session.execute(sql, params):
        recent, baseline = float(r.recent or 0), float(r.baseline or 0)
        if max(recent, baseline) < TREND_MIN or abs(recent - baseline) < 0.25 * max(recent, baseline):
            continue
        out.append(
            {
                "category_id": r.category_id,
                "category": r.category,
                "last_4_weeks": round(recent, 2),
                "typical_4_weeks": round(baseline, 2),
                "change_pct": round((recent - baseline) / baseline * 100) if baseline else None,
            }
        )
    out.sort(key=lambda t: -abs(t["last_4_weeks"] - t["typical_4_weeks"]))
    return out[:limit]


async def digest_data(session: AsyncSession, today: date) -> dict:
    end = today - timedelta(days=1)
    start = end - timedelta(days=6)
    base_start = start - timedelta(days=56)
    sql = text(
        f"""--sql
        SELECT sum(CASE WHEN t.txn_date >= :start THEN {EXPENSE} ELSE 0 END) AS spent,
               sum(CASE WHEN t.txn_date >= :start THEN {INCOME} ELSE 0 END) AS income,
               sum(CASE WHEN t.txn_date < :start THEN {EXPENSE} ELSE 0 END) / 8 AS weekly_avg,
               count(*) FILTER (WHERE t.txn_date >= :start) AS n
        {FROM}
        WHERE {REPORTABLE} AND t.txn_date BETWEEN :base_start AND :end
        """
    )
    totals = (await session.execute(sql, {"start": start, "end": end, "base_start": base_start})).one()
    month_period = period_for("month", today)
    month = await budget_status(session, month_period, today)
    watch = [r for r in month["rows"] if r["kind"] == "expense" and r["status"] in ("over", "pace")]
    open_anomalies = (
        await session.scalars(select(Anomaly).where(Anomaly.status == "open").order_by(Anomaly.created_at.desc()).limit(3))
    ).all()
    return {
        "start": start,
        "end": end,
        "spent": float(totals.spent or 0),
        "income": float(totals.income or 0),
        "weekly_avg": float(totals.weekly_avg or 0),
        "count": totals.n or 0,
        "categories": [c for c in await category_breakdown(session, start, end) if c["spent"] > 0][:5],
        "merchants": await top_merchants(session, start, end, limit=5),
        "notable": await _notable_charges(session, start, end),
        "trends": await _category_trends(session, end),
        "budget_period": month_period,
        "budget_label": month["period"]["label"],
        "budget_elapsed": month["period"]["elapsed"],
        "budget_total": month["total"],
        "budget_rows": [r for r in month["rows"] if r["kind"] == "expense" and r["budget"] > 0][:12],
        "budget_watch": watch[:6],
        "unbudgeted": month["unbudgeted"][:5],
        "anomaly_count": await session.scalar(select(func.count()).select_from(Anomaly).where(Anomaly.status == "open")),
        "anomalies": [{"title": a.title, "detail": a.detail} for a in open_anomalies],
        "new_statements": await session.scalar(
            select(func.count()).select_from(Statement).where(func.date(Statement.created_at) >= start)
        ),
        "statements_to_review": await session.scalar(
            select(func.count()).select_from(Statement).where(Statement.status == "suggested")
        ),
        "uncategorized": await session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(
                Transaction.deleted_at.is_(None),
                Transaction.category_id.is_(None),
                Transaction.txn_date >= today - timedelta(days=30),
            )
        ),
        "duplicates": await session.scalar(
            select(func.count()).select_from(DuplicatePair).where(DuplicatePair.status == "pending")
        ),
    }


NARRATIVE_SYSTEM = (
    "You write the narrative part of a household's weekly finance email. The JSON facts cover the past week's "
    "spending and income, a typical week (average of the 8 before), top categories and merchants, the week's largest "
    "charges, this month's budgets (budget_elapsed = share of the month gone; projected = expected month-end, counting "
    "regular monthly bills once and extrapolating other spending at the current pace), "
    "unbudgeted spending, category trends (last 4 weeks vs the typical 4 weeks over the prior 12), open out-of-norm "
    "findings and items waiting for review.\n"
    "Use only these facts: never invent amounts, merchants, dates or causes. Be plain and specific, use $ amounts, "
    "no greetings, filler or moralizing.\n"
    "A budget whose actual is already close to (but not over) its amount is usually a once-a-month bill that has been "
    "paid, so don't call it on pace to overspend. Likewise a 4-week window "
    "can catch two monthly payments or none, so don't report rent, mortgage, loan, insurance or utility swings as "
    "trends.\n"
    "summary: the headline of the week, including how it compares with a typical week.\n"
    "budget: where the month stands against budget given how much of it has passed, naming the categories that are "
    "over or on pace to overspend; if there are no budgets, say so in one sentence.\n"
    "notable: charges worth a second look (unusually large, new, one-off or flagged), each with merchant, amount and "
    "why it stands out; empty if none.\n"
    "trends: meaningful changes with direction and size; empty if none.\n"
    "recommendations: concrete actions tied to the facts, e.g. a category to hold back on for the rest of the "
    "month, a bill or charge to check, or items to review; empty if nothing warrants action."
)


def _row(r: dict, *keys: str) -> dict:
    return {k: float(r[k]) if isinstance(r[k], Decimal) else r[k] for k in keys}


def _narrative_facts(d: dict) -> str:
    facts = {
        "week": f"{d['start']} to {d['end']}",
        "spent": round(d["spent"], 2),
        "income": round(d["income"], 2),
        "typical_week": round(d["weekly_avg"], 2),
        "transactions": d["count"],
        "top_categories": [_row(c, "category", "spent", "count") for c in d["categories"]],
        "top_merchants": [{"merchant": m["example"], "spent": m["spent"], "count": m["count"]} for m in d["merchants"]],
        "largest_charges": [_row(n, "date", "name", "category", "amount") for n in d["notable"]],
        "budget_month": d["budget_label"],
        "budget_elapsed": d["budget_elapsed"],
        "budget_total": _row(d["budget_total"], "spent", "budget", "left", "over_count", "count"),
        "budgets": [_row(r, "name", "actual", "budget", "projected", "status") for r in d["budget_rows"]],
        "unbudgeted": [_row(u, "name", "actual") for u in d["unbudgeted"]],
        "trends": [{k: v for k, v in t.items() if k != "category_id"} for t in d["trends"]],
        "out_of_norm_findings": d["anomalies"],
        "to_review": {
            "open_findings": d["anomaly_count"],
            "uncategorized_last_30_days": d["uncategorized"],
            "possible_duplicates": d["duplicates"],
            "statements_awaiting_review": d["statements_to_review"],
        },
    }
    return json.dumps(facts, default=str)


async def _ai_narrative(d: dict) -> DigestNarrative | None:
    if not get_settings().gemini_key:
        return None
    from ledger.ai.client import generate

    try:
        return await generate(
            _narrative_facts(d),
            purpose="weekly_digest",
            tier="main",
            schema=DigestNarrative,
            temperature=0.3,
            system=NARRATIVE_SYSTEM,
        )
    except Exception:
        log.exception("Digest narrative failed")
        return None


def _narrative_blocks(n: DigestNarrative | None) -> list[tuple[str, str, list[str]]]:
    """(heading, paragraph, bullets) for each non-empty part of the narrative after the headline."""
    if n is None:
        return []
    blocks = [
        ("Budget", n.budget.strip(), []),
        ("Notable charges", "", [b.strip() for b in n.notable if b.strip()]),
        ("Trends", "", [b.strip() for b in n.trends if b.strip()]),
        ("Recommendations", "", [b.strip() for b in n.recommendations if b.strip()]),
    ]
    return [b for b in blocks if b[1] or b[2]]


async def render_digest(session: AsyncSession, today: date, ai: bool = True) -> dict:
    d = await digest_data(session, today)
    label = f"{d['start']:%d %b} – {d['end']:%d %b %Y}"
    narrative = await _ai_narrative(d) if ai else None
    summary = narrative.summary.strip() if narrative else None
    blocks = _narrative_blocks(narrative)
    diff = d["spent"] - d["weekly_avg"]
    week = {"start": d["start"], "end": d["end"]}
    sections: list[tuple[str, list[tuple[str, str | None]]]] = [
        (
            "The week",
            [
                (
                    f"Spent {money(d['spent'])} across {d['count']} transactions; income {money(d['income'])}.",
                    txns_link(**week),
                ),
                (
                    f"Typical week (last 8): {money(d['weekly_avg'])} — this week was {money(abs(diff))} "
                    f"{'above' if diff >= 0 else 'below'}.",
                    None,
                ),
            ],
        ),
        (
            "Top categories",
            [
                (
                    f"{c['category']}: {money(c['spent'])}",
                    txns_link(category=c["category_id"], **week)
                    if c["category_id"]
                    else txns_link(status="uncategorized", **week),
                )
                for c in d["categories"]
            ]
            or [("No spending recorded.", None)],
        ),
        (
            "Top merchants",
            [
                (f"{m['example']}: {money(m['spent'])} ({m['count']})", f"/merchants?key={quote(m['merchant'], safe='')}")
                for m in d["merchants"]
            ]
            or [("—", None)],
        ),
    ]
    if d["notable"]:
        sections.append(
            (
                "Largest charges",
                [
                    (f"{n['date']:%d %b} · {n['name']} ({n['category']}): {money(n['amount'])}", f"/transactions/{n['id']}")
                    for n in d["notable"]
                ],
            )
        )
    bt = d["budget_total"]
    budget_lines = [(f"{money(bt['spent'])} of {money(bt['budget'])} budgeted so far.", "/budgets")] if bt["count"] else []
    budget_lines += [
        (
            f"{r['name']}: {money(r['actual'])} of {money(r['budget'])}"
            + (" — over" if r["status"] == "over" else f" — on pace for {money(r['projected'])}"),
            budget_link(r, d["budget_period"]),
        )
        for r in d["budget_watch"]
    ]
    if budget_lines:
        sections.append((f"Budgets · {d['budget_label']}", budget_lines))
    todo = []
    if d["anomaly_count"]:
        todo.append((_n(d["anomaly_count"], "out-of-norm finding") + " open", "/findings"))
    if d["statements_to_review"]:
        todo.append((_n(d["statements_to_review"], "statement") + " awaiting review", "/bills"))
    if d["new_statements"]:
        todo.append((_n(d["new_statements"], "new statement") + " this week", "/bills"))
    if d["uncategorized"]:
        todo.append(
            (
                _n(d["uncategorized"], "uncategorized transaction") + " (last 30 days)",
                txns_link(status="uncategorized", start=today - timedelta(days=30), end=today),
            )
        )
    if d["duplicates"]:
        todo.append((_n(d["duplicates"], "possible duplicate") + " to review", "/duplicates"))
    if todo:
        sections.append(("To review", todo))

    def text_line(line: str, link: str | None) -> str:
        url = _url(link)
        return f"- {line}" + (f"\n  {url}" if url else "")

    def text_block(heading: str, paragraph: str, bullets: list[str]) -> str:
        return f"{heading}\n" + (paragraph + "\n" if paragraph else "") + "".join(f"- {b}\n" for b in bullets)

    text_body = (
        (f"{summary}\n\n" if summary else "")
        + "".join(text_block(*b) + "\n" for b in blocks)
        + "\n\n".join(f"{h}\n" + "\n".join(text_line(*item) for item in lines) for h, lines in sections)
    )
    url = _url("/")
    if url:
        text_body += f"\n\nOpen Ledger: {url}"
    tone = "good" if diff <= 0 else "over" if diff > d["weekly_avg"] * 0.25 else "info"
    kpis = mail.stats(
        [
            ("Spent", money(d["spent"]), _n(d["count"], "transaction")),
            ("Typical week", money(d["weekly_avg"]), f"{money(abs(diff))} {'above' if diff >= 0 else 'below'}"),
            ("Income", money(d["income"]), None),
        ]
    )
    week_url = _url(txns_link(**week))
    inner = (mail.card(mail.multiline(summary), tone) if summary else "") + kpis
    if week_url:
        inner += f'<div style="font-size:13px">{mail.link("See this week’s transactions →", week_url)}</div>'
    inner += "".join(mail.prose(*b) for b in blocks)
    inner += "".join(mail.section(h, [(line, _url(link)) for line, link in lines]) for h, lines in sections[1:])
    inner += mail.button("Open Ledger", url)
    preheader = summary or f"Spent {money(d['spent'])} across {_n(d['count'], 'transaction')}."
    return {
        "subject": f"Ledger weekly digest · {label}",
        "title": f"Weekly digest · {label}",
        "text": text_body + "\n",
        "html": mail.layout(
            kicker="Weekly digest", heading=label, body=inner, preheader=_preview([preheader]), footer=footer()
        ),
        "summary": summary,
    }


async def send_digest(session: AsyncSession, today: date | None = None, force: bool = False) -> dict:
    today = today or date.today()
    rule = await session.get(AlertRule, "weekly_digest")
    if not force and (rule is None or not rule.enabled):
        return {"status": "disabled"}
    iso = today.isocalendar()
    key = f"digest:{iso.year}-W{iso.week:02d}" + (f":manual:{datetime.now(UTC):%Y%m%d%H%M%S}" if force else "")
    if await session.scalar(select(AlertEvent.id).where(AlertEvent.subject_key == key)):
        return {"status": "already_sent"}
    d = await render_digest(session, today)
    event = AlertEvent(kind="weekly_digest", subject_key=key, title=d["title"], body=d["text"], link="/")
    session.add(event)
    await session.commit()
    status = await deliver(session, [event], d["subject"], d["text"], d["html"])
    return {"status": status, "event_id": event.id}
