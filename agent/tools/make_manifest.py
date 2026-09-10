"""Turn an ESP-IDF build directory into a flashable Fleetforge agent bundle.

Runs **inside** the pinned ESP-IDF builder (`agent/Dockerfile`), as the last step of the
same `RUN` that built the project. Standard library only: the builder image is not ours
to add packages to, and this script has to keep working when the IDF pin is bumped.

What it produces in `--out`:

    bootloader.bin  partition-table.bin  ota-data-initial.bin  app.bin
    sdkconfig.resolved   manifest.json

**Offsets are never typed, only read.** The bootloader lives at `0x1000` on ESP32 and at
`0x0` on the S3/C3/C6 — a hardcoded offset produces an image that flashes cleanly and
never boots, on half the targets, silently. ESP-IDF writes the authoritative
`{offset: file}` map to `build/flasher_args.json` for the target it just built, so that
file is the only source of an offset anywhere in this repo (`src/`, the frontend and the
justfile all carry the manifest's numbers through untouched).

Likewise `agent_version` and `idf_version` are parsed out of the **built binary's**
`esp_app_desc_t`, not out of `version.txt` or an environment variable: what the manifest
claims is then what the board will report in `up/announce`, even if the two ever diverge.

`ota_slot_size` is decoded from the generated `partition-table.bin`, not copied from
`partitions.csv`, for the same reason — and this script refuses to emit a bundle whose
decoded table has a `factory` partition or mismatched OTA slots, because a bundle that
gets to `agent/dist/` gets flashed onto a board (`CRITICAL.md`: wrong at R0 = recall).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import struct
import sys
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = 1

# ESP Web Tools / esptool-js spelling, so R0-fe-3 hands the manifest straight to the
# flasher with no translation table of its own.
CHIP_FAMILY = {
    "esp32": "ESP32",
    "esp32s2": "ESP32-S2",
    "esp32s3": "ESP32-S3",
    "esp32c2": "ESP32-C2",
    "esp32c3": "ESP32-C3",
    "esp32c6": "ESP32-C6",
    "esp32h2": "ESP32-H2",
    "esp32p4": "ESP32-P4",
}

# The keys ESP-IDF writes into flasher_args.json, and the stable logical names the API
# route and the flasher use. `path` in the manifest is always one of these bare names —
# never a build-directory path, which changes shape between IDF versions.
PARTS = [
    ("bootloader", "bootloader.bin"),
    ("partition-table", "partition-table.bin"),
    ("otadata", "ota-data-initial.bin"),
    ("app", "app.bin"),
]
LOGICAL_NAME = {"otadata": "ota-data"}

# esp_app_desc_t (esp_app_desc.h), which the linker places at offset 0x20 of the app
# image: magic, secure_version, 2 reserved words, then the strings.
APP_DESC_OFFSET = 0x20
APP_DESC_MAGIC = 0xABCD5432
APP_DESC_STRUCT = "<II8x32s32s16s16s32s"

# Partition table binary format (components/partition_table): 32-byte entries.
PARTITION_ENTRY = struct.Struct("<2sBBLL16sL")
PARTITION_MAGIC = b"\xaa\x50"
PARTITION_MD5_MAGIC = b"\xeb\xeb"
PARTITION_TYPE_APP = 0x00
PARTITION_SUBTYPE_FACTORY = 0x00
PARTITION_SUBTYPE_OTA_0 = 0x10
PARTITION_SUBTYPE_OTA_1 = 0x11

# The layout id lives in the CSV header, next to the layout it names, so there is one
# place to change and `tests/test_agent_partitions.py` guards the format.
LAYOUT_ID_RE = re.compile(r'layout id "([a-z0-9][a-z0-9.-]*)"')


class BuildError(RuntimeError):
    """The build directory is not the shape this script requires. Always fatal."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise BuildError(f"{path} is missing — did idf.py build actually run?")
    loaded: Any = json.loads(path.read_text())
    if not isinstance(loaded, dict):
        raise BuildError(f"{path} is not a JSON object")
    return loaded


