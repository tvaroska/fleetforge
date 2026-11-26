"""Staged project cohort rollout orchestration (`A-3`, `A-9`, `A-29`, `A-32`).

Transport-agnostic core orchestration: owns wave selection, per-target canary gating,
cohort halt checks, mid-wave publish-failure cleanup, wake republish bounds, and periodic
rollout reconciliation with zero FastAPI or router imports.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Callable, Collection
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.broker import (
    CommandPublisher,
    NullCommandPublisher,
    new_command_id,
    stage_payload,
)
from fleetforge.clock import now_utc
from fleetforge.config import Settings
from fleetforge.db.models import Device, Project
from fleetforge.deploy_precheck import (
    Finding,
    ResolvedArtifact,
    apply_refusals,
    refusals,
    unmet_gates,
)
from fleetforge.deploy_urls import mint_deploy_artifact_url
from fleetforge.deploys import (
    DeploySender,
    DeploySnapshot,
    deploy_transaction,
    latest_deploys,
    latest_open_transaction,
    record_requested,
    requested_rollout,
)
from fleetforge.partition_profiles import LayoutCatalog, load_layout_catalog
from fleetforge.presence import is_online
from fleetforge.rollout_helpers import (
    StageTask as _StageTask,
)
from fleetforge.rollout_helpers import (
    check_cohort_halt as _check_cohort_halt,
)
from fleetforge.rollout_helpers import (
    close_completed_rollout_if_idle as _close_completed_rollout_if_idle,
)
from fleetforge.rollout_helpers import (
    load_rollout_devices as _load_rollout_devices,
)
from fleetforge.rollout_helpers import (
    publish_stage_tasks as _publish_stage_tasks,
)
from fleetforge.rollout_helpers import (
    resolve_artifact_for_target as resolve_artifact_for_target,
)
from fleetforge.rollout_helpers import (
    resolve_rollout_tracking as _resolve_rollout_tracking,
)
from fleetforge.rollout_helpers import (
    sender_for as _sender_for,
)
from fleetforge.rollout_helpers import (
    skip_reason_for_snapshot as _skip_reason_for_snapshot,
)
from fleetforge.rollout_state import (
    DEFAULT_ROLLOUT_MAX_PARALLEL as DEFAULT_ROLLOUT_MAX_PARALLEL,
)
from fleetforge.rollout_state import (
    LINK_AIRTIME_MAX_PARALLEL as LINK_AIRTIME_MAX_PARALLEL,
)
from fleetforge.rollout_state import (
    PARENT_GATEWAY_MAX_PARALLEL as PARENT_GATEWAY_MAX_PARALLEL,
)
from fleetforge.rollout_state import (
    close_matching_rollouts as close_matching_rollouts,
)
from fleetforge.rollout_state import (
    effective_url_ttl_s as effective_url_ttl_s,
)
from fleetforge.rollout_state import (
    is_cohort_halt_snapshot as _is_cohort_halt_snapshot,
)
from fleetforge.rollout_state import (
    matches_project_tracking as matches_project_tracking,
)
from fleetforge.rollout_state import (
    reconcile_open_rollouts as reconcile_open_rollouts,
)
from fleetforge.rollout_state import (
    resolve_rollout_for_confirm,
    select_rollout_wave,
)
from fleetforge.rollout_types import (
    VERSION_PATTERN,
    ActorContext,
    DeployAccepted,
    RolloutValidationError,
)
from fleetforge.swarm_gateway import build_stage_urls_and_delta

logger = logging.getLogger(__name__)


async def _collect_eligible_devices(
    session: AsyncSession,
    *,
    devices: list[Device],
    snapshots: dict[str, DeploySnapshot],
    project_id: uuid.UUID,
    version: str,
    apply: str,
    override: Collection[str],
    catalog: LayoutCatalog,
    now: dt.datetime,
    settings: Settings,
    republish_in_flight: bool,
    rollout_started_at: dt.datetime | None,
) -> tuple[list[tuple[Device, ResolvedArtifact, bool]], dict[str, str]]:
    eligible: list[tuple[Device, ResolvedArtifact, bool]] = []
    skipped: dict[str, str] = {}
    artifacts_by_target: dict[str, ResolvedArtifact | None] = {}

    for device in devices:
        snap_skip = _skip_reason_for_snapshot(
            device,
            snapshots.get(device.device_id),
            version=version,
            republish_in_flight=republish_in_flight,
            rollout_started_at=rollout_started_at,
        )
        if snap_skip is not None:
            skipped[device.device_id] = snap_skip
            continue
        if device.platform_type not in artifacts_by_target:
            artifacts_by_target[device.platform_type] = await resolve_artifact_for_target(
                session,
                project_id=project_id,
                target=device.platform_type,
                version=version,
                settings=settings,
            )
        found_artifact = artifacts_by_target[device.platform_type]
        found_refusals: list[Finding] = [
            *refusals(device, found_artifact, version=version, catalog=catalog),
            *apply_refusals(device, apply),
        ]
        if found_refusals:
            skipped[device.device_id] = found_refusals[0].message
            continue
        gated = unmet_gates(device, override, artifact=found_artifact)
        if gated:
            skipped[device.device_id] = gated[0].message
            continue
        if found_artifact is None:
            skipped[device.device_id] = "no such artifact"
            continue
        online = is_online(device, now=now, tolerance=settings.presence_tolerance)
        eligible.append((device, found_artifact, online))
    return eligible, skipped


async def _prepare_stage_tasks(
    session: AsyncSession,
    *,
    wave_items: list[tuple[Device, ResolvedArtifact, bool]],
    project_id: uuid.UUID,
    version: str,
    apply: str,
    override: Collection[str],
    sent_by: DeploySender,
    rollout_meta: dict[str, Any],
    settings: Settings,
    url_builder: Callable[..., str],
    supports_relay_kwarg: bool,
) -> list[_StageTask]:
    stage_tasks: list[_StageTask] = []
    for device, found_artifact, online in wave_items:
        ttl_s = effective_url_ttl_s(device, settings)
        open_tx = await latest_open_transaction(session, device_id=device.device_id, window_s=ttl_s)
        reused = open_tx is not None and open_tx.sha256 == found_artifact.sha256
        cmd_id = open_tx.cmd_id if reused and open_tx else new_command_id()
        url, delta_payload = await build_stage_urls_and_delta(
            session,
            device=device,
            project_id=project_id,
            sha256=found_artifact.sha256,
            ttl_s=ttl_s,
            settings=settings,
            mint_url=url_builder,
            supports_relay_kwarg=supports_relay_kwarg,
        )
        if not reused:
            await record_requested(
                session,
                device_id=device.device_id,
                cmd_id=cmd_id,
                artifact_version=version,
                from_version=device.fw_version,
                sha256=found_artifact.sha256,
                size_bytes=found_artifact.size_bytes,
                target=device.platform_type,
                apply=apply,
                sent_by=sent_by,
                rollout=rollout_meta,
                override=override,
            )
        payload = stage_payload(
            cmd_id=cmd_id,
            url=url,
            sha256=found_artifact.sha256,
            size=found_artifact.size_bytes,
            version=version,
            apply=apply,
            confirm_timeout_s=settings.confirm_timeout_s,
            sig=found_artifact.sig,
            artifact_type=found_artifact.artifact_type,
            delta=delta_payload,
        )
        stage_tasks.append((device, found_artifact, cmd_id, reused, online, payload))
    return stage_tasks


async def deploy_version_to_project_devices(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    version: str,
    target_filter: str,
    apply: str,
    override: Collection[str],
    admin: ActorContext | None = None,
    settings: Settings,
    publisher: CommandPublisher,
    target_chip: str | None = None,
    group_id: uuid.UUID | None = None,
    best_effort_broker: bool = False,
    canary: bool = True,
    max_parallel: int = DEFAULT_ROLLOUT_MAX_PARALLEL,
    sent_by_override: DeploySender | None = None,
    republish_in_flight: bool = True,
    mint_url: Callable[..., str] | None = None,
    policy_id: uuid.UUID | None = None,
    rollout_started_at: dt.datetime | None = None,
    ignore_halts_before: dt.datetime | None = None,
) -> tuple[list[DeployAccepted], dict[str, str]]:
    """Stage `version` onto compatible live boards in `project_id` with staged rollout safety."""
    if not VERSION_PATTERN.match(version):
        raise RolloutValidationError(
            "version must be 1-64 characters of letters, digits, '.', '_', '+' or "
            "'-', starting with a letter or digit"
        )
    if best_effort_broker and (
        isinstance(publisher, NullCommandPublisher)
        or not settings.artifact_url_secret
        or not settings.public_base_url
    ):
        return [], {}

    await session.execute(select(Project.id).where(Project.id == project_id).with_for_update())
    project_devices, all_project_devices, devices = await _load_rollout_devices(
        session,
        project_id=project_id,
        target_filter=target_filter,
        target_chip=target_chip,
        group_id=group_id,
    )
    if not devices:
        return [], {}

    sent_by = sent_by_override or (
        await _sender_for(session, admin)
        if admin is not None
        else DeploySender("system", "rollout", None)
    )
    rollout_started_at = await _resolve_rollout_tracking(
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
        republish_in_flight=republish_in_flight,
        rollout_started_at=rollout_started_at,
    )

    snapshots = await latest_deploys(session, device_ids=[d.device_id for d in project_devices])
    halted_skipped = await _check_cohort_halt(
        session,
        all_project_devices=all_project_devices,
        devices=devices,
        snapshots=snapshots,
        project_id=project_id,
        version=version,
        target_filter=target_filter,
        target_chip=target_chip,
        group_id=group_id,
        policy_id=policy_id,
        include_cancelled=not republish_in_flight,
        ignore_halts_before=ignore_halts_before,
    )
    if halted_skipped is not None:
        return [], halted_skipped

    confirmed_targets = {
        d.platform_type
        for d in all_project_devices
        if d.fw_version == version
        or (
            (snap := snapshots.get(d.device_id)) is not None
            and snap.artifact_version == version
            and snap.state == "confirmed"
        )
    }
    catalog = await load_layout_catalog(session)
    now = now_utc()
    eligible, skipped = await _collect_eligible_devices(
        session,
        devices=devices,
        snapshots=snapshots,
        project_id=project_id,
        version=version,
        apply=apply,
        override=override,
        catalog=catalog,
        now=now,
        settings=settings,
        republish_in_flight=republish_in_flight,
        rollout_started_at=rollout_started_at,
    )
    if not eligible:
        await _close_completed_rollout_if_idle(
            session,
            devices=devices,
            snapshots=snapshots,
            project_id=project_id,
            version=version,
            target_filter=target_filter,
            target_chip=target_chip,
            group_id=group_id,
            policy_id=policy_id,
        )
        return [], skipped

    wave_items = select_rollout_wave(
        eligible=eligible,
        all_project_devices=all_project_devices,
        snapshots=snapshots,
        confirmed_targets=confirmed_targets,
        version=version,
        canary=canary,
        max_parallel=max_parallel,
        now=now,
        settings=settings,
        skipped=skipped,
        airtime_devices=project_devices,
    )
    if not wave_items:
        if isinstance(session, AsyncSession):
            await session.commit()
        return [], skipped

    rollout_meta: dict[str, Any] = {
        "target_filter": target_filter,
        "canary": canary,
        "max_parallel": max_parallel,
        "override": list(override),
        "target_chip": target_chip,
        "group_id": str(group_id) if group_id is not None else None,
        **({"policy_id": str(policy_id)} if policy_id is not None else {}),
    }
    url_builder = mint_url or mint_deploy_artifact_url
    stage_tasks = await _prepare_stage_tasks(
        session,
        wave_items=wave_items,
        project_id=project_id,
        version=version,
        apply=apply,
        override=override,
        sent_by=sent_by,
        rollout_meta=rollout_meta,
        settings=settings,
        url_builder=url_builder,
        supports_relay_kwarg=(url_builder is mint_deploy_artifact_url),
    )
    await session.commit()
    deployed = await _publish_stage_tasks(
        session,
        stage_tasks=stage_tasks,
        project_id=project_id,
        version=version,
        apply=apply,
        publisher=publisher,
        best_effort_broker=best_effort_broker,
        skipped=skipped,
    )
    return deployed, skipped


async def advance_project_rollout_on_confirm(
    session: AsyncSession,
    *,
    cmd_id: str,
    settings: Settings,
    publisher: CommandPublisher,
    device: Device | None = None,
    device_id: str | None = None,
) -> list[DeployAccepted]:
    """Advance a staged project cohort rollout when a board confirms its wave transaction."""
    if (
        isinstance(publisher, NullCommandPublisher)
        or not settings.artifact_url_secret
        or not settings.public_base_url
    ):
        logger.warning(
            "cannot advance staged rollout for cmd_id=%s: command publisher or artifact URL config missing",
            cmd_id,
        )
        return []
    resolved_device = device
    if resolved_device is None and device_id is not None:
        resolved_device = await session.get(Device, device_id)
    if resolved_device is None:
        return []

    req = await requested_rollout(session, device_id=resolved_device.device_id, cmd_id=cmd_id)
    if req is None:
        return []

    rollout_cfg = req.rollout
    target_filter = str(rollout_cfg.get("target_filter") or "all")
    canary = bool(rollout_cfg.get("canary", True))
    raw_max_parallel = rollout_cfg.get("max_parallel", DEFAULT_ROLLOUT_MAX_PARALLEL)
    max_parallel = (
        int(raw_max_parallel) if isinstance(raw_max_parallel, int) else DEFAULT_ROLLOUT_MAX_PARALLEL
    )
    raw_override = rollout_cfg.get("override")
    override = tuple(str(item) for item in raw_override) if isinstance(raw_override, list) else ()
    raw_chip = rollout_cfg.get("target_chip")
    target_chip = str(raw_chip) if isinstance(raw_chip, str) else None
    raw_group = rollout_cfg.get("group_id")
    group_id = uuid.UUID(raw_group) if isinstance(raw_group, str) and raw_group else None
    raw_policy = rollout_cfg.get("policy_id")
    policy_id = uuid.UUID(raw_policy) if isinstance(raw_policy, str) and raw_policy else None

    _, is_closed, started_at = await resolve_rollout_for_confirm(
        session,
        project_id=resolved_device.project_id,
        version=req.artifact_version,
        target_filter=target_filter,
        target_chip=target_chip,
        group_id=group_id,
        policy_id=policy_id,
        fallback_started_at=req.at,
    )
    if is_closed:
        return []

    from fleetforge.deploy_policies import (
        advance_project_policies_for_version,
        group_is_policy_governed,
    )

    if policy_id is not None or await group_is_policy_governed(
        session,
        project_id=resolved_device.project_id,
        version=req.artifact_version,
        group_id=group_id,
    ):
        return await advance_project_policies_for_version(
            session,
            project_id=resolved_device.project_id,
            version=req.artifact_version,
            settings=settings,
            publisher=publisher,
            target_policy_id=policy_id,
        )

    deployed, _ = await deploy_version_to_project_devices(
        session,
        project_id=resolved_device.project_id,
        version=req.artifact_version,
        target_filter=target_filter,
        apply=req.apply,
        override=override,
        admin=None,
        settings=settings,
        publisher=publisher,
        target_chip=target_chip,
        group_id=group_id,
        best_effort_broker=True,
        canary=canary,
        max_parallel=max_parallel,
        sent_by_override=req.sent_by,
        republish_in_flight=False,
        rollout_started_at=started_at,
    )
    policy_promoted = await advance_project_policies_for_version(
        session,
        project_id=resolved_device.project_id,
        version=req.artifact_version,
        settings=settings,
        publisher=publisher,
    )
    seen_ids = {item.device_id for item in deployed}
    for item in policy_promoted:
        if item.device_id not in seen_ids:
            deployed.append(item)
            seen_ids.add(item.device_id)
    return deployed


async def _peer_cohort_halted(
    session: AsyncSession,
    *,
    device: Device,
    cmd_id: str,
    version: str,
) -> bool:
    req = await requested_rollout(session, device_id=device.device_id, cmd_id=cmd_id)
    group_filter: uuid.UUID | None = None
    if req is not None:
        raw_group = req.rollout.get("group_id")
        if isinstance(raw_group, str) and raw_group:
            group_filter = uuid.UUID(raw_group)

    peer_stmt = select(Device.device_id).where(
        Device.project_id == device.project_id,
        Device.platform_type == device.platform_type,
        Device.decommissioned_at.is_(None),
    )
    if group_filter is not None:
        peer_stmt = peer_stmt.where(Device.group_id == group_filter)
    peer_ids = list((await session.scalars(peer_stmt)).all())
    if not peer_ids:
        return False
    peer_snaps = await latest_deploys(session, device_ids=peer_ids)
    return any(
        _is_cohort_halt_snapshot(peer_snaps.get(pid), version, include_cancelled=True)
        for pid in peer_ids
    )


async def republish_open_requested_on_wake(
    session: AsyncSession,
    *,
    device_id: str,
    settings: Settings,
    publisher: CommandPublisher,
) -> bool:
    """Re-mint a fresh signed URL and re-publish `stage` when a board wakes with an open `requested` deploy."""
    if (
        isinstance(publisher, NullCommandPublisher)
        or not settings.artifact_url_secret
        or not settings.public_base_url
    ):
        return False

    device = await session.get(Device, device_id)
    if device is None or device.decommissioned_at is not None:
        return False

    snap = (await latest_deploys(session, device_ids=[device_id])).get(device_id)
    if (
        snap is None
        or snap.state != "requested"
        or snap.is_terminal
        or not snap.artifact_version
        or not snap.cmd_id
    ):
        return False

    ttl_s = effective_url_ttl_s(device, settings)
    if (now_utc() - snap.at).total_seconds() > ttl_s:
        return False

    if await _peer_cohort_halted(
        session, device=device, cmd_id=snap.cmd_id, version=snap.artifact_version
    ):
        return False

    found_artifact = await resolve_artifact_for_target(
        session,
        project_id=device.project_id,
        target=device.platform_type,
        version=snap.artifact_version,
        settings=settings,
    )
    if found_artifact is None:
        return False

    tx = await deploy_transaction(session, device_id=device_id, cmd_id=snap.cmd_id)
    apply_mode = tx.apply if tx is not None and tx.apply else "auto"
    url, delta_payload = await build_stage_urls_and_delta(
        session,
        device=device,
        project_id=device.project_id,
        sha256=found_artifact.sha256,
        ttl_s=ttl_s,
        settings=settings,
        mint_url=mint_deploy_artifact_url,
    )
    payload = stage_payload(
        cmd_id=snap.cmd_id,
        url=url,
        sha256=found_artifact.sha256,
        size=found_artifact.size_bytes,
        version=snap.artifact_version,
        apply=apply_mode,
        confirm_timeout_s=settings.confirm_timeout_s,
        sig=found_artifact.sig,
        artifact_type=found_artifact.artifact_type,
        delta=delta_payload,
    )
    await publisher.publish(device_id, payload)
    return True
