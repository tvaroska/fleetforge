"""`fleetforge.firmware.bundledir` — reading and verifying a local bundle directory.

No toolchain and no ESP-IDF: every fixture here is a synthetic bundle written to a tmp
directory, because what is under test is the *reader*, not the build. The build's own
gate is `just agent-verify <target>` (see `tests/test_agent_partitions.py`'s docstring
for why that one cannot live in `just test`).

The rejections are the point. Each of the bad-bundle cases below is a board that would
otherwise be bricked or unflashable. Since S0-infra-6 they are **publish-time**
rejections and they *raise*: nothing must be uploaded, and `just agent-publish` must exit
non-zero with the target named, rather than reporting success and leaving the flasher on
the previous build. The drop-with-a-warning behaviour still exists on the read side and
is tested in `tests/test_agent_catalog_store.py`.

The module keeps its old name because `write_bundle` is imported from here by
`tests/test_api_agent.py` and `tests/test_agent_publish.py`.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from fleetforge.firmware import (
    EXPECTED_OTA_SLOT_SIZE,
    EXPECTED_PARTITION_LAYOUT,
    AgentBundleError,
    load_bundle_dir,
)
from tests.conftest import capture_logs

# Offsets as ESP-IDF emits them for an esp32 (bootloader at 0x1000) and an esp32c6
# (bootloader at 0x0) — the difference the manifest exists to carry.
PART_OFFSETS = {
    "esp32": {"bootloader": 4096, "partition-table": 32768, "ota-data": 61440, "app": 131072},
    "esp32c6": {"bootloader": 0, "partition-table": 32768, "ota-data": 61440, "app": 131072},
}
PART_FILES = {
    "bootloader": "bootloader.bin",
    "partition-table": "partition-table.bin",
    "ota-data": "ota-data-initial.bin",
    "app": "app.bin",
}


def write_bundle(
    root: Path,
    target: str = "esp32",
    *,
    manifest_overrides: dict[str, Any] | None = None,
    part_overrides: dict[str, dict[str, Any]] | None = None,
    skip_files: tuple[str, ...] = (),
    dir_name: str | None = None,
) -> Path:
    """Write a synthetic but structurally faithful bundle to `root/<target>`."""
    bundle_dir = root / (dir_name or target)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    parts: list[dict[str, Any]] = []
    for name, filename in PART_FILES.items():
        payload = f"{target}-{name}-payload".encode() * 8
        if filename not in skip_files:
            (bundle_dir / filename).write_bytes(payload)
        entry: dict[str, Any] = {
            "name": name,
            "path": filename,
            "offset": PART_OFFSETS.get(target, PART_OFFSETS["esp32"])[name],
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        entry.update((part_overrides or {}).get(name, {}))
        parts.append(entry)

    manifest: dict[str, Any] = {
        "schema": 1,
        "target": target,
        "chip_family": "ESP32-C6" if target == "esp32c6" else "ESP32",
        "agent_version": "0.1.0",
        "idf_version": "v5.5.5",
        "idf_image": "espressif/idf:v5.5.5@sha256:" + "a" * 64,
        "source_commit": "0" * 40,
        "built_at": "2026-09-09T12:00:00Z",
        "partition_layout": EXPECTED_PARTITION_LAYOUT,
        "ota_slot_size": EXPECTED_OTA_SLOT_SIZE,
        "flash_size": "4MB",
        # S0-infra-3 build identity. In the default fixture because a bundle *without* it
        # now logs a warning, and the tests that assert on warnings must be reading the
        # one they mean. `make_manifest.py` is what computes these for real; here they are
        # opaque 64-hex values, because this module is the reader.
        "config_sha256": "c0" * 32,
        "build_digest": "b1" * 32,
        # R0-fw-1: where the flasher writes per-board config. Optional in the model
        # because bundles built before it exist on disk — see the test below.
        "config_partition": {"label": "ff_cfg", "offset": 0x12000, "size": 0x1000},
        "parts": parts,
    }
    manifest.update(manifest_overrides or {})
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    (bundle_dir / "sdkconfig.resolved").write_text("CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y\n")
    return bundle_dir


def warnings_in(records: list[logging.LogRecord]) -> list[str]:
    return [record.getMessage() for record in records if record.levelno >= logging.WARNING]


class TestConfigPartition:
    """`config_partition` is what stops `R0-fe-3` typing `0x12000`, and it is additive."""

    def test_is_carried_through_to_the_bundle(self, tmp_path: Path) -> None:
        bundle = load_bundle_dir(write_bundle(tmp_path, "esp32"))
        assert bundle.manifest.config_partition is not None
        assert bundle.manifest.config_partition.label == "ff_cfg"
        assert bundle.manifest.config_partition.offset == 0x12000
        assert bundle.manifest.config_partition.size == 0x1000

    def test_a_bundle_without_one_still_loads(self, tmp_path: Path) -> None:
        """A bundle built before R0-fw-1 is on disk and in the registry. Refusing it would
        take the flasher offline for a field only the new flow needs."""
        bundle_dir = write_bundle(tmp_path, "esp32")
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
        del manifest["config_partition"]
        (bundle_dir / "manifest.json").write_text(json.dumps(manifest))

        assert load_bundle_dir(bundle_dir).manifest.config_partition is None

    def test_a_malformed_one_refuses_the_bundle(self, tmp_path: Path) -> None:
        """Present but wrong is not the same as absent: an offset of `null` reaching the
        flasher would write a config blob over whatever sits at address 0."""
        bundle_dir = write_bundle(tmp_path, "esp32")
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
        manifest["config_partition"] = {"label": "ff_cfg", "offset": None, "size": 4096}
        (bundle_dir / "manifest.json").write_text(json.dumps(manifest))

        with pytest.raises(AgentBundleError):
            load_bundle_dir(bundle_dir)


class TestBuildIdentity:
    """S0-infra-3. Two fields the reader carries and never recomputes.

    The behaviour that matters here is the *degradation*: an old bundle is still a
    flashable bundle. Refusing it would take the flasher offline over a field nothing on
    the flash path reads, so the answer is one warning and a `None`.
    """

    def test_both_are_carried_through_to_the_bundle(self, tmp_path: Path) -> None:
        bundle = load_bundle_dir(write_bundle(tmp_path, "esp32"))
        assert bundle.manifest.config_sha256 == "c0" * 32
        assert bundle.manifest.build_digest == "b1" * 32

    def test_a_bundle_without_them_loads_with_a_warning(self, tmp_path: Path) -> None:
        """A bundle built before S0-infra-3 is old, not invalid."""
        bundle_dir = write_bundle(tmp_path, "esp32")
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
        del manifest["config_sha256"]
        del manifest["build_digest"]
        (bundle_dir / "manifest.json").write_text(json.dumps(manifest))

        with capture_logs() as records:
            bundle = load_bundle_dir(bundle_dir)

        assert bundle.manifest.config_sha256 is None
        assert bundle.manifest.build_digest is None
        assert any("build identity" in message for message in warnings_in(records))

    def test_a_present_bundle_warns_about_nothing(self, tmp_path: Path) -> None:
        """Vacuity guard for the test above: the warning must be about the missing field,
        not something this fixture does on every load."""
        bundle_dir = write_bundle(tmp_path, "esp32")
        with capture_logs() as records:
            load_bundle_dir(bundle_dir)
        assert warnings_in(records) == []

    @pytest.mark.parametrize("field", ["config_sha256", "build_digest"])
    def test_a_malformed_digest_is_refused(self, tmp_path: Path, field: str) -> None:
        """Absent is old; truncated or non-hex is corrupt, and a corrupt identity is worse
        than none — it would be compared against another bundle's and believed."""
        bundle_dir = write_bundle(tmp_path, "esp32", manifest_overrides={field: "not-a-digest"})
        with pytest.raises(AgentBundleError):
            load_bundle_dir(bundle_dir)


