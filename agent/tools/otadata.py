"""Decode, and tear, the otadata partition of a QEMU flash image (R2-test-1, F4).

Runs on the HOST, against `.qemu/flash-<target>.bin` while no emulator owns it
(`just agent-qemu-otadata` refuses while one does). Standard library only, like its
siblings.

otadata is two 4 KB sectors with one 32-byte `{ota_seq, label[20], ota_state, crc}` entry
each (IDF `esp_ota_select_entry_t`). The bootloader boots the slot named by the valid entry
with the highest seq; a sector whose crc does not match is ignored.

`decode` prints exactly what the runbook's `otadecode` shell function prints, so old and
new transcripts compare line for line:

    sector0: seq=1 -> ota_0 state=VALID crc=ok
    sector1: empty

`tear` exists for one failure mode QEMU cannot produce by itself: a power cut INSIDE an
otadata write. QEMU completes every SPI flash command atomically, so a SIGKILL never leaves
a half-programmed page. IDF v5.5.5 writes an entry as erase-sector-then-program-32-bytes
(`rewrite_ota_seq`, and `esp_rewrite_ota_data`), so a cut leaves one of exactly two shapes,
and those are the only two this tool writes:

* `--mode erased`: the whole sector is 0xFF (cut after the erase, before the program);
* `--mode partial`: seq, label and state are back, the crc word is still 0xFFFFFFFF (cut
  inside the 32-byte program, before its last word).

**A torn sector is a state power loss produces. Forging any other state — writing an
ABORTED, an INVALID, a different seq — is not a test**, it is the very thing
`docs/runbooks/rollback-test.md` forbids ("Why it needs a special build").

Every offset comes from the bundle manifest (`parts[]` entry `ota-data`), never from a
constant — the same rule as `qemu_image.py`.

    python3 agent/tools/otadata.py --bundle agent/dist/esp32 --image .qemu/flash-esp32.bin decode
    python3 agent/tools/otadata.py --bundle … --image … tear --sector newest --mode erased
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

SECTOR_SIZE = 4096
ENTRY_FORMAT = "<I20sII"  # ota_seq, label[20], ota_state, crc
ENTRY_SIZE = struct.calcsize(ENTRY_FORMAT)  # 32
# The crc covers ota_seq only (IDF `bootloader_common_ota_select_crc`).
CRC_COVERED = 4
ERASED_WORD = 0xFFFFFFFF

# IDF `esp_ota_img_states_t`.
STATES = {
    0: "NEW",
    1: "PENDING_VERIFY",
    2: "VALID",
    3: "INVALID",
    4: "ABORTED",
    0xFFFFFFFF: "UNDEFINED",
}


class OtadataError(RuntimeError):
    """The inputs are not what this tool needs, or the request makes no sense. Fatal."""


def read_manifest(bundle: Path) -> dict[str, Any]:
    path = bundle / "manifest.json"
    if not path.is_file():
        raise OtadataError(f"{path} is missing — run: just agent-build <target>")
    loaded: Any = json.loads(path.read_text())
    if not isinstance(loaded, dict):
        raise OtadataError(f"{path} is not a JSON object")
    return loaded


def flash_bytes(flash_size: str) -> int:
    """`"4MB"` -> 4194304. The manifest's spelling, as `make_manifest.py` writes it."""
    if not flash_size.endswith("MB") or not flash_size[:-2].isdigit():
        raise OtadataError(f"cannot read flash_size {flash_size!r} from the manifest")
    return int(flash_size[:-2]) * 1024 * 1024


def otadata_location(manifest: dict[str, Any]) -> int:
    """The offset of otadata, after checking it is two sectors."""
    for part in manifest.get("parts", []):
        if part.get("name") == "ota-data":
            size = int(part["size"])
            if size != 2 * SECTOR_SIZE:
                raise OtadataError(
                    f"ota-data is {size} bytes, not two {SECTOR_SIZE}-byte sectors — "
                    "this tool knows only IDF's two-sector otadata"
                )
            return int(part["offset"])
    raise OtadataError("the manifest has no `ota-data` part, so there is no otadata to read")


