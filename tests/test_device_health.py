"""R4-be-1: the stored reset reason, fault counts, heartbeat health, telemetry and logs.

Driven through `handle_up_message` against the migrated test database (the rolled-back
`session` fixture), the way `test_ingestor.py` drives the rest of the ingest path. What is
held here, on every `just test`:

* `last_reset_reason` is written on every announce, live or retained, absent clearing it;
  a fault row is written on a live fault announce only, and a live heartbeat's
  `uptime_s` deletes the same boot's reconnect repeats without ever touching an earlier
  boot's row;
* the `device.announce` event carries the reset reason, every other type `null`;
* the heartbeat's health is written as a group, malformed fields as NULL, never costing
  the heartbeat;
* telemetry and log batches are stored (log text sanitised and never logged), logs bounded
  on write, everything swept by age, and the read helpers hide expired rows before any
  sweep;
* the retention numbers equal what `spec/prd.md` says (and what the pending patches say).

`NOW` is 2026-09-08, more than 30 days before the suite's real clock, which is why every
retention-aware read here takes `now` (the `progress.py` rule).
"""

import asyncio
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge import device_logs, faults, retention, telemetry_store
from fleetforge.db.models import (
    Device,
    DeviceFault,
    DeviceLog,
    DeviceMetric,
    DeviceTelemetry,
)
from fleetforge.events import DeviceEvent, EventType
from fleetforge.ingestor import main as ingestor_main
from fleetforge.ingestor.protocol import (
    LOG_MAX_AGE_MS,
    LOG_MAX_BYTES,
    TELEMETRY_MAX_AGE_MS,
    HeartbeatPayload,
    LogPayload,
    decode,
)
from tests.conftest import capture_logs
from tests.test_heartbeat_health import spec_hb_example
from tests.test_ingestor import ANNOUNCE, DEVICE_ID, NOW, publish, reload, seed_device
from tests.test_telemetry import _json_fence_after, _telemetry_example

REPO_ROOT = Path(__file__).resolve().parent.parent
DEVICE_PROTOCOL = REPO_ROOT / "spec" / "device-protocol.md"
PRD = REPO_ROOT / "spec" / "prd.md"
FEATURE_DOC = REPO_ROOT / "docs" / "features" / "health-telemetry.md"
LOG_SPEC_HEADING = "### `up/log` — firmware log lines"
OTHER_DEVICE = "a4cf12b3de9a"


def at(seconds: float) -> dt.datetime:
    return NOW + dt.timedelta(seconds=seconds)


async def fault_rows(
    session: AsyncSession, device_id: str = DEVICE_ID
) -> list[tuple[dt.datetime, str]]:
    rows = await session.execute(
        select(DeviceFault.at, DeviceFault.reason)
        .where(DeviceFault.device_id == device_id)
        .order_by(DeviceFault.at, DeviceFault.id)
    )
    return [(row.at, row.reason) for row in rows]


async def log_rows(session: AsyncSession, device_id: str = DEVICE_ID) -> list[DeviceLog]:
    """Oldest first, the insertion order."""
    rows = await session.scalars(
        select(DeviceLog)
        .where(DeviceLog.device_id == device_id)
        .order_by(DeviceLog.logged_at, DeviceLog.id)
        .execution_options(populate_existing=True)
    )
    return list(rows)


async def count(session: AsyncSession, model: Any, device_id: str = DEVICE_ID) -> int:
    value = await session.scalar(
        select(func.count()).select_from(model).where(model.device_id == device_id)
    )
    return int(value or 0)


# ---------------------------------------------------------------------------
# reset_reason is stored on every announce
# ---------------------------------------------------------------------------


async def test_a_live_announce_stores_its_reset_reason(session: AsyncSession) -> None:
    await seed_device(session)
    await publish(session, "announce", {**ANNOUNCE, "reset_reason": "panic"})
    assert (await reload(session)).last_reset_reason == "panic"


async def test_a_retained_announce_stores_it_too(session: AsyncSession) -> None:
    """The retained copy is the newest boot's reason: state, not liveness."""
    await seed_device(session)
    await publish(session, "announce", {**ANNOUNCE, "reset_reason": "sw"}, retained=True)
    device = await reload(session)
    assert device.last_reset_reason == "sw"
    assert device.last_seen is None


async def test_an_absent_reset_reason_clears_a_previous_one(session: AsyncSession) -> None:
    """A board OTA'd back to an agent before 0.5.0 must read "not reported"."""
    await seed_device(session)
    await publish(session, "announce", {**ANNOUNCE, "reset_reason": "panic"})
    await publish(session, "announce", ANNOUNCE, at=at(60))
    assert (await reload(session)).last_reset_reason is None


