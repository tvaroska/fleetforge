"""Find the Fleetforge OTA library marker in an app image. R3-be-1.

`spec/device-protocol.md` → *Library marker*: an image built with the Fleetforge OTA library
(the ESP-IDF component, the Arduino library, and so the prebuilt agent) contains one 64-byte
constant, written by the library's `ff_marker.c`. It is not at a fixed offset, and every
field is bytes or chars, so byte order does not matter.

| Offset | Size | Field |
|---|---|---|
| 0 | 16 | `magic` (`MAGIC`; the last 8 bytes are ASCII `FFOTALIB`) |
| 16 | 1 | `format`, at least 1 |
| 17 | 3 | reserved |
| 20 | 32 | `lib_version`: printable ASCII, NUL-terminated, non-empty |
| 52 | 12 | reserved |

The reading rule, verbatim from the spec: scan the whole image for `magic`, take each
occurrence in order and accept the first that has at least 64 bytes from the start of the
magic to the end of the image, `format >= 1` and a valid `lib_version` C string. No
occurrence, or no valid one, means no marker; **a malformed marker is no marker, never an
error**. Fields beyond `lib_version` are read only when `format` says they exist, and
`format` 1 has none, so this reader reads none.

`POST /v1/artifact` stores `find_marker(data) is not None` as `artifacts.has_lib_marker`,
and the deploy pre-check gates an unmarked build as `no_library_marker`. The marker proves
the library's code is linked, not that the firmware starts it (spec, *What it proves*).

Like `merged_image.py`, this never validates the format: it is one narrow read over opaque
bytes, and an unrecognised file is still accepted.

`MAGIC` is pinned equal to the spec table and to `ff_marker.c` by
`tests/test_lib_marker_reader.py`. Pure and stdlib only; never raises on short or odd input.
"""

from dataclasses import dataclass

MAGIC = bytes.fromhex("14a948d18f12cfdd46464f54414c4942")
MARKER_SIZE = 64
FORMAT_AT = 16
LIB_VERSION_AT, LIB_VERSION_END = 20, 52

_PRINTABLE_MIN, _PRINTABLE_MAX = 0x20, 0x7E


@dataclass(frozen=True, slots=True)
class LibMarker:
    """The first valid marker in an image: where it is and what it says."""

    offset: int
    format: int
    lib_version: str


def find_marker(data: bytes | memoryview) -> LibMarker | None:
    """The first valid library marker in `data`, or `None`. Never raises."""
    image = bytes(data)
    pos = image.find(MAGIC)
    while pos != -1:
        if len(image) - pos >= MARKER_SIZE and image[pos + FORMAT_AT] >= 1:
            raw = image[pos + LIB_VERSION_AT : pos + LIB_VERSION_END]
            end = raw.find(b"\0")
            if end > 0 and all(_PRINTABLE_MIN <= b <= _PRINTABLE_MAX for b in raw[:end]):
                return LibMarker(
                    offset=pos,
                    format=image[pos + FORMAT_AT],
                    lib_version=raw[:end].decode("ascii"),
                )
        pos = image.find(MAGIC, pos + 1)
    return None
