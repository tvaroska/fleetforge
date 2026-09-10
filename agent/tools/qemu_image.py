"""Assemble a 4 MB flash image (and an eFuse image) for the QEMU harness.

Runs **inside** the pinned ESP-IDF image, like its two siblings, and for the same
reasons: `esptool` and QEMU live in there, the project venv does not exist in there, and
the IDF pin is what decides how both behave. Standard library only.

Two things this script exists to avoid:

* **typing an offset.** Every offset comes from `agent/dist/<target>/manifest.json`,
  which `make_manifest.py` decoded from the built artefacts — including
  `config_partition.offset` for the `ff_cfg` blob. The bootloader sits at `0x1000` on
  ESP32 and `0x0` on the S3/C3/C6; a hardcoded offset produces an image that flashes
  cleanly and never boots, on half the targets, silently.
* **inventing eFuse bytes.** `--efuse` writes the exact default image ESP-IDF's own
  `idf.py qemu` uses (`idf_py_actions.qemu_ext.QEMU_TARGETS[target].default_efuse`),
  imported rather than retyped. Those bytes encode the chip revision; a wrong one boots
  into a bootloader that refuses the image.

The output flash image is written **once** and then belongs to QEMU: `-drive if=mtd`
writes back, so NVS — and therefore the enrolled credential — lives inside it between
runs. Regenerating it is what erases the board's memory. `just agent-qemu` refuses to
overwrite an existing image without `--fresh` for exactly that reason.

    python3 tools/qemu_image.py flash --bundle /d --config /q/ff_cfg.bin \\
        --out /q/flash-esp32.bin
    python3 tools/qemu_image.py efuse --target esp32 --out /q/efuse.bin
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# The flash image is always the full chip: QEMU's `if=mtd` drive must be exactly the
# device size or the emulated chip reads garbage past the end of the file.
FILL_FLASH_SIZE = "4MB"

# 0600: the finished image contains the NVS the board wrote its broker password into,
# and the ff_cfg blob with a live enrollment token. Same posture as `.sim/`'s state
# files (`simulator/state.py`) and the reason `.qemu/` is gitignored.
FILE_MODE = 0o600


class ImageError(RuntimeError):
    """The inputs are not what this script needs. Always fatal."""


def read_manifest(bundle: Path) -> dict[str, Any]:
    path = bundle / "manifest.json"
    if not path.is_file():
        raise ImageError(f"{path} is missing — run: just agent-build <target>")
    loaded: Any = json.loads(path.read_text())
    if not isinstance(loaded, dict):
        raise ImageError(f"{path} is not a JSON object")
    return loaded


def segments(manifest: dict[str, Any], bundle: Path, config: Path | None) -> list[tuple[int, Path]]:
    """`(offset, file)` for everything that goes into the image, in flash order."""
    placed: list[tuple[int, Path]] = []
    for part in manifest["parts"]:
        source = bundle / str(part["path"])
        if not source.is_file():
            raise ImageError(f"{source}, named by the manifest, does not exist")
        placed.append((int(part["offset"]), source))

    if config is not None:
        partition = manifest.get("config_partition")
        if not isinstance(partition, dict):
            raise ImageError(
                "this bundle's manifest has no config_partition, so there is nowhere to "
                "put a config blob. Rebuild it: just agent-build <target>"
            )
        size = int(partition["size"])
        actual = config.stat().st_size
        if actual != size:
            raise ImageError(
                f"{config} is {actual} bytes but the {partition['label']} partition is "
                f"{size}; agent/tools/ff_cfg.py always writes exactly {size}"
            )
        placed.append((int(partition["offset"]), config))

    return sorted(placed, key=lambda item: item[0])


def merge(manifest: dict[str, Any], placed: list[tuple[int, Path]], out: Path) -> list[str]:
    """The `esptool merge_bin` argv that produces the flash image."""
    argv = [
        sys.executable,
        "-m",
        "esptool",
        "--chip",
        str(manifest["target"]),
        "merge_bin",
        "-o",
        str(out),
        "--fill-flash-size",
        FILL_FLASH_SIZE,
    ]
    for offset, source in placed:
        argv += [hex(offset), str(source)]
    return argv


def default_efuse(target: str) -> bytes:
    """ESP-IDF's own default eFuse image for this target. Imported, never retyped."""
    idf_path = os.environ.get("IDF_PATH")
    if not idf_path:
        raise ImageError("IDF_PATH is unset: this script runs inside the ESP-IDF image")
    sys.path.insert(0, str(Path(idf_path) / "tools"))
    try:
        from idf_py_actions.qemu_ext import QEMU_TARGETS  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - only reachable on an IDF bump
        raise ImageError(f"cannot import qemu_ext from {idf_path}: {exc}") from exc
    if target not in QEMU_TARGETS:
        raise ImageError(f"this IDF's QEMU support does not know {target!r}")
    blob: bytes = QEMU_TARGETS[target].default_efuse
    return blob


def write_private(path: Path, blob: bytes) -> None:
    """Write 0600 from the start — never create world-readable and chmod afterwards."""
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    with os.fdopen(handle, "wb") as file:
        file.write(blob)
    os.chmod(path, FILE_MODE)


def cmd_flash(args: argparse.Namespace) -> int:
    manifest = read_manifest(args.bundle)
    placed = segments(manifest, args.bundle, args.config)
    argv = merge(manifest, placed, args.out)
    if args.print_only:
        print(" ".join(argv))
        return 0
    print(f"qemu_image: {manifest['target']} -> {args.out}")
    for offset, source in placed:
        print(f"  0x{offset:06x}  {source.name}")
    result = subprocess.run(argv, check=False)  # noqa: S603
    if result.returncode != 0:
        return result.returncode
    os.chmod(args.out, FILE_MODE)
    return 0


def cmd_efuse(args: argparse.Namespace) -> int:
    write_private(args.out, default_efuse(args.target))
    print(f"qemu_image: {args.target} default efuse -> {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    flash = sub.add_parser("flash", help="merge a bundle (+ ff_cfg blob) into a flash image")
    flash.add_argument("--bundle", type=Path, required=True, help="agent/dist/<target>")
    flash.add_argument("--config", type=Path, help="the ff_cfg blob from ff_cfg.py")
    flash.add_argument("--out", type=Path, required=True)
    flash.add_argument(
        "--print-only", action="store_true", help="print the esptool argv, run nothing"
    )
    flash.set_defaults(func=cmd_flash)

    efuse = sub.add_parser("efuse", help="write this target's default eFuse image")
    efuse.add_argument("--target", default="esp32")
    efuse.add_argument("--out", type=Path, required=True)
    efuse.set_defaults(func=cmd_efuse)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ImageError, KeyError, OSError, ValueError) as exc:
        print(f"qemu_image: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
