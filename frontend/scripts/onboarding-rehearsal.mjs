// R2b-test-1 software rehearsal. Drives the dev stack in a real Chromium and replays
// real agent log lines through a FAKE `navigator.serial`, so the console panel and the
// onboarding result card (Flow 1 steps 5-6) run in a browser for the first time, graded
// against the same bar a cold reader is held to (`spec/standards.md` -> Unaided onboarding):
// one cause, one next action, at most one remedy, one working "Copy diagnostic bundle", and
// no raw log tokens in the words the operator is asked to act on.
//
// Not a test and not wired into `npm test`: it needs the dev stack (`just up`) and the admin
// password. It is NOT the task's acceptance either. What it does not cover: detecting the
// chip, the flash itself (esptool-js speaks the ROM protocol, which is not faked) and a real
// native-USB re-enumeration. The unaided run on a real board is `docs/runbooks/unaided-onboarding.md`.
//
// The log lines are the ones the unit tests already use (`src/onboardingResult.test.ts`) and
// the bench fixtures (`src/fixtures/`), imported as .ts (Node 22.18+ strips the types).
//
// Playwright is resolved at runtime and is deliberately NOT a dependency of this package
// (see `theme-shots.mjs`): point PLAYWRIGHT_MODULE at an installed copy.
//
//   FF_ADMIN_PASSWORD=… \
//     PLAYWRIGHT_MODULE="$(npm root -g)/playwright/index.mjs" \
//     node scripts/onboarding-rehearsal.mjs [baseURL] [outDir]
//
// Exits non-zero if any scenario fails. `REHEARSAL_BREAK=<id>` flips that scenario's
// expectation on purpose, to prove the harness can fail (the vacuity check).

import { mkdir, writeFile } from 'node:fs/promises'

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? 'playwright')

const BASE = process.argv[2] ?? 'http://localhost:8088'
const OUT = process.argv[3] ?? '/tmp/ff-r2b-test-1'
const PASSWORD = process.env.FF_ADMIN_PASSWORD ?? 'fleetforge-dev-only'
const BREAK = process.env.REHEARSAL_BREAK ?? null

const fixture = async (file) => import(new URL(`../src/fixtures/${file}.ts`, import.meta.url).href)
const { BENCH_2026_10_04 } = await fixture('bench-2026-10-04')
const { BENCH_2026_09_11 } = await fixture('bench-2026-09-11')
const { REBOOT_DURING_WATCH } = await fixture('reboot-during-watch')

await mkdir(OUT, { recursive: true })

// ── Replayed logs ─────────────────────────────────────────────────────────────────────
// Copied from `src/onboardingResult.test.ts` (a test file cannot be imported without running
// its suites). `happy` gets a device id that is on the dev fleet, filled in at run time.

const FIXTURE_ID = 'a4cf12b3de90'

const happy = (id) => [
  'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
  `I (120) ff-id: device_id ${id}`,
  'I (800) ff-wifi: associated; waiting for DHCP',
  'I (900) ff-net: wifi link up, ip 192.168.1.40 gw 192.168.1.1 mask 255.255.255.0',
  'I (1500) ff-time: sntp: 1970-01-01T00:00:02Z -> 2026-09-10T21:00:00Z (via pool.ntp.org)',
  'I (2600) ff-enroll: enroll 200 https://bingo.tvaroska.sk/v1/enroll',
  `I (3100) ff-mqtt: mqtt connected as ${id} (mqtts://bingo.tvaroska.sk:8883)`,
]

// SILENT_BOARD with the disconnect reason 15 (the 4-way handshake timed out = wrong PSK),
// repeated the way the agent's reconnect loop prints it.
const WRONG_PSK = [
  'I (100) ff-agent: fleetforge agent 0.4.5 (idf v5.5.5), built Oct  4 2026 10:00:00',
  'I (120) ff-id: device_id a4cf12b3de90',
  'I (300) ff-wifi: wifi sta starting, ssid home-5g',
  'W (5300) ff-wifi: disconnected (reason 15); reconnecting in 1000 ms',
  'W (10300) ff-wifi: disconnected (reason 15); reconnecting in 1000 ms',
  'W (15300) ff-wifi: disconnected (reason 15); reconnecting in 1000 ms',
  'W (16300) ff-agent: no network yet; waiting for the link',
  'W (20300) ff-wifi: disconnected (reason 15); reconnecting in 1000 ms',
]

const SPENT_TOKEN = [
  'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
  'I (900) ff-net: wifi link up, ip 192.168.1.40 gw 192.168.1.1 mask 255.255.255.0',
  'I (1500) ff-time: sntp: 1970-01-01T00:00:02Z -> 2026-09-11T08:14:05Z (via pool.ntp.org)',
  'E (2600) ff-enroll: enroll 409: this token is already used, revoked or expired.',
  "E (2900) ff-agent: halted: this board's enrollment token was refused for good — re-flash " +
    'ff_cfg with a fresh ffe_ token (POST /v1/enrollment-tokens)',
]

