"""`fleetforge.firmware` — loading, verifying and indexing agent bundles.

No toolchain and no ESP-IDF: every fixture here is a synthetic bundle written to a tmp
directory, because what is under test is the *reader*, not the build. The build's own
gate is `just agent-verify <target>` (see `tests/test_agent_partitions.py`'s docstring
for why that one cannot live in `just test`).

The rejections are the point. Each of the bad-bundle cases below is a board that would
otherwise be bricked or unflashable, and each must be a **dropped bundle with a warning**
rather than an exception: one corrupt target must not stop the API from starting and
serving the other three.
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
    FirmwareCatalog,
    load_bundles,
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
) -> Path:
    """Write a synthetic but structurally faithful bundle to `root/<target>`."""
    bundle_dir = root / target
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


def dropped_targets(records: list[logging.LogRecord]) -> list[str]:
    return [record.getMessage() for record in records if record.levelno >= logging.WARNING]


class TestConfigPartition:
    """`config_partition` is what stops `R0-fe-3` typing `0x12000`, and it is additive."""

    def test_is_carried_through_to_the_bundle(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, "esp32")
        bundle = FirmwareCatalog.load(tmp_path).bundle("esp32")
        assert bundle is not None
        assert bundle.config_partition is not None
        assert bundle.config_partition.label == "ff_cfg"
        assert bundle.config_partition.offset == 0x12000
        assert bundle.config_partition.size == 0x1000

    def test_a_bundle_without_one_still_loads(self, tmp_path: Path) -> None:
        """A bundle built before R0-fw-1 is on disk and in the registry. Refusing it would
        take the flasher offline for a field only the new flow needs."""
        bundle_dir = write_bundle(tmp_path, "esp32")
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
        del manifest["config_partition"]
        (bundle_dir / "manifest.json").write_text(json.dumps(manifest))

        bundle = FirmwareCatalog.load(tmp_path).bundle("esp32")
        assert bundle is not None
        assert bundle.config_partition is None

    def test_a_malformed_one_drops_the_bundle(self, tmp_path: Path) -> None:
        """Present but wrong is not the same as absent: an offset of `null` reaching the
        flasher would write a config blob over whatever sits at address 0."""
        bundle_dir = write_bundle(tmp_path, "esp32")
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
        manifest["config_partition"] = {"label": "ff_cfg", "offset": None, "size": 4096}
        (bundle_dir / "manifest.json").write_text(json.dumps(manifest))

        assert FirmwareCatalog.load(tmp_path).bundle("esp32") is None


class TestBuildIdentity:
    """S0-infra-3. Two fields the reader carries and never recomputes.

    The behaviour that matters here is the *degradation*: an old bundle is still a
    flashable bundle. Dropping it would take the flasher offline over a field nothing on
    the flash path reads — the failure mode this module's docstring exists to prevent —
    so the answer is one warning and a `None`.
    """

    def test_both_are_carried_through_to_the_bundle(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, "esp32")
        bundle = FirmwareCatalog.load(tmp_path).bundle("esp32")
        assert bundle is not None
        assert bundle.config_sha256 == "c0" * 32
        assert bundle.build_digest == "b1" * 32

    def test_a_bundle_without_them_loads_with_a_warning(self, tmp_path: Path) -> None:
        """A bundle built before S0-infra-3 is old, not invalid."""
        bundle_dir = write_bundle(tmp_path, "esp32")
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
        del manifest["config_sha256"]
        del manifest["build_digest"]
        (bundle_dir / "manifest.json").write_text(json.dumps(manifest))

        with capture_logs() as records:
            catalog = FirmwareCatalog.load(tmp_path)

        bundle = catalog.bundle("esp32")
        assert bundle is not None, "an old bundle must still be servable"
        assert bundle.config_sha256 is None
        assert bundle.build_digest is None
        warnings = dropped_targets(records)
        assert any("build identity" in message for message in warnings)
        assert not any("dropped" in message for message in warnings)

    def test_a_present_bundle_warns_about_nothing(self, tmp_path: Path) -> None:
        """Vacuity guard for the test above: the warning must be about the missing field,
        not something this fixture does on every load."""
        write_bundle(tmp_path, "esp32")
        with capture_logs() as records:
            FirmwareCatalog.load(tmp_path)
        assert dropped_targets(records) == []

    @pytest.mark.parametrize("field", ["config_sha256", "build_digest"])
    def test_a_malformed_digest_is_refused(self, tmp_path: Path, field: str) -> None:
        """Absent is old; truncated or non-hex is corrupt, and a corrupt identity is worse
        than none — it would be compared against another bundle's and believed."""
        write_bundle(tmp_path, "esp32", manifest_overrides={field: "not-a-digest"})
        with capture_logs() as records:
            catalog = FirmwareCatalog.load(tmp_path)
        assert catalog.targets == ()
        assert any("esp32" in message for message in dropped_targets(records))


