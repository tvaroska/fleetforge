"""artifact library marker verdict

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-09

Task R3-be-1 — `POST /v1/artifact` scans every accepted upload for the Fleetforge OTA
library marker (`spec/device-protocol.md` → *Library marker*, reader
`fleetforge.lib_marker`) and stores the verdict in `artifacts.has_lib_marker`. The deploy
pre-check reads the stored verdict, never the bytes, and gates a deploy of an unmarked
build as `no_library_marker` unless `override` names it. Rationale for the column lives on
`Artifact` in `src/fleetforge/db/models.py`; `alembic check` keeps the two in step.

**Nullable, no default, no CHECK.** NULL means never scanned: every row from before
R3-be-1 starts there, and NULL never warns (fail-open, the `devices.rollback_capable`
idiom). No server default, so adding the column rewrites no existing row. This migration
cannot backfill (it has no object store); a re-upload of the same bytes fills a NULL
verdict. No CHECK: a boolean has nothing to constrain.

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"), so `downgrade()` must work and is tested
(`tests/test_schema.py::test_migration_downgrades_cleanly`). The reverse is honest: it
drops server-derived data only, which a re-upload of the same bytes recomputes. No
operator-entered data lives in the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # NULL until scanned (every pre-R3-be-1 row); true or false from R3-be-1 on.
    op.add_column("artifacts", sa.Column("has_lib_marker", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_column("artifacts", "has_lib_marker")