// Raw log vocabulary that must never be in the headline or the next action: the operator
// is not supposed to need the log (S0-test-3).
const RAW_TOKENS = ['rst:0x', 'ESP_ERR', 'reason 15', 'Guru Meditation', 'E BOD', 'POWERON_RESET']

// ── The fake Web Serial, installed before the app loads ──────────────────────────────

function installFakeSerial({ lines, gapMs }) {
  const encoder = new TextEncoder()
  let closed = false
  let readable = null
  const calls = { forget: 0, open: 0, close: 0, signals: 0, sent: 0, total: lines.length }
  window.__ffSerialCalls = calls

  const startStream = () => {
    let i = 0
    let timer = null
    // One line per tick with a gap between, so the panel sees them arrive over time. Once
    // the replay is spent the stream idles, like a board that went quiet.
    let controller = null
    readable = new ReadableStream({
      start(c) {
        controller = c
      },
      cancel() {
        if (timer !== null) clearTimeout(timer)
        closed = true
      },
    })
    const tick = () => {
      if (closed || i >= lines.length) return
      try {
        controller.enqueue(encoder.encode(`${lines[i]}\r\n`))
      } catch {
        return
      }
      i += 1
      calls.sent = i
      timer = setTimeout(tick, gapMs)
    }
    timer = setTimeout(tick, gapMs)
  }

  const port = {
    getInfo: () => ({ usbVendorId: 0x303a, usbProductId: 0x1001 }),
    async open() {
      calls.open += 1
      closed = false
      startStream()
    },
    async close() {
      calls.close += 1
      closed = true
      readable = null
    },
    async setSignals() {
      calls.signals += 1
    },
    async forget() {
      calls.forget += 1
    },
    get readable() {
      return readable
    },
    writable: null,
  }
  const serial = {
    requestPort: async () => port,
    getPorts: async () => [port],
    addEventListener() {},
    removeEventListener() {},
  }
  Object.defineProperty(navigator, 'serial', { value: serial, configurable: true })
}

// ── Scenarios ────────────────────────────────────────────────────────────────────────
// `expect(card, page)` returns a list of failure strings (empty = pass).

const rowValue = async (card, label) => {
  const dts = card.locator('[data-testid=result-rows] dt')
  const n = await dts.count()
  for (let i = 0; i < n; i += 1) {
    if ((await dts.nth(i).innerText()).trim() === label) {
      return (await card.locator('[data-testid=result-rows] dd').nth(i).innerText()).trim()
    }
  }
  return null
}

const expectOutcome = (outcome) => async (card, ctx) => {
  const got = await card.getAttribute('data-outcome')
  const want = BREAK === ctx.id ? (outcome === 'success' ? 'failure' : 'success') : outcome
  return got === want ? [] : [`data-outcome is ${got}, wanted ${want}`]
}

async function successRows(card, labels) {
  const problems = []
  const text = await card.innerText()
  for (const label of labels) {
    const value = await rowValue(card, label)
    if (value === null) problems.push(`row "${label}" missing`)
    else if (value === '') problems.push(`row "${label}" is empty`)
  }
  if (/UI and API differ/.test(text)) problems.push('card says UI and API differ')
  return problems
}

// The S0-test-3 bar, checked mechanically on a failure card.
async function failureBar(card, page) {
  const problems = []
  const headline = card.locator('[data-testid=result-headline]')
  const next = card.locator('[data-testid=result-next]')
  if ((await headline.count()) !== 1) problems.push(`${await headline.count()} headlines, wanted 1`)
  if ((await next.count()) !== 1) problems.push(`${await next.count()} next actions, wanted 1`)
  const remedies = await page.locator('[data-testid=console-remedy]').count()
  if (remedies > 1) problems.push(`${remedies} remedy buttons, wanted at most 1`)
  const copies = page.getByRole('button', { name: 'Copy diagnostic bundle' })
  let visible = 0
  for (let i = 0; i < (await copies.count()); i += 1) if (await copies.nth(i).isVisible()) visible += 1
  if (visible !== 1) problems.push(`${visible} visible "Copy diagnostic bundle" buttons, wanted 1`)
  else {
    await copies.first().click()
    await page.waitForTimeout(300)
    const clip = await page.evaluate(() => navigator.clipboard.readText()).catch((e) => `ERR ${e}`)
    if (typeof clip !== 'string' || clip.trim() === '' || clip.startsWith('ERR'))
      problems.push(`copy put nothing usable on the clipboard (${String(clip).slice(0, 60)})`)
  }
  const spoken = `${await headline.innerText().catch(() => '')}\n${await next.innerText().catch(() => '')}`
  for (const token of RAW_TOKENS)
    if (spoken.includes(token)) problems.push(`raw log token "${token}" in headline/next`)
  return problems
}

