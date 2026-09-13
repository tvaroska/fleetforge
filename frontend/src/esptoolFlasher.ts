// The ONLY file in the app that imports `esptool-js` or touches `navigator.serial`.
//
// It implements the `BoardFlasher` seam in `flasher.ts` and translates esptool-js's raw
// failures into something an operator can act on. Keeping the boundary here is what lets
// `flash.ts` — where all the rules live — be tested in jsdom, which has no Web Serial.
//
// Several settings below look arbitrary and are not:
//
// * `flashMode`/`flashFreq`/`flashSize` are all `'keep'`. Anything else makes esptool-js
//   rewrite the bootloader's flash-parameter byte and recompute the image SHA; the bundle
//   bytes are exactly what ESP-IDF produced for `ab-4m-v1` and must reach the chip
//   unmodified. The cost is that esptool-js skips its own "does this fit?" check when
//   `flashSize === 'keep'`, so `flash.ts::checkFlashable` does it instead.
// * `compress: true`, so `reportProgress` reports COMPRESSED bytes. The caller must not
//   mix `total` with the manifest's `size`.
// * No `calculateMD5Hash`. Verifying the flash read-back needs an MD5 implementation and
//   Web Crypto has none — adding `crypto-js` for it would be the wrong trade. `flash.ts`
//   verifies the sha256 of every downloaded part against the manifest BEFORE writing,
//   which is the failure that actually happens (a truncated download), and esptool-js
//   checksums every block on the wire anyway.
// * esptool-js WILL log `Warning: Image file at 0x12000 doesn't look like an image file,
//   so not changing any flash settings.` That is the `ff_cfg` blob, which is not an ESP
//   image. Expected, harmless, and not something to "fix".

import { ESPLoader, Transport, type FlashSizeValues } from 'esptool-js'
import { explainFlashError } from './flasher'
import type { BoardFlasher, ChipInfo, FlashPart, FlasherFactory, WriteOptions } from './flasher'

class EsptoolFlasher implements BoardFlasher {
  private closed = false

  constructor(
    private readonly loader: ESPLoader,
    private readonly transport: Transport,
    private readonly onLog: (line: string) => void,
  ) {}

  async detect(): Promise<ChipInfo> {
    // `main()` syncs, identifies the chip, uploads the stub and raises the baud rate.
    // Everything below is only safe afterwards — and the stub is also what makes
    // `eraseAll` work at all (`writeFlash` ignores it unless `IS_STUB`).
    const description = await this.loader.main()
    const chip = this.loader.chip
    let macAddress: string | null = null
    try {
      macAddress = await chip.readMac(this.loader)
    } catch (err) {
      // A missing MAC costs the device-id *prediction* in the UI and nothing else; the
      // board reads its own eFuse MAC at boot (`agent/main/ff_identity.c`).
      this.onLog(`could not read the MAC: ${explainFlashError(err)}`)
    }
    // `detectFlashSize()` is typed `Promise<string>` while `flashSizeBytes()` takes the
    // `FlashSizeValues` union — esptool-js's own typing gap, not ours.
    const flashSizeBytes = this.loader.flashSizeBytes(
      (await this.loader.detectFlashSize()) as FlashSizeValues,
    )
    return {
      chipName: chip.CHIP_NAME,
      description,
      macAddress,
      flashSizeBytes,
      features: await chip.getChipFeatures(this.loader),
    }
  }

  async write(parts: FlashPart[], options: WriteOptions): Promise<void> {
    await this.loader.writeFlash({
      fileArray: parts.map((part) => ({ data: part.data, address: part.address })),
      // See the header: 'keep' three times, on purpose.
      flashMode: 'keep',
      flashFreq: 'keep',
      flashSize: 'keep',
      // Never true. `writeFlash` erases the sectors it writes, which is all the erasing
      // this app is allowed to do: `eraseAll` wipes the whole chip including `phy_init`,
      // and the cached RF calibration in it is what a marginal board needs to survive its
      // next boot (`flash.ts::nvsWipe`). Clearing NVS is a part in the plan instead.
      eraseAll: false,
      compress: true,
      reportProgress: options.onProgress,
    })
  }

  async finish(): Promise<void> {
    try {
      await this.loader.after('hard_reset')
    } catch (err) {
      // On the native-USB parts (C3/C6/S3 over USB-JTAG-Serial) the device re-enumerates
      // on reset and the old `SerialPort` object throws. The flash already succeeded —
      // turning that into a failure would send an operator to re-flash a good board.
      this.onLog(`reset after flashing: ${explainFlashError(err)} (the image is already written)`)
    }
    await this.close()
  }

  async close(): Promise<void> {
    if (this.closed) return
    this.closed = true
    try {
      // NEVER `port.forget()`: that revokes the permission the operator just granted, so
      // the next board needs a fresh trip through the chooser.
      await this.transport.disconnect()
    } catch (err) {
      this.onLog(`releasing the port: ${explainFlashError(err)}`)
    }
  }
}

export const createEsptoolFlasher: FlasherFactory = async ({ baudRate, onLog }) => {
  // Synchronous, first, inside the click handler's gesture. Anything awaited before this
  // spends the gesture and Chromium then refuses to open the chooser.
  const port = await navigator.serial.requestPort()
  // `tracing: false` — tracing dumps every raw SLIP frame into the log panel.
  const transport = new Transport(port, false)
  // No `romBaudrate` here: 0.6.1 fixes it at 115200 internally and `LoaderOptions` has no
  // such field (older tutorials pass one — that is a different major). `main()` connects
  // at the ROM rate, loads the stub, then raises the link to `baudrate`.
  const loader = new ESPLoader({
    transport,
    baudrate: baudRate,
    terminal: { clean: () => {}, writeLine: onLog, write: onLog },
  })
  return new EsptoolFlasher(loader, transport, onLog)
}
