"""`up/telemetry` (R4-fw-2): the payload proposal, the firmware ring, the simulator, the ingestor.

The payload is a proposal until the owner applies Patch A (`docs/features/health-telemetry.md`
→ *up/telemetry proposal*); the example tests read the spec's section when it exists and the
proposal's otherwise, so the owner's paste is verified with no follow-up commit. What is
held here, on every `just test`:

* the example: keys `dropped, samples` and `age_ms, name, value`, in that order, oldest
  first, names inside the rule, values finite numbers;
* the firmware: QoS 0 and retain 0, the 64-sample ring, the 900 B payload, the 5 s flush,
  the monotonic clock only, and an `ff_telemetry()` that never logs, allocates or prints
  (R4-fw-3 will forward log lines; a metric call that logs could recurse);
* the session wiring in the CRITICAL `ff_mqtt.c`: two calls and one topic, nowhere near the
  confirm path;
* the public surface: one maker function, one Arduino member, both examples calling it;
* the simulator's ring is the firmware's design, with its constants retyped and tripwired;
* the ingestor skips a bad sample on its own and stamps `received_at - age_ms`.

Comments are stripped before any grep (`_code`), the idiom of the other agent tripwires.
"""

import asyncio
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.clock import device_time
from fleetforge.events import EventType
from fleetforge.ingestor.protocol import (
    TELEMETRY_MAX_AGE_MS,
    TELEMETRY_MAX_BYTES,
    TelemetryPayload,
    decode,
)
from fleetforge.ingestor.protocol import TELEMETRY_NAME_RE as INGESTOR_NAME_RE
from fleetforge.simulator import __main__ as cli
from fleetforge.simulator.device import (
    ANNOUNCE,
    HEARTBEAT,
    TELEMETRY,
    DeviceIdentity,
    LinkProfile,
    run_sleepy,
    up_topic,
)
from fleetforge.simulator.telemetry import (
    TELEMETRY_CAPACITY,
    TELEMETRY_FLUSH_S,
    TELEMETRY_PAYLOAD_MAX,
    TELEMETRY_QOS,
    TelemetryBuffer,
    format_value,
)
from fleetforge.simulator.telemetry import TELEMETRY_NAME_RE as SIMULATOR_NAME_RE
from tests.agent_src import (
    ARDUINO_WRAPPER_CPP,
    ARDUINO_WRAPPER_H,
    COMPONENT_CMAKE,
    COMPONENT_SRC,
    EXAMPLE_INO,
    FF_TELEMETRY_C,
    FF_TELEMETRY_H,
    FF_TELEMETRY_INTERNAL_H,
    IDF_EXAMPLE_MAIN_C,
)
from tests.conftest import capture_logs
from tests.test_agent_txn import _function_body
from tests.test_ff_cfg import _code
from tests.test_ingestor import publish, reload, seed_device
from tests.test_ota_component import _declared_functions
from tests.test_simulator import FakeClient, credential_for, drive_until, session_factory

REPO_ROOT = Path(__file__).resolve().parent.parent
DEVICE_PROTOCOL = REPO_ROOT / "spec" / "device-protocol.md"
FEATURE_DOC = REPO_ROOT / "docs" / "features" / "health-telemetry.md"
SPEC_HEADING = "### `up/telemetry` — user-defined metrics"
PROPOSAL_HEADING = "### up/telemetry proposal (R4-fw-2, proposed)"
FF_MQTT_C = COMPONENT_SRC / "ff_mqtt.c"

TOP_KEYS = ["dropped", "samples"]
SAMPLE_KEYS = ["age_ms", "name", "value"]


def _json_fence_after(text: str, heading: str) -> str:
    """The first ```json fence after `heading`, de-indented (a list item indents it)."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    opening = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "```json")
    closing = next(i for i in range(opening + 1, len(lines)) if lines[i].strip() == "```")
    return "\n".join(line.strip() for line in lines[opening + 1 : closing])


def _spec_has_the_section() -> bool:
    return SPEC_HEADING in DEVICE_PROTOCOL.read_text().splitlines()


def _proposal_example() -> str:
    return _json_fence_after(FEATURE_DOC.read_text(), PROPOSAL_HEADING)


def _telemetry_example() -> str:
    """The spec's example once Patch A is applied, the proposal's until then."""
    if _spec_has_the_section():
        return _json_fence_after(DEVICE_PROTOCOL.read_text(), SPEC_HEADING)
    return _proposal_example()


def _patch_a2_example() -> str:
    """The example inside the proposal's paste-ready A2 block."""
    return _json_fence_after(FEATURE_DOC.read_text(), SPEC_HEADING)