class TestHappyPath:
    def test_parts_are_indexed_by_logical_name_and_sorted_by_offset(self, tmp_path: Path) -> None:
        bundle = load_bundle_dir(write_bundle(tmp_path, "esp32"))
        assert [part.name for part in bundle.parts] == [
            "bootloader",
            "partition-table",
            "ota-data",
            "app",
        ]
        assert [part.offset for part in bundle.parts] == [4096, 32768, 61440, 131072]
        assert bundle.key == ("esp32", EXPECTED_PARTITION_LAYOUT)

    def test_the_chip_specific_bootloader_offset_survives_the_round_trip(
        self, tmp_path: Path
    ) -> None:
        """The one number a wrong implementation would normalise away.

        `0x1000` on ESP32, `0x0` on the C6. A loader that "helpfully" defaulted it would
        produce an image that flashes cleanly and never boots, on half the fleet.
        """
        esp32 = load_bundle_dir(write_bundle(tmp_path, "esp32"))
        esp32c6 = load_bundle_dir(write_bundle(tmp_path, "esp32c6"))
        assert next(p for p in esp32.parts if p.name == "bootloader").offset == 4096
        assert next(p for p in esp32c6.parts if p.name == "bootloader").offset == 0

    def test_part_paths_are_absolute_and_inside_the_bundle(self, tmp_path: Path) -> None:
        bundle_dir = write_bundle(tmp_path, "esp32")
        bundle = load_bundle_dir(bundle_dir)
        for part in bundle.parts:
            assert part.path.is_absolute()
            assert part.path.is_relative_to(bundle_dir)

    def test_the_manifest_bytes_are_the_file_verbatim(self, tmp_path: Path) -> None:
        """The digest the index records must be the digest of a file on disk.

        A re-serialised pydantic model hashes to something no one can reproduce from the
        artifact, and `build_digest` stops being checkable against it.
        """
        bundle_dir = write_bundle(tmp_path, "esp32")
        bundle = load_bundle_dir(bundle_dir)
        assert bundle.manifest_bytes == (bundle_dir / "manifest.json").read_bytes()

    def test_a_layout_suffixed_directory_loads(self, tmp_path: Path) -> None:
        """`<target>.<layout>` is how two layouts for one chip coexist (S0-infra-7)."""
        bundle_dir = write_bundle(tmp_path, "esp32", dir_name=f"esp32.{EXPECTED_PARTITION_LAYOUT}")
        assert load_bundle_dir(bundle_dir).key == ("esp32", EXPECTED_PARTITION_LAYOUT)