class TestHappyPath:
    def test_loads_every_bundle_sorted_by_target(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, "esp32")
        write_bundle(tmp_path, "esp32c6")

        catalog = FirmwareCatalog.load(tmp_path)

        assert catalog.targets == ("esp32", "esp32c6")
        assert bool(catalog) is True

    def test_parts_are_indexed_by_logical_name_and_sorted_by_offset(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, "esp32")
        bundle = FirmwareCatalog.load(tmp_path).bundle("esp32")
        assert bundle is not None

        assert [part.name for part in bundle.parts] == [
            "bootloader",
            "partition-table",
            "ota-data",
            "app",
        ]
        assert [part.offset for part in bundle.parts] == [4096, 32768, 61440, 131072]
        assert bundle.part("app") is not None
        assert bundle.part("nope") is None

    def test_the_chip_specific_bootloader_offset_survives_the_round_trip(
        self, tmp_path: Path
    ) -> None:
        """The one number a wrong implementation would normalise away.

        `0x1000` on ESP32, `0x0` on the C6. A loader that "helpfully" defaulted it would
        produce an image that flashes cleanly and never boots, on half the fleet.
        """
        write_bundle(tmp_path, "esp32")
        write_bundle(tmp_path, "esp32c6")
        catalog = FirmwareCatalog.load(tmp_path)

        esp32 = catalog.bundle("esp32")
        esp32c6 = catalog.bundle("esp32c6")
        assert esp32 is not None and esp32c6 is not None
        assert esp32.part("bootloader").offset == 4096  # type: ignore[union-attr]
        assert esp32c6.part("bootloader").offset == 0  # type: ignore[union-attr]

    def test_part_paths_are_absolute_and_inside_the_bundle(self, tmp_path: Path) -> None:
        bundle_dir = write_bundle(tmp_path, "esp32")
        bundle = FirmwareCatalog.load(tmp_path).bundle("esp32")
        assert bundle is not None
        for part in bundle.parts:
            assert part.path.is_absolute()
            assert part.path.is_relative_to(bundle_dir)

    def test_etag_is_a_strong_content_hash(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, "esp32")
        bundle = FirmwareCatalog.load(tmp_path).bundle("esp32")
        assert bundle is not None
        app = bundle.part("app")
        assert app is not None
        assert app.etag == f'"sha256-{app.sha256}"'


