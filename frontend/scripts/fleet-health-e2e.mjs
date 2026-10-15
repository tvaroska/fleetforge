// R4-fe-1: the fleet table's Health cell played through the REAL dashboard in a real
// Chromium, against the dev stack, with SIMULATED boards, and graded mechanically. It is
// the harness for "a `panic` reset shows as a warning badge" and "live fleet status".
//
// What it plays, in one logged-in session:
//
//   panic-badge     the fault board's row has a `[data-testid=reset-badge]` (data-reason
//                   "panic", the word in its text, computed bold weight and a border: the
//                   monochrome theme's two extra axes). The control board (poweron) has NO
//                   badge and the muted text "last reset: poweron"
//   health-fields   the fault row says "boot ok", "heap low 95.7 KiB" (the simulator sends
//                   min_free_heap 98000), an uptime, "-61 dBm", and "panic ×1"
//   live-metric     the `sim_counter` metric increases on the page with NO reload and no
//                   navigation (a sentinel on `window` survives), and the uptime text moves
//   traps           no token or password in any URL the browser requested
//
// The boards are simulator processes (`fleetforge.simulator fleet`): `fault` announces
// `--reset-reason panic`, heartbeats every 5 s and records `sim_counter` every 2 s; `calm`
// is the control (reset reason poweron, heartbeats, no telemetry).
//
// What it does NOT cover: the real agent (QEMU territory, R4-test-1). The simulator reports
// what it was told. Not a test and not wired into `npm test`. It needs the dev stack
// (`just up`), `uv`, and the admin password. Playwright is resolved at runtime and is
// deliberately NOT a dependency of this package (see `update-flow-e2e.mjs`).
//
//   FF_ADMIN_PASSWORD=… \
//     PLAYWRIGHT_MODULE="$(npm root -g)/playwright/index.mjs" \
//     node scripts/fleet-health-e2e.mjs [baseURL=http://localhost:8088] [outDir=/tmp/ff-r4-fe-1]
//
// or `just health-e2e`. The password falls back to the `FF_ADMIN_PASSWORD=` line of the
// repo-root `.env`; it is never printed and never put in an argv: the simulators get it
// through their environment.
//
// Every run uses a fresh prefix, so it enrolls two fresh boards with fresh fault history and
// leaves them (offline once the simulators exit) in the dev database; it deletes nothing.
//
// Exit: 0 all pass, 1 any scenario failed, 2 setup failed (stack down, login refused, the
// boards never came online). `E2E_BREAK=<scenario id>` flips that scenario's primary
// expectation on purpose, to prove the harness can fail (the vacuity check).

import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createWriteStream, readFileSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? 'playwright')

const BASE = process.argv[2] ?? 'http://localhost:8088'
const OUT = process.argv[3] ?? '/tmp/ff-r4-fe-1'
const BREAK = process.env.E2E_BREAK ?? null
const ROOT = fileURLToPath(new URL('../../', import.meta.url))
const RUN = Date.now().toString(36)

