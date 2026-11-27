// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { BOARDS, OTHER_BOARD_ID, shortlist } from './boards'
import type { ChipInfo } from './flasher'

const MB = 1024 * 1024

function makeChip(overrides: Partial<ChipInfo> = {}): ChipInfo {
  return {
    chipName: 'ESP32',
    description: 'ESP32-D0WD-V3 (revision v3.1)',
    macAddress: 'A4:CF:12:B3:DE:90',
    flashSizeBytes: 4 * MB,
    features: ['WiFi', 'BT', 'Dual Core'],
    ...overrides,
  }
}

describe('boards shortlist', () => {
  it('exposes the always-available manual escape hatch id', () => {
    expect(OTHER_BOARD_ID).toBe('other')
    expect(BOARDS.every((b) => b.id !== OTHER_BOARD_ID)).toBe(true)
  })

  it('hard-filters strictly by chip family and returns empty for unlisted chips', () => {
    const c3 = shortlist(makeChip({ chipName: 'ESP32-C3', flashSizeBytes: 4 * MB }))
    expect(c3.map((b) => b.id)).toEqual(['esp32c3-devkitm-1', 'esp32c3-xiao'])

    const c6 = shortlist(makeChip({ chipName: 'ESP32-C6', flashSizeBytes: 8 * MB }))
    expect(c6.map((b) => b.id)).toEqual(['esp32c6-devkitc-1'])

    expect(shortlist(makeChip({ chipName: 'ESP32-S2' }))).toEqual([])
    expect(shortlist(makeChip({ chipName: 'ESP32-H2' }))).toEqual([])
  })

  it('ranks PSRAM boards first when PSRAM is detected and demotes them when absent', () => {
    const withoutPsram = shortlist(makeChip({ chipName: 'ESP32', features: ['WiFi', 'BT'] }))
    expect(withoutPsram.map((b) => b.id)).toEqual([
      'esp32-devkitc',
      'esp32-wemos-d1-mini',
      'esp32-wrover',
    ])

    const withPsram = shortlist(
      makeChip({ chipName: 'ESP32', features: ['WiFi', 'BT', 'Embedded PSRAM 4MB'] }),
    )
    expect(withPsram[0]?.id).toBe('esp32-wrover')
    expect(withPsram.map((b) => b.id)).toEqual([
      'esp32-wrover',
      'esp32-devkitc',
      'esp32-wemos-d1-mini',
    ])
  })

  it('treats flash size as a ranking hint without excluding boards with custom flash', () => {
    const s3Custom16Mb = shortlist(
      makeChip({
        chipName: 'ESP32-S3',
        flashSizeBytes: 16 * MB,
        features: ['WiFi', 'BLE', 'Embedded PSRAM 8MB'],
      }),
    )
    expect(s3Custom16Mb.map((b) => b.id)).toEqual(['esp32s3-devkitc-1', 'esp32s3-xiao'])
  })
})
