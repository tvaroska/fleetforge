// Every failure in this file is a board that would not boot.
//
// `ffcfg.ts` is one of three implementations of the same sixteen bytes
// (`agent/tools/ff_cfg.py`, `agent/main/ff_cfg.c`). The firmware reader cannot be run
// here, so the contract is held by the golden vector: the digest in `ffcfg.vector.json`
// was produced by the PYTHON writer, and `tests/test_ff_cfg.py` asserts the same digest
// from that side. If the two writers ever disagree about a byte, exactly one of the two
// suites goes red — which is the point.

import { describe, expect, it } from 'vitest'
import {
  FILL,
  FfCfgError,
  HEADER_SIZE,
  KEY_ORDER,
  MAGIC,
  MAX_PAYLOAD,
  PARTITION_SIZE,
  VERSION,
  buildFfCfgFields,
  encodeFfCfg,
  validateFfCfg,
  type FfCfgFields,
} from './ffcfg'
import vector from './ffcfg.vector.json'

const MINIMAL: FfCfgFields = { api_base: 'http://10.0.2.2:8080', mqtt_uri: 'mqtt://10.0.2.2:8883' }

async function digest(bytes: Uint8Array): Promise<string> {
  const hash = await crypto.subtle.digest('SHA-256', new Uint8Array(bytes))
  return Array.from(new Uint8Array(hash))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}

/** A second, independently written CRC-32 — a table-driven bug agrees with itself. */
function bitwiseCrc32(bytes: Uint8Array): number {
  let crc = 0xffffffff
  for (const byte of bytes) {
    crc ^= byte
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc & 1) === 1 ? (crc >>> 1) ^ 0xedb88320 : crc >>> 1
    }
  }
  return (crc ^ 0xffffffff) >>> 0
}

function header(blob: Uint8Array) {
  const view = new DataView(blob.buffer, blob.byteOffset, blob.byteLength)
  return {
    magic: String.fromCharCode(...blob.slice(0, 4)),
    version: view.getUint16(4, true),
    reserved: view.getUint16(6, true),
    payloadLen: view.getUint32(8, true),
    crc: view.getUint32(12, true),
  }
}

describe('the ff_cfg golden vector', () => {
  it('reproduces the digest the Python writer produced for the same fields', async () => {
    // THE contract test. `tests/test_ff_cfg.py::TestTypeScriptWriterAgrees` asserts the
    // same constant from the other side, so neither writer can move alone.
    expect(await digest(encodeFfCfg(vector.fields))).toBe(vector.sha256)
  })

  it('is ASCII-only, because that is the only place the two writers agree on bytes', () => {
    // Python defaults to ensure_ascii=True, JSON.stringify does not.
    expect(/^[\x20-\x7e]*$/.test(JSON.stringify(vector.fields))).toBe(true)
  })
})

describe('encodeFfCfg', () => {
  it('is exactly one partition long, 0xff after the payload', () => {
    const blob = encodeFfCfg(MINIMAL)
    expect(blob.length).toBe(PARTITION_SIZE)
    const { payloadLen } = header(blob)
    expect(new Set(blob.slice(HEADER_SIZE + payloadLen))).toEqual(new Set([FILL]))
  })

  it('writes the documented little-endian header', () => {
    const blob = encodeFfCfg(MINIMAL)
    const { magic, version, reserved, payloadLen, crc } = header(blob)
    expect(magic).toBe(MAGIC)
    expect(version).toBe(VERSION)
    expect(reserved).toBe(0)

    const payload = blob.slice(HEADER_SIZE, HEADER_SIZE + payloadLen)
    expect(JSON.parse(new TextDecoder().decode(payload))).toEqual(MINIMAL)
    expect(crc).toBe(bitwiseCrc32(payload))
  })

  it('emits compact JSON — 4080 bytes is a real ceiling', () => {
    const blob = encodeFfCfg(MINIMAL)
    const { payloadLen } = header(blob)
    expect(new TextDecoder().decode(blob.slice(HEADER_SIZE, HEADER_SIZE + payloadLen))).not.toContain(
      ', ',
    )
  })

  it('refuses the two keys the firmware has no default for', () => {
    expect(() => encodeFfCfg({ api_base: 'http://x:8080' })).toThrow(/mqtt_uri/)
    expect(() => encodeFfCfg({ api_base: 'http://x:8080' })).toThrow(FfCfgError)
  })

  it('refuses an oversized payload without printing the secrets it measured', () => {
    const fields = {
      ...MINIMAL,
      token: 'ffe_the-secret-token',
      psk: 'p'.repeat(MAX_PAYLOAD),
    }
    try {
      encodeFfCfg(fields)
      throw new Error('expected FfCfgError')
    } catch (err) {
      expect(err).toBeInstanceOf(FfCfgError)
      const message = (err as Error).message
      expect(message).toContain('over the')
      expect(message).not.toContain(fields.token)
      expect(message).not.toContain('ppppp')
    }
  })
})

