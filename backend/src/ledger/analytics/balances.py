"""Month-end balances per account, for the net worth and checking + savings charts.

Each account's balance at a month end comes from its latest snapshot on or before that day: account_balance rows, and
the closing balances of statements for checking, savings, loan, mortgage and investment accounts. Statement balances
only fill in history before an account's Tiller or SimpleFIN feed starts. Checking and savings balances also move
with the ledger: after a snapshot by the transactions since, and before the account's first snapshot by working back
from it through the transactions in between (those points are marked estimated). Homes, vehicles, loans and mortgages
move along a straight line between known values, a home's or vehicle's purchase price counting as one. Other accounts
only carry snapshots forward. Every account joins the totals at its first known value; closed accounts drop out after
their last balance or transaction. Hidden accounts are left out altogether.
"""

from bisect import bisect_right
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.sources import SOURCE_KINDS

LIABILITIES = ("credit_card", "loan", "mortgage")
CASH = ("checking", "savings")
# Values that drift steadily between valuations or statements rather than jumping with each transaction.
INTERPOLATED = ("property", "vehicle", "loan", "mortgage")


def month_points(start: date, end: date, today: date) -> list[date]:
    """The last day of each month from start's month to end's, cut off at today."""
    out: list[date] = []
    m = date(start.year, start.month, 1)
    while m <= end and m <= today:
        nxt = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
        out.append(min(nxt - timedelta(days=1), today))
        m = nxt
    return out