class TestRejections:
    """Every case here must RAISE, naming the problem: nothing may be published."""

    def test_a_wrong_sha256_is_refused(self, tmp_path: Path) -> None:
        bundle_dir = write_bundle(tmp_path, "esp32", part_overrides={"app": {"sha256": "b" * 64}})
        with pytest.raises(AgentBundleError, match="sha256"):
            load_bundle_dir(bundle_dir)

    def test_a_flipped_byte_is_refused(self, tmp_path: Path) -> None:
        """The corruption the manifest exists to catch — a brick on someone's desk."""
        bundle_dir = write_bundle(tmp_path, "esp32")
        blob = bytearray((bundle_dir / "app.bin").read_bytes())
        blob[0] ^= 0xFF
        (bundle_dir / "app.bin").write_bytes(bytes(blob))

        with pytest.raises(AgentBundleError, match="sha256"):
            load_bundle_dir(bundle_dir)

    def test_a_wrong_size_is_refused(self, tmp_path: Path) -> None:
        bundle_dir = write_bundle(tmp_path, "esp32", part_overrides={"app": {"size": 999999}})
        with pytest.raises(AgentBundleError):
            load_bundle_dir(bundle_dir)

    def test_a_missing_part_file_is_refused(self, tmp_path: Path) -> None:
        bundle_dir = write_bundle(tmp_path, "esp32", skip_files=("app.bin",))
        with pytest.raises(AgentBundleError):
            load_bundle_dir(bundle_dir)

    def test_a_part_absent_from_the_manifest_is_refused(self, tmp_path: Path) -> None:
        bundle_dir = write_bundle(tmp_path, "esp32")
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
        manifest["parts"] = [p for p in manifest["parts"] if p["name"] != "ota-data"]
        (bundle_dir / "manifest.json").write_text(json.dumps(manifest))

        with pytest.raises(AgentBundleError, match="ota-data"):
            load_bundle_dir(bundle_dir)

    def test_a_superseded_partition_layout_is_refused(self, tmp_path: Path) -> None:
        """`spec/device-protocol.md` fixes the layout; a board flashed with another
        announces something the server's capability check does not understand."""
        bundle_dir = write_bundle(
            tmp_path, "esp32", manifest_overrides={"partition_layout": "ab-2m-v0"}
        )
        with pytest.raises(AgentBundleError, match="ab-2m-v0"):
            load_bundle_dir(bundle_dir)

    def test_a_wrong_ota_slot_size_is_refused(self, tmp_path: Path) -> None:
        bundle_dir = write_bundle(tmp_path, "esp32", manifest_overrides={"ota_slot_size": 1048576})
        with pytest.raises(AgentBundleError, match="ota_slot_size"):
            load_bundle_dir(bundle_dir)

    def test_a_manifest_target_that_disagrees_with_its_directory_is_refused(
        self, tmp_path: Path
    ) -> None:
        bundle_dir = write_bundle(tmp_path, "esp32", manifest_overrides={"target": "esp32c3"})
        with pytest.raises(AgentBundleError, match="esp32"):
            load_bundle_dir(bundle_dir)

    def test_an_unknown_schema_is_refused(self, tmp_path: Path) -> None:
        bundle_dir = write_bundle(tmp_path, "esp32", manifest_overrides={"schema": 2})
        with pytest.raises(AgentBundleError):
            load_bundle_dir(bundle_dir)

    def test_unparseable_json_is_refused(self, tmp_path: Path) -> None:
        bundle_dir = write_bundle(tmp_path, "esp32")
        (bundle_dir / "manifest.json").write_text("{not json")
        with pytest.raises(AgentBundleError):
            load_bundle_dir(bundle_dir)

    @pytest.mark.parametrize(
        "hostile_path",
        ["../../etc/passwd", "/etc/passwd", "sub/dir.bin", ".hidden", "app.bin\x00"],
    )
    def test_a_manifest_path_that_is_not_a_bare_filename_is_refused(
        self, tmp_path: Path, hostile_path: str
    ) -> None:
        bundle_dir = write_bundle(tmp_path, "esp32", part_overrides={"app": {"path": hostile_path}})
        with pytest.raises(AgentBundleError):
            load_bundle_dir(bundle_dir)

    def test_a_symlink_out_of_the_bundle_is_refused(self, tmp_path: Path) -> None:
        """`_resolve_part_path`'s second layer: the name is legal, the target is not."""
        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"not firmware")
        bundle_dir = write_bundle(tmp_path, "esp32")
        (bundle_dir / "app.bin").unlink()
        (bundle_dir / "app.bin").symlink_to(outside)

        with pytest.raises(AgentBundleError):
            load_bundle_dir(bundle_dir)

    def test_a_directory_with_no_manifest_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "esp32").mkdir()
        with pytest.raises(AgentBundleError, match="manifest.json"):
            load_bundle_dir(tmp_path / "esp32")

    def test_a_missing_directory_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(AgentBundleError):
            load_bundle_dir(tmp_path / "nope")

    def test_an_unusable_directory_name_is_refused(self, tmp_path: Path) -> None:
        bundle_dir = write_bundle(tmp_path, "esp32", dir_name="ESP32")
        with pytest.raises(AgentBundleError):
            load_bundle_dir(bundle_dir)
