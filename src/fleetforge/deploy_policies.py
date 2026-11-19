"""Multi-ring staged cohort deploy policy engine (`R11-1`, closing `V2`).

Maps Git branch or tag-glob provenance (`match_mode`, `match_ref`) to an ordered list of
`device_groups.id` cohort rings (for example `[canary, staging, prod]`) and gates
automatic ring-to-ring promotion on:
- Zero `rolled_back`, `cancelled`, or non-`publish_failed` `failed` outcomes on `version`
  across any policy ring.
- Zero boards entering `safe_mode` or recording fault resets (`panic`, `task_wdt`,
  `brownout`) after confirming `version`.
- Optional pre-flight simulation pass (`require_sim_passed`), minimum post-confirm soak
  duration (`min_soak_s`), and minimum free heap (`min_free_heap_bytes`).
"""

from __future__ import annotations

import fnmatch
import uuid
from typing import Literal

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
from fleetforge.db.models import DeployPolicy, Device
from fleetforge.deploys import DeploySnapshot, latest_deploys
from fleetforge.faults import fault_counts
from fleetforge.rollout import deploy_version_to_project_devices

_VERSION_TARGETS_SQL = text(
    """
    SELECT v.target, v.branch, v.tag, v.test_passed, a.sim_status
      FROM artifact_versions AS v
      JOIN artifacts AS a ON a.sha256 = v.sha256
     WHERE v.project_id = :project_id AND v.version = :version
    """
)

RingStatusLiteral = Literal["pending", "in_progress", "soaking", "promoted", "halted", "empty"]


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


def _device_confirmed_version(dev: Device, snap: DeploySnapshot | None, version: str) -> bool:
    if dev.fw_version == version:
        return True
    return snap is not None and snap.artifact_version == version and snap.state == "confirmed"


def _device_in_flight_version(snap: DeploySnapshot | None, version: str) -> bool:
    return snap is not None and snap.artifact_version == version and not snap.is_terminal


async def _detect_ring_halt(
    session: AsyncSession,
    *,
    ring_idx: int,
    ring_devices: list[Device],
    snapshots: dict[str, DeploySnapshot],
    version: str,
    min_free_heap_bytes: int | None,
) -> str | None:
    """Return a non-empty halt reason if any board in `ring_devices` tripped a safety/health gate."""
    for dev in ring_devices:
        snap = snapshots.get(dev.device_id)
        if snap is not None and snap.artifact_version == version:
            if snap.state == "rolled_back":
                return f"ring {ring_idx} halted: device {dev.device_id} rolled_back on version {version}"
            if snap.state == "cancelled":
                return (
                    f"ring {ring_idx} halted: device {dev.device_id} cancelled on version {version}"
                )
            if snap.state == "failed" and snap.reason != "publish_failed":
                return f"ring {ring_idx} halted: device {dev.device_id} failed on version {version}"

        if _device_confirmed_version(dev, snap, version):
            if dev.safe_mode:
                return (
                    f"ring {ring_idx} halted: device {dev.device_id} entered safe_mode "
                    f"on version {version}"
                )
            if (
                min_free_heap_bytes is not None
                and dev.free_heap is not None
                and dev.free_heap < min_free_heap_bytes
            ):
                return (
                    f"ring {ring_idx} halted: device {dev.device_id} free_heap "
                    f"{dev.free_heap} < {min_free_heap_bytes} bytes"
                )
            if snap is not None and snap.artifact_version == version:
                f_counts = await fault_counts(
                    session,
                    device_ids=[dev.device_id],
                    since=snap.at,
                )
                if sum(f_counts.get(dev.device_id, {}).values()) > 0:
                    return (
                        f"ring {ring_idx} halted: device {dev.device_id} recorded fault reset "
                        f"after confirming {version}"
                    )
    return None


