"""worker lease columns and worker-poll indexes on build_jobs and artifacts (`A-28`, `A-33`)

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-26

Adds:
- `build_jobs.lease_owner`, `build_jobs.lease_expires_at`, and
  `ix_build_jobs_status_created_at(status, created_at)` for atomic worker leasing
  and fast queue polling.
- `artifacts.sim_lease_owner`, `artifacts.sim_lease_expires_at`, and
  `ix_artifacts_sim_status(sim_status)` for atomic simulation leasing and polling.

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"), so `downgrade()` reverses `upgrade()` cleanly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("build_jobs", sa.Column("lease_owner", sa.Text(), nullable=True))
    op.add_column(
        "build_jobs",
        sa.Column("lease_expires_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_build_jobs_status_created_at",
        "build_jobs",
        ["status", "created_at"],
        unique=False,
    )

    op.add_column("artifacts", sa.Column("sim_lease_owner", sa.Text(), nullable=True))
    op.add_column(
        "artifacts",
        sa.Column("sim_lease_expires_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_artifacts_sim_status",
        "artifacts",
        ["sim_status"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_index("ix_artifacts_sim_status", table_name="artifacts")
    op.drop_column("artifacts", "sim_lease_expires_at")
    op.drop_column("artifacts", "sim_lease_owner")

    op.drop_index("ix_build_jobs_status_created_at", table_name="build_jobs")
    op.drop_column("build_jobs", "lease_expires_at")
    op.drop_column("build_jobs", "lease_owner")
