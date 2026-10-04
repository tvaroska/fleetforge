// The advisory header read behind the upload form's pre-fill (R2b-fe-7).

import { describe, expect, it } from 'vitest'
import { APP_IMAGE_HEAD_BYTES, readAppImage } from './appImage'

function image(
  over: { chip?: number; magic?: number; descMagic?: number; version?: string; project?: string } = {},
): Uint8Array {
  const bytes = new Uint8Array(APP_IMAGE_HEAD_BYTES)
  const view = new DataView(bytes.buffer)
  bytes[0] = over.magic ?? 0xe9
  view.setUint16(12, over.chip ?? 9, true)
  view.setUint32(0x20, over.descMagic ?? 0xabcd5432, true)
  const text = new TextEncoder()
  bytes.set(text.encode(over.version ?? '0.4.5-rbtest'), 0x30)
  bytes.set(text.encode(over.project ?? 'fleetforge-agent'), 0x50)
  return bytes
}

describe('readAppImage', () => {
  it('reads target, version and project from an esp32s3 app image', () => {
    expect(readAppImage(image())).toEqual({
      recognised: true,
      target: 'esp32s3',
      chipId: 9,
      version: '0.4.5-rbtest',
      projectName: 'fleetforge-agent',
    })
  })

  it.each([
    [0, 'esp32'],
    [5, 'esp32c3'],
    [13, 'esp32c6'],
  ])('maps chip id %i to %s', (chip, target) => {
    expect(readAppImage(image({ chip })).target).toBe(target)
  })

  it('keeps an unknown chip id and names no target', () => {
    const info = readAppImage(image({ chip: 99 }))
    expect(info.recognised).toBe(true)
    expect(info.target).toBeNull()
    expect(info.chipId).toBe(99)
  })

  it('does not recognise a file whose first byte is not 0xE9', () => {
    expect(readAppImage(image({ magic: 0xff }))).toEqual({
      recognised: false,
      target: null,
      chipId: null,
      version: null,
      projectName: null,
    })
  })

  it('gives a target but no version when there is no app descriptor (bootloader-like)', () => {
    const info = readAppImage(image({ descMagic: 0 }))
    expect(info.target).toBe('esp32s3')
    expect(info.version).toBeNull()
    expect(info.projectName).toBeNull()
  })

  it('refuses a version with a non-printable byte rather than normalising it', () => {
    const bytes = image()
    bytes[0x33] = 0x07
    expect(readAppImage(bytes).version).toBeNull()
  })

  it('reads an all-NUL version as none', () => {
    expect(readAppImage(image({ version: '' })).version).toBeNull()
  })

  it('does not throw on a buffer shorter than the descriptor', () => {
    const info = readAppImage(image().subarray(0, 0x40))
    expect(info.recognised).toBe(true)
    expect(info.version).toBeNull()
  })

  it('does not throw on an empty buffer', () => {
    expect(readAppImage(new Uint8Array(0)).recognised).toBe(false)
  })
})
