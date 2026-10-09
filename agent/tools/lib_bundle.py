"""Turn a PlatformIO build of the Arduino library example into a QEMU-flashable bundle.

R3-fw-3. `qemu_image.py flash` takes a bundle directory with a `manifest.json` that
names every part and its offset, plus the `ff_cfg` partition. `make_manifest.py` writes
that for the ESP-IDF agent from `flasher_args.json`; a PlatformIO Arduino build has no such
file, so this script writes the same shape from what PlatformIO does leave behind. Standard
library only, like its siblings: it runs on the host and in the ESP-IDF image alike.

**Offsets are read, never typed** (the rule `make_manifest.py` and `qemu_image.py` keep).
Every one comes from the build:

* `idedata.json` (`pio run -t idedata`) -> `extra.flash_images`: the bootloader, the
  partition table and `boot_app0.bin` (the Arduino otadata image), each with the offset the
  upload writes it to;
* the app's offset is where the build itself put `firmware.bin` inside
  `firmware.factory.bin`, the merged image PlatformIO writes from the same offsets the
  upload uses (`idedata.json`'s `application_offset` when present, checked against it);
* the partition table is decoded from the BUILT `partitions.bin` with `make_manifest.py`'s
  own decoder (imported, never copied), never read off `partitions.csv`.

It refuses, rather than writes a bundle that boots wrong:

* the decoded table's `otadata` is not where `boot_app0.bin` is written, or its `ota_0` is
  not where the app is written. Either one is the Arduino-recipe trap the layout ADR
  (design/decisions/arduino-gets-its-own-layout-id.md) was written about: a green build
  whose app lands across someone else's partition;
* no `ff_cfg`, a `factory` partition, or unequal OTA slots (`make_manifest.py`'s checks);
* the table's fingerprint is not `ab-4m-arduino-v1`'s. The id is written only when the
  geometry IS that layout, so a manifest can never call a different table by its name.

The output goes under the harness's `lib-qemu/.pio/` (ignored), NEVER `agent/dist/`: everything in
`agent/dist/` is an agent bundle that the size budget, the freshness gate and the publisher
iterate over.

    python3 agent/tools/lib_bundle.py --build-dir lib-qemu/.pio/build/esp32-qemu \\
        --out lib-qemu/.pio/bundle/esp32-qemu
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import sys
from pathlib import Path
from typing import Any

from make_manifest import (
    CHIP_FAMILY,
    MANIFEST_SCHEMA,
    PARTITION_SUBTYPE_OTA_0,
    PARTITION_TYPE_APP,
    PARTITION_TYPE_DATA,
    BuildError,
    config_partition,
    decode_partition_table,
    ota_slot_size,
    sha256_of,
)

# spec/device-protocol.md -> Partition layouts. Retyped with the spec named, as
# firmware/manifest.py::BUILTIN_LAYOUTS does; tests/test_arduino_library.py holds the
# three equal.
ARDUINO_LAYOUT_ID = "ab-4m-arduino-v1"
ARDUINO_LAYOUT_SHA256 = "05528998ae17fb6a7a5741443f9a7a4720c766f370fefc30814cbc3e391c1fc4"

# The data subtype of otadata (components/partition_table: data/ota = 0x00).
PARTITION_SUBTYPE_OTADATA = 0x00

# What PlatformIO calls each flash image, and the bundle names `make_manifest.py` uses.
FLASH_IMAGE_NAMES = {
    "bootloader.bin": ("bootloader", "bootloader.bin"),
    "partitions.bin": ("partition-table", "partition-table.bin"),
    "boot_app0.bin": ("ota-data", "ota-data-initial.bin"),
}
APP_FILE = "firmware.bin"
FACTORY_FILE = "firmware.factory.bin"

# An app partition starts on a 64 KB boundary (ESP-IDF's partition table rules), which is
# where the app is searched for in the factory image when idedata.json does not say.
APP_ALIGN = 0x10000

# esp_image_header_t (16 bytes) + esp_image_extended_header_t: chip_id is a u16 at 12.
IMAGE_MAGIC = 0xE9
IMAGE_CHIP_ID = struct.Struct("<H")
IMAGE_CHIP_ID_OFFSET = 12
CHIP_IDS = {0: "esp32", 2: "esp32s2", 5: "esp32c3", 9: "esp32s3", 13: "esp32c6"}


def layout_fingerprint(rows: list[dict[str, Any]]) -> str:
    """spec/device-protocol.md's `partition_table_sha256`: one `type:subtype:offset:size\\n`
    line per partition, decimal, sorted by offset. Geometry only, never labels or flags."""
    lines = "".join(
        f"{row['type']}:{row['subtype']}:{row['offset']}:{row['size']}\n"
        for row in sorted(rows, key=lambda row: int(row["offset"]))
    )
    return hashlib.sha256(lines.encode("ascii")).hexdigest()


def read_idedata(build_dir: Path) -> dict[str, Any]:
    path = build_dir / "idedata.json"
    if not path.is_file():
        raise BuildError(f"{path} is missing — run: pio run -e <env> -t idedata")
    loaded: Any = json.loads(path.read_text())
    if not isinstance(loaded, dict) or not isinstance(loaded.get("extra"), dict):
        raise BuildError(f"{path} has no `extra` object; the PlatformIO pin changed its shape")
    return loaded


def flash_images(idedata: dict[str, Any]) -> dict[str, tuple[int, Path]]:
    """`{bundle part name: (offset, source file)}` for every extra flash image."""
    images = idedata["extra"].get("flash_images")
    if not isinstance(images, list) or not images:
        raise BuildError("idedata.json lists no flash_images; nothing says where to write them")
    found: dict[str, tuple[int, Path]] = {}
    for image in images:
        source = Path(str(image["path"]))
        if source.name not in FLASH_IMAGE_NAMES:
            raise BuildError(
                f"idedata.json flashes {source.name}, which this script does not know; "
                "an unknown image at an unknown offset is not something to guess about"
            )
        if not source.is_file():
            raise BuildError(f"{source}, named by idedata.json, does not exist")
        name = FLASH_IMAGE_NAMES[source.name][0]
        found[name] = (int(str(image["offset"]), 0), source)
    missing = sorted({name for name, _file in FLASH_IMAGE_NAMES.values()} - set(found))
    if missing:
        raise BuildError(f"idedata.json does not flash {missing}")
    return found


def app_offset(idedata: dict[str, Any], app: bytes, factory: bytes) -> int:
    """Where the build writes the app: located in the merged factory image, and checked
    against idedata.json's `application_offset` when PlatformIO recorded one."""
    declared = idedata["extra"].get("application_offset")
    if declared is not None:
        offset = int(str(declared), 0)
        if factory[offset : offset + len(app)] != app:
            raise BuildError(
                f"idedata.json says the app goes to 0x{offset:x}, but {FACTORY_FILE} does not "
                "hold firmware.bin there"
            )
        return offset
    for offset in range(APP_ALIGN, len(factory), APP_ALIGN):
        if factory[offset : offset + len(app)] == app:
            return offset
    raise BuildError(f"firmware.bin is not inside {FACTORY_FILE} at any 64 KB boundary")


