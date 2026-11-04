"""alert_rule monthly_digest: an in-depth report on the previous month, sent once its accounts have loaded

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-03 20:00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "personal_finances"


def upgrade() -> None:
    op.execute(
        f"INSERT INTO {S}.alert_rule (kind, enabled, params) VALUES ('monthly_digest', true, '{{}}') "
        "ON CONFLICT (kind) DO NOTHING"
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM {S}.alert_rule WHERE kind = 'monthly_digest'")
