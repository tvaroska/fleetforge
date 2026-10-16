"""device commands: the verbs the server sent and the board's first answer

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-16

Task R5-be-2 — the server learns to send `apply`, `cancel`, `rollback` and `reboot`
(R5-spec-1 Patch A2) and to keep the board's answer on `up/ack` (Patch A5). One new table,
`device_commands`: one row per verb the server published, written before the publish, plus
the board's FIRST answer (`answered_at`, `result`, `answer_cmd_id`, `answer_detail`, all
NULL until it arrives, and possibly forever: an agent that predates R5 never answers).

A verb is **not** a deploy transaction state, so none of this goes into `deploy_events`
(the KPI table, kept forever). What a verb does to a transaction still arrives on
`up/status` under the transaction's `cmd_id` and is recorded there as before. Rationale for
every column lives on `DeviceCommand` in `src/fleetforge/db/models.py`; `alembic check`
keeps the two in step.

Additive only: a new table and its index, no change to any existing column, no data
migration. The FK is `ON DELETE CASCADE`: these rows are an operational record about a
board, never KPI history (`deploy_events` keeps its `RESTRICT`). No CHECK on `type` or
`result` (convention 1 in `db/models.py`: vocabulary lives in Python).

**This file is a CRITICAL.md path** ("Alembic migrations — irreversible schema/data
changes"), so `downgrade()` must work and is tested
(`tests/test_schema.py::test_migration_downgrades_cleanly`). The reverse is honest: it drops
operational command records only (which verbs were sent, by whom, and the board's answer).
No KPI history, no credential and nothing a board cannot be asked again lives in it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "device_commands",
        # The command's own `id`, server-minted (`broker.new_command_id()`).
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        # The transaction an `apply` / `cancel` names; NULL for `rollback` / `reboot`.
        sa.Column("cmd_id", sa.Text(), nullable=True),
        sa.Column(
            "sent_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_by", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("publish_failed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        # The board's first `up/ack`; NULL until (and unless) it arrives.
        sa.Column("answered_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("answer_cmd_id", sa.Text(), nullable=True),
        sa.Column("answer_detail", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.device_id"],
            name=op.f("fk_device_commands_device_id_devices"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_commands")),
    )
    # The fleet list's `last_command` (newest per board) and a board's history.
    op.create_index(
        "ix_device_commands_device_id_sent_at",
        "device_commands",
        ["device_id", sa.literal_column("sent_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema — exact reverse of upgrade()."""
    op.drop_index("ix_device_commands_device_id_sent_at", table_name="device_commands")
    op.drop_table("device_commands")