def app_description(app_bin: Path) -> tuple[str, str, str]:
    """`(agent_version, idf_version, project_name)` read from the built app's descriptor.

    Parsed from the binary rather than from `version.txt`/`$IDF_VERSION` so the manifest
    describes the artefact it ships, not the inputs someone believes went into it.
    """
    blob = app_bin.read_bytes()[
        APP_DESC_OFFSET : APP_DESC_OFFSET + struct.calcsize(APP_DESC_STRUCT)
    ]
    if len(blob) < struct.calcsize(APP_DESC_STRUCT):
        raise BuildError(f"{app_bin} is too small to hold an esp_app_desc_t")
    magic, _secure, version, project_name, _time, _date, idf_ver = struct.unpack(
        APP_DESC_STRUCT, blob
    )
    if magic != APP_DESC_MAGIC:
        raise BuildError(
            f"{app_bin} has no esp_app_desc_t magic at 0x{APP_DESC_OFFSET:x} "
            f"(got 0x{magic:08x}) — the image layout changed with the IDF pin"
        )

    def text(raw: bytes) -> str:
        return raw.split(b"\x00", 1)[0].decode("utf-8", "replace")

    return text(version), text(idf_ver), text(project_name)


def decode_partition_table(table_bin: Path) -> list[dict[str, Any]]:
    """Decode `partition-table.bin` into the rows the bootloader will actually see."""
    blob = table_bin.read_bytes()
    rows: list[dict[str, Any]] = []
    for start in range(0, len(blob), PARTITION_ENTRY.size):
        entry = blob[start : start + PARTITION_ENTRY.size]
        if len(entry) < PARTITION_ENTRY.size:
            break
        magic, ptype, subtype, offset, size, label, flags = PARTITION_ENTRY.unpack(entry)
        if magic in (PARTITION_MD5_MAGIC, b"\xff\xff"):
            break
        if magic != PARTITION_MAGIC:
            raise BuildError(f"{table_bin}: bad entry magic {magic!r} at byte {start}")
        rows.append(
            {
                "label": label.split(b"\x00", 1)[0].decode("utf-8", "replace"),
                "type": ptype,
                "subtype": subtype,
                "offset": offset,
                "size": size,
                "flags": flags,
            }
        )
    if not rows:
        raise BuildError(f"{table_bin} decoded to zero partitions")
    return rows


def ota_slot_size(rows: list[dict[str, Any]]) -> int:
    """The A/B slot size, and a refusal to ship a table that is not A/B.

    Every check here is a `CRITICAL.md` failure mode caught at build time rather than on
    a workbench: a `factory` partition (the board can never OTA its way to A/B), a
    missing second slot, or two slots of different sizes (an update that fits one way
    and not the other).
    """
    by_subtype = {row["subtype"]: row for row in rows if row["type"] == PARTITION_TYPE_APP}
    if PARTITION_SUBTYPE_FACTORY in by_subtype:
        raise BuildError(
            "the built partition table has a `factory` partition; "
            "design/architecture.md -> Flash-time immutables forbids it"
        )
    ota_0 = by_subtype.get(PARTITION_SUBTYPE_OTA_0)
    ota_1 = by_subtype.get(PARTITION_SUBTYPE_OTA_1)
    if ota_0 is None or ota_1 is None:
        raise BuildError("the built partition table is not A/B: ota_0 and ota_1 are required")
    if ota_0["size"] != ota_1["size"]:
        raise BuildError(
            f"ota_0 ({ota_0['size']}) and ota_1 ({ota_1['size']}) differ in size; "
            "an update would fit one slot and not the other"
        )
    if ota_1["offset"] != ota_0["offset"] + ota_0["size"]:
        raise BuildError("ota_1 does not start where ota_0 ends")
    size: int = ota_0["size"]
    return size


