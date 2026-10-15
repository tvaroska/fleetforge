"""S0-fw-6 — the announce says why the chip last reset.

`spec/device-protocol.md` -> `up/announce` `reset_reason`: `esp_reset_reason()` of the
current boot, lowercased without `ESP_RST_`, read once at boot, after `capabilities` and
before `lib_marker`. No host test can run the C, so these read it as text, against the
spec paragraph, the way `test_library_marker.py` does. The server half (logged, never
stored) is in `tests/test_ingestor.py`; the simulator half in `tests/test_simulator.py`.
"""

from __future__ import annotations

import json
import re

from fleetforge.announce_fields import RESET_REASON_MAX_CHARS, normalize_reset_reason
from tests.agent_src import (
    AGENT_MAIN_C,
    ARDUINO_WRAPPER_CPP,
    COMPONENT_SRC,
    IDF_EXAMPLE_START_C,
)
from tests.test_agent_txn import _function_body
from tests.test_ff_cfg import _code
from tests.test_library_marker import DEVICE_PROTOCOL

FF_IDENTITY_C = COMPONENT_SRC / "ff_identity.c"


def _spec_names() -> list[str]:
    """The value list of the spec's `reset_reason` paragraph, in its order."""
    text = DEVICE_PROTOCOL.read_text()
    start = text.index("`reset_reason` is why the chip last reset")
    paragraph = text[start : text.index("\n\n", start)]
    names = re.findall(r"`([a-z_]+)`", paragraph.split("prefix:", 1)[1].split(". It", 1)[0])
    assert names, "the spec's reset_reason paragraph could not be parsed"
    return names


def _firmware_names() -> dict[str, str]:
    """`ESP_RST_X` -> the name ff_identity.c::reset_reason_name returns for it."""
    body = _function_body(_code(FF_IDENTITY_C), "reset_reason_name")
    return dict(re.findall(r"case (ESP_RST_[A-Z_]+):\s*return \"([a-z_]+)\";", body))


class TestTheNamesAreTheSpec:
    def test_the_spec_lists_sixteen_names(self) -> None:
        names = _spec_names()
        assert len(names) == 16
        assert "unknown" in names and "poweron" in names
        assert all(len(name) <= RESET_REASON_MAX_CHARS for name in names)

    def test_every_name_is_the_lowercased_enum(self) -> None:
        mapping = _firmware_names()
        assert mapping, "ff_identity.c::reset_reason_name maps nothing"
        for enum, name in mapping.items():
            assert enum == "ESP_RST_" + name.upper(), enum

    def test_the_firmware_knows_exactly_the_spec_names(self) -> None:
        assert set(_firmware_names().values()) | {"unknown"} == set(_spec_names())

    def test_an_unmappable_value_is_unknown(self) -> None:
        body = _function_body(_code(FF_IDENTITY_C), "reset_reason_name")
        assert re.search(r'default:\s*return "unknown";', body)
        assert '"%d"' not in body and "%u" not in body, "never a number on the wire"


class TestTheAnnounceCarriesIt:
    def test_after_capabilities_before_lib_marker(self) -> None:
        body = _function_body(_code(FF_IDENTITY_C), "announce_object")
        line = 'cJSON_AddStringToObject(root, "reset_reason", s_reset_reason);'
        assert body.count(line) == 1
        assert body.index('"capabilities", capabilities') < body.index(line)
        assert body.index(line) < body.index('"lib_marker"')

    def test_it_is_read_once_at_boot(self) -> None:
        """In ff_identity_init and nowhere else: a reconnect repeats the boot's value."""
        code = _code(FF_IDENTITY_C)
        assert code.count("esp_reset_reason()") == 1
        init = _function_body(code, "ff_identity_init")
        assert "s_reset_reason = reset_reason_name(esp_reset_reason());" in init
        # No 12-hex run and before device_id: frontend/src/diagnostics.ts reads the id
        # from the last ff-id line.
        log = init.index('"reset_reason %s (announced)"')
        assert log < init.index('"device_id %s"')

    def test_every_boot_path_reaches_it(self) -> None:
        """The agent, the ESP-IDF example and the Arduino library all call
        ff_identity_init, so all three announce it from the one shared file."""
        for path in (AGENT_MAIN_C, IDF_EXAMPLE_START_C, ARDUINO_WRAPPER_CPP):
            assert "ff_identity_init()" in _code(path), path.name

    def test_the_spec_example_puts_it_last_but_one(self) -> None:
        text = DEVICE_PROTOCOL.read_text()
        start = text.index("### `up/announce` — identity")
        block_start = text.index("```json", start) + len("```json")
        example = json.loads(text[block_start : text.index("```", block_start)])
        keys = list(example)
        assert keys[-3:] == ["capabilities", "reset_reason", "lib_marker"]
        assert example["reset_reason"] == "poweron"
        assert example["proto"] == 1


class TestTheNormaliser:
    def test_every_spec_name_passes(self) -> None:
        for name in _spec_names():
            assert normalize_reset_reason(name) == name

    def test_unknown_names_pass_as_is(self) -> None:
        assert normalize_reset_reason("brand_new") == "brand_new"
        assert normalize_reset_reason("x" * RESET_REASON_MAX_CHARS) == "x" * 16

    def test_malformed_is_none(self) -> None:
        for value in (7, True, 1.5, ["sw"], {"a": 1}, "x" * 17, "sw\r\n", "\ud800", ""):
            assert normalize_reset_reason(value) is None, value
        assert normalize_reset_reason(None) is None
