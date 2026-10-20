"""The wire side of ingestion: topic parsing and tolerant payload decoding.

`spec/device-protocol.md` is a **CRITICAL.md** path and near-frozen; everything here
is a reading of it, never an extension of it.

**Tolerate everything, reject nothing on vocabulary** (*Evolution rules*: additive
changes only, both sides ignore unknown fields). So every payload model has
`extra="ignore"`, **every field is optional** — a missing field means "no change",
not "set to NULL" (the exceptions, `ssid` / `known_networks` and the three board
measurements, are explained in `store.py::_network_values` and
`store.py::_measurement_values`) — and an unknown `up/` channel is logged and dropped
rather than raising. A `ValidationError` escaping into the message loop would be a
fleet-visibility outage caused by one board's firmware bug.
"""

import json
import logging
import math
from dataclasses import dataclass
from enum import StrEnum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from fleetforge.announce_fields import (
    normalize_flash_chip_size,
    normalize_known_networks,
    normalize_partition_table_sha256,
    normalize_reset_reason,
    normalize_rollback_capable,
    normalize_ssid,
)
from fleetforge.identity import DEVICE_ID_RE
from fleetforge.retention import LOG_RETENTION, TELEMETRY_RETENTION
from fleetforge.telemetry_store import TELEMETRY_NAME_RE

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
    # R5-be-2 (R5-spec-1 Patch A5): a board's answer to a verb. QoS 1, never retained.
    ACK = "ack"


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


def _logged_device_id(info: ValidationInfo) -> str | None:
    """The payload's `device_id` for a log line, only if it is a canonical one.

    The topic is authoritative and the body's copy is unvalidated board text; a log
    line must not echo arbitrary bytes from it.
    """
    value = info.data.get("device_id")
    return value if isinstance(value, str) and DEVICE_ID_RE.match(value) else None


class AnnouncePayload(_TolerantPayload):
    """`up/announce` — full identity, republished on every boot, retained.

    `parent_device_id` is deliberately **not** mapped: parentage is an enrollment-time
    fact (R0-be-4), and letting a board reparent itself over MQTT is a V3 authz
    question. `boot_ok` / `uptime_s` / `rssi` / `free_heap` are R4 telemetry.

    `ssid` / `known_networks` (agent 0.4.6) are written as a pair on every announce,
    absent and null alike meaning "not reported" (`store.py::_network_values`); a
    malformed value **coerces to `None`** rather than raising, for the reason
    `StatusPayload` gives — a `ValidationError` here would lose the `fw_version` too.

    `flash_chip_size` / `partition_table_sha256` / `rollback_capable` (R2b-be-6) follow
    the same two rules: written as a triple on every announce, absent meaning "unknown"
    (`store.py::_measurement_values`), and malformed coercing to `None`. They are
    declared after `device_id` so a log line can name the board.

    `reset_reason` (S0-fw-6) is logged on a live announce and, since R4-be-1, stored on
    every announce as `devices.last_reset_reason` (`store.py::_reset_values`, absent
    meaning "not reported", so `store.ANNOUNCE_FIELDS` leaves it out) and counted as a
    fault on a live one (`fleetforge.faults`). Malformed coerces to `None` like the rest.
    """

    proto: int | None = None
    device_id: str | None = None
    platform_type: str | None = None
    fw_version: str | None = None
    agent_version: str | None = None
    link_type: str | None = None
    ssid: str | None = None
    known_networks: int | None = None
    power_class: str | None = None
    expected_wake_interval_s: int | None = None
    partition_layout: str | None = None
    ota_slot_size: int | None = None
    flash_chip_size: int | None = None
    partition_table_sha256: str | None = None
    rollback_capable: bool | None = None
    capabilities: list[str] | None = None
    reset_reason: str | None = None
    secrets_rev: int | None = None

    @field_validator("ssid", mode="before")
    @classmethod
    def _usable_ssid(cls, value: object, info: ValidationInfo) -> str | None:
        """The SSID as reported, or `None` — never a `ValidationError`."""
        return normalize_ssid(value, device_id=_logged_device_id(info))

    @field_validator("known_networks", mode="before")
    @classmethod
    def _usable_known_networks(cls, value: object, info: ValidationInfo) -> int | None:
        """A plausible count, or `None` — never a `ValidationError`."""
        return normalize_known_networks(value, device_id=_logged_device_id(info))

    @field_validator("flash_chip_size", mode="before")
    @classmethod
    def _usable_flash_chip_size(cls, value: object, info: ValidationInfo) -> int | None:
        """A positive size that fits the agent's `uint32_t`, or `None` — never raises."""
        return normalize_flash_chip_size(value, device_id=_logged_device_id(info))

    @field_validator("partition_table_sha256", mode="before")
    @classmethod
    def _usable_partition_table_sha256(cls, value: object, info: ValidationInfo) -> str | None:
        """64 lowercase hex characters, or `None` — never a `ValidationError`."""
        return normalize_partition_table_sha256(value, device_id=_logged_device_id(info))

    @field_validator("rollback_capable", mode="before")
    @classmethod
    def _usable_rollback_capable(cls, value: object, info: ValidationInfo) -> bool | None:
        """A real JSON boolean, or `None` — `"true"` and `1` are not coerced."""
        return normalize_rollback_capable(value, device_id=_logged_device_id(info))

    @field_validator("reset_reason", mode="before")
    @classmethod
    def _usable_reset_reason(cls, value: object, info: ValidationInfo) -> str | None:
        """A short string, or `None` — never a `ValidationError` (S0-fw-6)."""
        return normalize_reset_reason(value, device_id=_logged_device_id(info))

    @field_validator("secrets_rev", mode="before")
    @classmethod
    def _usable_secrets_rev(cls, value: object) -> int | None:
        """A non-negative integer, or `None` — never a `ValidationError` (CUJ-2)."""
        return _bounded_int(value, 0, 2**31 - 1)