describe('buildFfCfgFields', () => {
  it('emits keys in KEY_ORDER', () => {
    const fields = buildFfCfgFields({
      apiBase: 'http://host:8080',
      mqttUri: 'mqtt://host:8883',
      token: 'ffe_x',
      ssid: 'net',
      psk: 'pw',
      link: 'wifi',
      ntp: 'pool.ntp.org',
      hbS: '30',
      power: 'sleepy',
      wakeS: '120',
    })
    expect(Object.keys(fields)).toEqual([...KEY_ORDER])
  })

  it('omits every blank — an absent key is the firmware default, a written one freezes today', () => {
    const fields = buildFfCfgFields({
      apiBase: 'http://host:8080',
      mqttUri: 'mqtt://host:8883',
      link: 'ethernet',
      token: '',
      ssid: '',
      psk: '',
      hbS: '',
      wakeS: '',
    })
    expect(Object.keys(fields)).toEqual(['api_base', 'mqtt_uri', 'link'])
  })

  it('treats an empty ntp as a value and an absent one as absent', () => {
    // `''` is "no SNTP", the R0-fw-1 clock knob; `undefined` leaves the firmware default.
    expect(buildFfCfgFields({ apiBase: 'http://h', mqttUri: 'mqtt://h', ntp: '' }).ntp).toBe('')
    expect('ntp' in buildFfCfgFields({ apiBase: 'http://h', mqttUri: 'mqtt://h' })).toBe(false)
  })

  it('strips a trailing slash from api_base, exactly as fields_from_args does', () => {
    expect(
      buildFfCfgFields({ apiBase: 'http://host:8080///', mqttUri: 'mqtt://host:8883' }).api_base,
    ).toBe('http://host:8080')
  })

  it('refuses a non-integer interval', () => {
    expect(() => buildFfCfgFields({ apiBase: 'http://h', mqttUri: 'mqtt://h', hbS: 'soon' })).toThrow(
      FfCfgError,
    )
  })
})

describe('validateFfCfg', () => {
  it('accepts a valid ethernet config', () => {
    expect(() => validateFfCfg({ ...MINIMAL, link: 'ethernet', hb_s: 10 })).not.toThrow()
  })

  it('rejects link=wifi with no SSID', () => {
    expect(() => validateFfCfg({ ...MINIMAL, link: 'wifi' })).toThrow(/SSID/i)
  })

  it('rejects power=sleepy with no wake interval', () => {
    expect(() => validateFfCfg({ ...MINIMAL, link: 'ethernet', power: 'sleepy' })).toThrow(/sleepy/)
  })

  it('rejects a non-positive heartbeat', () => {
    expect(() => validateFfCfg({ ...MINIMAL, link: 'ethernet', hb_s: 0 })).toThrow(/heartbeat/)
  })

  it('rejects a schemeless mqtt_uri — the scheme is what selects TLS on the device', () => {
    expect(() =>
      validateFfCfg({ ...MINIMAL, mqtt_uri: '10.0.2.2:8883', link: 'ethernet' }),
    ).toThrow(/mqtt_uri/)
  })

  it('rejects an unknown link', () => {
    expect(() => validateFfCfg({ ...MINIMAL, link: 'lora' })).toThrow(/link/)
  })
})
