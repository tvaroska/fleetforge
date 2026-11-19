"""device safe_mode column (D-3)

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-19

Adds `devices.safe_mode` (`BOOLEAN`, nullable) to persist `up/announce.safe_mode`
(`spec/device-protocol.md` -> `up/announce`). Written on every announce (`store.py`),
with an absent or null field stored as `false` so a board that exits safe mode clears
its latch immediately.

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"), so `downgrade()` reverses `upgrade()` cleanly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("devices", sa.Column("safe_mode", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_column("devices", "safe_mode")
