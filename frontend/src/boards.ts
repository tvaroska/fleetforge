// The "confirm which board this is" shortlist.
//
// `spec/flows.md` Flow 1 step 3: detection is chip-level, so the page offers a short list
// of boards that match the detected chip "with 'enter manually' always available". This
// is a UI convenience and nothing more — the pick selects nothing on the server, is not
// written into `ff_cfg`, and is not persisted anywhere (there is no device-rename
// endpoint in R0, and an `ff_cfg` key nothing reads is dead weight in a 4 KB partition).
// What actually decides the bytes written is `chip_family` from the manifest.
//
// Keep this list SHORT and honest. It is not a board database, and a wrong guess here
// costs nothing but a radio button the operator does not choose.

import type { ChipInfo } from './flasher'

export type BoardCandidate = {
  id: string
  label: string
  /** Matched against `ChipInfo.chipName` / the manifest's `chip_family` with `===`. */
  chipFamily: string
  /** Nominal flash size in MB, when it is what distinguishes this board from its sibling. */
  flashMB?: number
  psram?: boolean
}

export const BOARDS: BoardCandidate[] = [
  { id: 'esp32-devkitc', label: 'ESP32-DevKitC (WROOM-32)', chipFamily: 'ESP32', flashMB: 4 },
  { id: 'esp32-wrover', label: 'ESP32-WROVER (PSRAM)', chipFamily: 'ESP32', flashMB: 4, psram: true },
  { id: 'esp32-wemos-d1-mini', label: 'Wemos D1 Mini ESP32', chipFamily: 'ESP32', flashMB: 4 },
  { id: 'esp32c3-devkitm-1', label: 'ESP32-C3-DevKitM-1', chipFamily: 'ESP32-C3', flashMB: 4 },
  { id: 'esp32c3-xiao', label: 'Seeed XIAO ESP32C3', chipFamily: 'ESP32-C3', flashMB: 4 },
  { id: 'esp32c6-devkitc-1', label: 'ESP32-C6-DevKitC-1', chipFamily: 'ESP32-C6', flashMB: 8 },
  { id: 'esp32s3-devkitc-1', label: 'ESP32-S3-DevKitC-1', chipFamily: 'ESP32-S3', flashMB: 8, psram: true },
  { id: 'esp32s3-xiao', label: 'Seeed XIAO ESP32S3', chipFamily: 'ESP32-S3', flashMB: 8, psram: true },
]

/** The id of the always-present escape hatch. Selected by default when nothing matches. */
export const OTHER_BOARD_ID = 'other'

const MB = 1024 * 1024

/**
 * Boards that could plausibly be the one on the desk, best guess first.
 *
 * Chip family is a hard filter (it is the only thing actually detected). Flash size and
 * PSRAM are *hints* that reorder, never exclude: a DevKitC with a reflowed 16 MB part is
 * still a DevKitC, and dropping it from the list would be a confident wrong answer.
 */
export function shortlist(chip: ChipInfo): BoardCandidate[] {
  const detectedMB = Math.round(chip.flashSizeBytes / MB)
  const hasPsram = chip.features.some((feature) => /psram/i.test(feature))
  return BOARDS.filter((board) => board.chipFamily === chip.chipName)
    .map((board, index) => {
      let score = 0
      if (board.flashMB !== undefined && board.flashMB === detectedMB) score += 2
      if (board.psram === true && hasPsram) score += 1
      if (board.psram === true && !hasPsram) score -= 1
      return { board, score, index }
    })
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .map((entry) => entry.board)
}
