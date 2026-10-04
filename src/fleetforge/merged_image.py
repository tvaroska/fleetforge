"""Recognise a merged full-flash image. R2b-be-3.

`spec/flows.md` Flow 2 step 2 lists "a full-flash merged binary (a `*.merged.bin` instead
of an app image)" among the refusals. A merged image is written over USB from offset 0x0;
an OTA update writes only the app, into a slot. `POST /v1/artifact` refuses it so no merged
label can ever be offered to a deploy.

Two signatures, either one means merged:

- **A. Bootloader at 0x1000.** ESP32 and ESP32-S2 put the second-stage bootloader at
  0x1000, and `esptool merge_bin` pads 0x0-0xFFF with `0xFF`. An app image always starts
  with `0xE9`, so it never starts with 4 KiB of `0xFF`.
- **B. Partition table at 0x8000.** The ESP-IDF default `CONFIG_PARTITION_TABLE_OFFSET`,
  also used by Arduino, PlatformIO and ESPHome. The first entry must carry the `AA 50`
  magic, a non-zero 4 KiB-aligned offset and a non-zero size.

Why not the header alone: on esp32s3, c3 and c6 the bootloader is at 0x0, so a merged file
starts with `0xE9` exactly like an app. "No app descriptor at 0x20" would separate them but
would also refuse non-IDF images, which is an opinion about toolchains the server must not
have. B catches that case unambiguously.

Narrow and negative: this says "this is the known wrong kind", never "this is a valid
app". An unrecognised file is still accepted (the bytes stay opaque, R1-be-1).

False positives: a real app with `AA 50` at exactly 0x8000, then an aligned non-zero u32
and a non-zero u32, is roughly 1 in 2^28 of random content. Accepted; the refusal sentence
says what was seen.

Known gap, documented not built: `merge_bin --target-offset 0x1000` (the file starts at the
bootloader, the table sits at file offset 0x7000). Arduino IDE, PlatformIO and ESPHome
write from 0x0.

Pure and stdlib only; never raises on short or odd input.
"""

import struct
from dataclasses import dataclass

BOOTLOADER_OFFSET_AT_0X1000 = 0x1000  # esp32, esp32s2
PARTITION_TABLE_OFFSET = 0x8000  # ESP-IDF default
IMAGE_MAGIC = 0xE9
PARTITION_MAGIC = b"\xaa\x50"
_PARTITION_ENTRY = struct.Struct("<2sBBII16sI")  # magic, type, subtype, offset, size, label, flags
PROBE_BYTES = PARTITION_TABLE_OFFSET + _PARTITION_ENTRY.size  # the most this module reads

BOOTLOADER_EVIDENCE = "the bootloader at 0x1000"
PARTITION_TABLE_EVIDENCE = "the partition table at 0x8000"


@dataclass(frozen=True, slots=True)
class MergedImage:
    evidence: tuple[str, ...]


def _bootloader_at_0x1000(data: bytes | memoryview) -> bool:
    start = BOOTLOADER_OFFSET_AT_0X1000
    return (
        len(data) > start and bytes(data[:start]) == b"\xff" * start and data[start] == IMAGE_MAGIC
    )


def _partition_table_at_0x8000(data: bytes | memoryview) -> bool:
    if len(data) < PROBE_BYTES:
        return False
    magic, _type, _subtype, offset, size, _label, _flags = _PARTITION_ENTRY.unpack_from(
        bytes(data[PARTITION_TABLE_OFFSET:PROBE_BYTES])
    )
    return bool(magic == PARTITION_MAGIC and offset != 0 and offset % 0x1000 == 0 and size != 0)


def detect_merged(data: bytes | memoryview) -> MergedImage | None:
    """The evidence that `data` is a merged full-flash image, or `None`."""
    evidence: list[str] = []
    if _bootloader_at_0x1000(data):
        evidence.append(BOOTLOADER_EVIDENCE)
    if _partition_table_at_0x8000(data):
        evidence.append(PARTITION_TABLE_EVIDENCE)
    return MergedImage(tuple(evidence)) if evidence else None
