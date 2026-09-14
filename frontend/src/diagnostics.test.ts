// What these tests defend:
//
// 1. **The bundle is the 2026-09-11 session.** The whole log, verbatim, with the fault
//    named in plain English at the top. If it is not, the escalation path is back to
//    retyping a UART log into a chat window.
// 2. **No secret ever leaves in it.** A bundle is DESIGNED to be pasted into a chat
//    window, so it is the single most likely way a live `ffe_` token, a Wi-Fi passphrase
//    or a broker password leaves the operator's machine. Three separate rules, one test
//    each, plus the anti-vacuity test below.
// 3. **It still says something.** Tests 2–4 would all pass over an empty string, so test 6
//    pins the fields that make the artifact worth pasting.

import { describe, expect, it } from 'vitest'
import { classifyConsoleLine, summarizeConsole, type ConsoleEvent } from './boardConsole'
import {
  MIN_SCRUB_LENGTH,
  buildDiagnosticBundle,
  redactSecrets,
  uriSecrets,
  type DiagnosticContext,
} from './diagnostics'
import { BENCH_2026_09_11 } from './fixtures/bench-2026-09-11'
import type { AgentBuildInfo } from './api'

const T0 = Date.parse('2026-09-11T08:19:13.000Z')

/** Fake, all of them. Nothing here has ever been near a board. */
const PASSPHRASE = 'correct-horse-battery-staple'
const PLAINTEXT = 'ffe_11111111-1111-4111-8111-111111111111.s3cr3tvalue'
const BROKER_PASSWORD = 's3cr3tbrokerpw'
const MQTT_URI = `mqtts://fleet:${BROKER_PASSWORD}@bench.local:8883`

const PAGE = { origin: 'https://bingo.tvaroska.sk', userAgent: 'Mozilla/5.0 (X11) Chrome/140' }

// S0-infra-3 build identity. Distinct 64-hex values so a test can tell which one was
// printed where — the two fields sit next to each other and answer different questions.
const CONFIG_SHA256 = 'c0'.repeat(32)
const BUILD_DIGEST = 'b1'.repeat(32)

function build(overrides: Partial<AgentBuildInfo> = {}): AgentBuildInfo {
  return {
    target: 'esp32',
    chip_family: 'ESP32',
    agent_version: '0.3.1',
    idf_version: 'v5.5.5',
    idf_image: 'espressif/idf:v5.5.5',
    source_commit: 'deadbeef',
    built_at: '2026-09-11T00:00:00Z',
    partition_layout: 'ab-4m-v1',
    ota_slot_size: 1966080,
    flash_size: '4MB',
    config_sha256: CONFIG_SHA256,
    build_digest: BUILD_DIGEST,
    config_partition: { label: 'ff_cfg', offset: 0x12000, size: 4096 },
    parts: [],
    ...overrides,
  }
}

function context(overrides: Partial<DiagnosticContext> = {}): DiagnosticContext {
  return {
    chip: {
      chipName: 'ESP32',
      description: 'ESP32-D0WD-V3 (revision v3.1)',
      macAddress: 'A4:CF:12:B3:DE:90',
      flashSizeBytes: 4 * 1024 * 1024,
      features: ['WiFi', 'BT', 'Dual Core'],
    },
    build: build(),
    agentVersion: '0.3.1',
    serverVersion: '0.3.1',
    config: {
      apiBase: 'https://bingo.tvaroska.sk',
      mqttUri: MQTT_URI,
      link: 'wifi',
      ssid: 'bench-2g',
      ntp: null,
      hbS: null,
      power: 'always_on',
      wakeS: null,
      pskLength: PASSPHRASE.length,
    },
    knownSecrets: [PASSPHRASE, ...uriSecrets(MQTT_URI)],
    ...overrides,
  }
}

/** Same idiom as `boardConsole.test.ts`: one line per second through the real classifier. */
function replay(lines: string[]): ConsoleEvent[] {
  return lines.map((line, i) => classifyConsoleLine(line, i, T0 + i * 1000))
}

function bundleOf(lines: string[], overrides: Partial<DiagnosticContext> | null = {}) {
  const events = replay(lines)
  const at = T0 + lines.length * 1000
  return buildDiagnosticBundle({
    events,
    summary: summarizeConsole(events, at),
    context: overrides === null ? null : context(overrides),
    page: PAGE,
    now: at,
  })
}

