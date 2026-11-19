"""swarm edge-relay gateways and binary delta OTA tables (V3-1, V3-2)

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-19

Creates `gateway_relays` (local edge artifact cache URL and cached `sha256` set per UGV
gateway device) and `artifact_deltas` (`ffdelta-v1` binary patches keyed by
`(from_sha256, to_sha256)`).

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"), so `downgrade()` reverses `upgrade()` cleanly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "gateway_relays",
        sa.Column("gateway_id", sa.Text(), nullable=False),
        sa.Column("relay_base_url", sa.Text(), nullable=False),
        sa.Column(
            "cached_artifacts",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["gateway_id"],
            ["devices.device_id"],
            name=op.f("fk_gateway_relays_gateway_id_devices"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("gateway_id", name=op.f("pk_gateway_relays")),
    )

    op.create_table(
        "artifact_deltas",
        sa.Column("from_sha256", sa.Text(), nullable=False),
        sa.Column("to_sha256", sa.Text(), nullable=False),
        sa.Column("patch_sha256", sa.Text(), nullable=False),
        sa.Column("patch_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("target_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "format",
            sa.Text(),
            server_default=sa.text("'ffdelta-v1'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["from_sha256"],
            ["artifacts.sha256"],
            name=op.f("fk_artifact_deltas_from_sha256_artifacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_sha256"],
            ["artifacts.sha256"],
            name=op.f("fk_artifact_deltas_to_sha256_artifacts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("from_sha256", "to_sha256", name=op.f("pk_artifact_deltas")),
    )
    op.create_index(
        op.f("ix_artifact_deltas_to_sha256"),
        "artifact_deltas",
        ["to_sha256"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_index(op.f("ix_artifact_deltas_to_sha256"), table_name="artifact_deltas")
    op.drop_table("artifact_deltas")
    op.drop_table("gateway_relays")
