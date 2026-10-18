"""Housekeeping: prune old job/AI logs and attachments nothing refers to any more."""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.db.engine import get_sessionmaker
from ledger.jobs.worker import JobContext, job_handler

log = logging.getLogger(__name__)

JOB_DAYS = 90
AI_LOG_DAYS = 365
ORPHAN_ATTACHMENT_DAYS = 7

_STATEMENTS = {
    "jobs": f"""--sql
        DELETE FROM job WHERE status IN ('succeeded', 'failed', 'cancelled')
          AND coalesce(finished_at, created_at) < now() - interval '{JOB_DAYS} days'""",
    "ai_calls": f"DELETE FROM ai_call_log WHERE created_at < now() - interval '{AI_LOG_DAYS} days'",
    "attachments": f"""--sql
        DELETE FROM attachment a
        WHERE a.created_at < now() - interval '{ORPHAN_ATTACHMENT_DAYS} days'
          AND NOT EXISTS (SELECT 1 FROM import_batch x WHERE x.attachment_id = a.id)
          AND NOT EXISTS (SELECT 1 FROM statement x WHERE x.attachment_id = a.id)
          AND NOT EXISTS (SELECT 1 FROM chat_message x WHERE x.attachment_id = a.id)
          AND NOT EXISTS (SELECT 1 FROM backfill_file x WHERE x.attachment_id = a.id)
          AND NOT EXISTS (SELECT 1 FROM transaction_source x WHERE x.attachment_id = a.id)
          AND NOT EXISTS (SELECT 1 FROM transaction_note x WHERE x.attachment_id = a.id)""",
}


async def cleanup(session: AsyncSession) -> dict:
    removed = {}
    for name, sql in _STATEMENTS.items():
        removed[name] = (await session.execute(text(sql))).rowcount or 0
    await session.commit()
    log.info("Cleanup removed %s", removed)
    return removed


@job_handler("cleanup")
async def cleanup_job(ctx: JobContext) -> dict:
    async with get_sessionmaker()() as session:
        return await cleanup(session)


async def carry_merchants_forward(session: AsyncSession) -> dict:
    """Before re-keying, keep merchant identities across a change to the key rules.

    A brand-new key that takes over part of a merchant that keeps other transactions becomes an alias of it (the one
    sending the most, if several), so earlier merges keep holding. An old merchant whose automatic transactions then all
    land on one key is merged into it, so its name, merges and category rule follow. Returns {"merged": n, "aliased": n}.
    """
    from collections import Counter, defaultdict

    from sqlalchemy import func, select

    from ledger.models import MerchantProfile, Transaction
    from ledger.services.merchants import alias_map, canonical, merge
    from ledger.services.normalize import normalize_merchant

    aliases = await alias_map(session)
    profiled = set(await session.scalars(select(MerchantProfile.key)))
    rows = await session.execute(
        select(Transaction.merchant, Transaction.description, func.count())
        .where(Transaction.merchant.is_not(None), Transaction.merchant_source.is_distinct_from("user"))
        .group_by(Transaction.merchant, Transaction.description)
    )
    counts: dict[str, Counter] = defaultdict(Counter)
    for merchant, description, n in rows:
        counts[merchant][normalize_merchant(description)] += n

    senders: dict[str | None, Counter] = defaultdict(Counter)
    for merchant, keys in counts.items():
        for k, n in keys.items():
            if canonical(k, aliases) != merchant:
                senders[canonical(k, aliases)][merchant] += n

    def destinations(merchant: str) -> set[str | None]:
        return {canonical(k, aliases) for k in counts[merchant]}

    # Merchants that keep some transactions, split across several keys or take in others can't simply be renamed.
    keepers = {m for m in counts if m in senders or len(destinations(m) - {m}) > 1 or m in destinations(m)}

    aliased = 0
    for dest, by_merchant in senders.items():
        if not dest or dest in profiled or dest in counts or dest in aliases:
            continue
        owners = [m for m in by_merchant if m in keepers]
        if owners:
            owner = max(owners, key=lambda m: (by_merchant[m], m))
            session.add(MerchantProfile(key=dest, alias_of=owner))
            aliases[dest] = owner
            aliased += 1

    merged = 0
    for merchant in counts.keys() - keepers:
        (dest,) = destinations(merchant)
        if dest and dest != merchant:
            await merge(session, merchant, dest)
            merged += 1
    await session.flush()
    return {"merged": merged, "aliased": aliased}


async def renormalize(session: AsyncSession, chunk: int = 5000, dry_run: bool = False) -> dict:
    """Recompute merchant keys (honouring merges and manual overrides) and fingerprints.

    Returns {"merged": n, "aliased": n, "updated": n}; with dry_run nothing is saved.
    """
    from sqlalchemy import bindparam, select, update

    from ledger.models import Transaction
    from ledger.services.merchants import alias_map, canonical
    from ledger.services.normalize import fingerprint, normalize_merchant

    summary = await carry_merchants_forward(session)
    if not dry_run:
        await session.commit()
    aliases = await alias_map(session)
    changed, last_id = 0, 0
    while True:
        rows = (
            await session.execute(
                select(
                    Transaction.id,
                    Transaction.account_id,
                    Transaction.txn_date,
                    Transaction.amount,
                    Transaction.description,
                    Transaction.merchant,
                    Transaction.merchant_source,
                    Transaction.fingerprint,
                )
                .where(Transaction.id > last_id)
                .order_by(Transaction.id)
                .limit(chunk)
            )
        ).all()
        if not rows:
            if dry_run:
                await session.rollback()
            return {**summary, "updated": changed}
        updates = []
        for r in rows:
            m = r.merchant if r.merchant_source == "user" else canonical(normalize_merchant(r.description), aliases)
            fp = fingerprint(r.account_id, r.txn_date, r.amount, r.description)
            if m != r.merchant or fp != r.fingerprint:
                updates.append({"tid": r.id, "m": m, "fp": fp})
        if updates and not dry_run:
            await session.execute(
                update(Transaction.__table__)
                .where(Transaction.__table__.c.id == bindparam("tid"))
                .values(merchant=bindparam("m"), fingerprint=bindparam("fp")),
                updates,
            )
            await session.commit()
        changed += len(updates)
        last_id = rows[-1].id
