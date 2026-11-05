"""Monthly report: an in-depth review of the previous month, emailed once every active account appears to have loaded it.

An account is active for a month when it is open, not hidden, not balance-only and has transactions in the
ACTIVE_DAYS up to the month's end. It has loaded the month when a committed statement (or a statement closing
balance) reaches the month's last day, or when its transactions or its Tiller/SimpleFIN balance reach
MONTHLY_DIGEST_SETTLE_DAYS past it, so charges that post late are in. The daily check sends the report as soon as every
active account has loaded, or from MONTHLY_DIGEST_CAP_DAY of the next month regardless, naming the accounts that hadn't.
"""

import json
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote

from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.alerts import email_html as mail
from ledger.alerts.service import (
    Block,
    Line,
    Section,
    _n,
    _preview,
    _url,
    ai_narrative,
    anomaly_link,
    budget_link,
    charge_lines,
    digest_html_sections,
    digest_text,
    footer,
    largest_charges,
    money,
    review_counts,
    review_facts,
    review_lines,
    row_facts,
    send_report,
    txns_link,
)
from ledger.analytics.balances import monthly_balances
from ledger.analytics.reports import EXPENSE, FROM, REPORTABLE, cashflow, top_merchants
from ledger.analytics.transfers import BALANCE_ONLY_TYPES
from ledger.budgets.service import add_months, budget_status, period_for
from ledger.config import get_settings
from ledger.models import AlertEvent, AlertRule, Anomaly
from ledger.models.analytics import ANOMALY_LABELS
from ledger.models.sources import SOURCE_KINDS

KIND = "monthly_digest"
ACTIVE_DAYS = 90
HISTORY_MONTHS = 12
# A category's month only counts as a change when it or the typical month reaches CHANGE_MIN and they differ by
# at least CHANGE_SHARE of the larger.
CHANGE_MIN = 75.0
CHANGE_SHARE = 0.25


def previous_month(today: date) -> date:
    return add_months(date(today.year, today.month, 1), -1)


def month_end(start: date) -> date:
    return date(start.year, start.month, monthrange(start.year, start.month)[1])


def send_by(start: date) -> date:
    """The day the report on the month goes out even if some accounts haven't loaded it."""
    nxt = add_months(start, 1)
    return date(nxt.year, nxt.month, get_settings().monthly_digest_cap_day)


def subject_key(start: date) -> str:
    return f"monthly-digest:{start:%Y-%m}"


def signed(v: float) -> str:
    return ("-" if v < 0 else "") + money(abs(v))


_ACCOUNTS = text(
    """--sql
    SELECT a.id, a.name,
           (SELECT max(t.txn_date) FROM "transaction" t
            WHERE t.account_id = a.id AND t.deleted_at IS NULL) AS txn_through,
           (SELECT max(b.as_of) FROM account_balance b
            WHERE b.account_id = a.id AND b.source = ANY(:feeds)) AS feed_through,
           greatest(
             (SELECT max(b.as_of) FROM account_balance b WHERE b.account_id = a.id AND b.source = 'statement'),
             (SELECT max(sc.period_end) FROM statement_check sc
              JOIN import_batch ib ON ib.id = sc.import_batch_id
              WHERE sc.account_id = a.id AND ib.status = 'committed')
           ) AS statement_through
    FROM account a
    WHERE NOT a.is_closed AND NOT a.is_hidden AND a.account_type <> ALL(:balance_only)
      AND EXISTS (SELECT 1 FROM "transaction" t
                  WHERE t.account_id = a.id AND t.deleted_at IS NULL
                    AND t.txn_date > :active_from AND t.txn_date <= :end)
    ORDER BY a.name
    """
)


async def month_readiness(session: AsyncSession, start: date) -> dict:
    """Whether every active account has loaded the month starting `start` (see the module docstring)."""
    end = month_end(start)
    needed = end + timedelta(days=get_settings().monthly_digest_settle_days)
    params = {
        "feeds": list(SOURCE_KINDS),
        "balance_only": list(BALANCE_ONLY_TYPES),
        "active_from": end - timedelta(days=ACTIVE_DAYS),
        "end": end,
    }
    accounts = []
    for r in await session.execute(_ACCOUNTS, params):
        latest = max((d for d in (r.txn_through, r.feed_through) if d), default=None)
        loaded = bool(r.statement_through and r.statement_through >= end) or bool(latest and latest >= needed)
        through = max((d for d in (latest, r.statement_through) if d), default=None)
        accounts.append({"id": r.id, "name": r.name, "loaded": loaded, "loaded_through": through})
    sent = await session.scalar(select(AlertEvent.status).where(AlertEvent.subject_key == subject_key(start)))
    return {
        "month": start,
        "label": f"{start:%B %Y}",
        "end": end,
        "needed_through": needed,
        "send_by": send_by(start),
        "ready": all(a["loaded"] for a in accounts),
        "accounts": accounts,
        "missing": [a["name"] for a in accounts if not a["loaded"]],
        "sent": sent,
    }