def check_image(image: Path, manifest: dict[str, Any]) -> None:
    if not image.is_file():
        raise OtadataError(f"{image} does not exist — boot it once with: just agent-qemu")
    expected = flash_bytes(str(manifest.get("flash_size", "")))
    actual = image.stat().st_size
    if actual != expected:
        raise OtadataError(
            f"{image} is {actual} bytes, but this bundle's flash is {expected}: "
            "wrong image for this bundle"
        )


def entry_crc(seq: int) -> int:
    return zlib.crc32(struct.pack("<I", seq), ERASED_WORD)


def parse_entry(raw: bytes) -> tuple[int, int, int]:
    """`(seq, state, crc)` of a sector's first 32 bytes."""
    seq, _label, state, crc = struct.unpack(ENTRY_FORMAT, raw[:ENTRY_SIZE])
    return seq, state, crc


def is_valid(raw: bytes) -> bool:
    seq, _state, crc = parse_entry(raw)
    return seq != ERASED_WORD and entry_crc(seq) == crc


def describe(index: int, raw: bytes) -> str:
    """One line, byte-for-byte the runbook's `otadecode`."""
    seq, state, crc = parse_entry(raw)
    if seq == ERASED_WORD:
        return f"sector{index}: empty"
    ok = entry_crc(seq) == crc
    name = STATES.get(state, hex(state))
    return (
        f"sector{index}: seq={seq} -> ota_{(seq - 1) % 2} state={name} crc={'ok' if ok else 'BAD'}"
    )


def read_sectors(image: Path, offset: int) -> tuple[bytes, bytes]:
    with image.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(2 * SECTOR_SIZE)
    return data[:SECTOR_SIZE], data[SECTOR_SIZE:]


def decode(image: Path, offset: int) -> list[str]:
    return [describe(index, raw) for index, raw in enumerate(read_sectors(image, offset))]


def newest_valid(sectors: tuple[bytes, bytes]) -> int:
    """The sector the bootloader would act on: valid, and the highest seq."""
    valid = [(parse_entry(raw)[0], index) for index, raw in enumerate(sectors) if is_valid(raw)]
    if not valid:
        raise OtadataError("neither otadata sector holds a valid entry — nothing to tear")
    return max(valid)[1]


def torn(raw: bytes, mode: str) -> bytes:
    """What a cut inside `rewrite_ota_seq` leaves of a sector that was going to hold `raw`."""
    sector = bytearray(b"\xff" * SECTOR_SIZE)
    if mode == "partial":
        # Everything up to, not including, the crc word: seq + label + state.
        sector[: ENTRY_SIZE - 4] = raw[: ENTRY_SIZE - 4]
    elif mode != "erased":
        raise OtadataError(f"unknown tear mode {mode!r}")
    return bytes(sector)


def tear(image: Path, offset: int, sector: str, mode: str) -> int:
    """Tear one sector in place. Returns its index. Touches those 4096 bytes only."""
    sectors = read_sectors(image, offset)
    index = newest_valid(sectors) if sector == "newest" else int(sector)
    if index not in (0, 1):
        raise OtadataError(f"sector must be 0, 1 or newest, got {sector!r}")
    replacement = torn(sectors[index], mode)
    # r+b keeps the file (and its 0600 mode) and writes nothing outside the sector.
    with image.open("r+b") as handle:
        handle.seek(offset + index * SECTOR_SIZE)
        handle.write(replacement)
    return index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bundle", type=Path, required=True, help="agent/dist/<target>")
    parser.add_argument("--image", type=Path, required=True, help=".qemu/flash-<target>.bin")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("decode", help="print both otadata entries")
    tear_cmd = sub.add_parser("tear", help="leave one sector as a power cut would")
    tear_cmd.add_argument("--sector", choices=("0", "1", "newest"), required=True)
    tear_cmd.add_argument("--mode", choices=("erased", "partial"), required=True)
    args = parser.parse_args(argv)

    try:
        manifest = read_manifest(args.bundle)
        offset = otadata_location(manifest)
        check_image(args.image, manifest)
        if args.command == "decode":
            print("\n".join(decode(args.image, offset)))
            return 0
        print("before:")
        print("\n".join(decode(args.image, offset)))
        index = tear(args.image, offset, args.sector, args.mode)
        print(f"tore sector{index} ({args.mode}) at 0x{offset + index * SECTOR_SIZE:x}")
        print("after:")
        print("\n".join(decode(args.image, offset)))
        return 0
    except OtadataError as exc:
        print(f"otadata: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
