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
// Known networks (R2b-fe-12): `--ssid`/`--psk` are network 1, and each repeatable
// `--net SSID [PSK]` adds the next one in priority order (omit PSK for an open network),
// exactly as `agent/tools/ff_cfg.py --net` does.
//
// It imports the app's encoder rather than copying it. A second copy here would agree
// with itself forever and prove nothing.
//
// **No secret is printed** — the same rule `agent/tools/ff_cfg.py` follows: key names and
// secret lengths, never values. The output holds a LIVE single-use enrollment token, so
// it is written 0600 into a directory that must be 0700 and gitignored (`.qemu/`).

import { chmodSync, mkdirSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import {
  buildFfCfgFields,
  encodeFfCfg,
  validateFfCfg,
  type FlashConfigInput,
  type FlashNetworkInput,
} from '../src/ffcfg'

const FILE_MODE = 0o600
const SECRET_KEYS = new Set(['token', 'psk'])

const USAGE = `usage: vite-node scripts/emit-ffcfg.ts -- --out FILE --api-base URL --mqtt-uri URI
             [--token T] [--ssid S] [--psk P] [--link wifi|ethernet]
             [--ntp HOST | --no-ntp] [--hb N] [--power always_on|sleepy] [--wake N]
             [--net SSID [PSK]]...   (networks 2..N; at most 4 networks in all)`

/**
 * `--flag value` pairs and bare `--flag` switches, plus the repeatable `--net SSID [PSK]`
 * collected in order. Deliberately tiny; no dependency.
 */
function parseArgs(argv: string[]): { args: Map<string, string>; nets: FlashNetworkInput[] } {
  const args = new Map<string, string>()
  const nets: FlashNetworkInput[] = []
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i]
    // Never echo the argument: after `--net` it may be a passphrase.
    if (!arg.startsWith('--')) throw new Error(`unexpected argument at position ${i + 1}\n${USAGE}`)
    const name = arg.slice(2)
    if (name === 'net') {
      const ssid = argv[i + 1]
      if (ssid === undefined || ssid.startsWith('--')) {
        throw new Error(`--net number ${nets.length + 1} needs an SSID\n${USAGE}`)
      }
      i += 1
      const psk = argv[i + 1]
      if (psk !== undefined && !psk.startsWith('--')) {
        nets.push({ ssid, psk })
        i += 1
      } else {
        nets.push({ ssid })
      }
      continue
    }
    const next = argv[i + 1]
    if (next === undefined || next.startsWith('--')) {
      args.set(name, '')
    } else {
      args.set(name, next)
      i += 1
    }
  }
  return { args, nets }
}

function main(argv: string[]): number {
  let args: Map<string, string>
  let nets: FlashNetworkInput[]
  try {
    ;({ args, nets } = parseArgs(argv))
  } catch (err) {
    process.stderr.write(`ff_cfg: ${err instanceof Error ? err.message : String(err)}\n`)
    return 2
  }
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
    nets,
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
  // `nets` carries nested passphrases: it is reported as a count, never rendered.
  const shown = keys
    .filter((key) => !SECRET_KEYS.has(key))
    .map((key) =>
      key === 'nets' ? `nets=<${fields.nets?.length ?? 0} networks, passphrases not shown>` : key,
    )
  // Top-level `token`/`psk` only — the only keys whose values are strings to measure.
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
