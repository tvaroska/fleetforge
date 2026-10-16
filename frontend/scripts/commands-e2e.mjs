// R5-fe-1: the self-test result, the rollback/apply wait wording and the Apply / Cancel
// update / Reboot controls, played through the REAL dashboard in a real Chromium, against
// the dev stack, with SIMULATED boards, and graded mechanically.
//
// What it plays, in one logged-in session:
//
//   selftest-passed     V: Deploy a good build -> the result card says "good", its reason
//                       says "self-test passed", a Self-test row reads "passed", and the
//                       API's deploy.selftest.outcome is "passed"
//   selftest-failed     V: Deploy a `-stfail` build -> "rolled-back" with "simulated
//                       self-test failure"; the Board says row equals the API detail; the
//                       Self-test row names the reason and "returned to ota_…"; no Send again
//   apply               V: Deploy with "Hold at staged" ticked -> the POST body carries
//                       apply "on_command" -> staged -> Apply -> the last-command line says
//                       "accepted" -> the card says confirmed ("good"), with no reload
//   cancel              V: a held deploy -> Cancel update -> "accepted" -> a "cancelled" card,
//                       API is_terminal true, fw unchanged, no Apply any more, the timeline's
//                       end item data-state="ended"
//   apply-waits         H (--safe-window hold): held deploy -> Apply -> "deferred" -> the state
//                       says "waiting for a safe moment", the board's "apply waits for a safe
//                       window" is shown, Cancel update is still offered; 20 s of samples show
//                       no stuck/timed out/spinner and no state change; then Cancel -> cancelled
//   auto-hold           H: a plain deploy parks at awaiting_safe_window with no detail -> the
//                       apply wording (inferred), the timeline's current milestone "staged";
//                       Cancel update -> cancelled
//   reboot              V: Reboot -> the confirm step, nothing POSTed; Keep running -> still
//                       nothing; Reboot -> Reboot now -> 202 -> "accepted" -> the board
//                       restarts (sim log), announces reset_reason sw, the Health cell says sw
//   refused-and-legacy  H with a held stage: Reboot now -> "refused: an update is in
//                       progress" (class bad); L (announces only `ota`): no command buttons,
//                       no hold box in its pre-check card, and a direct POST /reboot is a 409
//                       naming `ota`
//   traps               no token or password in any requested URL or simulator log; no
//                       visible null/undefined/NaN in the three boards' deploy cells
//
// The boards are simulator processes (`fleetforge.simulator fleet`): V announces ota apply
// cancel rollback reboot selftest and fails the self-test of any `-stfail` image; H is the
// same with `--safe-window hold`; L is a legacy board announcing only `ota`.
//
// What it does NOT cover: the real agent (QEMU territory). The simulator reports what it was
// told. Not a test and not wired into `npm test`. It needs the dev stack (`just up`), `uv`,
// and the admin password. Playwright is resolved at runtime and is deliberately NOT a
// dependency of this package (see `update-flow-e2e.mjs`).
//
//   FF_ADMIN_PASSWORD=… \
//     PLAYWRIGHT_MODULE="$(npm root -g)/playwright/index.mjs" \
//     node scripts/commands-e2e.mjs [baseURL=http://localhost:8088] [outDir=/tmp/ff-r5-fe-1]
//
// or `just commands-e2e`. The password falls back to the `FF_ADMIN_PASSWORD=` line of the
// repo-root `.env`; it is never printed and never put in an argv: the simulators get it
// through their environment.
//
// Every run uses a fresh prefix and fresh labels, so it enrolls three boards and uploads seven
// labels and leaves them in the dev database; it deletes nothing.
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
const OUT = process.argv[3] ?? '/tmp/ff-r5-fe-1'
const BREAK = process.env.E2E_BREAK ?? null
const ROOT = fileURLToPath(new URL('../../', import.meta.url))
const RUN = Date.now().toString(36)
const LAYOUT = 'ab-4m-v1'

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

// ── Labels and builds ─────────────────────────────────────────────────────────────────
// Each label < 32 bytes (the app descriptor's version field). The same build shape as
// `update-flow-e2e.mjs`: a real esp32s3 app head with the label at 0x30, plus one valid
// library marker so the pre-check does not gate on `no_library_marker`.

