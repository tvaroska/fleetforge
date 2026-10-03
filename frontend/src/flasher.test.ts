// `checkChosenPort` — S0-fe-8. The one port choice that is plainly not an ESP32 is a
// port with no USB id at all: the motherboard's COM1, which is the ONLY thing a Windows
// chooser lists when the board's bridge has no driver. It must be refused by name, before
// esptool spends its sync window on it and blames the board.

import { describe, expect, it, vi } from 'vitest'
import {
  BUILT_IN_PORT,
  checkChosenPort,
  describePort,
  explainFlashError,
  NO_BOARD_SELECTED,
} from './flasher'

describe('checkChosenPort', () => {
  it('refuses a port with no USB vendor id — COM1, ttyS0 — and points at the help', () => {
    const onLog = vi.fn()
    expect(() => checkChosenPort({}, onLog)).toThrow(BUILT_IN_PORT)
    expect(BUILT_IN_PORT).toMatch(/COM1/)
    expect(BUILT_IN_PORT).toMatch(/My board isn't listed/)
    // The translator must pass it through untouched; it is already operator-facing.
    expect(explainFlashError(new Error(BUILT_IN_PORT))).toBe(BUILT_IN_PORT)
  })

  it.each([
    [0x10c4, 0xea60, 'Silicon Labs CP210x bridge (USB 10c4:ea60)'],
    [0x1a86, 0x7523, 'WCH CH340/CH9102 bridge (USB 1a86:7523)'],
    [0x303a, 0x1001, 'Espressif native USB (no driver needed) (USB 303a:1001)'],
    [0x0403, 0x6001, 'FTDI bridge (USB 0403:6001)'],
  ])('names a known USB-serial vendor %s in the log and lets it through', (vid, pid, line) => {
    const onLog = vi.fn()
    checkChosenPort({ usbVendorId: vid, usbProductId: pid }, onLog)
    expect(onLog).toHaveBeenCalledWith(`port: ${line}`)
  })

  it('lets an unrecognised USB vendor through — unusual is not wrong', () => {
    const onLog = vi.fn()
    // Prolific PL2303: rare on ESP32 boards, but a board on one still flashes.
    checkChosenPort({ usbVendorId: 0x067b, usbProductId: 0x2303 }, onLog)
    expect(onLog).toHaveBeenCalledWith(expect.stringMatching(/^port: USB 067b:2303 .*trying it anyway/))
  })

  it('keeps the dismissed-chooser translation the view keys on', () => {
    expect(explainFlashError(new DOMException('No port selected by the user.', 'NotFoundError'))).toBe(
      NO_BOARD_SELECTED,
    )
  })
})

describe('describePort', () => {
  it.each([
    [
      { usbVendorId: 0x303a, usbProductId: 0x1001 },
      'Espressif native USB (no driver needed) (USB 303a:1001)',
    ],
    [{ usbVendorId: 0x10c4, usbProductId: 0xea60 }, 'Silicon Labs CP210x bridge (USB 10c4:ea60)'],
    [{ usbVendorId: 0x067b, usbProductId: 0x2303 }, 'USB 067b:2303'],
    [{ usbVendorId: 0x303a }, 'Espressif native USB (no driver needed) (USB 303a)'],
    [{}, 'a built-in port (no USB id)'],
  ])('%j reads as %s', (info, expected) => {
    expect(describePort(info)).toBe(expected)
  })
})
