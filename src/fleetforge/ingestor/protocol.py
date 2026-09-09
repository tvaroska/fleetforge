"""The wire side of ingestion: topic parsing and tolerant payload decoding.

`spec/device-protocol.md` is a **CRITICAL.md** path and near-frozen; everything here
is a reading of it, never an extension of it.

**Tolerate everything, reject nothing on vocabulary** (*Evolution rules*: additive
changes only, both sides ignore unknown fields). So every payload model has
`extra="ignore"`, **every field is optional** — a missing field means "no change",
not "set to NULL" — and an unknown `up/` channel is logged and dropped rather than
raising. A `ValidationError` escaping into the message loop would be a
fleet-visibility outage caused by one board's firmware bug.
"""

import json
import logging
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, ValidationError

from fleetforge.identity import DEVICE_ID_RE

logger = logging.getLogger(__name__)

# `DEVICE_ID_RE` — the eFuse MAC rule, and why it is a security control — now lives
# in `fleetforge.identity`: `POST /v1/enroll` (R0-be-4) must apply the same rule and
# the API may not import from `fleetforge.ingestor`.

TOPIC_PREFIX = ("ff", "v1", "d")


class UpChannel(StrEnum):
    """The `up/` channels this server knows about.

    A **vocabulary for dispatch, never a validator**: a v1.x agent may invent a
    channel, and `UpTopic.channel` is therefore a plain `str`.
    """

    ANNOUNCE = "announce"
    PRESENCE = "presence"
    HB = "hb"
    STATUS = "status"
    TELEMETRY = "telemetry"
    LOG = "log"


@dataclass(frozen=True, slots=True)
class UpTopic:
    """A parsed `ff/v1/d/{device_id}/up/{channel}` topic. The topic is authoritative."""

    device_id: str
    # `str`, not `UpChannel`, and it may contain '/': the subscription is `up/#`.
    channel: str


def parse_up_topic(topic: str) -> UpTopic | None:
    """Parse a device-to-server topic, or return `None` if it is not one.

    `None` covers a wrong prefix, a wrong protocol version, a `dn/` topic, and a
    `device_id` that does not match `DEVICE_ID_RE`. The caller logs at DEBUG and
    drops: an unparseable topic is not an error condition, it is noise.
    """
    parts = topic.split("/")
    if len(parts) < 6:
        return None
    if tuple(parts[:3]) != TOPIC_PREFIX or parts[4] != "up":
        return None
    device_id = parts[3]
    if not DEVICE_ID_RE.match(device_id):
        return None
    channel = "/".join(parts[5:])
    if not channel:
        return None
    return UpTopic(device_id=device_id, channel=channel)


class _TolerantPayload(BaseModel):
    """Base for every inbound payload: unknown fields are ignored, not rejected."""

    model_config = ConfigDict(extra="ignore")


class AnnouncePayload(_TolerantPayload):
    """`up/announce` — full identity, republished on every boot, retained.

    `parent_device_id` is deliberately **not** mapped: parentage is an enrollment-time
    fact (R0-be-4), and letting a board reparent itself over MQTT is a V3 authz
    question. `boot_ok` / `uptime_s` / `rssi` / `free_heap` are R3 telemetry.
    """

    proto: int | None = None
    device_id: str | None = None
    platform_type: str | None = None
    fw_version: str | None = None
    agent_version: str | None = None
    link_type: str | None = None
    power_class: str | None = None
    expected_wake_interval_s: int | None = None
    partition_layout: str | None = None
    ota_slot_size: int | None = None
    capabilities: list[str] | None = None


class PresencePayload(_TolerantPayload):
    """`up/presence` — `{"online":true}` from the device, `{"online":false}` from the LWT."""

    online: bool | None = None


class HeartbeatPayload(_TolerantPayload):
    """`up/hb` — the liveness beat. Its health fields (`uptime_s`, `rssi`, …) are R3."""

    fw_version: str | None = None


def decode[T: _TolerantPayload](model: type[T], topic: str, payload: bytes) -> T | None:
    """Decode `payload` into `model`, or return `None` if it is not usable.

    Malformed JSON, non-UTF-8 bytes, a JSON array where an object belongs and a
    wrong-typed field all land here — none of them may reach the caller as an
    exception. Only the topic and the payload **length** are logged: logs are not a
    data store, and R3 telemetry bodies get large.
    """
    try:
        data = json.loads(payload)
        return model.model_validate(data)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError) as exc:
        logger.info(
            "undecodable %s payload on %s (%d bytes): %s",
            model.__name__,
            topic,
            len(payload),
            type(exc).__name__,
        )
        return None