class MonthlyNarrative(BaseModel):
    summary: str = Field(description="3-4 sentence overview of the month.")
    budget: str = Field(description="2-4 sentences on how the month ended against its budgets.")
    spending: list[str] = Field(description="Up to 5 short bullets on where the money went and what changed.")
    notable: list[str] = Field(description="Up to 4 short bullets on charges or merchants worth a look.")
    recommendations: list[str] = Field(description="Up to 4 concrete, actionable suggestions for the coming month.")


_CATEGORIES = text(
    f"""--sql
    SELECT c.id AS category_id, coalesce(c.name, 'Uncategorized') AS category,
           sum(CASE WHEN t.txn_date >= :start THEN {EXPENSE} ELSE 0 END) AS spent,
           sum(CASE WHEN t.txn_date < :start THEN {EXPENSE} ELSE 0 END) AS history,
           count(*) FILTER (WHERE t.txn_date >= :start AND {EXPENSE} > 0) AS n
    {FROM}
    WHERE {REPORTABLE} AND t.txn_date BETWEEN :base_start AND :end
    GROUP BY 1, 2
    """
)

_NEW_MERCHANTS = text(
    f"""--sql
    SELECT * FROM (
        SELECT coalesce(t.merchant, lower(t.description)) AS merchant,
               coalesce(max(mp.display_name), min(t.description)) AS example, min(t.txn_date) AS first_date,
               sum(CASE WHEN t.txn_date >= :start THEN {EXPENSE} ELSE 0 END) AS spent,
               count(*) FILTER (WHERE t.txn_date >= :start) AS n
        {FROM}
        LEFT JOIN merchant_profile mp ON mp.key = t.merchant
        WHERE {REPORTABLE} AND t.txn_date <= :end AND t.amount < 0
        GROUP BY 1
    ) m
    WHERE m.first_date >= :start AND m.spent > 0
    ORDER BY m.spent DESC LIMIT :limit
    """
)


async def _categories(session: AsyncSession, start: date, end: date, months: int) -> list[dict]:
    """Expense categories this month next to their typical month (average of the `months` before)."""
    params = {"start": start, "end": end, "base_start": add_months(start, -HISTORY_MONTHS)}
    out = []
    for r in await session.execute(_CATEGORIES, params):
        spent, typical = float(r.spent or 0), float(r.history or 0) / months if months else 0.0
        if spent <= 0 and typical <= 0:
            continue
        out.append(
            {
                "category_id": r.category_id,
                "category": r.category,
                "spent": round(spent, 2),
                "typical": round(typical, 2),
                "count": r.n,
                "change_pct": round((spent - typical) / typical * 100) if months and typical else None,
            }
        )
    out.sort(key=lambda c: -c["spent"])
    return out


def _changes(categories: list[dict], months: int, limit: int = 6) -> list[dict]:
    if not months:
        return []
    out = [
        c
        for c in categories
        if max(c["spent"], c["typical"]) >= CHANGE_MIN
        and abs(c["spent"] - c["typical"]) >= CHANGE_SHARE * max(c["spent"], c["typical"])
    ]
    out.sort(key=lambda c: -abs(c["spent"] - c["typical"]))
    return out[:limit]


async def _net_worth(session: AsyncSession, start: date, end: date) -> dict | None:
    nw = await monthly_balances(session, add_months(start, -1), end, today=end)
    if not nw["accounts"] or len(nw["net_worth"]) != 2:
        return None
    return {
        "start": nw["net_worth"][0],
        "end": nw["net_worth"][1],
        "assets": nw["assets"][1],
        "liabilities": nw["liabilities"][1],
        "liquid_start": nw["liquid"][0],
        "liquid": nw["liquid"][1],
    }


