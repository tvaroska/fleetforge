/*
 * ff_delta.h — private `ffdelta-v1` binary delta OTA patch decoder & verifier (`V3-2`).
 *
 * Reconstructs the inactive OTA slot by copying unchanged spans from the active base slot
 * (`OP_COPY 0x01`) and splicing new literal spans (`OP_INSERT 0x02`), with strict bounds
 * checks so a corrupt or malicious patch can never read past `base_len`, write past
 * `out_cap`, or leave a truncated image. Caller verifies SHA-256 (`from_sha256` before
 * apply and `to_sha256` + ECDSA P-256 signature on the inactive slot BEFORE switching
 * `otadata`).
 */

#ifndef FF_DELTA_H
#define FF_DELTA_H

#include <stddef.h>
#include <stdint.h>

#define FF_DELTA_HEADER_BYTES 76U
#define FF_DELTA_SHA256_BYTES 32U
#define FF_DELTA_MAX_TARGET_BYTES (16U * 1024U * 1024U)

typedef enum {
    FF_DELTA_OK = 0,
    FF_DELTA_ERR_ARGS = -1,
    FF_DELTA_ERR_HEADER = -2,
    FF_DELTA_ERR_BASE_HASH = -3,
    FF_DELTA_ERR_BOUNDS = -4,
    FF_DELTA_ERR_OPCODE = -5,
    FF_DELTA_ERR_TRUNCATED = -6,
    FF_DELTA_ERR_SIZE_MISMATCH = -7,
} ff_delta_err_t;

typedef struct {
    uint8_t from_sha256[FF_DELTA_SHA256_BYTES];
    uint8_t to_sha256[FF_DELTA_SHA256_BYTES];
    uint32_t target_size;
} ff_delta_header_t;

ff_delta_err_t ff_delta_parse_header(const uint8_t *patch,
                                     size_t patch_len,
                                     ff_delta_header_t *out_hdr);

ff_delta_err_t ff_delta_apply_in_memory(const uint8_t *base,
                                        size_t base_len,
                                        const uint8_t base_sha256[FF_DELTA_SHA256_BYTES],
                                        const uint8_t *patch,
                                        size_t patch_len,
                                        uint8_t *out_buf,
                                        size_t out_cap,
                                        size_t *out_written);

#endif /* FF_DELTA_H */
