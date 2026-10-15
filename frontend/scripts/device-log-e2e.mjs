// R4-fe-2: the per-device log tail played through the REAL dashboard in a real Chromium,
// against the dev stack, with SIMULATED boards, and graded mechanically. It is the harness
// for "newest first, level, text, `dropped` gaps" and for "a new batch shows with no reload".
//
// Nothing on a device sends `up/log` yet (R4-fw-3 is gated on the owner), so the two
// boards are real simulator processes (enrolled, online, heartbeating) and this script
// publishes `up/log` batches AS THE TALKER BOARD, with that board's own broker credential,
// through the real broker, ingestor, database and API. The credential never enters Node: a
// Python child reads it from the simulator's state file (`.sim/<device_id>.json`).
//
// What it plays, in one logged-in session (batches A, B and C are published first):
//
//   tail-order      the talker's section reads newest first, in the server's order: the C
//                   line, `boom`, the gap row `3 lines missing here` directly under it,
//                   the W line, the first I line; levels I, E, W, I
//   levels          the E and W cells are bold (computed weight 700), the I cell is not
//   untrusted-text  a line carrying `<img ...>` and a URL shows as literal text with `<url>`
//                   in place of the URL: no img element, no `window.__pwned`, no evil URL
//   live-line       a new batch appears at the top within 5 s with no reload (a window
//                   sentinel survives)
//   gap-marker      a `dropped:7` batch with no lines shows as a gap row only
//   switch-board    picking the quiet board shows the empty state and none of the talker's
//   api-agrees      the API's lines, in order, equal the page's once the talker is re-picked
//   traps           no token or password in any requested URL or simulator log
//
// What it does NOT cover: a real agent emitting log lines (R4-fw-3, R4-test-1). Not a test
// and not wired into `npm test`. It needs the dev stack (`just up`), `uv`, the admin
// password, and the broker on 127.0.0.1:$FF_MQTT_PORT (default 8883). Playwright is
// resolved at runtime and is deliberately NOT a dependency of this package (see
// `update-flow-e2e.mjs`).
//
//   FF_ADMIN_PASSWORD=… \
//     PLAYWRIGHT_MODULE="$(npm root -g)/playwright/index.mjs" \
//     node scripts/device-log-e2e.mjs [baseURL=http://localhost:8088] [outDir=/tmp/ff-r4-fe-2]
//
// or `just log-e2e`. The password falls back to the `FF_ADMIN_PASSWORD=` line of the
// repo-root `.env`; it is never printed and never put in an argv.
//
// Every run uses a fresh prefix, so it enrolls two fresh boards and leaves them (offline
// once the simulators exit) and their log lines in the dev database; it deletes nothing.
//
// Exit: 0 all pass, 1 any scenario failed, 2 setup failed (stack down, login refused, the
// boards never came online, the up/log publish never landed). `E2E_BREAK=<scenario id>`
// flips that scenario's primary expectation on purpose, to prove the harness can fail.

import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createWriteStream, readFileSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? 'playwright')

const BASE = process.argv[2] ?? 'http://localhost:8088'
const OUT = process.argv[3] ?? '/tmp/ff-r4-fe-2'
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
  talker: { prefix: `lg${RUN}` },
  quiet: { prefix: `lg${RUN}q` },
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

// ── Publishing up/log as a board ──────────────────────────────────────────────────────

// Where the simulator keeps credentials, relative to ROOT as its own default is.
const STATE_DIR = process.env.FF_SIM_STATE_DIR ?? '.sim'

// A Python child reads the board's credential from its state file and publishes one message
// as that board. The password never enters Node, an argv or a log. A denied publish is
// silent in MQTT 3.1.1, so setup checks the API after the first one.
const PUBLISH_PY = [
  'import json, os, dotenv, paho.mqtt.publish as publish',
  'dotenv.load_dotenv()',
  'c = json.load(open(os.environ["FF_PUB_STATE"]))',
  'publish.single(os.environ["FF_PUB_TOPIC"], os.environ["FF_PUB_MESSAGE"], qos=1, hostname="127.0.0.1", port=int(os.environ.get("FF_MQTT_PORT", "8883")), auth={"username": c["mqtt_username"], "password": c["mqtt_password"]})',
].join('\n')