describe('buildDiagnosticBundle', () => {
  it('carries the 2026-09-11 brownout session verbatim and names the fault', () => {
    const bundle = bundleOf(BENCH_2026_09_11)

    // Every cycle, once each — the fault section points at its evidence rather than
    // reprinting it, so "how many times did this board brown out?" is answerable by eye.
    expect(bundle.split('E BOD: Brownout detector was triggered').length - 1).toBe(3)
    expect(bundle).toContain('rst:0xf (RTCWDT_BROWN_OUT_RESET)')
    // The fault is NAMED, not merely logged. This is the task's stated acceptance.
    expect(bundle).toContain('rail collapsed during radio calibration')
    expect(bundle).toContain('reboot loop')
    expect(bundle).toContain('yes — 3 boots without reaching the fleet')
    // And it is at the top: a recipient must not have to scroll to find the diagnosis.
    expect(bundle.split('\n').slice(0, 15).join('\n')).toContain(
      'rail collapsed during radio calibration',
    )
  })

  it('lets no ffe_ token survive, wherever it came from', () => {
    // Printed by the FIRMWARE, so it was never in `knownSecrets` — only the shape rule
    // can catch this one.
    const bundle = bundleOf([...BENCH_2026_09_11, `E (2600) ff-enroll: enroll 401 with ${PLAINTEXT}`])

    expect(bundle).not.toContain(PLAINTEXT)
    expect(bundle).not.toContain('s3cr3tvalue')
    expect(bundle).toContain('ffe_[REDACTED]')
  })

  it('never prints the Wi-Fi passphrase, even if the firmware does', () => {
    const bundle = bundleOf([...BENCH_2026_09_11, `I (900) ff-wifi: psk ${PASSPHRASE}`])

    expect(bundle).not.toContain(PASSPHRASE)
    // Lengths only, the same sentence `ff_cfg_log()` prints.
    expect(bundle).toContain('passphrase 28 chars (never printed)')
  })

  it('strips the MQTT password from every URI and keeps the username', () => {
    // The second URI is one this page NEVER saw — a different broker, printed by the
    // firmware — so only the shape rule can reach it. Without it the literal scrub alone
    // would carry this test and rule 2 would be untested.
    const bundle = bundleOf([
      ...BENCH_2026_09_11,
      `E (3000) ff-mqtt: cannot reach ${MQTT_URI}`,
      'E (3100) ff-mqtt: cannot reach mqtts://relay:hunter2pw@spare.local:8883',
    ])

    expect(bundle).not.toContain(BROKER_PASSWORD)
    expect(bundle).not.toContain('hunter2pw')
    // Twice: once in the config section, once in the line the board printed.
    expect(bundle.split('mqtts://fleet:[REDACTED]@').length - 1).toBe(2)
    // The username survives — it is diagnostic, and it is not a credential.
    expect(bundle).toContain('mqtts://relay:[REDACTED]@spare.local:8883')
  })

  it('does not shred the log over a two-character "secret"', () => {
    const bundle = bundleOf(BENCH_2026_09_11, { knownSecrets: ['ff'] })

    expect(bundle).toContain('E BOD: Brownout detector was triggered')
    expect(bundle).toContain('ff-agent: fleetforge agent 0.3.0')
    expect(MIN_SCRUB_LENGTH).toBe(4)
  })

  it('still says something — the anti-vacuity guard for the tests above', () => {
    const bundle = bundleOf(BENCH_2026_09_11)

    expect(bundle).toContain('bench-2g')
    expect(bundle).toContain('ESP32-D0WD-V3 (revision v3.1)')
    expect(bundle).toContain('api_base     https://bingo.tvaroska.sk')
    expect(bundle).toContain('server       0.3.1')
    expect(bundle).toContain('layout ab-4m-v1')
    // Both agent versions: what this page would flash, and what the board says it runs.
    // They differ exactly when the board is carrying a stale flash.
    expect(bundle).toContain('on board     fleetforge agent 0.3.0')
    expect(bundle).toContain('device id    3c8427b1f0a4 (reported by the board)')
    // `origin`, never `href`.
    expect(bundle).toContain('page         https://bingo.tvaroska.sk')
    expect(bundle).toContain('generated    2026-09-11T')
  })

  it('produces a bundle from a console opened without flashing', () => {
    const bundle = bundleOf(BENCH_2026_09_11, null)

    expect(bundle).toContain('E BOD: Brownout detector was triggered')
    expect(bundle).toContain('rail collapsed during radio calibration')
    expect(bundle).toContain('not detected in this tab')
    expect(bundle).toContain('unavailable (this page could not reach /v1/healthz)')
  })

  it('names the exact build the board was flashed from (S0-infra-3)', () => {
    const bundle = bundleOf(BENCH_2026_09_11)

    // In FULL. Truncating to a prefix would make "is this the same build as yours?" a
    // judgement call, which is the thing this field removes.
    expect(bundle).toContain(`config       ${CONFIG_SHA256}`)
    expect(bundle).toContain(`build id     ${BUILD_DIGEST}`)
    // Above the log, with the rest of the identification: a reader must not have to
    // scroll past a hundred UART lines to answer "which build?".
    expect(bundle.indexOf(BUILD_DIGEST)).toBeLessThan(bundle.indexOf('console log ('))
  })

  it('says so plainly when the build predates build identity', () => {
    const bundle = bundleOf(BENCH_2026_09_11, {
      build: build({ config_sha256: null, build_digest: null }),
    })

    expect(bundle).toContain('config       unknown (this bundle predates build identity)')
    expect(bundle).toContain('build id     unknown (this bundle predates build identity)')
    // Not the string `null`, and not an empty value that reads as a bug in this page.
    expect(bundle).not.toContain('config       null')
  })

  it('leaves prose that merely mentions a token alone', () => {
    // Three strings in `boardConsole.ts` contain a bare `ffe_` followed by a space. The
    // `{6,}` floor and the character class are the only things keeping them intact.
    const halted =
      "E (2900) ff-agent: halted: this board's enrollment token was refused for good — " +
      're-flash ff_cfg with a fresh ffe_ token (POST /v1/enrollment-tokens)'
    const bundle = bundleOf([
      ...BENCH_2026_09_11,
      halted,
      // Makes the panel's own `ffe_ token` prose the printed hint, so both the log copy
      // and the hint copy of that phrase are covered by this one case.
      'E (3000) ff-enroll: enroll 401 unauthorized',
    ])

    expect(bundle).toContain('a fresh ffe_ token (POST /v1/enrollment-tokens)')
    expect(bundle).toContain('The server does not know this ffe_ token.')
  })
})

