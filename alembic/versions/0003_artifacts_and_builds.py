"""artifacts and builds

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-14

Task S0-infra-4 — the content-addressed artifact metadata, frozen **before** R1 writes
the first object. Rationale for every column and constraint lives in the `Artifact` and
`Build` docstrings in `src/fleetforge/db/models.py`; the key rules themselves live in
`src/fleetforge/storage/blobs.py`; `alembic check` keeps models and migration in step.

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"). Consequences: `downgrade()` must work and is tested
(`tests/test_schema.py::test_migration_downgrades_cleanly`), and it is an honest reverse
here for one reason that will not hold again — **both tables land empty and unread.**
Nothing writes them until R1's upload endpoint and S0-infra-6, so dropping them loses
no rows at all. Once either has rows, a downgrade loses the only record of what a stored
digest *is*, while the blobs themselves survive in the bucket.

No `CREATE EXTENSION` and no hardcoded schema name, for the reason `0001`'s header
gives: in production fleetforge runs as a plain non-superuser owner on a shared
Postgres. No foreign keys either — `builds.outputs` references artifacts by digest
inside JSONB, which PostgreSQL cannot FK into (see the `Build` docstring for why that
trade was taken and what it obliges a future pruner to do).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "artifacts",
        # The digest IS the object key (`storage/blobs.blob_key`), hence a natural PK
        # and a format CHECK that is a security control rather than tidiness — the same
        # reasoning as `devices.device_id_format`.
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        # Plain TEXT, no CHECK: the vocabulary is `ArtifactKind`, advisory like
        # `LinkType` (`db/models.py`, convention 1).
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("partition_layout", sa.Text(), nullable=True),
        # The S0-infra-3 manifest identity, copied — never recomputed here.
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_artifacts_sha256_format")),
        # A zero-byte artifact is a deploy that ships nothing.
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_artifacts_size_positive")),
        sa.PrimaryKeyConstraint("sha256", name=op.f("pk_artifacts")),
    )
    # "The current app image for this chip."
    op.create_index("ix_artifacts_kind_target", "artifacts", ["kind", "target"], unique=False)
    op.create_table(
        "builds",
        sa.Column("cache_key", sa.Text(), nullable=False),
        sa.Column("key_inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("outputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("partition_layout", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("cache_key ~ '^[0-9a-f]{64}$'", name=op.f("ck_builds_cache_key_format")),
        # JSONB accepts `[]` and `"x"` as happily as an object; a reader doing
        # `outputs['app']` on a list gets a 500 rather than a cache miss.
        sa.CheckConstraint(
            "jsonb_typeof(key_inputs) = 'object'", name=op.f("ck_builds_key_inputs_object")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(outputs) = 'object'", name=op.f("ck_builds_outputs_object")
        ),
        sa.PrimaryKeyConstraint("cache_key", name=op.f("pk_builds")),
    )
    op.create_index(
        "ix_builds_target_partition_layout", "builds", ["target", "partition_layout"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_index("ix_builds_target_partition_layout", table_name="builds")
    op.drop_table("builds")
    op.drop_index("ix_artifacts_kind_target", table_name="artifacts")
    op.drop_table("artifacts")