function publishLog(board, payload) {
  return new Promise((resolve, reject) => {
    const child = spawn('uv', ['run', 'python', '-c', PUBLISH_PY], {
      cwd: ROOT,
      env: {
        ...process.env,
        FF_PUB_STATE: `${ROOT}${STATE_DIR}/${board.id}.json`,
        FF_PUB_TOPIC: `ff/v1/d/${board.id}/up/log`,
        FF_PUB_MESSAGE: JSON.stringify(payload),
      },
      stdio: ['ignore', 'ignore', 'pipe'],
    })
    let stderr = ''
    child.stderr.on('data', (chunk) => { stderr += chunk })
    child.on('error', reject)
    child.on('close', (code) =>
      code === 0 ? resolve() : reject(new Error(`publish exited ${code}: ${stderr.trim().split('\n').pop()}`)),
    )
  })
}

const EVIL = 'https://evil.example/x'
const BATCHES = {
  A: { dropped: 0, lines: [
    { age_ms: 5120, level: 'I', text: `morse: HELLO ${RUN}` },
    { age_ms: 0, level: 'W', text: `sensor: no reply ${RUN}` },
  ] },
  B: { dropped: 3, lines: [{ age_ms: 0, level: 'E', text: `boom ${RUN}` }] },
  C: { dropped: 0, lines: [{ age_ms: 0, level: 'I', text: `<img src=x onerror="window.__pwned=1"> ${EVIL} ${RUN}` }] },
  D: { dropped: 0, lines: [{ age_ms: 0, level: 'I', text: `live ${RUN}` }] },
  E: { dropped: 7, lines: [] },
}
const TEXT_C = `<img src=x onerror="window.__pwned=1"> <url> ${RUN}`

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


const sectionOf = (page, id) => page.locator(`[data-testid=device-log][data-device-id="${id}"]`)
const rowsOf = (section) => section.locator('[data-testid=log-line], [data-testid=log-gap]')

async function pick(page, id) {
  await rowOf(page, id).locator('button.pick-board').click()
  await sectionOf(page, id).waitFor({ timeout: 10_000 })
}

// The visible rows, in DOM order: {kind, text, level, dropped}.
function readRows(section) {
  return section.evaluate((el) =>
    [...el.querySelectorAll('[data-testid=log-line], [data-testid=log-gap]')].map((tr) =>
      tr.getAttribute('data-testid') === 'log-gap'
        ? { kind: 'gap', text: tr.innerText.replace(/\s+/g, ' ').trim(), dropped: tr.getAttribute('data-dropped') }
        : {
            kind: 'line',
            text: tr.cells[2].innerText,
            level: tr.cells[1].innerText.trim(),
            data_level: tr.getAttribute('data-level'),
            weight: getComputedStyle(tr.cells[1]).fontWeight,
          },
    ),
  )
}

const describeRows = (rows) => rows.map((r) => (r.kind === 'gap' ? `[gap ${r.dropped}] ${r.text}` : `${r.level} ${r.text}`))

// ── Scenarios ─────────────────────────────────────────────────────────────────────────

const requested = []