@pytest.mark.parametrize(
    "value",
    [7, True, ["sw"], {"r": "sw"}, "x" * 17, "sw\nfake log line", None],
)
async def test_a_malformed_reset_reason_is_stored_as_null_and_keeps_the_announce(
    session: AsyncSession, value: object
) -> None:
    await seed_device(session, last_reset_reason="panic")
    event = await publish(
        session, "announce", {**ANNOUNCE, "fw_version": "9.9.9", "reset_reason": value}
    )
    device = await reload(session)
    assert event is not None
    assert device.fw_version == "9.9.9"
    assert device.last_reset_reason is None
    assert await fault_rows(session) == []


# ---------------------------------------------------------------------------
# fault rows: live fault announces only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", faults.FAULT_REASONS)
async def test_a_live_fault_announce_writes_one_fault_row(
    session: AsyncSession, reason: str
) -> None:
    await seed_device(session)
    await publish(session, "announce", {**ANNOUNCE, "reset_reason": reason})
    assert await fault_rows(session) == [(NOW, reason)]


async def test_a_retained_fault_announce_writes_none(session: AsyncSession) -> None:
    """A replay repeats an old boot's reason on every ingestor reconnect."""
    await seed_device(session)
    await publish(session, "announce", {**ANNOUNCE, "reset_reason": "panic"}, retained=True)
    assert await fault_rows(session) == []


@pytest.mark.parametrize("reason", ["poweron", "sw", "new_in_idf_6", "unknown"])
async def test_a_non_fault_reason_writes_none(session: AsyncSession, reason: str) -> None:
    await seed_device(session)
    await publish(session, "announce", {**ANNOUNCE, "reset_reason": reason})
    assert (await reload(session)).last_reset_reason == reason
    assert await fault_rows(session) == []


async def test_an_unregistered_device_writes_no_fault(session: AsyncSession) -> None:
    event = await publish(session, "announce", {**ANNOUNCE, "reset_reason": "panic"})
    assert event is None
    assert await session.scalar(select(func.count()).select_from(DeviceFault)) == 0


async def test_fault_rows_are_trimmed_per_device(session: AsyncSession) -> None:
    await seed_device(session)
    await seed_device(session, OTHER_DEVICE)
    await faults.record_fault(session, device_id=OTHER_DEVICE, reason="panic", at=NOW)
    for i in range(5):
        await faults.record_fault(
            session, device_id=DEVICE_ID, reason="panic", at=at(i * 10), max_rows=3
        )
    assert [row_at for row_at, _ in await fault_rows(session)] == [at(20), at(30), at(40)]
    assert len(await fault_rows(session, OTHER_DEVICE)) == 1


# ---------------------------------------------------------------------------
# the reconnect dedupe (fleetforge.faults docstring has the safety argument)
# ---------------------------------------------------------------------------


async def announce_panic(session: AsyncSession, seconds: float) -> None:
    await publish(session, "announce", {**ANNOUNCE, "reset_reason": "panic"}, at=at(seconds))


async def heartbeat(session: AsyncSession, seconds: float, **fields: object) -> None:
    await publish(session, "hb", {"fw_version": "1.4.2", **fields}, at=at(seconds))


async def test_a_reconnect_inside_one_boot_is_not_a_second_panic(session: AsyncSession) -> None:
    await seed_device(session)
    await announce_panic(session, 0)
    await announce_panic(session, 300)  # a Wi-Fi blip: same boot, re-announced
    await heartbeat(session, 360, uptime_s=400)
    assert await fault_rows(session) == [(NOW, "panic")]


async def test_a_first_announce_inside_the_uptime_rounding_still_dates_the_boot(
    session: AsyncSession,
) -> None:
    """Found on the dev stack (R4-be-1 T2): the simulator announces ~0.1 s after its uptime
    origin, and `uptime_s` truncates, so `received_at - uptime_s` landed 80 ms after the
    boot's first announce and the reconnect row was kept instead."""
    await seed_device(session)
    await announce_panic(session, 0.05)  # the boot's announce, 50 ms after uptime 0
    await announce_panic(session, 53)  # broker restart: same boot
    await heartbeat(session, 68.13, uptime_s=68)  # true uptime 68.13 -> floor T+0.13 - 1 s
    assert await fault_rows(session) == [(at(0.05), "panic")]