const LABELS = {
  baseline: `1.0.0-cx${RUN}`,
  good: `1.1.0-cx${RUN}`,
  stfail: `1.2.0-cx${RUN}-stfail`,
  apply: `1.3.0-cx${RUN}`,
  cancel: `1.4.0-cx${RUN}`,
  hwait: `1.5.0-cx${RUN}`,
  hauto: `1.6.0-cx${RUN}`,
  hrefuse: `1.7.0-cx${RUN}`,
}

const FIXTURES = new URL('../../tests/fixtures/firmware/', import.meta.url)
const appHead = await readFile(new URL('esp32s3.app.head.bin', FIXTURES))
const VERSION_AT = 0x30
const VERSION_LEN = 32
const LIB_MARKER = Buffer.alloc(64)
Buffer.from('14a948d18f12cfdd46464f54414c4942', 'hex').copy(LIB_MARKER, 0)
LIB_MARKER[16] = 1
LIB_MARKER.write('0.0.0-e2e', 20, 'ascii')

function buildWith(label) {
  if (Buffer.byteLength(label) >= VERSION_LEN) throw new Error(`label ${label} does not fit the descriptor`)
  const bytes = Buffer.from(appHead)
  bytes.fill(0, VERSION_AT, VERSION_AT + VERSION_LEN)
  bytes.write(label, VERSION_AT, 'utf8')
  return Buffer.concat([bytes, LIB_MARKER])
}

// ── Simulated boards ──────────────────────────────────────────────────────────────────

// `fleetforge.simulator.device.derive_device_id`: the same bytes in JS.
function deviceIdOf(name) {
  const d = createHash('sha256').update(name, 'utf8').digest()
  return Buffer.from([(d[0] & 0xfe) | 0x02, ...d.subarray(1, 6)]).toString('hex')
}

const R5_CAPS = ['--capabilities', 'ota', 'apply', 'cancel', 'rollback', 'reboot', 'selftest']
const BOARDS = {
  V: { prefix: `cx${RUN}v`, extra: [...R5_CAPS, '--selftest-fail-marker=-stfail'] },
  H: { prefix: `cx${RUN}h`, extra: [...R5_CAPS, '--selftest-fail-marker=-stfail', '--safe-window', 'hold'] },
  L: { prefix: `cx${RUN}l`, extra: ['--capabilities', 'ota'] },
}
for (const [key, board] of Object.entries(BOARDS)) {
  board.key = key
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
    '--partition-layout', LAYOUT,
    '--fw-version', LABELS.baseline,
    '--heartbeat-interval', '5',
    '--confirm-timeout', '20',
    '--duration', '900', // the safety net: an orphaned simulator exits on its own
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

// Upload one build from inside the page (the upload form is not under test here).
function upload(page, label) {
  const query = new URLSearchParams({ target: 'esp32s3', version: label, partition_layout: LAYOUT })
  return page.evaluate(
    async ({ path, b64 }) => {
      const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
      const response = await fetch(path, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'content-type': 'application/octet-stream' },
        body: bytes,
      })
      return { status: response.status, text: await response.text() }
    },
    { path: `/v1/artifact?${query}`, b64: buildWith(label).toString('base64') },
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
const flat = (s) => s.replace(/\s+/g, ' ').trim()
const cellText = async (page, id) => flat(await cellOf(page, id).innerText().catch(() => ''))

async function openPrecheck(page, id, label) {
  const cell = cellOf(page, id)
  await cell.locator('select').selectOption(label)
  await cell.getByRole('button', { name: 'Deploy', exact: true }).click()
  const card = cell.locator('[data-testid=precheck-card]')
  await card.waitFor({ timeout: 20_000 })
  return card
}

// Deploy through the dashboard: select, Deploy, (tick hold), Send. Returns the cmd_id and the
// `apply` the browser actually put in the POST body.
async function deployVia(page, board, label, { hold = false } = {}) {
  const card = await openPrecheck(page, board.id, label)
  // Any gating warning (none expected) is ticked so the scenario tests what it is about.
  for (const box of await card.locator('input[aria-label^="Send anyway:"]').all()) await box.check()
  if (hold) await card.getByLabel('Hold at staged: install only when I click Apply').check()
  const [request, response] = await Promise.all([
    page.waitForRequest(
      (r) => r.url().endsWith(`/v1/devices/${board.id}/deploy`) && r.method() === 'POST',
      { timeout: 20_000 },
    ),
    page.waitForResponse(
      (r) => r.url().endsWith(`/v1/devices/${board.id}/deploy`) && r.request().method() === 'POST',
      { timeout: 20_000 },
    ),
    card.getByRole('button', { name: /^Send/ }).click(),
  ])
  const body = await response.json().catch(() => ({}))
  const sentApply = request.postDataJSON()?.apply ?? null
  return { status: response.status(), cmdId: body.cmd_id ?? null, sentApply }
}

// Wait until the API shows THIS transaction satisfying `pred`.
async function waitDeploy(page, id, cmdId, pred, timeoutMs) {
  let dev = null
  const hit = await until(async () => {
    dev = await device(page, id)
    return dev?.deploy?.cmd_id === cmdId && pred(dev.deploy) ? dev : null
  }, timeoutMs, 500)
  return { dev: hit ?? dev, ok: hit !== null }
}

// Click a command button and return the command_id from its 202 (null on any other answer).
async function clickVerb(page, id, button) {
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        new URL(r.url()).pathname.startsWith(`/v1/devices/${id}/`) &&
        /\/(apply|cancel|reboot)$/.test(r.url()),
      { timeout: 10_000 },
    ),
    button.click(),
  ])
  const body = await response.json().catch(() => null)
  return response.status() === 202 ? body?.command_id ?? null : null
}

