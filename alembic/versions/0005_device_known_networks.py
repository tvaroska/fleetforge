"""device ssid and known_networks

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-05

Task R2b-be-5 — agent 0.4.6 announces the network it joined (`ssid`) and how many its
ff_cfg lists (`known_networks`), right after `link_type` (`spec/device-protocol.md` →
`up/announce`). The Fleet row shows them ("on: shed", "knows 2 networks"), and for an
offline board "last on: shed", so the columns hold the **last value reported**. Rationale
for the columns lives on `Device` in `src/fleetforge/db/models.py`; `alembic check`
keeps the two in step.

**Nullable, no default, no CHECK.** NULL is the spec's "not reported": every existing
row starts there, and the broker's retained announces fill 0.4.6 boards back in as soon
as the ingestor restarts on the new image. No server default, so adding the columns
rewrites no existing row. No CHECK, because both edges normalise first
(`fleetforge.announce_fields`) and store a malformed value as NULL; a CHECK would only
turn a normaliser bug into an `IntegrityError` that loses the whole announce, including
the `fw_version` that proves an OTA landed.

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"), so `downgrade()` must work and is tested
(`tests/test_schema.py::test_migration_downgrades_cleanly`). The reverse is honest: it
drops device-reported data only, which every board re-supplies on its next announce. No
operator-entered data lives in either column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # As written in the board's ff_cfg, at most 32 bytes; NULL on ethernet / older agents.
    op.add_column("devices", sa.Column("ssid", sa.Text(), nullable=True))
    # 1 to 4 today; a later writer may raise the cap, so no upper bound in the schema.
    op.add_column("devices", sa.Column("known_networks", sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_column("devices", "known_networks")
    op.drop_column("devices", "ssid")
