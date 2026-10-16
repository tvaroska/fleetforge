"""The four verbs — `apply`, `cancel`, `rollback`, `reboot` — and one command read. R5-be-2.

R5-spec-1 Patch A2 is the wire; R5-fw-4/5 built the agent half. Admin-only, like
`routers/deploys.py`:

* `POST /v1/devices/{device_id}/deploys/{cmd_id}/apply` — release a transaction that a
  `apply: "on_command"` stage left at `staged`;
* `POST /v1/devices/{device_id}/deploys/{cmd_id}/cancel` — end a transaction before its
  reboot (it ends `cancelled`, reported by the device);
* `POST /v1/devices/{device_id}/rollback` — return the board to its other slot;
* `POST /v1/devices/{device_id}/reboot` — restart the board on the image it runs;
* `GET  /v1/devices/{device_id}/commands/{command_id}` — one command and its first answer.

No request body: the transaction is in the path and the command's `id` is server-minted.
202 with `CommandAccepted`. A separate module rather than more routes in `deploys.py` /
`devices.py` (DECISIONS R5-be-2): those two carry source tripwires scoped to the deploy path,
and the verb seam is its own thing.

**The server requests; it never forces.** It gates only what it alone knows, in this order:

1. the device is missing or decommissioned → 404 (the deploy route's sentence);
2. the board does not announce the verb in `capabilities` → 409 (R5-spec-1 D9: an agent
   that predates R5 ignores a verb silently, so sending it would be a lie of omission);
3. `apply` / `cancel` only: `cmd_id` is not a plausible id → 400; it names no transaction
   the server recorded for THIS board (`deploys.deploy_transaction`) → 404; that
   transaction already ended → 409 naming its terminal state;
4. `apply` only: the transaction was sent with `apply: auto` → 409.

Everything about the board's live state ("is it staged yet", "is an update in flight", "is
the image confirmed") is the board's call, because the server's view can lag: the board
answers on `up/ack` with its own reason (`nothing staged for this cmd_id`, `cancel first`,
`an update is in progress`, …).

**Write order: record, commit, publish** (the deploy posture): the `device_commands` row is
committed before the publish, so it exists when the answer arrives. A refused publish sets
`publish_failed` and answers 503. **A verb never writes a `deploy_events` row** — not
`requested`, not `failed`: the server authors no state for a verb, and a rollback's own
transaction is recorded only from the board's own `up/status` (and stays out of the deploy
KPIs, `deploys.counts_in_deploy_kpis`).

**No timers.** No retry, no sweep, no wait for the answer, no expiry: an answer may never
come (an old agent, an ack lost across a reboot), and `result: null` says so honestly.
`tests/test_api_deploy.py` extends its no-timer tripwire to this module.
"""

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.api.deps import (
    AdminDep,
    AuthContext,
    CommandPublisherDep,
    SessionMakerDep,
    SettingsDep,
    bearer_scheme,
    cookie_scheme,
)
from fleetforge.api.routers.deploys import _sender
from fleetforge.api.routers.devices import command_summary
from fleetforge.api.schemas import CommandAccepted, CommandRecord
from fleetforge.broker import CommandPublisher, CommandPublishError, new_command_id, verb_payload
from fleetforge.clock import now_utc
from fleetforge.config import Settings
from fleetforge.db.models import CommandType, Device
from fleetforge.deploys import deploy_transaction
from fleetforge.device_commands import get_command, mark_publish_failed, record_sent
from fleetforge.presence import is_online

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/devices",
    tags=["commands"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)

# The shape of a transaction id the server could have minted, or a board could echo:
# the firmware refuses ids of 64 bytes or more (R5-fw-4 `command id too long`).
CMD_ID_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{1,63}\Z")

PUBLISH_FAILED = "the broker could not accept the command; try again shortly"


async def _live_device(session: AsyncSession, device_id: str) -> Device:
    """The device, or the deploy route's 404 for an unknown or decommissioned one."""
    device = await session.get(Device, device_id)
    if device is None or device.decommissioned_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no such device: {device_id}"
        )
    return device


def _require_capability(device: Device, verb: str) -> None:
    """Gate 2: a board that does not announce the verb would ignore it (D9)."""
    if verb in device.capabilities:
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"this board's agent does not implement {verb} (it announces: "
            f"{', '.join(device.capabilities) or 'nothing'}), so it would ignore the command. "
            "Update the agent first."
        ),
    )


async def _require_transaction(
    session: AsyncSession, device_id: str, cmd_id: str, verb: str
) -> None:
    """Gates 3 and 4: an open transaction the server recorded for this board, and for
    `apply` one that was sent `on_command`."""
    if not CMD_ID_PATTERN.match(cmd_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cmd_id must be 1-63 characters of letters, digits, '_' or '-'",
        )
    transaction = await deploy_transaction(session, device_id=device_id, cmd_id=cmd_id)
    if transaction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no deploy {cmd_id} on this board"
        )
    if transaction.ended_state is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"deploy {cmd_id} already ended ({transaction.ended_state}); "
                f"there is nothing to {verb}"
            ),
        )
    if verb == CommandType.APPLY and transaction.apply != "on_command":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"deploy {cmd_id} was sent with apply: {transaction.apply or 'auto'}; "
                "the board applies it by itself"
            ),
        )