// Wait until THIS command (by command_id) has `result` on the API, and the last-command line
// shows `verb` with that result. A line left over from an earlier verb never matches.
async function waitLastCommand(page, id, commandId, verb, result, timeoutMs = 10_000) {
  if (commandId === null) return null
  const loc = cellOf(page, id).locator('[data-testid=last-command]')
  return until(async () => {
    const d = await device(page, id)
    if (d?.last_command?.command_id !== commandId || d.last_command.result !== result) return null
    if ((await loc.count()) === 0) return null
    const text = flat(await loc.innerText())
    const r = await loc.getAttribute('data-result')
    return r === result && text.startsWith(`${verb} ·`) ? `${text} [command ${commandId}]` : null
  }, timeoutMs, 300)
}

// The card for the current transaction, once its text names `label`.
async function waitCard(page, id, label, timeoutMs = 20_000) {
  const loc = cellOf(page, id).locator('[data-testid=deploy-result]')
  return until(async () => {
    if ((await loc.count()) === 0) return null
    const text = flat(await loc.innerText())
    return text.includes(label) ? { outcome: await loc.getAttribute('data-outcome'), text } : null
  }, timeoutMs, 500)
}

// A `<dt>label</dt><dd>value</dd>` row of the result card, or null.
async function cardRow(page, id, label) {
  return cellOf(page, id)
    .locator('[data-testid=deploy-result-rows]')
    .evaluate((dl, label) => {
      const dts = [...dl.querySelectorAll('dt')]
      const dt = dts.find((d) => d.textContent === label)
      return dt ? dt.nextElementSibling?.textContent ?? null : null
    }, label)
    .catch(() => null)
}

const state = { cmdIds: {}, commandIds: {} }
const requested = []
const rebootPosts = []

// Remember each verb's command_id from its 202.
function recordCommands(page) {
  page.on('response', async (r) => {
    const m = /\/v1\/devices\/[^/]+\/(?:deploys\/[^/]+\/)?(apply|cancel|reboot)$/.exec(new URL(r.url()).pathname)
    if (m === null || r.request().method() !== 'POST') return
    const body = await r.json().catch(() => null)
    if (body?.command_id) (state.commandIds[m[1]] ??= []).push(body.command_id)
  })
}

// ── Scenarios ─────────────────────────────────────────────────────────────────────────
// Each returns `{ problems: string[], text: string }`; empty problems = pass.

