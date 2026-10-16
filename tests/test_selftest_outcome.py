"""R5-be-1: the self-test outcome the server reads out of a deploy's `detail`.

Three groups: the grammar (R5-spec-1 D11), parity with the firmware's own `#define`s
(so the server and `ff_mqtt.c` cannot drift apart silently), and the state-consistency
rule (the grammar counts only on the state it belongs to).
"""

import re
from pathlib import Path

import pytest

from fleetforge.db.models import DeployState
from fleetforge.selftest_outcome import (
    SelfTestOutcome,
    parse_selftest_detail,
    selftest_outcome,
)
from tests.agent_src import COMPONENT_SRC

FF_MQTT_C = COMPONENT_SRC / "ff_mqtt.c"
FF_MQTT_INTERNAL_H = COMPONENT_SRC / "ff_mqtt_internal.h"

PASSED = SelfTestOutcome("passed", None, None)
REAL_FAILED_DETAIL = "selftest failed: i2c sensor 0x48 did not ans; returned to ota_1"


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("selftest passed", PASSED),
        (
            REAL_FAILED_DETAIL,
            SelfTestOutcome("failed", "i2c sensor 0x48 did not ans", "ota_1"),
        ),
        ("selftest timed out; returned to ota_1", SelfTestOutcome("timed_out", None, "ota_1")),
        ("selftest timed out", SelfTestOutcome("timed_out", None, None)),
        ("selftest failed: ; returned to ota_0", SelfTestOutcome("failed", None, "ota_0")),
        (
            "selftest failed: no reason given; returned to ota_0",
            SelfTestOutcome("failed", "no reason given", "ota_0"),
        ),
        ("selftest failed: boom", SelfTestOutcome("failed", "boom", None)),
        ("selftest passed; returned to ota_0", SelfTestOutcome("passed", None, "ota_0")),
    ],
)
def test_the_grammar_parses(detail: str, expected: SelfTestOutcome) -> None:
    assert parse_selftest_detail(detail) == expected


@pytest.mark.parametrize(
    "detail",
    [
        None,
        "",
        "selftest passed\n",
        "xselftest passed",
        "selftest passed ",
        "selftest exploded",
        "selftest failed: a; b; returned to ota_0",
        "selftest timed out; returned to ",
        "selftest timed out; returned to ota 1",
        "Selftest passed",
        "selftest failed:",
        "self-test passed",
    ],
)
def test_anything_else_is_not_a_selftest_outcome(detail: str | None) -> None:
    assert parse_selftest_detail(detail) is None


def _mqtt_define(name: str, path: Path = FF_MQTT_C) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"^#define {name} (.+)$", text, re.M)
    assert match is not None, name
    return match.group(1).strip()


def _string(name: str, path: Path = FF_MQTT_C) -> str:
    value = _mqtt_define(name, path)
    match = re.fullmatch(r'"(.*)"', value)
    assert match is not None, f"{name} is not a plain string literal: {value}"
    return match.group(1)


class TestFirmwareParity:
    """The strings the agent composes (`ff_mqtt.c`) must be the ones the server reads."""

    def test_the_composed_details_parse(self) -> None:
        passed = _string("DETAIL_SELFTEST_PASSED")
        failed = _string("DETAIL_SELFTEST_FAILED")
        timed_out = _string("DETAIL_SELFTEST_TIMED_OUT")
        returned = _string("DETAIL_RETURNED_TO")
        detail_max = int(_mqtt_define("DETAIL_MAX"))
        reason = "x" * 27

        assert parse_selftest_detail(passed) == PASSED
        composed = f"{failed}{reason}{returned}ota_1"
        assert len(composed.encode()) <= detail_max
        assert parse_selftest_detail(composed) == SelfTestOutcome("failed", reason, "ota_1")
        assert parse_selftest_detail(f"{timed_out}{returned}ota_0") == SelfTestOutcome(
            "timed_out", None, "ota_0"
        )
        assert parse_selftest_detail(timed_out) == SelfTestOutcome("timed_out", None, None)

    def test_the_no_reason_fallback_parses_as_a_reason(self) -> None:
        detail = f"{_string('DETAIL_SELFTEST_FAILED')}{_string('DETAIL_NO_REASON')}"
        assert parse_selftest_detail(detail) == SelfTestOutcome("failed", "no reason given", None)

    def test_the_details_that_are_not_outcomes_parse_to_none(self) -> None:
        returned = _string("DETAIL_RETURNED_TO")
        not_outcomes = [
            _string("DETAIL_BOOTED_NEW"),
            f"{_string('DETAIL_ROLLED_BACK_BY_COMMAND')}{returned}ota_0",
            "returned to ota_0; ota_1 did not confirm",
            _string("FF_DETAIL_ROLLBACK_WAITS", FF_MQTT_INTERNAL_H),
            _string("FF_DETAIL_APPLY_WAITS", FF_MQTT_INTERNAL_H),
        ]
        for detail in not_outcomes:
            assert parse_selftest_detail(detail) is None, detail
            for state in DeployState:
                assert selftest_outcome(state.value, detail) is None, (state, detail)


FAILED = "selftest failed: boom; returned to ota_1"
TIMED_OUT = "selftest timed out; returned to ota_1"
PASSED_TAIL = "selftest passed; returned to ota_1"


@pytest.mark.parametrize(
    ("state", "detail", "expected"),
    [
        ("confirmed", "selftest passed", "passed"),
        ("confirmed", PASSED_TAIL, None),
        ("confirmed", FAILED, None),
        ("confirmed", TIMED_OUT, None),
        ("rolled_back", FAILED, "failed"),
        ("rolled_back", TIMED_OUT, "timed_out"),
        ("rolled_back", "selftest timed out", "timed_out"),
        ("rolled_back", "selftest passed", None),
        ("rolled_back", PASSED_TAIL, None),
        ("failed", FAILED, None),
        ("failed", "selftest passed", None),
        ("rolling_back", FAILED, None),
        ("rolling_back", TIMED_OUT, None),
        ("awaiting_safe_window", FAILED, None),
        ("awaiting_safe_window", "selftest passed", None),
        ("requested", "selftest passed", None),
        ("requested", FAILED, None),
        ("confirming", "selftest passed", None),
        ("teleporting", FAILED, None),
        ("confirmed", None, None),
        ("rolled_back", None, None),
        ("rolled_back", "returned to ota_0; ota_1 did not confirm", None),
    ],
)
def test_the_grammar_counts_only_on_its_own_state(
    state: str, detail: str | None, expected: str | None
) -> None:
    result = selftest_outcome(state, detail)
    assert (result.outcome if result is not None else None) == expected
