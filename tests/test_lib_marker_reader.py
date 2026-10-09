"""`lib_marker.find_marker` — the server's reader of the library marker. R3-be-1.

The rule is spec/device-protocol.md -> *Library marker* -> *Reading it*. The real-bytes
fixtures are frozen images built at R3-fw-6 (library 0.4.7). Regenerate from a checkout
with those builds in place:

    cd /home/boris/products/fleetforge
    # a library build: Arduino Basic for esp32 (marker at 0x1b9ac)
    head -c 131072 agent/components/fleetforge/examples/Basic/.pio/build/esp32/firmware.bin \\
      > tests/fixtures/firmware/esp32.basic.app.head.bin
    # an agent bundle built from R3-fw-6 on (marker at 0x1acf8)
    head -c 131072 agent/dist/esp32/app.bin > tests/fixtures/firmware/esp32.agent.app.head.bin
    # a plain sketch: a copy of examples/Basic with the Fleetforge.begin(...) line deleted
    # (library installed and compiled, ff_marker.c.o included, never called), whole file
    cp /tmp/ff-nocall/.pio/build/esp32/firmware.bin tests/fixtures/firmware/esp32.nocall.app.bin

Check each source first: `grep -c -a FFOTALIB <file>` is 1, 1 and 0.
"""

from pathlib import Path

import pytest

from fleetforge.lib_marker import MAGIC, MARKER_SIZE, LibMarker, find_marker
from fleetforge.merged_image import detect_merged
from tests.test_library_marker import _firmware_magic, _spec_magic

FIXTURES = Path(__file__).parent / "fixtures" / "firmware"
LIBRARY_BUILDS = ["esp32.basic.app.head.bin", "esp32.agent.app.head.bin"]
UNMARKED = [
    "esp32.nocall.app.bin",
    "esp32.app.head.bin",
    "esp32s3.app.head.bin",
    "esp32.merged.head.bin",
    "esp32s3.merged.head.bin",
]


def marker(fmt: int = 1, version: bytes = b"0.4.7", *, size: int = MARKER_SIZE) -> bytes:
    """One marker laid out as the spec table: magic, format, 3 reserved, lib_version[32], 12."""
    raw = MAGIC + bytes([fmt]) + bytes(3) + version.ljust(32, b"\0")[:32] + bytes(12)
    assert len(raw) == MARKER_SIZE
    return raw[:size]


class TestTheMagicIsTheSpec:
    def test_it_is_the_spec_table_and_the_firmware(self) -> None:
        assert MAGIC == _spec_magic()
        assert MAGIC == _firmware_magic()
        assert MAGIC[8:] == b"FFOTALIB"
        assert MARKER_SIZE == 64


class TestRealImages:
    @pytest.mark.parametrize("name", LIBRARY_BUILDS)
    def test_a_library_build_has_one(self, name: str) -> None:
        found = find_marker((FIXTURES / name).read_bytes())
        assert found is not None
        assert (found.format, found.lib_version) == (1, "0.4.7")
        assert found.offset % 4 == 0

    @pytest.mark.parametrize("name", UNMARKED)
    def test_a_plain_or_older_image_has_none(self, name: str) -> None:
        assert find_marker((FIXTURES / name).read_bytes()) is None

    @pytest.mark.parametrize("name", [*LIBRARY_BUILDS, "esp32.nocall.app.bin"])
    def test_the_new_fixtures_are_app_images_that_upload(self, name: str) -> None:
        data = (FIXTURES / name).read_bytes()
        assert data[0] == 0xE9
        assert detect_merged(data) is None


class TestTheReadingRule:
    def test_found_at_an_odd_offset_among_other_bytes(self) -> None:
        data = b"\xe9" + b"x" * 4092 + marker(version=b"1.2.3-rc1") + b"tail"
        assert find_marker(data) == LibMarker(offset=4093, format=1, lib_version="1.2.3-rc1")

    def test_a_memoryview_is_read_the_same(self) -> None:
        data = b"head" + marker()
        assert find_marker(memoryview(data)) == find_marker(data)

    def test_a_higher_format_is_accepted(self) -> None:
        found = find_marker(marker(fmt=7))
        assert found is not None and found.format == 7

    def test_a_marker_ending_exactly_at_the_end_is_found(self) -> None:
        assert find_marker(b"abc" + marker()) is not None

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(b"", id="empty"),
            pytest.param(b"\xe9" * 1000, id="no-magic"),
            pytest.param(b"prints FFOTALIB on the console", id="ascii-half-only"),
            pytest.param(MAGIC, id="magic-only"),
            pytest.param(b"abc" + marker(size=63), id="truncated"),
            pytest.param(marker(fmt=0), id="format-0"),
            pytest.param(marker(version=b""), id="empty-lib-version"),
            pytest.param(marker(version=b"0.4\x017"), id="non-printable"),
            pytest.param(marker(version=b"0.4.\x7f"), id="del"),
            pytest.param(marker(version=b"0.4.\xe9"), id="high-bit"),
            pytest.param(marker(version=b"v" * 32), id="no-nul-within-32"),
        ],
    )
    def test_malformed_is_no_marker_never_an_error(self, data: bytes) -> None:
        assert find_marker(data) is None

    def test_an_invalid_first_occurrence_then_a_valid_one(self) -> None:
        data = marker(fmt=0) + b"pad" + marker(version=b"9.9.9")
        found = find_marker(data)
        assert found == LibMarker(offset=MARKER_SIZE + 3, format=1, lib_version="9.9.9")

    def test_the_first_valid_one_wins(self) -> None:
        data = marker(version=b"1.0.0") + marker(version=b"2.0.0")
        found = find_marker(data)
        assert found is not None and (found.offset, found.lib_version) == (0, "1.0.0")

    def test_an_overlapping_magic_inside_a_bad_marker_is_still_scanned(self) -> None:
        # The second magic starts inside the first marker's reserved tail: the scan resumes at
        # pos + 1, not pos + 64.
        bad = bytearray(marker(fmt=0))
        bad[52:64] = MAGIC[:12]
        data = bytes(bad) + MAGIC[12:] + marker()[16:]
        found = find_marker(data)
        assert found is not None and found.offset == 52