async def test_a_crash_loop_counts_every_boot(session: AsyncSession) -> None:
    """Boots that die before a heartbeat: the floor is after both, nothing is deleted."""
    await seed_device(session)
    await announce_panic(session, 0)
    await announce_panic(session, 40)
    await heartbeat(session, 50, uptime_s=8)
    assert len(await fault_rows(session)) == 2


async def test_a_queued_heartbeat_leaves_one_extra_row_until_a_fresh_one(
    session: AsyncSession,
) -> None:
    await seed_device(session)
    await announce_panic(session, 0)
    await announce_panic(session, 1000)  # reconnect
    await heartbeat(session, 1000, uptime_s=900)  # queued: floor T+100, sees only one
    assert len(await fault_rows(session)) == 2
    await heartbeat(session, 1060, uptime_s=1060)  # fresh: floor T, sees both
    assert await fault_rows(session) == [(NOW, "panic")]


async def test_an_earlier_boots_row_is_never_deleted(session: AsyncSession) -> None:
    await seed_device(session)
    await announce_panic(session, 0)  # boot 1
    await announce_panic(session, 600)  # boot 2
    await announce_panic(session, 630)  # boot 2 reconnect
    await heartbeat(session, 660, uptime_s=60)  # floor T+600
    assert await fault_rows(session) == [(at(0), "panic"), (at(600), "panic")]


@pytest.mark.parametrize("fields", [{}, {"uptime_s": "x"}, {"uptime_s": True}, {"uptime_s": -1}])
async def test_a_heartbeat_without_a_usable_uptime_deletes_nothing(
    session: AsyncSession, fields: dict[str, object]
) -> None:
    await seed_device(session)
    await announce_panic(session, 0)
    await announce_panic(session, 300)
    await heartbeat(session, 360, **fields)
    assert len(await fault_rows(session)) == 2


async def test_a_failed_fault_write_never_costs_the_announce(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_args: object, **_kwargs: object) -> bool:
        raise SQLAlchemyError("simulated")

    monkeypatch.setattr(faults, "record_fault", broken)
    await seed_device(session)
    with capture_logs() as records:
        event = await publish(session, "announce", {**ANNOUNCE, "reset_reason": "panic"})
    assert event is not None and event.reset_reason == "panic"
    device = await reload(session)
    assert (device.last_seen, device.last_reset_reason) == (NOW, "panic")
    assert any("fault bookkeeping failed; announce kept" in r.getMessage() for r in records)


# ---------------------------------------------------------------------------
# the SSE envelope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("retained", [False, True], ids=["live", "retained"])
async def test_the_announce_event_carries_the_reset_reason(
    session: AsyncSession, retained: bool
) -> None:
    await seed_device(session)
    event = await publish(
        session, "announce", {**ANNOUNCE, "reset_reason": "panic"}, retained=retained
    )
    assert event is not None and event.type is EventType.DEVICE_ANNOUNCE
    assert event.reset_reason == "panic"
    assert json.loads(event.model_dump_json())["reset_reason"] == "panic"


async def test_every_other_event_sends_null(session: AsyncSession) -> None:
    await seed_device(session, last_reset_reason="panic")
    for channel, body in [("hb", {}), ("presence", {"online": True}), ("log", {"lines": []})]:
        event = await publish(session, channel, body)
        assert event is not None
        assert event.reset_reason is None, channel
        assert json.loads(event.model_dump_json())["reset_reason"] is None


def test_an_envelope_without_the_field_still_validates() -> None:
    """An older ingestor's frame, read by a newer api."""
    old = {"v": 1, "type": "device.heartbeat", "device_id": DEVICE_ID, "at": NOW.isoformat()}
    assert DeviceEvent.model_validate({**old, "online": True}).reset_reason is None


# ---------------------------------------------------------------------------
# heartbeat health
# ---------------------------------------------------------------------------


def health(device: Device) -> tuple[object, ...]:
    return (
        device.uptime_s,
        device.rssi,
        device.free_heap,
        device.min_free_heap,
        device.boot_ok,
    )