async def _send(
    *,
    verb: CommandType,
    device_id: str,
    cmd_id: str | None,
    admin: AuthContext,
    settings: Settings,
    session: AsyncSession,
    publisher: CommandPublisher,
) -> CommandAccepted:
    """The four POSTs in one place: gate, record + commit, publish, answer 202."""
    device = await _live_device(session, device_id)
    _require_capability(device, verb.value)
    if cmd_id is not None:
        await _require_transaction(session, device_id, cmd_id, verb.value)

    command_id = new_command_id()
    payload = verb_payload(command_id=command_id, type=verb.value, cmd_id=cmd_id)
    await record_sent(
        session,
        command_id=command_id,
        device_id=device_id,
        type=verb.value,
        cmd_id=cmd_id,
        sent_by=await _sender(session, admin),
    )
    # Committed BEFORE the publish: record, then act. The answer finds the row.
    await session.commit()

    try:
        await publisher.publish(device_id, payload)
    except CommandPublishError:
        logger.exception("%s command %s to %s could not be published", verb, command_id, device_id)
        await mark_publish_failed(session, command_id=command_id)
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=PUBLISH_FAILED
        ) from None

    online = is_online(device, now=now_utc(), tolerance=settings.presence_tolerance)
    logger.info(
        "%s command %s to %s (cmd_id=%s) requested by %s",
        verb,
        command_id,
        device_id,
        cmd_id,
        admin.token_id,
    )
    return CommandAccepted(
        command_id=command_id,
        device_id=device_id,
        type=verb.value,
        cmd_id=cmd_id,
        # Reported, never blocking: the broker queues the QoS-1 command in the board's
        # persistent session.
        device_online=online,
    )


@router.post(
    "/{device_id}/deploys/{cmd_id}/apply",
    response_model=CommandAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Release a deploy that waits at staged under apply: on_command",
)
async def apply_deploy(
    device_id: str,
    cmd_id: str,
    admin: AdminDep,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
    publisher: CommandPublisherDep,
) -> CommandAccepted:
    """Publish one `apply` naming `cmd_id`. The board answers on `up/ack` and, if it acts,
    walks `applying → rebooting → confirming → …` under `cmd_id` (or waits in
    `awaiting_safe_window`)."""
    async with sessionmaker() as session:
        return await _send(
            verb=CommandType.APPLY,
            device_id=device_id,
            cmd_id=cmd_id,
            admin=admin,
            settings=settings,
            session=session,
            publisher=publisher,
        )


@router.post(
    "/{device_id}/deploys/{cmd_id}/cancel",
    response_model=CommandAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Cancel a deploy before its reboot",
)
async def cancel_deploy(
    device_id: str,
    cmd_id: str,
    admin: AdminDep,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
    publisher: CommandPublisherDep,
) -> CommandAccepted:
    """Publish one `cancel` naming `cmd_id`. If the board carries it out, the transaction
    ends `cancelled` (terminal, outside both deploy KPIs); from `applying` on it refuses."""
    async with sessionmaker() as session:
        return await _send(
            verb=CommandType.CANCEL,
            device_id=device_id,
            cmd_id=cmd_id,
            admin=admin,
            settings=settings,
            session=session,
            publisher=publisher,
        )


@router.post(
    "/{device_id}/rollback",
    response_model=CommandAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Return a device to the image in its other slot",
)
async def rollback_device(
    device_id: str,
    admin: AdminDep,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
    publisher: CommandPublisherDep,
) -> CommandAccepted:
    """Publish one `rollback`. While a stage is `confirming` it ends that transaction;
    otherwise the rollback is its own transaction under `command_id` (out of the KPIs).
    The board refuses with nothing to return to, or while an update is in hand."""
    async with sessionmaker() as session:
        return await _send(
            verb=CommandType.ROLLBACK,
            device_id=device_id,
            cmd_id=None,
            admin=admin,
            settings=settings,
            session=session,
            publisher=publisher,
        )


@router.post(
    "/{device_id}/reboot",
    response_model=CommandAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Restart a device on the image it runs",
)
async def reboot_device(
    device_id: str,
    admin: AdminDep,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
    publisher: CommandPublisherDep,
) -> CommandAccepted:
    """Publish one `reboot`. No transaction; the next announce's `reset_reason: sw` is the
    evidence. The board refuses while an update is in progress or unconfirmed."""
    async with sessionmaker() as session:
        return await _send(
            verb=CommandType.REBOOT,
            device_id=device_id,
            cmd_id=None,
            admin=admin,
            settings=settings,
            session=session,
            publisher=publisher,
        )


@router.get(
    "/{device_id}/commands/{command_id}",
    response_model=CommandRecord,
    summary="One command sent to a device, and its first answer",
)
async def get_device_command(
    device_id: str,
    command_id: str,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> CommandRecord:
    """The record and the board's first answer; 404 for an unknown id or another board's.
    `result: null` is "no answer yet", possibly never."""
    async with sessionmaker() as session:
        await _live_device(session, device_id)
        snapshot = await get_command(session, device_id=device_id, command_id=command_id)
    summary = command_summary(snapshot)
    if snapshot is None or summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no command {command_id} on this board",
        )
    return CommandRecord(
        **summary.model_dump(),
        device_id=snapshot.device_id,
        publish_failed=snapshot.publish_failed,
    )
