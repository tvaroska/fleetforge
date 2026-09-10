"""The `ff_cfg` blob — a cross-language contract, checked with no toolchain installed.

Three implementations have to agree about sixteen bytes: `agent/tools/ff_cfg.py` (the
writer used by the QEMU harness), `agent/main/ff_cfg.c` (the firmware reader, which no
host test can execute) and, at R0-fe-3, a TypeScript `encode()` in the browser flasher. A
disagreement is not a test failure on this box — it is a board that boots, finds a header
it does not recognise, and idles. On a workbench that looks like dead firmware.

So this file does two different jobs:

* **behaviour**, on the Python implementation: round-trip, exact size, and every rejection
  branch (the ones that matter are the ones that keep a half-written config from being
  treated as a good one).
* **tripwires**, on the C source read as *text*. The header's constants are retyped here,
  never imported, and the field names the firmware parses are grepped out of `ff_cfg.c`.
  That is the same idiom `test_agent_partitions.py` established for the partition table,
  and it is the only way a pure-Python test can hold the C side to the contract.

`agent/tools/ff_cfg.py` is imported by path: `agent/` is not a package and must not
become one — it ships inside the ESP-IDF builder image, where `fleetforge` does not exist.
"""

from __future__ import annotations

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
