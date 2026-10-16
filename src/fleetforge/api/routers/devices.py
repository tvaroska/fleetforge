"""`GET /v1/devices` — the fleet read model — and `PATCH /v1/devices/{device_id}`.

The other half of the SSE contract. `fleetforge.events` and `fleetforge.presence`
both define the rule as *"the event is a hint; the consumer re-reads
`GET /v1/devices`"*, because presence is derived and a sleepy device goes offline
with no event at all — so a client that patched its state from event payloads would
show a dead board as online forever. This endpoint is what that re-read hits, and
R0-fe-2, the CLI and Home Assistant all use it.

**Presence is computed here, on read**, by `fleetforge.presence.is_online` with
`settings.presence_tolerance` — there is no `2.5` in this file and no `online`
column in the database. One `now` serves the whole response: a list whose rows
disagree about the current time is a confusing thing to debug.

`PATCH /v1/devices/{device_id}` (R2b-be-1) sets the two operator-set facts, `name` and
`group_id` (the *group/tag* of `spec/flows.md`), with merge-patch semantics. Device
claims are not settable here: the board announces them. Names are unique among live
boards, case-insensitively, checked in the app (no DB constraint, which would need a
migration). The route emits `device.updated` so other open dashboards re-read.

Deliberately absent, each with a reason:

* `presence_reported` — `online` is the answer. Exposing the ingredient invites a
  client to re-derive the rule, and `presence.py` exists so there is exactly one
  implementation of it.
* decommissioned rows — a soft-deleted device is gone from the fleet view, and the
  ingestor drops its messages too. `?include_decommissioned=` arrives with the
  decommission endpoint (`docs/features/enrollment.md` → *Post-v1*).
* `GET /v1/devices/{device_id}` and pagination — `spec/prd.md` → *Capacity* is 25
  devices; the list is the read model. Same reasoning as `LIST_LIMIT`.

Two per-device **sub-resources** exist (R4-be-1), and they are not on the list because
they are unbounded per device: `GET /v1/devices/{device_id}/logs` (up to 5 000 stored
lines, `fleetforge.device_logs`) and `GET /v1/devices/{device_id}/telemetry?name=` (up
to 500 000 samples, `fleetforge.telemetry_store`). The list carries only what is
bounded: `last_reset_reason`, the fault counts over `settings.fault_window_s`
(`fleetforge.faults`, derived on read), the latest heartbeat's `health`, and the latest
value of at most 32 `metrics`, each read for exactly the ids the list already holds.

`arrivals` (S0-fw-1) rides on this response rather than getting an endpoint of its
own, for the same reason: the dashboard already re-reads this on every `ff_events`
hint, so boards *arriving* need no second fetch and no poll. They are the boards that
have reported a boot stage recently and are neither **currently online** nor **already
arrived** — the first decided by the same `is_online` call that fills in the rows
above, the second by `progress.has_already_arrived`.

Two clauses and not one, because "not online" alone cannot tell a board that is on its
way up from one that came up days ago and has since lost power: both are offline with
a recent stage, and the second was reappearing as "stalled at `mqtt_connected`" for the
rest of the progress window, duplicating a row this same response already showed as
offline (S0-fe-3). The distinction lives in `progress.py`, not here.

`deploy` (R1-fe-1) rides here for the third time on the same argument. The dashboard's
one refresh engine is hint→re-read-this-endpoint, so a deploy state on its own endpoint
would need its own poll to answer the case that matters: a board that reported
`awaiting_safe_window` and then went quiet for an hour emits no further event, and its
state is still the correct thing to show. The SQL is `deploys.latest_deploys` — that
module owns `deploy_events` for reads as well as writes — and it is bounded by the same
`LIST_LIMIT` as the list above, because it is handed exactly the ids already read.
Since R2b-fe-9 it also carries the current transaction's steps and the confirm window,
for the dashboard's update timeline.

`last_command` (R5-be-2) rides here on the same argument: the newest verb the server sent
the board and its first answer (`device_commands.latest_commands`, bounded by the same
ids). The verbs themselves are `api/routers/commands.py`.
"""

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.api.deps import (
    AdminDep,
    SessionMakerDep,
    SettingsDep,
    bearer_scheme,
    cookie_scheme,
)
from fleetforge.api.schemas import (
    ArrivalSummary,
    CommandSummary,
    DeploySelfTest,
    DeploySender,
    DeployStep,
    DeploySummary,
    DeviceList,
    DeviceLogLine,
    DeviceLogTail,
    DeviceSummary,
    DeviceUpdate,
    FaultResets,
    HealthSnapshot,
    MetricValue,
    TelemetryPoint,
    TelemetrySeries,
)
from fleetforge.clock import now_utc
from fleetforge.db.models import Device, DeviceGroup
from fleetforge.deploys import DeploySnapshot, latest_deploys
from fleetforge.device_commands import CommandSnapshot, latest_commands
from fleetforge.device_logs import read_tail
from fleetforge.events import DeviceEvent, EventType, emit
from fleetforge.faults import fault_counts
from fleetforge.presence import is_online
from fleetforge.progress import has_already_arrived, latest_progress
from fleetforge.retention import LOG_MAX_LINES_PER_DEVICE
from fleetforge.telemetry_store import TELEMETRY_NAME_RE, latest_metrics, read_series

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/devices",
    tags=["devices"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)

