"""`GET /v1/devices` — the fleet read model.

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

Deliberately absent, each with a reason:

* `presence_reported` — `online` is the answer. Exposing the ingredient invites a
  client to re-derive the rule, and `presence.py` exists so there is exactly one
  implementation of it.
* decommissioned rows — a soft-deleted device is gone from the fleet view, and the
  ingestor drops its messages too. `?include_decommissioned=` arrives with the
  decommission endpoint (`docs/features/enrollment.md` → *Post-v1*).
* `GET /v1/devices/{device_id}` and pagination — `spec/prd.md` → *Capacity* is 25
  devices; the list is the read model. Same reasoning as `LIST_LIMIT`.

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
"""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select

from fleetforge.api.deps import (
    AdminDep,
    SessionMakerDep,
    SettingsDep,
    bearer_scheme,
    cookie_scheme,
)
from fleetforge.api.schemas import ArrivalSummary, DeviceList, DeviceSummary
from fleetforge.clock import now_utc
from fleetforge.db.models import Device
from fleetforge.presence import is_online
from fleetforge.progress import has_already_arrived, latest_progress

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/devices",
    tags=["devices"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)

# Same cap and same reason as `routers/enrollment.py`: a guard against an unbounded
# response, not real paging, at a fleet size of ~25.
LIST_LIMIT = 200


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
            DeviceSummary(
                device_id=row.device_id,
                name=row.name,
                group_id=row.group_id,
                platform_type=row.platform_type,
                fw_version=row.fw_version,
                agent_version=row.agent_version,
                link_type=row.link_type,
                power_class=row.power_class,
                expected_wake_interval_s=row.expected_wake_interval_s,
                parent_device_id=row.parent_device_id,
                partition_layout=row.partition_layout,
                ota_slot_size=row.ota_slot_size,
                capabilities=list(row.capabilities),
                last_seen=row.last_seen,
                enrolled_at=row.enrolled_at,
                broker_provisioned_at=row.broker_provisioned_at,
                online=row.device_id in online_now,
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
