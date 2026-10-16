"""`device_commands` — the single writer, and the reads beside it. R5-be-2.

The server sends four verbs besides `stage` (R5-spec-1 Patch A2): `apply` and `cancel`,
which name a transaction (`cmd_id`), and `rollback` and `reboot`, which do not. Each verb
the server publishes is one row here, and the board's **first** answer on `up/ack`
(Patch A5) lands on that row. `api/routers/commands.py` writes the row; the ingestor
records the answer; `GET /v1/devices` reads the newest one per board.

**Everything that writes `device_commands` goes through this module**
(`tests/test_invariants.py` holds the tripwire), the `deploys.py` posture. SQL about
`deploy_events` still lives only in `deploys.py`: **a verb never writes a deploy row** —
not `requested`, not `failed`. What a verb does to a transaction arrives on `up/status`
under the transaction's `cmd_id`, and `deploys.record_observed_status` records it like
any other state.

Transport-agnostic like `deploys.py`: no FastAPI import and no settings object.

## The rules this module owns

**1. Record, commit, publish.** `record_sent` runs before the publish and the caller
commits before it publishes, so the row exists when the answer arrives (the firmware
enqueues the ack before any status). A refused publish is `mark_publish_failed`: the board
provably never saw it, so the row never takes an answer and is nobody's `last_command`.

**2. First answer wins.** `record_answer` updates only a row that has no answer yet, so a
QoS-1 redelivery or a second answer changes nothing.

**3. A device can never create a row, nor answer another board's command.** The update
matches on `(id, device_id)`; an ack for an id the server never sent (a hand-built
command, `identify`) is logged with the id's length and dropped.

**4. The server never waits.** No timer, no retry, no sweep, no expiry: an answer may
never come (an agent that predates R5 ignores a verb silently; an ack queued while
disconnected dies with a reboot, R5-fw-4 gap (e)), and NULL `result` says exactly that.

`detail` is device-controlled text: stored through `deploys.sanitise_device_text` and
never written into this process's log.
"""

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.db.models import AckResult, DeviceCommand
from fleetforge.deploys import DeploySender, SenderSnapshot, sanitise_device_text

logger = logging.getLogger(__name__)

# `up/ack.result` is an open vocabulary (`AckResult` is advisory): a printable string of at
# most this many characters is stored as sent; anything else is not an answer.
ACK_RESULT_MAX_CHARS = 16
# The firmware refuses command ids of 64 bytes or more (`command id too long`), so no id it
# echoes is longer; a longer `cmd_id` on an ack is stored as NULL.
ACK_ID_MAX_CHARS = 64

_KNOWN_RESULTS = frozenset(r.value for r in AckResult)

# Rule 2 and rule 3 in one statement: the board that sent the ack, the id the server sent,
# no answer yet, and a publish that went out. `RETURNING` tells a recorded answer from a
# duplicate, a stranger's id or a type mismatch without a second round trip.
_RECORD_ANSWER_SQL = text(
    """
    UPDATE device_commands
       SET answered_at = :at,
           result = :result,
           answer_cmd_id = :answer_cmd_id,
           answer_detail = :answer_detail
     WHERE id = :id
       AND device_id = :device_id
       AND type = :type
       AND answered_at IS NULL
       AND NOT publish_failed
    RETURNING id
    """
)

# Why an answer recorded nothing, for the log line: which of the conditions above failed.
_WHY_NOT_RECORDED_SQL = text(
    """
    SELECT type, answered_at IS NOT NULL AS answered, publish_failed
      FROM device_commands
     WHERE id = :id AND device_id = :device_id
    """
)

_MARK_PUBLISH_FAILED_SQL = text(
    """
    UPDATE device_commands SET publish_failed = true WHERE id = :id
    """
)

# The newest published command per board, bounded by the ids the caller already holds
# (the `deploys.latest_deploys` rule). `id DESC` breaks a same-microsecond tie the same way
# every time. A publish-failed row is skipped: the board never saw it.
_LATEST_COMMANDS_SQL = text(
    """
    SELECT DISTINCT ON (device_id)
           id, device_id, type, cmd_id, sent_at, sent_by, publish_failed,
           answered_at, result, answer_cmd_id, answer_detail
      FROM device_commands
     WHERE device_id = ANY(:device_ids)
       AND NOT publish_failed
     ORDER BY device_id, sent_at DESC, id DESC
    """
)

_GET_COMMAND_SQL = text(
    """
    SELECT id, device_id, type, cmd_id, sent_at, sent_by, publish_failed,
           answered_at, result, answer_cmd_id, answer_detail
      FROM device_commands
     WHERE id = :id AND device_id = :device_id
    """
)


@dataclass(frozen=True, slots=True)
class CommandSnapshot:
    """One command as the API may show it: what was sent, by whom, and the first answer.

    `sent_by` drops the token id (stored for audit, never surfaced: `SenderSnapshot`).
    `result` None means no answer yet, and possibly never. `detail` is the stored,
    sanitised answer text. `answer_cmd_id` is the transaction the board says it acted on
    (or, on a refusal, the one that caused it), which for a `rollback` of a confirmed image
    is the command's own id.
    """

    command_id: str
    device_id: str
    type: str
    cmd_id: str | None
    sent_at: dt.datetime
    sent_by: SenderSnapshot | None
    publish_failed: bool
    answered_at: dt.datetime | None
    result: str | None
    answer_cmd_id: str | None
    detail: str | None


def _sender_snapshot(value: Any) -> SenderSnapshot | None:
    """`sent_by` out of the JSONB blob, defensively (the `deploys._snapshot_sender` rule)."""
    if not isinstance(value, dict):
        return None
    subject = value.get("subject")
    if not isinstance(subject, str) or not subject:
        return None
    credential = value.get("credential")
    return SenderSnapshot(subject, credential if isinstance(credential, str) else None)


