"""Build identity in the agent manifest (S0-infra-3): `config_sha256` + `build_digest`.

The question this exists to make answerable is *"which build produced this bundle?"*.
The manifest has always carried `agent_version`, `source_commit`, `idf_version` and a
digest-pinned `idf_image`, and none of that distinguishes two builds of the **same
commit** with a different `sdkconfig` — which is exactly the pair S0-fw-3 spent three
sessions telling apart by correlating a bundle's `built_at` against `git log`.

So the load-bearing assertion here is not "the field is present". It is the pair:

* a one-line configuration difference, everything else identical → **different** digests;
* identical inputs at a different wall-clock → **identical** `build_digest`.

The second is the one nothing else would catch, and it is why `built_at` is excluded from
the digest: a timestamp in there would make every rebuild look like a new build and the
field would be decoration.

`make_manifest.py` is imported **by path**, the idiom `test_ff_cfg.py` established:
`agent/` is not a package and must not become one, because it ships inside the ESP-IDF
builder image where `fleetforge` does not exist. The build directory below is synthesised
rather than produced by a toolchain, for the same reason `test_firmware_catalog.py`
synthesises bundles — `just test` must run on a box with no ESP-IDF and no `agent/dist`.
The real two-pass build is `just agent-build`, and it is the acceptance evidence for this
task; this file is the regression net under it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import struct
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MAKE_MANIFEST_PY = REPO_ROOT / "agent" / "tools" / "make_manifest.py"

# Retyped from the formats, never imported from the module under test — the same rule
# `test_ff_cfg.py` and `test_agent_partitions.py` follow. A test that gets its constants
# from the code it checks agrees with that code by construction.
APP_DESC_OFFSET = 0x20
APP_DESC_MAGIC = 0xABCD5432
APP_DESC_STRUCT = "<II8x32s32s16s16s32s"
PARTITION_ENTRY = struct.Struct("<2sBBLL16sL")
PARTITION_MAGIC = b"\xaa\x50"
PARTITION_MD5_MAGIC = b"\xeb\xeb"

TYPE_APP, TYPE_DATA = 0x00, 0x01
SUBTYPE_OTA_0, SUBTYPE_OTA_1 = 0x10, 0x11
SUBTYPE_FF_CFG = 0x40

# `ab-4m-v1`, as `spec/device-protocol.md` freezes it. The numbers matter only in that
# the emitter refuses a table that is not A/B; they are not the subject of this file.
OTA_SLOT_SIZE = 1966080
OTA_0_OFFSET = 0x20000
FF_CFG_OFFSET = 0x12000
FF_CFG_SIZE = 0x1000

PART_OFFSETS = {
    "bootloader": 0x1000,
    "partition_table": 0x8000,
    "otadata": 0xF000,
    "app": OTA_0_OFFSET,
}

BASE_CONFIG = "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y\nCONFIG_ESP_DEFAULT_CPU_FREQ_MHZ=160\n"


def _load_make_manifest() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_manifest_tool", MAKE_MANIFEST_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_manifest = _load_make_manifest()


def _app_bin(payload: bytes = b"app-payload") -> bytes:
    """An app image with a real `esp_app_desc_t` where the linker puts one."""
    desc = struct.pack(
        APP_DESC_STRUCT,
        APP_DESC_MAGIC,
        0,
        b"0.2.0",
        b"fleetforge-agent",
        b"12:00:00",
        b"Sep 14 2026",
        b"v5.5.5",
    )
    head = b"\x00" * APP_DESC_OFFSET
    return head + desc + payload


def _partition_table() -> bytes:
    """A minimal but genuine `ab-4m-v1`-shaped table: ota_0, ota_1, ff_cfg."""
    rows = [
        (TYPE_APP, SUBTYPE_OTA_0, OTA_0_OFFSET, OTA_SLOT_SIZE, b"ota_0"),
        (TYPE_APP, SUBTYPE_OTA_1, OTA_0_OFFSET + OTA_SLOT_SIZE, OTA_SLOT_SIZE, b"ota_1"),
        (TYPE_DATA, SUBTYPE_FF_CFG, FF_CFG_OFFSET, FF_CFG_SIZE, b"ff_cfg"),
    ]
    blob = b"".join(
        PARTITION_ENTRY.pack(PARTITION_MAGIC, ptype, subtype, offset, size, label, 0)
        for ptype, subtype, offset, size, label in rows
    )
    # The real table ends with an MD5 entry; the decoder stops there, so the terminator is
    # part of the fixture's fidelity rather than decoration.
    return blob + PARTITION_MD5_MAGIC + b"\x00" * (PARTITION_ENTRY.size - 2)


def write_build_dir(
    root: Path,
    *,
    sdkconfig: str = BASE_CONFIG,
    app_payload: bytes = b"app-payload",
) -> tuple[Path, Path]:
    """A directory shaped like `idf.py build` left it. Returns `(build_dir, partitions_csv)`."""
    build_dir = root / "build"
    build_dir.mkdir(parents=True, exist_ok=True)

    (build_dir / "bootloader.bin").write_bytes(b"bootloader-payload")
    (build_dir / "partition-table.bin").write_bytes(_partition_table())
    (build_dir / "ota-data-initial.bin").write_bytes(b"\xff" * 8192)
    (build_dir / "app.bin").write_bytes(_app_bin(app_payload))

    (build_dir / "flasher_args.json").write_text(
        json.dumps(
            {
                "bootloader": {"offset": hex(PART_OFFSETS["bootloader"]), "file": "bootloader.bin"},
                "partition-table": {
                    "offset": hex(PART_OFFSETS["partition_table"]),
                    "file": "partition-table.bin",
                },
                "otadata": {"offset": hex(PART_OFFSETS["otadata"]), "file": "ota-data-initial.bin"},
                "app": {"offset": hex(PART_OFFSETS["app"]), "file": "app.bin"},
                "flash_settings": {"flash_size": "4MB"},
            }
        )
    )

    # `idf.py` writes the resolved config next to the project, and records where — which
    # is why `make_manifest.py` reads the location rather than typing it.
    resolved = root / "sdkconfig"
    resolved.write_text(sdkconfig)
    (build_dir / "project_description.json").write_text(
        json.dumps({"target": "esp32", "config_file": str(resolved)})
    )

    partitions_csv = root / "partitions.csv"
    partitions_csv.write_text('# layout id "ab-4m-v1"\nota_0, app, ota_0, 0x20000, 0x1E0000,\n')
    return build_dir, partitions_csv


def emit(
    root: Path,
    *,
    sdkconfig: str = BASE_CONFIG,
    app_payload: bytes = b"app-payload",
    source_commit: str = "0" * 40,
    idf_image: str = "espressif/idf:v5.5.5@sha256:" + "a" * 64,
) -> dict[str, Any]:
    """Run the real emitter over a synthesised build and return the manifest it would write."""
    build_dir, partitions_csv = write_build_dir(root, sdkconfig=sdkconfig, app_payload=app_payload)
    out_dir = root / "out"
    args = make_manifest.argparse.Namespace(
        build_dir=build_dir,
        out=out_dir,
        partitions=partitions_csv,
        source_commit=source_commit,
        idf_image=idf_image,
    )
    manifest: dict[str, Any] = make_manifest.build_manifest(args)
    return manifest


class TestConfigSha256:
    def test_is_the_hash_of_the_bundled_resolved_config(self, tmp_path: Path) -> None:
        """The bundle's own copy, not the source file — the copy is what ships."""
        manifest = emit(tmp_path)
        shipped = (tmp_path / "out" / "sdkconfig.resolved").read_bytes()
        assert manifest["config_sha256"] == hashlib.sha256(shipped).hexdigest()
        assert shipped.decode() == BASE_CONFIG

    def test_one_line_of_configuration_changes_it(self, tmp_path: Path) -> None:
        a = emit(tmp_path / "a")
        b = emit(tmp_path / "b", sdkconfig=BASE_CONFIG.replace("160", "80"))
        assert a["config_sha256"] != b["config_sha256"]


