"""server-side build jobs queue table (R10-1, R10-2)

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-19

Creates `build_jobs` tracking queued, running, succeeded, failed, and cancelled
server-side container firmware builds alongside streamed `log_text` and exact cache hits.

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"), so `downgrade()` reverses `upgrade()` cleanly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "build_jobs",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("repo_url", sa.Text(), nullable=False),
        sa.Column("git_ref", sa.Text(), nullable=False),
        sa.Column("commit_sha", sa.Text(), nullable=True),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column(
            "partition_layout",
            sa.Text(),
            server_default=sa.text("'ab-4m-v1'"),
            nullable=False,
        ),
        sa.Column("idf_image", sa.Text(), nullable=False),
        sa.Column("cache_key", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column(
            "cache_hit",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("artifact_sha256", sa.Text(), nullable=True),
        sa.Column(
            "log_text",
            sa.Text(),
            server_default=sa.text("''"),
            nullable=False,
        ),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("triggered_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_build_jobs_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_build_jobs")),
    )
    op.create_index(op.f("ix_build_jobs_project_id"), "build_jobs", ["project_id"], unique=False)
    op.create_index(
        "ix_build_jobs_project_created_at",
        "build_jobs",
        ["project_id", sa.text("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_index("ix_build_jobs_project_created_at", table_name="build_jobs")
    op.drop_index(op.f("ix_build_jobs_project_id"), table_name="build_jobs")
    op.drop_table("build_jobs")
