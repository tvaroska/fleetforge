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
  MAX_NETWORKS,
  MAX_PAYLOAD,
  PARTITION_SIZE,
  VERSION,
  agentReadsNets,
  buildFfCfgFields,
  encodeFfCfg,
  validateFfCfg,
  type FfCfgFields,
} from './ffcfg'
import netsVector from './ffcfg.nets.vector.json'
import vector from './ffcfg.vector.json'

const VECTORS: [string, { fields: FfCfgFields; sha256: string }][] = [
  ['single', vector as { fields: FfCfgFields; sha256: string }],
  ['nets', netsVector as { fields: FfCfgFields; sha256: string }],
]

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

describe.each(VECTORS)('the ff_cfg golden vector (%s)', (_name, v) => {
  it('reproduces the digest the Python writer produced for the same fields', async () => {
    // THE contract test. `tests/test_ff_cfg.py::TestTypeScriptWriterAgrees` asserts the
    // same constant from the other side, so neither writer can move alone.
    expect(await digest(encodeFfCfg(v.fields))).toBe(v.sha256)
  })

  it('is ASCII-only, because that is the only place the two writers agree on bytes', () => {
    // Python defaults to ensure_ascii=True, JSON.stringify does not.
    expect(/^[\x20-\x7e]*$/.test(JSON.stringify(v.fields))).toBe(true)
  })

  it('is a config this writer accepts', () => {
    expect(() => validateFfCfg(v.fields)).not.toThrow()
  })
})

