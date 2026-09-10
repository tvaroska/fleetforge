# Dashboard — look, feel and cross-cutting UI

The operator-facing shell: theme, typography, colour, accessibility, and anything else
that spans every page rather than belonging to one feature.

Per-page behaviour lives with its feature — the enroll page, the flasher, the live device
list and the serial console are all in [enrollment.md](enrollment.md). This file is for
changes that cut across all of them.

Completed-work archive for this feature area (`docs/features/`). This holds the
plan **substance**, not links — `.claude/plans/*` are local and gitignored, so
their reasoning must be captured HERE (and decisions logged in `DECISIONS.md`).
When `/implement` finishes a task, it appends a completed entry below.

---

## Completed Work

<!-- Newest first. One entry per completed task; capture what a future reader
     needs WITHOUT the local plan file. -->

### 2026-09-10 — S0-fe-2 Monochrome pixel-art theme

**What shipped:** A whole-app restyle to a monochrome pixel-art theme — login, fleet,
flash and enroll, plus the issued-token panel, the boot checklist and both log panels.
One achromatic hue ramp, a self-hosted pixel display face, hard edges everywhere (no
border radii, 2 px borders, offset block shadows), and — because a single hue ramp
removes colour as a carrier of meaning — a second encoding axis on every state.

**Approach:** Four files, and the CSS is nearly all of it.

- `frontend/src/index.css` (128 → ~330 lines). The palette became seven CSS custom
  properties, all achromatic: `--ff-bg #0c0c0c`, `--ff-panel #141414`, `--ff-line
  #3a3a3a`, `--ff-line-hi #6a6a6a`, `--ff-dim #8a8a8a`, `--ff-fg #d8d8d8`, `--ff-hi
  #fff`. Contrast on the background is 5.6:1 for `--ff-dim` and 14:1 for `--ff-fg`, so
  the dimmest text on the page still clears AA. Elements that previously had no rules at
  all — `button`, `input`, `select`, `a`, `h3`, `:focus-visible`, `progress` — got them,
  which is most of the line growth.
- **Two fonts, and the split is the whole readability story.** `--ff-display` is Press
  Start 2P and is used *only* for short chrome: `h1`, `h2`, `h3`, `th`, `legend`,
  `button`. `--ff-mono` (`ui-monospace` stack) carries everything else. The task's
  constraint was that a pixel font must not cost the legibility of 12-hex-digit device
  ids or the flash log, and confining the display face to chrome is how that is met
  rather than merely hoped for.
- `frontend/src/main.tsx` imports `@fontsource/press-start-2p/latin-400.css` — the latin
  subset alone (12 KB woff2), self-hosted and bundled by Vite. No remote font: no CSP
  change, no third-party fetch, works offline, and it survives into the production nginx
  image (verified).
- `frontend/src/FleetView.tsx` — the only JSX change, and the only place in the app where
  state was carried by colour alone. `StatusCell` rendered the same `●` for both states,
  green vs grey; on one hue ramp that is no signal at all. Now `█` for online and `░` for
  offline, so the pair differs on glyph, weight *and* lightness. `aria-label` untouched —
  a screen reader always heard the word, never the glyph.
- `frontend/index.html` — the inline SVG favicon was `#3fb950` green on `#0d1117`; now on
  the ramp, and its `rx="3"` corner radius dropped to match the hard edges.
- `frontend/scripts/theme-shots.mjs` — the T2 harness (see *Verification*).

**The audit that decided the scope.** Every `.ok`/`.bad`/`.warn`/`.muted` site across
`App.tsx`, `EnrollBoard.tsx`, `FlashBoard.tsx`, `FleetView.tsx`, `BoardConsole.tsx` and
`session.tsx` was checked for whether colour was its *only* carrier. Almost none were:
the token list renders the words "active"/"used", the stream status renders "Live", the
console fault and success render full sentences, the boot checklist already had `✓`/`…`/`·`
glyphs, and console log lines begin with ESP-IDF's own `E (…)`/`W (…)`/`I (…)` prefix. That
audit is why this restyle touched one component instead of six — and why `.log .bad`
explicitly cancels the underline `.bad` carries elsewhere: the level is already in the
text there, and underlining every error line would shred a monospace grid for nothing.

**Verification.**

T1: 108 frontend tests pass (the task said 71 — the suite grew with S0-fe-1), `tsc -b`
clean, `vite build` clean. The tests never load `index.css` (only `main.tsx` imports it,
and vitest does not render styles), so a CSS-only restyle *cannot* break them; the
`FleetView.tsx` glyph is the one change that could have, and no test asserts on it —
they assert on roles and labels, exactly as the task predicted.

T2, against `just up` with two live simulated boards and 25 offline ones, driven through
a real headless Chromium by `frontend/scripts/theme-shots.mjs`:

- *Every page renders in the theme* — login, fleet, flash (all four numbered steps,
  fieldsets, radio lists, disabled-button treatment), enroll, and the `.issued` panel with
  a real single-use token on screen. The harness asserts `document.fonts.check('12px
  "Press Start 2P"')` before shooting anything, so a silent fallback to the monospace
  stack cannot pass unnoticed.
- *Online/offline survives greyscale* — passes, and trivially, because the palette is
  already achromatic: the greyscale capture is pixel-identical to the colour one. The
  check was applied as a live `filter: grayscale(100%)` on the page rather than by
  converting the PNG afterwards, which is the stricter form — it would also have caught a
  coloured accent, an emoji or a hue-carrying form control.
- *Device ids and log output stay legible at default zoom* — shot at
  `deviceScaleFactor: 1` as well as 2. The 12-hex ids and a 70-character `ffe_` token read
  cleanly in the monospace face; the pixel font appears only in column headers, headings,
  legends and buttons.
- Production shape: `docker build --target production` on `frontend/` succeeds with the
  new dependency through `npm ci`, and the image serves
  `press-start-2p-latin-400-normal-*.woff2` from `/usr/share/nginx/html/assets/` — the
  font is genuinely self-hosted in prod, not a dev-server convenience.

**Decisions & gotchas:** see `DECISIONS.md` 2026-09-10 (S0-fe-2). The dev-loop footgun
worth repeating: after changing `frontend/package.json`, the `ff_node_modules` named
volume must be **deleted**, not rebuilt — Docker seeds a named volume from the image once,
at creation, so `just rebuild frontend` builds a correct image that the stale volume then
masks. It presents as Vite failing to resolve an import for a package that is plainly
installed. The recipe is now written into `docker-compose.override.yml` next to the
volume.

## In Progress

_Tracked in `TODO.md` (live status lives there, not here)._

## Planned Work

_Use `/new-feature` / `/new-task`; requirements land in `spec/`._
