"""Bills & statements: upload -> Gemini extraction -> suggested series/transaction links -> user approval.

Payments often reach the ledger days after their receipt is filed, so statements still waiting on one are re-matched
(without re-reading the document) whenever new transactions are committed. Paystubs go through the same flow but are
matched to the paycheck deposits (money in) around their pay date rather than to payments out.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import case, delete, func, or_, select, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.db.filters import id_in
from ledger.imports.parsing import detect_kind, parse_date
from ledger.imports.service import ImportError_, store_attachment
from ledger.models import (
    Attachment,
    Category,
    CategoryGroup,
    Statement,
    StatementSeries,
    StatementUsage,
    Tag,
    Transaction,
    statement_transaction,
    transaction_tag,
)

log = logging.getLogger(__name__)

AUTO_LINK_SCORE = 0.6
CHOSEN = "Chosen by you"
PAYSTUB = "paystub"


def is_paystub(st: Statement) -> bool:
    return st.document_type == PAYSTUB


async def create_statement(
    session: AsyncSession, data: bytes, filename: str, content_type: str | None, transaction_ids: list[int] | None = None
) -> Statement:
    from ledger.config import get_settings
    from ledger.jobs.worker import enqueue

    kind = detect_kind(filename)
    # Extension-less text/plain is an email body (receipts that arrive as the message itself).
    if kind != "document" and not (kind is None and content_type == "text/plain"):
        raise ImportError_("Upload a PDF or image of the bill or statement")
    if len(data) > get_settings().max_upload_mb * 1024 * 1024:
        raise ImportError_(f"File is larger than {get_settings().max_upload_mb} MB")
    att = await store_attachment(session, data, filename, content_type, source="statement")
    existing = await session.scalar(
        select(Statement)
        .where(Statement.attachment_id == att.id, Statement.status != "dismissed")
        .order_by(Statement.id.desc())
        .limit(1)
    )
    if existing:
        # The same file again (e.g. attached from a transaction after it was uploaded on its own): link, don't re-read.
        if existing.status == "failed":
            existing.status, existing.error = "processing", None
            existing.job_id = (await enqueue(session, "extract_bill", {"statement_id": existing.id})).id
        if transaction_ids:
            await add_links(session, existing, transaction_ids)
        return existing
    st = Statement(attachment_id=att.id, suggestion={"preset_transaction_ids": transaction_ids or []})
    session.add(st)
    await session.flush()
    job = await enqueue(session, "extract_bill", {"statement_id": st.id})
    st.job_id = job.id
    return st


async def _match_series(
    session: AsyncSession, vendor: str | None, service: str | None, paystub: bool = False
) -> tuple[int | None, str]:
    rows = (await session.scalars(select(StatementSeries).where(StatementSeries.is_active))).all()
    v, s = (vendor or "").lower(), (service or "").lower()
    for sr in rows:
        if v and s and (sr.vendor or "").lower() == v and (sr.service_type or "").lower() == s:
            return sr.id, f"Same vendor and service as {sr.name}"
    # Every paystub is a "Paycheck"; only the employer tells two earners' (or two jobs') pay apart.
    for sr in rows:
        if s and not paystub and (sr.name.lower() == s or (sr.service_type or "").lower() == s):
            return sr.id, f"Service type matches {sr.name}"
    for sr in rows:
        if v and (sr.vendor or "").lower() == v:
            return sr.id, f"{'Employer' if paystub else 'Vendor'} matches {sr.name}"
    return None, "No existing series matches; a new one will be created"


_CANDIDATES = text(
    """--sql
    SELECT t.id, t.txn_date, t.amount, t.description,
           similarity(coalesce(t.merchant, ''), lower(CAST(:vendor AS text))) AS sim,
           abs(abs(t.amount) - CAST(:amt AS numeric)) AS diff,
           EXISTS (
               SELECT 1 FROM statement_transaction x
               WHERE x.transaction_id = t.id AND x.statement_id IS DISTINCT FROM CAST(:sid AS bigint)
           ) AS already_linked,
           EXISTS (
               SELECT 1 FROM statement_transaction x
               JOIN statement s ON s.id = x.statement_id
               JOIN "transaction" t2 ON t2.id = x.transaction_id
               WHERE s.series_id = CAST(:series AS integer) AND t2.merchant = t.merchant
           ) AS series_merchant
    FROM "transaction" t
    WHERE t.deleted_at IS NULL
      AND sign(t.amount) = CAST(:sign AS integer)
      AND t.txn_date BETWEEN :lo AND :hi
      AND (CAST(:amt AS numeric) IS NULL
           OR abs(abs(t.amount) - CAST(:amt AS numeric)) <= greatest(1.00, CAST(:amt AS numeric) * 0.02))
    ORDER BY t.txn_date
    LIMIT 200
    """
)


def link_window(st: Statement) -> tuple[date, date]:
    """Dates a statement's payment may fall on: shortly before the document through a few weeks past its due date.

    A paycheck lands within days of its pay date (some banks post it early), and the next one is only a week or two
    later for the same amount, so a paystub's window stays tight around the pay date.
    """
    # created_at is a server default: unloaded (not lazily fetched) on a just-flushed row, which is created today anyway.
    created = sa_inspect(st).dict.get("created_at")
    today = created.date() if created else date.today()
    if is_paystub(st):
        if st.statement_date:
            return st.statement_date - timedelta(days=4), st.statement_date + timedelta(days=5)
        # No pay date printed: pay usually follows the period's end by up to a week or so.
        anchor = st.period_end or today
        return anchor - timedelta(days=2), anchor + timedelta(days=12)
    anchor = st.statement_date or st.period_end or st.due_date or today
    return anchor - timedelta(days=10), (st.due_date or anchor + timedelta(days=30)) + timedelta(days=20)


def amount_targets(st: Statement) -> list[Decimal | None]:
    """The amounts to look for: each direct deposit of a split paycheck, otherwise the statement's single amount."""
    deposits = [Decimal(str(round(d, 2))) for d in (st.extracted or {}).get("deposits") or [] if d]
    # An edited net pay that no longer adds up means the extracted split is stale.
    if is_paystub(st) and len(deposits) > 1 and (st.amount_due is None or abs(sum(deposits) - st.amount_due) <= 1):
        return deposits
    return [st.amount_due]