def week_points(days: int, today: date) -> list[date]:
    """Every seventh day going back `days` from today, oldest first."""
    return [today - timedelta(days=7 * k) for k in range(days // 7, -1, -1)]


def account_series(
    points: list[date],
    anchors: list[tuple[date, Decimal]],
    flows: list[tuple[date, Decimal]] | None = None,
    until: date | None = None,
    interpolate: bool = False,
) -> list[tuple[Decimal | None, bool]]:
    """(balance, estimated) at each point. `anchors`/`flows` are sorted by date, one entry per date; `flows` (net
    transaction amount per day) is given for accounts whose balance follows the ledger; `until` ends a closed account;
    `interpolate` draws a straight line between anchors instead of holding each until the next."""
    return [(v, e) for v, e, _ in explained_series(points, anchors, flows, until, interpolate)]


# How a point's balance was worked out from the anchors (see explained_series).
Basis = tuple[str, int, int | None] | None


def explained_series(
    points: list[date],
    anchors: list[tuple[date, Decimal]],
    flows: list[tuple[date, Decimal]] | None = None,
    until: date | None = None,
    interpolate: bool = False,
) -> list[tuple[Decimal | None, bool, Basis]]:
    """account_series with how each point was worked out: (kind, anchor index, next anchor index), the kind being
    "recorded" (an anchor on that day), "held" (the last anchor carried forward), "ledger" (the last anchor plus the
    transactions since), "between" (a straight line from one anchor to the next) or "estimated" (worked back from the
    first anchor through the transactions in between); None where there's no balance."""
    if not anchors:
        return [(None, False, None)] * len(points)
    a_dates = [d for d, _ in anchors]
    f_dates = [d for d, _ in flows or []]
    cum: list[Decimal] = []
    run = Decimal(0)
    for _, amt in flows or []:
        run += amt
        cum.append(run)

    def through(d: date) -> Decimal:
        i = bisect_right(f_dates, d)
        return cum[i - 1] if i else Decimal(0)

    out: list[tuple[Decimal | None, bool, Basis]] = []
    for p in points:
        if until is not None and p > until:
            out.append((None, False, None))
            continue
        i = bisect_right(a_dates, p)
        on_anchor = bool(i) and a_dates[i - 1] == p
        if i and interpolate and i < len(anchors):
            (d0, v0), (d1, v1) = anchors[i - 1], anchors[i]
            basis = ("recorded", i - 1, None) if on_anchor else ("between", i - 1, i)
            out.append((v0 + (v1 - v0) * Decimal((p - d0).days) / Decimal((d1 - d0).days), False, basis))
        elif i:
            as_of, bal = anchors[i - 1]
            kind = "recorded" if on_anchor else "ledger" if flows else "held"
            out.append((bal + through(p) - through(as_of) if flows else bal, False, (kind, i - 1, None)))
        elif flows and f_dates[0] <= p:
            as_of, bal = anchors[0]
            out.append((bal - (through(as_of) - through(p)), True, ("estimated", 0, None)))
        else:
            out.append((None, False, None))
    return out


_ACCOUNTS = """--sql
    SELECT id, name, account_type, is_closed FROM account WHERE {only} ORDER BY name
"""
_SNAPSHOTS = """--sql
    SELECT DISTINCT ON (account_id, as_of) account_id, as_of, balance, source FROM account_balance
    WHERE {only}
    ORDER BY account_id, as_of, source = 'statement', created_at DESC
"""
# Statement closing balances, dated by the period end printed on the statement and summed over the statement's accounts
# that map to the same ledger account. Checking and savings statements count only when their rows add up from the
# opening balance. Loan, mortgage and investment rows can't confirm a balance (interest, market moves), so whether
# theirs reconciled says nothing about the balance and every one counts. Credit card statements are left out.
_STATEMENT_CLOSINGS = """--sql
    SELECT sc.account_id, sc.import_batch_id, sc.account_ref, b.doc_meta->>'period_end' AS period_end,
           CASE WHEN sc.account_ref = '' THEN b.doc_meta->>'closing_balance'
                ELSE (SELECT x->>'closing_balance'
                      FROM jsonb_array_elements(coalesce(b.doc_meta->'accounts', '[]'::jsonb)) x
                      WHERE x->>'ref' = sc.account_ref LIMIT 1)
           END AS balance
    FROM statement_check sc
    JOIN import_batch b ON b.id = sc.import_batch_id
    JOIN account a ON a.id = sc.account_id
    WHERE b.status = 'committed' AND {only}
      AND ((a.account_type IN ('checking', 'savings') AND sc.trusted)
           OR a.account_type IN ('loan', 'mortgage', 'investment', 'retirement'))
"""
_FLOWS = """--sql
    SELECT account_id, coalesce(posted_date, txn_date) AS d, sum(amount) AS amount FROM "transaction"
    WHERE deleted_at IS NULL AND account_id = ANY(:ids)
    GROUP BY 1, 2 ORDER BY 1, 2
"""
_PURCHASES = """--sql
    SELECT account_id, purchase_date, purchase_price FROM asset
    WHERE purchase_date IS NOT NULL AND purchase_price IS NOT NULL AND {only}
"""


def _iso_date(s: str | None) -> date | None:
    """A date read off a document, or None when it's missing or malformed."""
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


async def _account_inputs(session: AsyncSession, account_id: int | None = None) -> list[tuple]:
    """(account, anchors, flows, until, origins) for every account that isn't hidden and has a known value, or only the
    one asked for, hidden or not. The first four are as account_series takes them, liabilities in amounts owed; origins
    says where each anchor came from: {"source": account_balance source | "statement" | "purchase", "refs": the
    statement accounts a closing was read from (last four), else []}."""

    def q(sql: str, column: str, everyone: str = "true") -> tuple:
        if account_id is None:
            return text(sql.format(only=everyone)), {}
        return text(sql.format(only=f"{column} = :a")), {"a": account_id}

    accounts = {r.id: r for r in await session.execute(*q(_ACCOUNTS, "id", "NOT is_hidden"))}
    snapshots = (await session.execute(*q(_SNAPSHOTS, "account_id"))).all()
    # Statements fill in history before an account's feed starts; from then on the feed is the record.
    feed_start: dict[int, date] = {}
    for r in snapshots:
        if r.source in SOURCE_KINDS:
            feed_start.setdefault(r.account_id, r.as_of)

    def before_feed(aid: int, d: date) -> bool:
        return aid not in feed_start or d < feed_start[aid]

    closings: dict[tuple[int, int], tuple[date, Decimal, list[str]]] = {}
    for r in await session.execute(*q(_STATEMENT_CLOSINGS, "sc.account_id")):
        as_of = _iso_date(r.period_end)
        if as_of is None or r.balance is None or not before_feed(r.account_id, as_of):
            continue
        _, so_far, refs = closings.get((r.account_id, r.import_batch_id), (as_of, Decimal(0), []))
        refs = [*refs, r.account_ref[-4:]] if r.account_ref else refs
        closings[(r.account_id, r.import_batch_id)] = (as_of, so_far + Decimal(r.balance), refs)
    # account -> day -> (balance, origin)
    anchors: dict[int, dict[date, tuple[Decimal, dict]]] = {}
    for (aid, _), (as_of, balance, refs) in closings.items():
        anchors.setdefault(aid, {})[as_of] = (balance, {"source": "statement", "refs": refs})
    # Feed and manual snapshots win over a statement closing on the same day.
    for r in snapshots:
        if r.source != "statement" or before_feed(r.account_id, r.as_of):
            anchors.setdefault(r.account_id, {})[r.as_of] = (r.balance, {"source": r.source, "refs": []})
    # A home's or vehicle's purchase is a known value too, unless something was recorded that day.
    for r in await session.execute(*q(_PURCHASES, "account_id")):
        anchors.setdefault(r.account_id, {}).setdefault(
            r.purchase_date, (r.purchase_price, {"source": "purchase", "refs": []})
        )
    anchors = {i: v for i, v in anchors.items() if i in accounts}
    cash_ids = [i for i in anchors if accounts[i].account_type in CASH]
    flows: dict[int, list[tuple[date, Decimal]]] = {}
    if cash_ids:
        for r in await session.execute(text(_FLOWS), {"ids": cash_ids}):
            flows.setdefault(r.account_id, []).append((r.d, r.amount))

    out = []
    for aid, by_date in anchors.items():
        a = accounts[aid]
        days = sorted(by_date)
        # Sources disagree on the sign of what's owed; work in amounts owed so lines between them stay straight.
        series = [(d, abs(by_date[d][0]) if a.account_type in LIABILITIES else by_date[d][0]) for d in days]
        own = flows.get(aid) if a.account_type in CASH else None
        until = None
        if a.is_closed:
            until = max(series[-1][0], own[-1][0]) if own else series[-1][0]
        out.append((a, series, own, until, [by_date[d][1] for d in days]))
    return out


async def account_balances(session: AsyncSession, points: list[date]) -> list[dict]:
    """Each account's balance at the given dates (liabilities negative, so they add up to net worth), largest first;
    hidden accounts and accounts with no balance at any of them are left out."""
    rows = []
    for a, series, own, until, _ in await _account_inputs(session):
        liability = a.account_type in LIABILITIES
        vals = account_series(points, series, own, until, a.account_type in INTERPOLATED)
        if all(v is None for v, _ in vals):
            continue
        rows.append(
            {
                "id": a.id,
                "name": a.name,
                "account_type": a.account_type,
                "liability": liability,
                "is_closed": a.is_closed,
                "values": [None if v is None else round(float(-abs(v) if liability else v), 2) for v, _ in vals],
                "estimated": [e for _, e in vals],
            }
        )
    rows.sort(key=lambda r: (r["liability"], -abs(next((v for v in reversed(r["values"]) if v is not None), 0))))
    return rows


async def account_history(
    session: AsyncSession, account_id: int, start: date | None, end: date, today: date | None = None
) -> list[dict]:
    """One account's balance from start (or its first known value) to end, worked out as account_balances does: weekly
    over two years or less, at month ends beyond that. Liabilities come as positive amounts owed. Each point says how it
    was worked out: basis = {"kind": see explained_series, "from": the anchor it starts from, "to": the anchor a
    "between" line runs to}, anchors given as {"as_of", "balance", "source", "refs"}."""
    inputs = await _account_inputs(session, account_id)
    if not inputs:
        return []
    a, series, own, until, origins = inputs[0]
    end = min(end, today or date.today())
    first = min(series[0][0], own[0][0]) if own else series[0][0]
    start = max(start or first, first)
    if start > end:
        return []
    if (end - start).days <= 731:
        points = [end - timedelta(days=7 * k) for k in range((end - start).days // 7, -1, -1)]
    else:
        points = month_points(start, end, end)
    vals = explained_series(points, series, own, until, a.account_type in INTERPOLATED)

    def anchor(i: int | None) -> dict | None:
        if i is None:
            return None
        return {"as_of": series[i][0], "balance": round(float(series[i][1]), 2), **origins[i]}

    return [
        {
            "as_of": p,
            "balance": round(float(v), 2),
            "estimated": e,
            "basis": {"kind": basis[0], "from": anchor(basis[1]), "to": anchor(basis[2])},
        }
        for p, (v, e, basis) in zip(points, vals, strict=True)
        if v is not None and basis is not None
    ]


async def monthly_balances(session: AsyncSession, start: date, end: date, today: date | None = None) -> dict:
    """Per-account month-end balances with monthly totals for net worth and checking + savings."""
    points = month_points(start, end, today or date.today())
    rows = await account_balances(session, points)

    def total(pick) -> list[float]:
        return [round(sum(r["values"][i] or 0 for r in rows if pick(r)), 2) for i in range(len(points))]

    assets = total(lambda r: not r["liability"])
    debts = [-v for v in total(lambda r: r["liability"])]
    return {
        "months": [date(p.year, p.month, 1) for p in points],
        "dates": points,
        "accounts": rows,
        "assets": assets,
        "liabilities": debts,
        "net_worth": [round(a - d, 2) for a, d in zip(assets, debts, strict=True)],
        "cash": total(lambda r: r["account_type"] in CASH),
    }
