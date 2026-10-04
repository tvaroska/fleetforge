"""`merged_image.detect_merged` against real esptool-merged and real app images. R2b-be-3.

The fixtures are the first 0x9000 bytes of real images built by IDF v5.5.5. Regenerate from
`agent/dist/{esp32,esp32s3}` (esptool 4.8.1; v5 renamed the subcommand), then `head -c 36864`:

    uvx --from 'esptool==4.8.1' esptool.py --chip esp32s3 merge_bin -o esp32s3.merged.bin \\
      0x0 bootloader.bin 0x8000 partition-table.bin 0xf000 ota-data-initial.bin 0x20000 app.bin
    uvx --from 'esptool==4.8.1' esptool.py --chip esp32 merge_bin -o esp32.merged.bin \\
      0x1000 bootloader.bin 0x8000 partition-table.bin 0xf000 ota-data-initial.bin 0x20000 app.bin
    head -c 36864 esp32s3.merged.bin > tests/fixtures/firmware/esp32s3.merged.head.bin
    head -c 36864 app.bin > tests/fixtures/firmware/<chip>.app.head.bin
"""

import struct
from pathlib import Path

from fleetforge.merged_image import PROBE_BYTES, detect_merged

FIXTURES = Path(__file__).parent / "fixtures" / "firmware"
BOOT = "the bootloader at 0x1000"
TABLE = "the partition table at 0x8000"


def fx(name: str) -> bytes:
    return (FIXTURES / f"{name}.head.bin").read_bytes()


def table_entry(offset: int, size: int) -> bytes:
    return struct.pack("<2sBBII16sI", b"\xaa\x50", 1, 2, offset, size, b"nvs", 0)


def with_table(base: bytes, entry: bytes) -> bytes:
    buf = bytearray(base.ljust(0x9000, b"\x00"))
    buf[0x8000 : 0x8000 + len(entry)] = entry
    return bytes(buf)


class TestFixtures:
    def test_they_are_what_they_claim(self) -> None:
        assert fx("esp32s3.merged")[0] == 0xE9, "header-only cannot tell s3 merged from an app"
        for name in ("esp32.app", "esp32s3.app"):
            data = fx(name)
            assert struct.unpack_from("<I", data, 0x20)[0] == 0xABCD5432
        for name in ("esp32.merged", "esp32s3.merged", "esp32.app", "esp32s3.app"):
            assert len(fx(name)) == 36864


class TestRealImages:
    def test_esp32s3_merged_is_found_through_the_partition_table(self) -> None:
        found = detect_merged(fx("esp32s3.merged"))
        assert found is not None and found.evidence == (TABLE,)

    def test_esp32_merged_is_found_through_both(self) -> None:
        found = detect_merged(fx("esp32.merged"))
        assert found is not None and found.evidence == (BOOT, TABLE)

    def test_real_app_heads_are_not_detected(self) -> None:
        assert detect_merged(fx("esp32.app")) is None
        assert detect_merged(fx("esp32s3.app")) is None


class TestSynthetic:
    def test_not_merged(self) -> None:
        for data in (b"", b"\xe9", b"\xff" * 0x1000, b"\xff" * 0x2000, b"\x00" * 1966080):
            assert detect_merged(data) is None
        app = b"\xe9\x06\x02\x20" + b"firmware bytes, opaque to the server" * 37
        assert detect_merged(app) is None

    def test_a_misaligned_or_empty_entry_is_not_a_table(self) -> None:
        base = b"\xe9\x06\x02\x20"
        assert detect_merged(with_table(base, table_entry(0x1234, 0x6000))) is None
        assert detect_merged(with_table(base, table_entry(0, 0))) is None
        assert detect_merged(with_table(base, table_entry(0x9000, 0))) is None

    def test_bootloader_only_in_a_short_file(self) -> None:
        found = detect_merged(b"\xff" * 0x1000 + b"\xe9" + b"\x00" * 100)
        assert found is not None and found.evidence == (BOOT,)

    def test_table_only(self) -> None:
        found = detect_merged(with_table(b"\xe9\x03\x02\x2f", table_entry(0x9000, 0x6000)))
        assert found is not None and found.evidence == (TABLE,)

    def test_truncation_at_the_probe_boundary(self) -> None:
        data = fx("esp32s3.merged")
        assert detect_merged(data[: PROBE_BYTES - 1]) is None
        assert detect_merged(data[:PROBE_BYTES]) is not None
