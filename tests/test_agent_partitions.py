"""The agent's flash-time immutables. **CRITICAL** — no toolchain, no broker, no database.

`CRITICAL.md` → *Partition table, `sdkconfig` bootloader options, eFuse burns (agent
firmware)*: "flash-time immutables, not changeable by OTA. **Wrong at R0 = physical
recall of the fleet.**" An OTA image writes *into* a partition; it can never rewrite the
table, add a partition, or change a bootloader build-time option. So these two files are
the only artefacts in this repo whose mistakes cost a van and a screwdriver.

The real proof that a *build* carries this posture is `just agent-verify <target>`, which
decodes the generated `partition-table.bin` and greps the resolved `sdkconfig` — it needs
the ESP-IDF container and therefore cannot be part of `just test` (`test_broker_config.py`
set the same rule for the broker, `R0-be-6` for MinIO). What is checkable with nothing
installed is exactly this: that the source files still say what the design claims.

Every assertion below is a specific way the fleet has already been able to break:

* an `ota_1` offset that does not equal `ota_0` end (slots overlap → an OTA write
  corrupts the running app);
* a slot size that drifts from the `ota_slot_size` the protocol advertises (the server's
  capability check passes and the device runs out of flash mid-download);
* a `factory` partition creeping back in (a factory-only board can never OTA its way to
  A/B — `design/architecture.md` → *Flash-time immutables*, item 1);
* `ff_cfg` disappearing (the flasher then has nowhere to write per-board config, and a
  partition cannot be added later);
* `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE` dropped (R2 auto-rollback silently impossible
  on every board already in the field);
* anti-rollback / secure boot / flash encryption turned on (each one an irreversible
  eFuse burn, each explicitly *off* in the v1 posture).

Expected values are **retyped literally** here on purpose: a tripwire must not import the
constant it guards. The single exception is `spec/device-protocol.md`, which is read as
text — the spec is the contract, and this is the one place the two are allowed to meet.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"
PARTITIONS_CSV = AGENT_DIR / "partitions.csv"
SDKCONFIG_DEFAULTS = AGENT_DIR / "sdkconfig.defaults"
DEVICE_PROTOCOL = REPO_ROOT / "spec" / "device-protocol.md"

# Verbatim from agent/partitions.csv, retyped. (name, type, subtype, offset, size)
EXPECTED_PARTITIONS = [
    ("nvs", "data", "nvs", 0x9000, 0x6000),
    ("otadata", "data", "ota", 0xF000, 0x2000),
    ("phy_init", "data", "phy", 0x11000, 0x1000),
    ("ff_cfg", "data", "0x40", 0x12000, 0x1000),
    ("ota_0", "app", "ota_0", 0x20000, 0x1E0000),
    ("ota_1", "app", "ota_1", 0x200000, 0x1E0000),
]

# spec/device-protocol.md → up/announce. The number the protocol promises the server.
OTA_SLOT_SIZE = 1966080
# The smallest flash design/architecture.md budgets for. Everything must fit under it.
FLASH_SIZE_4MB = 0x400000

# Present, and each one an option no OTA can add later.
REQUIRED_SDKCONFIG = [
    "CONFIG_PARTITION_TABLE_CUSTOM=y",
    'CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"',
    "CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y",
    "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y",
]

# Absent, and each one an irreversible eFuse burn. `=n` and "not set" both count as
# absent; only `=y` is a failure. Matched as a prefix so `CONFIG_SECURE_BOOT_V2_ENABLED`
# and every other `CONFIG_SECURE_BOOT*` spelling is covered.
FORBIDDEN_SDKCONFIG_PREFIXES = [
    "CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK",
    "CONFIG_SECURE_BOOT",
    "CONFIG_SECURE_FLASH_ENC_ENABLED",
]


def _rows(path: Path) -> list[tuple[str, str, str, int, int]]:
    """Parse an ESP-IDF partition CSV into (name, type, subtype, offset, size)."""
    parsed: list[tuple[str, str, str, int, int]] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = [field.strip() for field in stripped.split(",")]
        name, ptype, subtype, offset, size = fields[0], fields[1], fields[2], fields[3], fields[4]
        parsed.append((name, ptype, subtype, int(offset, 0), int(size, 0)))
    return parsed


def _config_lines(path: Path) -> list[str]:
    """Every non-comment, non-blank line of an sdkconfig fragment."""
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _enabled_options(path: Path) -> list[str]:
    """The names of every `CONFIG_X=y` in `path`."""
    return [
        line.split("=", 1)[0]
        for line in _config_lines(path)
        if line.startswith("CONFIG_") and line.split("=", 1)[1].strip() == "y"
    ]


def _named(rows: list[tuple[str, str, str, int, int]], name: str) -> tuple[str, str, str, int, int]:
    matches = [row for row in rows if row[0] == name]
    assert len(matches) == 1, f"expected exactly one {name!r} partition, got {len(matches)}"
    return matches[0]


class TestPartitionTable:
    """`agent/partitions.csv` is the layout every deployed board carries for life."""

    def test_is_exactly_the_frozen_ab_4m_v1_layout(self) -> None:
        assert _rows(PARTITIONS_CSV) == EXPECTED_PARTITIONS

    def test_both_ota_slots_are_the_advertised_ota_slot_size(self) -> None:
        rows = _rows(PARTITIONS_CSV)
        assert _named(rows, "ota_0")[4] == OTA_SLOT_SIZE == 0x1E0000
        assert _named(rows, "ota_1")[4] == OTA_SLOT_SIZE == 0x1E0000

    def test_ota_slot_size_is_the_number_in_the_protocol_spec(self) -> None:
        """The CSV and `spec/device-protocol.md` must be the same number.

        The server performs `flows.md`'s capability check ("reject on chip / partition-size
        mismatch") against the `ota_slot_size` the device announces. If the spec and the
        flashed layout ever disagree, the check passes and the device runs out of flash
        halfway through a download.
        """
        spec_text = DEVICE_PROTOCOL.read_text()
        assert f'"ota_slot_size": {OTA_SLOT_SIZE}' in spec_text
        assert '"partition_layout": "ab-4m-v1"' in spec_text

    def test_slots_are_adjacent_and_fit_in_4mb(self) -> None:
        rows = _rows(PARTITIONS_CSV)
        ota_0, ota_1 = _named(rows, "ota_0"), _named(rows, "ota_1")
        assert ota_1[3] == ota_0[3] + ota_0[4], "ota_1 must start exactly where ota_0 ends"
        assert ota_1[3] + ota_1[4] <= FLASH_SIZE_4MB, "the layout must fit 4 MB flash"

    def test_app_partitions_are_64k_aligned(self) -> None:
        """The ESP-IDF bootloader requires it; a misaligned app partition will not boot."""
        for row in _rows(PARTITIONS_CSV):
            if row[1] == "app":
                assert row[3] % 0x10000 == 0, f"{row[0]} is not 64 KB aligned"

    def test_no_partition_overlaps_its_neighbour(self) -> None:
        rows = sorted(_rows(PARTITIONS_CSV), key=lambda row: row[3])
        for earlier, later in zip(rows, rows[1:], strict=False):
            assert earlier[3] + earlier[4] <= later[3], f"{earlier[0]} overlaps {later[0]}"

    def test_there_is_no_factory_partition(self) -> None:
        """A factory-only board can never OTA its way to A/B — architecture.md, item 1."""
        rows = _rows(PARTITIONS_CSV)
        assert not [row for row in rows if row[0] == "factory" or row[2] == "factory"]

    def test_otadata_exists(self) -> None:
        """Without it the bootloader has nowhere to record which slot is active."""
        assert _named(rows := _rows(PARTITIONS_CSV), "otadata")[1:3] == ("data", "ota")
        assert _named(rows, "otadata")[4] == 0x2000

    def test_ff_cfg_exists_for_flash_time_configuration(self) -> None:
        """`spec/flows.md` Flow 1 step 4 flashes "the matching prebuilt agent + baked config".

        A partition cannot be added by OTA, so the space for that config is reserved at R0
        even though `R0-fw-1`/`R0-fe-3` define its payload format. Deleting this row means
        retrieving every board before the flasher can configure one.
        """
        name, ptype, subtype, offset, size = _named(_rows(PARTITIONS_CSV), "ff_cfg")
        assert (name, ptype, subtype, offset, size) == ("ff_cfg", "data", "0x40", 0x12000, 0x1000)


class TestBootloaderPosture:
    """`agent/sdkconfig.defaults` — bootloader build-time options and the eFuse posture."""

    @pytest.mark.parametrize("option", REQUIRED_SDKCONFIG)
    def test_required_option_is_present(self, option: str) -> None:
        assert option in _config_lines(SDKCONFIG_DEFAULTS)

    @pytest.mark.parametrize("prefix", FORBIDDEN_SDKCONFIG_PREFIXES)
    def test_forbidden_option_is_not_enabled_anywhere(self, prefix: str) -> None:
        for path in [SDKCONFIG_DEFAULTS, *sorted(AGENT_DIR.glob("sdkconfig.defaults.*"))]:
            enabled = [name for name in _enabled_options(path) if name.startswith(prefix)]
            assert not enabled, f"{path.name} enables {enabled} — an irreversible eFuse burn"

    def test_per_target_files_carry_no_safety_option(self) -> None:
        """Deltas only. A safety option in one target's file is a posture that silently
        differs per chip — and the common file stops being the one place to read it from.
        """
        guarded = ("CONFIG_BOOTLOADER_", "CONFIG_SECURE_", "CONFIG_PARTITION_TABLE_")
        for path in sorted(AGENT_DIR.glob("sdkconfig.defaults.*")):
            offenders = [line for line in _config_lines(path) if line.startswith(guarded)]
            assert not offenders, f"{path.name} sets {offenders}; move it to sdkconfig.defaults"

    def test_every_build_target_has_a_defaults_file(self) -> None:
        """`agent/Dockerfile` names `sdkconfig.defaults.$IDF_TARGET` unconditionally."""
        present = {path.suffix.lstrip(".") for path in AGENT_DIR.glob("sdkconfig.defaults.*")}
        assert {"esp32", "esp32s3", "esp32c3", "esp32c6"} <= present

    def test_agent_holds_no_credential(self) -> None:
        """Credentials arrive at flash time, per board, in `ff_cfg` — never in the source.

        The whole point of the config partition is that one build serves the fleet. A Wi-Fi
        SSID or an enrollment token committed here would be baked into every image and,
        being firmware, unrotatable.
        """
        forbidden = re.compile(
            r"(wifi[_-]?(ssid|password)|ffe_[A-Za-z0-9]|ffa_[A-Za-z0-9]|mqtt_password)\s*=?\s*['\"]"
            r"[^'\"]+['\"]",
            re.IGNORECASE,
        )
        for path in sorted(AGENT_DIR.rglob("*")):
            if not path.is_file() or "dist" in path.parts or "build" in path.parts:
                continue
            match = forbidden.search(path.read_text(errors="replace"))
            assert match is None, f"{path} looks like it carries a credential: {match!r}"
