// Reads the ESP-IDF partition table binary that every flash already downloads.
//
// Exists for one reason: the flasher needs to erase a named partition rather than a
// hardcoded address, and the only honest source for where it lives is the table being
// written to the board in the same operation. Hardcoding 0x9000 would be right for
// `ab-4m-v1` today and silently wrong for the next layout — and "silently wrong" here
// means erasing the wrong 24 KB.
//
// This originally read "wipe `nvs` without touching `phy_init`". That goal was misconceived
// — the RF calibration is in `nvs` too, and `phy_init` is unused in our build (S0-fw-4).
// The lookup-by-label is still right; only its stated purpose was wrong.
//
// Format (ESP-IDF `partition_table/gen_esp32part.py`): a run of 32-byte little-endian
// entries at 0x8000. Each is magic 0xAA50, type, subtype, offset, size, a 16-byte
// NUL-padded label and flags. The run ends at an 0xEBEB entry (the optional MD5 checksum)
// or at erased flash (0xFFFF). Stable since ESP-IDF v3.

const ENTRY_SIZE = 32
/** Little-endian read of the two magic bytes `AA 50`. */
const MAGIC_ENTRY = 0x50aa
/** The MD5 checksum row `gen_esp32part.py` appends; not a partition. */
const MAGIC_MD5 = 0xebeb

export type PartitionEntry = {
  label: string
  type: number
  subtype: number
  offset: number
  size: number
}

/**
 * Every partition in the table, in table order.
 *
 * Throws only when the bytes are not a partition table at all — a truncated or corrupt
 * table is a bad download, and the caller has already verified the sha256, so reaching
 * that means something is wrong that a wipe must not be attempted on top of.
 */
export function parsePartitionTable(bytes: Uint8Array): PartitionEntry[] {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const entries: PartitionEntry[] = []
  for (let at = 0; at + ENTRY_SIZE <= bytes.byteLength; at += ENTRY_SIZE) {
    const magic = view.getUint16(at, true)
    // The MD5 row and erased flash both mean "no more partitions". Anything else with a
    // wrong magic means we are not reading a partition table, and guessing is worse than
    // stopping: `findPartition` will report the label it could not find.
    if (magic !== MAGIC_ENTRY) break
    const label = new TextDecoder()
      .decode(bytes.subarray(at + 12, at + 28))
      .replace(/\0.*$/, '')
    entries.push({
      label,
      type: view.getUint8(at + 2),
      subtype: view.getUint8(at + 3),
      offset: view.getUint32(at + 4, true),
      size: view.getUint32(at + 8, true),
    })
  }
  if (entries.length === 0) {
    const magic = bytes.byteLength >= 2 ? view.getUint16(0, true) : 0
    throw new Error(
      magic === MAGIC_MD5
        ? 'the partition table contains no partitions, only its checksum row'
        : `not an ESP-IDF partition table (first entry magic 0x${magic.toString(16)})`,
    )
  }
  return entries
}

/** The entry with this label, or null. Labels are unique in a valid table. */
export function findPartition(entries: PartitionEntry[], label: string): PartitionEntry | null {
  return entries.find((entry) => entry.label === label) ?? null
}
