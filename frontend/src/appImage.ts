// What an ESP-IDF app image says about itself, read from its first bytes (R2b-fe-7,
// `spec/flows.md` Flow 2 step 1: "Chip target, version ... are read from the file header
// where possible. No shell step."). Pure, no React, no esptool-js (the flasher's heavy
// dependency stays out of this bundle path).
//
// ADVISORY ONLY. The server treats the bytes as opaque on purpose and never checks a
// target against them; this read pre-fills a form and lets the browser refuse an obvious
// chip mismatch. A file that is not an app image (a merged full-flash image, a bootloader)
// simply yields nulls here; refusing those is R2b-be-3, not this file.
//
// Layout (esp_image_header_t, one segment header, then esp_app_desc_t at 0x20):
//   0x00 u8   magic 0xE9
//   0x0C u16  chip id, little endian
//   0x20 u32  app descriptor magic 0xABCD5432
//   0x30      version      char[32], NUL-terminated
//   0x50      project_name char[32], NUL-terminated

export type AppImageInfo = {
  /** false when byte 0 is not 0xE9 — not an ESP app image we can read. */
  recognised: boolean
  /** IDF target name from the header chip id, or null when unknown / not recognised. */
  target: string | null
  chipId: number | null
  /** esp_app_desc_t.version (NUL-terminated C string), or null when no descriptor. */
  version: string | null
  projectName: string | null
}

/** Enough to cover the 24-byte header, the 8-byte segment header and the 256-byte desc head. */
export const APP_IMAGE_HEAD_BYTES = 256

const IMAGE_MAGIC = 0xe9
const CHIP_ID_OFFSET = 12
const DESC_OFFSET = 0x20
const DESC_MAGIC = 0xabcd5432
const VERSION_OFFSET = 0x30
const PROJECT_OFFSET = 0x50
const FIELD_LEN = 32

/**
 * `IMAGE_CHIP_ID` of each target in `node_modules/esptool-js/lib/targets/*.js`
 * (esp_chip_id_t in ESP-IDF).
 */
export const CHIP_TARGETS: Record<number, string> = {
  0: 'esp32',
  2: 'esp32s2',
  5: 'esp32c3',
  9: 'esp32s3',
  12: 'esp32c2',
  13: 'esp32c6',
  16: 'esp32h2',
  18: 'esp32p4',
  20: 'esp32c61',
  23: 'esp32c5',
}

const NOT_AN_APP_IMAGE: AppImageInfo = {
  recognised: false,
  target: null,
  chipId: null,
  version: null,
  projectName: null,
}

/**
 * A NUL-terminated C string field, or null when it is empty, runs past the buffer's end
 * or holds anything outside printable ASCII. Never trimmed or normalised ("rejected,
 * never normalised"): the server's `VERSION_PATTERN` is the authority on a label.
 */
function cString(head: Uint8Array, offset: number): string | null {
  if (head.length < offset + FIELD_LEN) return null
  const field = head.subarray(offset, offset + FIELD_LEN)
  const nul = field.indexOf(0)
  const bytes = nul === -1 ? field : field.subarray(0, nul)
  if (bytes.length === 0) return null
  for (const byte of bytes) {
    if (byte < 0x20 || byte > 0x7e) return null
  }
  return new TextDecoder().decode(bytes)
}

export function readAppImage(head: Uint8Array): AppImageInfo {
  if (head.length === 0 || head[0] !== IMAGE_MAGIC) return NOT_AN_APP_IMAGE

  const view = new DataView(head.buffer, head.byteOffset, head.byteLength)
  const chipId = head.length >= CHIP_ID_OFFSET + 2 ? view.getUint16(CHIP_ID_OFFSET, true) : null
  const target = chipId === null ? null : (CHIP_TARGETS[chipId] ?? null)

  const hasDesc =
    head.length >= DESC_OFFSET + 4 && view.getUint32(DESC_OFFSET, true) === DESC_MAGIC
  return {
    recognised: true,
    target,
    chipId,
    version: hasDesc ? cString(head, VERSION_OFFSET) : null,
    projectName: hasDesc ? cString(head, PROJECT_OFFSET) : null,
  }
}
