"""move each transaction's static note into transaction_note and drop the column

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-18 11:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "personal_finances"

# One note per non-blank line (merges used to append lines to the static note). "Imported from <file>" lines are
# dropped: the transaction's sources already name the file. A line found in the notes of an import row backing the
# transaction is credited to that import (preferring the row that created it); "Also described as" lines came from
# duplicate merges; anything else was typed by hand.
MIGRATE_NOTES = rf"""
    INSERT INTO {S}.transaction_note
        (transaction_id, body, source, import_batch_id, attachment_id, created_at, updated_at)
    SELECT l.transaction_id, l.body,
           CASE WHEN l.body LIKE 'Also described as: %' THEN 'merge'
                WHEN src.found THEN 'import'
                ELSE 'user' END,
           src.import_batch_id, src.attachment_id,
           coalesce(src.at, l.updated_at), coalesce(src.at, l.updated_at)
    FROM (
        SELECT DISTINCT ON (t.id, trim(x.line)) t.id AS transaction_id, trim(x.line) AS body, x.n, t.updated_at
        FROM {S}."transaction" t
        CROSS JOIN LATERAL regexp_split_to_table(t.notes, '\r?\n') WITH ORDINALITY AS x(line, n)
        WHERE nullif(trim(t.notes), '') IS NOT NULL
        ORDER BY t.id, trim(x.line), x.n
    ) l
    LEFT JOIN LATERAL (
        SELECT true AS found, s.import_batch_id, s.attachment_id, coalesce(b.committed_at, b.created_at) AS at
        FROM {S}.transaction_source s
        JOIN {S}.import_row r ON r.id = s.import_row_id
        LEFT JOIN {S}.import_batch b ON b.id = s.import_batch_id
        WHERE s.transaction_id = l.transaction_id AND position(l.body IN coalesce(r.notes, '')) > 0
        ORDER BY (s.role = 'created') DESC, s.created_at, s.id
        LIMIT 1
    ) src ON true
    WHERE l.body <> '' AND l.body NOT LIKE 'Imported from %'
      AND NOT EXISTS (SELECT 1 FROM {S}.transaction_note n
                      WHERE n.transaction_id = l.transaction_id AND trim(n.body) = l.body)
    ORDER BY l.transaction_id, l.n"""

# Downgrade: fold hand-typed and merge notes back into the column; import-sourced notes stay in transaction_note.
RESTORE_NOTES = f"""
    UPDATE {S}."transaction" t SET notes = n.body
    FROM (SELECT transaction_id, string_agg(body, E'\\n' ORDER BY created_at, id) AS body
          FROM {S}.transaction_note WHERE source IN ('user', 'merge') GROUP BY transaction_id) n
    WHERE n.transaction_id = t.id"""

# v_transactions as of 0021; only the notes column differs between the two versions.
INTERNAL = f"""(t.transfer_match_id IS NOT NULL AND NOT coalesce(a.is_hidden, false)
    AND EXISTS (SELECT 1 FROM {S}.transaction tm LEFT JOIN {S}.account ma ON ma.id = tm.account_id
                WHERE tm.id = t.transfer_match_id AND tm.deleted_at IS NULL AND NOT coalesce(ma.is_hidden, false)))"""
REPORTABLE = f"""(NOT coalesce(c.hide_from_reports OR g.hide_from_reports, false)
    AND NOT coalesce(a.is_hidden, false) AND NOT {INTERNAL})"""


def _txn_view(notes: str) -> str:
    return f"""
        SELECT t.id, t.txn_date AS date, t.posted_date, t.description, t.merchant, t.amount,
               c.name AS category, g.name AS category_group, c.type AS category_type,
               coalesce(c.hide_from_reports OR g.hide_from_reports, false) AS hidden_from_reports,
               a.name AS account, a.account_type, i.name AS institution, m.name AS member,
               ARRAY(SELECT tg.name FROM {S}.transaction_tag tt JOIN {S}.tag tg ON tg.id = tt.tag_id
                     WHERE tt.transaction_id = t.id ORDER BY tg.name) AS tags,
               {notes} AS notes, t.source_type AS source, t.budget_spread_months,
               CAST(t.created_at AS date) AS date_added,
               EXISTS (SELECT 1 FROM {S}.statement_transaction st WHERE st.transaction_id = t.id) AS has_statement,
               coalesce(a.is_hidden, false) AS account_hidden, {INTERNAL} AS internal_transfer,
               {REPORTABLE} AS reportable
        FROM {S}.transaction t
        LEFT JOIN {S}.category c ON c.id = t.category_id
        LEFT JOIN {S}.category_group g ON g.id = c.group_id
        LEFT JOIN {S}.account a ON a.id = t.account_id
        LEFT JOIN {S}.institution i ON i.id = a.institution_id
        LEFT JOIN {S}.household_member m ON m.id = t.member_id
        WHERE t.deleted_at IS NULL"""


JOINED_NOTES = f"""(SELECT string_agg(n.body, E'\\n' ORDER BY n.created_at, n.id)
                FROM {S}.transaction_note n WHERE n.transaction_id = t.id)"""


def _replace_view(sql: str) -> None:
    op.execute(f"CREATE OR REPLACE VIEW {S}.v_transactions AS {sql}")
    op.execute(f"GRANT SELECT ON {S}.v_transactions TO pf_readonly")


def upgrade() -> None:
    op.execute(MIGRATE_NOTES)
    _replace_view(_txn_view(JOINED_NOTES))
    op.drop_column("transaction", "notes", schema=S)


def downgrade() -> None:
    op.add_column("transaction", sa.Column("notes", sa.Text(), nullable=True), schema=S)
    op.execute(RESTORE_NOTES)
    _replace_view(_txn_view("t.notes"))