class TestRejections:
    """Every case here is a bundle that must be DROPPED, with a warning, not raised."""

    def test_a_wrong_sha256_drops_the_bundle(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, "esp32", part_overrides={"app": {"sha256": "b" * 64}})
        with capture_logs() as records:
            catalog = FirmwareCatalog.load(tmp_path)
        assert catalog.targets == ()
        assert any(
            "esp32" in message and "sha256" in message for message in dropped_targets(records)
        )

    def test_a_flipped_byte_drops_the_bundle(self, tmp_path: Path) -> None:
        """The corruption the manifest exists to catch — a brick on someone's desk."""
        bundle_dir = write_bundle(tmp_path, "esp32")
        blob = bytearray((bundle_dir / "app.bin").read_bytes())
        blob[0] ^= 0xFF
        (bundle_dir / "app.bin").write_bytes(bytes(blob))

        with capture_logs() as records:
            catalog = FirmwareCatalog.load(tmp_path)
        assert catalog.targets == ()
        assert dropped_targets(records)

    def test_a_wrong_size_drops_the_bundle(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, "esp32", part_overrides={"app": {"size": 999999}})
        with capture_logs() as records:
            assert FirmwareCatalog.load(tmp_path).targets == ()
        assert dropped_targets(records)

    def test_a_missing_part_file_drops_the_bundle(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, "esp32", skip_files=("app.bin",))
        with capture_logs() as records:
            assert FirmwareCatalog.load(tmp_path).targets == ()
        assert dropped_targets(records)

    def test_a_part_absent_from_the_manifest_drops_the_bundle(self, tmp_path: Path) -> None:
        bundle_dir = write_bundle(tmp_path, "esp32")
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
        manifest["parts"] = [p for p in manifest["parts"] if p["name"] != "ota-data"]
        (bundle_dir / "manifest.json").write_text(json.dumps(manifest))

        with capture_logs() as records:
            assert FirmwareCatalog.load(tmp_path).targets == ()
        assert any("ota-data" in message for message in dropped_targets(records))

    def test_a_superseded_partition_layout_drops_the_bundle(self, tmp_path: Path) -> None:
        """`spec/device-protocol.md` fixes the layout; a board flashed with another
        announces something the server's capability check does not understand."""
        write_bundle(tmp_path, "esp32", manifest_overrides={"partition_layout": "ab-2m-v0"})
        with capture_logs() as records:
            assert FirmwareCatalog.load(tmp_path).targets == ()
        assert any("ab-2m-v0" in message for message in dropped_targets(records))

    def test_a_wrong_ota_slot_size_drops_the_bundle(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, "esp32", manifest_overrides={"ota_slot_size": 1048576})
        with capture_logs() as records:
            assert FirmwareCatalog.load(tmp_path).targets == ()
        assert dropped_targets(records)

    def test_a_manifest_target_that_disagrees_with_its_directory_drops_the_bundle(
        self, tmp_path: Path
    ) -> None:
        write_bundle(tmp_path, "esp32", manifest_overrides={"target": "esp32c3"})
        with capture_logs() as records:
            assert FirmwareCatalog.load(tmp_path).targets == ()
        assert dropped_targets(records)

    def test_an_unknown_schema_drops_the_bundle(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, "esp32", manifest_overrides={"schema": 2})
        with capture_logs() as records:
            assert FirmwareCatalog.load(tmp_path).targets == ()
        assert dropped_targets(records)

    def test_unparseable_json_drops_the_bundle(self, tmp_path: Path) -> None:
        bundle_dir = write_bundle(tmp_path, "esp32")
        (bundle_dir / "manifest.json").write_text("{not json")
        with capture_logs() as records:
            assert FirmwareCatalog.load(tmp_path).targets == ()
        assert dropped_targets(records)

    @pytest.mark.parametrize(
        "hostile_path",
        ["../../etc/passwd", "/etc/passwd", "sub/dir.bin", ".hidden", "app.bin\x00"],
    )
    def test_a_manifest_path_that_is_not_a_bare_filename_drops_the_bundle(
        self, tmp_path: Path, hostile_path: str
    ) -> None:
        write_bundle(tmp_path, "esp32", part_overrides={"app": {"path": hostile_path}})
        with capture_logs() as records:
            assert FirmwareCatalog.load(tmp_path).targets == ()
        assert dropped_targets(records)

    def test_a_symlink_out_of_the_bundle_drops_it(self, tmp_path: Path) -> None:
        """`_resolve_part_path`'s second layer: the name is legal, the target is not."""
        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"not firmware")
        bundle_dir = write_bundle(tmp_path, "esp32")
        (bundle_dir / "app.bin").unlink()
        (bundle_dir / "app.bin").symlink_to(outside)

        with capture_logs() as records:
            assert FirmwareCatalog.load(tmp_path).targets == ()
        assert dropped_targets(records)

    def test_one_bad_bundle_does_not_take_the_good_ones_with_it(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, "esp32")
        write_bundle(tmp_path, "esp32c6", part_overrides={"app": {"sha256": "c" * 64}})

        with capture_logs() as records:
            catalog = FirmwareCatalog.load(tmp_path)

        assert catalog.targets == ("esp32",)
        assert any("esp32c6" in message for message in dropped_targets(records))


class TestEmptyAndMissing:
    def test_an_empty_directory_is_an_empty_catalog(self, tmp_path: Path) -> None:
        with capture_logs() as records:
            catalog = FirmwareCatalog.load(tmp_path)
        assert catalog.targets == ()
        assert bool(catalog) is False
        assert dropped_targets(records), "an empty bundle dir must warn, not pass silently"

    def test_a_missing_directory_does_not_raise(self, tmp_path: Path) -> None:
        with capture_logs() as records:
            catalog = FirmwareCatalog.load(tmp_path / "nope")
        assert catalog.targets == ()
        assert dropped_targets(records)

    def test_none_is_an_empty_catalog(self) -> None:
        assert FirmwareCatalog.load(None).targets == ()

    def test_stray_files_and_dirs_are_ignored(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, "esp32")
        (tmp_path / "README.md").write_text("not a bundle")
        (tmp_path / "scratch").mkdir()
        assert FirmwareCatalog.load(tmp_path).targets == ("esp32",)

    def test_load_bundles_returns_a_list(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, "esp32")
        assert [bundle.target for bundle in load_bundles(tmp_path)] == ["esp32"]
