"""Pair transfer legs between household accounts.

A transfer-category row is matched 1:1 with a row of the opposite amount in a different account within a few
days (the partner must be a transfer or uncategorized). Within one account, two transfer-category rows that offset on
the same day also pair up (e.g. a card moving a promotional balance to standard purchases). Both rows point at each
other via transfer_match_id.

A matched pair between two visible (not hidden) accounts is an internal transfer: both legs are left out of transaction
lists, reports and category/merchant pages (account pages keep them). Transfers that are unmatched, or matched with a
hidden account, show everywhere and count as income/expense by sign. Unmatched transfers in visible accounts are flagged
as anomalies.
Rows in hidden accounts are left out the same way; hidden accounts' balances still count toward net worth.
"""

from sqlalchemy import ColumnElement, and_, exists, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ledger.models import Account, Transaction

MATCH_WINDOW_DAYS = 7

# For raw SQL where the transaction is aliased `t`.
HIDDEN_ACCOUNT = "EXISTS (SELECT 1 FROM account xa WHERE xa.id = t.account_id AND xa.is_hidden)"
INTERNAL_TRANSFER = """(
    t.transfer_match_id IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM account ha WHERE ha.id = t.account_id AND ha.is_hidden)
    AND EXISTS (
        SELECT 1 FROM "transaction" tm
        WHERE tm.id = t.transfer_match_id AND tm.deleted_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM account hb WHERE hb.id = tm.account_id AND hb.is_hidden)
    )
)"""


def internal_transfer() -> ColumnElement[bool]:
    """ORM form of INTERNAL_TRANSFER for queries on Transaction."""
    match, ha, hb = aliased(Transaction), aliased(Account), aliased(Account)
    return and_(
        Transaction.transfer_match_id.is_not(None),
        ~exists().where(ha.id == Transaction.account_id, ha.is_hidden),
        exists().where(
            match.id == Transaction.transfer_match_id,
            match.deleted_at.is_(None),
            ~exists().where(hb.id == match.account_id, hb.is_hidden),
        ),
    )


def hidden_account() -> ColumnElement[bool]:
    """ORM form of HIDDEN_ACCOUNT."""
    return exists().where(Account.id == Transaction.account_id, Account.is_hidden)


_CANDIDATES = text(
    """--sql
    SELECT a.id AS a_id, b.id AS b_id, abs(b.txn_date - a.txn_date) AS gap, (cb.type IS NULL) AS b_uncategorized,
           (b.account_id IS NOT DISTINCT FROM a.account_id) AS same_account
    FROM "transaction" a
    JOIN category ca ON ca.id = a.category_id AND ca.type = 'transfer'
    JOIN "transaction" b ON b.deleted_at IS NULL AND b.amount = -a.amount
        AND abs(b.txn_date - a.txn_date) <= CAST(:window AS integer)
    LEFT JOIN category cb ON cb.id = b.category_id
    WHERE a.deleted_at IS NULL AND a.amount <> 0 AND coalesce(cb.type, 'transfer') = 'transfer'
      AND (b.account_id IS DISTINCT FROM a.account_id OR (b.txn_date = a.txn_date AND cb.type = 'transfer'))
    """
)

_CURRENT = text('SELECT id, transfer_match_id FROM "transaction" WHERE transfer_match_id IS NOT NULL')

_SET = text('UPDATE "transaction" SET transfer_match_id = :match WHERE id = :id')


async def match_transfers(session: AsyncSession) -> dict:
    """Recompute all transfer pairs (closest date first, transfer-to-transfer before uncategorized partners, other
    accounts before same-account offsets)."""
    edges = (await session.execute(_CANDIDATES, {"window": MATCH_WINDOW_DAYS})).all()
    edges.sort(key=lambda e: (e.b_uncategorized, e.same_account, e.gap, min(e.a_id, e.b_id), max(e.a_id, e.b_id)))
    want: dict[int, int] = {}
    for e in edges:
        if e.a_id not in want and e.b_id not in want:
            want[e.a_id], want[e.b_id] = e.b_id, e.a_id
    have = dict((await session.execute(_CURRENT)).all())
    changes = [{"id": i, "match": want.get(i)} for i in have.keys() | want.keys() if have.get(i) != want.get(i)]
    if changes:
        await session.execute(_SET, changes)
    return {"pairs": len(want) // 2, "changed": len(changes)}
