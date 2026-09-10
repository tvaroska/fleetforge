// The `ff_cfg` flash-time configuration blob — **the third implementation of a
// cross-language contract**, and the only one that runs in a browser.
//
// The other two are `agent/tools/ff_cfg.py` (the writer the QEMU harness uses, and the
// authoritative prose description of the format — read its module docstring first) and
// `agent/main/ff_cfg.c` (the firmware reader). All three must agree about sixteen bytes.
// A disagreement is not a failed test on this box: it is a board that boots, does not
// recognise its own config partition, and idles. On a bench that looks like dead firmware.
//
// Layout — little-endian, a 16-byte header then compact JSON, `0xff` to the end:
//
//     0x000  4  magic       "FFCF"
//     0x004  2  version     u16 = 1
//     0x006  2  reserved    u16 = 0
//     0x008  4  payload_len u32, 1 … 4080
//     0x00c  4  crc32       u32, IEEE (the same polynomial zlib.crc32 uses)
//     0x010  N  payload     compact UTF-8 JSON object
//
// `frontend/src/ffcfg.vector.json` is a golden vector both this file's tests and
// `tests/test_ff_cfg.py` assert against, so the two writers cannot drift while both
// suites stay green. It is ASCII-only on purpose: Python's `json.dumps` defaults to
// `ensure_ascii=True` and `JSON.stringify` does not, so a non-ASCII SSID produces
// *different bytes* from the two writers even though both decode to the same object.
// The contract is the decoded object; the vector can only pin bytes where they agree.
//
// **DOM-free and dependency-free on purpose.** `scripts/emit-ffcfg.ts` compiles this
// same file under `tsconfig.node.json` so the QEMU harness can boot a blob produced by
// the browser's encoder — which is the only hardware-free way to prove this writer
// against the real firmware reader.

export const MAGIC = 'FFCF'
export const VERSION = 1
export const HEADER_SIZE = 16

// `agent/partitions.csv` → `ff_cfg, data, 0x40, 0x12000, 0x1000`, frozen at R0. Retyped
// rather than derived: a blob that is not exactly one partition long cannot be flashed
// at an offset without overwriting a neighbour. The *offset* is never retyped — it comes
// from the manifest (see `flash.ts`).
export const PARTITION_SIZE = 4096
export const MAX_PAYLOAD = PARTITION_SIZE - HEADER_SIZE

// Erased flash. `0xff` rather than `0x00` so a short write still looks erased after its
// payload instead of looking like a payload of NUL bytes.
export const FILL = 0xff

/** Every key `agent/main/ff_cfg.c` reads, in the order `ff_cfg.py:fields_from_args` emits. */
export const KEY_ORDER = [
  'api_base',
  'mqtt_uri',
  'token',
  'ssid',
  'psk',
  'link',
  'ntp',
  'hb_s',
  'power',
  'wake_s',
] as const

/** The two the firmware cannot invent a default for: nowhere to enroll, nowhere to connect. */
export const REQUIRED_KEYS = ['api_base', 'mqtt_uri'] as const

export const LINKS = ['wifi', 'ethernet'] as const
export const POWER_CLASSES = ['always_on', 'sleepy'] as const

export type FfCfgKey = (typeof KEY_ORDER)[number]
export type FfCfgFields = Partial<Record<FfCfgKey, string | number>>

/** Always fatal, and never carries a secret value — lengths only, like `ff_cfg.py:describe`. */
export class FfCfgError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'FfCfgError'
  }
}

/**
 * The form, as the operator filled it in — strings throughout, because that is what an
 * `<input>` yields and what a CLI flag yields.
 *
 * A blank optional value is an ABSENT KEY, not an empty one: `ff_cfg.c` applies its own
 * defaults (from `spec/prd.md` → *Timing*), and writing today's default into the blob
 * would freeze it into every board flashed today. `ntp` is the one exception — `''` is a
 * meaningful value there ("no SNTP", the R0-fw-1 clock knob), so `undefined` omits it and
 * `''` writes it.
 */
export type FlashConfigInput = {
  apiBase: string
  mqttUri: string
  token?: string
  ssid?: string
  psk?: string
  link?: string
  ntp?: string
  hbS?: string
  power?: string
  wakeS?: string
}

/** A blank-tolerant integer parse. `field` names the *key*, never the value. */
function intOrUndefined(text: string | undefined, field: string): number | undefined {
  if (text === undefined || text.trim() === '') return undefined
  const value = Number(text)
  if (!Number.isInteger(value)) {
    throw new FfCfgError(`${field} must be a whole number of seconds`)
  }
  return value
}

/**
 * The operator's form as blob fields, in `KEY_ORDER`, with every blank omitted.
 *
 * Key order carries no meaning to the reader (it is JSON), but a stable order makes two
 * blobs diffable by eye and keeps the golden vector byte-comparable with the Python writer.
 */
export function buildFfCfgFields(input: FlashConfigInput): FfCfgFields {
  const fields: FfCfgFields = {}
  // `ff_cfg.py:fields_from_args` does `args.api_base.rstrip("/")`. Same here, and here
  // rather than in `encodeFfCfg`, so the two writers normalise at the same stage.
  fields.api_base = input.apiBase.replace(/\/+$/, '')
  fields.mqtt_uri = input.mqttUri
  if (input.token) fields.token = input.token
  if (input.ssid) fields.ssid = input.ssid
  if (input.psk) fields.psk = input.psk
  if (input.link) fields.link = input.link
  if (input.ntp !== undefined) fields.ntp = input.ntp
  const hb = intOrUndefined(input.hbS, 'heartbeat interval')
  if (hb !== undefined) fields.hb_s = hb
  if (input.power) fields.power = input.power
  const wake = intOrUndefined(input.wakeS, 'wake interval')
  if (wake !== undefined) fields.wake_s = wake
  return fields
}

