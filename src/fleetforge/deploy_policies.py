"""Multi-ring staged cohort deploy policy engine (`R11-1`, `A-22`).

Maps Git branch or tag-glob provenance (`match_mode`, `match_ref`) to an ordered list of
`device_groups.id` cohort rings (for example `[canary, staging, prod]`) and gates every
wave and ring-to-ring promotion on:
- Persisted per-`(policy_id, version)` run state (`deploy_policy_runs`) so a halt survives
  later device events until an operator passes `resume=True` explicitly.
- Valid, non-empty rings: a deleted group or a ring with zero compatible devices halts.
- Zero `rolled_back`, `cancelled`, or non-`publish_failed` `failed` outcomes on `version`
  (current or historical since the last resume) across any policy ring.
- Zero boards entering `safe_mode` or recording fault resets (`panic`, `task_wdt`,
  `brownout`) after confirming `version` (via `up/status` or `up/announce`).
- Known, sufficient `free_heap` when `min_free_heap_bytes` is configured (unknown health
  never passes the gate), optional `require_sim_passed`, and `min_soak_s` counted from
  first ring completeness.
"""

from __future__ import annotations

import fnmatch
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.api.deps import AuthContext
from fleetforge.api.schemas_deploys import DeployAccepted
from fleetforge.api.schemas_v2 import (
    DeployPolicyEvaluation,
    PolicyEvalStatusLiteral,
    PolicyRingStatus,
)
from fleetforge.broker import CommandPublisher, NullCommandPublisher
from fleetforge.clock import now_utc
from fleetforge.config import Settings
from fleetforge.db.models import DeployPolicy, Device, DeviceGroup, Project
from fleetforge.deploy_policy_gates import (
    RingStatusLiteral as RingStatusLiteral,
)
from fleetforge.deploy_policy_gates import (
    build_halted_rings as _build_halted_rings,
)
from fleetforge.deploy_policy_gates import (
    detect_ring_halt as _detect_ring_halt,
)
from fleetforge.deploy_policy_gates import (
    device_confirmed_version as _device_confirmed_version,
)
from fleetforge.deploy_policy_gates import (
    device_in_flight_version as _device_in_flight_version,
)
from fleetforge.deploy_policy_gates import (
    historical_version_halts as _historical_version_halts,
)
from fleetforge.deploy_policy_gates import (
    load_or_create_run as _load_or_create_run,
)
from fleetforge.deploy_policy_gates import (
    parse_iso_timestamp as _parse_iso_timestamp,
)
from fleetforge.deploy_policy_gates import (
    pending_tail_rings as _pending_tail_rings,
)
from fleetforge.deploy_policy_gates import (
    persist_halt as _persist_halt,
)
from fleetforge.deploys import latest_deploys
from fleetforge.rollout import deploy_version_to_project_devices
from fleetforge.rollout_state import close_matching_rollouts

_VERSION_TARGETS_SQL = text(
    """
    SELECT v.target, v.branch, v.tag, v.test_passed, v.created_at, a.sim_status
      FROM artifact_versions AS v
      JOIN artifacts AS a ON a.sha256 = v.sha256
     WHERE v.project_id = :project_id AND v.version = :version
    """
)


def matches_deploy_policy(
    policy: DeployPolicy,
    *,
    branch: str | None,
    tag: str | None,
    test_passed: bool | None,
    sim_status: str | None,
) -> bool:
    """Return True when an artifact's provenance satisfies `policy`'s trigger rule."""
    if not policy.enabled:
        return False
    if test_passed is False:
        return False
    if policy.require_sim_passed and sim_status != "passed":
        return False
    ref = policy.match_ref.strip()
    if not ref:
        return False
    if policy.match_mode == "branch":
        return branch is not None and branch == ref
    if policy.match_mode == "tag":
        return tag is not None and fnmatch.fnmatchcase(tag, ref)
    return False