async def _scored(
    session: AsyncSession, st: Statement, series_id: int | None, amt: Decimal | None, split: bool
) -> list[dict]:
    lo, hi = link_window(st)
    paystub = is_paystub(st)
    rows = (
        await session.execute(
            _CANDIDATES,
            {
                "vendor": st.vendor or "",
                "amt": amt,
                "sign": 1 if paystub else -1,
                "series": series_id,
                "sid": st.id,
                "lo": lo,
                "hi": hi,
            },
        )
    ).mappings()
    out = []
    for r in rows:
        sim = float(r["sim"])
        if amt is None and sim < 0.3:
            continue
        score, reasons = 0.0, []
        if amt is not None:
            exact = r["diff"] == 0
            score += 0.5 if exact else 0.3
            what = f"split deposit of {money(amt)}" if split else "amount"
            reasons.append(f"Exact {what}" if exact else f"{what[0].upper()}{what[1:]} within {money(r['diff'])}")
            # Payroll descriptions rarely name the employer the way the stub does; the narrow pay-date window does
            # the disambiguating instead.
            if exact and paystub and st.statement_date:
                score += 0.1
                reasons.append("Within days of the pay date")
        score += 0.3 * sim
        if sim >= 0.3:
            reasons.append(f"{'Payer' if paystub else 'Payee'} resembles {st.vendor}")
        if r["series_merchant"]:
            score += 0.2
            reasons.append(
                "Same payer as earlier paychecks in this series" if paystub else "Same payee as earlier bills in this series"
            )
        if r["already_linked"]:
            score -= 0.3
            reasons.append("Already linked to another statement")
        out.append(
            {
                "transaction_id": r["id"],
                "date": r["txn_date"].isoformat(),
                "amount": str(r["amount"]),
                "description": r["description"],
                "score": round(max(score, 0), 3),
                "reason": "; ".join(reasons),
            }
        )
    out.sort(key=lambda c: c["score"], reverse=True)
    return out


async def link_candidates(session: AsyncSession, st: Statement, series_id: int | None) -> tuple[list[dict], list[int]]:
    """Likely payments (paycheck deposits, for a paystub), best first, and those confident enough to pre-select.

    Each expected amount gets its own best match, so every part of a split paycheck deposit is linked.
    """
    targets = amount_targets(st)
    merged: dict[int, dict] = {}
    picks: list[int] = []
    for amt in targets:
        found = await _scored(session, st, series_id, amt, split=len(targets) > 1)
        best = next((c for c in found if c["transaction_id"] not in picks), None)
        if best and best["score"] >= AUTO_LINK_SCORE:
            picks.append(best["transaction_id"])
        for c in [*found[:3], *([best] if best else [])]:
            if c["transaction_id"] not in merged or c["score"] > merged[c["transaction_id"]]["score"]:
                merged[c["transaction_id"]] = c
    return sorted(merged.values(), key=lambda c: c["score"], reverse=True), picks


