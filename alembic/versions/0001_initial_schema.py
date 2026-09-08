"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-08

Task R0-db-1 — the registry: device_groups, devices, enrollment_tokens,
admin_tokens, deploy_events. Rationale for every column and constraint lives in the
docstrings of `src/fleetforge/db/models.py`; `alembic check` keeps the two in step.

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"). Consequences: `downgrade()` must work and is tested
(`tests/test_schema.py::test_migration_downgrades_cleanly`); tables are created in FK
dependency order and dropped in the exact reverse; no `CREATE EXTENSION` and no
hardcoded schema name, because in production fleetforge runs as a plain non-superuser
owner on a shared Postgres.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "device_groups",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_groups")),
        sa.UniqueConstraint("name", name=op.f("uq_device_groups_name")),
    )
    op.create_table(
        "devices",
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("platform_type", sa.Text(), nullable=False),
        sa.Column("proto", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("fw_version", sa.Text(), nullable=True),
        sa.Column("agent_version", sa.Text(), nullable=True),
        sa.Column("link_type", sa.Text(), nullable=False),
        sa.Column("power_class", sa.Text(), nullable=False),
        sa.Column("expected_wake_interval_s", sa.Integer(), nullable=True),
        sa.Column("parent_device_id", sa.Text(), nullable=True),
        sa.Column("partition_layout", sa.Text(), nullable=True),
        sa.Column("ota_slot_size", sa.BigInteger(), nullable=True),
        sa.Column(
            "capabilities",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("group_id", sa.Uuid(), nullable=True),
        sa.Column("last_seen", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("presence_reported", sa.Boolean(), nullable=True),
        sa.Column("broker_provisioned_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "enrolled_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decommissioned_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "device_id ~ '^[0-9a-f]{12}$'", name=op.f("ck_devices_device_id_format")
        ),
        sa.CheckConstraint(
            "power_class <> 'sleepy' "
            "OR (expected_wake_interval_s IS NOT NULL AND expected_wake_interval_s > 0)",
            name=op.f("ck_devices_sleepy_wake_interval"),
        ),
        sa.CheckConstraint(
            "power_class IN ('always_on', 'sleepy')", name=op.f("ck_devices_power_class")
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["device_groups.id"],
            name=op.f("fk_devices_group_id_device_groups"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_device_id"],
            ["devices.device_id"],
            name=op.f("fk_devices_parent_device_id_devices"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("device_id", name=op.f("pk_devices")),
    )
    op.create_index(op.f("ix_devices_group_id"), "devices", ["group_id"], unique=False)
    op.create_index(op.f("ix_devices_last_seen"), "devices", ["last_seen"], unique=False)
    op.create_index(
        op.f("ix_devices_parent_device_id"), "devices", ["parent_device_id"], unique=False
    )
    op.create_table(
        "enrollment_tokens",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("used_by_device_id", sa.Text(), nullable=True),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "used_by_device_id IS NULL OR used_at IS NOT NULL",
            name=op.f("ck_enrollment_tokens_used_consistency"),
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["device_groups.id"],
            name=op.f("fk_enrollment_tokens_group_id_device_groups"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["used_by_device_id"],
            ["devices.device_id"],
            name=op.f("fk_enrollment_tokens_used_by_device_id_devices"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_enrollment_tokens")),
    )
    op.create_index(
        op.f("ix_enrollment_tokens_expires_at"), "enrollment_tokens", ["expires_at"], unique=False
    )
    op.create_table(
        "admin_tokens",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), server_default=sa.text("'admin'"), nullable=False),
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{admin}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_tokens")),
    )
    op.create_table(
        "deploy_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("cmd_id", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("is_terminal", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("from_version", sa.Text(), nullable=True),
        sa.Column("artifact_version", sa.Text(), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # ON DELETE RESTRICT: KPI history is kept forever, so a device is retired by
        # setting devices.decommissioned_at, never by DELETE.
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.device_id"],
            name=op.f("fk_deploy_events_device_id_devices"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deploy_events")),
    )
    op.create_index("ix_deploy_events_cmd_id", "deploy_events", ["cmd_id"], unique=False)
    op.create_index(
        "ix_deploy_events_device_id_at",
        "deploy_events",
        ["device_id", sa.literal_column("at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_deploy_events_terminal",
        "deploy_events",
        ["device_id", sa.literal_column("at DESC")],
        unique=False,
        postgresql_where=sa.text("is_terminal"),
    )


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_index(
        "ix_deploy_events_terminal",
        table_name="deploy_events",
        postgresql_where=sa.text("is_terminal"),
    )
    op.drop_index("ix_deploy_events_device_id_at", table_name="deploy_events")
    op.drop_index("ix_deploy_events_cmd_id", table_name="deploy_events")
    op.drop_table("deploy_events")
    op.drop_table("admin_tokens")
    op.drop_index(op.f("ix_enrollment_tokens_expires_at"), table_name="enrollment_tokens")
    op.drop_table("enrollment_tokens")
    op.drop_index(op.f("ix_devices_parent_device_id"), table_name="devices")
    op.drop_index(op.f("ix_devices_last_seen"), table_name="devices")
    op.drop_index(op.f("ix_devices_group_id"), table_name="devices")
    op.drop_table("devices")
    op.drop_table("device_groups")
