"""artifact cargo type (H5 / R7-cargo-1)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-24

Adds `artifacts.artifact_type` (`TEXT NOT NULL DEFAULT 'app'`) to persist the
`stage.artifact.type` discriminator defined in `spec/device-protocol.md` (`app`,
`mcu_fw`, `model_weights`, `fpga_bitstream`, `data_partition`, or any future
additive cargo type). Non-`app` cargo uploads bypass ESP32 partition-table slot
limits and merged-image checks, and require the target device's `capabilities`
to advertise the cargo type before staging.

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"), so `downgrade()` must work and is tested
(`tests/test_schema.py::test_migration_downgrades_cleanly`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "artifacts",
        sa.Column(
            "artifact_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'app'"),
        ),
    )
    op.add_column(
        "devices",
        sa.Column(
            "sub_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_column("devices", "sub_versions")
    op.drop_column("artifacts", "artifact_type")
