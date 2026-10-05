// R2b-test-3: the update flow (`spec/flows.md` Flow 2) played end to end through the REAL
// dashboard in a real Chromium, against the dev stack, with SIMULATED boards, and graded
// mechanically. It is the harness for three CUJ-1 rows that had none: step 5 (the dashboard
// half), step 6 (the server + dashboard half) and "a wrong flash layout is refused, not
// flashed".
//
// What it plays, in one logged-in session:
//
//   upload-good            the form reads the build's own header (no shell step), uploads it
//   upload-merged          the merged full-flash image is refused at upload, nothing stored
//   precheck-wrong-layout  a build for another partition layout: refusal card, no Send, a
//                          direct POST /deploy is a 409 and sends nothing; the board whose
//                          layout it fits gets a deployable card (positive control)
//   deploy-good            Send -> confirmed -> the result card says "good"
//   deploy-broken          Send of a `-rbtest` build -> never confirms -> rolled back to the
//                          good build; the card says "rolled back", with no "Send again"
//   traps                  no token or password in any URL the browser requested or in a
//                          simulator log
//
// The boards are two simulator processes (`fleetforge.simulator fleet`): one `ab-4m-v1`
// board that takes BOTH builds (`--broken-marker=-rbtest`: an image whose version carries
// `-rbtest` never confirms, like an FF_ROLLBACK_TEST build; any other image confirms), and
// one `ab-4m-arduino-v1` board. The Arduino board is what lets the operator pick that
// layout in the form at all, and the positive control for the pre-check.
//
// What it does NOT cover: the real agent. The simulator reports versions it was told; the
// CUJ judge's "fw_version from the running image's own descriptor" and a real rollback are
// QEMU / bench territory (R2-test-1, R2b-test-2). Independent grading of the good deploy:
// `just bench-judge confirmed <good cmd_id> dev`.
//
// Not a test and not wired into `npm test`. It needs the dev stack (`just up`), `uv`, and
// the admin password. Playwright is resolved at runtime and is deliberately NOT a dependency
// of this package (see `onboarding-rehearsal.mjs`, `theme-shots.mjs`).
//
//   FF_ADMIN_PASSWORD=… \
//     PLAYWRIGHT_MODULE="$(npm root -g)/playwright/index.mjs" \
//     node scripts/update-flow-e2e.mjs [baseURL=http://localhost:8088] [outDir=/tmp/ff-r2b-test-3]
//
// or `just update-e2e`. The password falls back to the `FF_ADMIN_PASSWORD=` line of the
// repo-root `.env`; it is never printed and never put in an argv (the process list is
// readable by every user): the simulators get it through their environment.
//
// Every label and board name carries a per-run id: labels are permanent (a re-used label
// with other bytes is a 409), and a fresh board has no leftover deploy state. A run leaves
// two simulated boards (they go offline when the simulators exit) and four labels in the dev
// database; it deletes nothing.
//
// Exit: 0 all pass, 1 any scenario failed, 2 setup failed (stack down, login refused, the
// simulated boards never came online). `E2E_BREAK=<scenario id>` flips that scenario's
// primary expectation on purpose, to prove the harness can fail (the vacuity check).

import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createWriteStream, readFileSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? 'playwright')

const BASE = process.argv[2] ?? 'http://localhost:8088'
const OUT = process.argv[3] ?? '/tmp/ff-r2b-test-3'
const BREAK = process.env.E2E_BREAK ?? null
const ROOT = fileURLToPath(new URL('../../', import.meta.url))
const RUN = Date.now().toString(36)

const GOOD_DEADLINE_S = 60
const BROKEN_DEADLINE_S = 120
const CUJ_DEADLINE_S = 300

const LAYOUT = 'ab-4m-v1'
const ARDUINO_LAYOUT = 'ab-4m-arduino-v1'

const LABELS = {
  baseline: `1.0.0-ue${RUN}`,
  good: `1.1.0-ue${RUN}`,
  wrong: `1.1.1-ue${RUN}-arduino`,
  broken: `1.2.0-ue${RUN}-rbtest`,
  merged: `1.3.0-ue${RUN}-merged`,
}

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

