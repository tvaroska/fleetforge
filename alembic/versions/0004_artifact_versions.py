"""artifact version labels

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-16

Task R1-be-1 — the mutable label layer over the immutable blobs `0003` froze. Rationale
for the table, the FK and the absent `version` CHECK lives in the `ArtifactVersion`
docstring in `src/fleetforge/db/models.py`; `alembic check` keeps the two in step.

**Why a table and not a `version` column on `artifacts`.** `artifacts.sha256` is the
primary key, so a column would bind one digest to exactly one label and make re-tagging
byte-identical firmware a PK collision. `spec/device-protocol.md` puts `version` in the
`stage` payload and `spec/prd.md` → *Retention* counts "20 stored versions per platform",
so the label has to be first-class and queryable — but it is a pointer, not an identity.

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"), so `downgrade()` must work and is tested
(`tests/test_schema.py::test_migration_downgrades_cleanly`). It is an honest reverse for
the same reason `0003`'s was and for the last time: the table lands **empty** — the
endpoint that writes it ships in this same task, so nothing has written a row yet.
Once labels exist, a downgrade loses the only record of what a release is *called*,
while the blobs it names survive in the bucket.

Unlike `0003` this migration **does** carry a foreign key. `0003`'s header explains its
absence as a limitation, not a principle — `builds.outputs` names artifacts inside JSONB
and PostgreSQL cannot FK into JSONB. Here the reference is a plain column, so the
constraint is available, and `ondelete=RESTRICT` is what stops R2's pruner deleting bytes
a label still points at.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "artifact_versions",
        # NOT NULL where `artifacts.target` is nullable: a label with no platform cannot
        # answer "20 versions per platform", and every upload knows its target.
        sa.Column("target", sa.Text(), nullable=False),
        # No CHECK on purpose — the vocabulary is the user's (semver, a date, a CI build
        # number). The API rejects unsafe labels at the edge; see the model docstring.
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Mirrors `ck_artifacts_sha256_format`. The FK already guarantees the row exists;
        # this guarantees the value names a key `blobs.blob_key` will agree to build.
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_artifact_versions_sha256_format")
        ),
        sa.ForeignKeyConstraint(
            ["sha256"],
            ["artifacts.sha256"],
            name=op.f("fk_artifact_versions_sha256_artifacts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("target", "version", name=op.f("pk_artifact_versions")),
    )
    # "The 20 newest versions for this platform" — the R2 pruner's only query.
    op.create_index(
        "ix_artifact_versions_target_created_at",
        "artifact_versions",
        ["target", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_index("ix_artifact_versions_target_created_at", table_name="artifact_versions")
    op.drop_table("artifact_versions")
