# Dashboard — look, feel and cross-cutting UI

The operator-facing shell: theme, typography, color, accessibility, and anything else
that spans every page rather than belonging to one feature.

Per-page behavior lives with its feature — the enroll page, the flasher, the live device
list and the serial console are all in [enrollment.md](enrollment.md). This file is for
changes that cut across all of them.

Completed-work archive for this feature area (`docs/features/`). This holds the
plan **substance**, not links. `.claude/plans/*` are local and gitignored, so
their reasoning must live HERE (and decisions logged in `DECISIONS.md`).
When `/implement` finishes a task, it appends a completed entry below.

---

## Completed Work

<!-- Newest first. One entry per completed task. Capture what a future reader
     needs WITHOUT the local plan file. -->

### 2026-10-04 — R2b-fe-7 Upload a build from the dashboard

**What shipped:** An "Upload a build" section between the Fleet table and the flasher
(`UploadBuild.tsx`). File, chip target, version and partition layout; `POST /v1/artifact`
with the raw file as the body (`application/octet-stream`, metadata in the query string, no
credential anywhere). A finished upload calls the page's one `useArtifacts().reload`, so the
new version is in every matching row's Deploy select with no reload. The runbook
`docs/runbooks/upload-artifact.sh` is deleted; `rollback-test.md` now points at the form.

- **The header pre-fills, the server decides.** `appImage.ts` reads the first 256 bytes:
  byte 0 `0xE9`, chip id (u16 at 12), and the `esp_app_desc_t` version / project name at
  0x30 / 0x50. Target and version are pre-filled from it; a file built for another chip than
  the one selected is refused in the browser (`upload-target-mismatch`, button disabled). That
  is UI only: the server still treats the bytes as opaque. A merged image just reads as "no
  version"; refusing it is R2b-be-3.
- **Target is a select** (fleet chips + `esp32`/`esp32c3`/`esp32c6`/`esp32s3`), because a
  typo'd target is accepted by the server and then matches no board. **Layout is a select**
  defaulting to the one the chip's boards report (deploy compatibility is layout equality).
- **Server sentences render verbatim** (409, 413 carry the numbers and the next action). Only
  an empty or HTML body (a proxy page) is replaced, by status, in `uploadErrorMessage`.
- **Finding: prod nginx capped bodies at 1 MiB.** `frontend/nginx.conf` set no
  `client_max_body_size`, so any image over 1 MiB would have got nginx's own HTML 413 in
  prod; the old runbook only worked because the esp32s3 agent (998,672 B) fits. The Vite dev
  proxy hides this. Fixed with an exact-match `location = /v1/artifact` at 4m (the API stays
  the size authority at 1966080); `/v1/` keeps the default. Guarded by
  `tests/test_frontend_nginx.py`.
- **Not done:** drag-and-drop, upload progress (fetch has none), the dry-run pre-check
  (R2b-be-2 / R2b-fe-8).

**T2 evidence (dev stack, real Chromium; nginx path via the production image):**
- `agent/dist/esp32c6/app.bin` (1,106,384 B): line "app.bin — 1,106,384 bytes · built as
  fleetforge-agent 0.4.5 for esp32c6", target `esp32c6`, version `0.4.5` pre-filled. Version
  set to `0.4.5-t2-1791133925`: "Uploaded esp32c6 0.4.5-t2-1791133925 (1106384 bytes). It is
  in the Deploy list of every esp32c6 board." POST `/v1/artifact?target=esp32c6&version=…&partition_layout=ab-4m-v1`,
  `application/octet-stream`, 201. With no reload (`window.__ffT2` still 1) the esp32c6
  rows' Deploy selects offered and had selected the new version first.
- Same file and version again: "Already uploaded: these exact bytes are esp32c6 … Nothing
  changed." (200), no duplicate option.
- Version `1.5.0`, different bytes: the server's 409 sentence verbatim. 2,000,000 B file: "artifact
  is 2000000 bytes; an OTA slot in this partition layout is 1966080. …" (413). esp32s3 image
  with target esp32c6: `upload-target-mismatch`, Upload disabled, no request. 4 KB random
  file: "Not recognised as an ESP-IDF app image; the version and target were not read from it."
- Through nginx (`docker build --target production`, port 18080): login 200; 1,500,000 B
  upload 201 `created:true`; 2,000,000 B upload 413 with the API's JSON detail (not nginx
  HTML); `/v1/artifact/{sha}/bin` still answered by the API (403 `this download link is not
  valid`). `docker logs fleetforge-api` carried no password.

### 2026-09-10 — S0-fe-2 Monochrome pixel-art theme

**What shipped:** A whole-app restyle to a monochrome pixel-art theme — login, fleet,
flash and enroll, plus the issued-token panel, the boot checklist and both log panels.
One achromatic hue ramp, a self-hosted pixel show face, hard edges everywhere (no
border radii, 2 px borders, offset block shadows), and (because a single hue ramp
deletes color as a carrier of meaning) a second encoding axis on every state.

**Approach:** Four files, and the CSS is nearly all of it.