# Same cap and same reason as `routers/enrollment.py`: a guard against an unbounded
# response, not real paging, at a fleet size of ~25.
LIST_LIMIT = 200

# The sub-resources' page sizes (R4-be-1). A log tail can ask for everything a device
# keeps; a series for at most as many points.
LOG_TAIL_DEFAULT = 200
SERIES_DEFAULT = 500
SERIES_MAX = 5_000


@dataclass(slots=True)
class _HealthExtras:
    """What R4-be-1 adds to each row, read once for all the ids a response holds."""

    window_s: int
    faults: dict[str, dict[str, int]] = field(default_factory=dict)
    metrics: dict[str, list[MetricValue]] = field(default_factory=dict)

    def fault_resets(self, device_id: str) -> FaultResets:
        counts = self.faults.get(device_id, {})
        return FaultResets(
            window_s=self.window_s,
            panic=counts.get("panic", 0),
            task_wdt=counts.get("task_wdt", 0),
            brownout=counts.get("brownout", 0),
        )


async def _health_extras(
    session: AsyncSession, *, device_ids: Sequence[str], now: dt.datetime, window_s: int
) -> _HealthExtras:
    """Fault counts and latest metrics for exactly `device_ids` (the `latest_deploys` rule)."""
    ids = list(device_ids)
    counts = await fault_counts(session, device_ids=ids, since=now - dt.timedelta(seconds=window_s))
    snapshots = await latest_metrics(session, device_ids=ids, now=now)
    return _HealthExtras(
        window_s=window_s,
        faults=counts,
        metrics={
            device_id: [MetricValue(name=m.name, value=m.value, at=m.at) for m in metrics]
            for device_id, metrics in snapshots.items()
        },
    )


def _health_snapshot(row: Device) -> HealthSnapshot | None:
    """The stored heartbeat snapshot, or `None` when no live heartbeat ever arrived."""
    if row.health_at is None:
        return None
    return HealthSnapshot(
        at=row.health_at,
        uptime_s=row.uptime_s,
        rssi=row.rssi,
        free_heap=row.free_heap,
        min_free_heap=row.min_free_heap,
        boot_ok=row.boot_ok,
    )


def _deploy_summary(
    snapshot: DeploySnapshot | None, *, confirm_timeout_s: int
) -> DeploySummary | None:
    """A board's newest deploy state, or `None` when it has never been deployed to.

    A field-by-field copy rather than `model_validate`, the `DeviceSummary` rule: a
    column added to `deploy_events` must not leak into a response by default.
    """
    if snapshot is None:
        return None
    return DeploySummary(
        cmd_id=snapshot.cmd_id,
        state=snapshot.state,
        at=snapshot.at,
        is_terminal=snapshot.is_terminal,
        artifact_version=snapshot.artifact_version,
        from_version=snapshot.from_version,
        pct=snapshot.pct,
        detail=snapshot.detail,
        steps=[DeployStep(state=step.state, at=step.at) for step in snapshot.steps],
        confirm_timeout_s=confirm_timeout_s,
        sent_by=(
            DeploySender(subject=snapshot.sent_by.subject, credential=snapshot.sent_by.credential)
            if snapshot.sent_by is not None
            else None
        ),
        selftest=(
            DeploySelfTest(
                outcome=snapshot.selftest.outcome,
                reason=snapshot.selftest.reason,
                returned_to=snapshot.selftest.returned_to,
            )
            if snapshot.selftest is not None
            else None
        ),
    )


