"""artifact detached signature (R6-SEC-1)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-22

Task R6-SEC-1 — `POST /v1/artifact` either computes an `ecdsa-p256-sha256:<base64>`
detached signature over the firmware's SHA-256 digest (when `ARTIFACT_SIGNING_KEY` is set)
or verifies and records a caller-supplied `?sig=...` detached signature against the
configured public key rotation ring (`ARTIFACT_SIGNING_PUBLIC_KEYS`). The deploy router
propagates `artifacts.sig` into `stage.artifact.sig` so the agent (`ff_ota.c`) and simulator
(`simulator/device.py`) verify the slot's digest against the trusted public key ring before
moving the boot pointer.

**Nullable, no default.** NULL means unsigned (every pre-R6 row, or an upload when server
signing is not configured and no detached signature was supplied); a re-upload of the same
bytes with a valid signature fills a NULL `sig` without rewriting an existing one.

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"), so `downgrade()` must work and is tested
(`tests/test_schema.py::test_migration_downgrades_cleanly`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("artifacts", sa.Column("sig", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_column("artifacts", "sig")
