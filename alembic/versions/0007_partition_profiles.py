"""partition profiles

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-09

Task R3-be-2 — the flash maps this server supports, as a table instead of a code dict.
One row per partition-table fingerprint (`spec/device-protocol.md` → *Partition
layouts*): its name (`layout_id`, what an artifact's `partition_layout` carries), its OTA
slot size (the upload ceiling and the gate's promise), and where it came from:

* `builtin`: the two layouts every agent and library build knows, seeded below and never
  changed (the API answers 409);
* `user`: a map an operator entered by fingerprint;
* `detected`: a map a registered board announced that nobody knew. It lands **pending**
  (no name, not deployable) and becomes deployable when an operator names it (adopts it).

Rationale for every column and CHECK lives on `PartitionProfile` in
`src/fleetforge/db/models.py`; `alembic check` keeps the two in step. In short: the
fingerprint is the key because every board on an unknown map announces the same reserved
id `unknown` (R3-fw-5); `layout_id` is a nullable UNIQUE name in the `SafeSegment` shape
(it must be uploadable) and never `unknown`; `origin` is CHECKed because builtin
immutability depends on it and no device input reaches it; the remaining CHECKs are the
adoption state machine (named iff adopted, only `detected` may be pending, an adopted
profile has a slot, a slot is positive).

**The seed is two retyped literals**, never an import from `fleetforge`: a migration must
mean the same thing forever, and the code dict (`firmware.manifest.BUILTIN_LAYOUTS`) may
be renamed or moved. `tests/test_partition_profiles.py` pins the seeded rows equal to
`BUILTIN_LAYOUTS`, which `tests/test_deploy_precheck.py` pins equal to the spec table.
**A future builtin is a new migration that INSERTs one row.** Check first that no user
row already uses that `layout_id` (UNIQUE) or that fingerprint (PK): an operator may
already have entered the same map under another name.

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"), so `downgrade()` must work and is tested
(`tests/test_schema.py::test_migration_downgrades_cleanly`). **Unlike 0005 and 0006, the
downgrade drops operator-entered data**: every user profile and every adoption. After a
downgrade, artifacts labelled with a user layout id no longer upload or deploy until the
profile is re-created (by fingerprint, `POST /v1/partition-profiles`). The builtins and
the detected rows come back on their own (the seed, and the next announce).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# spec/device-protocol.md → Partition layouts, retyped. Never imported from fleetforge.
_BUILTIN_SEED = sa.text(
    """
    INSERT INTO partition_profiles
           (partition_table_sha256, layout_id, origin, ota_slot_size, adopted_at)
    VALUES ('1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed',
            'ab-4m-v1', 'builtin', 1966080, now()),
           ('05528998ae17fb6a7a5741443f9a7a4720c766f370fefc30814cbc3e391c1fc4',
            'ab-4m-arduino-v1', 'builtin', 1966080, now())
    """
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "partition_profiles",
        sa.Column("partition_table_sha256", sa.Text(), nullable=False),
        # NULL = pending: a detected map nobody has named yet.
        sa.Column("layout_id", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        # NULL only while pending, when the detecting board did not report it.
        sa.Column("ota_slot_size", sa.BigInteger(), nullable=True),
        sa.Column("flash_chip_size", sa.BigInteger(), nullable=True),
        # Informational, no FK: devices are soft-deleted (precedent: device_progress).
        sa.Column("detected_device_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("adopted_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "partition_table_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_partition_profiles_sha256_format"),
        ),
        # Server-authored vocabulary; builtin immutability depends on it.
        sa.CheckConstraint(
            "origin IN ('builtin', 'user', 'detected')",
            name=op.f("ck_partition_profiles_origin"),
        ),
        # Uploadable (SafeSegment) and never the reserved `unknown` (R3-fw-5).
        sa.CheckConstraint(
            "layout_id IS NULL OR "
            "(layout_id ~ '^[a-z0-9][a-z0-9-]{0,31}$' AND layout_id <> 'unknown')",
            name=op.f("ck_partition_profiles_layout_id_format"),
        ),
        # The adoption state machine.
        sa.CheckConstraint(
            "(layout_id IS NULL) = (adopted_at IS NULL)",
            name=op.f("ck_partition_profiles_adopted_is_named"),
        ),
        sa.CheckConstraint(
            "origin = 'detected' OR adopted_at IS NOT NULL",
            name=op.f("ck_partition_profiles_only_detected_pending"),
        ),
        sa.CheckConstraint(
            "adopted_at IS NULL OR ota_slot_size IS NOT NULL",
            name=op.f("ck_partition_profiles_adopted_has_slot"),
        ),
        sa.CheckConstraint(
            "ota_slot_size IS NULL OR ota_slot_size > 0",
            name=op.f("ck_partition_profiles_slot_positive"),
        ),
        sa.PrimaryKeyConstraint("partition_table_sha256", name=op.f("pk_partition_profiles")),
        sa.UniqueConstraint("layout_id", name=op.f("uq_partition_profiles_layout_id")),
    )
    op.execute(_BUILTIN_SEED)


def downgrade() -> None:
    """Downgrade schema — drops the table, operator profiles and adoptions included."""
    op.drop_table("partition_profiles")