async def test_the_spec_heartbeat_lands_verbatim(session: AsyncSession) -> None:
    example = spec_hb_example()
    await seed_device(session)
    await publish(session, "hb", example)
    device = await reload(session)
    assert device.health_at == NOW
    assert health(device) == (
        example["uptime_s"],
        example["rssi"],
        example["free_heap"],
        example["min_free_heap"],
        example["boot_ok"],
    )
    assert device.fw_version == example["fw_version"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("free_heap", True),
        ("uptime_s", "x"),
        ("free_heap", -5),
        ("boot_ok", 1),
        ("rssi", 5),
        ("uptime_s", 10**12),
        ("min_free_heap", 2**41),
    ],
)
async def test_a_malformed_health_field_is_null_and_keeps_the_heartbeat(
    session: AsyncSession, field: str, value: object
) -> None:
    body = {**spec_hb_example(), "fw_version": "9.9.9", field: value}
    await seed_device(session)
    event = await publish(session, "hb", body)
    device = await reload(session)
    assert event is not None
    assert (device.fw_version, device.last_seen, device.health_at) == ("9.9.9", NOW, NOW)
    assert getattr(device, field) is None
    others = {"uptime_s", "rssi", "free_heap", "min_free_heap", "boot_ok"} - {field}
    assert all(getattr(device, name) is not None for name in others)


async def test_each_heartbeat_is_the_whole_report(session: AsyncSession) -> None:
    """An agent before 0.5.0 sends no `min_free_heap`: NULL, not the previous board's value."""
    await seed_device(session)
    await publish(session, "hb", spec_hb_example())
    old_agent = {k: v for k, v in spec_hb_example().items() if k != "min_free_heap"}
    await publish(session, "hb", {**old_agent, "rssi": None}, at=at(60))
    device = await reload(session)
    assert device.health_at == at(60)
    assert (device.min_free_heap, device.rssi) == (None, None)
    assert device.free_heap == spec_hb_example()["free_heap"]


def test_the_heartbeat_model_never_raises_on_health() -> None:
    raw = b'{"uptime_s":[1],"rssi":{"a":1},"free_heap":1.5,"boot_ok":"true"}'
    decoded = decode(HeartbeatPayload, "t", raw)
    assert decoded is not None
    assert (decoded.uptime_s, decoded.rssi, decoded.free_heap, decoded.boot_ok) == (
        None,
        None,
        None,
        None,
    )


# ---------------------------------------------------------------------------
# telemetry storage
# ---------------------------------------------------------------------------


async def telemetry_rows(
    session: AsyncSession, device_id: str = DEVICE_ID
) -> list[tuple[str, float, dt.datetime]]:
    rows = await session.execute(
        select(DeviceTelemetry.name, DeviceTelemetry.value, DeviceTelemetry.sample_at)
        .where(DeviceTelemetry.device_id == device_id)
        .order_by(DeviceTelemetry.id)
    )
    return [(row.name, row.value, row.sample_at) for row in rows]


async def metric_rows(session: AsyncSession) -> dict[str, tuple[float, dt.datetime]]:
    rows = await session.execute(
        select(DeviceMetric.name, DeviceMetric.value, DeviceMetric.sample_at).where(
            DeviceMetric.device_id == DEVICE_ID
        )
    )
    return {row.name: (row.value, row.sample_at) for row in rows}


def ms(milliseconds: int) -> dt.timedelta:
    return dt.timedelta(milliseconds=milliseconds)


async def test_the_example_batch_is_stored_with_device_relative_times(
    session: AsyncSession,
) -> None:
    await seed_device(session)
    await publish(session, "telemetry", json.loads(_telemetry_example()))
    assert await telemetry_rows(session) == [
        ("morse_loops", 41.0, NOW - ms(13600)),
        ("morse_loops", 42.0, NOW - ms(6800)),
        ("battery_v", 3.71, NOW),
    ]
    assert await metric_rows(session) == {
        "morse_loops": (42.0, NOW - ms(6800)),
        "battery_v": (3.71, NOW),
    }


async def test_a_backlog_never_overwrites_a_newer_latest_value(session: AsyncSession) -> None:
    await seed_device(session)
    await publish(session, "telemetry", json.loads(_telemetry_example()))
    stale = {"samples": [{"age_ms": 60_000, "name": "battery_v", "value": 1.0}]}
    await publish(session, "telemetry", stale, at=at(10))
    assert (await metric_rows(session))["battery_v"] == (3.71, NOW)
    fresh = {"samples": [{"age_ms": 0, "name": "battery_v", "value": 3.6}]}
    await publish(session, "telemetry", fresh, at=at(20))
    assert (await metric_rows(session))["battery_v"] == (3.6, at(20))
    assert len(await telemetry_rows(session)) == 5


