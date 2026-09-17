"""The `ff_cfg` blob — a cross-language contract, checked with no toolchain installed.

Three implementations have to agree about sixteen bytes: `agent/tools/ff_cfg.py` (the
writer used by the QEMU harness), `agent/main/ff_cfg.c` (the firmware reader, which no
host test can execute) and — since R0-fe-3 — `frontend/src/ffcfg.ts`, the browser
flasher's `encodeFfCfg()`, which no Python test can execute either. A disagreement is not
a test failure on this box: it is a board that boots, finds a header it does not
recognise, and idles. On a workbench that looks like dead firmware.

So this file does two different jobs:

* **behaviour**, on the Python implementation: round-trip, exact size, and every rejection
  branch (the ones that matter are the ones that keep a half-written config from being
  treated as a good one).
* **tripwires**, on the C and TypeScript sources read as *text*, plus a shared golden
  vector for the TypeScript writer. The header's constants are retyped here, never
  imported, and the field names the other two implementations use are grepped out of
  them. That is the same idiom `test_agent_partitions.py` established for the partition
  table, and it is the only way a pure-Python test can hold the other two to the contract.

`agent/tools/ff_cfg.py` is imported by path: `agent/` is not a package and must not
become one — it ships inside the ESP-IDF builder image, where `fleetforge` does not exist.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import struct
import zlib
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"
FF_CFG_PY = AGENT_DIR / "tools" / "ff_cfg.py"
FF_CFG_C = AGENT_DIR / "main" / "ff_cfg.c"
FF_CFG_H = AGENT_DIR / "main" / "ff_cfg.h"
FF_IDENTITY_C = AGENT_DIR / "main" / "ff_identity.c"
FF_IDENTITY_H = AGENT_DIR / "main" / "ff_identity.h"
FF_MQTT_H = AGENT_DIR / "main" / "ff_mqtt.h"
FF_OTA_C = AGENT_DIR / "main" / "ff_ota.c"
# The third implementation and the vector both writers are pinned to (R0-fe-3).
FF_CFG_TS = REPO_ROOT / "frontend" / "src" / "ffcfg.ts"
FF_CFG_VECTOR = REPO_ROOT / "frontend" / "src" / "ffcfg.vector.json"
DEVICE_PROTOCOL = REPO_ROOT / "spec" / "device-protocol.md"

# Retyped from the format's documentation, never imported from the module under test.
# `<4sHHII` = magic, version, reserved, payload_len, crc32.
MAGIC = b"FFCF"
VERSION = 1
HEADER = struct.Struct("<4sHHII")
HEADER_SIZE = 16
PARTITION_SIZE = 4096
MAX_PAYLOAD = 4080

# A minimal, valid config: the two keys the firmware has no default for.
MINIMAL = {"api_base": "http://10.0.2.2:8080", "mqtt_uri": "mqtt://10.0.2.2:8883"}


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ff_cfg_tool", FF_CFG_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ff_cfg = _load_module()


def _blob(fields: dict[str, object] | None = None) -> bytes:
    return ff_cfg.encode(dict(MINIMAL) if fields is None else fields)


def _code(path: Path) -> str:
    """C source with comments removed.

    The comments in these files quote the shapes they exist to forbid — `ff_identity.c`
    says in prose that a nested `{"token":…, "identity":{…}}` would be a fleet recall — so
    a grep for a forbidden spelling has to look at code, not at the explanation of why the
    code is written the way it is.
    """
    source = path.read_text()
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.DOTALL)


class TestEncode:
    def test_is_exactly_one_partition(self) -> None:
        """A blob is flashed at an offset; anything but 4096 bytes overwrites a neighbour."""
        assert len(_blob()) == PARTITION_SIZE

    def test_header_is_the_documented_layout(self) -> None:
        magic, version, reserved, payload_len, crc = HEADER.unpack(_blob()[:HEADER_SIZE])
        payload = _blob()[HEADER_SIZE : HEADER_SIZE + payload_len]
        assert (magic, version, reserved) == (MAGIC, VERSION, 0)
        assert json.loads(payload) == MINIMAL
        assert crc == zlib.crc32(payload)

    def test_tail_is_erased_flash(self) -> None:
        """0xff, not 0x00: a short write then looks erased rather than like NUL data."""
        blob = _blob()
        payload_len = HEADER.unpack(blob[:HEADER_SIZE])[3]
        tail = blob[HEADER_SIZE + payload_len :]
        assert set(tail) == {0xFF}

    def test_round_trip(self) -> None:
        fields = {
            **MINIMAL,
            "token": "ffe_" + "x" * 32,
            "link": "ethernet",
            "hb_s": 10,
            "ntp": "pool.ntp.org",
            "power": "always_on",
        }
        assert ff_cfg.decode(ff_cfg.encode(fields)) == fields

    def test_payload_is_compact(self) -> None:
        """No spaces: 4080 bytes is a real ceiling and pretty-printing wastes ~15% of it."""
        blob = _blob()
        payload_len = HEADER.unpack(blob[:HEADER_SIZE])[3]
        assert b", " not in blob[HEADER_SIZE : HEADER_SIZE + payload_len]

    def test_requires_the_two_keys_with_no_default(self) -> None:
        with pytest.raises(ff_cfg.ConfigError, match="mqtt_uri"):
            ff_cfg.encode({"api_base": "http://x:8080"})

    def test_refuses_an_oversized_payload(self) -> None:
        """A long URL plus a long passphrase really does reach 4080 bytes."""
        with pytest.raises(ff_cfg.ConfigError, match="over the"):
            ff_cfg.encode({**MINIMAL, "psk": "p" * MAX_PAYLOAD})

    def test_refuses_a_non_object(self) -> None:
        with pytest.raises(ff_cfg.ConfigError):
            ff_cfg.encode(["api_base"])  # type: ignore[arg-type]


class TestDecodeRejections:
    """Every branch here has a twin in `ff_cfg.c`, and both exist for one reason: a board
    with an unreadable config must idle loudly rather than connect approximately."""

    def test_erased_flash_says_never_configured(self) -> None:
        with pytest.raises(ff_cfg.ConfigError, match="never been configured"):
            ff_cfg.decode(b"\xff" * PARTITION_SIZE)

    def test_bad_magic(self) -> None:
        blob = bytearray(_blob())
        blob[0:4] = b"XXXX"
        with pytest.raises(ff_cfg.ConfigError, match="magic"):
            ff_cfg.decode(bytes(blob))

    def test_unknown_version(self) -> None:
        blob = bytearray(_blob())
        blob[4:6] = (VERSION + 1).to_bytes(2, "little")
        with pytest.raises(ff_cfg.ConfigError, match="version 2"):
            ff_cfg.decode(bytes(blob))

    def test_nonzero_reserved(self) -> None:
        blob = bytearray(_blob())
        blob[6:8] = (1).to_bytes(2, "little")
        with pytest.raises(ff_cfg.ConfigError, match="reserved"):
            ff_cfg.decode(bytes(blob))

    def test_payload_len_past_the_partition(self) -> None:
        blob = bytearray(_blob())
        blob[8:12] = (MAX_PAYLOAD + 1).to_bytes(4, "little")
        with pytest.raises(ff_cfg.ConfigError, match="payload_len"):
            ff_cfg.decode(bytes(blob))

    def test_crc_mismatch_is_a_partial_write(self) -> None:
        """The single most important rejection: a torn write is not a valid config."""
        blob = bytearray(_blob())
        blob[HEADER_SIZE] = ord("[")  # corrupt one payload byte, leave the header alone
        with pytest.raises(ff_cfg.ConfigError, match="crc32 mismatch"):
            ff_cfg.decode(bytes(blob))

    def test_short_blob(self) -> None:
        with pytest.raises(ff_cfg.ConfigError, match="shorter than"):
            ff_cfg.decode(_blob()[:8])

    def test_payload_that_is_not_json(self) -> None:
        payload = b"not json at all"
        header = HEADER.pack(MAGIC, VERSION, 0, len(payload), zlib.crc32(payload))
        blob = header + payload
        with pytest.raises(ff_cfg.ConfigError, match="not JSON"):
            ff_cfg.decode(blob + b"\xff" * (PARTITION_SIZE - len(blob)))

    def test_payload_that_is_json_but_not_an_object(self) -> None:
        payload = b"[1,2,3]"
        header = HEADER.pack(MAGIC, VERSION, 0, len(payload), zlib.crc32(payload))
        blob = header + payload
        with pytest.raises(ff_cfg.ConfigError, match="not an object"):
            ff_cfg.decode(blob + b"\xff" * (PARTITION_SIZE - len(blob)))


class TestValidate:
    """Refuse, before flashing, what the server would refuse after a token is burned."""

    def test_sleepy_needs_a_wake_interval(self) -> None:
        with pytest.raises(ff_cfg.ConfigError, match="sleepy"):
            ff_cfg.validate({**MINIMAL, "link": "ethernet", "power": "sleepy"})

    def test_wifi_needs_an_ssid(self) -> None:
        with pytest.raises(ff_cfg.ConfigError, match="ssid"):
            ff_cfg.validate({**MINIMAL, "link": "wifi"})

    def test_scheme_selects_tls_so_it_is_checked(self) -> None:
        with pytest.raises(ff_cfg.ConfigError, match="api_base"):
            ff_cfg.validate({**MINIMAL, "api_base": "10.0.2.2:8080", "link": "ethernet"})

    def test_a_valid_ethernet_config_passes(self) -> None:
        ff_cfg.validate({**MINIMAL, "link": "ethernet", "hb_s": 10})


class TestDescribeHidesSecrets:
    def test_token_and_psk_are_never_rendered(self) -> None:
        rendered = ff_cfg.describe({**MINIMAL, "token": "ffe_secret", "psk": "hunter2"})
        assert "ffe_secret" not in rendered
        assert "hunter2" not in rendered
        assert "not shown" in rendered


class TestTypeScriptWriterAgrees:
    """The third implementation: `frontend/src/ffcfg.ts`, the browser flasher's encoder.

    It cannot be executed here (no node in this suite's environment), so the contract is
    held two ways, exactly as the C reader's is:

    * a **golden vector** — `frontend/src/ffcfg.vector.json` carries the fields and the
      sha256 of the blob the PYTHON writer makes from them. `frontend/src/ffcfg.test.ts`
      asserts the same digest from the TypeScript side, so if the two writers ever
      disagree about a byte exactly one of the two suites goes red.
    * **text tripwires** over the TypeScript source, in the idiom `TestCSourceAgrees`
      established: constants retyped here, never imported.

    The vector is ASCII-only on purpose and the file says so: `json.dumps` defaults to
    `ensure_ascii=True` and `JSON.stringify` does not, so a non-ASCII SSID makes the two
    writers emit different bytes for the same decoded object. The contract is the decoded
    object; a byte-exact vector is only possible where the two encodings coincide.
    """

    def _vector(self) -> dict[str, object]:
        return json.loads(FF_CFG_VECTOR.read_text())

    def test_the_python_writer_reproduces_the_vector_digest(self) -> None:
        vector = self._vector()
        blob = ff_cfg.encode(vector["fields"])  # type: ignore[arg-type]
        assert hashlib.sha256(blob).hexdigest() == vector["sha256"]

    def test_the_vector_is_ascii_only(self) -> None:
        """Non-ASCII would make the two writers disagree on bytes while agreeing on fields."""
        assert json.dumps(self._vector()["fields"]).isascii()

    def test_the_vector_is_a_config_both_sides_would_accept(self) -> None:
        ff_cfg.validate(self._vector()["fields"])  # type: ignore[arg-type]

    @pytest.mark.parametrize("key", ff_cfg.KNOWN_KEYS)
    def test_every_field_name_is_written_by_the_browser(self, key: str) -> None:
        assert f"'{key}'" in FF_CFG_TS.read_text()

    @pytest.mark.parametrize(
        "constant",
        ["'FFCF'", "4096", "4080", "16", "0xedb88320"],
    )
    def test_the_encoder_retypes_the_format_constants(self, constant: str) -> None:
        """`0xedb88320` is the reflected CRC-32/IEEE polynomial `zlib.crc32` uses."""
        assert constant in FF_CFG_TS.read_text()


class TestCSourceAgrees:
    """Tripwires over the C reader, which no host test can run. Read as text on purpose."""

    @pytest.mark.parametrize(
        "constant",
        ['"FFCF"', "1", "16", "4096", "4080", '"ff_cfg"', "0x40"],
    )
    def test_header_retypes_the_format_constants(self, constant: str) -> None:
        assert constant in FF_CFG_H.read_text()

    @pytest.mark.parametrize(
        "key",
        ["api_base", "mqtt_uri", "token", "ssid", "psk", "link", "ntp", "hb_s", "power", "wake_s"],
    )
    def test_every_field_name_is_read_by_the_firmware(self, key: str) -> None:
        """A rename on one side fails here rather than on a bench with a serial cable."""
        assert f'"{key}"' in _code(FF_CFG_C)

    def test_the_python_writer_and_the_c_reader_know_the_same_keys(self) -> None:
        source = _code(FF_CFG_C)
        assert all(f'"{key}"' in source for key in ff_cfg.KNOWN_KEYS)

    def test_link_values_match(self) -> None:
        """`link` selects an adapter behind the ff_net seam, so the firmware decides it and
        must know both spellings. `power` is passed through verbatim to `up/announce` and
        judged by the server (`EnrollRequest`), so only its default is fixed here."""
        source = _code(FF_CFG_C)
        for value in ff_cfg.LINKS:
            assert f'"{value}"' in source
        assert f'"{ff_cfg.POWER_CLASSES[0]}"' in _code(FF_CFG_H)


class TestAnnounceMatchesTheSpec:
    """`ff_identity.c` builds the announce payload; the spec prints it. Same keys, or the
    server stores an identity the board did not send."""

    def _spec_announce_keys(self) -> list[str]:
        text = DEVICE_PROTOCOL.read_text()
        start = text.index("### `up/announce` — identity")
        block = text[start : text.index("```", text.index("```json", start) + 7)]
        return re.findall(r'^\s*"([a-z_]+)":', block, re.MULTILINE)

    def test_the_firmware_builds_exactly_the_spec_keys(self) -> None:
        source = _code(FF_IDENTITY_C)
        spec_keys = self._spec_announce_keys()
        assert spec_keys, "the spec's up/announce example could not be parsed"
        for key in spec_keys:
            assert f'"{key}"' in source, f"ff_identity.c never emits {key!r}"

    def test_the_enroll_body_is_flat(self) -> None:
        """`{token, …announce}`, never `{token, identity:{…}}` — spec step 2, and what
        `api/schemas.py::EnrollRequest` reads."""
        source = _code(FF_IDENTITY_C)
        assert '"identity"' not in source
        assert '"token"' in source

    def test_heartbeat_carries_the_spec_fields(self) -> None:
        source = _code(FF_IDENTITY_C)
        for key in ("fw_version", "uptime_s", "rssi", "free_heap", "boot_ok"):
            assert f'"{key}"' in source

    def test_the_announce_claims_the_ota_capability(self) -> None:
        """R1-fw-1. The agent stages and applies, so it must say so.

        `POST /v1/devices/{id}/deploy` answers **409** to a device whose `capabilities`
        does not contain `ota` (`api/routers/deploys.py`), and an empty array is exactly
        what this file shipped at R0. The failure is silent and remote: firmware that can
        perform an update, in a fleet the server refuses to deploy to, with the error
        appearing nowhere near the cause.
        """
        source = _code(FF_IDENTITY_C)
        assert '"capabilities"' in source
        assert '"ota"' in source, "ff_identity.c no longer claims the ota capability"


class TestTheReportedVersionIsTheRunningOne:
    """R1-fw-2. `fw_version` is what BOOTED, never what a command asked for.

    A board that reports the version it was told to install reports the right answer on
    every deploy that worked and the wrong one on every deploy that did not — i.e. it is
    silent exactly when the fleet needs it. Nothing at runtime would say so, and the fix
    ships by OTA to a board whose OTA reporting is the thing that is broken.
    """

    def test_the_running_version_has_one_source(self) -> None:
        source = _code(FF_IDENTITY_C)
        assert "ff_identity_fw_version(void)" in source
        assert "esp_app_get_description()->version" in source
        assert "const char *ff_identity_fw_version(void);" in _code(FF_IDENTITY_H)

    def test_announce_and_heartbeat_both_use_it(self) -> None:
        source = _code(FF_IDENTITY_C)
        assert (
            source.count('cJSON_AddStringToObject(root, "fw_version", ff_identity_fw_version());')
            == 2
        ), "up/announce and up/hb must both take fw_version from the running image"

    def test_the_ota_path_cannot_report_a_version(self) -> None:
        """`ff_ota_cmd_t::version` is the version the SERVER asked for. It reaches one log
        line and dies there: ff_ota.c neither builds an identity payload nor knows how."""
        source = _code(FF_OTA_C)
        assert '"fw_version"' not in source
        assert "ff_identity" not in source


class TestStatusStatesMatchTheSpec:
    """`up/status` may only carry a state the spec's machine contains.

    `ff_mqtt.h` spells the states once for both publishers (ff_mqtt.c's `failed` paths and
    ff_ota.c's walk). A state the spec does not print is a state
    `ingestor/protocol.py` will file and no dashboard can explain — and, being firmware,
    it cannot be corrected without an OTA of the thing that is broken.
    """

    def _spec_states(self) -> set[str]:
        text = DEVICE_PROTOCOL.read_text()
        start = text.index("### `up/status` — the update transaction")
        # The second fenced block in that section is the state machine; the first is the
        # JSON example.
        fence = text.index("```", text.index("```", text.index("```json", start) + 7) + 3)
        block = text[fence : text.index("```", fence + 3)]
        return set(re.findall(r"[a-z_]{4,}", block))

    def test_every_state_the_firmware_can_publish_is_in_the_machine(self) -> None:
        declared = set(re.findall(r'#define FF_STATUS_[A-Z_]+ "([a-z_]+)"', FF_MQTT_H.read_text()))
        assert declared, "ff_mqtt.h declares no FF_STATUS_* states"
        spec_states = self._spec_states()
        assert spec_states, "the spec's up/status machine could not be parsed"
        assert declared <= spec_states, f"not in spec/device-protocol.md: {declared - spec_states}"

    def test_the_walk_the_agent_performs_is_declared(self) -> None:
        """R1 ends at `rebooting`; `confirming`/`confirmed`/`rolling_back`/`rolled_back`
        are R2's (the agent confirms silently, at the announce PUBACK) and
        `awaiting_safe_window` belongs to a board with a window to wait for."""
        declared = set(re.findall(r'#define FF_STATUS_[A-Z_]+ "([a-z_]+)"', FF_MQTT_H.read_text()))
        assert declared == {
            "staging",
            "downloading",
            "verifying",
            "staged",
            "applying",
            "rebooting",
            "failed",
        }
