// Write an `ff_cfg` blob with the BROWSER's encoder, from a shell. A dev tool — it is
// not bundled, not served, and not part of the app.
//
// It exists for one reason: the only hardware-free way to prove `src/ffcfg.ts` against
// the real firmware reader is to boot `agent/main/ff_cfg.c` on a blob this encoder made.
// `just agent-qemu` does exactly that with `.qemu/ff_cfg.bin`, so this script is the
// bridge between the two (R0-fe-3, T2-A).
//
//     npx vite-node scripts/emit-ffcfg.ts -- --out ../.qemu/ff_cfg.bin \
//       --api-base http://10.0.2.2:8080 --mqtt-uri mqtt://10.0.2.2:8883 \
//       --link ethernet --hb 10 --ntp pool.ntp.org --token "$FFE"
//
// It imports the app's encoder rather than copying it. A second copy here would agree
// with itself forever and prove nothing.
//
// **No secret is printed** — the same rule `agent/tools/ff_cfg.py` follows: key names and
// secret lengths, never values. The output holds a LIVE single-use enrollment token, so
// it is written 0600 into a directory that must be 0700 and gitignored (`.qemu/`).

import { chmodSync, mkdirSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { buildFfCfgFields, encodeFfCfg, validateFfCfg, type FlashConfigInput } from '../src/ffcfg'

const FILE_MODE = 0o600
const SECRET_KEYS = new Set(['token', 'psk'])

const USAGE = `usage: vite-node scripts/emit-ffcfg.ts -- --out FILE --api-base URL --mqtt-uri URI
             [--token T] [--ssid S] [--psk P] [--link wifi|ethernet]
             [--ntp HOST | --no-ntp] [--hb N] [--power always_on|sleepy] [--wake N]`

/** `--flag value` pairs and bare `--flag` switches. Deliberately tiny; no dependency. */
function parseArgs(argv: string[]): Map<string, string> {
  const args = new Map<string, string>()
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i]
    if (!arg.startsWith('--')) throw new Error(`unexpected argument "${arg}"\n${USAGE}`)
    const name = arg.slice(2)
    const next = argv[i + 1]
    if (next === undefined || next.startsWith('--')) {
      args.set(name, '')
    } else {
      args.set(name, next)
      i += 1
    }
  }
  return args
}

function main(argv: string[]): number {
  const args = parseArgs(argv)
  const out = args.get('out')
  const apiBase = args.get('api-base')
  const mqttUri = args.get('mqtt-uri')
  if (!out || !apiBase || !mqttUri) {
    process.stderr.write(`ff_cfg: --out, --api-base and --mqtt-uri are required\n${USAGE}\n`)
    return 2
  }

  const input: FlashConfigInput = {
    apiBase,
    mqttUri,
    token: args.get('token'),
    ssid: args.get('ssid'),
    psk: args.get('psk'),
    link: args.get('link'),
    // `--no-ntp` writes an explicit `""` ("no SNTP", the R0-fw-1 clock knob); an absent
    // `--ntp` leaves the key out so the firmware default applies.
    ntp: args.has('no-ntp') ? '' : args.get('ntp'),
    hbS: args.get('hb'),
    power: args.get('power'),
    wakeS: args.get('wake'),
  }

  let blob: Uint8Array
  let fields: ReturnType<typeof buildFfCfgFields>
  try {
    fields = buildFfCfgFields(input)
    validateFfCfg(fields)
    blob = encodeFfCfg(fields)
  } catch (err) {
    process.stderr.write(`ff_cfg: ${err instanceof Error ? err.message : String(err)}\n`)
    return 1
  }

  mkdirSync(dirname(out), { recursive: true })
  // The mode argument only applies when the file is CREATED, so chmod as well — an
  // existing 0644 blob from an earlier run must not keep a live token world-readable.
  // (`agent/tools/ff_cfg.py:main` carries the same pair of calls for the same reason.)
  writeFileSync(out, blob, { mode: FILE_MODE })
  chmodSync(out, FILE_MODE)

  const keys = Object.keys(fields)
  const shown = keys.filter((key) => !SECRET_KEYS.has(key))
  const secret = keys.filter((key) => SECRET_KEYS.has(key))
  process.stdout.write(`ff_cfg: wrote ${blob.length} bytes to ${out} (0${FILE_MODE.toString(8)})\n`)
  process.stdout.write(
    `  keys: ${shown.join(', ')}` +
      (secret.length > 0
        ? ` (+ ${secret.map((k) => `${k}=<${String(fields[k as keyof typeof fields]).length} chars, not shown>`).join(', ')})`
        : '') +
      '\n',
  )
  return 0
}

process.exitCode = main(process.argv.slice(2))