async def test_unusable_samples_are_skipped_on_their_own(session: AsyncSession) -> None:
    await seed_device(session)
    body = {
        "samples": [
            {"name": "Bad", "value": 1},
            {"name": "ok", "value": "x"},
            {"name": "ok", "value": 2},
        ]
    }
    await publish(session, "telemetry", body)
    assert await telemetry_rows(session) == [("ok", 2.0, NOW)]


@pytest.mark.parametrize(
    ("raw", "retained", "registered"),
    [
        (b'{"samples":[' + b" " * 9000 + b"]}", False, True),
        (b"\xff not json", False, True),
        (None, True, True),
        (None, False, False),
    ],
    ids=["oversized", "undecodable", "retained", "unregistered"],
)
async def test_telemetry_that_must_not_be_stored_is_not(
    session: AsyncSession, raw: bytes | None, retained: bool, registered: bool
) -> None:
    if registered:
        await seed_device(session)
    body = json.loads(_telemetry_example())
    await publish(session, "telemetry", body, raw=raw, retained=retained)
    assert await session.scalar(select(func.count()).select_from(DeviceTelemetry)) == 0
    assert await session.scalar(select(func.count()).select_from(DeviceMetric)) == 0


def test_the_telemetry_age_limit_is_the_retention_window() -> None:
    assert dt.timedelta(milliseconds=TELEMETRY_MAX_AGE_MS) == retention.TELEMETRY_RETENTION
    assert dt.timedelta(milliseconds=LOG_MAX_AGE_MS) == retention.LOG_RETENTION


# ---------------------------------------------------------------------------
# log storage
# ---------------------------------------------------------------------------


def log_example() -> str:
    """The spec's `up/log` example once Patch A is applied, the proposal's A2 copy until then."""
    if LOG_SPEC_HEADING in DEVICE_PROTOCOL.read_text().splitlines():
        return _json_fence_after(DEVICE_PROTOCOL.read_text(), LOG_SPEC_HEADING)
    return _json_fence_after(FEATURE_DOC.read_text(), LOG_SPEC_HEADING)


def log_summaries(records: list[Any]) -> list[str]:
    return [r.getMessage() for r in records if " log lines=" in r.getMessage()]


async def test_the_example_batch_is_stored_oldest_first(session: AsyncSession) -> None:
    example = json.loads(log_example())
    await seed_device(session)
    with capture_logs() as records:
        event = await publish(session, "log", example)
    rows = await log_rows(session)
    assert event is not None and event.type is EventType.DEVICE_SEEN
    assert [(r.level, r.text, r.logged_at, r.dropped) for r in rows] == [
        (line["level"], line["text"], NOW - ms(line["age_ms"]), 0) for line in example["lines"]
    ]
    assert rows[0].id < rows[1].id
    assert {r.received_at for r in rows} == {NOW}
    assert log_summaries(records) == [f"device {DEVICE_ID} log lines=2 dropped=0 unusable=0"]


async def test_log_text_is_sanitised_bounded_and_never_logged(session: AsyncSession) -> None:
    secret = "a\nb\x1b[31m https://x.y/sig?=s"
    body = {"lines": [{"text": secret, "level": "E"}, {"text": "Z" * 300}]}
    await seed_device(session)
    with capture_logs() as records:
        await publish(session, "log", body)
    rows = await log_rows(session)
    assert rows[0].text == "ab[31m <url>"
    assert rows[1].text == "Z" * 200
    assert records, "nothing was captured"
    every_message = "\n".join(r.getMessage() for r in records)
    for fragment in ("x.y/sig", "[31m", "ZZZZ"):
        assert fragment not in every_message


@pytest.mark.parametrize(
    ("line", "level"),
    [
        ({"text": "t"}, "I"),
        ({"text": "t", "level": 5}, "I"),
        ({"text": "t", "level": "TOOLONGLEVEL"}, "I"),
        ({"text": "t", "level": "\n"}, "I"),
        ({"text": "t", "level": "W"}, "W"),
        ({"text": "t", "level": "warn"}, "warn"),
    ],
)
async def test_the_level_is_kept_as_sent_or_defaults_to_i(
    session: AsyncSession, line: dict[str, object], level: str
) -> None:
    await seed_device(session)
    await publish(session, "log", {"lines": [line]})
    assert [r.level for r in await log_rows(session)] == [level]


async def test_unusable_lines_are_counted_into_the_first_stored_lines_gap(
    session: AsyncSession,
) -> None:
    body = {
        "dropped": 3,
        "lines": [
            {"text": 7},
            {"text": "kept", "age_ms": 10},
            {"text": "x", "age_ms": "x"},
            {"text": "last"},
        ],
    }
    await seed_device(session)
    with capture_logs() as records:
        await publish(session, "log", body)
    rows = await log_rows(session)
    assert [(r.text, r.dropped) for r in rows] == [("kept", 5), ("last", 0)]
    assert log_summaries(records) == [f"device {DEVICE_ID} log lines=2 dropped=3 unusable=2"]