async def evaluate_policy_for_version(
    session: AsyncSession,
    *,
    policy: DeployPolicy,
    version: str,
    settings: Settings,
    publisher: CommandPublisher,
    admin: AuthContext | None = None,
    republish_in_flight: bool = False,
    resume: bool = False,
) -> DeployPolicyEvaluation:
    """Evaluate `policy` for `version`, halting on unhealthy/missing rings or promoting."""
    target_rows = (
        await session.execute(
            _VERSION_TARGETS_SQL,
            {"project_id": policy.project_id, "version": version},
        )
    ).all()
    if not target_rows:
        return DeployPolicyEvaluation(
            policy_id=policy.id,
            version=version,
            status="skipped",
            halt_reason=f"version {version} does not exist in project",
            rings=[],
            deployed=[],
        )

    all_targets = {str(r.target) for r in target_rows}
    eligible_targets = (
        {str(r.target) for r in target_rows if r.sim_status == "passed"}
        if policy.require_sim_passed
        else all_targets
    )
    if policy.require_sim_passed and not eligible_targets:
        return DeployPolicyEvaluation(
            policy_id=policy.id,
            version=version,
            status="halted",
            halt_reason=f"policy requires sim_status='passed' before staging version {version}",
            rings=[
                PolicyRingStatus(
                    ring_index=idx,
                    group_id=gid,
                    status="halted" if idx == 0 else "pending",
                    total_devices=0,
                    confirmed_devices=0,
                    in_flight_devices=0,
                )
                for idx, gid in enumerate(policy.ring_group_ids)
            ],
            deployed=[],
        )

    # Lock the project row BEFORE reading run state, devices, or snapshots (`A-22`).
    if isinstance(session, AsyncSession):
        await session.execute(
            select(Project.id).where(Project.id == policy.project_id).with_for_update()
        )

    now = now_utc()
    run = await _load_or_create_run(session, policy_id=policy.id, version=version, now=now)

    ring_ids = list(policy.ring_group_ids)
    existing_group_ids = set(
        (
            await session.scalars(
                select(DeviceGroup.id).where(
                    DeviceGroup.project_id == policy.project_id,
                    DeviceGroup.id.in_(ring_ids),
                )
            )
        ).all()
    )
    devices_rows = list(
        (
            await session.scalars(
                select(Device)
                .where(
                    Device.project_id == policy.project_id,
                    Device.decommissioned_at.is_(None),
                    Device.group_id.in_(ring_ids),
                )
                .order_by(Device.device_id)
            )
        ).all()
    )
    by_group: dict[uuid.UUID, list[Device]] = {gid: [] for gid in ring_ids}
    for dev in devices_rows:
        if (
            dev.group_id is not None
            and dev.group_id in by_group
            and dev.platform_type in eligible_targets
        ):
            by_group[dev.group_id].append(dev)

    all_ring_device_ids = [d.device_id for d in devices_rows]
    snapshots = (
        await latest_deploys(session, device_ids=all_ring_device_ids) if all_ring_device_ids else {}
    )

    if run is not None and run.status == "halted":
        if not resume:
            halted_idx = run.halted_ring_index if run.halted_ring_index is not None else 0
            return DeployPolicyEvaluation(
                policy_id=policy.id,
                version=version,
                status="halted",
                halt_reason=run.halt_reason or f"ring {halted_idx} halted",
                rings=_build_halted_rings(ring_ids, by_group, snapshots, version, halted_idx),
                deployed=[],
            )
        prior_halts = await _historical_version_halts(
            session,
            device_ids=all_ring_device_ids,
            version=version,
            since=None,
        )
        # Anchor resume past existing events so small ingestor/API clock skew never re-trips it.
        resume_watermark = max(
            [
                now,
                *(snap.at for snap in snapshots.values()),
                *(ev_at for _, ev_at in prior_halts.values()),
            ]
        )
        run.status = "active"
        run.halted_ring_index = None
        run.halt_reason = None
        run.halted_at = None
        run.resumed_at = resume_watermark
        run.resumed_by = admin.subject if admin is not None else None
        run.updated_at = now

    resumed_at = run.resumed_at if run is not None else None
    historical_halts = await _historical_version_halts(
        session,
        device_ids=all_ring_device_ids,
        version=version,
        since=resumed_at,
    )
    created_times = [
        r.created_at for r in target_rows if getattr(r, "created_at", None) is not None
    ]
    artifact_created_at = min(created_times) if created_times else None

    halted_ring_idx: int | None = None
    halt_reason: str | None = None
    for idx, gid in enumerate(ring_ids):
        reason = await _detect_ring_halt(
            session,
            ring_idx=idx,
            ring_devices=by_group.get(gid, []),
            snapshots=snapshots,
            version=version,
            min_free_heap_bytes=policy.min_free_heap_bytes,
            historical_halts=historical_halts,
            artifact_created_at=artifact_created_at,
            resumed_at=resumed_at,
        )
        if reason is not None:
            halted_ring_idx = idx
            halt_reason = reason
            break

    if halted_ring_idx is not None and halt_reason is not None:
        await _persist_halt(
            session,
            policy=policy,
            run=run,
            version=version,
            ring_idx=halted_ring_idx,
            reason=halt_reason,
            now=now,
        )
        return DeployPolicyEvaluation(
            policy_id=policy.id,
            version=version,
            status="halted",
            halt_reason=halt_reason,
            rings=_build_halted_rings(ring_ids, by_group, snapshots, version, halted_ring_idx),
            deployed=[],
        )

    ring_statuses: list[PolicyRingStatus] = []
    deployed_all: list[DeployAccepted] = []
    overall_status: PolicyEvalStatusLiteral = "completed"

    for idx, gid in enumerate(ring_ids):
        r_devs = by_group.get(gid, [])
        if gid not in existing_group_ids or not r_devs:
            missing_reason = (
                f"ring {idx} halted: group {gid} was deleted"
                if gid not in existing_group_ids
                else f"ring {idx} halted: group {gid} has no compatible devices for version {version}"
            )
            await _persist_halt(
                session,
                policy=policy,
                run=run,
                version=version,
                ring_idx=idx,
                reason=missing_reason,
                now=now,
            )
            return DeployPolicyEvaluation(
                policy_id=policy.id,
                version=version,
                status="halted",
                halt_reason=missing_reason,
                rings=_build_halted_rings(ring_ids, by_group, snapshots, version, idx),
                deployed=[],
            )

        confirmed_devs = [
            d for d in r_devs if _device_confirmed_version(d, snapshots.get(d.device_id), version)
        ]
        in_flight_devs = [
            d for d in r_devs if _device_in_flight_version(snapshots.get(d.device_id), version)
        ]
        unknown_heap_devs = (
            [d for d in confirmed_devs if d.free_heap is None]
            if policy.min_free_heap_bytes is not None
            else []
        )

        if len(confirmed_devs) < len(r_devs):
            if unknown_heap_devs:
                # Unknown free_heap on an already-confirmed board does not pass the heap gate:
                # hold further waves inside this ring until telemetry proves healthy heap.
                staged_now: list[DeployAccepted] = []
            else:
                override_codes = ("sim_failed",) if not policy.require_sim_passed else ()
                staged_now, _ = await deploy_version_to_project_devices(
                    session,
                    project_id=policy.project_id,
                    version=version,
                    target_filter="all",
                    apply="auto",
                    override=override_codes,
                    admin=admin,
                    settings=settings,
                    publisher=publisher,
                    group_id=gid,
                    best_effort_broker=True,
                    canary=True,
                    max_parallel=policy.max_parallel,
                    republish_in_flight=republish_in_flight,
                    policy_id=policy.id,
                    ignore_halts_before=resumed_at,
                )
            deployed_all.extend(staged_now)
            newly_staged_ids = {item.device_id for item in staged_now}
            in_flight_total = len({d.device_id for d in in_flight_devs} | newly_staged_ids)
            ring_statuses.append(
                PolicyRingStatus(
                    ring_index=idx,
                    group_id=gid,
                    status="in_progress",
                    total_devices=len(r_devs),
                    confirmed_devices=len(confirmed_devs),
                    in_flight_devices=in_flight_total,
                )
            )
            ring_statuses.extend(
                _pending_tail_rings(
                    ring_ids, idx + 1, by_group, snapshots, version, count_confirmed=True
                )
            )
            overall_status = "in_progress"
            break

        if unknown_heap_devs:
            # Unknown free_heap on a confirmed board never passes `min_free_heap_bytes`.
            ring_statuses.append(
                PolicyRingStatus(
                    ring_index=idx,
                    group_id=gid,
                    status="soaking",
                    total_devices=len(r_devs),
                    confirmed_devices=len(confirmed_devs),
                    in_flight_devices=0,
                )
            )
            ring_statuses.extend(
                _pending_tail_rings(ring_ids, idx + 1, by_group, snapshots, version)
            )
            overall_status = "soaking"
            break

        ring_key = str(idx)
        recorded_ts = (
            _parse_iso_timestamp(run.ring_completed_at.get(ring_key))
            if run is not None and isinstance(run.ring_completed_at, dict)
            else None
        )
        if recorded_ts is None:
            confirm_times = [
                snap.at
                for d in confirmed_devs
                if (snap := snapshots.get(d.device_id)) is not None
                and snap.artifact_version == version
                and snap.state == "confirmed"
            ]
            completed_at = max(confirm_times) if len(confirm_times) == len(confirmed_devs) else now
            if run is not None:
                updated_map = dict(run.ring_completed_at or {})
                updated_map[ring_key] = completed_at.isoformat()
                run.ring_completed_at = updated_map
                run.updated_at = now
        else:
            completed_at = recorded_ts

        if policy.min_soak_s > 0 and (now - completed_at).total_seconds() < policy.min_soak_s:
            ring_statuses.append(
                PolicyRingStatus(
                    ring_index=idx,
                    group_id=gid,
                    status="soaking",
                    total_devices=len(r_devs),
                    confirmed_devices=len(confirmed_devs),
                    in_flight_devices=0,
                )
            )
            ring_statuses.extend(
                _pending_tail_rings(ring_ids, idx + 1, by_group, snapshots, version)
            )
            overall_status = "soaking"
            break

        ring_statuses.append(
            PolicyRingStatus(
                ring_index=idx,
                group_id=gid,
                status="promoted",
                total_devices=len(r_devs),
                confirmed_devices=len(confirmed_devs),
                in_flight_devices=0,
            )
        )

    if overall_status == "completed":
        if run is not None:
            run.status = "completed"
            run.updated_at = now
        await close_matching_rollouts(
            session,
            project_id=policy.project_id,
            version=version,
            target_filter="all",
            target_chip=None,
            group_id=None,
            policy_id=policy.id,
            status_value="completed",
            reason=None,
        )

    if isinstance(session, AsyncSession):
        await session.commit()

    return DeployPolicyEvaluation(
        policy_id=policy.id,
        version=version,
        status=overall_status,
        halt_reason=None,
        rings=ring_statuses,
        deployed=deployed_all,
    )