def _parsed_example() -> dict[str, Any]:
    loaded: Any = json.loads(_telemetry_example())
    assert isinstance(loaded, dict)
    return loaded


def _define(name: str) -> int:
    match = re.search(rf"#define {name} (\d+)\b", _code(FF_TELEMETRY_C))
    assert match is not None, name
    return int(match.group(1))


def _critical_sections(code: str) -> list[str]:
    sections = re.findall(
        r"portENTER_CRITICAL\(&s_lock\);(.*?)portEXIT_CRITICAL\(&s_lock\);", code, re.S
    )
    assert sections, "no critical section found"
    return sections


class TestThePayloadExample:
    def test_keys_are_in_order_and_oldest_first(self) -> None:
        example = _parsed_example()
        assert list(example) == TOP_KEYS
        assert example["dropped"] == 0
        samples = example["samples"]
        assert isinstance(samples, list) and samples
        for sample in samples:
            assert list(sample) == SAMPLE_KEYS
            assert INGESTOR_NAME_RE.fullmatch(sample["name"])
            assert not isinstance(sample["value"], bool)
            assert isinstance(sample["value"], int | float)
            assert isinstance(sample["age_ms"], int) and not isinstance(sample["age_ms"], bool)
        ages = [sample["age_ms"] for sample in samples]
        assert ages == sorted(ages, reverse=True), "samples are oldest first"
        assert len(_telemetry_example().encode()) < 2048

    def test_the_proposal_pastes_the_example_it_describes(self) -> None:
        assert _patch_a2_example() == _proposal_example()

    def test_the_spec_section_once_pasted_is_the_proposal(self) -> None:
        """Switches over by itself when the owner applies Patch A."""
        if _spec_has_the_section():
            assert _json_fence_after(DEVICE_PROTOCOL.read_text(), SPEC_HEADING) == (
                _proposal_example()
            )
        else:
            assert "| `up/telemetry` | 0 | no |" in DEVICE_PROTOCOL.read_text()

    def test_the_example_decodes_on_the_server(self) -> None:
        decoded = decode(TelemetryPayload, "t", _telemetry_example().encode())
        assert decoded is not None
        assert decoded.unusable == 0
        assert [sample.name for sample in decoded.samples] == [
            sample["name"] for sample in _parsed_example()["samples"]
        ]


class TestTheFirmwareRing:
    def test_the_component_compiles_it(self) -> None:
        assert '"src/ff_telemetry.c"' in COMPONENT_CMAKE.read_text()

    def test_the_format_strings_emit_the_example_keys_in_order(self) -> None:
        keys = re.findall(r'\\"([a-z_]+)\\":', _code(FF_TELEMETRY_C))
        assert keys == TOP_KEYS + SAMPLE_KEYS

    def test_one_publish_at_qos_0_never_retained(self) -> None:
        calls = re.findall(r"esp_mqtt_client_publish\(([^;]*)\);", _code(FF_TELEMETRY_C))
        assert len(calls) == 1, calls
        args = [arg.strip() for arg in calls[0].split(",")]
        assert args[-2:] == ["0", "0"], "qos 0, retain 0"
        assert "esp_mqtt_client_enqueue" not in _code(FF_TELEMETRY_C)

    def test_the_limits(self) -> None:
        assert _define("FF_TELEMETRY_CAPACITY") == 64
        assert _define("FF_TELEMETRY_PAYLOAD_MAX") <= 960
        assert _define("FF_TELEMETRY_FLUSH_MS") == 5000

    def test_the_name_max_is_the_ingestor_rule(self) -> None:
        """The tripwire: the server's `{0,30}` after the first letter is NAME_MAX - 1."""
        match = re.fullmatch(r"\[a-z\]\[a-z0-9_\]\{0,(\d+)\}", INGESTOR_NAME_RE.pattern)
        assert match is not None, INGESTOR_NAME_RE.pattern
        assert int(match.group(1)) == _define("FF_TELEMETRY_NAME_MAX") - 1 == 30

    def test_the_monotonic_clock_only(self) -> None:
        code = _code(FF_TELEMETRY_C)
        assert "esp_timer_get_time()" in code
        for forbidden in ("time(", "gettimeofday", "sntp", "localtime", "ff_time"):
            assert not re.search(rf"\b{re.escape(forbidden)}", code), forbidden

    def test_a_non_finite_value_and_the_reserved_prefix_are_refused(self) -> None:
        code = _code(FF_TELEMETRY_C)
        body = _function_body(code, "ff_telemetry")
        assert "isfinite(value)" in body
        assert "ESP_ERR_INVALID_ARG" in body
        assert 'strncmp(name, "ff_", 3)' in _function_body(code, "valid_name_len")

    def test_recording_never_logs_allocates_or_prints(self) -> None:
        """The R4-fw-3 recursion guard: a metric call that logs could loop through the log
        forwarder."""
        code = _code(FF_TELEMETRY_C)
        for name in ("ff_telemetry", "valid_name_len"):
            body = _function_body(code, name)
            for forbidden in ("ESP_LOG", "malloc", "calloc", "printf", "free(", "vTaskDelay"):
                assert forbidden not in body, (name, forbidden)

    def test_the_lock_guards_copies_only(self) -> None:
        for section in _critical_sections(_code(FF_TELEMETRY_C)):
            for forbidden in ("printf", "ESP_LOG", "esp_mqtt_", "strtod", "Notify"):
                assert forbidden not in section, forbidden

    def test_a_failed_publish_commits_nothing(self) -> None:
        """Publish, then commit: `s_unsent` moves only after a publish esp-mqtt accepted."""
        body = _function_body(_code(FF_TELEMETRY_C), "publish_one")
        publish_at = body.index("esp_mqtt_client_publish(")
        failure = body.index("if (id < 0)")
        commit = body.index("s_unsent = last_seq + 1")
        assert publish_at < failure < commit
        assert "return false;" in body[failure:commit]