def command_summary(snapshot: CommandSnapshot | None) -> CommandSummary | None:
    """A board's newest command and its first answer, or `None` (R5-be-2). Field by field."""
    if snapshot is None:
        return None
    return CommandSummary(
        command_id=snapshot.command_id,
        type=snapshot.type,
        cmd_id=snapshot.cmd_id,
        sent_at=snapshot.sent_at,
        sent_by=(
            DeploySender(subject=snapshot.sent_by.subject, credential=snapshot.sent_by.credential)
            if snapshot.sent_by is not None
            else None
        ),
        result=snapshot.result,
        detail=snapshot.detail,
        answered_at=snapshot.answered_at,
    )


def _device_summary(
    row: Device,
    *,
    online: bool,
    deploy: DeploySummary | None,
    extras: _HealthExtras,
    last_command: CommandSummary | None = None,
) -> DeviceSummary:
    """One row as the fleet view sees it. Field by field, `from_attributes` stays off."""
    return DeviceSummary(
        device_id=row.device_id,
        name=row.name,
        group_id=row.group_id,
        platform_type=row.platform_type,
        fw_version=row.fw_version,
        agent_version=row.agent_version,
        link_type=row.link_type,
        ssid=row.ssid,
        known_networks=row.known_networks,
        power_class=row.power_class,
        expected_wake_interval_s=row.expected_wake_interval_s,
        parent_device_id=row.parent_device_id,
        partition_layout=row.partition_layout,
        ota_slot_size=row.ota_slot_size,
        flash_chip_size=row.flash_chip_size,
        partition_table_sha256=row.partition_table_sha256,
        rollback_capable=row.rollback_capable,
        capabilities=list(row.capabilities),
        last_seen=row.last_seen,
        enrolled_at=row.enrolled_at,
        broker_provisioned_at=row.broker_provisioned_at,
        online=online,
        last_reset_reason=row.last_reset_reason,
        fault_resets=extras.fault_resets(row.device_id),
        health=_health_snapshot(row),
        metrics=extras.metrics.get(row.device_id, []),
        last_command=last_command,
        deploy=deploy,
    )


@router.get("", response_model=DeviceList, summary="Enrolled devices, with derived presence")
async def list_devices(
    admin: AdminDep,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
) -> DeviceList:
    """The fleet, newest enrollment first, with `online` derived per row."""
    now = now_utc()
    async with sessionmaker() as session:
        rows = (
            await session.scalars(
                select(Device)
                .where(Device.decommissioned_at.is_(None))
                .order_by(Device.enrolled_at.desc(), Device.device_id)
                .limit(LIST_LIMIT)
            )
        ).all()
        arrivals = await latest_progress(
            session,
            now=now,
            window_s=settings.progress_window_s,
            stall_s=settings.progress_stall_s,
        )
        # Handed the ids this response already read, so there is no second unbounded
        # scan and the deploy read inherits `LIST_LIMIT` for free.
        deploys = await latest_deploys(session, device_ids=[row.device_id for row in rows])
        # R5-be-2: the newest verb per board, bounded by the same ids.
        commands = await latest_commands(session, device_ids=[row.device_id for row in rows])
        extras = await _health_extras(
            session,
            device_ids=[row.device_id for row in rows],
            now=now,
            window_s=settings.fault_window_s,
        )

    online_now = {
        row.device_id
        for row in rows
        if is_online(row, now=now, tolerance=settings.presence_tolerance)
    }
    # The same rows the fleet list is built from — the arrivals filter needs their
    # `broker_provisioned_at` and `last_seen`, and re-querying for them would be a
    # second read of a list this endpoint already holds.
    by_device_id = {row.device_id: row for row in rows}

    return DeviceList(
        devices=[
            _device_summary(
                row,
                online=row.device_id in online_now,
                deploy=_deploy_summary(
                    deploys.get(row.device_id), confirm_timeout_s=settings.confirm_timeout_s
                ),
                extras=extras,
                last_command=command_summary(commands.get(row.device_id)),
            )
            for row in rows
        ],
        # A board still arriving is one that has reported a stage and has not made it
        # into the fleet yet. Not filtered by `decommissioned_at`: a retired board
        # being re-flashed reports stages again, and hiding that is the gap all over —
        # and since such a board is absent from `by_device_id`, `has_already_arrived`
        # returns False for it and that stays true.
        arrivals=[
            ArrivalSummary(
                device_id=arrival.device_id,
                stage=arrival.stage,
                detail=arrival.detail,
                at=arrival.at,
                stalled=arrival.stalled,
            )
            for arrival in arrivals
            if arrival.device_id not in online_now
            and not has_already_arrived(by_device_id.get(arrival.device_id), stage_at=arrival.at)
        ],
    )