const SCENARIOS = [
  {
    id: 'happy',
    lines: (ctx) => happy(ctx.deviceId),
    async expect(card, ctx) {
      const problems = [...(await expectOutcome('success')(card, ctx))]
      problems.push(
        ...(await successRows(card, [
          'Device id', 'Firmware', 'Link', 'Clock source', 'Enrolled', 'On the fleet', 'UI / API',
        ])),
      )
      const form = ctx.page.locator('[data-testid=name-board]')
      if (ctx.deviceIsOnFleet) {
        if ((await form.count()) !== 1) problems.push('no "Board name" form on the success card')
        else problems.push(...(await nameRoundTrip(ctx)))
      } else {
        ctx.notes.push('fixture id is not on the dev fleet: name form not asserted')
      }
      return problems
    },
  },
  {
    id: 'bench-2026-10-04',
    lines: () => BENCH_2026_10_04,
    async expect(card, ctx) {
      const problems = [...(await expectOutcome('success')(card, ctx))]
      problems.push(...(await successRows(card, ['Clock source', 'UI / API'])))
      const clock = await rowValue(card, 'Clock source')
      if (clock === null || /not set/i.test(clock) || !/kept/i.test(clock))
        problems.push(`Clock source reads "${clock}", wanted the "kept across the reset" wording`)
      return problems
    },
  },
  {
    id: 'brownout-loop',
    lines: () => BENCH_2026_09_11,
    async expect(card, ctx) {
      const problems = [...(await expectOutcome('failure')(card, ctx))]
      problems.push(...(await failureBar(card, ctx.page)))
      const text = await card.innerText()
      if (!/power|brownout|supply|usb|cable/i.test(text)) problems.push('card does not name a power cause')
      if (!/reboot|restart/i.test(await ctx.page.locator('section[aria-labelledby=console-heading]').innerText()))
        problems.push('no restarts/reboot-loop line on the panel')
      if ((await ctx.page.locator('[data-testid=console-remedy]').count()) !== 0)
        problems.push('a software remedy button is offered for a power fault')
      return problems
    },
  },
  {
    id: 'reboot-during-watch',
    lines: () => REBOOT_DURING_WATCH,
    async expect(card, ctx) {
      const panel = await ctx.page.locator('section[aria-labelledby=console-heading]').innerText()
      const problems = []
      if (!/rebooted 3.{0,3}: brownout/i.test(panel)) problems.push('panel lacks "rebooted 3×: brownout"')
      if (!/Boot 4\s*·\s*reset: brownout/i.test(panel)) problems.push('panel lacks "Boot 4 · reset: brownout"')
      if (!/lost/i.test(panel)) problems.push('no milestone marked lost at the restart')
      const got = await card.getAttribute('data-outcome').catch(() => null)
      if (got === 'success') problems.push('data-outcome is success for a rebooting board')
      if (got === 'failure') problems.push(...(await failureBar(card, ctx.page)))
      return problems
    },
    needsCard: false,
  },
  {
    id: 'wrong-psk',
    lines: () => WRONG_PSK,
    waitCardMs: 80_000,
    async expect(card, ctx) {
      const problems = [...(await expectOutcome('failure')(card, ctx))]
      problems.push(...(await failureBar(card, ctx.page)))
      const text = `${await card.locator('[data-testid=result-headline]').innerText()}\n${await card
        .locator('[data-testid=result-next]')
        .innerText()}`
      if (!/password|passphrase/i.test(text)) problems.push('headline/next do not name the Wi-Fi password')
      return problems
    },
  },
  {
    id: 'spent-token',
    lines: () => SPENT_TOKEN,
    async expect(card, ctx) {
      // The break flips this one: it wants "success", which a spent token can never be.
      const problems = [...(await expectOutcome('failure')(card, ctx))]
      problems.push(...(await failureBar(card, ctx.page)))
      const headline = await card.locator('[data-testid=result-headline]').innerText()
      if (!/enrolment refused/i.test(headline)) problems.push(`headline is "${headline}"`)
      const remedy = ctx.page.locator('[data-testid=console-remedy]')
      if ((await remedy.count()) !== 1) problems.push(`${await remedy.count()} remedy buttons, wanted 1`)
      else if (!/re-?flash/i.test(await remedy.innerText())) problems.push('the remedy is not a re-flash button')
      return problems
    },
  },
]