async def evaluate_policy_for_version(
    session: AsyncSession,
    *,
    policy: DeployPolicy,
    version: str,
    settings: Settings,
    publisher: CommandPublisher,
    admin: AuthContext | None = None,
    republish_in_flight: bool = False,
) -> DeployPolicyEvaluation:
    """Evaluate `policy` for `version`, halting on unhealthy rings or promoting the active ring."""
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

    ring_ids = list(policy.ring_group_ids)
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
        )
        if reason is not None:
            halted_ring_idx = idx
            halt_reason = reason
            break

    if halted_ring_idx is not None and halt_reason is not None:
        halted_rings: list[PolicyRingStatus] = []
        for idx, gid in enumerate(ring_ids):
            r_devs = by_group.get(gid, [])
            confirmed_n = sum(
                1
                for d in r_devs
                if _device_confirmed_version(d, snapshots.get(d.device_id), version)
            )
            in_flight_n = sum(
                1 for d in r_devs if _device_in_flight_version(snapshots.get(d.device_id), version)
            )
            r_status: RingStatusLiteral = (
                "halted"
                if idx == halted_ring_idx
                else ("promoted" if idx < halted_ring_idx and r_devs else "pending")
            )
            halted_rings.append(
                PolicyRingStatus(
                    ring_index=idx,
                    group_id=gid,
                    status=r_status,
                    total_devices=len(r_devs),
                    confirmed_devices=confirmed_n,
                    in_flight_devices=in_flight_n,
                )
            )
        return DeployPolicyEvaluation(
            policy_id=policy.id,
            version=version,
            status="halted",
            halt_reason=halt_reason,
            rings=halted_rings,
            deployed=[],
        )

    now = now_utc()
    ring_statuses: list[PolicyRingStatus] = []
    deployed_all: list[DeployAccepted] = []
    overall_status: PolicyEvalStatusLiteral = "completed"

    for idx, gid in enumerate(ring_ids):
        r_devs = by_group.get(gid, [])
        if not r_devs:
            ring_statuses.append(
                PolicyRingStatus(
                    ring_index=idx,
                    group_id=gid,
                    status="empty",
                    total_devices=0,
                    confirmed_devices=0,
                    in_flight_devices=0,
                )
            )
            continue

        confirmed_devs = [
            d for d in r_devs if _device_confirmed_version(d, snapshots.get(d.device_id), version)
        ]
        in_flight_devs = [
            d for d in r_devs if _device_in_flight_version(snapshots.get(d.device_id), version)
        ]

        if len(confirmed_devs) < len(r_devs):
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
            for rem_idx, rem_gid in enumerate(ring_ids[idx + 1 :], start=idx + 1):
                rem_devs = by_group.get(rem_gid, [])
                ring_statuses.append(
                    PolicyRingStatus(
                        ring_index=rem_idx,
                        group_id=rem_gid,
                        status="pending",
                        total_devices=len(rem_devs),
                        confirmed_devices=sum(
                            1
                            for d in rem_devs
                            if _device_confirmed_version(d, snapshots.get(d.device_id), version)
                        ),
                        in_flight_devices=0,
                    )
                )
            overall_status = "in_progress"
            break

        if policy.min_soak_s > 0:
            confirm_times = [
                snap.at
                for d in confirmed_devs
                if (snap := snapshots.get(d.device_id)) is not None
                and snap.artifact_version == version
                and snap.state == "confirmed"
            ]
            latest_confirm = max(confirm_times) if confirm_times else now
            if (now - latest_confirm).total_seconds() < policy.min_soak_s:
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
                for rem_idx, rem_gid in enumerate(ring_ids[idx + 1 :], start=idx + 1):
                    rem_devs = by_group.get(rem_gid, [])
                    ring_statuses.append(
                        PolicyRingStatus(
                            ring_index=rem_idx,
                            group_id=rem_gid,
                            status="pending",
                            total_devices=len(rem_devs),
                            confirmed_devices=0,
                            in_flight_devices=0,
                        )
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

    return DeployPolicyEvaluation(
        policy_id=policy.id,
        version=version,
        status=overall_status,
        halt_reason=None,
        rings=ring_statuses,
        deployed=deployed_all,
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
) -> list[DeployAccepted]:
    """Promote any enabled `DeployPolicy` whose active ring just finished confirming `version`."""
    if (
        not isinstance(session, AsyncSession)
        or isinstance(publisher, NullCommandPublisher)
        or not settings.artifact_url_secret
        or not settings.public_base_url
    ):
        return []

    raw_policies = (
        await session.scalars(
            select(DeployPolicy)
            .where(DeployPolicy.project_id == project_id, DeployPolicy.enabled.is_(True))
            .order_by(DeployPolicy.created_at.asc(), DeployPolicy.id.asc())
        )
    ).all()
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
