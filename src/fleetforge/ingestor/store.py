"""The ingestor's writes. **`UPDATE` only — this module must never `INSERT`.**

The only way into the registry is `POST /v1/enroll` burning a single-use token
(R0-be-4). An `INSERT … ON CONFLICT` here — "so we don't lose the announce" — would
be an enrollment bypass: anyone who can publish to the broker joins the fleet, and
the dev broker is anonymous today (`mosquitto/conf.d/10-dev-anonymous.conf`, the one
file R0-sec-1 deletes). Every statement below is therefore

    UPDATE devices … WHERE device_id = :id AND decommissioned_at IS NULL RETURNING *

and zero rows back means "unregistered or decommissioned" — the caller logs and drops
the message. Decommissioned boards are dropped deliberately: a soft-deleted device
that still publishes must not reappear in the dashboard, and the log line is the
signal that its broker credential was never revoked.

`RETURNING` the whole row is what lets the caller derive `online` from post-write
state inside the same transaction, with no read-then-write.

Nothing here commits: the caller owns the transaction, the same convention the API's
routers follow.
"""

import datetime as dt
import logging
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.db.models import Device, PowerClass
from fleetforge.ingestor.protocol import AnnouncePayload

logger = logging.getLogger(__name__)

# Identity fields an announce is allowed to move. `device_id`, `group_id`, `name`,
# `parent_device_id`, `enrolled_at` and `broker_provisioned_at` are operator or
# enrollment facts, never device claims, so they are absent on purpose.
ANNOUNCE_FIELDS = (
    "proto",
    "platform_type",
    "fw_version",
    "agent_version",
    "link_type",
    "partition_layout",
    "ota_slot_size",
    "capabilities",
)


def _live_device(device_id: str) -> Any:
    """The one WHERE clause: this device, and not soft-deleted."""
    return (Device.device_id == device_id, Device.decommissioned_at.is_(None))


async def _apply(session: AsyncSession, device_id: str, values: dict[str, Any]) -> Device | None:
    """Run the one statement shape and return the updated row, or `None`."""
    stmt = (
        update(Device)
        .where(*_live_device(device_id))
        .values(**values)
        .returning(Device)
        .execution_options(synchronize_session=False)
    )
    return (await session.scalars(stmt)).one_or_none()


async def fetch_live_device(session: AsyncSession, device_id: str) -> Device | None:
    """Read the row without writing it — for a message that changes nothing.

    Public because `handlers.py` needs it for a **retained** `up/status`: the state is
    ingested but proves no liveness, so there is nothing to UPDATE and the caller still
    needs the "registered and not decommissioned" answer this module owns. A read does
    not break the module's "UPDATE only, never INSERT" rule.
    """
    return (await session.scalars(select(Device).where(*_live_device(device_id)))).one_or_none()


def _last_seen_value(at: dt.datetime) -> Any:
    """`GREATEST(last_seen, :at)` — monotonic `last_seen`, in SQL.

    QoS 1 is at-least-once and a reconnect can re-deliver, so a duplicate or an
    out-of-order delivery must not move a device backwards in time. PostgreSQL's
    `GREATEST` ignores NULLs, so the very first message still lands.
    """
    return func.greatest(Device.last_seen, at)


async def touch_last_seen(session: AsyncSession, device_id: str, at: dt.datetime) -> Device | None:
    """Record that a live message arrived, and nothing else."""
    return await _apply(session, device_id, {"last_seen": _last_seen_value(at)})


async def apply_announce(
    session: AsyncSession,
    device_id: str,
    payload: AnnouncePayload,
    at: dt.datetime,
    *,
    advance_last_seen: bool,
) -> Device | None:
    """Apply the identity fields an announce actually carried.

    Fields absent from the payload mean "no change" and are not written
    (`spec/device-protocol.md` → *Evolution rules*). `link_type` is written straight
    through however odd it looks — the column is TEXT and there are no PG enums, so a
    board on a link nobody has invented yet still shows up in the dashboard.
    """
    values: dict[str, Any] = {
        field: getattr(payload, field)
        for field in ANNOUNCE_FIELDS
        if getattr(payload, field) is not None
    }
    values.update(_power_class_values(device_id, payload))
    if advance_last_seen:
        values["last_seen"] = _last_seen_value(at)
    if not values:
        # A retained announce carrying nothing we map changes nothing; `UPDATE … SET`
        # needs at least one column, so read the row instead of writing a no-op.
        return await fetch_live_device(session, device_id)
    return await _apply(session, device_id, values)


def _power_class_values(device_id: str, payload: AnnouncePayload) -> dict[str, Any]:
    """`power_class` and `expected_wake_interval_s`: written as a pair, or not at all.

    Both have DB CHECKs (`power_class`, `sleepy_wake_interval`), and an
    `IntegrityError` here would lose the **whole** announce — including the
    `fw_version` that tells the operator the OTA landed — over a field the dashboard
    barely uses. So the pair is validated in Python and dropped from the update when
    it would not hold, with the CHECK left as the backstop.

    They are written at all (rather than frozen at enrollment) because a firmware
    change can genuinely move a board from `always_on` to `sleepy`, and a stale
    `power_class` silently selects the wrong presence rule: the device then reads
    permanently online or permanently offline with nothing in the UI hinting why.
    """
    power_class = payload.power_class
    interval = payload.expected_wake_interval_s
    if power_class is None and interval is None:
        return {}

    if power_class is not None and power_class not in set(PowerClass):
        logger.warning(
            "device %s announced unknown power_class %r; keeping the stored value",
            device_id,
            power_class,
        )
        return {}

    values: dict[str, Any] = {}
    if power_class is not None:
        values["power_class"] = power_class
    if interval is not None:
        values["expected_wake_interval_s"] = interval

    if power_class == PowerClass.SLEEPY and (interval is None or interval <= 0):
        # The effective interval would come from the stored row, which the CHECK may
        # well reject. Only `expected_wake_interval_s` present in this same payload is
        # provably safe; anything else is refused as a pair.
        logger.warning(
            "device %s announced power_class=sleepy with no usable wake interval; "
            "keeping the stored values",
            device_id,
        )
        return {}
    if interval is not None and interval <= 0:
        logger.warning(
            "device %s announced expected_wake_interval_s=%r; keeping the stored value",
            device_id,
            interval,
        )
        return {}
    return values


async def apply_presence(
    session: AsyncSession,
    device_id: str,
    online: bool,
    at: dt.datetime,
    *,
    advance_last_seen: bool,
) -> Device | None:
    """Record the retained `up/presence` value — the `always_on` presence authority."""
    values: dict[str, Any] = {"presence_reported": online}
    if advance_last_seen:
        values["last_seen"] = _last_seen_value(at)
    return await _apply(session, device_id, values)


async def apply_heartbeat(
    session: AsyncSession,
    device_id: str,
    fw_version: str | None,
    at: dt.datetime,
    *,
    advance_last_seen: bool,
) -> Device | None:
    """Advance `last_seen`, and take the reported firmware version if there is one.

    `up/hb` is QoS 1 with retain **no**, so `advance_last_seen` is always true in
    practice; it is a parameter rather than an assumption so that the retained rule
    lives in exactly one place for all four writers.
    """
    values: dict[str, Any] = {}
    if advance_last_seen:
        values["last_seen"] = _last_seen_value(at)
    if fw_version is not None:
        values["fw_version"] = fw_version
    if not values:
        return await fetch_live_device(session, device_id)
    return await _apply(session, device_id, values)