- `frontend/src/index.css` (128 → ~330 lines). The palette became seven CSS custom
  properties, all achromatic: `--ff-bg #0c0c0c`, `--ff-panel #141414`, `--ff-line
  #3a3a3a`, `--ff-line-hi #6a6a6a`, `--ff-dim #8a8a8a`, `--ff-fg #d8d8d8`, `--ff-hi
  #fff`. Contrast on the background is 5.6:1 for `--ff-dim` and 14:1 for `--ff-fg`. Thus,
  the dimmest text on the page still clears AA. Elements that previously had no rules at
  all (`button`, `input`, `select`, `a`, `h3`, `:focus-visible`, `progress`) got them.
  This is most of the line growth.
- **Two fonts, and the split is the whole readability story**. `--ff-display` is Press
  Start 2P and runs *only* for short chrome: `h1`, `h2`, `h3`, `th`, `legend`,
  `button`. `--ff-mono` (`ui-monospace` stack) carries everything else. The task's
  constraint was that a pixel font must not cost the legibility of 12-hex-digit device
  ids or the flash log, and confining the show face to chrome is how that is met
  rather than merely hoped for.
- `frontend/src/main.tsx` imports `@fontsource/press-start-2p/latin-400.css` — the latin
  subset alone (12 KB woff2), self-hosted and bundled by Vite. No remote font: no CSP
  change, no third-party get, works offline. It survives into the production nginx
  image (checked).
- `frontend/src/FleetView.tsx` — the only JSX change, and the only place in the app where
  state was carried by color alone. `StatusCell` rendered the same `●` for both states,
  green versus grey. On one hue ramp that is no signal at all. Now `█` for online and `░` for
  offline, so the pair differs on glyph, weight *and* lightness. `aria-label` untouched —
  a screen reader always heard the word, never the glyph.
- `frontend/index.html` — the inline SVG favicon was `#3fb950` green on `#0d1117`. Now on
  the ramp, and its `rx="3"` corner radius dropped to match the hard edges.
- `frontend/scripts/theme-shots.mjs` — the T2 harness (see *Verification*).

**The audit that decided the scope**. Every `.ok`/`.bad`/`.warn`/`.muted` site across
`App.tsx`, `EnrollBoard.tsx`, `FlashBoard.tsx`, `FleetView.tsx`, `BoardConsole.tsx` and
`session.tsx` passed checks for whether color was its *only* carrier. Almost none were:
the token list renders the words "active"/"used", the stream status renders "Live", the
console fault and success render full sentences, the boot checklist already had `✓`/`…`/`·`
glyphs, and console log lines start with ESP-IDF's own `E (…)`/`W (…)`/`I (…)` prefix. That
audit is why this restyle touched one component instead of six — and why `.log .bad`
explicitly cancels the underline `.bad` carries elsewhere. The level is already in the
text there, and underlining every error line would shred a monospace grid for nothing.

**Verification**.

T1: 108 frontend tests pass (the task said 71 — the suite grew with S0-fe-1), `tsc -b`
clean, `vite build` clean. The tests never load `index.css` (only `main.tsx` imports it,
and vitest does not render styles). Thus, a CSS-only restyle *cannot* break them. The
`FleetView.tsx` glyph is the one change that could have, and no test asserts on it —
they assert on roles and labels, exactly as the task predicted.

T2, against `just up` with two live simulated boards and 25 offline ones, driven through
a real headless Chromium by `frontend/scripts/theme-shots.mjs`:

- *Every page renders in the theme* — login, fleet, flash (all four numbered steps,
  fieldsets, radio lists, disabled-button treatment), enroll, and the `.issued` panel with
  a real single-use token on screen. The harness asserts `document.fonts.check('12px
  "Press Start 2P"')` before shooting anything, so a silent fallback to the monospace
  stack cannot pass unnoticed.
- *Online/offline survives greyscale* — passes, and trivially. This is because the palette is
  already achromatic. The greyscale capture is pixel-identical to the color one. The
  check applied as a live `filter: grayscale(100%)` on the page rather than by
  converting the PNG afterwards, which is the stricter form. It would also caught a
  colored accent, an emoji or a hue-carrying form control.
- *Device ids and log output stay legible at default zoom* — shot at
  `deviceScaleFactor: 1` as well as 2. The 12-hex ids and a 70-character `ffe_` token read
  cleanly in the monospace face. The pixel font appears only in column headers, headings,
  legends and buttons.
- Production shape: `docker build --target production` on `frontend/` succeeds with the
  new dependency through `npm ci`, and the image serves
  `press-start-2p-latin-400-normal-*.woff2` from `/usr/share/nginx/html/assets/`. The
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

_Use `/new-feature` / `/new-task`. Requirements land in `spec/`._

### Name a board (Priority: P2)
- **Problem:** `spec/flows.md` Flow 1 step 7 specifies naming, and nothing implements it.
  `GET /v1/devices` returns `name: null` for every row and `api/routers/devices.py` has
  exactly one route — a GET. There is no PATCH. Confirmed live 2026-09-23: all 41 rows in
  the dev fleet have `name: null`. So the fleet table is a list of 12-hex MACs. The
  operator has to remember which of `9e417ad42ca4` and `9eeda8084215` is the coop door.
  That gets worse exactly as the fleet grows toward the persona's 3–15 boards. It is
  the difference between deploying to the right board and deploying to a neighbor.
- **Scope note:** the column exists on `devices`. This is a PATCH route plus an editable
  cell. Tags and groups are deliberately **out** of scope. `DeviceGroup` already exists
  for that and conflating the two is how naming turns into a hierarchy feature.
- **Added:** 2026-09-23
