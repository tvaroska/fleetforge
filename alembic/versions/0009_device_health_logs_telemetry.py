"""device health, reset reason, faults, logs and telemetry

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-15

Task R4-be-1 — the ingestor stops being log-only for what a board reports about its own
health. Seven nullable columns on `devices` (the last announced `reset_reason` and the
latest live heartbeat's health snapshot) and four new tables:

* `device_faults` — one row per boot whose reset reason is a fault (`panic`, `task_wdt`,
  `brownout`); the API counts them over a window, so the counts are derived, never stored.
* `device_logs` — `up/log` lines, bounded on write to the newest 5 000 per device and
  swept after 7 days.
* `device_telemetry` — `up/telemetry` samples, swept after 30 days and capped at the newest
  500 000 per device by the same sweep.
* `device_metrics` — the latest value per (device, metric), so the fleet list never scans
  the sample table.

Rationale for every column lives on the models in `src/fleetforge/db/models.py`; `alembic
check` keeps the two in step. **Plain tables, not hypertables** (DECISIONS 2026-10-15,
R4-be-1): dev and the test suite run `postgres:16`, and a schema that differs in prod is
one no test has run against. Retention is `fleetforge.retention`.

**The `devices` columns are nullable with no server default**, so adding them rewrites no
existing row; NULL is "not reported", which is what every existing board is until its next
announce or heartbeat. No CHECKs on them: both edges normalise first
(`ingestor/protocol.py`), and a CHECK would turn a normaliser bug into an `IntegrityError`
that loses the whole heartbeat. The new tables' FKs are `ON DELETE CASCADE`: these rows are
debugging data about a board, never KPI history (`deploy_events` keeps its `RESTRICT`).

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"), so `downgrade()` must work and is tested
(`tests/test_schema.py::test_migration_downgrades_cleanly`). The reverse is honest: it drops
device-reported debugging data only (health snapshots, fault rows, log lines, telemetry),
which boards re-supply as they keep reporting. No KPI history, credential or
operator-entered data lives in any of it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # The normalised `reset_reason` of the newest announce, written on every announce.
    op.add_column("devices", sa.Column("last_reset_reason", sa.Text(), nullable=True))
    # The latest live heartbeat's health, written as a group; `health_at` is its receipt time.
    op.add_column(
        "devices", sa.Column("health_at", postgresql.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column("devices", sa.Column("uptime_s", sa.BigInteger(), nullable=True))
    op.add_column("devices", sa.Column("rssi", sa.SmallInteger(), nullable=True))
    op.add_column("devices", sa.Column("free_heap", sa.BigInteger(), nullable=True))
    op.add_column("devices", sa.Column("min_free_heap", sa.BigInteger(), nullable=True))
    op.add_column("devices", sa.Column("boot_ok", sa.Boolean(), nullable=True))

    op.create_table(
        "device_faults",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.device_id"],
            name=op.f("fk_device_faults_device_id_devices"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_faults")),
    )
    # Serves the window count, the reconnect dedupe and the per-device trim.
    op.create_index(
        "ix_device_faults_device_id_at",
        "device_faults",
        ["device_id", sa.literal_column("at DESC")],
        unique=False,
    )

    op.create_table(
        "device_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("logged_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("received_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("dropped", sa.Integer(), server_default=sa.text("0"), nullable=False),
        # NULL = a gap marker only (lines lost, none of the batch usable).
        sa.Column("text", sa.Text(), nullable=True),
        sa.CheckConstraint("dropped >= 0", name=op.f("ck_device_logs_dropped_non_negative")),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.device_id"],
            name=op.f("fk_device_logs_device_id_devices"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_logs")),
    )
    # The tail read and the bound-on-write trim, both newest first with `id` breaking ties.
    op.create_index(
        "ix_device_logs_device_id_logged_at",
        "device_logs",
        ["device_id", sa.literal_column("logged_at DESC"), sa.literal_column("id DESC")],
        unique=False,
    )

    op.create_table(
        "device_telemetry",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("value", sa.Double(), nullable=False),
        sa.Column("sample_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.device_id"],
            name=op.f("fk_device_telemetry_device_id_devices"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_telemetry")),
    )
    # One metric's series, newest first.
    op.create_index(
        "ix_device_telemetry_device_id_name_sample_at",
        "device_telemetry",
        ["device_id", "name", sa.literal_column("sample_at DESC")],
        unique=False,
    )
    # The age sweep.
    op.create_index(
        "ix_device_telemetry_sample_at", "device_telemetry", ["sample_at"], unique=False
    )

    op.create_table(
        "device_metrics",
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("value", sa.Double(), nullable=False),
        sa.Column("sample_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.device_id"],
            name=op.f("fk_device_metrics_device_id_devices"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("device_id", "name", name=op.f("pk_device_metrics")),
    )


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_table("device_metrics")
    op.drop_index("ix_device_telemetry_sample_at", table_name="device_telemetry")
    op.drop_index("ix_device_telemetry_device_id_name_sample_at", table_name="device_telemetry")
    op.drop_table("device_telemetry")
    op.drop_index("ix_device_logs_device_id_logged_at", table_name="device_logs")
    op.drop_table("device_logs")
    op.drop_index("ix_device_faults_device_id_at", table_name="device_faults")
    op.drop_table("device_faults")
    op.drop_column("devices", "boot_ok")
    op.drop_column("devices", "min_free_heap")
    op.drop_column("devices", "free_heap")
    op.drop_column("devices", "rssi")
    op.drop_column("devices", "uptime_s")
    op.drop_column("devices", "health_at")
    op.drop_column("devices", "last_reset_reason")