// The vector pins `encodeFfCfg`; these pin the path the browser actually takes, from the
// form's strings to the fields, so a one-network flash stays byte-identical to the format
// before `nets` and a three-network one matches what the Python writer makes.
describe('buildFfCfgFields reproduces the golden vectors', () => {
  const base = {
    apiBase: 'http://10.0.2.2:8080',
    mqttUri: 'mqtt://10.0.2.2:8883',
    token: vector.fields.token,
    ssid: 'fleetforge-test',
    psk: 'not-a-real-passphrase',
    link: 'wifi',
    hbS: '60',
  }

  it('writes the nets vector byte for byte, an empty passphrase as an absent key', async () => {
    const fields = buildFfCfgFields({
      ...base,
      nets: [
        { ssid: 'fleetforge-shed', psk: 'also-not-a-real-one' },
        { ssid: 'fleetforge-open', psk: '' },
      ],
    })
    expect(JSON.stringify(fields)).toBe(JSON.stringify(netsVector.fields))
    expect(JSON.stringify(fields)).not.toContain('"psk":""')
    expect(await digest(encodeFfCfg(fields))).toBe(netsVector.sha256)
  })

  it.each([
    ['nets undefined', undefined],
    ['nets empty', []],
  ])('one network (%s) still writes the single vector', async (_name, nets) => {
    const fields = buildFfCfgFields({ ...base, nets })
    expect('nets' in fields).toBe(false)
    expect(JSON.stringify(fields)).toBe(JSON.stringify(vector.fields))
    expect(await digest(encodeFfCfg(fields))).toBe(vector.sha256)
  })

  it('keeps a blank extra row, so the validator can refuse it by its form number', () => {
    const fields = buildFfCfgFields({ ...base, nets: [{ ssid: '', psk: '' }] })
    expect(fields.nets).toEqual([{ ssid: '' }])
    expect(() => validateFfCfg(fields)).toThrow(/network 2 needs an SSID/)
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
      nets: [{ ssid: 'b' }],
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

describe('validateFfCfg — known networks', () => {
  const SECRETS = ['s3cret-one', 's3cret-two', 's3cret-three', 's3cret-four']
  const WIFI: FfCfgFields = { ...MINIMAL, link: 'wifi', ssid: 'home', psk: SECRETS[0] }

  /** Refused with a message matching every pattern, and naming no passphrase. */
  function refuses(fields: FfCfgFields, ...patterns: RegExp[]) {
    let message = ''
    try {
      validateFfCfg(fields)
    } catch (err) {
      expect(err).toBeInstanceOf(FfCfgError)
      message = (err as Error).message
    }
    expect(message, 'expected a refusal').not.toBe('')
    for (const pattern of patterns) expect(message).toMatch(pattern)
    for (const secret of [...SECRETS, 'p'.repeat(20)]) expect(message).not.toContain(secret)
  }

  it('accepts four networks with an open one', () => {
    expect(() =>
      validateFfCfg({
        ...WIFI,
        nets: [{ ssid: 'shed', psk: SECRETS[1] }, { ssid: 'bench' }, { ssid: 'car', psk: SECRETS[3] }],
      }),
    ).not.toThrow()
  })

  it('accepts a list on ethernet, as the Python writer does (the QEMU proof)', () => {
    expect(() =>
      validateFfCfg({ ...WIFI, link: 'ethernet', nets: [{ ssid: 'shed' }] }),
    ).not.toThrow()
  })

  it('refuses five networks', () => {
    expect(MAX_NETWORKS).toBe(4)
    refuses(
      {
        ...WIFI,
        nets: [
          { ssid: 'b', psk: SECRETS[1] },
          { ssid: 'c', psk: SECRETS[2] },
          { ssid: 'd', psk: SECRETS[3] },
          { ssid: 'e' },
        ],
      },
      /at most 4/,
    )
  })

  it('refuses nets with no network 1, whatever the link', () => {
    refuses({ ...MINIMAL, link: 'ethernet', nets: [{ ssid: 'b', psk: SECRETS[1] }] }, /network 1/)
  })

  it('refuses an empty entry SSID by its form number', () => {
    refuses({ ...WIFI, nets: [{ ssid: '', psk: SECRETS[1] }] }, /network 2/, /SSID/)
  })

  it('counts SSID length in UTF-8 bytes, not characters', () => {
    // 17 characters, 34 bytes.
    refuses({ ...WIFI, nets: [{ ssid: 'é'.repeat(17) }] }, /network 2/, /32 bytes/)
    expect(() => validateFfCfg({ ...WIFI, nets: [{ ssid: 'é'.repeat(16) }] })).not.toThrow()
  })

  it('refuses a 65-byte entry passphrase', () => {
    refuses({ ...WIFI, nets: [{ ssid: 'b', psk: 'p'.repeat(65) }] }, /network 2/, /passphrase/)
    expect(() => validateFfCfg({ ...WIFI, nets: [{ ssid: 'b', psk: 'p'.repeat(64) }] })).not.toThrow()
  })

  it('refuses an over-length top-level passphrase and SSID', () => {
    refuses({ ...WIFI, psk: 'p'.repeat(65) }, /network 1/, /passphrase/)
    refuses({ ...WIFI, ssid: 's'.repeat(33) }, /network 1/, /SSID/)
  })

  it('refuses a duplicate of network 1 and a duplicate between entries', () => {
    refuses({ ...WIFI, nets: [{ ssid: 'home', psk: SECRETS[1] }] }, /network 2/, /repeats/)
    refuses(
      { ...WIFI, nets: [{ ssid: 'shed', psk: SECRETS[1] }, { ssid: 'shed', psk: SECRETS[2] }] },
      /network 3/,
      /repeats/,
    )
  })

  it('refuses a non-list nets and a non-object entry', () => {
    refuses({ ...WIFI, nets: 'shed' } as unknown as FfCfgFields, /list/)
    refuses({ ...WIFI, nets: ['shed'] } as unknown as FfCfgFields, /network 2/)
    refuses({ ...WIFI, nets: [{ ssid: 'b', psk: 7 }] } as unknown as FfCfgFields, /network 2/)
  })
})

describe('agentReadsNets', () => {
  it.each([
    ['0.4.6', true],
    ['0.4.5', false],
    ['0.4.6-rbtest', true],
    ['1.0.0', true],
    ['0.10.0', true],
    ['0.3.2', false],
    ['dev', null],
  ])('%s → %s', (version, expected) => {
    expect(agentReadsNets(version)).toBe(expected)
  })
})
