"""staged cohort deploy policies table (R11-1)

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-19

Creates `deploy_policies` storing ordered multi-ring cohort rollout rules (`branch` or
`tag` glob -> ordered `device_groups` rings) with simulation, soak-window, free-heap,
safe-mode, and rollback auto-halt gates closing `V2`.

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"), so `downgrade()` reverses `upgrade()` cleanly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "deploy_policies",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("match_mode", sa.Text(), nullable=False),
        sa.Column("match_ref", sa.Text(), nullable=False),
        sa.Column("ring_group_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column(
            "max_parallel",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
        ),
        sa.Column(
            "require_sim_passed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "min_soak_s",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("min_free_heap_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_deploy_policies_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deploy_policies")),
        sa.UniqueConstraint(
            "project_id",
            "name",
            name=op.f("uq_deploy_policies_project_id_name"),
        ),
    )
    op.create_index(
        op.f("ix_deploy_policies_project_id"),
        "deploy_policies",
        ["project_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_index(op.f("ix_deploy_policies_project_id"), table_name="deploy_policies")
    op.drop_table("deploy_policies")