class TestTheSessionWiring:
    def test_the_drain_starts_after_the_announce_and_the_outcome(self) -> None:
        body = _function_body(_code(FF_MQTT_C), "on_connected")
        assert body.count("ff_telemetry_session_up(") == 1
        up = body.index("ff_telemetry_session_up(ctx->client, ctx->topic_telemetry)")
        assert body.index("ctx->topic_announce") < up
        assert body.index("report_txn_on_connect()") < up

    def test_the_disconnect_stops_it(self) -> None:
        code = _code(FF_MQTT_C)
        case = re.search(r"case MQTT_EVENT_DISCONNECTED:(.*?)\bcase\b", code, re.S)
        assert case is not None
        assert "ff_telemetry_session_down();" in case.group(1)

    def test_nothing_near_the_confirm_path(self) -> None:
        code = _code(FF_MQTT_C)
        published = re.search(r"case MQTT_EVENT_PUBLISHED:(.*?)\bcase\b", code, re.S)
        assert published is not None
        assert "ff_telemetry" not in published.group(1)
        for name in ("confirm_this_image", "confirm_timeout_cb", "classify_txn"):
            assert "ff_telemetry" not in _function_body(code, name), name
        assert len(re.findall(r"\bff_telemetry_session_(up|down)\(", code)) == 2

    def test_the_topic_is_spelled_in_ff_mqtt(self) -> None:
        code = _code(FF_MQTT_C)
        assert 'TOPIC_ROOT "/%s/up/telemetry"' in code
        assert "up/telemetry" not in _code(FF_TELEMETRY_C)

    def test_the_hooks_are_private(self) -> None:
        internal = _declared_functions(FF_TELEMETRY_INTERNAL_H)
        assert internal == {"ff_telemetry_session_up", "ff_telemetry_session_down"}
        assert FF_TELEMETRY_INTERNAL_H.parent == COMPONENT_SRC


class TestThePublicSurface:
    def test_the_maker_header_declares_one_function(self) -> None:
        assert _declared_functions(FF_TELEMETRY_H) == {"ff_telemetry"}
        assert "esp_err_t ff_telemetry(const char *name, double value);" in _code(FF_TELEMETRY_H)

    def test_the_arduino_member(self) -> None:
        assert "bool metric(const char *name, double value);" in _code(ARDUINO_WRAPPER_H)
        body = _function_body(_code(ARDUINO_WRAPPER_CPP), "FleetforgeClass::metric")
        assert re.findall(r"\b(ff_[a-z0-9_]+)\s*\(", body) == ["ff_telemetry"]
        assert "== ESP_OK" in body

    def test_both_examples_report_a_metric(self) -> None:
        assert 'Fleetforge.metric("morse_loops"' in _code(EXAMPLE_INO)
        assert 'ff_telemetry("morse_loops"' in _code(IDF_EXAMPLE_MAIN_C)