const SCENARIOS = [
  {
    id: 'selftest-passed',
    shotOf: (page) => cellOf(page, BOARDS.V.id),
    async run(page) {
      const problems = []
      const V = BOARDS.V
      const sent = await deployVia(page, V, LABELS.good)
      state.cmdIds.good = sent.cmdId
      if (sent.status !== 202) return { problems: [`deploy answered ${sent.status}`], text: '' }
      const { dev, ok } = await waitDeploy(page, V.id, sent.cmdId, (d) => d.is_terminal, 90_000)
      if (!ok) return { problems: [`never terminal: ${JSON.stringify(dev?.deploy)}`], text: '' }
      const card = await waitCard(page, V.id, LABELS.good)
      const selftestRow = await cardRow(page, V.id, 'Self-test')
      const wanted = BREAK === 'selftest-passed' ? 'rolled-back' : 'good'
      if (card?.outcome !== wanted) problems.push(`card outcome ${card?.outcome}, wanted ${wanted}`)
      if (!card?.text.includes('self-test passed')) problems.push(`card text lacks "self-test passed": ${card?.text}`)
      if (selftestRow !== 'passed') problems.push(`Self-test row is ${selftestRow}`)
      if (dev.deploy.selftest?.outcome !== 'passed') problems.push(`API selftest ${JSON.stringify(dev.deploy.selftest)}`)
      return {
        problems,
        text: `cmd_id ${sent.cmdId}\nAPI selftest ${JSON.stringify(dev.deploy.selftest)}\nSelf-test row: ${selftestRow}\ncard: ${card?.text}`,
      }
    },
  },
  {
    id: 'selftest-failed',
    shotOf: (page) => cellOf(page, BOARDS.V.id),
    async run(page) {
      const problems = []
      const V = BOARDS.V
      const sent = await deployVia(page, V, LABELS.stfail)
      state.cmdIds.stfail = sent.cmdId
      const { dev, ok } = await waitDeploy(page, V.id, sent.cmdId, (d) => d.is_terminal, 90_000)
      if (!ok) return { problems: [`never terminal: ${JSON.stringify(dev?.deploy)}`], text: '' }
      const card = await waitCard(page, V.id, LABELS.stfail)
      const boardSays = await cardRow(page, V.id, 'Board says')
      const selftestRow = await cardRow(page, V.id, 'Self-test')
      const sendAgain = await cellOf(page, V.id).locator('[data-testid=deploy-send-again]').count()
      if (card?.outcome !== 'rolled-back') problems.push(`card outcome ${card?.outcome}`)
      if (!card?.text.includes('simulated self-test failure')) problems.push('card lacks "simulated self-test failure"')
      if (boardSays !== dev.deploy.detail) problems.push(`Board says "${boardSays}" != API detail "${dev.deploy.detail}"`)
      if (!/^failed: simulated self-test failure \(returned to ota_\w+\)$/.test(selftestRow ?? ''))
        problems.push(`Self-test row is "${selftestRow}"`)
      if (BREAK === 'selftest-failed') {
        if (sendAgain === 0) problems.push('expected a Send again button (break) and found none')
      } else if (sendAgain !== 0) problems.push('a Send again button after a self-test rollback')
      return {
        problems,
        text: `cmd_id ${sent.cmdId}\nAPI detail ${dev.deploy.detail}\nAPI selftest ${JSON.stringify(dev.deploy.selftest)}\nBoard says: ${boardSays}\nSelf-test row: ${selftestRow}\ncard: ${card?.text}`,
      }
    },
  },
  {
    id: 'apply',
    shotOf: (page) => cellOf(page, BOARDS.V.id),
    async run(page, notes) {
      const problems = []
      const V = BOARDS.V
      await page.evaluate((run) => { window.__ffRun = run }, RUN)
      const sent = await deployVia(page, V, LABELS.apply, { hold: true })
      state.cmdIds.apply = sent.cmdId
      const wantedApply = BREAK === 'apply' ? 'auto' : 'on_command'
      if (sent.sentApply !== wantedApply) problems.push(`the POST body had apply ${sent.sentApply}, wanted ${wantedApply}`)
      const staged = await waitDeploy(page, V.id, sent.cmdId, (d) => d.state === 'staged', 60_000)
      if (!staged.ok) return { problems: [...problems, `never staged: ${staged.dev?.deploy?.state}`], text: '' }
      const button = cellOf(page, V.id).locator('[data-testid=command-apply]')
      await button.waitFor({ timeout: 10_000 })
      await sleep(3000)
      const stillStaged = (await device(page, V.id))?.deploy?.state
      if (stillStaged !== 'staged') problems.push(`a held stage moved on its own to ${stillStaged}`)
      const applyId = await clickVerb(page, V.id, button)
      const line = await waitLastCommand(page, V.id, applyId, 'Apply', 'accepted')
      if (line === null) problems.push('no "Apply · accepted" line within 10 s')
      const done = await waitDeploy(page, V.id, sent.cmdId, (d) => d.is_terminal, 60_000)
      const card = await waitCard(page, V.id, LABELS.apply)
      if (done.dev?.deploy?.state !== 'confirmed') problems.push(`ended ${done.dev?.deploy?.state}`)
      if (card?.outcome !== 'good') problems.push(`card outcome ${card?.outcome}`)
      const sentinel = await page.evaluate(() => window.__ffRun)
      if (sentinel !== RUN) problems.push('the page was reloaded or navigated (the window sentinel is gone)')
      notes.push(`cmd_id ${sent.cmdId}`)
      return {
        problems,
        text: `cmd_id ${sent.cmdId}\nPOST apply ${sent.sentApply}\nlast-command: ${line}\nfinal ${done.dev?.deploy?.state}\ncard: ${card?.text}`,
      }
    },
  },
  {
    id: 'cancel',
    shotOf: (page) => cellOf(page, BOARDS.V.id),
    async run(page) {
      const problems = []
      const V = BOARDS.V
      const fwBefore = (await device(page, V.id))?.fw_version
      const sent = await deployVia(page, V, LABELS.cancel, { hold: true })
      state.cmdIds.cancel = sent.cmdId
      const staged = await waitDeploy(page, V.id, sent.cmdId, (d) => d.state === 'staged', 60_000)
      if (!staged.ok) return { problems: [`never staged: ${staged.dev?.deploy?.state}`], text: '' }
      const button = cellOf(page, V.id).locator('[data-testid=command-cancel]')
      await button.waitFor({ timeout: 10_000 })
      const cancelId = await clickVerb(page, V.id, button)
      const line = await waitLastCommand(page, V.id, cancelId, 'Cancel', 'accepted')
      if (line === null) problems.push('no "Cancel · accepted" line within 10 s')
      const done = await waitDeploy(page, V.id, sent.cmdId, (d) => d.is_terminal, 30_000)
      const card = await waitCard(page, V.id, LABELS.cancel)
      const wanted = BREAK === 'cancel' ? 'failed' : 'cancelled'
      if (card?.outcome !== wanted) problems.push(`card outcome ${card?.outcome}, wanted ${wanted}`)
      if (done.dev?.deploy?.state !== 'cancelled' || done.dev?.deploy?.is_terminal !== true)
        problems.push(`API deploy ${done.dev?.deploy?.state} is_terminal ${done.dev?.deploy?.is_terminal}`)
      await sleep(2000)
      const fwAfter = (await device(page, V.id))?.fw_version
      if (fwAfter !== fwBefore) problems.push(`fw_version changed ${fwBefore} -> ${fwAfter}`)
      if ((await cellOf(page, V.id).locator('[data-testid=command-apply]').count()) !== 0)
        problems.push('Apply is still offered on a cancelled deploy')
      const ended = await cellOf(page, V.id).locator('[data-testid=deploy-timeline] li').last().getAttribute('data-state')
      if (ended !== 'ended') problems.push(`the timeline end item is data-state="${ended}"`)
      return {
        problems,
        text: `cmd_id ${sent.cmdId}\nlast-command: ${line}\nAPI ${done.dev?.deploy?.state} is_terminal ${done.dev?.deploy?.is_terminal}\nfw ${fwBefore} -> ${fwAfter}\ntimeline end ${ended}\ncard: ${card?.text}`,
      }
    },
  },
  {
    id: 'apply-waits',
    shotOf: (page) => cellOf(page, BOARDS.H.id),
    async run(page) {
      const problems = []
      const H = BOARDS.H
      const sent = await deployVia(page, H, LABELS.hwait, { hold: true })
      state.cmdIds.hwait = sent.cmdId
      const staged = await waitDeploy(page, H.id, sent.cmdId, (d) => d.state === 'staged', 60_000)
      if (!staged.ok) return { problems: [`never staged: ${staged.dev?.deploy?.state}`], text: '' }
      const applyButton = cellOf(page, H.id).locator('[data-testid=command-apply]')
      await applyButton.waitFor({ timeout: 10_000 })
      const applyId = await clickVerb(page, H.id, applyButton)
      const line = await waitLastCommand(page, H.id, applyId, 'Apply', 'deferred')
      if (line === null) problems.push('no "Apply · deferred" line within 10 s')
      const waiting = await waitDeploy(page, H.id, sent.cmdId, (d) => d.state === 'awaiting_safe_window', 15_000)
      if (waiting.dev?.deploy?.detail !== 'apply waits for a safe window')
        problems.push(`API detail ${waiting.dev?.deploy?.detail}`)
      await until(async () => (await cellText(page, H.id)).includes('waiting for a safe moment'), 10_000)
      const stateText = flat(await cellOf(page, H.id).locator('[data-testid=deploy-state]').innerText())
      const wanted = BREAK === 'apply-waits' ? 'rolling back' : 'waiting for a safe moment'
      if (!stateText.includes(wanted)) problems.push(`state says "${stateText}", wanted "${wanted}"`)
      if (!(await cellText(page, H.id)).includes('apply waits for a safe window'))
        problems.push('the board’s "apply waits for a safe window" is not shown')
      if ((await cellOf(page, H.id).locator('[data-testid=command-cancel]').count()) !== 1)
        problems.push('Cancel update is not offered on a deferred apply')
      const samples = []
      for (let i = 0; i < 10; i++) {
        await sleep(2000)
        const text = await cellText(page, H.id)
        const s = (await device(page, H.id))?.deploy?.state
        samples.push(s)
        if (/stuck|timed out/i.test(text)) problems.push(`sample ${i}: "${text}"`)
        if ((await cellOf(page, H.id).locator('[role=progressbar], .spinner').count()) !== 0) problems.push(`sample ${i}: a spinner`)
        if (s !== 'awaiting_safe_window') problems.push(`sample ${i}: state ${s}`)
      }
      // Cleanup, and proof that a deferred apply is cancellable.
      const cancelId = await clickVerb(page, H.id, cellOf(page, H.id).locator('[data-testid=command-cancel]'))
      const cancelLine = await waitLastCommand(page, H.id, cancelId, 'Cancel', 'accepted')
      if (cancelLine === null) problems.push('no "Cancel · accepted" line for the deferred apply')
      const done = await waitDeploy(page, H.id, sent.cmdId, (d) => d.state === 'cancelled' && d.is_terminal, 30_000)
      if (!done.ok) problems.push(`the deferred apply did not cancel: ${done.dev?.deploy?.state}`)
      return {
        problems: [...new Set(problems)],
        text: `cmd_id ${sent.cmdId}\nlast-command: ${line}\nstate: ${stateText}\nAPI detail ${waiting.dev?.deploy?.detail}\n20 s samples: ${samples.join(',')}\nthen ${done.dev?.deploy?.state}`,
      }
    },
  },
  {
    id: 'auto-hold',
    shotOf: (page) => cellOf(page, BOARDS.H.id),
    async run(page) {
      const problems = []
      const H = BOARDS.H
      const sent = await deployVia(page, H, LABELS.hauto)
      state.cmdIds.hauto = sent.cmdId
      if (sent.sentApply !== 'auto') problems.push(`the POST body had apply ${sent.sentApply}`)
      const waiting = await waitDeploy(page, H.id, sent.cmdId, (d) => d.state === 'awaiting_safe_window', 60_000)
      if (!waiting.ok) return { problems: [`never parked: ${waiting.dev?.deploy?.state}`], text: '' }
      if (waiting.dev.deploy.detail !== null) problems.push(`API detail ${waiting.dev.deploy.detail}, wanted null`)
      await until(async () => (await cellText(page, H.id)).includes('waiting for a safe moment'), 10_000)
      const stateText = flat(await cellOf(page, H.id).locator('[data-testid=deploy-state]').innerText())
      if (!stateText.includes('waiting for a safe moment')) problems.push(`state says "${stateText}"`)
      const current = flat(
        await cellOf(page, H.id).locator('[data-testid=deploy-timeline] li[data-state=current]').innerText().catch(() => ''),
      )
      const wanted = BREAK === 'auto-hold' ? 'confirming' : 'staged'
      if (!current.startsWith(`… ${wanted}`) && !current.startsWith(wanted))
        problems.push(`the timeline's current item is "${current}", wanted ${wanted}`)
      const cancel = cellOf(page, H.id).locator('[data-testid=command-cancel]')
      let cancelId = null
      if ((await cancel.count()) !== 1) problems.push('Cancel update is not offered on an auto hold')
      else cancelId = await clickVerb(page, H.id, cancel)
      const line = await waitLastCommand(page, H.id, cancelId, 'Cancel', 'accepted')
      if (line === null) problems.push('no "Cancel · accepted" line within 10 s')
      const done = await waitDeploy(page, H.id, sent.cmdId, (d) => d.state === 'cancelled' && d.is_terminal, 30_000)
      if (!done.ok) problems.push(`not cancelled: ${done.dev?.deploy?.state}`)
      return {
        problems,
        text: `cmd_id ${sent.cmdId}\nstate: ${stateText}\ntimeline current: ${current}\nlast-command: ${line}\nthen ${done.dev?.deploy?.state}`,
      }
    },
  },
  {
    id: 'reboot',
    shotOf: (page) => rowOf(page, BOARDS.V.id),
    async run(page) {
      const problems = []
      const V = BOARDS.V
      const cell = cellOf(page, V.id)
      const posts = () => rebootPosts.filter((u) => u.includes(V.id)).length
      await cell.locator('[data-testid=command-reboot]').click()
      const confirm = cell.locator('[data-testid=command-reboot-confirm]')
      if (!(await confirm.isVisible())) problems.push('no confirm step after Reboot')
      const confirmText = flat(await confirm.innerText().catch(() => ''))
      if (posts() !== 0) problems.push('Reboot posted before the confirm')
      await cell.getByRole('button', { name: 'Keep running', exact: true }).click()
      await sleep(1000)
      if (BREAK === 'reboot') {
        if (posts() === 0) problems.push('expected a POST after Keep running (break) and there was none')
      } else if (posts() !== 0) problems.push('Keep running posted a reboot')
      const logBefore = await readFile(V.log, 'utf8').catch(() => '')
      const restartsBefore = (logBefore.match(/reboot {3}restarting on/g) ?? []).length
      await cell.locator('[data-testid=command-reboot]').click()
      const rebootId = await clickVerb(page, V.id, cell.getByRole('button', { name: 'Reboot now', exact: true }))
      if (rebootId === null) problems.push('Reboot now was not answered 202')
      const line = await waitLastCommand(page, V.id, rebootId, 'Reboot', 'accepted')
      if (line === null) problems.push('no "Reboot · accepted" line within 10 s')
      const restarted = await until(async () => {
        const log = await readFile(V.log, 'utf8').catch(() => '')
        return (log.match(/reboot {3}restarting on/g) ?? []).length > restartsBefore
      }, 30_000)
      if (restarted === null) problems.push('the simulator never restarted')
      const sw = await until(async () => {
        const d = await device(page, V.id)
        return d?.online && d.last_reset_reason === 'sw' ? d : null
      }, 30_000)
      if (sw === null) problems.push('the API never showed last_reset_reason sw with the board online')
      const health = flat(await rowOf(page, V.id).locator('[data-testid=health-cell]').innerText().catch(() => ''))
      if (!/\bsw\b/.test(health)) problems.push(`the Health cell lacks sw: ${health}`)
      return {
        problems,
        text: `confirm: ${confirmText}\nPOSTs to /reboot: ${posts()}\nlast-command: ${line}\nreset ${sw?.last_reset_reason}\nhealth: ${health}`,
      }
    },
  },
  {
    id: 'refused-and-legacy',
    shotOf: (page) => cellOf(page, BOARDS.H.id),
    async run(page) {
      const problems = []
      const H = BOARDS.H
      const L = BOARDS.L
      const sent = await deployVia(page, H, LABELS.hrefuse, { hold: true })
      state.cmdIds.hrefuse = sent.cmdId
      const staged = await waitDeploy(page, H.id, sent.cmdId, (d) => d.state === 'staged', 60_000)
      if (!staged.ok) return { problems: [`never staged: ${staged.dev?.deploy?.state}`], text: '' }
      const cell = cellOf(page, H.id)
      await cell.locator('[data-testid=command-reboot]').click()
      const rebootId = await clickVerb(page, H.id, cell.getByRole('button', { name: 'Reboot now', exact: true }))
      const line = await waitLastCommand(page, H.id, rebootId, 'Reboot', 'refused')
      if (line === null || !line.includes('an update is in progress')) problems.push(`refusal line: ${line}`)
      const bad = await cell.locator('[data-testid=last-command] .bad').innerText().catch(() => '')
      if (bad !== 'refused') problems.push(`the refusal word is not class bad: "${bad}"`)
      await clickVerb(page, H.id, cell.locator('[data-testid=command-cancel]'))
      const cleaned = await waitDeploy(page, H.id, sent.cmdId, (d) => d.state === 'cancelled', 30_000)
      if (!cleaned.ok) problems.push(`H's held stage did not cancel: ${cleaned.dev?.deploy?.state}`)

      // L: legacy, `ota` only.
      const lCell = cellOf(page, L.id)
      const lButtons = await lCell.locator('[data-testid=command-controls] button').count()
      if (lButtons !== 0) problems.push(`L has ${lButtons} command buttons`)
      const card = await openPrecheck(page, L.id, LABELS.good)
      const holdBoxes = await card.getByLabel('Hold at staged: install only when I click Apply').count()
      if (holdBoxes !== 0) problems.push('L has a hold box')
      await card.getByRole('button', { name: 'Cancel', exact: true }).click()
      const direct = await api(page, 'POST', `/v1/devices/${L.id}/reboot`)
      const wanted = BREAK === 'refused-and-legacy' ? 202 : 409
      if (direct.status !== wanted) problems.push(`direct POST /reboot on L: ${direct.status}, wanted ${wanted}`)
      if (!/\bota\b/.test(direct.json?.detail ?? '')) problems.push(`the 409 does not name ota: ${direct.text}`)
      return {
        problems,
        text: `cmd_id ${sent.cmdId}\nH last-command: ${line}\nL buttons ${lButtons}, hold boxes ${holdBoxes}\nL POST /reboot ${direct.status}: ${direct.json?.detail}`,
      }
    },
  },
  {
    id: 'traps',
    async run(page) {
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
      const holes = []
      for (const board of Object.values(BOARDS)) {
        const text = await cellText(page, board.id)
        if (/\b(null|undefined|NaN)\b/.test(text)) holes.push(`${board.key}: ${text}`)
      }
      // Broken: demand a leak, which a clean run cannot supply.
      if (BREAK === 'traps') {
        if (leaks.length === 0) problems.push('expected a leaked secret and found none')
      } else problems.push(...leaks)
      problems.push(...holes)
      return { problems, text: `${requested.length} requests recorded.\nholes: ${holes.length}\n` }
    },
  },
]

