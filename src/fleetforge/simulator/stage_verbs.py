"""R5 verb dispatch (`apply`, `cancel`, `rollback`, `reboot`), safe-window release, and `on_boot` confirmation for `StageRunner`.

Kept server-independent (`tests/test_simulator.py` AST purity guard).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import aiomqtt

from fleetforge.simulator.board_state import (
    ACK_ACCEPTED,
    ACK_CANCEL_FIRST,
    ACK_CMD_ID_MISSING,
    ACK_DEFERRED,
    ACK_NO_IMAGE,
    ACK_NOT_CONFIRMED,
    ACK_NOTHING_STAGED,
    ACK_NOTHING_TO_CANCEL,
    ACK_REFUSED,
    ACK_UPDATE_IN_PROGRESS,
    COMMAND_APPLY,
    COMMAND_CANCEL,
    COMMAND_REBOOT,
    COMMAND_ROLLBACK,
    CONFIRM_AUTO,
    DETAIL_APPLY_WAITS,
    DETAIL_ROLLBACK_WAITS,
    RESET_REASON_SW,
    RETURNED_TO,
    ROLLED_BACK_BY_COMMAND,
    SELFTEST_FAIL_REASON,
    SELFTEST_FAILED,
    SELFTEST_PASSED,
    SELFTEST_TIMED_OUT,
    STATE_AWAITING_SAFE_WINDOW,
    STATE_CANCELLED,
    STATE_CONFIRMED,
    STATE_CONFIRMING,
    STATE_ROLLED_BACK,
    STATE_ROLLING_BACK,
    PendingConfirm,
    Step,
)

if TYPE_CHECKING:
    from fleetforge.simulator.stage_runner import StageRunner


async def run_verb(
    runner: StageRunner,
    client: aiomqtt.Client,
    command_id: str,
    kind: str,
    body: dict[str, object],
    step: Step,
) -> None:
    if kind in (COMMAND_APPLY, COMMAND_CANCEL):
        cmd_id = body.get("cmd_id")
        if not isinstance(cmd_id, str) or not cmd_id:
            await runner._ack(client, command_id, kind, ACK_REFUSED, None, ACK_CMD_ID_MISSING, step)
            return
        if kind == COMMAND_APPLY:
            await run_apply(runner, client, command_id, cmd_id, step)
        else:
            await run_cancel(runner, client, command_id, cmd_id, step)
    elif kind == COMMAND_ROLLBACK:
        await run_rollback_command(runner, client, command_id, step)
    else:
        await run_reboot_command(runner, client, command_id, step)


async def run_apply(
    runner: StageRunner, client: aiomqtt.Client, command_id: str, cmd_id: str, step: Step
) -> None:
    if runner.staged == cmd_id:
        if runner.apply_deferred == cmd_id:
            await runner._ack(client, command_id, COMMAND_APPLY, ACK_ACCEPTED, cmd_id, None, step)
            return
        if runner._unsafe_now():
            await runner._ack(client, command_id, COMMAND_APPLY, ACK_DEFERRED, cmd_id, None, step)
            await runner._status(
                client,
                cmd_id,
                STATE_AWAITING_SAFE_WINDOW,
                step,
                pct=100,
                detail=DETAIL_APPLY_WAITS,
            )
            runner.apply_deferred = cmd_id
            step(f"apply    {runner._waits('release')}")
            return
        await runner._ack(client, command_id, COMMAND_APPLY, ACK_ACCEPTED, cmd_id, None, step)
        version = runner.staged_version
        runner._clear_staged()
        runner.last_reboot_cmd_id = command_id
        await runner._release(client, cmd_id, version, step, note_stage_reboot=False)
        return
    if runner.pending is not None and runner.pending.cmd_id == cmd_id:
        await runner._ack(client, command_id, COMMAND_APPLY, ACK_ACCEPTED, cmd_id, None, step)
        return
    await runner._ack(
        client, command_id, COMMAND_APPLY, ACK_REFUSED, None, ACK_NOTHING_STAGED, step
    )


async def run_cancel(
    runner: StageRunner, client: aiomqtt.Client, command_id: str, cmd_id: str, step: Step
) -> None:
    if runner.staged != cmd_id:
        await runner._ack(
            client, command_id, COMMAND_CANCEL, ACK_REFUSED, None, ACK_NOTHING_TO_CANCEL, step
        )
        return
    await runner._ack(client, command_id, COMMAND_CANCEL, ACK_ACCEPTED, cmd_id, None, step)
    await runner._status(client, cmd_id, STATE_CANCELLED, step)
    runner._clear_staged()
    step(f"cancel   {cmd_id} cancelled — the staged image will not boot")


async def run_rollback_command(
    runner: StageRunner, client: aiomqtt.Client, command_id: str, step: Step
) -> None:
    if runner.staged is not None:
        await runner._ack(
            client,
            command_id,
            COMMAND_ROLLBACK,
            ACK_REFUSED,
            runner.staged,
            ACK_CANCEL_FIRST,
            step,
        )
        return
    if runner.rollback_deferred is not None:
        await runner._ack(
            client,
            command_id,
            COMMAND_ROLLBACK,
            ACK_ACCEPTED,
            runner.rollback_deferred,
            None,
            step,
        )
        return
    pending = runner.pending
    if pending is not None and not runner.reboot.is_set():
        if await run_deferred_rollback(runner, client, command_id, pending.cmd_id, step):
            return
        await runner._ack(
            client, command_id, COMMAND_ROLLBACK, ACK_ACCEPTED, pending.cmd_id, None, step
        )
        runner.last_reboot_cmd_id = command_id
        await run_carry_out_rollback(runner, client, pending.cmd_id, step)
        return
    if runner.returnable is None:
        await runner._ack(
            client, command_id, COMMAND_ROLLBACK, ACK_REFUSED, None, ACK_NO_IMAGE, step
        )
        return
    if await run_deferred_rollback(runner, client, command_id, command_id, step):
        return
    await runner._ack(client, command_id, COMMAND_ROLLBACK, ACK_ACCEPTED, command_id, None, step)
    runner.last_reboot_cmd_id = command_id
    await run_carry_out_rollback(runner, client, command_id, step)


async def run_carry_out_rollback(
    runner: StageRunner, client: aiomqtt.Client, transaction: str, step: Step
) -> None:
    pending = runner.pending
    if pending is not None and pending.cmd_id == transaction and not runner.reboot.is_set():
        await run_rollback(runner, client, pending, ROLLED_BACK_BY_COMMAND, step)
        return
    if runner.returnable is None:
        step(f"rollback {transaction}: nothing to return to any more — not carried out")
        return
    try:
        await runner._status(client, transaction, STATE_ROLLING_BACK, step)
    except aiomqtt.MqttError as exc:
        step(f"rollback {STATE_ROLLING_BACK} not delivered ({exc}); rolling back anyway")
    identity, slot = runner.returnable
    runner.identity = replace(identity, reset_reason=RESET_REASON_SW)
    runner.slot = slot
    runner.rolled_back = transaction
    runner.rolled_back_head = ROLLED_BACK_BY_COMMAND
    runner.returnable = None
    runner._confirm_deadline = None
    step(f"rollback returning to {identity.fw_version} in {slot}")
    runner.reboot.set()


async def run_deferred_rollback(
    runner: StageRunner, client: aiomqtt.Client, command_id: str, transaction: str, step: Step
) -> bool:
    if not runner._unsafe_now():
        return False
    await runner._ack(client, command_id, COMMAND_ROLLBACK, ACK_DEFERRED, transaction, None, step)
    await runner._status(
        client, transaction, STATE_AWAITING_SAFE_WINDOW, step, detail=DETAIL_ROLLBACK_WAITS
    )
    runner.rollback_deferred = transaction
    step(f"rollback {runner._waits('rollback')}")
    return True


async def run_reboot_command(
    runner: StageRunner, client: aiomqtt.Client, command_id: str, step: Step
) -> None:
    if runner.staged is not None:
        await runner._ack(
            client,
            command_id,
            COMMAND_REBOOT,
            ACK_REFUSED,
            runner.staged,
            ACK_UPDATE_IN_PROGRESS,
            step,
        )
        return
    if runner.pending is not None and not runner.reboot.is_set():
        await runner._ack(
            client,
            command_id,
            COMMAND_REBOOT,
            ACK_REFUSED,
            runner.pending.cmd_id,
            ACK_NOT_CONFIRMED,
            step,
        )
        return
    if runner.reboot_deferred:
        await runner._ack(client, command_id, COMMAND_REBOOT, ACK_ACCEPTED, None, None, step)
        return
    if runner._unsafe_now():
        await runner._ack(client, command_id, COMMAND_REBOOT, ACK_DEFERRED, None, None, step)
        runner.reboot_deferred = True
        step(f"reboot   {runner._waits('reboot')}")
        return
    await runner._ack(client, command_id, COMMAND_REBOOT, ACK_ACCEPTED, None, None, step)
    runner.last_reboot_cmd_id = command_id
    runner.identity = replace(runner.identity, reset_reason=RESET_REASON_SW)
    step(f"reboot   restarting on {runner.identity.fw_version} for command {command_id}")
    runner.reboot.set()


async def run_release_if_safe(runner: StageRunner, client: aiomqtt.Client, step: Step) -> None:
    if runner.reboot.is_set():
        return
    if runner.apply_deferred is None and runner.rollback_deferred is None:
        if not runner.reboot_deferred:
            return
    if runner._unsafe_now():
        return
    if runner.apply_deferred is not None:
        cmd_id = runner.apply_deferred
        version = runner.staged_version
        runner._clear_staged()
        step(f"window   the safe window opened: releasing the staged update {cmd_id}")
        await runner._release(client, cmd_id, version, step)
        return
    if runner.rollback_deferred is not None:
        transaction = runner.rollback_deferred
        runner.rollback_deferred = None
        step(f"window   the safe window opened: carrying out the rollback {transaction}")
        await run_carry_out_rollback(runner, client, transaction, step)
        return
    runner.reboot_deferred = False
    runner.identity = replace(runner.identity, reset_reason=RESET_REASON_SW)
    step(f"window   the safe window opened: restarting on {runner.identity.fw_version}")
    runner.reboot.set()


async def run_on_boot(
    runner: StageRunner, client: aiomqtt.Client, step: Step
) -> asyncio.Task[None] | None:
    if runner.rolled_back is not None:
        head = runner.rolled_back_head
        await runner._status(
            client,
            runner.rolled_back,
            STATE_ROLLED_BACK,
            step,
            detail=(
                f"{head}{RETURNED_TO}{runner.slot}"
                if head is not None
                else f"returned to {runner.identity.fw_version}; the new image did not confirm"
            ),
        )
        runner.rolled_back = None
        runner.rolled_back_head = None
        return None

    pending = runner.pending
    if pending is None:
        return None
    await runner._status(client, pending.cmd_id, STATE_CONFIRMING, step)
    broken = runner._is_broken(runner.identity.fw_version)
    registered = runner._selftest_registered
    if runner.confirm == CONFIRM_AUTO and not broken:
        if registered and runner._fails_selftest(runner.identity.fw_version):
            step(
                f"selftest {runner.identity.fw_version} failed its self-test "
                f"({SELFTEST_FAIL_REASON}) — rolling back now"
            )
            await run_rollback(
                runner, client, pending, SELFTEST_FAILED + SELFTEST_FAIL_REASON, step
            )
            return None
        await runner._status(
            client,
            pending.cmd_id,
            STATE_CONFIRMED,
            step,
            pct=100,
            detail=SELFTEST_PASSED if registered else None,
        )
        step(f"confirm  {runner.identity.fw_version} confirmed at the announce ack")
        runner.returnable = (pending.previous_identity, pending.previous_slot)
        runner.pending = None
        runner._confirm_deadline = None
        return None

    if runner._confirm_deadline is None:
        runner._confirm_deadline = runner.clock() + runner.confirm_timeout_s
    remaining = max(0.0, runner._confirm_deadline - runner.clock())
    why = (
        f"{runner.identity.fw_version} carries the broken marker {runner.broken_marker!r}"
        if broken
        else "--confirm never"
    )
    step(f"confirm  {why}: rolling back in {remaining:.1f}s unless confirmed")
    return asyncio.create_task(run_confirm_timeout(runner, client, pending, remaining, step))


async def run_confirm_timeout(
    runner: StageRunner, client: aiomqtt.Client, pending: PendingConfirm, delay_s: float, step: Step
) -> None:
    await asyncio.sleep(delay_s)
    step(
        f"confirm  no confirm {runner.confirm_timeout_s:g}s after boot — rolling back to "
        f"{pending.previous_identity.fw_version}"
    )
    await run_rollback(
        runner, client, pending, SELFTEST_TIMED_OUT if runner._selftest_registered else None, step
    )


async def run_rollback(
    runner: StageRunner,
    client: aiomqtt.Client,
    pending: PendingConfirm,
    head: str | None,
    step: Step,
) -> None:
    try:
        await runner._status(client, pending.cmd_id, STATE_ROLLING_BACK, step)
    except aiomqtt.MqttError as exc:
        step(f"confirm  {STATE_ROLLING_BACK} not delivered ({exc}); rolling back anyway")
    runner.identity = replace(pending.previous_identity, reset_reason=RESET_REASON_SW)
    runner.slot = pending.previous_slot
    runner.rolled_back = pending.cmd_id
    runner.rolled_back_head = head
    runner.pending = None
    runner.returnable = None
    runner._confirm_deadline = None
    runner.reboot.set()
