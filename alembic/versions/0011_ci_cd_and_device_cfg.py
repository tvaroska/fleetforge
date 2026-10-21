"""project CD settings, artifact Git provenance, and device dev/cfg columns

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-20

Task CI-CD-be-1 / CUJ-2 — Git-driven continuous deployment (`projects` CD settings and
`artifact_versions` Git/CI provenance), per-device development flags (`devices.is_dev`
and `devices.stay_awake` for retained `dn/cfg`), and `secrets_rev` tracking on `projects`
(target revision) and `devices` (last announced revision).

Additive only: new columns on `projects`, `devices`, and `artifact_versions`. Rationale for
every column lives on the models in `src/fleetforge/db/models.py`; `alembic check` keeps the
two in step.

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"), so `downgrade()` must work and is tested
(`tests/test_schema.py::test_migration_downgrades_cleanly`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("projects", sa.Column("repo_url", sa.Text(), nullable=True))
    op.add_column(
        "projects",
        sa.Column("track_mode", sa.Text(), server_default=sa.text("'off'"), nullable=False),
    )
    op.add_column("projects", sa.Column("track_ref", sa.Text(), nullable=True))
    op.add_column(
        "projects",
        sa.Column("auto_deploy_target", sa.Text(), server_default=sa.text("'off'"), nullable=False),
    )
    op.add_column("projects", sa.Column("ci_webhook_url", sa.Text(), nullable=True))
    op.add_column("projects", sa.Column("ci_webhook_header", sa.Text(), nullable=True))
    op.add_column(
        "projects",
        sa.Column("secrets_rev", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("projects", sa.Column("otel_endpoint", sa.Text(), nullable=True))
    op.add_column("projects", sa.Column("otel_headers", sa.Text(), nullable=True))

    op.add_column(
        "devices",
        sa.Column("is_dev", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "devices",
        sa.Column("stay_awake", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("devices", sa.Column("secrets_rev", sa.Integer(), nullable=True))
    op.add_column("devices", sa.Column("mqtt_password_hash", sa.Text(), nullable=True))

    op.create_table(
        "project_secrets",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "key ~ '^[a-zA-Z0-9_.-]{1,15}$'", name="ck_project_secrets_secret_key_format"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_secrets_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("project_id", "key", name=op.f("pk_project_secrets")),
    )

    op.add_column("artifact_versions", sa.Column("repo_url", sa.Text(), nullable=True))
    op.add_column("artifact_versions", sa.Column("branch", sa.Text(), nullable=True))
    op.add_column("artifact_versions", sa.Column("commit_sha", sa.Text(), nullable=True))
    op.add_column("artifact_versions", sa.Column("tag", sa.Text(), nullable=True))
    op.add_column("artifact_versions", sa.Column("build_url", sa.Text(), nullable=True))
    op.add_column("artifact_versions", sa.Column("test_passed", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_column("artifact_versions", "test_passed")
    op.drop_column("artifact_versions", "build_url")
    op.drop_column("artifact_versions", "tag")
    op.drop_column("artifact_versions", "commit_sha")
    op.drop_column("artifact_versions", "branch")
    op.drop_column("artifact_versions", "repo_url")

    op.drop_table("project_secrets")

    op.drop_column("devices", "mqtt_password_hash")
    op.drop_column("devices", "secrets_rev")
    op.drop_column("devices", "stay_awake")
    op.drop_column("devices", "is_dev")

    op.drop_column("projects", "otel_headers")
    op.drop_column("projects", "otel_endpoint")
    op.drop_column("projects", "secrets_rev")
    op.drop_column("projects", "ci_webhook_header")
    op.drop_column("projects", "ci_webhook_url")
    op.drop_column("projects", "auto_deploy_target")
    op.drop_column("projects", "track_ref")
    op.drop_column("projects", "track_mode")
    op.drop_column("projects", "repo_url")
