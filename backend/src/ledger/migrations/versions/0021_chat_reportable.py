"""chat views: flag hidden accounts, internal transfers and reportable rows

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-12 12:00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "personal_finances"

# Mirrors ledger.analytics.transfers.INTERNAL_TRANSFER and ledger.analytics.reports.REPORTABLE (t, a, c, g joined).
INTERNAL = f"""(t.transfer_match_id IS NOT NULL AND NOT coalesce(a.is_hidden, false)
    AND EXISTS (SELECT 1 FROM {S}.transaction tm LEFT JOIN {S}.account ma ON ma.id = tm.account_id
                WHERE tm.id = t.transfer_match_id AND tm.deleted_at IS NULL AND NOT coalesce(ma.is_hidden, false)))"""
REPORTABLE = f"""(NOT coalesce(c.hide_from_reports OR g.hide_from_reports, false)
    AND NOT coalesce(a.is_hidden, false) AND NOT {INTERNAL})"""

TXN_COLUMNS = f"""
        SELECT t.id, t.txn_date AS date, t.posted_date, t.description, t.merchant, t.amount,
               c.name AS category, g.name AS category_group, c.type AS category_type,
               coalesce(c.hide_from_reports OR g.hide_from_reports, false) AS hidden_from_reports,
               a.name AS account, a.account_type, i.name AS institution, m.name AS member,
               ARRAY(SELECT tg.name FROM {S}.transaction_tag tt JOIN {S}.tag tg ON tg.id = tt.tag_id
                     WHERE tt.transaction_id = t.id ORDER BY tg.name) AS tags,
               t.notes, t.source_type AS source, t.budget_spread_months, CAST(t.created_at AS date) AS date_added,
               EXISTS (SELECT 1 FROM {S}.statement_transaction st WHERE st.transaction_id = t.id) AS has_statement"""
TXN_FROM = f"""
        FROM {S}.transaction t
        LEFT JOIN {S}.category c ON c.id = t.category_id
        LEFT JOIN {S}.category_group g ON g.id = c.group_id
        LEFT JOIN {S}.account a ON a.id = t.account_id
        LEFT JOIN {S}.institution i ON i.id = a.institution_id
        LEFT JOIN {S}.household_member m ON m.id = t.member_id
        WHERE t.deleted_at IS NULL"""

NEW = {
    "v_transactions": f"""{TXN_COLUMNS},
               coalesce(a.is_hidden, false) AS account_hidden, {INTERNAL} AS internal_transfer,
               {REPORTABLE} AS reportable
        {TXN_FROM}""",
    "v_monthly_category": f"""
        SELECT CAST(date_trunc('month', t.txn_date) AS date) AS month, c.name AS category,
               g.name AS category_group, c.type AS category_type,
               coalesce(c.hide_from_reports OR g.hide_from_reports, false) AS hidden_from_reports,
               sum(t.amount) AS net_amount, count(*) AS transactions, {REPORTABLE} AS reportable
        FROM {S}.transaction t
        LEFT JOIN {S}.category c ON c.id = t.category_id
        LEFT JOIN {S}.category_group g ON g.id = c.group_id
        LEFT JOIN {S}.account a ON a.id = t.account_id
        WHERE t.deleted_at IS NULL
        GROUP BY 1, 2, 3, 4, 5, 8""",
}

OLD = {
    "v_transactions": f"{TXN_COLUMNS}{TXN_FROM}",
    "v_monthly_category": f"""
        SELECT CAST(date_trunc('month', t.txn_date) AS date) AS month, c.name AS category,
               g.name AS category_group, c.type AS category_type,
               coalesce(c.hide_from_reports OR g.hide_from_reports, false) AS hidden_from_reports,
               sum(t.amount) AS net_amount, count(*) AS transactions
        FROM {S}.transaction t
        LEFT JOIN {S}.category c ON c.id = t.category_id
        LEFT JOIN {S}.category_group g ON g.id = c.group_id
        WHERE t.deleted_at IS NULL
        GROUP BY 1, 2, 3, 4, 5""",
}


def upgrade() -> None:
    for name, sql in NEW.items():
        op.execute(f"CREATE OR REPLACE VIEW {S}.{name} AS {sql}")
        op.execute(f"GRANT SELECT ON {S}.{name} TO pf_readonly")


def downgrade() -> None:
    for name, sql in OLD.items():
        op.execute(f"DROP VIEW IF EXISTS {S}.{name}")
        op.execute(f"CREATE VIEW {S}.{name} AS {sql}")
        op.execute(f"GRANT SELECT ON {S}.{name} TO pf_readonly")