def money(v) -> str:
    return f"${Decimal(v):,.2f}"


async def _pin(session: AsyncSession, candidates: list[dict], ids: Sequence[int], reason: str) -> list[dict]:
    """Put chosen transactions at the top of the candidates, adding any the matcher didn't find."""
    for tid in reversed(list(ids)):
        if (i := next((n for n, c in enumerate(candidates) if c["transaction_id"] == tid), None)) is not None:
            candidates.insert(0, candidates.pop(i))
            continue
        t = await session.get(Transaction, tid)
        if t and t.deleted_at is None:
            candidates.insert(
                0,
                {
                    "transaction_id": tid,
                    "date": t.txn_date.isoformat(),
                    "amount": str(t.amount),
                    "description": t.description,
                    "score": 1.0,
                    "reason": reason,
                },
            )
    return candidates


async def _category_for(session: AsyncSession, service: str | None) -> int | None:
    if not service:
        return None
    return await session.scalar(select(Category.id).where(func.lower(Category.name) == service.lower()))


_PAY_WORDS = ("paycheck", "salary", "payroll", "wage")


async def _paycheck_category(session: AsyncSession, service: str | None) -> int | None:
    """The household's income category for paychecks, whatever it's called (e.g. "Paycheck Income" in a "Paycheck"
    group); one matching by both its own and its group's name wins over e.g. a side job's "Home Care Paycheck"."""
    if found := await _category_for(session, service):
        return found
    hits = [
        case((or_(*[col.ilike(f"%{w}%") for w in _PAY_WORDS]), 1), else_=0) for col in (Category.name, CategoryGroup.name)
    ]
    return await session.scalar(
        select(Category.id)
        .join(CategoryGroup, CategoryGroup.id == Category.group_id)
        .where(Category.type == "income", Category.is_active, hits[0] + hits[1] > 0)
        .order_by((hits[0] + hits[1]).desc(), CategoryGroup.sort_order, Category.sort_order, Category.id)
        .limit(1)
    )


async def extract_into_statement(session: AsyncSession, st: Statement) -> dict:
    from ledger.ai.statements import extract_bill

    att = st.attachment
    content = await session.scalar(select(Attachment.content).where(Attachment.id == att.id))
    r = await extract_bill(content, att.mime_type, att.filename)
    st.document_type, st.vendor, st.summary = r.document_type, r.vendor, r.summary
    st.account_ref = r.account_last4
    st.statement_date, st.due_date = parse_date(r.statement_date), parse_date(r.due_date)
    st.period_start, st.period_end = parse_date(r.period_start), parse_date(r.period_end)
    st.amount_due = Decimal(str(round(r.amount_due, 2))) if r.amount_due is not None else None
    st.extracted = r.model_dump()
    st.usage = [
        StatementUsage(metric=u.metric[:60], value=Decimal(str(u.value)), unit=(u.unit or None), is_primary=u.is_primary)
        for u in r.usage
    ]
    paystub = r.document_type == PAYSTUB
    series_id, series_reason = await _match_series(session, r.vendor, r.service_type, paystub)
    candidates, picks = await link_candidates(session, st, series_id)
    presets = [i for i in st.suggestion.get("preset_transaction_ids", []) if i]
    candidates = await _pin(session, candidates, presets, CHOSEN)
    if paystub:
        new_name = f"{r.service_type or 'Paycheck'} · {r.vendor}" if r.vendor else (r.service_type or "Paycheck")
    else:
        new_name = r.service_type or r.vendor or "Other bills"
    st.suggestion = {
        **st.suggestion,
        "series_id": series_id,
        "series_reason": series_reason,
        "new_series_name": None if series_id else new_name[:120],
        "new_series_category_id": None
        if series_id
        else await (_paycheck_category if paystub else _category_for)(session, r.service_type),
        # No recurring service and no existing series: a one-off receipt, not a new bill named after the vendor.
        "one_off": series_id is None and not r.service_type and not paystub,
        "service_type": r.service_type,
        "candidates": candidates,
        "transaction_ids": presets or picks,
        "refreshed_at": datetime.now(UTC).isoformat(),
    }
    st.status, st.error = "suggested", None
    return {"candidates": len(candidates), "series_id": series_id}