async def test_a_gap_with_no_line_is_one_marker_row(session: AsyncSession) -> None:
    await seed_device(session)
    await publish(session, "log", {"dropped": 4, "lines": []})
    rows = await log_rows(session)
    assert [(r.text, r.dropped, r.level, r.logged_at) for r in rows] == [(None, 4, "I", NOW)]


async def test_a_line_older_than_the_window_is_unusable(session: AsyncSession) -> None:
    await seed_device(session)
    await publish(session, "log", {"lines": [{"text": "old", "age_ms": LOG_MAX_AGE_MS + 1}]})
    assert [(r.text, r.dropped) for r in await log_rows(session)] == [(None, 1)]


@pytest.mark.parametrize("body", [{"lines": []}, {}, {"dropped": 0}, {"lines": "x"}])
async def test_a_batch_with_nothing_in_it_stores_nothing(
    session: AsyncSession, body: dict[str, object]
) -> None:
    await seed_device(session)
    event = await publish(session, "log", body)
    assert event is not None
    assert await log_rows(session) == []


async def test_an_oversized_log_payload_is_dropped_but_proves_liveness(
    session: AsyncSession,
) -> None:
    raw = json.dumps({"lines": [{"text": "x" * 100}] * 100}).encode()
    assert len(raw) > LOG_MAX_BYTES
    await seed_device(session)
    with capture_logs() as records:
        event = await publish(session, "log", raw=raw)
    assert event is not None
    assert (await reload(session)).last_seen == NOW
    assert await log_rows(session) == []
    assert any("log payload of" in r.getMessage() for r in records)


async def test_retained_or_unregistered_log_lines_are_not_stored(session: AsyncSession) -> None:
    example = json.loads(log_example())
    assert await publish(session, "log", example) is None  # unregistered
    await seed_device(session)
    assert await publish(session, "log", example, retained=True) is None
    assert await session.scalar(select(func.count()).select_from(DeviceLog)) == 0


async def test_a_failed_log_write_never_costs_liveness(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_args: object, **_kwargs: object) -> int:
        raise SQLAlchemyError("simulated")

    monkeypatch.setattr(device_logs, "record_log_batch", broken)
    await seed_device(session)
    with capture_logs() as records:
        event = await publish(session, "log", json.loads(log_example()))
    assert event is not None
    assert (await reload(session)).last_seen == NOW
    assert any("log storage failed; liveness kept" in r.getMessage() for r in records)


# ---------------------------------------------------------------------------
# bound on write
# ---------------------------------------------------------------------------


def batch(texts: list[str]) -> LogPayload:
    lines = [{"text": t, "age_ms": (len(texts) - 1 - i) * 1000} for i, t in enumerate(texts)]
    decoded = decode(LogPayload, "t", json.dumps({"lines": lines}).encode())
    assert decoded is not None
    return decoded


async def test_three_batches_of_six_keep_the_newest_ten(session: AsyncSession) -> None:
    await seed_device(session)
    for k in range(3):
        await device_logs.record_log_batch(
            session,
            device_id=DEVICE_ID,
            payload=batch([f"b{k}-l{i}" for i in range(6)]),
            received_at=at(k * 10),
            max_lines=10,
        )
    texts = [r.text for r in await log_rows(session)]
    assert texts == [f"b1-l{i}" for i in range(2, 6)] + [f"b2-l{i}" for i in range(6)]


async def test_the_real_bound_is_five_thousand_lines(session: AsyncSession) -> None:
    await seed_device(session)
    await seed_device(session, OTHER_DEVICE)
    await session.execute(
        text(
            "INSERT INTO device_logs (device_id, logged_at, received_at, level, text) "
            "SELECT :d, CAST(:start AS timestamptz) + make_interval(secs => i), :start, 'I', 'bulk-' || i "
            "FROM generate_series(1, :n) AS i"
        ),
        {
            "d": DEVICE_ID,
            "start": NOW - dt.timedelta(hours=3),
            "n": retention.LOG_MAX_LINES_PER_DEVICE,
        },
    )
    await session.execute(
        text(
            "INSERT INTO device_logs (device_id, logged_at, received_at, level, text) "
            "VALUES (:d, :t, :t, 'I', 'other'), (:d, :t, :t, 'I', 'other')"
        ),
        {"d": OTHER_DEVICE, "t": NOW - dt.timedelta(days=1)},
    )
    await publish(session, "log", {"lines": [{"text": "new-1", "age_ms": 1000}, {"text": "new-2"}]})
    texts = [r.text for r in await log_rows(session)]
    assert len(texts) == retention.LOG_MAX_LINES_PER_DEVICE
    assert texts[-2:] == ["new-1", "new-2"]
    assert texts[0] == "bulk-3"
    assert await count(session, DeviceLog, OTHER_DEVICE) == 2