// ── Driver ────────────────────────────────────────────────────────────────────────────

async function shot(pageArg, name, target = null) {
  await (target ?? pageArg).screenshot({ path: `${OUT}/${name}.png` }).catch(() => {})
}

for (const board of Object.values(BOARDS)) startBoard(board)

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1400, height: 1400 } })
const page = await context.newPage()
page.on('request', (request) => {
  requested.push(request.url())
  if (request.method() === 'POST' && request.url().endsWith('/reboot')) rebootPosts.push(request.url())
})
recordCommands(page)

try {
  await page.goto(BASE)
  try {
    await login(page)
  } catch {
    await stopBoards()
    setupFailed(`the dashboard at ${BASE} refused FF_ADMIN_PASSWORD (or did not answer)`)
  }

  for (const label of Object.values(LABELS).filter((l) => l !== LABELS.baseline)) {
    const result = await upload(page, label)
    if (result.status !== 201 && result.status !== 200) {
      await stopBoards()
      setupFailed(`uploading ${label} answered ${result.status}: ${result.text}`)
    }
  }

  const ready = await until(async () => {
    for (const board of Object.values(BOARDS)) {
      const d = await device(page, board.id)
      if (d?.online !== true || d.fw_version !== LABELS.baseline) return null
    }
    return true
  }, 60_000, 1000)
  if (ready === null) {
    await stopBoards()
    setupFailed(`the simulated boards never came online (see ${BOARDS.V.log})`)
  }
  // The artifact list is read once on mount: reload so the new labels are offered.
  await page.reload()
  for (const board of Object.values(BOARDS))
    await page.waitForSelector(`[data-testid=device-row][data-device-id="${board.id}"]`, { timeout: 15_000 })
  await until(async () => (await cellOf(page, BOARDS.V.id).locator(`option[value="${LABELS.hrefuse}"]`).count()) > 0, 15_000)

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
        devices: Object.fromEntries(Object.values(BOARDS).map((b) => [b.key, b.id])),
        labels: LABELS,
        cmd_ids: state.cmdIds,
        command_ids: state.commandIds,
        results,
      },
      null,
      2,
    )}\n`,
  )
  console.log(`\n${SCENARIOS.length - failed}/${SCENARIOS.length} pass; wrote to ${OUT}`)
  console.log(`cmd_ids ${JSON.stringify(state.cmdIds)}`)
  console.log(`command_ids ${JSON.stringify(state.commandIds)}`)
  process.exitCode = failed === 0 ? 0 : 1
} finally {
  await browser.close().catch(() => {})
  await stopBoards()
}