# ── the simulator ─────────────────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _payload(built: tuple[bytes, int, int] | None) -> dict[str, Any]:
    assert built is not None
    loaded: Any = json.loads(built[0])
    assert isinstance(loaded, dict)
    return loaded


class TestTheSimulatorBuffer:
    def test_ages_are_measured_at_build_oldest_first(self) -> None:
        clock = FakeClock()
        buffer = TelemetryBuffer(clock)
        for at, value in ((0.0, 1), (5.0, 2), (10.0, 3)):
            clock.now = at
            assert buffer.record("sim_counter", value)
        clock.now = 12.0
        built = buffer.next_payload()
        payload = _payload(built)
        assert list(payload) == TOP_KEYS
        assert [list(sample) for sample in payload["samples"]] == [SAMPLE_KEYS] * 3
        assert [sample["age_ms"] for sample in payload["samples"]] == [12000, 7000, 2000]
        assert built is not None and b'"value":1}' in built[0], "an integral value prints as 1"

    def test_overflow_drops_the_oldest_and_counts_them(self) -> None:
        buffer = TelemetryBuffer(FakeClock())
        for value in range(70):
            buffer.record("sim_counter", value)
        assert len(buffer) == TELEMETRY_CAPACITY
        payload = _payload(buffer.next_payload())
        assert payload["dropped"] == 6
        assert payload["samples"][0]["value"] == 6

    def test_an_uncommitted_payload_is_sent_again(self) -> None:
        buffer = TelemetryBuffer(FakeClock())
        buffer.record("a", 1)
        first = buffer.next_payload()
        assert first is not None
        assert buffer.next_payload() == first

    def test_a_commit_keeps_what_came_after_the_build(self) -> None:
        buffer = TelemetryBuffer(FakeClock(), capacity=3)
        for value in range(3):
            buffer.record("a", value)
        built = buffer.next_payload()
        assert built is not None
        _, last_seq, dropped = built
        assert dropped == 0
        buffer.record("a", 999)  # full: displaces 0, which the built payload already holds
        buffer.commit(last_seq, dropped)
        payload = _payload(buffer.next_payload())
        assert [sample["value"] for sample in payload["samples"]] == [999]
        # The named over-count: a sample displaced after it was built into a payload.
        assert payload["dropped"] == 1

    def test_a_burst_splits_in_order_under_the_size_cap(self) -> None:
        buffer = TelemetryBuffer(FakeClock())
        for value in range(TELEMETRY_CAPACITY):
            buffer.record("a_rather_long_metric_name_xyz", value + 0.123456789)
        values: list[float] = []
        payloads = 0
        while (built := buffer.next_payload()) is not None:
            assert len(built[0]) <= TELEMETRY_PAYLOAD_MAX
            values += [sample["value"] for sample in _payload(built)["samples"]]
            buffer.commit(built[1], built[2])
            payloads += 1
        assert payloads > 1
        assert values == [value + 0.123456789 for value in range(TELEMETRY_CAPACITY)]

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("Temp", 1.0),
            ("1temp", 1.0),
            ("temp-c", 1.0),
            ("", 1.0),
            ("a" * 32, 1.0),
            ("ff_heap", 1.0),
            ("temp", float("nan")),
            ("temp", float("inf")),
            ("temp", float("-inf")),
            ("temp", True),
            ("temp", 10**400),
        ],
    )
    def test_what_the_firmware_refuses(self, name: str, value: float) -> None:
        buffer = TelemetryBuffer(FakeClock())
        assert buffer.record(name, value) is False
        assert len(buffer) == 0

    def test_the_longest_valid_name(self) -> None:
        assert TelemetryBuffer(FakeClock()).record("a" * 31, 1.5)

    def test_the_retyped_constants_match(self) -> None:
        assert SIMULATOR_NAME_RE.pattern == INGESTOR_NAME_RE.pattern
        assert _define("FF_TELEMETRY_CAPACITY") == TELEMETRY_CAPACITY
        assert _define("FF_TELEMETRY_PAYLOAD_MAX") == TELEMETRY_PAYLOAD_MAX
        assert _define("FF_TELEMETRY_FLUSH_MS") == int(TELEMETRY_FLUSH_S * 1000)
        assert TELEMETRY_QOS == 0

    @pytest.mark.parametrize(
        ("value", "text"),
        [(42.0, "42"), (3.71, "3.71"), (-0.5, "-0.5"), (0.1 + 0.2, "0.30000000000000004")],
    )
    def test_values_print_like_cjson(self, value: float, text: str) -> None:
        assert format_value(value) == text


