"""artifact pre-flight simulation verdict columns (R9-2)

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-19

Adds `sim_status` (`TEXT`, nullable), `sim_detail` (`TEXT`, nullable), and `sim_ran_at`
(`TIMESTAMPTZ`, nullable) to `artifacts` so pre-flight simulation verdicts (`passed`,
`failed`, `pending`, `running`, `skipped`) persist alongside content-addressed images.

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"), so `downgrade()` reverses `upgrade()` cleanly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("artifacts", sa.Column("sim_status", sa.Text(), nullable=True))
    op.add_column("artifacts", sa.Column("sim_detail", sa.Text(), nullable=True))
    op.add_column(
        "artifacts",
        sa.Column("sim_ran_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_column("artifacts", "sim_ran_at")
    op.drop_column("artifacts", "sim_detail")
    op.drop_column("artifacts", "sim_status")
