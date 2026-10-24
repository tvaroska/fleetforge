---
name: Fleetforge Monochrome Pixel
colors:
  surface: '#0c0c0c'
  surface-dim: '#0c0c0c'
  surface-bright: '#141414'
  surface-container-lowest: '#0c0c0c'
  surface-container-low: '#141414'
  surface-container: '#141414'
  surface-container-high: '#3a3a3a'
  surface-container-highest: '#6a6a6a'
  on-surface: '#d8d8d8'
  on-surface-variant: '#8a8a8a'
  inverse-surface: '#ffffff'
  inverse-on-surface: '#0c0c0c'
  outline: '#3a3a3a'
  outline-variant: '#6a6a6a'
  surface-tint: '#ffffff'
  primary: '#ffffff'
  on-primary: '#0c0c0c'
  primary-container: '#141414'
  on-primary-container: '#ffffff'
  inverse-primary: '#0c0c0c'
  secondary: '#d8d8d8'
  on-secondary: '#0c0c0c'
  secondary-container: '#141414'
  on-secondary-container: '#d8d8d8'
  tertiary: '#8a8a8a'
  on-tertiary: '#0c0c0c'
  tertiary-container: '#141414'
  on-tertiary-container: '#8a8a8a'
  error: '#ffffff'
  on-error: '#0c0c0c'
  error-container: '#141414'
  on-error-container: '#ffffff'
  background: '#0c0c0c'
  on-background: '#d8d8d8'
  surface-variant: '#141414'
typography:
  display-lg:
    fontFamily: Press Start 2P
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 25.6px
    letterSpacing: '0'
  headline-md:
    fontFamily: Press Start 2P
    fontSize: 12.8px
    fontWeight: '400'
    lineHeight: 20.5px
    letterSpacing: '0'
  headline-sm:
    fontFamily: Press Start 2P
    fontSize: 10.4px
    fontWeight: '400'
    lineHeight: 17.7px
    letterSpacing: '0'
  body-base:
    fontFamily: DejaVu Sans Mono
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
    letterSpacing: '0'
  body-bold:
    fontFamily: DejaVu Sans Mono
    fontSize: 16px
    fontWeight: '700'
    lineHeight: 24px
    letterSpacing: '0'
  label-caps:
    fontFamily: Press Start 2P
    fontSize: 8.8px
    fontWeight: '400'
    lineHeight: 15.8px
    letterSpacing: '0'
  code-log:
    fontFamily: DejaVu Sans Mono
    fontSize: 12.8px
    fontWeight: '400'
    lineHeight: 19.2px
    letterSpacing: '0'
rounded:
  sm: 0px
  DEFAULT: 0px
  md: 0px
  lg: 0px
  xl: 0px
  full: 0px
spacing:
  unit: 4px
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 40px
  gutter: 12px
  margin-mobile: 24px
  margin-desktop: 64px
---

# Design System: Fleetforge

## Brand & Style

Fleetforge is a self-hosted over-the-air (OTA) firmware management and Web Serial bench-flashing console for embedded hardware fleets (ESP32, Raspberry Pi, and field gateways). Its visual language is **Monochrome Pixel-Art Utilitarianism**—an uncompromising, high-contrast 1-bit/greyscale aesthetic built for hardware engineers working across serial terminals, bootloaders, and live telemetry feeds. Every ornament that could obscure a 12-hex-digit MAC address, misrepresent download progress, or hide a reboot loop is stripped away in favor of verbatim server truth and tactile retro-industrial precision.

Two load-bearing rules govern the entire interface. First, the palette uses a **single achromatic hue ramp** (`r == g == b` on every token), which means semantic state is never carried by color alone: statuses encode across weight, underline, and explicit ASCII/Unicode block glyphs (`█` vs `░`, `✓`, `…`, `↺`, `✗`) to guarantee WCAG 1.4.1 compliance even in greyscale screenshots. Second, typography is strictly split between an 8-bit bitmap display face (**Press Start 2P**) for short structural chrome and a crisp technical monospace (**DejaVu Sans Mono** / system `ui-monospace`) for device IDs, SHA-256 fingerprints, and serial logs.

## Colors

Every color in Fleetforge belongs to a single seven-step achromatic ramp anchored against near-black carbon surfaces. All foreground steps meet or exceed WCAG AA contrast against the root canvas (`5.6:1` for muted ash, `14:1` for primary body silver, and `19:1` for pure signal white).

