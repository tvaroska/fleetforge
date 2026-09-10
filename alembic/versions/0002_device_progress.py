"""device_progress

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-10

Task S0-fw-1 — the boot/enrolment stage reports an agent posts over HTTPS while it is
still too early to be in the fleet. Rationale for every column lives in the
`DeviceProgress` docstring in `src/fleetforge/db/models.py`; `alembic check` keeps the
two in step.

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"). Consequences: `downgrade()` must work and is tested
(`tests/test_schema.py::test_migration_downgrades_cleanly`), and it is an honest
reverse here — dropping this table loses only debugging history, never KPI history or
a credential.

Deliberately **no foreign keys**: the rows worth having are the ones written before a
`devices` row exists (and, for `token_id`, possibly after the token row is swept), so
an FK would make exactly the failure case unrecordable.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "device_progress",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("token_id", sa.Uuid(), nullable=False),
        # No CHECK on `stage`: an agent the server cannot update must still be able to
        # say something new. The API bounds the string's shape, not its vocabulary.
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_progress")),
    )
    # Serves both reads: the arrivals query (newest row per device) and the row cap's
    # "which rows fall off the end for this device".
    op.create_index(
        "ix_device_progress_device_id_at",
        "device_progress",
        ["device_id", sa.literal_column("at DESC")],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_index("ix_device_progress_device_id_at", table_name="device_progress")
    op.drop_table("device_progress")