def partition_layout_id(csv_path: Path) -> str:
    """The layout id declared in `partitions.csv`'s header (`layout id "ab-4m-v1"`)."""
    match = LAYOUT_ID_RE.search(csv_path.read_text())
    if match is None:
        raise BuildError(
            f'{csv_path} declares no layout id. Its header must contain: layout id "ab-4m-v1"'
        )
    return match.group(1)


def collect_parts(
    build_dir: Path, flasher_args: dict[str, Any], out_dir: Path
) -> list[dict[str, Any]]:
    """Copy each flashable file to `out_dir` under its logical name, with its offset."""
    parts: list[dict[str, Any]] = []
    for key, filename in PARTS:
        spec = flasher_args.get(key)
        if not isinstance(spec, dict) or "offset" not in spec or "file" not in spec:
            raise BuildError(
                f"flasher_args.json has no usable '{key}' entry; the IDF pin changed its shape"
            )
        source = build_dir / str(spec["file"])
        if not source.is_file():
            raise BuildError(f"{source} named by flasher_args.json does not exist")
        destination = out_dir / filename
        shutil.copyfile(source, destination)
        parts.append(
            {
                "name": LOGICAL_NAME.get(key, key),
                "path": filename,
                # int(..., 0) because IDF writes hex strings; esptool-js wants integers.
                "offset": int(str(spec["offset"]), 0),
                "size": destination.stat().st_size,
                "sha256": sha256_of(destination),
            }
        )
    return sorted(parts, key=lambda part: part["offset"])


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    build_dir: Path = args.build_dir
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    flasher_args = read_json(build_dir / "flasher_args.json")
    project = read_json(build_dir / "project_description.json")

    target = str(project.get("target") or "")
    if target not in CHIP_FAMILY:
        raise BuildError(f"unknown or missing IDF target {target!r} in project_description.json")

    parts = collect_parts(build_dir, flasher_args, out_dir)

    # The resolved config is NOT in the build directory: `idf.py` writes it next to the
    # project (`build/config/` holds only the generated .h/.cmake/.json views of it).
    # `project_description.json` records where it actually went, which is the only
    # spelling that survives an IDF bump — hence no literal path here either.
    resolved_config = Path(str(project.get("config_file") or ""))
    if not resolved_config.is_file():
        raise BuildError(
            f"the resolved sdkconfig ({resolved_config}) is missing; "
            "project_description.json -> config_file did not point at it"
        )
    shutil.copyfile(resolved_config, out_dir / "sdkconfig.resolved")

    agent_version, idf_version, project_name = app_description(out_dir / "app.bin")
    rows = decode_partition_table(out_dir / "partition-table.bin")
    slot_size = ota_slot_size(rows)

    app_part = next(part for part in parts if part["name"] == "app")
    if app_part["size"] > slot_size:
        raise BuildError(
            f"app.bin is {app_part['size']} bytes but an OTA slot is {slot_size}; "
            "this image cannot be updated over the air"
        )

    flash_settings = flasher_args.get("flash_settings")
    flash_size = str(flash_settings.get("flash_size")) if isinstance(flash_settings, dict) else ""

    return {
        "schema": MANIFEST_SCHEMA,
        "target": target,
        "chip_family": CHIP_FAMILY[target],
        "project_name": project_name,
        "agent_version": agent_version,
        "idf_version": idf_version,
        "idf_image": args.idf_image,
        "source_commit": args.source_commit,
        "built_at": dt.datetime.now(dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "partition_layout": partition_layout_id(args.partitions),
        "ota_slot_size": slot_size,
        "flash_size": flash_size,
        "parts": parts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=Path("build"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--partitions", type=Path, default=Path("partitions.csv"))
    parser.add_argument("--source-commit", default="unknown")
    parser.add_argument("--idf-image", default="unknown")
    args = parser.parse_args(argv)

    try:
        manifest = build_manifest(args)
    except BuildError as exc:
        print(f"make_manifest: {exc}", file=sys.stderr)
        return 1

    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"make_manifest: {manifest['target']} -> {args.out}")
    for part in manifest["parts"]:
        print(f"  0x{part['offset']:06x}  {part['path']:<22} {part['size']:>8} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