### Primary Foundation
- **Carbon Void (`#0c0c0c` / `--ff-bg`)** — Root page canvas (`body` and `main` background), creating a dark, zero-glare terminal environment.
- **Obsidian Panel (`#141414` / `--ff-panel`)** — Elevated container surface for sticky status strips, text/select inputs, buttons, stepped progress bars, and scrollable serial log consoles.
- **Graphite Rule (`#3a3a3a` / `--ff-line`)** — Structural 2px borders on tables, fieldsets, form inputs, log boxes, and the hard-edged offset drop shadows behind buttons and callout cards.
- **Steel Highlight Rule (`#6a6a6a` / `--ff-line-hi`)** — High-emphasis 2px borders for interactive buttons, table header baselines, radio/checkbox accents, and active callout cards (`.issued`, `.precheck`, `.deploy-result`).

### Typography & Semantic State Hierarchy
- **Pure Signal White (`#ffffff` / `--ff-hi`)** — Display headings (`h1`, `h2`), button labels, links, focus-visible outlines, and high-priority states: `.ok` (`#ffffff` + bold `700`) and `.bad` (`#ffffff` + bold `700` + `3px` offset underline).
- **Phosphor Silver (`#d8d8d8` / `--ff-fg`)** — Default body text, table cells, form input values, stepped progress bar fill, and `.warn` / `.waiting` / `.current` states (rendered at bold `700`).
- **Muted Ash (`#8a8a8a` / `--ff-dim`)** — Secondary explanatory notes (`.muted`), table column headers (`th`), fieldset legends, definition list keys (`dt`), disabled button labels, and offline/skipped/pending indicators.

## Typography

Fleetforge enforces a strict **two-font architecture** where the distinction between chrome and data is functional:

1. **Display Chrome (`--ff-display`: `'Press Start 2P', ui-monospace, monospace`)** — An unmistakable 8-bit bitmap pixel font with a wide horizontal advance and no descender room. Because it is 2.5x wider than standard monospace and lacks bold/lowercase contrast, it is restricted exclusively to short structural chrome:
   - **Page Title (`h1`)**: `1rem` (`16px`), line-height `1.6`, weight `400`, Pure Signal White (`#ffffff`), bottom margin `1.5rem`.
   - **Section Heading (`h2`)**: `0.8rem` (`12.8px`), line-height `1.6`, weight `400`, Pure Signal White (`#ffffff`), top margin `2.5rem`, bottom margin `1rem`.
   - **Step & Card Heading (`h3`)**: `0.65rem` (`10.4px`), line-height `1.7`, weight `400`, Phosphor Silver (`#d8d8d8`), used for numbered flasher steps (`1 · The board` through `4 · Watch the board`) and inline card titles.
   - **Table Headers, Fieldset Legends & Buttons (`th`, `legend`, `button`)**: `0.55rem` (`8.8px`), line-height `1.8`, weight `400`.

2. **Operator Data & Logs (`--ff-mono`: `ui-monospace, 'DejaVu Sans Mono', Menlo, Consolas, monospace`)** — Used for all prose, tabular telemetry, form controls, 12-hex-digit device IDs, and ESP-IDF serial output:
   - **Body & Table Cells**: `1rem` (`16px`) with `font-variant-numeric: tabular-nums` on tables, `<dd>` values, and the sticky status strip so ticking relative ages (`(4 s ago)`) never jitter horizontally.
   - **Form Controls & Boot Checklists (`input`, `select`, `.milestones`)**: `0.9rem` (`14.4px`).
   - **Inline Identifiers (`code`)**: `0.85em` with `word-break: break-all` so 12-character hex device IDs and SHA-256 partition fingerprints wrap cleanly without forcing horizontal table scroll.
   - **Serial & Esptool Log Consoles (`.log`, `.console`)**: `0.8rem` (`12.8px`), line-height `1.5`, `white-space: pre-wrap`, `word-break: break-all`. Inside `.log`, `.bad` suppresses its underline so error lines (`E (...)`) do not shred the monospace grid.

## Layout & Spacing

The application uses a **Single-Column Document Flow** inside a centered `<main>` container (`max-width: 68rem` / `1088px`, `margin: 0 auto`, `padding: 2.5rem 1.5rem 4rem`). This width gives the 7-column Fleet Table (`Status`, `Device`, `Platform`, `Firmware`, `Last seen`, `Power`, `Deploy`) and its `38%`-wide (`min-width: 22rem`) `Deploy` column ample room to render inline pre-check and update-result cards without crowding columns 1–6.