# ---------------------------------------------------------------------------
# retention: the sweep, the read predicates and the loop
# ---------------------------------------------------------------------------


async def seed_aged_rows(session: AsyncSession) -> None:
    await seed_device(session)
    await session.execute(
        text(
            "INSERT INTO device_logs (device_id, logged_at, received_at, level, text) VALUES "
            "(:d, :expired, :now, 'I', 'expired'), (:d, :kept, :now, 'I', 'kept')"
        ),
        {
            "d": DEVICE_ID,
            "now": NOW,
            "expired": NOW - dt.timedelta(days=7, seconds=1),
            "kept": NOW - dt.timedelta(days=7) + dt.timedelta(minutes=1),
        },
    )
    await session.execute(
        text(
            "INSERT INTO device_telemetry (device_id, name, value, sample_at) VALUES "
            "(:d, 'm', 1, :expired), (:d, 'm', 2, :kept)"
        ),
        {
            "d": DEVICE_ID,
            "expired": NOW - dt.timedelta(days=30, seconds=1),
            "kept": NOW - dt.timedelta(days=29),
        },
    )
    await session.execute(
        text(
            "INSERT INTO device_metrics (device_id, name, value, sample_at) VALUES "
            "(:d, 'stale', 1, :expired), (:d, 'fresh', 2, :kept)"
        ),
        {
            "d": DEVICE_ID,
            "expired": NOW - dt.timedelta(days=31),
            "kept": NOW - dt.timedelta(days=1),
        },
    )
    await session.execute(
        text(
            "INSERT INTO device_faults (device_id, at, reason) VALUES "
            "(:d, :expired, 'panic'), (:d, :kept, 'panic')"
        ),
        {
            "d": DEVICE_ID,
            "expired": NOW - dt.timedelta(days=31),
            "kept": NOW - dt.timedelta(days=2),
        },
    )


async def test_the_sweep_deletes_exactly_what_is_past_its_window(session: AsyncSession) -> None:
    await seed_aged_rows(session)
    result = await retention.sweep(session, now=NOW)
    assert (result.logs, result.telemetry_expired, result.metrics, result.faults) == (1, 1, 1, 1)
    assert result.total == 4
    assert [r.text for r in await log_rows(session)] == ["kept"]
    assert [value for _, value, _ in await telemetry_rows(session)] == [2.0]
    assert set(await metric_rows(session)) == {"fresh"}
    assert len(await fault_rows(session)) == 1


async def test_the_sweep_caps_each_devices_telemetry(session: AsyncSession) -> None:
    await seed_device(session)
    await seed_device(session, OTHER_DEVICE)
    for device_id, n in ((DEVICE_ID, 8), (OTHER_DEVICE, 3)):
        await session.execute(
            text(
                "INSERT INTO device_telemetry (device_id, name, value, sample_at) "
                "SELECT :d, 'm', i, CAST(:now AS timestamptz) - make_interval(mins => i) "
                "FROM generate_series(1, :n) i"
            ),
            {"d": device_id, "now": NOW, "n": n},
        )
    result = await retention.sweep(session, now=NOW, telemetry_cap=5)
    assert result.telemetry_capped == 3
    assert sorted(value for _, value, _ in await telemetry_rows(session)) == [1, 2, 3, 4, 5]
    assert await count(session, DeviceTelemetry, OTHER_DEVICE) == 3


async def test_reads_hide_expired_rows_before_any_sweep(session: AsyncSession) -> None:
    await seed_device(session)
    await publish(session, "log", {"lines": [{"text": "hello"}]})
    await publish(session, "telemetry", {"samples": [{"name": "m", "value": 1}]})

    def tail(now: dt.datetime) -> Any:
        return device_logs.read_tail(session, device_id=DEVICE_ID, now=now, limit=10)

    assert [line.text for line in await tail(at(86_400))] == ["hello"]
    assert await tail(NOW + dt.timedelta(days=8)) == []
    series = await telemetry_store.read_series(
        session, device_id=DEVICE_ID, name="m", now=NOW + dt.timedelta(days=31), limit=10
    )
    assert series == []
    later = NOW + dt.timedelta(days=31)
    assert await telemetry_store.latest_metrics(session, device_ids=[DEVICE_ID], now=later) == {}
    assert DEVICE_ID in await telemetry_store.latest_metrics(
        session, device_ids=[DEVICE_ID], now=NOW
    )


