"""device_logs.context — board snapshot per log line (OTEL-be-2)

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-06

Adds `device_logs.context` (nullable JSONB): the board's latest heartbeat state
(`firmware.version`, `device.rssi_dbm`, `device.free_heap_bytes`,
`device.min_free_heap_bytes`, `device.uptime_s`, `fleetforge.secrets_rev`,
`fleetforge.health_at`) as it stood when the `up/log` batch arrived, keyed by
OpenTelemetry attribute name. Server-derived from the `devices` row — never device
text — so a line keeps its context after the next heartbeat overwrites the row.
Rows written before this migration stay NULL.

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"), so `downgrade()` must work and is tested
(`tests/test_schema.py::test_migration_downgrades_cleanly`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "device_logs",
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_column("device_logs", "context")
