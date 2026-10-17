"""ai_call_log: thinking tokens and estimated cost

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-17 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "personal_finances"

# Snapshot of ledger.ai.pricing at this revision (USD per 1M tokens): model -> (input, output, long-prompt
# threshold, long input, long output). Earlier rows have no thinking-token count, so their estimate runs low.
BACKFILL = {
    "gemini-3.8-flash": (0.75, 3.75, None, None, None),
    "gemini-3.5-flash-lite": (0.30, 2.50, None, None, None),
    "gemini-3.1-pro-preview": (2.00, 12.00, 200_000, 4.00, 18.00),
}


def upgrade() -> None:
    op.add_column("ai_call_log", sa.Column("thinking_tokens", sa.Integer(), nullable=True), schema=S)
    op.add_column("ai_call_log", sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True), schema=S)
    for model, (rate_in, rate_out, long_prompt, long_in, long_out) in BACKFILL.items():
        cost = f"coalesce(input_tokens, 0) * {rate_in} + coalesce(output_tokens, 0) * {rate_out}"
        if long_prompt is not None:
            long_cost = f"coalesce(input_tokens, 0) * {long_in} + coalesce(output_tokens, 0) * {long_out}"
            cost = f"CASE WHEN input_tokens > {long_prompt} THEN {long_cost} ELSE {cost} END"
        op.execute(
            sa.text(
                f"UPDATE {S}.ai_call_log SET cost_usd = round(({cost}) / 1000000.0, 6) "
                "WHERE ok AND (input_tokens IS NOT NULL OR output_tokens IS NOT NULL) "
                "AND (model = :m OR model LIKE :prefix)"
            ).bindparams(m=model, prefix=f"{model}-%")
        )


def downgrade() -> None:
    op.drop_column("ai_call_log", "cost_usd", schema=S)
    op.drop_column("ai_call_log", "thinking_tokens", schema=S)