async def test_samples_taken_asleep_arrive_on_the_next_wake_with_their_spacing() -> None:
    """The acceptance in miniature: the sampler runs through the sleep, the wake drains."""
    identity = DeviceIdentity(
        device_id="a4cf12b3de90", power_class="sleepy", expected_wake_interval_s=1
    )
    device_id = identity.device_id
    clients, factory = session_factory(device_id)
    stop = asyncio.Event()

    def batches(fake: FakeClient) -> list[tuple[bytes, int, bool]]:
        topic = up_topic(device_id, TELEMETRY)
        return [(payload, qos, retain) for t, payload, qos, retain in fake.published if t == topic]

    await drive_until(
        run_sleepy(
            identity,
            credential_for(device_id),
            client_factory=factory,
            link=LinkProfile(),
            heartbeat_interval_s=10.0,
            wake_interval_s=0.4,
            awake_s=0.1,
            stop=stop,
            telemetry_every_s=0.05,
        ),
        lambda: len(clients) >= 2 and batches(clients[1]) != [],
        stop,
    )

    second = clients[1]
    published = batches(second)
    assert all(qos == 0 and retain is False for _, qos, retain in published)
    samples = [sample for payload, _, _ in published for sample in json.loads(payload)["samples"]]
    assert len(samples) >= 5, "the batch holds what was recorded while asleep"
    assert {sample["name"] for sample in samples} == {"sim_counter"}
    values = [sample["value"] for sample in samples]
    assert values == list(range(int(values[0]), int(values[0]) + len(values)))
    ages = [sample["age_ms"] for sample in samples]
    gaps = [older - newer for older, newer in zip(ages, ages[1:], strict=False)]
    assert all(20 <= gap <= 80 for gap in gaps), gaps
    calls = second.calls
    assert calls.index(f"publish {up_topic(device_id, ANNOUNCE)}") < calls.index(
        f"publish {up_topic(device_id, TELEMETRY)}"
    )


async def test_telemetry_is_off_by_default() -> None:
    identity = DeviceIdentity(
        device_id="a4cf12b3de90", power_class="sleepy", expected_wake_interval_s=1
    )
    clients, factory = session_factory(identity.device_id)
    stop = asyncio.Event()

    await drive_until(
        run_sleepy(
            identity,
            credential_for(identity.device_id),
            client_factory=factory,
            link=LinkProfile(),
            heartbeat_interval_s=0.01,
            wake_interval_s=0.05,
            awake_s=0.05,
            stop=stop,
        ),
        lambda: len(clients) >= 2,
        stop,
    )

    topics = {topic for fake in clients for topic, _, _, _ in fake.published}
    assert up_topic(identity.device_id, HEARTBEAT) in topics
    assert up_topic(identity.device_id, TELEMETRY) not in topics


def test_the_cli_flag() -> None:
    args = cli.build_parser().parse_args(["run", "--telemetry-every", "5"])
    assert args.telemetry_every == 5.0
    assert cli.build_parser().parse_args(["run"]).telemetry_every is None
    assert cli.build_parser().parse_args(["fleet", "--telemetry-every", "2"]).telemetry_every == 2
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["run", "--telemetry-every", "0"])


# ── the ingestor ──────────────────────────────────────────────────────────────────────

NOW = dt.datetime(2026, 9, 8, 12, 0, tzinfo=dt.UTC)


def _decode(body: object) -> TelemetryPayload:
    decoded = decode(TelemetryPayload, "t", json.dumps(body).encode())
    assert decoded is not None
    return decoded


