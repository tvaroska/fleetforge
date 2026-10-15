"""R4-fw-1 — `up/hb` carries `min_free_heap`, and `boot_ok` means "the confirm path has run".

`spec/device-protocol.md` -> `up/hb`: the example is the contract, keys in its order.
`min_free_heap` is `esp_get_minimum_free_heap_size()` (the low-water mark since boot) beside
`free_heap`; `boot_ok` is false only while otadata says `ESP_OTA_IMG_PENDING_VERIFY`, read
on every heartbeat. No host test can run the C, so these read it as text, against the spec
example, the way `test_reset_reason.py` does. One builder (`ff_identity_heartbeat_json`)
serves the agent, the ESP-IDF example and the Arduino library. The simulator's
`boot_ok` session behaviour is in `tests/test_simulator.py`.
"""

from __future__ import annotations

import json
import re

from fleetforge.ingestor.protocol import HeartbeatPayload, decode
from fleetforge.simulator.device import DeviceIdentity
from tests.agent_src import COMPONENT_SRC
from tests.test_agent_txn import _function_body
from tests.test_ff_cfg import _code
from tests.test_library_marker import DEVICE_PROTOCOL

FF_IDENTITY_C = COMPONENT_SRC / "ff_identity.c"
HB_TOPIC = "ff/v1/d/a4cf12b3de90/up/hb"


def _spec_hb_example_text() -> str:
    """The raw JSON of the first ```json fence under the spec's `up/hb` heading."""
    text = DEVICE_PROTOCOL.read_text()
    start = text.index("### `up/hb` — heartbeat")
    fence = text.index("```json\n", start) + len("```json\n")
    return text[fence : text.index("```", fence)].strip()


def spec_hb_example() -> dict[str, object]:
    """The spec's `up/hb` example, parsed. Shared with `test_ff_cfg.py`."""
    example = json.loads(_spec_hb_example_text())
    assert isinstance(example, dict) and example, "the spec's up/hb example could not be parsed"
    return example


def spec_hb_keys() -> list[str]:
    return list(spec_hb_example())


def _heartbeat_body() -> str:
    return _function_body(_code(FF_IDENTITY_C), "ff_identity_heartbeat_json")


def _firmware_hb_keys() -> list[str]:
    """Every key `ff_identity_heartbeat_json` adds, in source order, deduplicated.

    The rssi branch adds `"rssi"` twice (a number, or null on a link without one).
    """
    keys = re.findall(r'cJSON_Add\w+ToObject\(root,\s*"([a-z_]+)"', _heartbeat_body())
    return list(dict.fromkeys(keys))


def test_the_spec_example_has_min_free_heap_and_boot_ok() -> None:
    example = spec_hb_example()
    min_free_heap = example.get("min_free_heap")
    free_heap = example.get("free_heap")
    assert isinstance(min_free_heap, int) and not isinstance(min_free_heap, bool)
    assert isinstance(free_heap, int)
    assert min_free_heap <= free_heap, "a low-water mark above the current free heap"
    assert isinstance(example.get("boot_ok"), bool)


def test_the_firmware_builds_exactly_the_spec_keys() -> None:
    assert _firmware_hb_keys() == spec_hb_keys()


def test_min_free_heap_is_the_idf_low_water_mark() -> None:
    lines = [line for line in _heartbeat_body().splitlines() if '"min_free_heap"' in line]
    assert len(lines) == 1
    assert "esp_get_minimum_free_heap_size()" in lines[0]


def test_boot_ok_is_read_per_heartbeat() -> None:
    """False only for PENDING_VERIFY, read fresh each beat, never cached at boot.

    A cached value (like `reset_reason`'s) would report false for the whole life of an OTA
    boot that confirmed; also treating `ESP_OTA_IMG_NEW` as not-ok would make an image on a
    rollback-less bootloader a permanent false alarm (DECISIONS 2026-10-15, R4-fw-1).
    """
    source = _code(FF_IDENTITY_C)
    hb = _heartbeat_body()
    assert re.search(r'cJSON_AddBoolToObject\(root,\s*"boot_ok",\s*boot_ok\(\)\)', hb)

    predicate = _function_body(source, "boot_ok")
    assert "esp_ota_get_state_partition(" in predicate
    states = set(re.findall(r"\bESP_OTA_IMG_[A-Z_]+\b", predicate))
    # UNDEFINED is only the initialiser for a failed read; the comparison is PENDING_VERIFY.
    assert states <= {"ESP_OTA_IMG_UNDEFINED", "ESP_OTA_IMG_PENDING_VERIFY"}
    assert re.search(r"return\s+state\s*!=\s*ESP_OTA_IMG_PENDING_VERIFY\s*;", predicate)
    assert "ESP_OTA_IMG_NEW" not in predicate

    # No file-scope cache of the answer: nothing static is named like it.
    assert not re.search(r"\bstatic\s+(?:volatile\s+)?bool\s+s_\w*boot\w*", source)
    assert not re.search(r"\bs_boot_ok\b", source)


def test_the_simulator_sends_exactly_the_spec_keys() -> None:
    body = DeviceIdentity(device_id="a4cf12b3de90").heartbeat(41)
    assert list(body) == spec_hb_keys()
    min_free_heap, free_heap = body["min_free_heap"], body["free_heap"]
    assert isinstance(min_free_heap, int) and isinstance(free_heap, int)
    assert 0 < min_free_heap <= free_heap
    assert body["boot_ok"] is True
    assert DeviceIdentity(device_id="a4cf12b3de90").heartbeat(41, boot_ok=False)["boot_ok"] is False


def test_the_ingestor_reads_the_spec_example() -> None:
    """Additive is harmless server-side: the example still decodes and keeps fw_version."""
    payload = decode(HeartbeatPayload, HB_TOPIC, _spec_hb_example_text().encode())
    assert payload is not None
    assert payload.fw_version == spec_hb_example()["fw_version"]
