/*
 * ff_marker — the library marker. PRIVATE (src/, never reachable from a consumer's main).
 *
 * spec/device-protocol.md -> Library marker (R3-spec-3, implemented by R3-fw-6): one
 * 64-byte constant that the server finds in an uploaded image, proof that the library's
 * code is linked into it. Defined once, in ff_marker.c; ff_identity.c reads it, and that
 * read is the ONLY thing that keeps it in the image. Never force-keep it (no `used`, no
 * `retain`, no linker KEEP, no undefined-symbol flag): a force-kept marker would mark a
 * firmware that merely has the library installed, which the spec forbids.
 *
 * The layout is the spec table. It grows only additively: a new field takes reserved
 * bytes and a higher format, and nothing here ever moves or changes type.
 */

#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* The `format` this build writes, and what up/announce reports as `lib_marker`. */
#define FF_LIB_MARKER_FORMAT 1

typedef struct {
    uint8_t magic[16];      /* offset 0: 8 random bytes, then ASCII "FFOTALIB" */
    uint8_t format;         /* offset 16 */
    uint8_t reserved0[3];   /* offset 17: zero */
    char lib_version[32];   /* offset 20: printable ASCII, NUL-terminated, non-empty */
    uint8_t reserved1[12];  /* offset 52: zero */
} ff_lib_marker_t;

extern const ff_lib_marker_t ff_lib_marker;

#ifdef __cplusplus
}
#endif