async def monthly_data(session: AsyncSession, start: date) -> dict:
    end = month_end(start)
    flows = await cashflow(session, add_months(start, -HISTORY_MONTHS), end)
    month, prior = flows[-1], flows[-2]
    history = [m for m in flows[:-1] if m["count"]]
    n = len(history)
    typical = {k: sum(m[k] for m in history) / n if n else 0.0 for k in ("income", "expenses", "net")}
    categories = await _categories(session, start, end, n)
    period = period_for("month", start)
    budgets = await budget_status(session, period, end)
    findings = (
        await session.scalars(
            select(Anomaly)
            .where(Anomaly.period == start, Anomaly.status.in_(("open", "reviewed")))
            .order_by(Anomaly.score.desc())
            .limit(5)
        )
    ).all()
    new_merchants = await session.execute(_NEW_MERCHANTS, {"start": start, "end": end, "limit": 5})
    return {
        "start": start,
        "end": end,
        "label": f"{start:%B %Y}",
        "income": month["income"],
        "spent": month["expenses"],
        "net": month["net"],
        "count": month["count"],
        "prior": {"label": f"{prior['month']:%B %Y}", **{k: prior[k] for k in ("income", "expenses", "net")}},
        "typical": typical,
        "history_months": n,
        "categories": categories,
        "changes": _changes(categories, n),
        "budget_period": period,
        "budget_total": budgets["total"],
        "budget_rows": [r for r in budgets["rows"] if r["kind"] == "expense" and r["budget"] > 0],
        "unbudgeted": budgets["unbudgeted"][:5],
        "merchants": await top_merchants(session, start, end, limit=8),
        "notable": await largest_charges(session, start, end, limit=8),
        "new_merchants": [
            {"merchant": r.merchant, "example": r.example, "spent": float(r.spent), "count": r.n} for r in new_merchants
        ],
        "net_worth": await _net_worth(session, start, end),
        "findings": list(findings),
        "review": await review_counts(session, start, end),
        "readiness": await month_readiness(session, start),
    }


NARRATIVE_SYSTEM = (
    "You write the narrative part of a household's monthly finance review email. The JSON facts cover the month's "
    "income, spending and net (income minus spending), the previous month, a typical month (average of up to 12 "
    "before), spending by category next to its typical month, the month's final budget results, unbudgeted "
    "spending, top merchants, largest charges, merchants used for the first time, net worth and liquid assets "
    "(checking + savings less credit card balances) at the start and end of the month, out-of-norm findings and "
    "items waiting for review.\n"
    "Use only these facts: never invent amounts, merchants, dates or causes. Be plain and specific, use $ amounts, "
    "no greetings, filler or moralizing. A month can catch two payments of a monthly bill or none, so treat rent, "
    "mortgage, loan, insurance or utility swings as timing unless the facts say otherwise. If some accounts hadn't "
    "loaded (accounts_not_loaded), say once that the totals may be incomplete.\n"
    "summary: the month in brief: income vs spending, the savings rate, and how it compares with the previous and "
    "a typical month.\n"
    "budget: how the month ended against its budgets, naming the ones that went over and by how much, and any "
    "large unbudgeted spending; if there are no budgets, say so in one sentence.\n"
    "spending: where the money went and the meaningful changes vs a typical month, with direction and size.\n"
    "notable: charges or new merchants worth a second look, each with merchant, amount and why; empty if none.\n"
    "recommendations: concrete actions for the coming month tied to the facts, e.g. a budget to adjust, a category "
    "to hold back on, a subscription or charge to check, or items to review; empty if nothing warrants action."
)


def _narrative_facts(d: dict) -> str:
    r = d["readiness"]
    facts = {
        "month": d["label"],
        "income": round(d["income"], 2),
        "spent": round(d["spent"], 2),
        "net": round(d["net"], 2),
        "transactions": d["count"],
        "previous_month": {k: round(v, 2) if isinstance(v, float) else v for k, v in d["prior"].items()},
        "typical_month": {k: round(v, 2) for k, v in d["typical"].items()},
        "typical_based_on_months": d["history_months"],
        "categories": [row_facts(c, "category", "spent", "typical", "change_pct") for c in d["categories"][:15]],
        "budget_total": row_facts(d["budget_total"], "spent", "budget", "left", "over_count", "count"),
        "budgets": [row_facts(b, "name", "actual", "budget", "left", "status") for b in d["budget_rows"]],
        "unbudgeted": [row_facts(u, "name", "actual") for u in d["unbudgeted"]],
        "top_merchants": [{"merchant": m["example"], "spent": m["spent"], "count": m["count"]} for m in d["merchants"]],
        "largest_charges": [row_facts(n, "date", "name", "category", "amount") for n in d["notable"]],
        "new_merchants": [{"merchant": m["example"], "spent": m["spent"], "count": m["count"]} for m in d["new_merchants"]],
        "net_worth": d["net_worth"],
        "out_of_norm_findings": [{"title": a.title, "detail": a.detail} for a in d["findings"]],
        "to_review": review_facts(d["review"]),
        "accounts_not_loaded": r["missing"],
    }
    return json.dumps(facts, default=str)