async def test_the_read_helpers_are_newest_first_and_limited(session: AsyncSession) -> None:
    await seed_device(session)
    for i in range(4):
        await publish(session, "log", {"lines": [{"text": f"l{i}"}]}, at=at(i))
        await publish(session, "telemetry", {"samples": [{"name": "m", "value": i}]}, at=at(i))
    tail = await device_logs.read_tail(session, device_id=DEVICE_ID, now=at(10), limit=3)
    assert [line.text for line in tail] == ["l3", "l2", "l1"]
    series = await telemetry_store.read_series(
        session, device_id=DEVICE_ID, name="m", now=at(10), limit=2
    )
    assert [(p.at, p.value) for p in series] == [(at(3), 3.0), (at(2), 2.0)]


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None


async def test_the_retention_loop_survives_a_failed_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dt.datetime] = []

    async def flaky(_session: object, *, now: dt.datetime) -> retention.SweepResult:
        calls.append(now)
        if len(calls) == 1:
            raise SQLAlchemyError("database went away")
        return retention.SweepResult(logs=2)

    monkeypatch.setattr(ingestor_main.retention, "sweep", flaky)
    with capture_logs() as records:
        task = asyncio.create_task(
            ingestor_main.retention_loop(_FakeSession, interval_s=0.01)  # type: ignore[arg-type]
        )
        for _ in range(200):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    messages = [r.getMessage() for r in records]
    assert len(calls) >= 2
    assert "retention sweep failed: database went away" in messages
    assert any(m.startswith("retention sweep deleted logs=2 ") for m in messages)


async def test_the_first_sweep_is_always_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    async def empty(_session: object, *, now: dt.datetime) -> retention.SweepResult:
        return retention.SweepResult()

    monkeypatch.setattr(ingestor_main.retention, "sweep", empty)
    with capture_logs() as records:
        await ingestor_main.sweep_once(_FakeSession, always_log=True)  # type: ignore[arg-type]
        await ingestor_main.sweep_once(_FakeSession)  # type: ignore[arg-type]
    swept = [r.getMessage() for r in records if "retention sweep deleted" in r.getMessage()]
    assert swept == [
        "retention sweep deleted logs=0 telemetry_expired=0 telemetry_capped=0 metrics=0 faults=0"
    ]


# ---------------------------------------------------------------------------
# the numbers are the spec's (and the pending patches')
# ---------------------------------------------------------------------------


def _retention_row(text_: str, label: str) -> str:
    row = next((line for line in text_.splitlines() if line.startswith(f"| {label}")), None)
    assert row is not None, label
    return row


def _days(row: str) -> dt.timedelta:
    match = re.search(r"\*\*(\d+) days\*\*", row)
    assert match is not None, row
    return dt.timedelta(days=int(match.group(1)))


def _bound(row: str) -> int:
    match = re.search(r"newest \*\*([\d ]+?)(?: lines)?\*\*", row)
    assert match is not None, row
    return int(match.group(1).replace(" ", ""))


def test_the_retention_windows_are_the_prds() -> None:
    prd = PRD.read_text()
    assert _days(_retention_row(prd, "Telemetry samples")) == retention.TELEMETRY_RETENTION
    assert _days(_retention_row(prd, "Device logs")) == retention.LOG_RETENTION


def test_the_per_device_bounds_are_the_prds_or_the_pending_patches() -> None:
    """Read from `spec/prd.md` once the owner applies the patch, from the proposal until then."""
    prd, proposals = PRD.read_text(), FEATURE_DOC.read_text()
    for label, constant in (
        ("Device logs", retention.LOG_MAX_LINES_PER_DEVICE),
        ("Telemetry samples", retention.TELEMETRY_MAX_SAMPLES_PER_DEVICE),
    ):
        row = _retention_row(prd, label)
        if "newest" not in row:
            proposed = [line.strip() for line in proposals.splitlines()]
            row = next(
                line for line in proposed if line.startswith(f"| {label}") and "newest" in line
            )
        assert _bound(row) == constant, label