class TestBuildDigest:
    """The acceptance criterion, and the exclusion that makes it mean anything."""

    def test_two_builds_of_one_commit_differing_only_in_config_are_distinguishable(
        self, tmp_path: Path
    ) -> None:
        """The S0-fw-3 case: same commit, same IDF, 160 MHz vs 80 MHz.

        Every provenance field the manifest carried *before* this task is equal across
        these two — that is the whole point, and the assertion below says so explicitly
        rather than leaving it implied.
        """
        a = emit(tmp_path / "a")
        b = emit(tmp_path / "b", sdkconfig=BASE_CONFIG.replace("160", "80"))

        for field in ("source_commit", "agent_version", "idf_version", "idf_image", "target"):
            assert a[field] == b[field], f"{field} should be identical for this pair"

        assert a["config_sha256"] != b["config_sha256"]
        assert a["build_digest"] != b["build_digest"]

    def test_identical_inputs_produce_an_identical_digest(self, tmp_path: Path) -> None:
        """`built_at` is excluded, so a rebuild of the same thing IS the same build.

        Without this the digest would change on every run and could never answer "is the
        bundle on my bench the one you built?".
        """
        a = emit(tmp_path / "a")
        b = emit(tmp_path / "b")
        assert a["built_at"] is not None
        assert a["build_digest"] == b["build_digest"]

    def test_changed_bytes_change_it_with_the_config_untouched(self, tmp_path: Path) -> None:
        """The other half: same configuration, different artefact."""
        a = emit(tmp_path / "a")
        b = emit(tmp_path / "b", app_payload=b"app-payload-rebuilt")
        assert a["config_sha256"] == b["config_sha256"]
        assert a["build_digest"] != b["build_digest"]

    def test_a_different_commit_changes_it(self, tmp_path: Path) -> None:
        a = emit(tmp_path / "a")
        b = emit(tmp_path / "b", source_commit="1" * 40)
        assert a["build_digest"] != b["build_digest"]

    def test_it_is_recomputable_from_the_manifest_alone(self, tmp_path: Path) -> None:
        """What `verify_bundle.py` does on every `just agent-verify`: trust nothing."""
        manifest = emit(tmp_path)
        assert make_manifest.build_identity(manifest) == manifest["build_digest"]

    def test_part_order_does_not_move_it(self, tmp_path: Path) -> None:
        """Parts are sorted by name inside the digest, so the manifest's own ordering (by
        offset, for the flasher) cannot leak into the identity."""
        manifest = emit(tmp_path)
        shuffled = dict(manifest)
        shuffled["parts"] = list(reversed(manifest["parts"]))
        assert make_manifest.build_identity(shuffled) == manifest["build_digest"]

    def test_it_refuses_to_guess_at_a_missing_field(self, tmp_path: Path) -> None:
        """A digest computed over a hole would be a digest that silently covers less."""
        manifest = emit(tmp_path)
        without_config = {k: v for k, v in manifest.items() if k != "config_sha256"}
        with pytest.raises(make_manifest.BuildError, match="config_sha256"):
            make_manifest.build_identity(without_config)


class TestWhatIsEmitted:
    def test_both_fields_are_64_hex_characters(self, tmp_path: Path) -> None:
        """The reader's `Sha256Hex` pattern refuses anything else, so the writer must not
        produce anything else — a bundle the API drops is a flasher with nothing to offer."""
        manifest = emit(tmp_path)
        for field in ("config_sha256", "build_digest"):
            assert len(manifest[field]) == 64
            assert set(manifest[field]) <= set("0123456789abcdef")

    def test_the_schema_is_not_bumped(self, tmp_path: Path) -> None:
        """Both fields are additive and optional; an old bundle stays loadable, so there is
        nothing for a reader to switch on. `MANIFEST_SCHEMA` bumps when one becomes
        required."""
        assert emit(tmp_path)["schema"] == 1