function readPassword() {
  if (process.env.FF_ADMIN_PASSWORD) return process.env.FF_ADMIN_PASSWORD
  try {
    const line = readFileSync(`${ROOT}.env`, 'utf8')
      .split('\n')
      .find((l) => l.startsWith('FF_ADMIN_PASSWORD='))
    if (line !== undefined) {
      const value = line.slice('FF_ADMIN_PASSWORD='.length).trim()
      return value.replace(/^(['"])(.*)\1$/, '$2')
    }
  } catch {
    // no .env: handled below
  }
  return null
}

const PASSWORD = readPassword()

function setupFailed(message) {
  console.error(`SETUP FAILED: ${message}`)
  process.exit(2)
}

if (!PASSWORD) setupFailed('no FF_ADMIN_PASSWORD in the environment or in the repo-root .env')

await mkdir(OUT, { recursive: true })

// ── Simulated boards ──────────────────────────────────────────────────────────────────

// `fleetforge.simulator.device.derive_device_id`: the same bytes in JS.
function deviceIdOf(name) {
  const d = createHash('sha256').update(name, 'utf8').digest()
  return Buffer.from([(d[0] & 0xfe) | 0x02, ...d.subarray(1, 6)]).toString('hex')
}

const BOARDS = {
  fault: { prefix: `fh${RUN}`, extra: ['--reset-reason', 'panic', '--telemetry-every', '2'] },
  calm: { prefix: `fh${RUN}c`, extra: [] },
}
for (const [key, board] of Object.entries(BOARDS)) {
  board.name = `${board.prefix}-01`
  board.id = deviceIdOf(board.name)
  board.log = `${OUT}/sim-${key}.log`
}

const children = []

function killAll(signal) {
  for (const child of children) {
    try {
      process.kill(-child.pid, signal) // the whole group: `uv run` forks the interpreter
    } catch {
      // already gone
    }
  }
}
process.on('exit', () => killAll('SIGKILL'))

function startBoard(board) {
  const args = [
    'run', 'python', '-m', 'fleetforge.simulator', 'fleet',
    '--count', '1',
    '--prefix', board.prefix,
    '--platform-type', 'esp32s3',
    '--heartbeat-interval', '5',
    '--duration', '300', // the safety net: an orphaned simulator exits on its own
    '--api-base', BASE,
    ...board.extra,
  ]
  const log = createWriteStream(board.log)
  // The password goes through the environment, never argv.
  const child = spawn('uv', args, {
    cwd: ROOT,
    env: { ...process.env, PYTHONPATH: 'src', FF_ADMIN_PASSWORD: PASSWORD },
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: true,
  })
  child.stdout.pipe(log, { end: false })
  child.stderr.pipe(log, { end: false })
  child.exited = new Promise((resolve) => child.on('close', resolve))
  children.push(child)
  board.child = child
}

async function stopBoards() {
  killAll('SIGINT')
  const timer = setTimeout(() => killAll('SIGKILL'), 5000)
  await Promise.all(children.map((c) => c.exited))
  clearTimeout(timer)
}

// ── Helpers ───────────────────────────────────────────────────────────────────────────

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

// Poll `fn` until it returns something other than null/false, or give up (-> null).
async function until(fn, timeoutMs, intervalMs = 500) {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const value = await fn()
    if (value !== null && value !== false && value !== undefined) return value
    if (Date.now() > deadline) return null
    await sleep(intervalMs)
  }
}

// A request from inside the page, so it carries the session cookie exactly as `api.ts` does.
function api(page, method, path) {
  return page.evaluate(
    async ({ method, path }) => {
      const response = await fetch(path, { method, credentials: 'same-origin' })
      const text = await response.text()
      let json = null
      try {
        json = JSON.parse(text)
      } catch {
        // not JSON
      }
      return { status: response.status, json, text }
    },
    { method, path },
  )
}

async function device(page, id) {
  const { json } = await api(page, 'GET', '/v1/devices')
  return json?.devices?.find((d) => d.device_id === id) ?? null
}

async function login(page) {
  await page.waitForSelector('input[type=password]')
  await page.fill('input[type=password]', PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForSelector('[data-testid=device-row]', { timeout: 15_000 })
}

const rowOf = (page, id) => page.locator(`[data-testid=device-row][data-device-id="${id}"]`)
const flat = (s) => s.replace(/\s+/g, ' ').trim()

// The number after `sim_counter` in the metric element, or null.
async function counterOf(page, id) {
  const el = rowOf(page, id).locator('[data-testid=metric][data-name=sim_counter]')
  if ((await el.count()) === 0) return null
  const m = /sim_counter\s+(-?[\d.]+)/.exec(await el.innerText())
  return m === null ? null : Number(m[1])
}

// ── Scenarios ─────────────────────────────────────────────────────────────────────────

const requested = []

const SCENARIOS = [
  {
    id: 'panic-badge',
    async run(page) {
      const problems = []
      const fault = BOARDS.fault
      const calm = BOARDS.calm
      // E2E_BREAK flips the primary expectation: the badge is looked for on the CALM row.
      const subject = BREAK === 'panic-badge' ? calm : fault
      const badge = rowOf(page, subject.id).locator('[data-testid=reset-badge]')
      const text = []
      if ((await badge.count()) !== 1) problems.push(`${subject.name}: expected one reset-badge, found ${await badge.count()}`)
      else {
        const info = await badge.evaluate((el) => {
          const cs = getComputedStyle(el)
          return { reason: el.getAttribute('data-reason'), text: el.innerText, weight: cs.fontWeight, border: cs.borderTopStyle }
        })
        text.push(`badge: ${JSON.stringify(info)}`)
        if (info.reason !== 'panic') problems.push(`data-reason is ${info.reason}, wanted panic`)
        if (!info.text.includes('panic')) problems.push(`badge text lacks the word panic: ${info.text}`)
        if (Number(info.weight) < 700) problems.push(`badge weight is ${info.weight}, wanted bold`)
        if (info.border === 'none') problems.push('badge has no border')
        if (!(await badge.isVisible())) problems.push('badge is not visible')
      }
      const calmRow = rowOf(page, calm.id)
      if ((await calmRow.locator('[data-testid=reset-badge]').count()) !== 0 && BREAK !== 'panic-badge')
        problems.push('the control row has a reset badge')
      const reason = calmRow.locator('[data-testid=reset-reason]')
      const reasonText = (await reason.count()) === 0 ? '' : flat(await reason.innerText())
      text.push(`control: ${reasonText}`)
      if (!reasonText.includes('last reset: poweron')) problems.push(`the control row says "${reasonText}", wanted last reset: poweron`)
      return { problems, text: text.join('\n'), shot: rowOf(page, fault.id) }
    },
  },
  {
    id: 'health-fields',
    async run(page) {
      const problems = []
      const cell = rowOf(page, BOARDS.fault.id).locator('[data-testid=health-cell]')
      const cellText = flat(await cell.innerText())
      const wanted = BREAK === 'health-fields'
        ? ['boot not confirmed']
        : ['boot ok', 'heap low 95.7 KiB', 'up ', '-61 dBm', 'panic ×1']
      for (const piece of wanted) if (!cellText.includes(piece)) problems.push(`the health cell lacks "${piece}": ${cellText}`)
      const faults = await cell.locator('[data-testid=fault-resets]').innerText().catch(() => '')
      if (BREAK !== 'health-fields' && !faults.includes('panic ×1')) problems.push(`fault-resets says "${faults}"`)
      return { problems, text: cellText, shot: rowOf(page, BOARDS.fault.id) }
    },
  },
  {
    id: 'live-metric',
    async run(page, notes) {
      const problems = []
      const id = BOARDS.fault.id
      await page.evaluate((run) => { window.__ffRun = run }, RUN)
      const vitals = () => rowOf(page, id).locator('[data-testid=device-vitals]').innerText().then(flat)
      const v1 = await counterOf(page, id)
      const up1 = await vitals()
      if (v1 === null) return { problems: ['no sim_counter metric on the fault row'], text: '(none)', shot: rowOf(page, id) }
      let changed = null
      if (BREAK === 'live-metric') {
        // Broken: expect NO change after 3 s, which a live board makes false.
        await sleep(3000)
        const v = await counterOf(page, id)
        if (v === v1) changed = v
      } else {
        changed = await until(async () => {
          const v = await counterOf(page, id)
          return v !== null && v > v1 ? v : null
        }, 20_000, 500)
      }
      const up2 = await vitals()
      const sentinel = await page.evaluate(() => window.__ffRun)
      notes.push(`sim_counter ${v1} -> ${await counterOf(page, id)}`)
      if (BREAK === 'live-metric') {
        if (changed === null) problems.push(`sim_counter moved from ${v1} within 3 s (the break expected it frozen)`)
      } else {
        if (changed === null) problems.push(`sim_counter did not rise above ${v1} in 20 s with no reload`)
        if (up1 === up2) problems.push(`the vitals text never changed: ${up1}`)
      }
      if (sentinel !== RUN) problems.push('the page was reloaded or navigated (the window sentinel is gone)')
      return { problems, text: `sim_counter ${v1} -> ${changed}\nvitals ${up1} -> ${up2}`, shot: rowOf(page, id) }
    },
  },
  {
    id: 'traps',
    async run() {
      const problems = []
      const encoded = encodeURIComponent(PASSWORD)
      const leaks = []
      for (const url of requested) {
        if (/ff[ea]_/.test(url)) leaks.push(`a URL with a token: ${url.replace(/ff[ea]_\w+/g, 'ffX_…')}`)
        if (url.includes(PASSWORD) || url.includes(encoded)) leaks.push('a URL carrying the admin password')
      }
      for (const board of Object.values(BOARDS)) {
        const log = await readFile(board.log, 'utf8').catch(() => '')
        if (/ff[ea]_[A-Za-z0-9_-]{6,}/.test(log)) leaks.push(`${board.log} contains a token plaintext`)
        if (log.includes(PASSWORD)) leaks.push(`${board.log} contains the admin password`)
      }
      // Broken: demand a leak, which a clean run cannot supply.
      if (BREAK === 'traps') {
        if (leaks.length === 0) problems.push('expected a leaked secret and found none')
      } else problems.push(...leaks)
      return { problems, text: `${requested.length} requests recorded.\n` }
    },
  },
]

// ── Driver ────────────────────────────────────────────────────────────────────────────

startBoard(BOARDS.fault)
startBoard(BOARDS.calm)

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1400, height: 1000 } })
const page = await context.newPage()
page.on('request', (request) => requested.push(request.url()))