/**
 * Refuse, before a token is minted, what the server or the firmware would refuse after.
 *
 * Mirrors `ff_cfg.py:validate` branch for branch. The ordering rule matters: a `sleepy`
 * board with no wake interval that reaches `POST /v1/enroll` spends a single-use token to
 * earn a 422 — the same rule the simulator and the CLI both follow.
 */
export function validateFfCfg(fields: FfCfgFields): void {
  const link = String(fields.link ?? 'wifi')
  if (!(LINKS as readonly string[]).includes(link)) {
    throw new FfCfgError(`link must be one of ${LINKS.join(', ')}, not "${link}"`)
  }
  if (link === 'wifi' && !fields.ssid) {
    throw new FfCfgError('link=wifi needs an SSID: the board cannot join a network without one')
  }
  const power = String(fields.power ?? 'always_on')
  if (!(POWER_CLASSES as readonly string[]).includes(power)) {
    throw new FfCfgError(`power must be one of ${POWER_CLASSES.join(', ')}, not "${power}"`)
  }
  if (power === 'sleepy' && !(Number(fields.wake_s ?? 0) > 0)) {
    throw new FfCfgError(
      'power=sleepy needs a positive wake interval, or POST /v1/enroll refuses the identity ' +
        '(422) and presence has no 2.5x window to compute from',
    )
  }
  if (fields.hb_s !== undefined && !(Number(fields.hb_s) > 0)) {
    throw new FfCfgError('the heartbeat interval must be positive')
  }
  for (const [scheme, key] of [
    ['http', 'api_base'],
    ['mqtt', 'mqtt_uri'],
  ] as const) {
    const value = String(fields[key] ?? '')
    if (!value.startsWith(`${scheme}://`) && !value.startsWith(`${scheme}s://`)) {
      throw new FfCfgError(
        `${key} must start with ${scheme}:// or ${scheme}s:// — the scheme is what selects ` +
          `TLS on the device (got "${value}")`,
      )
    }
  }
}

// CRC-32/IEEE: reflected, polynomial 0xEDB88320, init and xorout 0xFFFFFFFF. Exactly what
// `zlib.crc32` computes on the Python side and what `ff_cfg.c` checks on the device.
// Built once, lazily, at first use.
let crcTable: Uint32Array | null = null

function crc32Table(): Uint32Array {
  if (crcTable !== null) return crcTable
  const table = new Uint32Array(256)
  for (let n = 0; n < 256; n += 1) {
    let c = n
    for (let k = 0; k < 8; k += 1) {
      c = (c & 1) === 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    }
    table[n] = c >>> 0
  }
  crcTable = table
  return table
}

/** CRC-32/IEEE over exactly `bytes`, as an unsigned 32-bit number. */
export function crc32(bytes: Uint8Array): number {
  const table = crc32Table()
  let crc = 0xffffffff
  for (let i = 0; i < bytes.length; i += 1) {
    crc = table[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8)
  }
  return (crc ^ 0xffffffff) >>> 0
}

/**
 * Serialise `fields` into exactly `PARTITION_SIZE` bytes.
 *
 * `JSON.stringify` already emits compact separators and drops `undefined` values, which
 * is the "absent flags are absent keys, never nulls" rule. Keys are NOT sorted —
 * insertion order is the order, and `buildFfCfgFields` set it.
 */
export function encodeFfCfg(fields: FfCfgFields): Uint8Array {
  const missing = REQUIRED_KEYS.filter((key) => !fields[key])
  if (missing.length > 0) {
    throw new FfCfgError(
      `ff_cfg needs ${missing.join(', ')}: the agent has no compiled-in default for ` +
        'either, and a board that guesses one connects somewhere unexpected',
    )
  }

  const payload = new TextEncoder().encode(JSON.stringify(fields))
  if (payload.length > MAX_PAYLOAD) {
    // Lengths, never values: this message is rendered to the operator and `token`/`psk`
    // are both in the payload being measured.
    throw new FfCfgError(
      `ff_cfg payload is ${payload.length} bytes, ${payload.length - MAX_PAYLOAD} over the ` +
        `${MAX_PAYLOAD}-byte limit (${PARTITION_SIZE}-byte partition minus a ` +
        `${HEADER_SIZE}-byte header). A long URL plus a long Wi-Fi passphrase really does ` +
        'reach this; shorten one.',
    )
  }

  const blob = new Uint8Array(PARTITION_SIZE).fill(FILL)
  const view = new DataView(blob.buffer)
  for (let i = 0; i < 4; i += 1) {
    blob[i] = MAGIC.charCodeAt(i)
  }
  // Little-endian on every field. `DataView` defaults to BIG-endian, so the flag is not
  // optional: omit one and the firmware reads a version of 256 and refuses to boot.
  view.setUint16(4, VERSION, true)
  view.setUint16(6, 0, true)
  view.setUint32(8, payload.length, true)
  view.setUint32(12, crc32(payload), true)
  blob.set(payload, HEADER_SIZE)
  return blob
}