def _snapshot(row: Any) -> CommandSnapshot:
    return CommandSnapshot(
        command_id=row.id,
        device_id=row.device_id,
        type=row.type,
        cmd_id=row.cmd_id,
        sent_at=row.sent_at,
        sent_by=_sender_snapshot(row.sent_by),
        publish_failed=row.publish_failed,
        answered_at=row.answered_at,
        result=row.result,
        answer_cmd_id=row.answer_cmd_id,
        detail=row.answer_detail,
    )


async def record_sent(
    session: AsyncSession,
    *,
    command_id: str,
    device_id: str,
    type: str,
    cmd_id: str | None,
    sent_by: DeploySender,
) -> DeviceCommand:
    """Record a verb about to be published (rule 1). Flushes; the caller commits, then
    publishes. `sent_by` is the R2b-be-4 snapshot, token id included (audit only)."""
    row = DeviceCommand(
        id=command_id,
        device_id=device_id,
        type=type,
        cmd_id=cmd_id,
        sent_by={
            "subject": sent_by.subject,
            "token_id": sent_by.token_id,
            "credential": sent_by.credential,
        },
    )
    session.add(row)
    await session.flush()
    return row


async def mark_publish_failed(session: AsyncSession, *, command_id: str) -> None:
    """The broker refused the publish: the board never saw this command (rule 1). The
    caller commits and answers 503. No deploy row, ever."""
    await session.execute(_MARK_PUBLISH_FAILED_SQL, {"id": command_id})
    logger.warning("command %s recorded as not published", command_id)


def _usable_result(result: str | None) -> str | None:
    """A printable string of 1-16 characters, as sent; anything else is not an answer."""
    if (
        isinstance(result, str)
        and 1 <= len(result) <= ACK_RESULT_MAX_CHARS
        and result.isprintable()
        and not result.isspace()
    ):
        return result
    return None


def _logged_result(result: str | None) -> str:
    """A result fit for a log line: one of the three known values, else `<other>`."""
    return result if result in _KNOWN_RESULTS else "<other>"


async def record_answer(
    session: AsyncSession,
    *,
    device_id: str,
    command_id: str | None,
    type: str | None,
    result: str | None,
    cmd_id: str | None,
    detail: str | None,
    at: dt.datetime,
) -> CommandSnapshot | None:
    """Keep the board's first answer to a command the server sent it; None if nothing
    was recorded (rules 2 and 3).

    Nothing is recorded, and nothing is created, for: an ack with no usable id or result;
    an id the server never sent this board (INFO, the id's length only); a type that
    disagrees with the stored one (WARNING); a command already answered (DEBUG, the QoS-1
    duplicate); a command whose publish failed. `at` is the server's receipt time. The
    `detail` is stored sanitised and is never logged.
    """
    usable = _usable_result(result)
    if not isinstance(command_id, str) or not command_id or len(command_id) > ACK_ID_MAX_CHARS:
        logger.info(
            "device %s answered (%s) a command with no usable id; ignored",
            device_id,
            _logged_result(result),
        )
        return None
    if usable is None:
        logger.info(
            "device %s answered a command (id of %d chars) with no usable result; ignored",
            device_id,
            len(command_id),
        )
        return None
    answer_cmd_id = (
        cmd_id if isinstance(cmd_id, str) and 0 < len(cmd_id) <= ACK_ID_MAX_CHARS else None
    )
    answer_detail = sanitise_device_text(detail) if isinstance(detail, str) else None
    recorded = (
        await session.execute(
            _RECORD_ANSWER_SQL,
            {
                "id": command_id,
                "device_id": device_id,
                "type": type if isinstance(type, str) else "",
                "at": at,
                "result": usable,
                "answer_cmd_id": answer_cmd_id,
                "answer_detail": answer_detail or None,
            },
        )
    ).first()
    if recorded is not None:
        logger.info("device %s answered command %s: %s", device_id, command_id, usable)
        return await get_command(session, device_id=device_id, command_id=command_id)

    why = (
        await session.execute(_WHY_NOT_RECORDED_SQL, {"id": command_id, "device_id": device_id})
    ).first()
    if why is None:
        # Never echo the id itself: it is board text the server did not mint.
        logger.info(
            "device %s answered (%s) a command id this server did not send it (%d chars); "
            "no row created",
            device_id,
            _logged_result(usable),
            len(command_id),
        )
    elif why.type != type:
        # The id matched a row, so it is server-minted; the ack's type is board text.
        logger.warning(
            "device %s answered command %s with another type than the %s it was sent as; "
            "not recorded",
            device_id,
            command_id,
            why.type,
        )
    elif why.publish_failed:
        logger.warning(
            "device %s answered command %s, which was never published; not recorded",
            device_id,
            command_id,
        )
    else:
        logger.debug(
            "device %s answered command %s again; the first answer stands", device_id, command_id
        )
    return None


async def get_command(
    session: AsyncSession, *, device_id: str, command_id: str
) -> CommandSnapshot | None:
    """One command of `device_id`, publish-failed included; None for an unknown id or
    another board's."""
    row = (
        await session.execute(_GET_COMMAND_SQL, {"id": command_id, "device_id": device_id})
    ).first()
    return _snapshot(row) if row is not None else None


async def latest_commands(
    session: AsyncSession, *, device_ids: Sequence[str]
) -> dict[str, CommandSnapshot]:
    """The newest published command of each of `device_ids`, keyed by device. A board
    never commanded (or whose only commands failed to publish) is absent."""
    rows = (await session.execute(_LATEST_COMMANDS_SQL, {"device_ids": list(device_ids)})).all()
    return {row.device_id: _snapshot(row) for row in rows}