async def group_is_policy_governed(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    version: str,
    group_id: uuid.UUID | None,
) -> bool:
    """Return True if an enabled `DeployPolicy` matching `version` includes `group_id`."""
    if group_id is None or not isinstance(session, AsyncSession):
        return False
    raw_policies = (
        await session.scalars(
            select(DeployPolicy).where(
                DeployPolicy.project_id == project_id,
                DeployPolicy.enabled.is_(True),
            )
        )
    ).all()
    candidates = [
        p for p in raw_policies if isinstance(p, DeployPolicy) and group_id in p.ring_group_ids
    ]
    if not candidates:
        return False
    target_rows = (
        await session.execute(
            _VERSION_TARGETS_SQL,
            {"project_id": project_id, "version": version},
        )
    ).all()
    if not target_rows:
        return False
    return any(
        matches_deploy_policy(
            pol,
            branch=r.branch,
            tag=r.tag,
            test_passed=r.test_passed,
            sim_status=r.sim_status,
        )
        for pol in candidates
        for r in target_rows
    )


async def trigger_matching_policies_on_upload(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    version: str,
    branch: str | None,
    tag: str | None,
    test_passed: bool | None,
    sim_status: str | None,
    admin: AuthContext | None,
    settings: Settings,
    publisher: CommandPublisher,
) -> list[str]:
    """Evaluate all enabled `DeployPolicy` rows matching an uploaded artifact's provenance."""
    if (
        isinstance(publisher, NullCommandPublisher)
        or not settings.artifact_url_secret
        or not settings.public_base_url
    ):
        return []

    policies = list(
        (
            await session.scalars(
                select(DeployPolicy)
                .where(DeployPolicy.project_id == project_id, DeployPolicy.enabled.is_(True))
                .order_by(DeployPolicy.created_at.asc(), DeployPolicy.id.asc())
            )
        ).all()
    )
    staged_ids: list[str] = []
    for policy in policies:
        if not matches_deploy_policy(
            policy,
            branch=branch,
            tag=tag,
            test_passed=test_passed,
            sim_status=sim_status,
        ):
            continue
        evaluation = await evaluate_policy_for_version(
            session,
            policy=policy,
            version=version,
            settings=settings,
            publisher=publisher,
            admin=admin,
            republish_in_flight=True,
        )
        for item in evaluation.deployed:
            if item.device_id not in staged_ids:
                staged_ids.append(item.device_id)
    return staged_ids


