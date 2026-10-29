"""household_member.can_sign_in: members allowed to sign in with the Google account matching their email

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-28 21:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "personal_finances"


def upgrade() -> None:
    op.add_column(
        "household_member",
        sa.Column("can_sign_in", sa.Boolean(), server_default="false", nullable=False),
        schema=S,
    )


def downgrade() -> None:
    op.drop_column("household_member", "can_sign_in", schema=S)