describe('redactSecrets', () => {
  it('treats a secret as a literal, not as a pattern', () => {
    // A passphrase is arbitrary text. `new RegExp(secret)` here would be a regex injection
    // into our own redactor — and `.*` would eat the whole bundle.
    expect(redactSecrets('psk .* here', ['.*'])).toBe('psk .* here')
    expect(redactSecrets('psk a.*b here', ['a.*b'])).toBe('psk [REDACTED] here')
  })

  it('strips userinfo from a URI this page has never seen', () => {
    // Rule 2 exists for exactly this: a credential the page was never handed, in a URI
    // the FIRMWARE printed. `knownSecrets` is empty here on purpose.
    expect(redactSecrets('cannot reach https://ops:letmein99@example.test/v1/enroll', [])).toBe(
      'cannot reach https://ops:[REDACTED]@example.test/v1/enroll',
    )
    // A URI with no userinfo is left exactly as it was.
    expect(redactSecrets('mqtts://bingo.tvaroska.sk:8883', [])).toBe('mqtts://bingo.tvaroska.sk:8883')
  })

  it('covers ffa_ admin tokens as well as ffe_ enrolment ones', () => {
    expect(redactSecrets('Authorization: Bearer ffa_abcdef0123456789', [])).toBe(
      'Authorization: Bearer ffa_[REDACTED]',
    )
  })
})

describe('uriSecrets', () => {
  it('returns the password half of a userinfo, and nothing otherwise', () => {
    expect(uriSecrets(MQTT_URI)).toEqual([BROKER_PASSWORD])
    expect(uriSecrets('mqtts://bingo.tvaroska.sk:8883')).toEqual([])
    expect(uriSecrets('')).toEqual([])
  })
})