- **Header & Status Bar Alignment**: The sticky `.status-strip` (`display: flex; justify-content: space-between; padding: 0.55rem 0.85rem; border: 2px solid #3a3a3a; box-shadow: 3px 3px 0 #3a3a3a`) pins `[ UI · API ]` on the left and the active board summary on the right. Below it, `<header>` aligns `<h1>Fleetforge</h1>` and the signed-in operator / `Sign out` control on a single horizontal baseline separated by a `2px solid #3a3a3a` rule.
- **2-Column Aligned Form Grids**: Form labels wrapping text, number, password, file, or select controls (`label:not(.choice):has(> input, > select)`) use `display: inline-grid; grid-template-columns: 11rem minmax(14rem, max-content); column-gap: 0.75rem; color: #8a8a8a`, matching the `<dl>` (`<dt>` / `<dd>`) rhythm so stacked form controls share a clean vertical left edge. Inside `.network-row`, `SSID` and `Passphrase` use compact `5.5rem 13rem` grid columns side-by-side.
- **Vertical Rhythm**: Major operational sections (`Fleet`, `Partition profiles`, `Upload a build`, `Flash a board`, `Enroll a board`) are separated by `2.5rem` (`40px`) top margins on `<h2>` headings, while sub-steps use `1.75rem` (`28px`) above `<h3>` headings and `1.5rem` (`24px`) around `<fieldset>` and `.issued` blocks.
- **Definition Lists (`dl`)**: Two-column CSS Grid (`grid-template-columns: max-content 1fr`, `gap: 0.5rem 1.5rem`) used for hardware chip inspection (`Chip`, `MAC`, `Flash`, `Features`, `Device ID`) and update result metadata.
- **Tables**: Full-width (`width: 100%`, `border-collapse: collapse`) with left-aligned cells, `0.6rem 0.85rem 0.6rem 0.4rem` cell padding, `2px solid #3a3a3a` row borders, and `2px solid #6a6a6a` header borders. Selected fleet rows (`tbody tr[data-selected='true']`) receive an elevated `#141414` surface and a `3px` solid `#ffffff` inset left bar on the first cell alongside the `▶` glyph.

## Elevation & Depth

Fleetforge rejects blur, gradients, and glassmorphism in favor of **Hard-Edged Pixel Offset Shadows** and crisp `2px` borders:

1. **Base Canvas (`#0c0c0c`)**: Flat dark background hosting tables and prose directly.
2. **Recessed Panels & Consoles (`#141414` + `2px solid #3a3a3a`)**: Used for `<input>`, `<select>`, `<progress>`, `.log` consoles, and `<fieldset>` groups.
3. **Interactive Tactile Controls (`#141414` + `2px solid #6a6a6a` + `3px 3px 0 #3a3a3a`)**: Buttons cast a rigid `3px` diagonal block shadow. On `:active`, the button translates `translate(3px, 3px)` and drops its shadow to `none`, physically pressing into the surface.
4. **High-Priority Callout Cards (`.issued` with `2px solid #6a6a6a` + `4px 4px 0 #3a3a3a`)**: Newly minted single-use enrollment tokens and onboarding result cards stand off the page with a `4px` hard block shadow.
5. **Sticky Status Strip (`position: sticky; top: 0; z-index: 1`)**: Pinned across the top of the viewport with `#141414` background and a `2px solid #3a3a3a` bottom border.

## Shapes

- **Strict Zero-Radius Geometry (`border-radius: 0`)**: Every element—buttons, inputs, selects, fieldsets, progress bars, log boxes, and callout cards—uses sharp 90-degree corners (`0px` radius).
- **2px Structural Hairlines**: All borders and focus outlines are `2px solid` (`outline: 2px solid #ffffff; outline-offset: 2px` on `:focus-visible`).
- **Stepped Flat Progress Bars**: `<progress>` elements (`height: 0.9rem`, `border: 2px solid #3a3a3a`, `background: #141414`) use a flat `#d8d8d8` value bar (strictly reserved for USB `esptool` byte writes; network OTA deploys never render a progress bar).

## Components

### Buttons & Board Picker Links
- **Standard Action Button (`button`)**: `Press Start 2P` at `0.55rem`, `#ffffff` text on `#141414` background, `2px solid #6a6a6a` border, `0.5rem 0.8rem` padding, and `3px 3px 0 #3a3a3a` shadow. Hover fills the background with `#3a3a3a`; active translates `(3px, 3px)` with no shadow; disabled dims text to `#8a8a8a`, border to `#3a3a3a`, and removes the shadow.
- **Inline Board Picker (`.pick-board`)**: Resets button chrome back to `--ff-mono` with `text-decoration: underline dotted`, `text-underline-offset: 3px`, and no border or shadow so clicking a device name/ID in the Fleet Table selects it into the Sticky Status Strip while still reading as data.
- **Remedy Button (`.remedy`)**: Nested inside `.bad` or `.warn` alert paragraphs with `display: inline-block; margin-top: 0.5rem; text-decoration: none` so parent error underlines do not bleed into the recovery button label.

