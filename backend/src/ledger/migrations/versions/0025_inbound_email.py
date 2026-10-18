"""inbound_email: emails the inbox poller has filed, so already-read mail is filed only once

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-18 14:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "personal_finances"


def upgrade() -> None:
    op.create_table(
        "inbound_email",
        sa.Column("message_key", sa.String(300), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("filed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("message_key", name=op.f("pk_inbound_email")),
        schema=S,
    )


def downgrade() -> None:
    op.drop_table("inbound_email", schema=S)