// ── Build files ───────────────────────────────────────────────────────────────────────
// A real app-image head (magic 0xE9, chip id 9 = esp32s3, app descriptor with a version at
// 0x30). Each build's version field is overwritten with its label, so its bytes are DISTINCT
// (artifacts are keyed by sha256 and the upsert is `ON CONFLICT DO NOTHING`: the same bytes
// under a second label with another layout would silently keep the FIRST layout) and the
// form's header read fills the label in.

const FIXTURES = new URL('../../tests/fixtures/firmware/', import.meta.url)
const appHead = await readFile(new URL('esp32s3.app.head.bin', FIXTURES))
const mergedHead = await readFile(new URL('esp32s3.merged.head.bin', FIXTURES))

const VERSION_AT = 0x30
const VERSION_LEN = 32

function buildWith(label) {
  if (Buffer.byteLength(label) >= VERSION_LEN) throw new Error(`label ${label} does not fit the descriptor`)
  const bytes = Buffer.from(appHead)
  bytes.fill(0, VERSION_AT, VERSION_AT + VERSION_LEN)
  bytes.write(label, VERSION_AT, 'utf8')
  return bytes
}

await mkdir(`${OUT}/bins`, { recursive: true })
const FILES = {
  good: `${OUT}/bins/good.bin`,
  wrong: `${OUT}/bins/wrong-layout.bin`,
  broken: `${OUT}/bins/broken.bin`,
  merged: `${OUT}/bins/merged.bin`,
}
await writeFile(FILES.good, buildWith(LABELS.good))
await writeFile(FILES.wrong, buildWith(LABELS.wrong))
await writeFile(FILES.broken, buildWith(LABELS.broken))
await writeFile(FILES.merged, mergedHead)

// ── Simulated boards ──────────────────────────────────────────────────────────────────

// `fleetforge.simulator.device.derive_device_id`: the same bytes in JS.
function deviceIdOf(name) {
  const d = createHash('sha256').update(name, 'utf8').digest()
  return Buffer.from([(d[0] & 0xfe) | 0x02, ...d.subarray(1, 6)]).toString('hex')
}