async def refresh_links(session: AsyncSession, st: Statement) -> bool:
    """Re-match a suggested or approved statement against the ledger as it is now, without re-reading the document.

    A suggested statement with no payment chosen yet gets the best match pre-selected for approval (for a split
    paycheck, each deposit still missing); approved ones only get fresh candidates (linking them stays the user's
    call). True when the statement now has candidates.
    """
    sug = dict(st.suggestion or {})
    presets = [i for i in sug.get("preset_transaction_ids", []) if i]
    candidates, picks = await link_candidates(session, st, st.series_id if st.status == "approved" else sug.get("series_id"))
    candidates = await _pin(session, candidates, presets, CHOSEN)
    found = {c["transaction_id"] for c in candidates}
    chosen = [i for i in sug.get("transaction_ids", []) if i in found]
    if st.status == "suggested":
        wanted = len(amount_targets(st))
        if not chosen:
            chosen = [i for i in presets if i in found] or picks
        elif wanted > 1 and len(chosen) < wanted:
            chosen = [*chosen, *[p for p in picks if p not in chosen]][:wanted]
    st.suggestion = {
        **sug,
        "candidates": candidates,
        "transaction_ids": chosen,
        "refreshed_at": datetime.now(UTC).isoformat(),
    }
    return bool(candidates)


async def relink_unmatched(session: AsyncSession, start: date | None = None, end: date | None = None) -> int:
    """Refresh matches for statements still waiting on a payment whose link window overlaps [start, end].

    Covers suggested statements with nothing chosen and approved ones with no linked transaction. Returns how many
    now have candidates.
    """
    linked = select(statement_transaction.c.statement_id)
    rows = (
        (
            await session.scalars(
                select(Statement).where(Statement.status.in_(("suggested", "approved")), Statement.id.not_in(linked))
            )
        )
        .unique()
        .all()
    )
    matched = 0
    for st in rows:
        # A split paycheck keeps waiting until every deposit has a match.
        if st.status == "suggested" and len((st.suggestion or {}).get("transaction_ids") or []) >= len(amount_targets(st)):
            continue
        lo, hi = link_window(st)
        if (end and lo > end) or (start and hi < start):
            continue
        matched += await refresh_links(session, st)
    return matched


async def relink_after_insert(session: AsyncSession, txn_ids: Sequence[int]) -> int:
    """Called once new transactions are in the session; a failure here never blocks the caller's commit."""
    if not txn_ids:
        return 0
    lo, hi = (
        await session.execute(
            select(func.min(Transaction.txn_date), func.max(Transaction.txn_date)).where(id_in(Transaction.id, txn_ids))
        )
    ).one()
    if lo is None:
        return 0
    try:
        async with session.begin_nested():
            return await relink_unmatched(session, lo, hi)
    except Exception:
        log.exception("Re-matching statements after new transactions failed")
        return 0


async def _existing_ids(session: AsyncSession, ids: list[int]) -> None:
    if not ids:
        return
    found = (
        await session.scalars(select(Transaction.id).where(Transaction.id.in_(ids), Transaction.deleted_at.is_(None)))
    ).all()
    if len(found) != len(ids):
        raise ImportError_("Unknown transaction id")


async def _apply_series(session: AsyncSession, ids: list[int], series: StatementSeries) -> None:
    """Tag a series' payments and give uncategorized ones the series' category."""
    await session.execute(
        insert(transaction_tag)
        .values([{"transaction_id": i, "tag_id": series.tag_id} for i in ids])
        .on_conflict_do_nothing()
    )
    if series.category_id:
        await session.execute(
            Transaction.__table__.update()
            .where(Transaction.id.in_(ids), Transaction.category_id.is_(None))
            .values(category_id=series.category_id, category_source="statement")
        )


