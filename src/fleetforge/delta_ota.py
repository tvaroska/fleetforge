"""Deterministic `ffdelta-v1` binary delta OTA encoder and verifier (`V3-2`).

Wire layout (`ffdelta-v1`):
- Bytes `[0..8]`: ASCII magic `b"FFDLTA01"`.
- Bytes `[8..40]`: 32-byte raw SHA-256 of the base image (`from_sha256`).
- Bytes `[40..72]`: 32-byte raw SHA-256 of the reconstructed target image (`to_sha256`).
- Bytes `[72..76]`: big-endian `uint32` `target_size` (`<= MAX_DELTA_TARGET_BYTES`).
- Bytes `[76..]`: opcode stream terminated by `OP_END (0x00)`:
  - `OP_COPY (0x01)` + `uint32 src_offset` + `uint32 length`: copy `base[src_offset : src_offset + length]`.
  - `OP_INSERT (0x02)` + `uint32 length` + `bytes[length]`: append literal bytes.
  - `OP_END (0x00)`: end of stream (must land at exact `len(patch)` and `target_size`).

Matches `agent/components/fleetforge/src/ff_delta.c` and `simulator/gateway.py` byte-for-byte.
"""

from __future__ import annotations

import hashlib
import struct

DELTA_FORMAT = "ffdelta-v1"
DELTA_MAGIC = b"FFDLTA01"
HEADER_SIZE = 76
MAX_DELTA_TARGET_BYTES = 16 * 1024 * 1024

OP_END = 0x00
OP_COPY = 0x01
OP_INSERT = 0x02

_BLOCK_WINDOW = 64
_INDEX_STRIDE = 16


class DeltaPatchError(ValueError):
    """Raised when an `ffdelta-v1` patch is malformed, out-of-bounds, or fails digest verification."""


def create_delta_patch(base: bytes, target: bytes) -> bytes:
    """Encode a deterministic `ffdelta-v1` patch reconstructing `target` from `base`."""
    if not target:
        raise DeltaPatchError("target image must not be empty")
    if len(target) > MAX_DELTA_TARGET_BYTES or len(base) > MAX_DELTA_TARGET_BYTES:
        raise DeltaPatchError(f"image exceeds {MAX_DELTA_TARGET_BYTES} byte delta ceiling")

    from_digest = hashlib.sha256(base).digest()
    to_digest = hashlib.sha256(target).digest()

    out = bytearray()
    out.extend(DELTA_MAGIC)
    out.extend(from_digest)
    out.extend(to_digest)
    out.extend(struct.pack(">I", len(target)))

    # Index 64-byte windows of `base` at 16-byte stride (keeping earliest offset for determinism).
    index: dict[bytes, int] = {}
    base_len = len(base)
    if base_len >= _BLOCK_WINDOW:
        for b_pos in range(0, base_len - _BLOCK_WINDOW + 1, _INDEX_STRIDE):
            window = base[b_pos : b_pos + _BLOCK_WINDOW]
            if window not in index:
                index[window] = b_pos

    target_len = len(target)
    t_pos = 0
    lit_start = 0

    while t_pos + _BLOCK_WINDOW <= target_len:
        window = target[t_pos : t_pos + _BLOCK_WINDOW]
        b_match = index.get(window)
        if b_match is None:
            t_pos += 1
            continue

        # Extend match backward into any un-flushed literal prefix if identical.
        while t_pos > lit_start and b_match > 0 and target[t_pos - 1] == base[b_match - 1]:
            t_pos -= 1
            b_match -= 1

        if t_pos > lit_start:
            lit = target[lit_start:t_pos]
            out.append(OP_INSERT)
            out.extend(struct.pack(">I", len(lit)))
            out.extend(lit)

        match_len = _BLOCK_WINDOW
        max_extra = min(base_len - (b_match + match_len), target_len - (t_pos + match_len))
        extra = 0
        while (
            extra < max_extra
            and base[b_match + match_len + extra] == target[t_pos + match_len + extra]
        ):
            extra += 1
        match_len += extra

        out.append(OP_COPY)
        out.extend(struct.pack(">II", b_match, match_len))
        t_pos += match_len
        lit_start = t_pos

    if lit_start < target_len:
        lit = target[lit_start:target_len]
        out.append(OP_INSERT)
        out.extend(struct.pack(">I", len(lit)))
        out.extend(lit)

    out.append(OP_END)
    patch_bytes = bytes(out)
    reconstructed = apply_delta_patch(base, patch_bytes, expected_to_sha256=to_digest.hex())
    if reconstructed != target:
        raise DeltaPatchError("internal delta encoder self-check failed")
    return patch_bytes


def apply_delta_patch(
    base: bytes,
    patch: bytes,
    *,
    expected_to_sha256: str | None = None,
) -> bytes:
    """Verify `from_sha256` against `base`, apply `patch`, and verify `to_sha256` before returning."""
    if len(patch) < HEADER_SIZE + 1:
        raise DeltaPatchError("delta patch shorter than 77-byte minimum header + OP_END")
    if patch[:8] != DELTA_MAGIC:
        raise DeltaPatchError("delta patch magic mismatch (expected FFDLTA01)")

    expected_from = patch[8:40]
    header_to = patch[40:72]
    (target_size,) = struct.unpack(">I", patch[72:76])

    if target_size == 0 or target_size > MAX_DELTA_TARGET_BYTES:
        raise DeltaPatchError(f"invalid delta target_size {target_size}")
    if expected_to_sha256 is not None and header_to.hex() != expected_to_sha256:
        raise DeltaPatchError("delta patch header target digest disagrees with stage command")
    if hashlib.sha256(base).digest() != expected_from:
        raise DeltaPatchError("active base slot sha256 does not match delta from_sha256")

    out = bytearray()
    pos = HEADER_SIZE
    patch_len = len(patch)
    base_len = len(base)
    saw_end = False

    while pos < patch_len:
        op = patch[pos]
        pos += 1
        if op == OP_END:
            saw_end = True
            break
        if op == OP_COPY:
            if pos + 8 > patch_len:
                raise DeltaPatchError("truncated OP_COPY header")
            src_off, length = struct.unpack(">II", patch[pos : pos + 8])
            pos += 8
            if length == 0 or src_off + length > base_len:
                raise DeltaPatchError("OP_COPY out of base slot bounds")
            if len(out) + length > target_size:
                raise DeltaPatchError("OP_COPY overflows declared target_size")
            out.extend(base[src_off : src_off + length])
        elif op == OP_INSERT:
            if pos + 4 > patch_len:
                raise DeltaPatchError("truncated OP_INSERT header")
            (length,) = struct.unpack(">I", patch[pos : pos + 4])
            pos += 4
            if length == 0 or pos + length > patch_len:
                raise DeltaPatchError("OP_INSERT truncated in patch stream")
            if len(out) + length > target_size:
                raise DeltaPatchError("OP_INSERT overflows declared target_size")
            out.extend(patch[pos : pos + length])
            pos += length
        else:
            raise DeltaPatchError(f"unknown ffdelta-v1 opcode 0x{op:02x}")

    if not saw_end or pos != patch_len:
        raise DeltaPatchError("delta stream did not terminate cleanly at OP_END")
    if len(out) != target_size:
        raise DeltaPatchError(f"reconstructed size {len(out)} != declared {target_size}")

    result = bytes(out)
    if hashlib.sha256(result).digest() != header_to:
        raise DeltaPatchError("reconstructed target sha256 mismatch")
    return result