class TestTheIngestorReading:
    def test_device_time_subtracts_the_age(self) -> None:
        assert device_time(NOW, 13600) == NOW - dt.timedelta(milliseconds=13600)
        assert device_time(NOW, 0) == NOW
        assert device_time(NOW, -5) == NOW

    def test_a_bad_sample_is_skipped_on_its_own(self) -> None:
        good = {"age_ms": 100, "name": "battery_v", "value": 3.71}
        bad = [
            {"age_ms": 1, "name": "Battery", "value": 1},
            {"age_ms": 1, "name": "a" * 32, "value": 1},
            {"age_ms": 1, "name": 7, "value": 1},
            {"age_ms": 1, "name": "flag", "value": True},
            {"age_ms": 1, "name": "text", "value": "1"},
            {"age_ms": True, "name": "age_bool", "value": 1},
            {"age_ms": 1.5, "name": "age_float", "value": 1},
            {"age_ms": TELEMETRY_MAX_AGE_MS + 1, "name": "too_old", "value": 1},
            {"name": "no_value"},
            "not an object",
        ]
        decoded = _decode({"dropped": 2, "samples": [good, *bad]})
        assert [sample.name for sample in decoded.samples] == ["battery_v"]
        assert decoded.unusable == len(bad)
        assert decoded.dropped == 2

    def test_json_infinity_is_unusable(self) -> None:
        raw = b'{"samples":[{"age_ms":0,"name":"a","value":1e999},{"name":"b","value":2}]}'
        decoded = decode(TelemetryPayload, "t", raw)
        assert decoded is not None
        assert [sample.name for sample in decoded.samples] == ["b"]
        assert decoded.unusable == 1

    def test_ages_absent_negative_and_at_the_limit(self) -> None:
        decoded = _decode(
            {
                "samples": [
                    {"name": "a", "value": 1},
                    {"age_ms": -40, "name": "b", "value": 2},
                    {"age_ms": TELEMETRY_MAX_AGE_MS, "name": "c", "value": 3},
                ]
            }
        )
        assert [sample.age_ms for sample in decoded.samples] == [0, 0, TELEMETRY_MAX_AGE_MS]

    def test_the_reserved_prefix_is_accepted_server_side(self) -> None:
        """`ff_` is reserved for the agent, not invalid."""
        assert [
            s.name for s in _decode({"samples": [{"name": "ff_heap", "value": 1}]}).samples
        ] == ["ff_heap"]

    @pytest.mark.parametrize("samples", [None, 5, "x", {"name": "a", "value": 1}])
    def test_samples_that_are_not_a_list_are_none(self, samples: object) -> None:
        decoded = _decode({"samples": samples})
        assert decoded.samples == [] and decoded.unusable == 0

    @pytest.mark.parametrize("dropped", [-1, True, "3", 1.5])
    def test_an_implausible_dropped_is_none(self, dropped: object) -> None:
        assert _decode({"dropped": dropped, "samples": []}).dropped is None

    def test_a_wire_unusable_is_ignored(self) -> None:
        assert _decode({"samples": [], "unusable": 99}).unusable == 0


DEVICE_ID = "a4cf12b3de90"


def _summaries(records: list[Any]) -> list[str]:
    return [r.getMessage() for r in records if "telemetry samples=" in r.getMessage()]


async def test_a_live_batch_moves_last_seen_and_is_logged(session: AsyncSession) -> None:
    await seed_device(session)

    with capture_logs() as records:
        event = await publish(session, "telemetry", json.loads(_telemetry_example()))

    assert event is not None and event.type is EventType.DEVICE_SEEN
    assert (await reload(session)).last_seen == event.at
    assert records, "nothing was captured"
    assert _summaries(records) == [
        f"device {DEVICE_ID} telemetry samples=3 dropped=0 unusable=0 oldest_age_ms=13600"
    ]
    stamped = [r.getMessage() for r in records if " metric " in r.getMessage()]
    assert len(stamped) == 3
    assert stamped[0].endswith((event.at - dt.timedelta(milliseconds=13600)).isoformat())


async def test_an_oversized_payload_is_dropped_but_proves_liveness(session: AsyncSession) -> None:
    await seed_device(session)
    raw = b'{"samples":[' + b" " * (TELEMETRY_MAX_BYTES + 800) + b"]}"

    with capture_logs() as records:
        event = await publish(session, "telemetry", raw=raw)

    assert event is not None and event.type is EventType.DEVICE_SEEN
    assert (await reload(session)).last_seen is not None
    assert any("bytes dropped (over 8192)" in r.getMessage() for r in records)
    assert _summaries(records) == []


async def test_undecodable_bytes_still_prove_liveness(session: AsyncSession) -> None:
    await seed_device(session)

    with capture_logs() as records:
        event = await publish(session, "telemetry", raw=b"\xff not json")

    assert event is not None
    assert (await reload(session)).last_seen is not None
    assert records and _summaries(records) == []


async def test_an_unregistered_device_logs_no_summary(session: AsyncSession) -> None:
    with capture_logs() as records:
        event = await publish(session, "telemetry", json.loads(_telemetry_example()))

    assert event is None
    assert records and _summaries(records) == []
