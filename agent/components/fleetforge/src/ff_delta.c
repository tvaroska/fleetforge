/*
 * ff_delta.c — deterministic, overflow-safe `ffdelta-v1` patch decoder (`V3-2`).
 */

#include "ff_delta.h"

#include <string.h>

#define FF_DELTA_OP_END 0x00U
#define FF_DELTA_OP_COPY 0x01U
#define FF_DELTA_OP_INSERT 0x02U

static const uint8_t k_delta_magic[8] = {'F', 'F', 'D', 'L', 'T', 'A', '0', '1'};

static uint32_t read_be_u32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static int ct_memeq32(const uint8_t a[FF_DELTA_SHA256_BYTES],
                      const uint8_t b[FF_DELTA_SHA256_BYTES])
{
    uint8_t diff = 0U;
    for (size_t i = 0; i < FF_DELTA_SHA256_BYTES; ++i) {
        diff |= (uint8_t)(a[i] ^ b[i]);
    }
    return diff == 0U;
}

ff_delta_err_t ff_delta_parse_header(const uint8_t *patch,
                                     size_t patch_len,
                                     ff_delta_header_t *out_hdr)
{
    if (patch == NULL || out_hdr == NULL) {
        return FF_DELTA_ERR_ARGS;
    }
    if (patch_len < FF_DELTA_HEADER_BYTES + 1U) {
        return FF_DELTA_ERR_HEADER;
    }
    if (memcmp(patch, k_delta_magic, sizeof(k_delta_magic)) != 0) {
        return FF_DELTA_ERR_HEADER;
    }
    memcpy(out_hdr->from_sha256, patch + 8U, FF_DELTA_SHA256_BYTES);
    memcpy(out_hdr->to_sha256, patch + 40U, FF_DELTA_SHA256_BYTES);
    out_hdr->target_size = read_be_u32(patch + 72U);
    if (out_hdr->target_size == 0U || out_hdr->target_size > FF_DELTA_MAX_TARGET_BYTES) {
        return FF_DELTA_ERR_HEADER;
    }
    return FF_DELTA_OK;
}

ff_delta_err_t ff_delta_apply_in_memory(const uint8_t *base,
                                        size_t base_len,
                                        const uint8_t base_sha256[FF_DELTA_SHA256_BYTES],
                                        const uint8_t *patch,
                                        size_t patch_len,
                                        uint8_t *out_buf,
                                        size_t out_cap,
                                        size_t *out_written)
{
    if (base == NULL || base_sha256 == NULL || patch == NULL || out_buf == NULL ||
        out_written == NULL) {
        return FF_DELTA_ERR_ARGS;
    }
    *out_written = 0U;

    ff_delta_header_t hdr;
    ff_delta_err_t err = ff_delta_parse_header(patch, patch_len, &hdr);
    if (err != FF_DELTA_OK) {
        return err;
    }
    if (!ct_memeq32(base_sha256, hdr.from_sha256)) {
        return FF_DELTA_ERR_BASE_HASH;
    }
    if ((size_t)hdr.target_size > out_cap) {
        return FF_DELTA_ERR_BOUNDS;
    }

    size_t pos = FF_DELTA_HEADER_BYTES;
    size_t written = 0U;
    int saw_end = 0;

    while (pos < patch_len) {
        uint8_t op = patch[pos++];
        if (op == FF_DELTA_OP_END) {
            saw_end = 1;
            break;
        }
        if (op == FF_DELTA_OP_COPY) {
            if (patch_len - pos < 8U) {
                return FF_DELTA_ERR_TRUNCATED;
            }
            uint32_t src_off = read_be_u32(patch + pos);
            uint32_t len = read_be_u32(patch + pos + 4U);
            pos += 8U;
            if (len == 0U || (size_t)src_off > base_len || (size_t)len > base_len - (size_t)src_off) {
                return FF_DELTA_ERR_BOUNDS;
            }
            if ((size_t)len > (size_t)hdr.target_size - written) {
                return FF_DELTA_ERR_BOUNDS;
            }
            memcpy(out_buf + written, base + src_off, (size_t)len);
            written += (size_t)len;
        } else if (op == FF_DELTA_OP_INSERT) {
            if (patch_len - pos < 4U) {
                return FF_DELTA_ERR_TRUNCATED;
            }
            uint32_t len = read_be_u32(patch + pos);
            pos += 4U;
            if (len == 0U || (size_t)len > patch_len - pos) {
                return FF_DELTA_ERR_TRUNCATED;
            }
            if ((size_t)len > (size_t)hdr.target_size - written) {
                return FF_DELTA_ERR_BOUNDS;
            }
            memcpy(out_buf + written, patch + pos, (size_t)len);
            pos += (size_t)len;
            written += (size_t)len;
        } else {
            return FF_DELTA_ERR_OPCODE;
        }
    }

    if (!saw_end || pos != patch_len) {
        return FF_DELTA_ERR_TRUNCATED;
    }
    if (written != (size_t)hdr.target_size) {
        return FF_DELTA_ERR_SIZE_MISMATCH;
    }
    *out_written = written;
    return FF_DELTA_OK;
}