async def _ai_narrative(d: dict) -> MonthlyNarrative | None:
    return await ai_narrative(_narrative_facts(d), MonthlyNarrative, NARRATIVE_SYSTEM, KIND)


def _blocks(n: MonthlyNarrative | None) -> list[Block]:
    if n is None:
        return []

    def clean(items: list[str]) -> list[str]:
        return [b.strip() for b in items if b.strip()]

    blocks = [
        ("Budgets", n.budget.strip(), []),
        ("Where the money went", "", clean(n.spending)),
        ("Worth a look", "", clean(n.notable)),
        ("Recommendations", "", clean(n.recommendations)),
    ]
    return [b for b in blocks if b[1] or b[2]]


def _vs(value: float, typical: float) -> str:
    diff = value - typical
    return f"{money(abs(diff))} {'above' if diff >= 0 else 'below'} typical"


def _merchant_lines(rows: list[dict]) -> list[Line]:
    return [
        (f"{m['example']}: {money(m['spent'])} ({m['count']})", f"/merchants?key={quote(m['merchant'], safe='')}")
        for m in rows
    ]


def _sections(d: dict) -> list[Section]:
    month = {"start": d["start"], "end": d["end"]}
    t, p = d["typical"], d["prior"]
    rate = f" ({d['net'] / d['income'] * 100:.0f}% saved)" if d["income"] > 0 else ""
    overview: list[Line] = [
        (
            f"Income {money(d['income'])}, spending {money(d['spent'])} across {d['count']} transactions; "
            f"net {signed(d['net'])}{rate}.",
            txns_link(**month),
        ),
        (f"{p['label']}: income {money(p['income'])}, spending {money(p['expenses'])}.", None),
    ]
    if d["history_months"]:
        overview.append(
            (
                f"Typical month (last {d['history_months']}): income {money(t['income'])}, spending {money(t['expenses'])}.",
                None,
            )
        )
    sections: list[Section] = [("The month", overview)]

    nw = d["net_worth"]
    if nw:
        change = nw["end"] - nw["start"]
        liquid_change = nw["liquid"] - nw["liquid_start"]
        sections.append(
            (
                "Net worth",
                [
                    (
                        f"{signed(nw['end'])} at month end, {'up' if change >= 0 else 'down'} {money(abs(change))} "
                        "on the month.",
                        "/reports",
                    ),
                    (f"Assets {money(nw['assets'])}; liabilities {money(nw['liabilities'])}.", None),
                    (
                        f"Liquid assets (checking + savings less credit cards) {signed(nw['liquid'])}, "
                        f"{'up' if liquid_change >= 0 else 'down'} {money(abs(liquid_change))}.",
                        None,
                    ),
                ],
            )
        )

    def category_link(c: dict) -> str:
        if c["category_id"]:
            return txns_link(category=c["category_id"], **month)
        return txns_link(status="uncategorized", **month)

    cats = [c for c in d["categories"] if c["spent"] > 0][:10]
    if cats:
        sections.append(
            (
                "Spending by category",
                [
                    (
                        f"{c['category']}: {money(c['spent'])}"
                        + (
                            f" · typical {money(c['typical'])}"
                            + (f" ({c['change_pct']:+d}%)" if c["change_pct"] is not None else "")
                            if d["history_months"] and c["typical"]
                            else ""
                        ),
                        category_link(c),
                    )
                    for c in cats
                ],
            )
        )
    if d["changes"]:
        sections.append(
            (
                "Changes vs a typical month",
                [
                    (f"{c['category']}: {money(c['spent'])}, {_vs(c['spent'], c['typical'])}", category_link(c))
                    for c in d["changes"]
                ],
            )
        )

    bt = d["budget_total"]
    if d["budget_rows"]:
        lines: list[Line] = [(f"{money(bt['spent'])} of {money(bt['budget'])} budgeted spent.", "/budgets")]
        lines += [
            (
                f"{r['name']}: {money(r['actual'])} of {money(r['budget'])}"
                + (f" — {money(-r['left'])} over" if r["left"] < 0 else f" — {money(r['left'])} left"),
                budget_link(r, d["budget_period"]),
            )
            for r in d["budget_rows"]
        ]
        sections.append((f"Budgets · {d['label']}", lines))
    if d["unbudgeted"]:
        sections.append(
            (
                "Unbudgeted spending",
                [
                    (f"{u['name']}: {money(u['actual'])}", txns_link(category=u["category_id"], **month))
                    for u in d["unbudgeted"]
                ],
            )
        )
    if d["merchants"]:
        sections.append(("Top merchants", _merchant_lines(d["merchants"])))
    if d["notable"]:
        sections.append(("Largest charges", charge_lines(d["notable"])))
    if d["new_merchants"]:
        sections.append(("New merchants", _merchant_lines(d["new_merchants"])))
    if d["findings"]:
        sections.append(
            (
                "Findings this month",
                [
                    (f"{ANOMALY_LABELS.get(a.kind, 'Out-of-norm')} · {a.title} {money(a.amount)}", anomaly_link(a))
                    for a in d["findings"]
                ],
            )
        )
    todo = review_lines(d["review"], d["start"], d["end"], f"in {d['start']:%B}")
    if todo:
        sections.append(("To review", todo))

    r = d["readiness"]
    if r["accounts"]:
        if r["ready"]:
            loaded = [(f"All {_n(len(r['accounts']), 'active account')} had loaded {d['label']}.", None)]
        else:
            loaded = [
                (
                    f"{a['name']}: not loaded"
                    + (f" (data through {a['loaded_through']:%d %b})" if a["loaded_through"] else ""),
                    f"/accounts/{a['id']}",
                )
                for a in r["accounts"]
                if not a["loaded"]
            ]
        sections.append(("Accounts", loaded))
    return sections