const BOARDS = {
  main: { prefix: `ue${RUN}`, layout: LAYOUT, marker: true },
  arduino: { prefix: `ue${RUN}a`, layout: ARDUINO_LAYOUT, marker: false },
}
for (const board of Object.values(BOARDS)) {
  board.name = `${board.prefix}-01`
  board.id = deviceIdOf(board.name)
  board.log = `${OUT}/sim-${board === BOARDS.main ? 'main' : 'arduino'}.log`
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
    '--fw-version', LABELS.baseline,
    '--capabilities', 'ota',
    '--partition-layout', board.layout,
    '--heartbeat-interval', '5',
    '--confirm-timeout', '20',
    '--duration', '900', // the safety net: an orphaned simulator exits on its own
    '--api-base', BASE,
  ]
  // `=` is required: argparse reads a bare `-rbtest` as an option.
  if (board.marker) args.push('--broken-marker=-rbtest')
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
function api(page, method, path, body) {
  return page.evaluate(
    async ({ method, path, body }) => {
      const response = await fetch(path, {
        method,
        credentials: 'same-origin',
        headers: body === undefined ? undefined : { 'content-type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      })
      const text = await response.text()
      let json = null
      try {
        json = JSON.parse(text)
      } catch {
        // not JSON
      }
      return { status: response.status, json, text }
    },
    { method, path, body },
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
const cellOf = (page, id) => rowOf(page, id).locator('[data-testid=deploy-cell]')
const uploadForm = (page) => page.locator('section[aria-labelledby=upload-heading]')

const hasOption = async (page, id, label) =>
  (await cellOf(page, id).locator(`select option[value="${label}"]`).count()) > 0

async function waitForOption(page, id, label, timeoutMs = 25_000) {
  return (await until(() => hasOption(page, id, label), timeoutMs, 500)) === true
}

// Play the upload form the way an operator does: pick the file, let the header read fill the
// form, choose the target and layout explicitly (the default layout depends on what the whole
// dev fleet reports), press Upload.
async function uploadBuild(page, { file, layout, typeVersion = null }) {
  const form = uploadForm(page)
  await form.locator('input[type=file]').setInputFiles(file)
  const info = form.locator('[data-testid=upload-file-info]')
  await info.waitFor({ timeout: 10_000 })
  await page.waitForFunction(
    () =>
      /built as|no version to read|Not recognised/.test(
        document.querySelector('[data-testid=upload-file-info]')?.innerText ?? '',
      ),
    null,
    { timeout: 10_000 },
  )
  const infoText = await info.innerText()
  const prefilled = await form.getByLabel('Version', { exact: true }).inputValue()
  if (typeVersion !== null) await form.getByLabel('Version', { exact: true }).fill(typeVersion)
  await form.getByLabel('Chip target', { exact: true }).selectOption('esp32s3')
  await form.getByLabel('Partition layout', { exact: true }).selectOption(layout)
  await form.getByRole('button', { name: 'Upload', exact: true }).click()
  const outcome = form.locator('[data-testid=upload-result], [data-testid=upload-error]').first()
  await outcome.waitFor({ timeout: 30_000 })
  const kind = (await outcome.getAttribute('data-testid')) === 'upload-result' ? 'result' : 'error'
  return { kind, text: (await outcome.innerText()).trim(), infoText, prefilled }
}

async function openPrecheck(page, id, label) {
  const cell = cellOf(page, id)
  await cell.locator('select').selectOption(label)
  await cell.getByRole('button', { name: 'Deploy', exact: true }).click()
  const card = cell.locator('[data-testid=precheck-card]')
  await card.waitFor({ timeout: 20_000 })
  return card
}

// Send from an open pre-check card and return the transaction's cmd_id from the 202.
async function send(page, id, card) {
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) => r.url().endsWith(`/v1/devices/${id}/deploy`) && r.request().method() === 'POST',
      { timeout: 20_000 },
    ),
    card.getByRole('button', { name: /^Send/ }).click(),
  ])
  const body = await response.json().catch(() => ({}))
  return { status: response.status(), cmdId: body.cmd_id ?? null, at: Date.now() }
}

// Wait until THIS transaction is terminal on the API, then until the card for it (its text
// names the transaction's version) is on the row. While waiting, sample the row once a
// second for a "good" card naming `watchLabel`: a verdict of good for a build that is not
// what the board runs is the CUJ-1 "milestone shown as reached while no longer true" trap.
async function awaitDeploy(page, id, { cmdId, label, deadlineS, watchLabel = null }) {
  const started = Date.now()
  const samples = []
  let dev = null
  const terminal = await until(
    async () => {
      dev = await device(page, id)
      if (watchLabel !== null) {
        const good = rowOf(page, id).locator('[data-testid=deploy-result][data-outcome="good"]')
        if ((await good.count()) > 0 && (await good.first().innerText()).includes(watchLabel)) {
          samples.push(`a "good" card named ${watchLabel} at +${Math.round((Date.now() - started) / 1000)}s`)
        }
      }
      return dev?.deploy?.cmd_id === cmdId && dev.deploy.is_terminal === true ? dev : null
    },
    deadlineS * 1000,
    1000,
  )
  if (terminal === null) return { dev, card: null, samples, elapsedS: null, timedOut: true }
  const cardLocator = rowOf(page, id).locator('[data-testid=deploy-result]')
  const card = await until(
    async () => {
      if ((await cardLocator.count()) === 0) return null
      const text = await cardLocator.first().innerText()
      return text.includes(label) ? { outcome: await cardLocator.first().getAttribute('data-outcome'), text } : null
    },
    20_000,
    500,
  )
  return { dev, card, samples, elapsedS: (Date.now() - started) / 1000, timedOut: false }
}

const stepStates = (dev) => (dev?.deploy?.steps ?? []).map((s) => s.state)

async function rowText(page, id) {
  return (await cellOf(page, id).innerText().catch(() => '(no cell)')) ?? ''
}

// ── Scenarios ─────────────────────────────────────────────────────────────────────────
// Each returns `{ problems: string[], text: string }`; empty problems = pass.

const state = { uploaded: {}, cmdIds: { good: null, broken: null }, elapsedS: { good: null, broken: null } }
const skipped = (...needs) => {
  const missing = needs.filter((n) => state.uploaded[n] !== true)
  return missing.length > 0 ? `skipped: depends on upload of the ${missing.join(' and ')} build` : null
}

const SCENARIOS = [
  {
    id: 'upload-good',
    shotOf: (page) => uploadForm(page),
    async run(page, notes) {
      const problems = []
      const r = await uploadBuild(page, { file: FILES.good, layout: LAYOUT })
      if (!/built as fleetforge-agent 1\.1\.0-ue\w* for esp32s3/.test(r.infoText))
        problems.push(`header line is "${r.infoText}", wanted "built as fleetforge-agent 1.1.0-ue… for esp32s3"`)
      if (r.prefilled !== LABELS.good)
        problems.push(`the Version field held "${r.prefilled}" after the pick, wanted ${LABELS.good} read from the header`)
      const wantKind = BREAK === 'upload-good' ? 'error' : 'result'
      if (r.kind !== wantKind) problems.push(`got upload-${r.kind}: ${r.text}`)
      else if (r.kind === 'result') {
        state.uploaded.good = true
        if (!r.text.startsWith(`Uploaded esp32s3 ${LABELS.good}`))
          problems.push(`result is "${r.text}", wanted it to start "Uploaded esp32s3 ${LABELS.good}"`)
        if (!(await waitForOption(page, BOARDS.main.id, LABELS.good)))
          problems.push(`${LABELS.good} never appeared in the board's Deploy select`)
        notes.push(`header read pre-filled ${r.prefilled}`)
      }
      return { problems, text: `${r.infoText}\n${r.text}\n\n--- row ---\n${await rowText(page, BOARDS.main.id)}` }
    },
  },
  {
    id: 'upload-merged',
    shotOf: (page) => uploadForm(page),
    async run(page) {
      const problems = []
      const r = await uploadBuild(page, { file: FILES.merged, layout: LAYOUT, typeVersion: LABELS.merged })
      if (!/no version to read/.test(r.infoText)) problems.push(`header line is "${r.infoText}", wanted "no version to read"`)
      const wantKind = BREAK === 'upload-merged' ? 'result' : 'error'
      if (r.kind !== wantKind) problems.push(`got upload-${r.kind}: ${r.text}`)
      if (r.kind === 'error') {
        if (!/merged full-flash image/.test(r.text)) problems.push(`error lacks "merged full-flash image": ${r.text}`)
        if (!/app \.bin/.test(r.text)) problems.push(`error lacks "app .bin": ${r.text}`)
      }
      const list = await api(page, 'GET', '/v1/artifact')
      if (list.json?.artifacts?.some((a) => a.version === LABELS.merged))
        problems.push(`GET /v1/artifact lists ${LABELS.merged}`)
      if (await hasOption(page, BOARDS.main.id, LABELS.merged))
        problems.push(`the Deploy select offers ${LABELS.merged}`)
      return { problems, text: `${r.infoText}\n${r.text}` }
    },
  },
  {
    id: 'precheck-wrong-layout',
    shotOf: (page) => cellOf(page, BOARDS.arduino.id),
    async run(page, notes) {
      const problems = []
      const up = await uploadBuild(page, { file: FILES.wrong, layout: ARDUINO_LAYOUT })
      if (up.kind !== 'result') return { problems: [`the wrong-layout build was not uploaded: ${up.text}`], text: up.text }
      state.uploaded.wrong = true
      const main = BOARDS.main
      if (!(await waitForOption(page, main.id, LABELS.wrong))) problems.push(`${LABELS.wrong} not in the main board's select`)
      if (!(await waitForOption(page, BOARDS.arduino.id, LABELS.wrong)))
        problems.push(`${LABELS.wrong} not in the Arduino board's select`)
      if (problems.length > 0) return { problems, text: '' }

      const before = (await device(page, main.id))?.deploy?.cmd_id ?? null
      const card = await openPrecheck(page, main.id, LABELS.wrong)
      const wantDeployable = BREAK === 'precheck-wrong-layout' ? 'true' : 'false'
      const got = await card.getAttribute('data-deployable')
      if (got !== wantDeployable) problems.push(`data-deployable is ${got}, wanted ${wantDeployable}`)
      const refusals = card.locator('[data-testid=precheck-refusals]')
      const cardText = await card.innerText()
      if ((await refusals.count()) === 0) problems.push('the card has no refusals list')
      else {
        const said = await refusals.innerText()
        for (const needle of ['Refused:', LAYOUT, ARDUINO_LAYOUT])
          if (!said.includes(needle)) problems.push(`refusals lack "${needle}": ${said.replace(/\s+/g, ' ')}`)
      }
      const sendButtons = await card.getByRole('button', { name: /^Send/ }).count()
      if (sendButtons !== 0) problems.push(`the refusal card has ${sendButtons} Send button(s)`)
      if (!/Refusals cannot be overridden\./.test(cardText)) problems.push('"Refusals cannot be overridden." missing')
      await card.getByRole('button', { name: 'Cancel' }).click()

      // Defence in depth, outside the UI: the server refuses the same deploy.
      const direct = await api(page, 'POST', `/v1/devices/${main.id}/deploy`, { version: LABELS.wrong, apply: 'auto' })
      if (direct.status !== 409) problems.push(`direct POST /deploy answered ${direct.status}, wanted 409: ${direct.text.slice(0, 200)}`)
      const after = (await device(page, main.id))?.deploy?.cmd_id ?? null
      if (after !== before) problems.push(`deploy.cmd_id changed ${before} -> ${after}: something was sent`)
      notes.push(`direct POST /deploy -> ${direct.status}`)

      // Positive control: the board this layout fits is offered the same build.
      const arduinoCard = await openPrecheck(page, BOARDS.arduino.id, LABELS.wrong)
      if ((await arduinoCard.getAttribute('data-deployable')) !== 'true')
        problems.push(`the Arduino board's pre-check is not deployable: ${(await arduinoCard.innerText()).replace(/\s+/g, ' ')}`)
      if ((await arduinoCard.locator('[data-testid=precheck-refusals]').count()) !== 0)
        problems.push("the Arduino board's pre-check lists refusals")
      const arduinoText = await arduinoCard.innerText()
      await arduinoCard.getByRole('button', { name: 'Cancel' }).click() // never sent

      return { problems, text: `--- main board ---\n${cardText}\n\n--- Arduino board ---\n${arduinoText}` }
    },
  },
  {
    id: 'deploy-good',
    shotOf: (page) => rowOf(page, BOARDS.main.id),
    async run(page, notes) {
      const dependsOn = skipped('good')
      if (dependsOn) return { problems: [dependsOn], text: '' }
      const problems = []
      const main = BOARDS.main
      const card = await openPrecheck(page, main.id, LABELS.good)
      if ((await card.getAttribute('data-deployable')) !== 'true')
        problems.push(`pre-check not deployable: ${(await card.innerText()).replace(/\s+/g, ' ')}`)
      if ((await card.locator('[data-testid=precheck-rollback]').count()) === 0) problems.push('no rollback promise on the card')
      else if (!/rolls back on its own/.test(await card.locator('[data-testid=precheck-rollback]').innerText()))
        problems.push('rollback line does not say "rolls back on its own"')
      const sent = await send(page, main.id, card)
      if (sent.status !== 202 || sent.cmdId === null) {
        problems.push(`Send answered ${sent.status} with cmd_id ${sent.cmdId}`)
        return { problems, text: await rowText(page, main.id) }
      }
      state.cmdIds.good = sent.cmdId
      const r = await awaitDeploy(page, main.id, { cmdId: sent.cmdId, label: LABELS.good, deadlineS: GOOD_DEADLINE_S })
      if (r.timedOut) return { problems: [`not terminal after ${GOOD_DEADLINE_S}s: ${JSON.stringify(r.dev?.deploy?.state)}`], text: await rowText(page, main.id) }
      const elapsed = (Date.now() - sent.at) / 1000
      state.elapsedS.good = Math.round(elapsed * 10) / 10
      notes.push(`Send to card ${state.elapsedS.good}s`)
      if (elapsed > CUJ_DEADLINE_S) problems.push(`took ${elapsed}s, more than ${CUJ_DEADLINE_S}s`)

      const wantOutcome = BREAK === 'deploy-good' ? 'rolled-back' : 'good'
      if (r.card === null) problems.push('no result card naming the good build')
      else {
        if (r.card.outcome !== wantOutcome) problems.push(`card data-outcome is ${r.card.outcome}, wanted ${wantOutcome}`)
        for (const needle of [LABELS.baseline, LABELS.good])
          if (!r.card.text.includes(needle)) problems.push(`card lacks ${needle}`)
        if (/UI and API differ/.test(r.card.text)) problems.push('card says UI and API differ')
      }
      const d = r.dev
      if (d.fw_version !== LABELS.good) problems.push(`API fw_version is ${d.fw_version}, wanted ${LABELS.good}`)
      if (d.deploy.state !== 'confirmed') problems.push(`API state is ${d.deploy.state}, wanted confirmed`)
      if (d.deploy.is_terminal !== true) problems.push('API is_terminal is not true')
      if (d.deploy.from_version !== LABELS.baseline) problems.push(`from_version is ${d.deploy.from_version}, wanted ${LABELS.baseline}`)
      if (d.deploy.artifact_version !== LABELS.good) problems.push(`artifact_version is ${d.deploy.artifact_version}, wanted ${LABELS.good}`)
      const steps = stepStates(d)
      if (!(steps.includes('confirming') && steps.indexOf('confirming') < steps.indexOf('confirmed')))
        problems.push(`steps lack confirming before confirmed: ${steps.join(', ')}`)
      return { problems, text: `${r.card?.text ?? '(no card)'}\n\nsteps: ${steps.join(' > ')}` }
    },
  },
  {
    id: 'deploy-broken',
    shotOf: (page) => rowOf(page, BOARDS.main.id),
    async run(page, notes) {
      const dependsOn = state.cmdIds.good === null ? 'skipped: depends on deploy-good' : null
      if (dependsOn) return { problems: [dependsOn], text: '' }
      const problems = []
      const main = BOARDS.main
      const up = await uploadBuild(page, { file: FILES.broken, layout: LAYOUT })
      if (up.kind !== 'result') return { problems: [`the broken build was not uploaded: ${up.text}`], text: up.text }
      if (up.prefilled !== LABELS.broken) problems.push(`header read gave "${up.prefilled}", wanted ${LABELS.broken}`)
      if (!(await waitForOption(page, main.id, LABELS.broken))) return { problems: [`${LABELS.broken} not in the select`], text: '' }

      const card = await openPrecheck(page, main.id, LABELS.broken)
      if ((await card.getAttribute('data-deployable')) !== 'true')
        return { problems: [`pre-check not deployable: ${(await card.innerText()).replace(/\s+/g, ' ')}`], text: '' }
      const sent = await send(page, main.id, card)
      if (sent.status !== 202 || sent.cmdId === null) return { problems: [`Send answered ${sent.status}`], text: '' }
      state.cmdIds.broken = sent.cmdId
      const r = await awaitDeploy(page, main.id, {
        cmdId: sent.cmdId, label: LABELS.broken, deadlineS: BROKEN_DEADLINE_S, watchLabel: LABELS.broken,
      })
      if (r.timedOut) return { problems: [`not terminal after ${BROKEN_DEADLINE_S}s: ${JSON.stringify(r.dev?.deploy?.state)}`], text: await rowText(page, main.id) }
      state.elapsedS.broken = Math.round(((Date.now() - sent.at) / 1000) * 10) / 10
      notes.push(`Send to card ${state.elapsedS.broken}s`)
      for (const s of r.samples) problems.push(`milestone shown as reached while no longer true: ${s}`)

      const wantOutcome = BREAK === 'deploy-broken' ? 'good' : 'rolled-back'
      if (r.card === null) problems.push('no result card naming the broken build')
      else {
        if (r.card.outcome !== wantOutcome) problems.push(`card data-outcome is ${r.card.outcome}, wanted ${wantOutcome}`)
        const rows = await rowOf(page, main.id).locator('[data-testid=deploy-result-rows]').innerText()
        if (!rows.includes(`${LABELS.good} again`)) problems.push(`the After row does not name ${LABELS.good} again: ${rows.replace(/\s+/g, ' ')}`)
        if ((await rowOf(page, main.id).locator('[data-testid=deploy-result-next]').count()) !== 1)
          problems.push('the card has no single "Next" line')
        if ((await rowOf(page, main.id).locator('[data-testid=deploy-send-again]').count()) !== 0)
          problems.push('the card offers "Send again" after a rollback')
        if (/UI and API differ/.test(r.card.text)) problems.push('card says UI and API differ')
      }
      const d = r.dev
      if (d.fw_version !== LABELS.good) problems.push(`API fw_version is ${d.fw_version}, wanted ${LABELS.good} (the pre-deploy version)`)
      if (d.deploy.state !== 'rolled_back') problems.push(`API state is ${d.deploy.state}, wanted rolled_back`)
      if (d.deploy.is_terminal !== true) problems.push('API is_terminal is not true')
      if (d.deploy.artifact_version !== LABELS.broken) problems.push(`artifact_version is ${d.deploy.artifact_version}, wanted ${LABELS.broken}`)
      if (d.deploy.from_version !== LABELS.good) problems.push(`from_version is ${d.deploy.from_version}, wanted ${LABELS.good}`)
      const steps = stepStates(d)
      if (!(steps.includes('confirming') && steps.indexOf('confirming') < steps.indexOf('rolled_back')))
        problems.push(`steps lack confirming before rolled_back: ${steps.join(', ')}`)
      if (steps.includes('confirmed')) problems.push(`steps contain confirmed: ${steps.join(', ')}`)
      if (!steps.includes('rolling_back')) notes.push('rolling_back not recorded (best effort on the board)')
      return { problems, text: `${r.card?.text ?? '(no card)'}\n\nsteps: ${steps.join(' > ')}` }
    },
  },
  {
    id: 'traps',
    async run(page) {
      const problems = []
      const encoded = encodeURIComponent(PASSWORD)
      for (const url of requested) {
        if (/ff[ea]_/.test(url)) problems.push(`the browser requested a URL with a token: ${url.replace(/ff[ea]_\w+/g, 'ffX_…')}`)
        if (url.includes(PASSWORD) || url.includes(encoded)) problems.push('the browser requested a URL carrying the admin password')
      }
      for (const board of Object.values(BOARDS)) {
        const log = await readFile(board.log, 'utf8').catch(() => '')
        if (/ff[ea]_[A-Za-z0-9_-]{6,}/.test(log)) problems.push(`${board.log} contains a token plaintext`)
        if (log.includes(PASSWORD)) problems.push(`${board.log} contains the admin password`)
      }
      return { problems, text: `${requested.length} requests recorded, none carried a token or the password (unless listed).\n` }
    },
  },
]

// ── Driver ────────────────────────────────────────────────────────────────────────────

const requested = []
let page

// The dev fleet can hold hundreds of rows, so a full-page shot is 30000 px tall and unreadable:
// shoot the element the scenario is about (its `shotOf`), else the viewport.
async function shot(pageArg, name, target = null) {
  const options = { path: `${OUT}/${name}.png` }
  await (target ?? pageArg).screenshot(options).catch(() => {})
}

startBoard(BOARDS.main)
startBoard(BOARDS.arduino)

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1280, height: 1400 } })
page = await context.newPage()
page.on('request', (request) => requested.push(request.url()))

