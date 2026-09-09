"""The device registry's one write path. **The only `INSERT` into `devices`.**

Its counterpart, `ingestor/store.py`, exists in order *not* to do this: "an
`INSERT … ON CONFLICT` there would be an enrollment bypass — anyone who can publish to
the broker joins the fleet". The only way into the registry is a burned single-use
enrollment token, and this module is the function that burn calls.

Transport-agnostic and **it does not commit** — the caller owns the transaction,
because the insert and the burn (`auth.enrollment.BURN_SQL`) must be atomic with each
other: `enrollment_tokens.used_by_device_id` is a real FK to `devices.device_id`, so
burning first is an `IntegrityError` on the happy path, and a refused burn must roll
the device row back rather than leave a half-enrolled board behind.

The upsert is deliberate. A re-flashed board legitimately re-enrolls with a *new*
token, and a caller holding a valid unburned token can already enroll any `device_id`
it likes — so refusing a known `device_id` buys no security and costs the bench a
manual `DELETE`.
"""

import datetime as dt
import logging
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.db.models import Device

logger = logging.getLogger(__name__)

# Everything the freshly flashed agent is authoritative about. Overwritten on
# re-enrollment: firmware genuinely changes a board's `power_class`, its partition
# layout and its capabilities, and a stale value silently selects the wrong presence
# rule or the wrong OTA slot size.
IDENTITY_FIELDS = (
    "platform_type",
    "proto",
    "fw_version",
    "agent_version",
    "link_type",
    "power_class",
    "expected_wake_interval_s",
    "parent_device_id",
    "partition_layout",
    "ota_slot_size",
    "capabilities",
)


async def enroll_device(
    session: AsyncSession,
    *,
    device_id: str,
    group_id: Any,
    identity: dict[str, Any],
    enrolled_at: dt.datetime,
) -> Device:
    """Insert (or re-enroll) `device_id` and return the row. Does not commit.

    `identity` holds the announced facts (`IDENTITY_FIELDS`); `device_id` and
    `group_id` are passed separately because neither is a device claim — the group
    comes from the **token**, never from the request body, and the device id has
    already been validated against `fleetforge.identity.DEVICE_ID_RE` (it is about to
    become an MQTT username that two `%u` pattern ACLs bind to).

    What the conflict branch does, and why:

    * `group_id = COALESCE(:group_id, devices.group_id)` — an ungrouped token must not
      un-group a device an operator already placed.
    * `enrolled_at = :enrolled_at` — this *is* a new enrollment.
    * `decommissioned_at = NULL` — re-enrolling a retired board revives it; otherwise
      the ingestor keeps dropping its messages forever with no visible cause.
    * `broker_provisioned_at = NULL` — not provisioned *yet*. The caller stamps it
      after the broker actually holds the credential.
    * `presence_reported = NULL` — the old retained presence describes a session that
      no longer exists.
    * `name` is **untouched**: operator-set (`spec/flows.md` Flow 1 step 7).
    * `last_seen` is **untouched**: a historical fact, and monotonic everywhere else.
    * `created_at` is insert-only and must never appear in the update set.
    """
    values: dict[str, Any] = {
        "device_id": device_id,
        "group_id": group_id,
        "enrolled_at": enrolled_at,
        **{field: identity.get(field) for field in IDENTITY_FIELDS},
    }
    # `capabilities` is NOT NULL with a server default; an absent list means "empty",
    # never NULL.
    if values.get("capabilities") is None:
        values["capabilities"] = []
    if values.get("proto") is None:
        values["proto"] = 1

    statement = insert(Device).values(**values)
    updates: dict[str, Any] = {field: statement.excluded[field] for field in IDENTITY_FIELDS}
    updates.update(
        {
            "group_id": func.coalesce(statement.excluded.group_id, Device.group_id),
            "enrolled_at": statement.excluded.enrolled_at,
            "decommissioned_at": None,
            "broker_provisioned_at": None,
            "presence_reported": None,
            "updated_at": func.now(),
        }
    )
    statement = statement.on_conflict_do_update(index_elements=[Device.device_id], set_=updates)

    row = (
        await session.scalars(
            statement.returning(Device), execution_options={"populate_existing": True}
        )
    ).one()
    await session.flush()
    return row