### Sticky Status Strip (`.status-strip`)
- Single-line persistent top banner rendered in `--ff-mono` with `tabular-nums`.
- Left segment shows `[ UI <version> · API <version> ]` and flags a `.warn` banner if UI and API versions drift.
- Right segment (`BoardHalf`) shows the currently selected or single enrolled board (`Board <name> (<device_id>) · <platform> · <firmware> · <state>`).

### Fleet Table & Inline Deploy Cell (`FleetTable`, `DeployCell`, `PrecheckCard`, `DeployResultCard`, `DeployTimeline`)
- **Status Cell**: Encodes online/offline state with both a block glyph and weight (`█ online` in bold `#ffffff` vs `░ offline` in `#8a8a8a`), followed by the board's current or `last on` Wi-Fi SSID and count of known networks.
- **Arriving List**: Displayed above the table when flashed boards have reported early boot stages (`boot`, `wifi`, `enroll`) before completing fleet registration, flagging stalled arrivals with `<strong class="bad">stalled</strong>`.
- **Deploy Cell**: Pairs a chip-filtered version `<select>` with a `Deploy` button. Clicking `Deploy` runs a dry-run pre-check and opens an inline `.precheck` card (`Before you send`) showing slot fit, rollback window, non-overridable `Refused:` items, or gating `Warning:` checkboxes (`Send anyway despite this`).
- **Update Timeline & Result Card**: In-flight OTA transactions render an ordered `.milestones.deploy-timeline` list with status glyphs (`✓` done, `–` implied, `…` current with elapsed seconds, `·` pending, `✗` failed) and board-side stall deadlines. Terminal transactions collapse the timeline into a `<details>` summary (`took N s`) and render a `.deploy-result` card (`Update result`) with verdict (`good` or `rolled back`), next action, and a `Send again` button when applicable.

### Inputs, Fieldsets & Multi-Network Forms
- **Text, Number, Password & Select Controls**: `--ff-mono` at `0.9rem`, `#d8d8d8` text on `#141414` background, `2px solid #3a3a3a` border, `0.35rem 0.5rem` padding.
- **Fieldsets & Radio Choices**: `2px solid #3a3a3a` frame with `Press Start 2P` `0.55rem` `<legend>` in `#8a8a8a`. Radio and checkbox labels use `.choice` (`display: block; padding: 0.2rem 0`) with `accent-color: #6a6a6a`.
- **Password-Manager Network Rows (`.network-row`)**: Each Wi-Fi network (up to 4) is wrapped in its own `<form>` and `<fieldset>` (`Network 1` … `Network 4`) pairing `SSID` (`autoComplete="username"`) with `Passphrase` (`autoComplete="current-password"`).

### Web Serial Flasher & Board Console (`FlashBoard`, `BoardConsolePanel`, `ResultCard`)
- Structured into four numbered steps (`1 · The board`, `2 · What to bake in`, `3 · Flash`, `4 · Watch the board`).
- **Pre-flight & Onboarding Result Cards (`PreflightCard`, `ResultCard`)**: Before flashing, warns if a board is already enrolled on the fleet or if its partition layout will change. After flashing or watching over USB serial (115200 baud), `.milestones` tracks boot progression (`✓` / `…` / `↺` / `·`) merged with the server's live view, culminating in an `.issued` `ResultCard` (with inline `NameBoard` form on success, or one-click `Re-flash` / `Reboot` remedy and secret-redacted `Copy diagnostic bundle` `<textarea class="log console">` on failure).

### Multi-Use-Case & Cohort Scale Patterns (20–80+ MCUs per Use-Case)
- **2-Axis Navigation (`ProjectBar` + 4-Tab Console)**: Persistent top `.project-bar` scopes the entire workspace (fleet, artifact catalog, tokens, SSE stream) to the active Use-Case (`projects`), paired with 4 mode tabs (`1. FLEET`, `2. BUILDS`, `3. ADD BOARD`, `4. ACCESS`) toggled via `.tab-panel:not(.active) { display: none; }` so the single `useFleet` SSE connection and active Web Serial ports remain mounted across tab switches.
- **Exception-First Table Density**: For cohorts of 20–80+ boards, steady-state `good` rows running the confirmed target version collapse their `.deploy-result` card to a single-line inline badge (`1.5.0 ✓`), while full `.deploy-result` diagnostic cards auto-expand only for active (`…`/`↺`), `rolled_back` (`✗`), or `failed` (`✗`) rows.
- **Exception Sorting & Filter Pills**: Default row order pins `rolled_back`/`failed` first, `updating` second, `version drift` third, and healthy `on-target` rows last, paired with filter pills (`All`, `Attention`, `Updating`, `Drift`, `Offline`) and a cohort convergence bar (`█`/`░`).