try {
  await page.goto(BASE)
  try {
    await login(page)
  } catch {
    await stopBoards()
    setupFailed(`the dashboard at ${BASE} refused FF_ADMIN_PASSWORD (or did not answer); the dev .env password and the api's hash have drifted before`)
  }

  // Both boards online on the baseline, with the layout they were told to report.
  const ready = await until(async () => {
    const [main, arduino] = [await device(page, BOARDS.main.id), await device(page, BOARDS.arduino.id)]
    const ok = (d, layout) => d !== null && d.online === true && d.fw_version === LABELS.baseline && d.partition_layout === layout
    return ok(main, LAYOUT) && ok(arduino, ARDUINO_LAYOUT) ? true : null
  }, 60_000, 1000)
  if (ready === null) {
    await stopBoards()
    setupFailed(`the simulated boards never came online (see ${BOARDS.main.log})`)
  }
  for (const board of Object.values(BOARDS)) {
    const log = await readFile(board.log, 'utf8').catch(() => '')
    if (!log.includes(`board    ${board.name} -> ${board.id}`))
      console.error(`note: ${board.log} does not name ${board.name} -> ${board.id}; the id derivation may have drifted`)
  }
  await page.reload()
  await page.waitForSelector(`[data-testid=device-row][data-device-id="${BOARDS.main.id}"]`, { timeout: 15_000 })

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
    await shot(page, scenario.id, scenario.shotOf?.(page) ?? null)
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
      {
        run: RUN,
        base: BASE,
        devices: { main: BOARDS.main.id, arduino: BOARDS.arduino.id },
        labels: LABELS,
        cmd_ids: state.cmdIds,
        elapsed_s: state.elapsedS,
        results,
      },
      null,
      2,
    )}\n`,
  )
  console.log(`\n${SCENARIOS.length - failed}/${SCENARIOS.length} pass; wrote to ${OUT}`)
  console.log(`good cmd_id ${state.cmdIds.good ?? '-'}`)
  console.log(`broken cmd_id ${state.cmdIds.broken ?? '-'}`)
  process.exitCode = failed === 0 ? 0 : 1
} finally {
  await browser.close().catch(() => {})
  await stopBoards()
}