try {
  await page.goto(BASE)
  try {
    await login(page)
  } catch {
    await stopBoards()
    setupFailed(`the dashboard at ${BASE} refused FF_ADMIN_PASSWORD (or did not answer)`)
  }

  const ready = await until(async () => {
    const [fault, calm] = [await device(page, BOARDS.fault.id), await device(page, BOARDS.calm.id)]
    const ok =
      fault?.online === true &&
      fault.health !== null && fault.health !== undefined &&
      (fault.metrics ?? []).some((m) => m.name === 'sim_counter') &&
      calm?.online === true &&
      calm.last_reset_reason === 'poweron'
    return ok ? true : null
  }, 60_000, 1000)
  if (ready === null) {
    await stopBoards()
    setupFailed(`the simulated boards never reported health (see ${BOARDS.fault.log})`)
  }
  await page.reload()
  for (const board of Object.values(BOARDS))
    await page.waitForSelector(`[data-testid=device-row][data-device-id="${board.id}"]`, { timeout: 15_000 })

  const results = {}
  let failed = 0
  for (const scenario of SCENARIOS) {
    const notes = []
    let outcome
    try {
      outcome = await scenario.run(page, notes)
    } catch (err) {
      outcome = { problems: [`harness: ${err instanceof Error ? err.message : String(err)}`], text: '(no text)' }
    }
    results[scenario.id] = outcome.problems
    await writeFile(`${OUT}/${scenario.id}.txt`, `${outcome.text}\n`)
    await (outcome.shot ?? rowOf(page, BOARDS.fault.id)).screenshot({ path: `${OUT}/${scenario.id}.png` }).catch(() => {})
    const note = notes.length > 0 ? ` [${notes.join('; ')}]` : ''
    if (outcome.problems.length === 0) console.log(`PASS ${scenario.id}: as expected${note}`)
    else {
      failed += 1
      console.log(`FAIL ${scenario.id}: ${outcome.problems.join(' | ')}${note}`)
    }
  }

  await writeFile(
    `${OUT}/run.json`,
    `${JSON.stringify(
      { run: RUN, base: BASE, devices: { fault: BOARDS.fault.id, calm: BOARDS.calm.id }, results },
      null,
      2,
    )}\n`,
  )
  console.log(`\n${SCENARIOS.length - failed}/${SCENARIOS.length} pass; wrote to ${OUT}`)
  process.exitCode = failed === 0 ? 0 : 1
} finally {
  await browser.close().catch(() => {})
  await stopBoards()
}
