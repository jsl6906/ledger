"""balance-only accounts: unhide investment, retirement, property, vehicle, loan and mortgage accounts, and leave their
rows out of the chat views' reportable flag (mirrors ledger.analytics.transfers.HIDDEN_ACCOUNT)

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-21 19:00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "personal_finances"

BALANCE_ONLY = "('investment', 'retirement', 'property', 'vehicle', 'loan', 'mortgage')"

# Unchanged since 0021: a matched pair between two accounts that aren't hidden.
INTERNAL = f"""(t.transfer_match_id IS NOT NULL AND NOT coalesce(a.is_hidden, false)
    AND EXISTS (SELECT 1 FROM {S}.transaction tm LEFT JOIN {S}.account ma ON ma.id = tm.account_id
                WHERE tm.id = t.transfer_match_id AND tm.deleted_at IS NULL AND NOT coalesce(ma.is_hidden, false)))"""
OLD_REPORTABLE = f"""(NOT coalesce(c.hide_from_reports OR g.hide_from_reports, false)
    AND NOT coalesce(a.is_hidden, false) AND NOT {INTERNAL})"""
NEW_REPORTABLE = f"""(NOT coalesce(c.hide_from_reports OR g.hide_from_reports, false)
    AND NOT coalesce(a.is_hidden, false) AND NOT coalesce(a.account_type IN {BALANCE_ONLY}, false)
    AND NOT {INTERNAL})"""


def _txn_view(reportable: str) -> str:
    """v_transactions as of 0024 with the given reportable expression."""
    return f"""
        SELECT t.id, t.txn_date AS date, t.posted_date, t.description, t.merchant, t.amount,
               c.name AS category, g.name AS category_group, c.type AS category_type,
               coalesce(c.hide_from_reports OR g.hide_from_reports, false) AS hidden_from_reports,
               a.name AS account, a.account_type, i.name AS institution, m.name AS member,
               ARRAY(SELECT tg.name FROM {S}.transaction_tag tt JOIN {S}.tag tg ON tg.id = tt.tag_id
                     WHERE tt.transaction_id = t.id ORDER BY tg.name) AS tags,
               (SELECT string_agg(n.body, E'\\n' ORDER BY n.created_at, n.id)
                FROM {S}.transaction_note n WHERE n.transaction_id = t.id) AS notes,
               t.source_type AS source, t.budget_spread_months,
               CAST(t.created_at AS date) AS date_added,
               EXISTS (SELECT 1 FROM {S}.statement_transaction st WHERE st.transaction_id = t.id) AS has_statement,
               coalesce(a.is_hidden, false) AS account_hidden, {INTERNAL} AS internal_transfer,
               {reportable} AS reportable
        FROM {S}.transaction t
        LEFT JOIN {S}.category c ON c.id = t.category_id
        LEFT JOIN {S}.category_group g ON g.id = c.group_id
        LEFT JOIN {S}.account a ON a.id = t.account_id
        LEFT JOIN {S}.institution i ON i.id = a.institution_id
        LEFT JOIN {S}.household_member m ON m.id = t.member_id
        WHERE t.deleted_at IS NULL"""


def _monthly_view(reportable: str) -> str:
    """v_monthly_category as of 0021 with the given reportable expression."""
    return f"""
        SELECT CAST(date_trunc('month', t.txn_date) AS date) AS month, c.name AS category,
               g.name AS category_group, c.type AS category_type,
               coalesce(c.hide_from_reports OR g.hide_from_reports, false) AS hidden_from_reports,
               sum(t.amount) AS net_amount, count(*) AS transactions, {reportable} AS reportable
        FROM {S}.transaction t
        LEFT JOIN {S}.category c ON c.id = t.category_id
        LEFT JOIN {S}.category_group g ON g.id = c.group_id
        LEFT JOIN {S}.account a ON a.id = t.account_id
        WHERE t.deleted_at IS NULL
        GROUP BY 1, 2, 3, 4, 5, 8"""


def _replace_views(reportable: str) -> None:
    for name, sql in (("v_transactions", _txn_view(reportable)), ("v_monthly_category", _monthly_view(reportable))):
        op.execute(f"CREATE OR REPLACE VIEW {S}.{name} AS {sql}")
        op.execute(f"GRANT SELECT ON {S}.{name} TO pf_readonly")


def upgrade() -> None:
    op.execute(f"UPDATE {S}.account SET is_hidden = false WHERE is_hidden AND account_type IN {BALANCE_ONLY}")
    _replace_views(NEW_REPORTABLE)


def downgrade() -> None:
    # Which accounts were hidden before the upgrade isn't recorded, so they stay visible.
    _replace_views(OLD_REPORTABLE)
