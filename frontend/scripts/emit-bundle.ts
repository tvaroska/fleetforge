// Print an S0-fe-7 diagnostic bundle for the 2026-09-11 brownout session. A dev tool — it
// is not bundled, not served, and not part of the app.
//
//     npx vite-node scripts/emit-bundle.ts
//
// It exists because a bundle is read by a human, and the only way to judge "can someone who
// has never seen this repo tell what is wrong from the first fifteen lines?" is to look at
// one. It is the human-readable half of S0-fe-7's acceptance; the machine-readable half is
// `src/diagnostics.test.ts`.
//
// It replays the real fixture through the real `classifyConsoleLine` / `summarizeConsole` /
// `buildDiagnosticBundle` — no reimplementation here, or the output would agree with itself
// and prove nothing. Same precedent and invocation style as `scripts/emit-ffcfg.ts`.
//
// **Every secret below is FAKE and is planted on purpose**: a Wi-Fi passphrase, a broker
// password inside an MQTT URI, and an `ffe_` enrolment token in a log line the firmware
// printed. None of them may appear in the output — that is what the `! grep` half of the
// acceptance checks.

import { classifyConsoleLine, summarizeConsole } from '../src/boardConsole'
import { buildDiagnosticBundle, uriSecrets, type DiagnosticContext } from '../src/diagnostics'
import { BENCH_2026_09_11 } from '../src/fixtures/bench-2026-09-11'

/** Fake. Not a credential; it has never been anywhere near a board. */
const PASSPHRASE = 'correct-horse-battery-staple'
/** Fake. The broker password rides in the URI, exactly as an operator would paste it. */
const MQTT_URI = 'mqtts://fleet:s3cr3tbrokerpw@bench.local:8883'
/** Fake. Printed by the *firmware*, so nothing but the shape rule can catch it. */
const TOKEN_LINE =
  'E (2600) ff-enroll: enroll 401 with ffe_11111111-1111-4111-8111-111111111111.s3cr3tflashersecret'

const T0 = Date.parse('2026-09-11T08:19:13.000Z')
const NOW = T0 + 120_000

// The planted lines go FIRST, as an earlier attempt on the same board. They must not be
// the newest failure in the log: `summarizeConsole` names the most recent explained fault,
// and the fault this bundle has to lead with is the brownout the session was actually
// about.
const lines = [TOKEN_LINE, `I (900) ff-wifi: psk ${PASSPHRASE}`, ...BENCH_2026_09_11]
const events = lines.map((line, i) => classifyConsoleLine(line, i, T0 + i * 1000))
const summary = summarizeConsole(events, NOW)

const context: DiagnosticContext = {
  chip: {
    chipName: 'ESP32',
    description: 'ESP32-D0WD-V3 (revision v3.1)',
    macAddress: 'A4:CF:12:B3:DE:90',
    flashSizeBytes: 4 * 1024 * 1024,
    features: ['WiFi', 'BT', 'Dual Core'],
  },
  build: {
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
    config_partition: { label: 'ff_cfg', offset: 0x12000, size: 4096 },
    parts: [],
  },
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
}

process.stdout.write(
  buildDiagnosticBundle({
    events,
    summary,
    context,
    page: {
      origin: 'https://bingo.tvaroska.sk',
      userAgent:
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
    },
    now: NOW,
  }),
)