@router.patch(
    "/{device_id}",
    response_model=DeviceSummary,
    summary="Set a device's name and group",
)
async def update_device(
    device_id: str,
    body: DeviceUpdate,
    admin: AdminDep,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
) -> DeviceSummary:
    """Merge-patch `name` / `group_id`; returns the updated row as the list shows it.

    Atomic: every check runs before any assignment, so a refused PATCH changes
    nothing and emits nothing. Only field *names* are logged, never the name text.
    """
    now = now_utc()
    fields = body.model_fields_set
    async with sessionmaker() as session:
        device = await session.get(Device, device_id)
        if device is None or device.decommissioned_at is not None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such device: {device_id}")

        if "name" in fields and body.name is not None:
            other = await session.scalar(
                select(Device.device_id)
                .where(
                    func.lower(Device.name) == body.name.lower(),
                    Device.device_id != device_id,
                    Device.decommissioned_at.is_(None),
                )
                .limit(1)
            )
            if other is not None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"name already used by device {other}: {body.name}",
                )

        if "group_id" in fields and body.group_id is not None:
            if await session.get(DeviceGroup, body.group_id) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such group: {body.group_id}")

        changed = {field for field in fields if getattr(device, field) != getattr(body, field)}
        if changed:
            for field in changed:
                setattr(device, field, getattr(body, field))
            await emit(
                session,
                DeviceEvent(
                    type=EventType.DEVICE_UPDATED,
                    device_id=device_id,
                    at=now,
                    online=is_online(device, now=now, tolerance=settings.presence_tolerance),
                    fw_version=device.fw_version,
                ),
            )
            try:
                await session.commit()
            except IntegrityError:
                # The only FK being written is `group_id`: the group vanished between
                # the check above and the commit (same reasoning as enrollment.py).
                await session.rollback()
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, f"no such group: {body.group_id}"
                ) from None

        deploys = await latest_deploys(session, device_ids=[device_id])
        commands = await latest_commands(session, device_ids=[device_id])
        extras = await _health_extras(
            session, device_ids=[device_id], now=now, window_s=settings.fault_window_s
        )
        summary = _device_summary(
            device,
            online=is_online(device, now=now, tolerance=settings.presence_tolerance),
            deploy=_deploy_summary(
                deploys.get(device_id), confirm_timeout_s=settings.confirm_timeout_s
            ),
            extras=extras,
            last_command=command_summary(commands.get(device_id)),
        )

    logger.info(
        "device %s updated by %s: %s", device_id, admin.token_id, sorted(changed) or "no change"
    )
    return summary


async def _live_device_or_404(session: AsyncSession, device_id: str) -> Device:
    """The device, or the PATCH route's 404 for an unknown or decommissioned one."""
    device = await session.get(Device, device_id)
    if device is None or device.decommissioned_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such device: {device_id}")
    return device


@router.get(
    "/{device_id}/logs",
    response_model=DeviceLogTail,
    summary="A device's newest firmware log lines (up/log), newest first",
)
async def device_log_tail(
    device_id: str,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
    limit: Annotated[int, Query(ge=1, le=LOG_MAX_LINES_PER_DEVICE)] = LOG_TAIL_DEFAULT,
) -> DeviceLogTail:
    """Stored lines inside the 7-day window. `text` is sanitised device text; never logged."""
    now = now_utc()
    async with sessionmaker() as session:
        await _live_device_or_404(session, device_id)
        lines = await read_tail(session, device_id=device_id, now=now, limit=limit)
    return DeviceLogTail(
        device_id=device_id,
        lines=[
            DeviceLogLine(
                id=line.id,
                logged_at=line.logged_at,
                received_at=line.received_at,
                level=line.level,
                text=line.text,
                dropped=line.dropped,
            )
            for line in lines
        ],
    )


@router.get(
    "/{device_id}/telemetry",
    response_model=TelemetrySeries,
    summary="One metric's newest samples (up/telemetry), newest first",
)
async def device_telemetry_series(
    device_id: str,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
    name: Annotated[str, Query(pattern=f"^{TELEMETRY_NAME_RE.pattern}$")],
    limit: Annotated[int, Query(ge=1, le=SERIES_MAX)] = SERIES_DEFAULT,
) -> TelemetrySeries:
    """Samples inside the 30-day window; `name` is the ingestor's metric-name rule."""
    now = now_utc()
    async with sessionmaker() as session:
        await _live_device_or_404(session, device_id)
        points = await read_series(session, device_id=device_id, name=name, now=now, limit=limit)
    return TelemetrySeries(
        device_id=device_id,
        name=name,
        samples=[TelemetryPoint(at=point.at, value=point.value) for point in points],
    )