// Name the board from the card, find it in the fleet table, then clear it (leave the dev DB
// as found). Resolves to a list of problems.
async function nameRoundTrip({ page, deviceId }) {
  const problems = []
  const NAME = 'rehearsal coop door'
  const form = page.locator('[data-testid=name-board]')
  await form.getByLabel('Board name').fill(NAME)
  await form.getByLabel('Board name').press('Enter')
  try {
    await page.locator('table').filter({ hasText: NAME }).first().waitFor({ timeout: 8000 })
  } catch {
    problems.push(`fleet table never showed "${NAME}"`)
  }
  // Clear it again.
  await form.getByLabel('Board name').fill('')
  await form.getByLabel('Board name').press('Enter')
  await page.waitForFunction(
    (n) => !Array.from(document.querySelectorAll('table')).some((t) => t.innerText.includes(n)),
    NAME,
    { timeout: 8000 },
  ).catch(() => problems.push(`"${NAME}" is still in the fleet table after clearing`))
  const left = await page.evaluate(async (id) => {
    const r = await fetch('/v1/devices')
    const body = await r.json()
    return body.devices.find((d) => d.device_id === id)?.name ?? null
  }, deviceId)
  if (left !== null) problems.push(`dev DB left with name ${JSON.stringify(left)}`)
  return problems
}

// ── Driver ───────────────────────────────────────────────────────────────────────────

const browser = await chromium.launch()
let failed = 0

// Pick a device that is on the dev fleet (any live row will do: the card matches on id) and
// carries no name, so the round trip can leave it as found.
async function pickFleetId() {
  const context = await browser.newContext()
  const page = await context.newPage()
  await page.goto(BASE)
  await login(page)
  const id = await page.evaluate(async () => {
    const body = await (await fetch('/v1/devices')).json()
    return body.devices.find((d) => d.name === null && d.device_id !== '000000000000')?.device_id ?? null
  })
  await context.close()
  return id
}

async function login(page) {
  await page.waitForSelector('input[type=password]')
  await page.fill('input[type=password]', PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForSelector('[data-testid=device-row]', { timeout: 15_000 })
}

const fleetId = await pickFleetId()
const deviceId = fleetId ?? FIXTURE_ID

for (const scenario of SCENARIOS) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 1000 },
    permissions: ['clipboard-read', 'clipboard-write'],
  })
  const page = await context.newPage()
  const notes = []
  const ctx = { id: scenario.id, page, deviceId, deviceIsOnFleet: fleetId !== null, notes }
  let problems = []
  try {
    await page.addInitScript(installFakeSerial, { lines: scenario.lines(ctx), gapMs: 200 })
    await page.goto(BASE)
    await login(page)
    // Step 2 as an operator leaves it after a flash: the network filled in, which is what
    // makes the card's re-flash remedy a button rather than "fill in step 2". Not a secret.
    await page.getByLabel('SSID').fill('rehearsal-net')
    await page.getByLabel('Passphrase').fill('rehearsal-not-a-secret')
    await page.getByRole('button', { name: 'Watch a board' }).click()
    const card = page.locator('[data-testid=result-card]')
    const needsCard = scenario.needsCard !== false
    try {
      await card.waitFor({ timeout: scenario.waitCardMs ?? 20_000 })
    } catch {
      if (needsCard) throw new Error('no result card appeared')
      // reboot-during-watch is judged on the panel; the card may never settle.
      await page.waitForTimeout(3000)
    }
    // Let the whole replay land, then the 1 Hz tick settle, before reading anything.
    await page.waitForFunction(() => window.__ffSerialCalls.sent === window.__ffSerialCalls.total, null, {
      timeout: 30_000,
    })
    await page.waitForTimeout(1500)
    problems = await scenario.expect(card.first(), ctx)
    const forgets = await page.evaluate(() => window.__ffSerialCalls?.forget ?? 0)
    if (forgets !== 0) problems.push(`port.forget() was called ${forgets}x`)
    const text = (await card.count()) > 0 ? await card.first().innerText() : '(no result card)'
    const panel = await page.locator('section[aria-labelledby=console-heading]').innerText()
    await writeFile(`${OUT}/${scenario.id}.txt`, `${text}\n\n--- console panel ---\n${panel}\n`)
  } catch (err) {
    problems = [...problems, `harness: ${err instanceof Error ? err.message : String(err)}`]
    await writeFile(`${OUT}/${scenario.id}.txt`, `(no card text: ${problems.join('; ')})\n`).catch(() => {})
  }
  await page.screenshot({ path: `${OUT}/${scenario.id}.png`, fullPage: true }).catch(() => {})
  await context.close()
  const note = notes.length > 0 ? ` [${notes.join('; ')}]` : ''
  if (problems.length === 0) console.log(`PASS ${scenario.id}: as expected${note}`)
  else {
    failed += 1
    console.log(`FAIL ${scenario.id}: ${problems.join(' | ')}${note}`)
  }
}

await browser.close()
console.log(`\n${SCENARIOS.length - failed}/${SCENARIOS.length} pass; wrote to ${OUT}`)
process.exit(failed === 0 ? 0 : 1)