async def render_monthly(session: AsyncSession, start: date, ai: bool = True) -> dict:
    d = await monthly_data(session, start)
    narrative = await _ai_narrative(d) if ai else None
    summary = narrative.summary.strip() if narrative else None
    blocks = _blocks(narrative)
    sections = _sections(d)
    r = d["readiness"]
    warning = (
        f"Some accounts hadn’t loaded {d['label']} yet ({', '.join(r['missing'])}), so totals may be incomplete."
        if r["missing"]
        else None
    )

    t = d["typical"]
    if not d["history_months"] or d["spent"] <= t["expenses"]:
        tone = "good"
    else:
        tone = "over" if d["spent"] > t["expenses"] * 1.25 else "info"
    rate = f"{d['net'] / d['income'] * 100:.0f}% saved" if d["income"] > 0 else None
    kpis = mail.stats(
        [
            ("Income", money(d["income"]), f"typical {money(t['income'])}" if d["history_months"] else None),
            ("Spending", money(d["spent"]), _vs(d["spent"], t["expenses"]) if d["history_months"] else None),
            ("Net", signed(d["net"]), rate),
        ]
    )
    inner = mail.card(mail.multiline(warning), "pace") if warning else ""
    inner += (mail.card(mail.multiline(summary), tone) if summary else "") + kpis
    month_url = _url(txns_link(start=d["start"], end=d["end"]))
    if month_url:
        see = f"See {d['label']}’s transactions →"
        inner += f'<div style="font-size:13px">{mail.link(see, month_url)}</div>'
    inner += "".join(mail.prose(*b) for b in blocks)
    inner += digest_html_sections(sections[1:])
    inner += mail.button("Open Ledger", _url("/"))
    preheader = summary or f"Income {money(d['income'])}, spending {money(d['spent'])}, net {signed(d['net'])}."
    body = digest_text(summary, blocks, sections)
    return {
        "subject": f"Ledger monthly report · {d['label']}",
        "title": f"Monthly report · {d['label']}",
        "text": (f"{warning}\n\n" if warning else "") + body,
        "html": mail.layout(
            kicker="Monthly report", heading=d["label"], body=inner, preheader=_preview([preheader]), footer=footer()
        ),
        "summary": summary,
        "readiness": r,
    }


async def send_monthly(session: AsyncSession, today: date | None = None, force: bool = False) -> dict:
    """Send the report on the previous month once its accounts have loaded (or from the cap day); forced sends go
    out now and don't count as the month's report."""
    today = today or date.today()
    start = previous_month(today)
    rule = await session.get(AlertRule, KIND)
    if not force and (rule is None or not rule.enabled):
        return {"status": "disabled"}
    key = subject_key(start) + (f":manual:{datetime.now(UTC):%Y%m%d%H%M%S}" if force else "")
    if await session.scalar(select(AlertEvent.id).where(AlertEvent.subject_key == key)):
        return {"status": "already_sent", "month": f"{start:%Y-%m}"}
    if not force:
        ready = await month_readiness(session, start)
        if not ready["ready"] and today < ready["send_by"]:
            return {"status": "waiting", "month": f"{start:%Y-%m}", "missing": ready["missing"]}
    d = await render_monthly(session, start)
    return {**await send_report(session, KIND, key, d), "month": f"{start:%Y-%m}"}
