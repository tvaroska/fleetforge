// The NVS wipe aims at whatever this parser says `nvs` is, so a wrong answer here erases
// the wrong region of a real board's flash. These tests are about the ways the bytes can
// be something other than the happy case.

import { describe, expect, it } from 'vitest'
import { findPartition, parsePartitionTable } from './partitionTable'

type Row = { label: string; type: number; subtype: number; offset: number; size: number }

function encode(rows: Row[], { md5 = true }: { md5?: boolean } = {}): Uint8Array {
  const bytes = new Uint8Array(rows.length * 32 + (md5 ? 32 : 0))
  const view = new DataView(bytes.buffer)
  rows.forEach((row, index) => {
    const at = index * 32
    view.setUint16(at, 0x50aa, true)
    view.setUint8(at + 2, row.type)
    view.setUint8(at + 3, row.subtype)
    view.setUint32(at + 4, row.offset, true)
    view.setUint32(at + 8, row.size, true)
    bytes.set(new TextEncoder().encode(row.label), at + 12)
  })
  if (md5) view.setUint16(rows.length * 32, 0xebeb, true)
  return bytes
}

const NVS: Row = { label: 'nvs', type: 1, subtype: 2, offset: 0x9000, size: 0x6000 }
const PHY: Row = { label: 'phy_init', type: 1, subtype: 1, offset: 0x11000, size: 0x1000 }

describe('parsePartitionTable', () => {
  it('reads the ab-4m-v1 table the agent actually ships', () => {
    const entries = parsePartitionTable(encode([NVS, PHY]))
    expect(entries).toEqual([NVS, PHY])
    expect(findPartition(entries, 'nvs')?.offset).toBe(0x9000)
    expect(findPartition(entries, 'nvs')?.size).toBe(0x6000)
  })

  it('stops at the md5 row instead of reading it as a partition', () => {
    // The failure this prevents is specific and destructive: the 0xEBEB row's bytes 4..12
    // are checksum, so reading it as an entry yields a partition at a random offset with a
    // random size — and the caller erases that.
    expect(parsePartitionTable(encode([NVS]))).toHaveLength(1)
  })

  it('stops at erased flash when the table has no md5 row', () => {
    const padded = new Uint8Array(0xc00).fill(0xff)
    padded.set(encode([NVS, PHY], { md5: false }), 0)
    expect(parsePartitionTable(padded).map((e) => e.label)).toEqual(['nvs', 'phy_init'])
  })

  it('keeps a full 16-byte label, which has no NUL terminator', () => {
    const long = { ...NVS, label: 'sixteen_chars_16' }
    expect(parsePartitionTable(encode([long]))[0].label).toBe('sixteen_chars_16')
  })

  it('refuses bytes that are not a partition table rather than guessing an offset', () => {
    expect(() => parsePartitionTable(new Uint8Array(96).fill(0x3c))).toThrow(
      /not an ESP-IDF partition table/,
    )
  })

  it('says so when the table holds only its checksum row', () => {
    expect(() => parsePartitionTable(encode([]))).toThrow(/only its checksum row/)
  })

  it('returns null for a label that is not there, so the caller can refuse', () => {
    expect(findPartition(parsePartitionTable(encode([PHY])), 'nvs')).toBeNull()
  })
})