def target_of(app: bytes) -> str:
    """The chip the app image was built for, from its own header."""
    if len(app) < IMAGE_CHIP_ID_OFFSET + IMAGE_CHIP_ID.size or app[0] != IMAGE_MAGIC:
        raise BuildError("firmware.bin is not an ESP application image (no 0xE9 magic)")
    (chip_id,) = IMAGE_CHIP_ID.unpack_from(app, IMAGE_CHIP_ID_OFFSET)
    target = CHIP_IDS.get(chip_id)
    if target is None or target not in CHIP_FAMILY:
        raise BuildError(f"firmware.bin is for chip id {chip_id}, which this script does not know")
    return target


def _row(rows: list[dict[str, Any]], ptype: int, subtype: int, what: str) -> dict[str, Any]:
    matches = [row for row in rows if row["type"] == ptype and row["subtype"] == subtype]
    if len(matches) != 1:
        raise BuildError(f"the built partition table has {len(matches)} {what} partitions, not 1")
    return matches[0]


def build_bundle(build_dir: Path, out_dir: Path) -> dict[str, Any]:
    idedata = read_idedata(build_dir)
    images = flash_images(idedata)
    app_path = build_dir / APP_FILE
    factory_path = build_dir / FACTORY_FILE
    for path in (app_path, factory_path):
        if not path.is_file():
            raise BuildError(f"{path} is missing — did `pio run` finish?")
    app = app_path.read_bytes()
    factory = factory_path.read_bytes()

    # Every extra image must sit in the factory image exactly where idedata.json says: the
    # two are written from the same offsets, so a disagreement means the inputs are stale.
    for name, (offset, source) in images.items():
        if factory[offset : offset + source.stat().st_size] != source.read_bytes():
            raise BuildError(f"{FACTORY_FILE} does not hold {source.name} at 0x{offset:x} ({name})")
    app_at = app_offset(idedata, app, factory)

    rows = decode_partition_table(images["partition-table"][1])
    slot_size = ota_slot_size(rows)
    ota_0 = _row(rows, PARTITION_TYPE_APP, PARTITION_SUBTYPE_OTA_0, "ota_0")
    otadata = _row(rows, PARTITION_TYPE_DATA, PARTITION_SUBTYPE_OTADATA, "otadata")
    if ota_0["offset"] != app_at:
        raise BuildError(
            f"the app is written to 0x{app_at:x} but the table's ota_0 starts at "
            f"0x{ota_0['offset']:x}: this board would boot into the middle of another partition"
        )
    if otadata["offset"] != images["ota-data"][0]:
        raise BuildError(
            f"boot_app0.bin is written to 0x{images['ota-data'][0]:x} but the table's otadata "
            f"is at 0x{otadata['offset']:x}: the upload would overwrite another partition"
        )
    if len(app) > slot_size:
        raise BuildError(f"firmware.bin is {len(app)} bytes but an OTA slot is {slot_size}")
    cfg = config_partition(rows)
    fingerprint = layout_fingerprint(rows)
    if fingerprint != ARDUINO_LAYOUT_SHA256:
        raise BuildError(
            f"the built table's fingerprint is {fingerprint}, not {ARDUINO_LAYOUT_ID}'s "
            f"({ARDUINO_LAYOUT_SHA256}, spec/device-protocol.md). Ship the example's "
            "partitions.csv unchanged; a different map needs a new layout id"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    placed = [(name, offset, source) for name, (offset, source) in images.items()]
    placed.append(("app", app_at, app_path))
    parts: list[dict[str, Any]] = []
    for name, offset, source in sorted(placed, key=lambda item: item[1]):
        filename = "app.bin" if name == "app" else FLASH_IMAGE_NAMES[source.name][1]
        destination = out_dir / filename
        shutil.copyfile(source, destination)
        parts.append(
            {
                "name": name,
                "path": filename,
                "offset": offset,
                "size": destination.stat().st_size,
                "sha256": sha256_of(destination),
            }
        )

    target = target_of(app)
    return {
        "schema": MANIFEST_SCHEMA,
        "producer": "lib_bundle",
        "target": target,
        "chip_family": CHIP_FAMILY[target],
        "partition_layout": ARDUINO_LAYOUT_ID,
        "partition_table_sha256": fingerprint,
        "ota_slot_size": slot_size,
        "config_partition": cfg,
        "parts": parts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True, help=".pio/build/<env>")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = build_bundle(args.build_dir, args.out)
    except (BuildError, KeyError, OSError, ValueError) as exc:
        print(f"lib_bundle: {exc}", file=sys.stderr)
        return 1
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"lib_bundle: {manifest['target']} {manifest['partition_layout']} -> {args.out}")
    for part in manifest["parts"]:
        print(f"  0x{part['offset']:06x}  {part['path']:<22} {part['size']:>8} bytes")
    cfg = manifest["config_partition"]
    print(f"  0x{cfg['offset']:06x}  ({cfg['label']}, {cfg['size']} bytes, flashed per board)")
    print(f"  partition_table_sha256 {manifest['partition_table_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
