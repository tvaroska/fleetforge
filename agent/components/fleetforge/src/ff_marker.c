/*
 * ff_marker — the one definition of the library marker (spec/device-protocol.md -> Library
 * marker; R3-spec-3, R3-fw-6). See ff_marker.h for the layout and the never-force-keep rule.
 *
 * Two things worth knowing before editing this file:
 *
 * 1. **Nothing here keeps the object alive, on purpose.** It sits alone in this TU, so
 *    -ffunction-sections/-fdata-sections plus --gc-sections drop it unless library code in
 *    another TU references it. ff_identity.c does: the announce reports its `format` as
 *    `lib_marker`, and the boot log prints its `lib_version`. A sketch that has the library
 *    installed but never calls it links neither, so its image carries no marker, which is
 *    what the spec's "installing the library without using it does not mark an image"
 *    requires. A `used`/`retain` attribute, a linker KEEP or an undefined-symbol flag would
 *    mark that sketch too and make the marker worthless to the pre-check.
 * 2. **`lib_version` is FF_LIB_VERSION in every build** (IDF agent, a maker's IDF project,
 *    Arduino). Not PROJECT_VER: in a maker's ESP-IDF project that is the maker's own
 *    version. Not CMake's ../../version.txt: a copied component has no such file.
 *    ff_lib_version.h is pinned equal to agent/version.txt and library.json by
 *    tests/test_arduino_library.py.
 */

#include "ff_marker.h"

#include <stddef.h>

#include "ff_lib_version.h"

_Static_assert(sizeof(ff_lib_marker_t) == 64, "the library marker is 64 bytes (spec)");
_Static_assert(offsetof(ff_lib_marker_t, format) == 16, "format is at offset 16 (spec)");
_Static_assert(offsetof(ff_lib_marker_t, lib_version) == 20, "lib_version is at offset 20 (spec)");
_Static_assert(offsetof(ff_lib_marker_t, reserved1) == 52, "reserved1 is at offset 52 (spec)");
_Static_assert(sizeof(FF_LIB_VERSION) > 1 && sizeof(FF_LIB_VERSION) <= 32,
               "lib_version is non-empty and leaves room for its NUL in 32 bytes (spec)");

const ff_lib_marker_t ff_lib_marker __attribute__((aligned(4))) = {
    /* 8 random bytes, then ASCII "FFOTALIB". Frozen: never regenerated. */
    .magic = {0x14, 0xa9, 0x48, 0xd1, 0x8f, 0x12, 0xcf, 0xdd,
              0x46, 0x46, 0x4f, 0x54, 0x41, 0x4c, 0x49, 0x42},
    .format = FF_LIB_MARKER_FORMAT,
    .lib_version = FF_LIB_VERSION,
};