class PresencePayload(_TolerantPayload):
    """`up/presence` — `{"online":true}` from the device, `{"online":false}` from the LWT."""

    online: bool | None = None


# Bounds for the heartbeat's health fields (R4-be-1). Generous: they only keep a value
# that is not a reading (or would not fit its column) out of the database.
UPTIME_MAX_S = 315_360_000  # ten years
RSSI_MIN_DBM = -127
HEAP_MAX_BYTES = 2**40


def _bounded_int(value: object, low: int, high: int) -> int | None:
    """An integer in `[low, high]`, or `None`. `bool` is an `int` in Python and is not one here."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


class HeartbeatPayload(_TolerantPayload):
    """`up/hb` — the liveness beat, and since R4-be-1 the health snapshot.

    `{"fw_version","uptime_s","rssi","free_heap","min_free_heap","boot_ok"}`
    (`spec/device-protocol.md` -> `up/hb`). The five health fields are written as a group
    on every live heartbeat (`store.py::_health_values`), so each is **tolerant**: a
    malformed one is `None` ("not reported"), never a `ValidationError` that would lose
    the heartbeat, its `last_seen` and its `fw_version`. `rssi` is null on ethernet;
    `min_free_heap` is absent from agents before 0.5.0 (R4-fw-1); `boot_ok` must be a
    real JSON boolean (`1` is not coerced).
    """

    fw_version: str | None = None
    uptime_s: int | None = None
    rssi: int | None = None
    free_heap: int | None = None
    min_free_heap: int | None = None
    boot_ok: bool | None = None

    @field_validator("uptime_s", mode="before")
    @classmethod
    def _plausible_uptime(cls, value: object) -> int | None:
        return _bounded_int(value, 0, UPTIME_MAX_S)

    @field_validator("rssi", mode="before")
    @classmethod
    def _plausible_rssi(cls, value: object) -> int | None:
        return _bounded_int(value, RSSI_MIN_DBM, 0)

    @field_validator("free_heap", "min_free_heap", mode="before")
    @classmethod
    def _plausible_heap(cls, value: object) -> int | None:
        return _bounded_int(value, 0, HEAP_MAX_BYTES)

    @field_validator("boot_ok", mode="before")
    @classmethod
    def _real_boolean(cls, value: object) -> bool | None:
        return value if isinstance(value, bool) else None


class StatusPayload(_TolerantPayload):
    """`up/status` — the update transaction, QoS 1 and **retained**.

    Tolerance matters more here than on any other channel. A `ValidationError` makes
    `decode` return `None` and the message is dropped — and because the value is
    retained, the same drop repeats on every reconnect, so the outcome is lost forever
    rather than for one delivery. `pct` and `detail` therefore **coerce** instead of
    raising: a future agent's richer `detail`, or a `pct` sent as a string, must not
    cost us the `state` that is the whole point of the message.

    `cmd_id` and `state` stay strictly `str | None`: a non-string there is a broken
    board and there is nothing honest to record.
    """

    cmd_id: str | None = None
    state: str | None = None
    pct: int | None = None
    detail: str | None = None

    @field_validator("pct", mode="before")
    @classmethod
    def _plausible_pct(cls, value: object) -> int | None:
        """A percentage or nothing. `bool` is an `int` in Python and is not one here."""
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if 0 <= value <= 100 else None

    @field_validator("detail", mode="before")
    @classmethod
    def _detail_as_text(cls, value: object) -> str | None:
        """Anything the board sent, rendered as text. The writer sanitises and truncates."""
        if value is None or isinstance(value, str):
            return value
        return str(value)


# `up/telemetry` (R4-fw-2; the payload is a proposal, docs/features/health-telemetry.md ->
# *up/telemetry proposal*). `TELEMETRY_NAME_RE` (imported above) is a valid Prometheus
# metric name, and the firmware's FF_TELEMETRY_NAME_MAX (31) is `{0,30}` + 1:
# tests/test_telemetry.py holds the two, and the simulator's retyped copy, together. It
# lives in `fleetforge.telemetry_store` since R4-be-1, because the API validates the
# `?name=` of the series read with it and may not import `fleetforge.ingestor`. `ff_`
# names are reserved for the agent, not invalid, so the server accepts them.
# A payload over this is dropped whole (the device keeps one under 2048 B).
TELEMETRY_MAX_BYTES = 8192
# The retention window (spec/prd.md -> Retention: telemetry 30 days). An older sample could
# never be kept, so it is unusable rather than stored and expired at once.
TELEMETRY_MAX_AGE_MS = int(TELEMETRY_RETENTION.total_seconds() * 1000)


class TelemetrySample(_TolerantPayload):
    """One `up/telemetry` sample. Strict: a sample that fails here is skipped on its own.

    `age_ms` is the device's monotonic time from the sample being taken to the payload
    being built; `clock.device_time` turns it into a timestamp. Absent means 0, negative is
    read as 0.
    """

    name: str
    value: float
    age_ms: int = 0

    @field_validator("name", mode="before")
    @classmethod
    def _metric_name(cls, value: object) -> str:
        if not isinstance(value, str) or TELEMETRY_NAME_RE.fullmatch(value) is None:
            raise ValueError("not a metric name")
        return value

    @field_validator("value", mode="before")
    @classmethod
    def _finite_number(cls, value: object) -> float:
        """A JSON number. `bool` is an `int` in Python and is not one here; `json.loads`
        reads `1e999` as `inf`, and an integer too large for a double overflows."""
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("not a number")
        try:
            number = float(value)
        except OverflowError as exc:
            raise ValueError("not a finite number") from exc
        if not math.isfinite(number):
            raise ValueError("not a finite number")
        return number

    @field_validator("age_ms", mode="before")
    @classmethod
    def _plausible_age(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("age_ms is not an integer")
        if value > TELEMETRY_MAX_AGE_MS:
            raise ValueError("age_ms is beyond the retention window")
        return max(value, 0)


class TelemetryPayload(_TolerantPayload):
    """`up/telemetry` — `{"dropped": n, "samples": [{"age_ms", "name", "value"}, ...]}`.

    **One bad sample never costs the rest.** Each raw sample is validated on its own and
    the unusable ones are counted in `unusable` (set here, never read off the wire), so a
    firmware that reports one bad name still gets its other metrics through. `samples`
    that is not a list is read as none; `dropped` that is not a non-negative integer is
    `None`.
    """

    dropped: int | None = None
    samples: list[TelemetrySample] = Field(default_factory=list)
    unusable: int = 0

    @model_validator(mode="before")
    @classmethod
    def _usable_samples(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data  # not an object: the model refuses it and `decode` returns None
        raw = data.get("samples")
        items = raw if isinstance(raw, list) else []
        samples: list[TelemetrySample] = []
        for item in items:
            try:
                samples.append(TelemetrySample.model_validate(item))
            except ValidationError:
                continue
        return {**data, "samples": samples, "unusable": len(items) - len(samples)}

    @field_validator("dropped", mode="before")
    @classmethod
    def _plausible_dropped(cls, value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value


# `up/log` (R4-be-1 builds the server half of the up/log proposal,
# docs/features/health-telemetry.md -> *up/log proposal*, Patch A owner pending). The
# device keeps a payload under 2048 B; one over this is dropped whole, liveness kept.
LOG_MAX_BYTES = 8192
# The retention window (spec/prd.md -> Retention: device logs 7 days): an older line is
# unusable rather than stored and expired at once.
LOG_MAX_AGE_MS = int(LOG_RETENTION.total_seconds() * 1000)
# The level when absent or unusable (the proposal: "absent means `I`").
LOG_DEFAULT_LEVEL = "I"
LOG_LEVEL_MAX_CHARS = 8


class LogLine(_TolerantPayload):
    """One `up/log` line. A line that fails here is skipped on its own and counted.

    `text` must be a string (anything else is an unusable line); it is stored through
    `deploys.sanitise_device_text`, and a lone surrogate (which `json.loads` produces from
    `"\\ud800"` and PostgreSQL cannot store) is replaced here. `level` is an open
    vocabulary: a printable string of 1-8 characters is kept as sent, anything else is
    `I`. `age_ms` follows `TelemetrySample`'s rule, against the 7-day window.
    """

    text: str
    level: str = LOG_DEFAULT_LEVEL
    age_ms: int = 0

    @field_validator("text", mode="before")
    @classmethod
    def _line_text(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("text is not a string")
        return value.encode("utf-8", "replace").decode("utf-8")

    @field_validator("level", mode="before")
    @classmethod
    def _usable_level(cls, value: object) -> str:
        if (
            isinstance(value, str)
            and 1 <= len(value) <= LOG_LEVEL_MAX_CHARS
            and value.isprintable()
            and not value.isspace()
        ):
            return value
        return LOG_DEFAULT_LEVEL

    @field_validator("age_ms", mode="before")
    @classmethod
    def _plausible_age(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("age_ms is not an integer")
        if value > LOG_MAX_AGE_MS:
            raise ValueError("age_ms is beyond the retention window")
        return max(value, 0)


class LogPayload(_TolerantPayload):
    """`up/log` — `{"dropped": n, "lines": [{"age_ms", "level", "text"}, ...]}`, oldest first.

    The telemetry pair's shape on purpose: **one bad line never costs the rest**, the
    unusable ones are counted in `unusable` (set here, never read off the wire); `lines`
    that is not a list is read as none; `dropped` that is not a non-negative integer is
    `None`.
    """

    dropped: int | None = None
    lines: list[LogLine] = Field(default_factory=list)
    unusable: int = 0

    @model_validator(mode="before")
    @classmethod
    def _usable_lines(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data  # not an object: the model refuses it and `decode` returns None
        raw = data.get("lines")
        items = raw if isinstance(raw, list) else []
        lines: list[LogLine] = []
        for item in items:
            try:
                lines.append(LogLine.model_validate(item))
            except ValidationError:
                continue
        return {**data, "lines": lines, "unusable": len(items) - len(lines)}

    @field_validator("dropped", mode="before")
    @classmethod
    def _plausible_dropped(cls, value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value


# `up/ack` (R5-be-2, R5-spec-1 Patch A5). The agent's answers are well under 256 B (every
# `detail` is at most 63 B); one over this is dropped whole, liveness kept.
ACK_MAX_BYTES = 1024


class AckPayload(_TolerantPayload):
    """`up/ack` — `{"id","type","result","cmd_id","detail"}`, a board's answer to a verb.

    Never a transaction state and never a `deploy_events` row: the ingestor keeps the
    first answer on the `device_commands` row of the command it answers
    (`device_commands.record_answer`), or nothing at all for an id the server never sent.
    Every field is optional and strictly `str | None` except `detail`, which coerces to
    text like `StatusPayload.detail`; what makes an answer usable is decided by the writer.
    """

    id: str | None = None
    type: str | None = None
    result: str | None = None
    cmd_id: str | None = None
    detail: str | None = None

    @field_validator("detail", mode="before")
    @classmethod
    def _detail_as_text(cls, value: object) -> str | None:
        """Anything the board sent, rendered as text. The writer sanitises and truncates."""
        if value is None or isinstance(value, str):
            return value
        return str(value)


def decode[T: _TolerantPayload](model: type[T], topic: str, payload: bytes) -> T | None:
    """Decode `payload` into `model`, or return `None` if it is not usable.

    Malformed JSON, non-UTF-8 bytes, a JSON array where an object belongs and a
    wrong-typed field all land here — none of them may reach the caller as an
    exception. Only the topic and the payload **length** are logged: logs are not a
    data store, and R4 telemetry bodies get large.
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
