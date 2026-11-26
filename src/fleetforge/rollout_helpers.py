"""Shared DB query, cohort-halt, and MQTT publish helpers for `fleetforge.rollout` (`A-29`, `T-24`)."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Collection
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.broker import CommandPublisher, CommandPublishError
from fleetforge.config import Settings
from fleetforge.db.models import AdminToken, Device
from fleetforge.deploy_precheck import ResolvedArtifact
from fleetforge.deploys import DeploySender, DeploySnapshot, record_publish_failure
from fleetforge.rollout_state import (
    close_matching_rollouts,
    find_open_rollout,
    has_newer_confirmed_or_in_flight,
    open_or_supersede_rollout,
)
from fleetforge.rollout_state import (
    is_cohort_halt_snapshot as _is_cohort_halt_snapshot,
)
from fleetforge.rollout_types import ActorContext, DeployAccepted, RolloutBrokerError
from fleetforge.signing import SigningKeyRing

logger = logging.getLogger(__name__)

StageTask = tuple[Device, ResolvedArtifact, str, bool, bool, dict[str, Any]]

_ARTIFACT_FOR_TARGET_SQL = text(
    """
    SELECT a.sha256, a.size_bytes, a.partition_layout, a.has_lib_marker, a.sig, a.artifact_type,
           a.sim_status, a.sim_detail
      FROM artifact_versions AS v
      JOIN artifacts AS a ON a.sha256 = v.sha256
     WHERE v.project_id = :project_id AND v.target = :target AND v.version = :version
    """
)


async def resolve_artifact_for_target(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    target: str,
    version: str,
    settings: Settings | None = None,
) -> ResolvedArtifact | None:
    """Resolve the artifact behind `(project_id, target, version)`, if any."""
    row = (
        await session.execute(
            _ARTIFACT_FOR_TARGET_SQL,
            {"project_id": project_id, "target": target, "version": version},
        )
    ).first()
    if row is None:
        return None
    uses_dev = False
    if row.sig is not None:
        try:
            ring = SigningKeyRing.from_config(
                private_key=settings.artifact_signing_key if settings else None,
                public_keys=settings.artifact_signing_public_keys if settings else None,
            )
            uses_dev = ring.uses_dev_keys and ring.signed_by_dev_key(row.sha256, row.sig)
        except ValueError:
            uses_dev = False
    return ResolvedArtifact(
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        partition_layout=row.partition_layout,
        has_lib_marker=row.has_lib_marker,
        sig=row.sig,
        artifact_type=getattr(row, "artifact_type", "app"),
        is_signed=(row.sig is not None),
        uses_dev_key_ring=uses_dev,
        sim_status=getattr(row, "sim_status", None),
        sim_detail=getattr(row, "sim_detail", None),
    )


async def sender_for(session: AsyncSession, admin: ActorContext) -> DeploySender:
    """Resolve the audit `DeploySender` label for an operator token."""
    token = await session.get(AdminToken, admin.token_id)
    return DeploySender(
        subject=admin.subject,
        token_id=str(admin.token_id),
        credential=token.name if token is not None else None,
    )


async def load_rollout_devices(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    target_filter: str,
    target_chip: str | None,
    group_id: uuid.UUID | None,
) -> tuple[list[Device], list[Device], list[Device]]:
    """Return `(project_devices, cohort_devices, target_devices)` in one query.

    `project_devices` spans the entire project so radio/gateway airtime caps account for
    in-flight downloads in sibling groups/targets, while `cohort_devices` stays scoped to
    `target_chip` and `group_id` for canary gating and cohort halt isolation.
    """
    stmt = (
        select(Device)
        .where(Device.project_id == project_id, Device.decommissioned_at.is_(None))
        .order_by(Device.device_id)
    )
    project_devices = list((await session.scalars(stmt)).all())
    cohort_devices = [
        d
        for d in project_devices
        if (target_chip is None or d.platform_type == target_chip)
        and (group_id is None or d.group_id == group_id)
    ]
    devices = (
        [d for d in cohort_devices if d.is_dev] if target_filter == "dev" else list(cohort_devices)
    )
    return project_devices, cohort_devices, devices


async def check_cohort_halt(
    session: AsyncSession,
    *,
    all_project_devices: list[Device],
    devices: list[Device],
    snapshots: dict[str, DeploySnapshot],
    project_id: uuid.UUID,
    version: str,
    target_filter: str,
    target_chip: str | None,
    group_id: uuid.UUID | None,
    policy_id: uuid.UUID | None,
    include_cancelled: bool,
    ignore_halts_before: dt.datetime | None,
) -> dict[str, str] | None:
    """Halt the cohort and return per-device skip reasons if any peer tripped a halt state."""
    for d in all_project_devices:
        snap = snapshots.get(d.device_id)
        if (
            snap is not None
            and (ignore_halts_before is None or snap.at > ignore_halts_before)
            and _is_cohort_halt_snapshot(snap, version, include_cancelled=include_cancelled)
        ):
            halt_reason = f"rollout halted: device {d.device_id} {snap.state} on version {version}"
            await close_matching_rollouts(
                session,
                project_id=project_id,
                version=version,
                target_filter=target_filter,
                target_chip=target_chip,
                group_id=group_id,
                policy_id=policy_id,
                status_value="halted",
                reason=halt_reason,
            )
            if isinstance(session, AsyncSession):
                await session.commit()
            return {cand.device_id: halt_reason for cand in devices}
    return None


def skip_reason_for_snapshot(
    device: Device,
    dev_snap: DeploySnapshot | None,
    *,
    version: str,
    republish_in_flight: bool,
    rollout_started_at: dt.datetime | None,
) -> str | None:
    """Return a skip reason if `device` already satisfies, is in-flight on, or outruns `version`."""
    if device.fw_version == version or (
        dev_snap is not None
        and dev_snap.artifact_version == version
        and dev_snap.state == "confirmed"
    ):
        return f"already running version {version}"
    if (
        not republish_in_flight
        and dev_snap is not None
        and dev_snap.artifact_version == version
        and not dev_snap.is_terminal
    ):
        return f"already in flight on version {version}"
    if not republish_in_flight and has_newer_confirmed_or_in_flight(
        dev_snap, version=version, rollout_started_at=rollout_started_at
    ):
        return (
            f"already running newer rollout version {dev_snap.artifact_version}"
            if dev_snap is not None
            else "already running newer rollout version"
        )
    return None


async def resolve_rollout_tracking(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    version: str,
    target_filter: str,
    target_chip: str | None,
    group_id: uuid.UUID | None,
    policy_id: uuid.UUID | None,
    apply: str,
    canary: bool,
    max_parallel: int,
    override: Collection[str],
    sent_by: DeploySender,
    republish_in_flight: bool,
    rollout_started_at: dt.datetime | None,
) -> dt.datetime | None:
    """Open/supersede `ProjectRollout` on operator triggers, or look up its start timestamp."""
    if republish_in_flight:
        await open_or_supersede_rollout(
            session,
            project_id=project_id,
            version=version,
            target_filter=target_filter,
            target_chip=target_chip,
            group_id=group_id,
            policy_id=policy_id,
            apply=apply,
            canary=canary,
            max_parallel=max_parallel,
            override=override,
            sent_by=sent_by,
        )
        return rollout_started_at
    rollout_row = await find_open_rollout(
        session,
        project_id=project_id,
        version=version,
        target_filter=target_filter,
        target_chip=target_chip,
        group_id=group_id,
        policy_id=policy_id,
    )
    if rollout_started_at is None and rollout_row is not None:
        return rollout_row.created_at
    return rollout_started_at


async def close_completed_rollout_if_idle(
    session: AsyncSession,
    *,
    devices: list[Device],
    snapshots: dict[str, DeploySnapshot],
    project_id: uuid.UUID,
    version: str,
    target_filter: str,
    target_chip: str | None,
    group_id: uuid.UUID | None,
    policy_id: uuid.UUID | None,
) -> None:
    """Close the open rollout as `completed` when no eligible or in-flight devices remain."""
    any_in_flight = any(
        (snap := snapshots.get(d.device_id)) is not None
        and snap.artifact_version == version
        and not snap.is_terminal
        for d in devices
    )
    if not any_in_flight:
        await close_matching_rollouts(
            session,
            project_id=project_id,
            version=version,
            target_filter=target_filter,
            target_chip=target_chip,
            group_id=group_id,
            policy_id=policy_id,
            status_value="completed",
            reason=None,
        )
    if isinstance(session, AsyncSession):
        await session.commit()


def accepted_from_task(task: StageTask, *, version: str, apply: str) -> DeployAccepted:
    """Build the API response item for a published wave task."""
    device, found_artifact, cmd_id, reused, online, _ = task
    return DeployAccepted(
        cmd_id=cmd_id,
        device_id=device.device_id,
        version=version,
        sha256=found_artifact.sha256,
        size_bytes=found_artifact.size_bytes,
        apply=apply,
        reused=reused,
        device_online=online,
    )


async def publish_stage_tasks(
    session: AsyncSession,
    *,
    stage_tasks: list[StageTask],
    project_id: uuid.UUID,
    version: str,
    apply: str,
    publisher: CommandPublisher,
    best_effort_broker: bool,
    skipped: dict[str, str],
) -> list[DeployAccepted]:
    """Publish staged MQTT commands in batch with per-device fallback and failure cleanup."""
    if len(stage_tasks) > 1:
        try:
            await publisher.publish_many(
                [(dev.device_id, payload) for dev, _, _, _, _, payload in stage_tasks]
            )
            return [accepted_from_task(t, version=version, apply=apply) for t in stage_tasks]
        except CommandPublishError:
            logger.warning(
                "batch MQTT publish failed for project %s version %s; falling back per device",
                project_id,
                version,
                exc_info=True,
            )

    deployed: list[DeployAccepted] = []
    for idx, task in enumerate(stage_tasks):
        device, _, cmd_id, _, _, payload = task
        try:
            await publisher.publish(device.device_id, payload)
        except CommandPublishError:
            if best_effort_broker:
                logger.exception(
                    "auto-deploy %s for %s could not be published", cmd_id, device.device_id
                )
                await record_publish_failure(
                    session,
                    device_id=device.device_id,
                    cmd_id=cmd_id,
                    artifact_version=version,
                    from_version=device.fw_version,
                )
                await session.commit()
                skipped[device.device_id] = "broker publish failed"
                continue

            logger.exception("deploy %s for %s could not be published", cmd_id, device.device_id)
            for rem_device, _, rem_cmd_id, _, _, _ in stage_tasks[idx:]:
                await record_publish_failure(
                    session,
                    device_id=rem_device.device_id,
                    cmd_id=rem_cmd_id,
                    artifact_version=version,
                    from_version=rem_device.fw_version,
                )
            await session.commit()
            raise RolloutBrokerError(
                "the broker could not accept the deploy command; try again shortly"
            ) from None

        deployed.append(accepted_from_task(task, version=version, apply=apply))
    return deployed