async def add_links(session: AsyncSession, st: Statement, transaction_ids: Sequence[int]) -> Statement:
    """Link more transactions to a statement. Approved statements are linked right away; ones awaiting approval get
    the transactions pre-selected (replacing an automatic guess) so approving links them."""
    ids = list(dict.fromkeys(transaction_ids))
    await _existing_ids(session, ids)
    if not ids:
        return st
    if st.status == "approved":
        await session.execute(
            insert(statement_transaction)
            .values([{"statement_id": st.id, "transaction_id": i} for i in ids])
            .on_conflict_do_nothing()
        )
        series = await session.get(StatementSeries, st.series_id) if st.series_id else None
        if series and series.tag_id:
            await _apply_series(session, ids, series)
        return st
    if st.status not in ("processing", "suggested"):
        raise ImportError_(f"Statement is {st.status}")
    sug = dict(st.suggestion or {})
    presets = [i for i in sug.get("preset_transaction_ids", []) if i]
    sug["preset_transaction_ids"] = list(dict.fromkeys([*presets, *ids]))
    if st.status == "suggested":
        kept = [i for i in sug.get("transaction_ids", []) if i in presets]
        sug["transaction_ids"] = list(dict.fromkeys([*kept, *ids]))
        sug["candidates"] = await _pin(session, list(sug.get("candidates", [])), ids, CHOSEN)
        sug["refreshed_at"] = datetime.now(UTC).isoformat()
    st.suggestion = sug
    return st


async def _series_tag(session: AsyncSession, name: str) -> int:
    await session.execute(insert(Tag).values(name=name[:80]).on_conflict_do_nothing())
    return await session.scalar(select(Tag.id).where(Tag.name == name[:80]))


async def _resolve_series(session: AsyncSession, st: Statement, body) -> StatementSeries:
    series_id = body.series_id if "series_id" in body.model_fields_set else None
    if series_id is None and not body.new_series_name and "series_id" not in body.model_fields_set:
        series_id = st.suggestion.get("series_id")
    if series_id:
        series = await session.get(StatementSeries, series_id)
        if series is None:
            raise ImportError_("Series not found")
    else:
        name = (body.new_series_name or st.suggestion.get("new_series_name") or st.vendor or "Other bills").strip()
        series = await session.scalar(select(StatementSeries).where(func.lower(StatementSeries.name) == name.lower()))
        if series is None:
            series = StatementSeries(
                name=name[:120],
                vendor=st.vendor,
                service_type=st.suggestion.get("service_type"),
                category_id=body.new_series_category_id
                if body.new_series_category_id is not None
                else st.suggestion.get("new_series_category_id"),
            )
            session.add(series)
            await session.flush()
    if series.tag_id is None:
        series.tag_id = await _series_tag(session, series.name)
    primary = next((u for u in st.usage if u.is_primary), st.usage[0] if st.usage else None)
    if primary and not series.primary_metric:
        series.primary_metric, series.unit = primary.metric, primary.unit
    if not series.vendor and st.vendor:
        series.vendor = st.vendor
    return series


async def approve_statement(session: AsyncSession, st: Statement, body) -> Statement:
    """body: schemas.StatementApprove."""
    for f in ("vendor", "period_start", "period_end", "statement_date", "due_date", "amount_due"):
        if f in body.model_fields_set:
            setattr(st, f, getattr(body, f))
    if body.usage is not None:
        st.usage = [StatementUsage(metric=u.metric, value=u.value, unit=u.unit, is_primary=u.is_primary) for u in body.usage]

    chose_series = "series_id" in body.model_fields_set or bool(body.new_series_name)
    one_off = (
        body.one_off if "one_off" in body.model_fields_set else (not chose_series and bool(st.suggestion.get("one_off")))
    )
    series = None if one_off else await _resolve_series(session, st, body)
    st.series_id = series.id if series else None

    ids = list(dict.fromkeys(body.transaction_ids))
    await _existing_ids(session, ids)
    await session.execute(delete(statement_transaction).where(statement_transaction.c.statement_id == st.id))
    if ids:
        await session.execute(
            insert(statement_transaction).values([{"statement_id": st.id, "transaction_id": i} for i in ids])
        )
    if ids and series:
        await _apply_series(session, ids, series)
    st.status, st.approved_at = "approved", datetime.now(UTC)
    return st


async def linked_transactions(session: AsyncSession, statement_ids: list[int]) -> dict[int, list[Transaction]]:
    if not statement_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(statement_transaction.c.statement_id, Transaction)
                .join(Transaction, Transaction.id == statement_transaction.c.transaction_id)
                .where(statement_transaction.c.statement_id.in_(statement_ids), Transaction.deleted_at.is_(None))
                .order_by(Transaction.txn_date)
            )
        )
        .unique()
        .all()
    )
    out: dict[int, list[Transaction]] = {}
    for sid, t in rows:
        out.setdefault(sid, []).append(t)
    return out
