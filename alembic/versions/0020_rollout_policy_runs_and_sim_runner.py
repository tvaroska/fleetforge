"""persisted cohort rollout state, policy run state, and artifact sim_runner (`A-21`, `A-22`, `A-24`)

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-21

Adds:
- `artifacts.sim_runner`: records which simulation backend produced `sim_status`.
- `project_rollouts`: tracks open/completed/halted/superseded/cancelled cohort rollouts
  so the reconcile loop never re-stages finished or superseded versions.
- `deploy_policy_runs`: persists per-`(policy_id, version)` halt state, explicit resume
  audit fields, and per-ring first-completeness timestamps for soak timing.

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"), so `downgrade()` reverses `upgrade()` cleanly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("artifacts", sa.Column("sim_runner", sa.Text(), nullable=True))

    op.create_table(
        "project_rollouts",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column(
            "target_filter",
            sa.Text(),
            server_default=sa.text("'all'"),
            nullable=False,
        ),
        sa.Column("target_chip", sa.Text(), nullable=True),
        sa.Column("group_id", sa.Uuid(), nullable=True),
        sa.Column("policy_id", sa.Uuid(), nullable=True),
        sa.Column(
            "apply",
            sa.Text(),
            server_default=sa.text("'auto'"),
            nullable=False,
        ),
        sa.Column(
            "canary",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "max_parallel",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
        ),
        sa.Column(
            "override",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("sent_by", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'open'"),
            nullable=False,
        ),
        sa.Column("close_reason", sa.Text(), nullable=True),
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
        sa.Column("closed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["device_groups.id"],
            name=op.f("fk_project_rollouts_group_id_device_groups"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["deploy_policies.id"],
            name=op.f("fk_project_rollouts_policy_id_deploy_policies"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_rollouts_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_rollouts")),
    )
    op.create_index(
        op.f("ix_project_rollouts_project_id"),
        "project_rollouts",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_project_rollouts_project_status_updated",
        "project_rollouts",
        ["project_id", "status", sa.text("updated_at DESC")],
        unique=False,
    )

    op.create_table(
        "deploy_policy_runs",
        sa.Column("policy_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("halted_ring_index", sa.Integer(), nullable=True),
        sa.Column("halt_reason", sa.Text(), nullable=True),
        sa.Column("halted_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "ring_completed_at",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("resumed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resumed_by", sa.Text(), nullable=True),
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
            ["policy_id"],
            ["deploy_policies.id"],
            name=op.f("fk_deploy_policy_runs_policy_id_deploy_policies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("policy_id", "version", name=op.f("pk_deploy_policy_runs")),
    )


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_table("deploy_policy_runs")
    op.drop_index("ix_project_rollouts_project_status_updated", table_name="project_rollouts")
    op.drop_index(op.f("ix_project_rollouts_project_id"), table_name="project_rollouts")
    op.drop_table("project_rollouts")
    op.drop_column("artifacts", "sim_runner")
