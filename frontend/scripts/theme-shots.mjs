// S0-fe-2 T2 harness. Drives the dev stack in a real Chromium and captures every screen
// of the app in the theme, plus a greyscale pass over the fleet table.
//
// Not a test and not wired into `npm test`: it needs `just up`, the admin password and
// at least one live simulated board, none of which a unit test may assume. Kept because
// the theme's acceptance is visual — a future restyle should re-shoot these rather than
// re-derive how to reach each state.
//
// The app is one authenticated page with stacked <section>s, not a router, so "every
// page" means the login gate plus each section of that page.
//
// Playwright is resolved at runtime and is deliberately NOT a dependency of this
// package: it and its browsers are ~400 MB, `frontend/Dockerfile` runs `npm ci`, and a
// visual harness must not land in the production image. Point PLAYWRIGHT_MODULE at an
// installed copy (`npm root -g`/playwright/index.mjs is the usual one on this box).
//
//   just up && FF_ADMIN_PASSWORD=… \
//     PLAYWRIGHT_MODULE="$(npm root -g)/playwright/index.mjs" \
//     node scripts/theme-shots.mjs [baseURL] [outDir]

import { mkdir } from 'node:fs/promises'

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? 'playwright')

const BASE = process.argv[2] ?? 'http://localhost:8080'
const OUT = process.argv[3] ?? '/tmp/ff-theme'
const PASSWORD = process.env.FF_ADMIN_PASSWORD ?? 'fleetforge-dev-only'

await mkdir(OUT, { recursive: true })

const browser = await chromium.launch()
// deviceScaleFactor 2 so "legible at default zoom" is judged on a real pixel grid rather
// than a downsampled one — a pixel font is precisely the case where that would lie.
const page = await browser.newPage({
  viewport: { width: 1280, height: 1000 },
  deviceScaleFactor: 2,
})

const shot = async (name, target = page, opts = {}) => {
  await target.screenshot({ path: `${OUT}/${name}.png`, ...opts })
  console.log(`  ${name}.png`)
}

// ── 1. Login ─────────────────────────────────────────────────────────────────
await page.goto(BASE)
await page.waitForSelector('input[type=password]')
// Prove the self-hosted pixel font actually loaded before judging any screenshot;
// otherwise every shot below silently falls back to the monospace stack.
await page.waitForFunction(() => document.fonts.check('12px "Press Start 2P"'))
console.log('login:')
await shot('1-login')

// ── 2. Fleet ─────────────────────────────────────────────────────────────────
await page.fill('input[type=password]', PASSWORD)
await page.locator('button[type=submit]').click()
await page.waitForSelector('[data-testid=device-row]')
await page.waitForTimeout(2000) // let the 10 s re-read settle; presence is the server's answer
console.log('fleet:')
const fleet = page.locator('section[aria-labelledby=fleet-heading]')
await shot('2-fleet', fleet)

const table = page.locator('section[aria-labelledby=fleet-heading] table').first()
await shot('2a-fleet-table', table)

// The acceptance criterion. Applied as a live CSS filter rather than by converting the
// PNG afterwards — that is the stricter test, because it also catches anything that
// depends on hue at render time (a coloured accent, an emoji, a form control).
await page.addStyleTag({ content: 'html { filter: grayscale(100%) !important; }' })
await shot('2b-fleet-table-greyscale', table)
await shot('2c-fleet-greyscale', fleet)
await page.reload()
await page.waitForSelector('[data-testid=device-row]')
await page.waitForTimeout(1000)

// ── 3. Flash ─────────────────────────────────────────────────────────────────
// No board is attached to this box, so this is the pre-connect state: the form, the
// fieldsets, the choice lists, the disabled-button treatment and the step headings.
console.log('flash:')
await shot('3-flash', page.locator('section[aria-labelledby=flash-heading]'))

// ── 4. Enroll ────────────────────────────────────────────────────────────────
console.log('enroll:')
const enroll = page.locator('section[aria-labelledby=enroll-heading]')
await enroll.scrollIntoViewIfNeeded()
await shot('4-enroll', enroll)

// The issued-token panel: `.issued`, the one element carrying the offset block shadow,
// with a real single-use token on screen (12-hex device ids and a token are the two
// long strings the theme must not damage).
await page.getByRole('button', { name: /generate|issue|mint/i }).first().click()
await page.waitForSelector('.issued')
await page.waitForTimeout(500)
await shot('4a-enroll-issued', page.locator('.issued'))

// ── 5. The whole page, and a legibility crop at 1:1 ──────────────────────────
console.log('page:')
await shot('5-full-page', page, { fullPage: true })

// A second context, because deviceScaleFactor is fixed per context and this shot exists
// to judge the pixel font at exactly 1 device pixel per CSS pixel — the case where a
// pixel face either lands on the grid or turns to mush. The cost is a second login: the
// session cookie belongs to the context, so it does not come along.
const oneToOneCtx = await browser.newContext({
  viewport: { width: 1280, height: 1000 },
  deviceScaleFactor: 1,
})
const oneToOne = await oneToOneCtx.newPage()
await oneToOne.goto(BASE)
await oneToOne.waitForSelector('input[type=password]')
await oneToOne.fill('input[type=password]', PASSWORD)
await oneToOne.locator('button[type=submit]').click()
await oneToOne.waitForSelector('[data-testid=device-row]')
await oneToOne.waitForTimeout(1500)
await shot('5a-fleet-table-1x', oneToOne.locator('table').first())

await browser.close()
console.log(`\nwrote to ${OUT}`)