async def advance_project_policies_for_version(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    version: str,
    settings: Settings,
    publisher: CommandPublisher,
    target_policy_id: uuid.UUID | None = None,
) -> list[DeployAccepted]:
    """Promote enabled `DeployPolicy` runs whose active ring confirmed `version`."""
    if (
        not isinstance(session, AsyncSession)
        or isinstance(publisher, NullCommandPublisher)
        or not settings.artifact_url_secret
        or not settings.public_base_url
    ):
        return []

    stmt = (
        select(DeployPolicy)
        .where(DeployPolicy.project_id == project_id, DeployPolicy.enabled.is_(True))
        .order_by(DeployPolicy.created_at.asc(), DeployPolicy.id.asc())
    )
    if target_policy_id is not None:
        stmt = stmt.where(DeployPolicy.id == target_policy_id)

    raw_policies = (await session.scalars(stmt)).all()
    policies = [p for p in raw_policies if isinstance(p, DeployPolicy)]
    if not policies:
        return []

    target_rows = (
        await session.execute(
            _VERSION_TARGETS_SQL,
            {"project_id": project_id, "version": version},
        )
    ).all()
    if not target_rows:
        return []
    advanced: list[DeployAccepted] = []
    for policy in policies:
        matched_any = any(
            matches_deploy_policy(
                policy,
                branch=r.branch,
                tag=r.tag,
                test_passed=r.test_passed,
                sim_status=r.sim_status,
            )
            for r in target_rows
        )
        if not matched_any:
            continue
        evaluation = await evaluate_policy_for_version(
            session,
            policy=policy,
            version=version,
            settings=settings,
            publisher=publisher,
            admin=None,
            republish_in_flight=False,
        )
        advanced.extend(evaluation.deployed)
    return advanced
