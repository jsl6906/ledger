"""category sort order; resort groups and categories by transaction count

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-17 19:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "personal_finances"

# Mirrors ledger.api.reference.resort_categories: most-used first (live transactions), ties by name, steps of 10.
RESORT_GROUPS = f"""
    UPDATE {S}.category_group g SET sort_order = r.rn * 10
    FROM (SELECT g.id, row_number() OVER (ORDER BY count(t.id) DESC, lower(g.name)) AS rn
          FROM {S}.category_group g
          LEFT JOIN {S}.category c ON c.group_id = g.id
          LEFT JOIN {S}.transaction t ON t.category_id = c.id AND t.deleted_at IS NULL
          GROUP BY g.id) r
    WHERE r.id = g.id"""
RESORT_CATEGORIES = f"""
    UPDATE {S}.category c SET sort_order = r.rn * 10
    FROM (SELECT c.id, row_number() OVER (PARTITION BY c.group_id ORDER BY count(t.id) DESC, lower(c.name)) AS rn
          FROM {S}.category c
          LEFT JOIN {S}.transaction t ON t.category_id = c.id AND t.deleted_at IS NULL
          GROUP BY c.id) r
    WHERE r.id = c.id"""


def upgrade() -> None:
    op.add_column("category", sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False), schema=S)
    op.execute(RESORT_GROUPS)
    op.execute(RESORT_CATEGORIES)


def downgrade() -> None:
    op.drop_column("category", "sort_order", schema=S)
