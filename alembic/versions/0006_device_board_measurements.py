"""device board measurements

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-05

Task R2b-be-6 — a board announces three measurements right after `ota_slot_size`
(`spec/device-protocol.md` → `up/announce`): `flash_chip_size`, the physical flash chip
size in bytes; `partition_table_sha256`, the fingerprint of its decoded partition table;
and `rollback_capable`, whether its bootloader has been seen to put an OTA image into
`PENDING_VERIFY`. The columns hold the **last value reported**, written on every
announce, and R2b-be-7's deploy pre-check reads them. Rationale for the columns lives on
`Device` in `src/fleetforge/db/models.py`; `alembic check` keeps the two in step.

`flash_chip_size` is BIGINT, not INTEGER: the agent reads it into a `uint32_t`, so a
valid value can pass int4's 2^31 - 1. The normaliser caps it at 2^32 - 1.

**Nullable, no default, no CHECK.** NULL is the spec's "unknown": every existing row
starts there, and so does every board on an agent that does not send the keys yet. No
server default, so adding the columns rewrites no existing row. No CHECK, because both
edges normalise first (`fleetforge.announce_fields`) and store a malformed value as
NULL; a CHECK would only turn a normaliser bug into an `IntegrityError` that loses the
whole announce, including the `fw_version` that proves an OTA landed.

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"), so `downgrade()` must work and is tested
(`tests/test_schema.py::test_migration_downgrades_cleanly`). The reverse is honest: it
drops device-reported data only, which every board re-supplies on its next announce. No
operator-entered data lives in any of the three columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Bytes, 1 .. 2^32 - 1 after normalisation; NULL when the board cannot read it.
    op.add_column("devices", sa.Column("flash_chip_size", sa.BigInteger(), nullable=True))
    # 64 lowercase hex characters after normalisation.
    op.add_column("devices", sa.Column("partition_table_sha256", sa.Text(), nullable=True))
    # NULL until the board has booted an OTA-written image (true or false after that).
    op.add_column("devices", sa.Column("rollback_capable", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_column("devices", "rollback_capable")
    op.drop_column("devices", "partition_table_sha256")
    op.drop_column("devices", "flash_chip_size")