const SCENARIOS = [
  {
    id: 'tail-order',
    async run(page) {
      const problems = []
      const section = sectionOf(page, BOARDS.talker.id)
      const rows = await readRows(section)
      const first = rows.slice(0, 5)
      const wantTexts = BREAK === 'tail-order'
        ? [`morse: HELLO ${RUN}`]
        : [TEXT_C, `boom ${RUN}`, '⋯ 3 lines missing here', `sensor: no reply ${RUN}`, `morse: HELLO ${RUN}`]
      const got = first.map((r) => r.text.replace(/\s+/g, ' ').trim())
      const gotShown = got.map((t) => t.replace(/^⋯ /, '⋯ '))
      if (BREAK === 'tail-order') {
        if (gotShown[0] !== wantTexts[0]) problems.push(`the first row is "${gotShown[0]}", the break expected the morse line first`)
      } else {
        wantTexts.forEach((want, i) => {
          if (gotShown[i] !== want) problems.push(`row ${i + 1} is "${gotShown[i]}", wanted "${want}"`)
        })
        const levels = first.filter((r) => r.kind === 'line').map((r) => r.level).join(',')
        if (levels !== 'I,E,W,I') problems.push(`levels read ${levels}, wanted I,E,W,I`)
        if (first[2]?.kind !== 'gap') problems.push('the third row is not a gap row')
      }
      return { problems, text: describeRows(rows).join('\n'), shot: section }
    },
  },
  {
    id: 'levels',
    async run(page) {
      const problems = []
      const section = sectionOf(page, BOARDS.talker.id)
      const lines = (await readRows(section)).filter((r) => r.kind === 'line')
      const byLevel = (l) => lines.find((r) => r.level === l)
      const e = byLevel('E')
      const w = byLevel('W')
      const i = byLevel('I')
      if (!e || !w || !i) return { problems: ['expected E, W and I lines on the page'], text: '(none)', shot: section }
      const bold = (r) => Number(r.weight) >= 700
      if (BREAK === 'levels') {
        if (!bold(i)) problems.push(`the I cell is weight ${i.weight}, the break expected it bold`)
      } else {
        if (!bold(e)) problems.push(`the E cell is weight ${e.weight}, wanted 700`)
        if (!bold(w)) problems.push(`the W cell is weight ${w.weight}, wanted 700`)
        if (bold(i)) problems.push(`the I cell is weight ${i.weight}, wanted it plain`)
        for (const r of lines) if (r.data_level !== r.level) problems.push(`data-level ${r.data_level} != cell ${r.level}`)
      }
      return { problems, text: `E ${e.weight}\nW ${w.weight}\nI ${i.weight}`, shot: section }
    },
  },
  {
    id: 'untrusted-text',
    async run(page) {
      const problems = []
      const section = sectionOf(page, BOARDS.talker.id)
      const rows = await readRows(section)
      const row = rows.find((r) => r.kind === 'line' && r.text.includes('<img src=x'))
      const imgs = await section.locator('img').count()
      const pwned = await page.evaluate(() => window.__pwned)
      const pageText = await page.locator('body').innerText()
      if (BREAK === 'untrusted-text') {
        if (imgs === 0) problems.push('no img element in the section (the break demanded one)')
      } else {
        if (!row) problems.push('no row carries the literal `<img src=x` text')
        else {
          if (!row.text.includes('<url>')) problems.push(`the row lacks the <url> placeholder: ${row.text}`)
          if (row.text.includes('evil.example')) problems.push('the row still carries the evil URL')
        }
        if (imgs !== 0) problems.push(`${imgs} img element(s) inside the section`)
        if (pwned !== undefined) problems.push('window.__pwned was set: the text ran as markup')
        if (pageText.includes('evil.example')) problems.push('evil.example appears on the page')
        if (requested.some((u) => u.includes('evil.example'))) problems.push('the browser requested evil.example')
      }
      return { problems, text: row?.text ?? '(no row)', shot: section }
    },
  },
  {
    id: 'live-line',
    async run(page, notes) {
      const problems = []
      const talker = BOARDS.talker
      const section = sectionOf(page, talker.id)
      await page.evaluate((run) => { window.__ffRun = run }, RUN)
      const firstText = async () => (await readRows(section))[0]?.text ?? ''
      const sent = Date.now()
      await publishLog(talker, BATCHES.D)
      const sentDone = Date.now()
      let seen = null
      if (BREAK === 'live-line') {
        await sleep(3000)
        if ((await firstText()).includes(`live ${RUN}`)) seen = true
      } else {
        seen = await until(async () => ((await firstText()).includes(`live ${RUN}`) ? true : null), 5000, 100)
      }
      const latency = Date.now() - sentDone
      const sentinel = await page.evaluate(() => window.__ffRun)
      notes.push(`publish ${sentDone - sent} ms; visible ${latency} ms after the publish returned`)
      if (BREAK === 'live-line') {
        if (seen !== null) problems.push('the new line appeared within 3 s (the break expected it not to)')
      } else {
        if (seen === null) problems.push('the new line did not reach the top within 5 s with no reload')
        if (sentinel !== RUN) problems.push('the page was reloaded or navigated (the window sentinel is gone)')
      }
      return { problems, text: `first row: ${await firstText()}`, shot: section }
    },
  },
  {
    id: 'gap-marker',
    async run(page) {
      const problems = []
      const talker = BOARDS.talker
      const section = sectionOf(page, talker.id)
      const before = await section.locator('[data-testid=log-line]').count()
      await publishLog(talker, BATCHES.E)
      const isGap = async () => {
        const rows = await readRows(section)
        return rows[0]?.kind === 'gap' && rows[0].dropped === '7' ? rows : null
      }
      const rows = BREAK === 'gap-marker' ? (await sleep(3000), await readRows(section)) : await until(isGap, 5000, 100)
      const after = await section.locator('[data-testid=log-line]').count()
      if (BREAK === 'gap-marker') {
        if (rows[0]?.kind !== 'line') problems.push(`the first row is a ${rows[0]?.kind}, the break expected a log-line`)
      } else {
        if (rows === null) problems.push('the first row never became a 7-line gap row within 5 s')
        else if (!rows[0].text.includes('7 lines missing here')) problems.push(`gap text: ${rows[0].text}`)
        if (after !== before) problems.push(`line rows went from ${before} to ${after}: the marker added an empty line`)
      }
      return { problems, text: describeRows(rows ?? (await readRows(section))).slice(0, 4).join('\n'), shot: section }
    },
  },
  {
    id: 'switch-board',
    async run(page) {
      const problems = []
      await pick(page, BOARDS.quiet.id)
      const section = sectionOf(page, BOARDS.quiet.id)
      const empty = section.locator('[data-testid=device-log-empty]')
      await empty.waitFor({ timeout: 10_000 }).catch(() => {})
      const text = await section.innerText()
      if (BREAK === 'switch-board') {
        if (!text.includes(`boom ${RUN}`)) problems.push('the quiet board does not show the talker\'s boom line (the break expected it)')
      } else {
        if ((await empty.count()) !== 1) problems.push('the quiet board does not show the empty state')
        if (text.includes(RUN)) problems.push('the quiet board shows text from the talker')
        if ((await section.getAttribute('data-device-id')) !== BOARDS.quiet.id) problems.push('data-device-id is not the quiet board')
      }
      return { problems, text: flat(text), shot: section }
    },
  },
  {
    id: 'api-agrees',
    async run(page) {
      const problems = []
      const talker = BOARDS.talker
      await pick(page, talker.id)
      const section = sectionOf(page, talker.id)
      await section.locator('[data-testid=log-line]').first().waitFor({ timeout: 10_000 })
      const { status, json } = await api(page, 'GET', `/v1/devices/${talker.id}/logs?limit=200`)
      if (status !== 200) return { problems: [`the logs endpoint answered ${status}`], text: '(none)', shot: section }
      let apiTexts = json.lines.filter((l) => l.text !== null).map((l) => l.text)
      if (BREAK === 'api-agrees') apiTexts = [...apiTexts].reverse()
      const pageTexts = (await readRows(section)).filter((r) => r.kind === 'line').map((r) => r.text)
      if (JSON.stringify(apiTexts) !== JSON.stringify(pageTexts)) {
        problems.push(`the API lists ${JSON.stringify(apiTexts)}; the page lists ${JSON.stringify(pageTexts)}`)
      }
      return { problems, text: `${apiTexts.length} API lines vs ${pageTexts.length} on the page`, shot: section }
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

startBoard(BOARDS.talker)
startBoard(BOARDS.quiet)

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

  const online = await until(async () => {
    const [talker, quiet] = [await device(page, BOARDS.talker.id), await device(page, BOARDS.quiet.id)]
    return talker?.online === true && quiet?.online === true ? true : null
  }, 60_000, 1000)
  if (online === null) {
    await stopBoards()
    setupFailed(`the simulated boards never came online (see ${BOARDS.talker.log})`)
  }

  // Batches A, B, C, a second apart so receipt times (and so the server's order) differ.
  try {
    await publishLog(BOARDS.talker, BATCHES.A)
  } catch (err) {
    await stopBoards()
    setupFailed(`could not publish up/log as the talker: ${err instanceof Error ? err.message : err}`)
  }
  const landed = await until(async () => {
    const { json } = await api(page, 'GET', `/v1/devices/${BOARDS.talker.id}/logs?limit=200`)
    return (json?.lines ?? []).length >= 2 ? true : null
  }, 20_000, 500)
  if (landed === null) {
    await stopBoards()
    setupFailed('the up/log publish never landed (credential or ACL?)')
  }
  for (const key of ['B', 'C']) {
    await sleep(1200)
    await publishLog(BOARDS.talker, BATCHES[key])
  }
  await sleep(1200)
  const all = await until(async () => {
    const { json } = await api(page, 'GET', `/v1/devices/${BOARDS.talker.id}/logs?limit=200`)
    return (json?.lines ?? []).length >= 4 ? true : null
  }, 20_000, 500)
  if (all === null) {
    await stopBoards()
    setupFailed('batches B and C never landed')
  }

  await page.reload()
  for (const board of Object.values(BOARDS))
    await page.waitForSelector(`[data-testid=device-row][data-device-id="${board.id}"]`, { timeout: 15_000 })

  // The first scenarios read the talker's section; selecting it is the page's own control.
  await pick(page, BOARDS.talker.id)
  await sectionOf(page, BOARDS.talker.id).locator('[data-testid=log-line]').first().waitFor({ timeout: 10_000 })
  await sleep(500)

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
    await (outcome.shot ?? page.locator('body')).screenshot({ path: `${OUT}/${scenario.id}.png` }).catch(() => {})
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
      { run: RUN, base: BASE, devices: { talker: BOARDS.talker.id, quiet: BOARDS.quiet.id }, results },
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
