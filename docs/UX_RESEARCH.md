# Fleetforge UI/UX Research, Multi-Persona Evaluation & Architectural Recommendation (`docs/UX_RESEARCH.md`)

**Status:** Canonical UX Research & Implementation Specification (12/12 Evaluator Sign-Off)  
**Baseline Audited:** Fleetforge `v0.7.2` (`R0`–`R9` Backend/Firmware vs. React + TypeScript Frontend in `frontend/src/`)  
**Research Methodology:** 2-Round Multi-Agent Swarm Study — Current UI Audit (`ux-auditor`) + Industrial Fleet UX Best Practices (`ux-best-practices`) + 6 Architectural Hypotheses (`H1`–`H4` in Round 1; `H5`–`H6` in Round 2 by `design-architect`) + 12 Independent Persona Evaluators (6 Canonical Personas $\times$ 2 Sub-Profiles) + Cross-Persona Synthesis (`ux-synthesizer`).

---

## 1. Executive Summary & Recommended Architecture Verdict

### 1.1 Context & Problem Statement
Fleetforge's backend (`src/fleetforge/`) and ESP-IDF / Arduino firmware agent (`agent/`) have matured through releases `R0`–`R9` into a rich embedded lifecycle platform supporting A/B OTA with automatic self-test rollback (`R5`), safe-window hold & multi-component companion cargo (`sub_versions`), rotating NVS secrets (`ff_sec` / `secrets_rev`), OTLP/HTTP log and metric forwarding (`R8`), multi-project RBAC (`owner` / `operator` / `viewer`) with scoped `ffa_...` tokens (`R9`), and offline venue radio lockout (`ff_set_offline_mode(true)` / `RULE E301`) with tethered USB-C / pogo-pin dock synchronization (`POST /v1/devices/{id}/dock-sync`).

However, a comprehensive code-level audit of `frontend/src/` and multi-persona evaluation across **12 distinct operator profiles** revealed that the current `v0.7.2` web UI scores a grand mean of **`4.08 / 10`** (ranging from `2.40 / 10` for contested-RF tournament pit crews to `5.80 / 10` for PlatformIO makers). Three systemic classes of UX failure hold the platform back:
1. **Unbounded Vertical Layout Shift & Scale Breakdown on `1. FLEET`:** Expanding inline per-row action drawers (`Deploy | Config` in `FleetView.tsx`) adds `180–280 px` per row while 1 Hz SSE ticks (`useFleet.ts`) re-sort rows dynamically under the operator's cursor (`Fitts's Law` violation). Furthermore, `DeviceLog.tsx` is rendered at the bottom of the entire page below the table, forcing `2,400 px` of vertical scrolling on 60-board HIL racks, while `LIST_LIMIT = 200` (`devices.py:136`) silently truncates 250-board commercial fleets.
2. **Backend-to-Frontend Capability Chasm (11 Dark or Incomplete Backend Features):** Critical backend APIs built in `R5`–`R9` have **zero UI surface** or broken wiring in `frontend/src/api.ts`, including `POST /v1/devices/{id}/dock-sync` (tethered USB-C dock staging & ring drain), `GET /v1/devices/{id}/telemetry` (time-series sparklines), `POST /v1/devices/{id}/rollback` (manual A/B slot revert), `safe_mode: true` (`R5-fw-6` 3-crash latch), clickable `build_url` / `repo_url` CI links, `ContinuousDeploymentCard` `skipped` reasons & webhook HTTP failure alerts, `ffa_...` token `expires_in_days` / `enroll` scope / `last_used_at` audit columns, and ECDSA P-256 2-key rotation ring (`[K1_pub, K2_pub]`) visibility.
3. **Unresolved Inter-Persona Tensions Across Operating Regimes:** A solo Arduino maker with 5 heterogeneous sketches (`Alex`) suffers false project-wide `≠ Version Drift` alarms and tab ping-pong when forced through enterprise cohort matrices; a 60-board HIL engineer (`Sarah`) cannot scan `deploy.selftest.reason` + `returned_to` slot or compare 4 rolled-back boards side-by-side; a drone swarm commander (`Elena`) lacks `Hold at staged (on_command)` + `Leapfrog (max 1/parent)` cohort controls and `◎ SORTIE` vs. `!! OVERDUE` presence states; and an FRC competition pit lead (`Chloe`) has zero browser UX for 90-second USB-C dock turnarounds under `RULE E301` (`802.11` Wi-Fi ban).

### 1.2 Summary of Quantitative Progression Across Rounds

| Design / Evaluation Stage | Grand Mean (`/ 10`) | Min Evaluator | Max Evaluator | Evaluators `< 7.0` | Key Takeaway |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **Current Baseline (`v0.7.2`)** | **`4.08 / 10`** | `2.40` (`chloe-2`) | `5.80` (`alex-2`) | `12 / 12` | Severe vertical scroll fatigue, row-jump jitter, and 11 missing backend/firmware surfaces. |
| **Round 1 `H1` (Evolutionary Accordion Table)** | `7.10 / 10` | `6.00` (`sarah-2`) | `7.90` (`alex-2`) | `4 / 12` | Fixes surface bugs, but inline accordion rows still jump `180–240 px` during wave rollouts. |
| **Round 1 `H3` (Release Pipeline & Ring Swimlanes)** | `7.40 / 10` | `5.30` (`alex-1`) | `8.80` (`marcus-1`) | `3 / 12` | Beloved by OEM/CI leads (`marcus-1`, `siddharth-1..2`), but swimlanes break 60-board heap sorting and overwhelm solo makers. |
| **Round 1 `H4` (Adaptive 4-Mode Console)** | `7.90 / 10` | `6.20` (`sarah-2`) | `9.66` (`chloe-1`) | `2 / 12` | 90s Pit Cockpit (`Mode 3`) & 1-Screen Bench (`Mode 1`) delight `chloe-1..2` & `alex-1`, but cause "Mode Blindness" for multi-board operators. |
| **Round 1 `H2` (Split-Pane Operator Workbench)** | `8.29 / 10` | `7.60` (`siddharth-1`) | `9.00` (`alex-2`) | **`0 / 12`** | **Round 1 Winner:** Fixed `40px` Master Grid (`0 px` shift) + Pinned Right Inspector scales from 5 to 250 boards with zero rejections. |
| **Round 2 `H6` (3-Tab + Slide-Down Matrix & Full-Width Pit)** | **`9.73 / 10`** | **`9.54`** (`elena-2`, `marcus-2`) | **`9.98`** (`chloe-1`) | **`0 / 12`** | **5 Primary Votes:** Slide-Down `Artifact × Ring × ff_sec` Matrix Drawer & Full-Width Pit Cockpit + Mini-Fleet Strip win `chloe-1..2`, `sarah-1`, `elena-1`, `alex-1`. |
| **Round 2 `H5` (4-Tab Unified Split-Pane Workbench)** | **`9.77 / 10`** | **`9.50`** (`elena-1`) | **`9.96`** (`alex-2`) | **`0 / 12`** | **7 Primary Votes:** Clean 4-Tab RBAC separation (`Tab 2 CI` vs `Tab 4 Security`) + unbroken 58%/42% Split-Pane spine win `marcus-1..2`, `siddharth-1..2`, `alex-2`, `elena-2`, `sarah-2`. |
| **Final Recommended Architecture (`H5` Spine + 3 `H6` Enhancements)** | **`9.88 / 10`** | **`9.70`** (`elena-1`, `marcus-2`) | **`9.98`** (`chloe-1`) | **`0 / 12`** | **12 / 12 Unanimous Sign-Off (`+5.80` over Baseline).** |

### 1.3 Recommended Architecture Verdict (The Converged `H5 + H6` Specification)
All 12 evaluators signed off on adopting **`H5` ("4-Tab Unified Adaptive Split-Pane Workbench")** as the primary structural spine, augmented with **three surgical enhancements from `H6`**:
1. **4-Tab Role-Aligned Top Navigation (`H5`) + Multi-Project Health & Quick-Toggle Bar (`H5` + `H6`):**
   - `1. FLEET WORKBENCH` · `2. RELEASES, CI & CARGO MATRIX` · `3. ADD BOARD & DOCK` · `4. SECURITY, SECRETS & GOVERNANCE`
   - Preserves a crisp security boundary between daily `operator` release promotion (`Tab 2`) and `owner`/`superadmin`-only security governance (`Tab 4`), while adding per-project health pills (`[agri-oem (! 1 ✗)] [valley-ag (✓ 40)] [flux-hub (⊘ EMCON)]`), `[✓ Pit Cache Ready]`, and 1-click `[32px Rack]` + `[☀ Pit Contrast]` (`21:1`) toggles in `.project-bar`.
2. **3-Tier Progressive Release & Convergence Ribbon (`H5`) with On-Demand Slide-Down `Artifact × Ring × ff_sec` Matrix Drawer (`H6` Enhancement #1):**
   - Auto-collapses to a **1-Line Maker Summary Bar** scoped by `appImage.projectName` when `groups.length === 0` (zero enterprise ring clutter or false sketch drift for `Alex-1`).
   - Expands to the **4-Stage Release Pipeline & Circuit-Breaker Rail** (`0. Dev -> 1. Canary -> 2. Beta -> 3. Prod`, `DeployKpiStrip` with clickable `[Rollback saves: 4 ▸]`, `Companion Cargo sub_versions`, and clickable `Secrets: sec:r4 ([! 3 on r3 ▸])`) for multi-ring fleets, with an on-demand **`[▼ Expand Full Release & Cargo Matrix]`** slide-down drawer (`H6`) so operators can audit cross-ring `app`, `sub_versions`, and `ff_sec` convergence without leaving Tab 1.
3. **58% Fixed-Height (`40px` / `32px`) Zero-Jitter Master Fleet Grid + 42% Unified Single-View Right Inspector (`H5` + `H6` Core):**
   - **Left Master Grid (`58%`):** Guarantees **0 px vertical layout shift** on 1 Hz SSE ticks (`[x] Freeze sort on hover`), deterministic 7-tier exception priority sorting (`Score -1: !! SAFE MODE` $\to$ `Score 0: ✗ rolled_back / !! OVERDUE`), 6-state non-color `StatusCell` (`█ WIFI`, `█ DOCK`, `◐ ASLEEP ~12m`, `◎ SORTIE ETA 11m`, `⊘ VENUE EMCON`, `!! OVERDUE`), always-visible sortable `free/min` heap (`148K/110K ✓`, `!wdt 62K/14K!`), and **verbatim single-line `DEPLOY / VERDICT` exception cells** (`✗ selftest: i2c 0x48 timeout → returned_to ota_1`).
   - **Right Contextual Inspector (`42%`):** Eliminates all sub-tab ping-pong. Mode **`[A. Triage & Bench]`** unifies an embedded `.bin`/cargo Drag-and-Drop uploader, `PrecheckCard`, `DeployTimeline`, `DeployResultCard` (`returned_to`), `[↶ Rollback to ota_0]`, bounded lease `[⚡ stay awake: 28m left ▾]`, Vitals + 1-bit `TelemetrySeries` sparklines (`▇▇▆▅▄▃▂▁`), and server-filtered `DeviceLog` (`?prefix=[prev-boot]` + pinned 4-line `RTC_NOINIT_ATTR` crash box) on **one continuous vertical screen**. Mode **`[C. Compare / Diff (N)]`** stacks 2–6 failed/soak boards side-by-side.
4. **Dual-Access `RULE E301` USB-C / Carrier Dock (`H5` In-Inspector `[B. 🔌 Dock]` + `H6` Enhancement #2 Full-Width Tab 3C Pit Cockpit with Pinned Mini-Fleet Strip):**
   - Offers both an in-context `[B. 🔌 Dock]` mode inside the 42% Right Inspector on Tab 1 (keeping 21–60 swarm/rack rows visible on the left for `Elena`, `Sarah`, and `Marcus`, while auto-hiding the pill when no board uses `link_type: "dock"` or `offline_mode` for `Alex-1`) **and** a `[⤢ Full-Screen Pit Cockpit]` in Tab 3C (`PitDockCockpit.tsx`) topped by `H6`'s **Pinned Live Mini-Fleet & Peer Mesh Strip** for 90-second competition turnarounds (`Chloe`).

---

## 2. Current Application Flow Audit & Identified UX Gaps (`v0.7.2` Baseline)

```
┌────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ CURRENT BASELINE ARCHITECTURE (v0.7.2) — KEY STRUCTURAL BOTTLENECKS                                            │
├────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│  .project-bar: <select> (no cross-project health) │ Role hidden │ No Pit Cache │ No 32px or Sunlight toggles   │
│  Tabs: [1. FLEET (N)]  [2. BUILDS (N / ! K MAP)]  [3. ADD A BOARD]  [4. ACCESS]                                │
├────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│  TAB 1 (FleetView.tsx):                                                                                        │
│  ┌─ DeployKpiStrip (hidden inside collapsible "Deploy firmware" drawer when closed!) ────────────────────────┐ │
│  ├─ GroupFilterBar (mixes filtering + group creation + delete; no search, no status/drift/RSSI/RMA filters) ─┤ │
│  ├─ Single Full-Width HTML <table> (capped at LIST_LIMIT = 200; re-sorts live under cursor on 1 Hz SSE)     ─┤ │
│  │   Row 1..N: Clicking `[Deploy ▾]` or `[Config ▾]` injects a 180–280px inline <tr class="row-detail">      │ │
│  │             that pushes all lower rows down the viewport!                                                 │ │
│  │   Columns: `DEPLOY` truncates selftest reason & hides `returned_to`; `HEALTH` hides `min_free_heap` on    │ │
│  │            healthy rows and ignores `safe_mode: true` (`R5-fw-6`).                                        │ │
│  └─ DeviceLog.tsx: Rendered at the VERY BOTTOM of the page below all 60–200 rows (~2,400px scroll distance!) │ │
│                    Slices client-side to 200 lines (`log.slice(-200)`), evicting `[prev-boot]` crash lines!  │ │
└────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Tab-by-Tab Structural & Ergonomic Audit

#### A. Global Shell & Top Bar (`App.tsx`, `ProjectAccess.tsx`, `index.css`)
- **Theme Drift vs. `.stitch/DESIGN.md`:** While `.stitch/DESIGN.md` mandates a strict 7-step monochrome pixel palette (`#0c0c0c` to `#ffffff`, `Press Start 2P` + `DejaVu Sans Mono`, zero chromatic hues), `frontend/src/index.css` (`L16–47`) drifted to warm CRT amber (`#ffb000`), phosphor green (`#33ff66`), and coral red (`#ff4444`) with `1px` borders.
- **Blind Multi-Project Switcher:** `.project-bar` renders a plain `<select>` with no health, rollback, or EMCON indicators for the user's other projects, and hides the user's active RBAC role (`owner` / `operator` / `viewer`).
- **Missing `groups` Prop on `3. ADD A BOARD`:** `App.tsx` (`L202–228`) passes `groups={groups}` to `FleetView` and `UploadedBuilds`, but **omits `groups` on `FlashBoard` and `EnrollBoard`**, making it impossible to assign a newly flashed board to a release ring (`canary`, `beta`, `prod`) during onboarding (`spec/flows.md` Flow 1 gap).

#### B. Tab 1 — `1. FLEET` (`FleetView.tsx`, `DeployKpiStrip.tsx`, `DeviceLog.tsx`)
- **Inline Accordion Vertical Jitter:** Opening `Deploy` or `Config` on 3 rows during a wave rollout injects `600+ px` of inline `<tr className="row-detail">` drawers while 1 Hz SSE frames update progress bars and re-sort rows (`deviceAttentionRank`).
- **Disconnected Bottom-of-Page Log Panel:** Clicking `[Logs]` on row 2 sets `selected` and renders `<DeviceLog>` **below the entire `<table>`** (`App.tsx:207`). An operator debugging a board at the top of a 60-board rack must scroll `2,400 px` down to read its logs and `2,400 px` back up to click `Reboot` or `Deploy`.
- **Client-Side 200-Line Log Truncation Evicts `[prev-boot]` Crashes:** `DeviceLog.tsx` (`L115`) calls `api.deviceLogs(deviceId, 200)` without server-side `?prefix=` or `?level=` parameters. A chatty post-rollback boot (`> 200` `I`-level lines) pushes the 4 `RTC_NOINIT_ATTR` `[prev-boot]` crash lines out of the 200-line slice even though Postgres retains 5,000 lines (`devices.py:258`).
- **Hidden `DeployKpiStrip`:** The North-Star KPI strip (`Delivery >= 90%`, `Fleet safety = 100%`, `Rollback saves: N`) is nested inside the collapsible `"Deploy firmware"` `<details>` drawer (`FleetView.tsx:266`)—invisible when the drawer is collapsed.
- **False Companion Cargo Drift Bug (`deploy.ts:153`):** `deployOutcome()` checks `device.fw_version === d.artifact_version` for **all** artifacts. Deploying a non-`app` companion cargo (`data_partition`, `mcu_fw`, `model_weights`, `fpga_bitstream`) updates `device.sub_versions[key]` while leaving `fw_version` unchanged, causing the UI to falsely display `drift` (`Confirmed X, now running Y`) on every cargo update!
- **False Maker Sketch Drift:** Convergence calculation (`FleetView.tsx:161–171`) compares every board in a project against a single `latestVersion`, flagging 4 out of 5 boards in a heterogeneous maker project (`coop_door`, `garden_soil`, `porch_led`) as `≠ Drift`.

#### C. Tab 2 — `2. BUILDS` (`UploadBuild.tsx`, `UploadedBuilds.tsx`, `ContinuousDeploymentCard.tsx`, `PartitionProfiles.tsx`)
- **45-Pixel Scrollable `<select>` in `UploadBuild`:** `UploadBuild.tsx` (`L290`) sets `size={Math.min(artifacts.length, 4)}` on the version baseline dropdown, rendering a cramped 45px listbox that breaks keyboard tab flow.
- **10-Target CI Row Flood & Unlinked Provenance:** Pushing 1 Git commit across 10 chip targets creates 10 separate rows in `UploadedBuilds`. Worse, `a.build_url` and `a.repo_url` are fetched in `api.ts` (`L97–98`) but **never rendered as clickable `<a>` links**.
- **Silent CD Gate & Webhook Failures:** `ContinuousDeploymentCard.tsx` (`L128`) only prints `skipped ${Object.keys(res.skipped).length}` while discarding the actual per-device skip reasons (`no_matching_target`, `partition_layout_mismatch`), and silently ignores `ci_webhook_triggered === false` when a GitHub Actions / Cloud Build webhook returns HTTP `401`/`500`.
- **SSE Blind Spot on CI Uploads:** `useFleet.ts` reloads `devices` on SSE messages but never calls `artifacts.reload()`, so CI-uploaded builds do not appear until a manual browser refresh.

#### D. Tab 3 — `3. ADD A BOARD` (`FlashBoard.tsx`, `EnrollBoard.tsx`, `PreflightCard.tsx`, `BoardConsole.tsx`)
- **Vertical Scrolling During Live Flash:** `BoardConsole` is stacked vertically below the 3 setup steps (`FlashBoard.tsx:644`), forcing the user to scroll up and down between config inputs and the serial boot log.
- **Batch Assembly-Line Friction:** Even with `keepSettings` checked (`FlashBoard.tsx:311`), `setName('')` clears the board name after every flash instead of auto-incrementing a prefix (`gh-node-01..40`), and there is no Batch CLI Token Generator (`N=40` tokens + CSV/script download) for headless factory `esptool.py` jigs.
- **Cross-Project RMA `409` Trap:** `PreflightCard.tsx` checks if a board's MAC is enrolled in the *current* project, but if a returned RMA unit is still enrolled in *another* project, flashing succeeds and the board then fails `POST /v1/enroll` with HTTP `409 Conflict` at boot Step 4.

#### E. Tab 4 — `4. ACCESS` (`ProjectAccess.tsx`)
- **Incomplete `ffa_...` Token Lifecycle:** `ProjectAccess.tsx` (`L296`) omits `expires_in_days` (all UI-minted tokens are immortal), omits the `enroll` scope checkbox (`VALID_SCOPES` in `auth.py:42`), allows non-superadmins to uncheck `"Pin to project"` (triggering an unhandled `403`), has no `[Copy]` button on minted tokens, and hides `last_used_at` and `expires_at` in the token table.
- **Blind `ff_sec` Secret Rotation & Unverifiable OTLP:** `ProjectAccess.tsx` shows how many secrets exist (`rev 4`), but does not list *which* sleepy/offline boards are still on `rev 3` before an admin revokes an old API key upstream. The OTLP form cannot clear saved `otel_headers` (`L248` omits `otel_headers` when the input is blank) and offers no `[Send Test Ping]` or live export status (`200 OK` vs `401`).

---

### 2.2 Complete Inventory of Backend & Firmware Capabilities Missing from `frontend/src/`

| # | Backend / Firmware Capability | Backend / Firmware Source | Status in `v0.7.2` Frontend (`frontend/src/`) | Impacted Personas |
| :-: | :--- | :--- | :--- | :--- |
| **1** | **Tethered USB-C / Carrier Dock Sync (`dock-sync`)** | `POST /v1/devices/{id}/dock-sync` (`devices.py:216`); `ff_ota_dock_stage` | **0% (Completely Dark):** Not in `api.ts` or any `.tsx` file. | `Chloe` (`1.0/10`), `Elena` (`2.0/10`), `Marcus-2` |
| **2** | **Venue Offline / EMCON Radio Lockout (`E301`)** | `ff_set_offline_mode(true)` (`fleetforge.h:158`), `offline_mode` | **0% (Completely Dark):** No `⊘ VENUE EMCON` badge, no radio posture breakdown, no toggle. | `Chloe` (`1.5/10`), `Elena` |
| **3** | **Device Telemetry Time-Series API** | `GET /v1/devices/{id}/telemetry` (`devices.py:281`, `TelemetrySeries`) | **0% (Unwired):** Defined in `api.ts:276`, **0 callers** across all `.tsx` components. | `Sarah-2`, `Elena-1..2`, `Chloe-1..2` |
| **4** | **Manual A/B Slot Rollback API (`R5-be-2`)** | `POST /v1/devices/{id}/rollback` (`deploy.py:553`) | **0% (Completely Dark):** Not in `api.ts` or `FleetView.tsx`. | `Sarah-1..2`, `Marcus-1..2`, `Elena-1` |
| **5** | **Firmware `safe_mode: true` Latch (`R5-fw-6`)** | `DeviceSummary.safe_mode` (`schemas.py:42`, 3 crashes in 60s) | **0% (Completely Dark):** Omitted from `api.ts` `Device` interface and `FleetView.tsx`. | `Sarah-1..2`, `Alex-1..2` |
| **6** | **Server-Filtered 5,000-Line Device Logs** | `GET /v1/devices/{id}/logs?level=&prefix=&limit=1000` (`devices.py:258`) | **15% (Hardcoded `limit=200`):** `api.ts:253` drops `level` & `prefix`; `DeviceLog` slices 200 lines client-side. | `Sarah-1..2`, `Elena-1..2`, `Alex-1..2` |
| **7** | **Decommissioned RMA Archive Query** | `GET /v1/devices?include_decommissioned=true` (`devices.py:124`) | **0% (Hardcoded off):** Never passed by `api.ts:154`. | `Marcus-1..2` |
| **8** | **Planned Sortie Leases (`expected_back_s`, `parent_device_id`)** | `SWARM.md` §5.2–5.3 (`link_type: "dock"`, `reason: "sortie"`, `parent_device_id`) | **0% (Completely Dark):** Flat table only knows `online`, `sleepy`, `offline`. | `Elena-1..2` |
| **9** | **ECDSA P-256 2-Key Ring (`[K1_pub, K2_pub]`)** | `signing.py` (`R6-fw-2`), `ArtifactOut.sig` | **30%:** Shows generic `✓sig` only when `sig` non-null; hides key ring, key ID, and `! UNSIGNED` warning. | `Siddharth-1..2`, `Marcus-1` |
| **10** | **Clickable CI Provenance & CD Skip / Webhook Diagnostics** | `ArtifactOut.build_url`, `repo_url`; `TrackOut.skipped`, `ci_webhook_triggered` | **25%:** `build_url`/`repo_url` never linked; `skipped` values & webhook failures discarded. | `Siddharth-1..2`, `Alex-2` |
| **11** | **Scoped `ffa_...` Token Audit & `enroll` Scope** | `POST /v1/auth/tokens` (`expires_in_days`, `enroll`, `last_used_at`) | **40%:** Missing `expires_in_days`, `enroll` checkbox, `last_used_at` column, and `[Copy]`. | `Siddharth-1..2`, `Marcus-2` |

---

## 3. Embedded Fleet UX Best Practices & Monochrome Pixel Ergonomic Principles

### 3.1 Industrial Benchmark Matrix (What Best-in-Class Embedded Platforms Do Right)

| Platform / Benchmark | Core UX Pattern | Direct Application to Fleetforge |
| :--- | :--- | :--- |
| **BalenaCloud** | **Target Release Matrix + Persistent Slide-Over Inspector:** Never expands full forms inside table rows; clicking a device opens a sticky right-hand inspector while keeping the fleet table fixed. | Drives **Fixed-Height (`40px`/`32px`) Master Fleet Grid + 42% Pinned Right Inspector (`DeviceInspector.tsx`)** with `0 px` vertical layout shift. |
| **Memfault** | **Hardware Health Triage, Reboot Reason Histograms & Preserved Crash Rings:** Elevates `min_free_heap` watermarks, `fault_resets`, and `RTC_NOINIT_ATTR` `[prev-boot]` coredumps to first-class sortable columns and pinned crash headers. | Drives **Sortable `HEALTH / HEAP` Column (`148K/110K ✓`, `!wdt 62K/14K!`, `!! SAFE MODE`)**, 1-bit `TelemetrySeries` sparklines (`▇▇▆▅▄▃▂▁`), and **Pinned `[prev-boot]` Crash Box** backed by `?prefix=[prev-boot]`. |
| **Mender.io & AWS IoT** | **Staged Deployment Rings (`Dev -> Canary -> Beta -> Prod`) with Circuit-Breaker Gates:** Visualizes phased promotion as a horizontal pipeline rail with automatic rollback halt and blast-radius preview. | Drives **3-Tier Progressive `ReleaseConvergenceRibbon.tsx`** and **On-Demand Slide-Down `Artifact × Ring × ff_sec` Matrix Drawer**. |
| **Golioth** | **Unified Device Workspace + Copyable CLI/API Parity + CoAP/OTel Pipeline Health:** Every UI action has a 1-click CLI equivalent, and OTLP/secret sync states show per-device convergence. | Drives **`[📋 Copy CLI / CI Snippet ▾]` Drawer**, clickable **`[! 3 on sec:r3 ▸]`** sleepy-board filter, and **`[Send Test OTel Ping ⚡]`** (`200 OK`). |
| **Foxglove Studio & QGroundControl** | **High-Stress Field / Pit Cockpits (`RULE E301` EMCON, Safe-Window Arming Gates & Store-and-Forward Replay):** Separates stage (`in-air`) from apply (`on-ground`), shows explicit radio lockouts, and reconstructs `age_ms` capture timestamps. | Drives **6-State `StatusCell` (`◎ SORTIE`, `⊘ VENUE EMCON`, `!! OVERDUE`)**, **2-Phase `Hold at Staged` $\to$ `[▶ Apply Grounded]`**, and **1-Screen USB-C Pit & Carrier Dock Cockpit**. |

### 3.2 Monochrome Pixel Hierarchy Without Color Reliance (`.stitch/DESIGN.md`)
Because `.stitch/DESIGN.md` enforces a **strict 7-step monochrome pixel aesthetic** (`#0c0c0c` `--ff-bg`, `#141414` `--ff-panel`, `#3a3a3a` `--ff-line`, `#6a6a6a` `--ff-line-hi`, `#8a8a8a` `--ff-dim`, `#d8d8d8` `--ff-fg`, `#ffffff` `--ff-hi`), severity and state must never depend on red/amber/green hue alone. Instead, Fleetforge encodes state across **five orthogonal visual channels**:
1. **Unique Prefix Glyph:** `✓` (Confirmed/Healthy), `✗` (Failed/Rolled Back), `!!` (Emergency Safe Mode / Overdue Lease), `!` (Warning / Drift / Fault Reset), `◷` (Held / Queued for Safe Window or Wake), `…` / `↺` (Active In-Flight), `█` (Online Wi-Fi / Dock), `◐` (Sleepy Asleep), `◎` (Planned Sortie), `⊘` (Venue EMCON Radio Lockout), `░` (Offline).
2. **Surface & Border Weight:**
   - **Emergency / Venue Lockout (`.emcon` / `.safe-mode`):** **Inverse Pill** — Solid `#ffffff` background with `#0c0c0c` bold text.
   - **Critical Failure (`.bad`):** `#ffffff` bold `700` text with a `3px solid #ffffff` bottom underline and `2px solid #ffffff` frame.
   - **Active Tethered Dock (`.dock`):** `#ffffff` bold `700` text inside a `2px solid #ffffff` box (`█ DOCK · USB-C`).
   - **Warning / Held / Sortie (`.warn`):** `#d8d8d8` text inside a `1px solid #6a6a6a` pill.
   - **Expected Asleep (`.sleepy`):** `#8a8a8a` text inside a `1px dashed #3a3a3a` pill (`◐ ASLEEP ~12m(30m)`).
3. **Typography & Tabular Alignment:** `Press Start 2P` (`0.55rem`–`1rem`) strictly for structural headers, tabs, and tactile buttons (`2px solid #3a3a3a` + `3px 3px 0 #3a3a3a` shadow); `DejaVu Sans Mono` with `font-variant-numeric: tabular-nums` for all IDs, SHAs, heap gauges, 1-bit sparklines (`▁▂▃▄▅▆▇█`), and 1 Hz relative ages so digits never jitter horizontally.
4. **Spatial Priority Pinning (`Freeze Order on Pointer Hover`):** Exception rows (`Score -1` and `Score 0`) automatically pin to the top of the Fleet Grid, but row order freezes whenever the operator's pointer is inside `<tbody>` or a row is selected—preventing 1 Hz SSE re-sorts from moving a button under the cursor.
5. **High-Contrast Sunlight / Arena Pit Mode (`[☀ Pit Contrast]`):** A 1-click top-bar toggle switches `--ff-bg` to pure `#000000` and borders to `2px solid #ffffff` (`21:1` contrast ratio) for outdoor drone fields (`Elena`) and harsh arena lighting (`Chloe`).

---

## 4. Round 1 Hypothesis Designs (`H1`–`H4`) — Architecture, Wireframes & Flow Comparisons

In Round 1 (`03_hypothesis_designs_round1.md`), `@design-architect` created **4 distinct architectural hypotheses** to test competing structural paradigms across all 6 personas:

```
┌────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ ROUND 1 ARCHITECTURAL PARADIGMS TESTED across 12 PERSONA EVALUATORS                                            │
├───────────────────────────┬───────────────────────────┬───────────────────────────┬────────────────────────────┤
│ H1: Evolutionary 4-Tab    │ H2: Split-Pane Operator   │ H3: Release Pipeline &    │ H4: Role/Context-Adaptive  │
│ + Accordion Table         │ Workbench + Right Drawer  │ Ring Swimlane Console     │ 4-Mode Mission & Pit UI    │
├───────────────────────────┼───────────────────────────┼───────────────────────────┼────────────────────────────┤
│ • Keeps 4 tabs & full-    │ • 60% Fixed-Height (40px) │ • Top 4-Stage Release     │ • Top Mode Switcher:       │
│   width table.            │   Master Grid (0px shift) │   Pipeline Rail (`Dev ->  │   [1. Bench/Maker (1 brd)] │
│ • Single-row accordion    │   + 40% Pinned Right      │   Canary -> Beta -> Prod`)│   [2. Fleet Matrix]        │
│   drawer (`Deploy|Vitals| │   Inspector Drawer.       │ • Collapsible Ring        │   [3. Pit / USB-C Dock]    │
│   Logs|Config` sub-tabs). │ • `j`/`k` keyboard nav +  │   Swimlanes on Tab 1.     │   [4. CI & Security]       │
│ • Fixes missing APIs in   │   Multi-Select Bulk Bar.  │ • `Artifact × Ring`       │ • Dedicated 1-screen       │
│   place with minimal      │ • Side-by-side Tab 3      │   Convergence Matrix on   │   90s `RULE E301` Pit      │
│   structural disruption.  │   Flash + Serial Console. │   Tab 2.                  │   Cockpit in Mode 3.       │
├───────────────────────────┼───────────────────────────┼───────────────────────────┼────────────────────────────┤
│ R1 Mean: 7.10 / 10 (4th)  │ R1 Mean: 8.29 / 10 (1st)  │ R1 Mean: 7.40 / 10 (3rd)  │ R1 Mean: 7.90 / 10 (2nd)   │
└───────────────────────────┴───────────────────────────┴───────────────────────────┴────────────────────────────┘
```

### 4.1 Summary of Round 1 Hypotheses (`H1`–`H4`)
1. **`H1` ("Evolutionary 4-Tab Polish & Inline Accordion Table"):** Retained the existing 4-tab layout (`1. FLEET`, `2. BUILDS`, `3. ONBOARD & DOCK`, `4. ACCESS`), added a pinned `FleetSummaryRibbon`, replaced the disconnected bottom `DeviceLog` with a 4-sub-tab inline row accordion (`[1. Deploy] [2. Vitals] [3. Logs] [4. Config]`), and wired `dock-sync` into Tab 3.
2. **`H2` ("Split-Pane Operator Workbench & Persistent Inspector Drawer"):** Replaced inline row expansion and the bottom log panel with a **60% Left Master Fleet Grid** (locked to fixed `40px` single-line rows with `0 px` vertical layout shift and `[x] Freeze order on hover`) paired with a **40% Right Contextual Inspector Drawer** (Single-Board Inspector when 1 row is clicked or navigated via `j`/`k`; Cohort Rollout & Bulk Actions when 0 or 2+ rows are selected). Placed 4-step Web Serial config on the left and `BoardConsole` on the right in Tab 3.
3. **`H3` ("Release Pipeline & Ring Matrix Console"):** Replaced the flat device table with a **Top Active Release Pipeline Rail** (`Git CD -> 0. Dev -> 1. Canary -> 2. Beta -> 3. Prod` with circuit-breaker halt banners) above **Collapsible Ring Swimlanes** in Tab 1, and introduced an **Artifact & `sub_versions` Convergence Matrix** across rings in Tab 2.
4. **`H4` ("Role/Context-Adaptive Mission & Pit Console"):** Introduced a 4-Mode Switcher (`[1. BENCH / MAKER]`, `[2. FLEET MATRIX]`, `[3. PIT / USB-C DOCK]`, `[4. CI & SECURITY]`). Mode 1 put 1-board drag-and-drop `.bin` upload, `Fast-Deploy`, and live logs on one screen; Mode 3 provided a dedicated 1-screen **`RULE E301` Venue Offline & USB-C Pit Cockpit** with 3-pill radio posture, zero-click auto-drain, 3-peripheral `ff_selftest`, and a 3-node Peer Mesh `sub_versions` Skew Matrix.

---

## 5. Round 1 Multi-Persona Evaluation Matrix (12 Evaluators: Quantitative Scores, Verbatim Critiques & Tension Analysis)

### 5.1 Complete 12-Evaluator $\times$ 4-Hypothesis Quantitative Score Matrix (Round 1)

| Evaluator | Persona & Sub-Profile | Baseline (`v0.7.2`) | `H1` (Accordion) | `H2` (Split-Pane) | `H3` (Ring Matrix) | `H4` (Adaptive Modes) | R1 Winner |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **`alex-1`** | P1: Weekend Arduino Homestead Maker (5 boards) | `4.80` | `7.30` | `8.00` | `5.30` | **`9.10`** | **`H4`** |
| **`alex-2`** | P1: PlatformIO E-Ink & Sleepy-Frame Tinkerer (12 boards) | `5.80` | `7.90` | **`9.00`** | `7.20` | `8.30` | **`H2`** |
| **`elena-1`** | P2: Air-Ground Drone Swarm Flight Commander (5 UGVs + 20 Drones) | `3.40` | `6.60` | `8.20` | **`8.40`** | `7.80` | **`H3`** *(H2 8.2)* |
| **`elena-2`** | P2: Autonomous Carrier-Deck & Data-Mule Lead (3 UGVs + 18 Quads) | `3.80` | `6.40` | **`8.10`** | `7.60` | **`8.10`** | **`H2` / `H4` Tie** |
| **`marcus-1`** | P3: AgTech & Commercial Sensor Startup CTO (240 boards, 6 projects) | `4.60` | `7.20` | `8.40` | **`8.80`** | `6.80` | **`H3`** *(H2 8.4)* |
| **`marcus-2`** | P3: Factory Provisioning & Field RMA Ops Lead (250+ boards) | `4.70` | `7.20` | **`8.80`** | `8.30` | `7.00` | **`H2`** |
| **`siddharth-1`** | P4: CI/CD Release Pipeline & Provenance Architect (10 targets) | `4.60` | `7.00` | `7.60` | **`8.10`** | `7.10` | **`H3`** |
| **`siddharth-2`** | P4: Security, Secrets & OTLP Platform Engineer (38–200 boards) | `4.90` | `7.40` | `8.00` | **`8.10`** | `7.90` | **`H3`** *(H2 8.0)* |
| **`sarah-1`** | P5: High-Throughput HIL Regression Rack Lead (60 mixed boards) | `3.00` | `7.10` | **`8.68`** | `5.95` | `6.88` | **`H2`** |
| **`sarah-2`** | P5: HIL Soak-Test, Memory-Leak & Crash-Triage Engineer (24–60 boards) | `3.30` | `6.00` | **`8.40`** | `5.60` | `6.20` | **`H2`** |
| **`chloe-1`** | P6: FRC Pit Crew & Robot Cart Hardware Lead (`CUJ-3` 90s Pit) | `2.80` | `7.60` | `8.20` | `8.00` | **`9.66`** | **`H4`** |
| **`chloe-2`** | P6: Contested-RF Scouting Chief & Telemetry Strategist (`CUJ-3`) | `2.40` | `7.50` | `8.10` | `7.60` | **`9.60`** | **`H4`** |
| **Grand Mean** | **All 12 Evaluators (6 Personas $\times$ 2 Instances)** | **`4.08`** | **`7.10`** | **`8.29`** | **`7.40`** | **`7.90`** | **`H2` (+0.39)** |

---

### 5.2 Per-Criterion Score Matrix & Verbatim Critiques by Persona (Round 1)

#### Persona 1: Alex — Solo Maker & Home-Lab Tinkerer (`alex-1` & `alex-2`)
| Evaluator | Criterion | Base | `H1` | `H2` | `H3` | `H4` | Key Verbatim Critique from Round 1 Evaluation |
| :--- | :--- | :-: | :-: | :-: | :-: | :-: | :--- |
| **`alex-1`**<br>*(Arduino Homestead, 5 heterogeneous sketches)* | (a) Onboarding (1st & Nth board)<br>(b) Daily Code $\to$ Deploy $\to$ Verify<br>(c) Triage & Rollback Recovery<br>(d) Network / Secrets / Sleepy Mgmt<br>(e) Cognitive Load & Visual Clarity<br>**Mean** | 6.0<br>4.5<br>5.0<br>4.0<br>4.5<br>**4.80** | 8.0<br>7.0<br>7.5<br>7.5<br>6.5<br>**7.30** | 8.0<br>7.5<br>8.5<br>8.5<br>7.5<br>**8.00** | 6.5<br>5.5<br>6.5<br>5.0<br>3.0<br>**5.30** | **9.5**<br>**9.5**<br>**9.0**<br>**9.0**<br>**8.5**<br>**9.10** | *"My 5 homestead boards run 5 different Arduino sketches (`coop_door`, `garden_soil`, `porch_led`). `H1`–`H3` compare every board against the latest uploaded version, screaming `≠ 4 Version Drift` permanently! And in `H2`, I still have to jump to Tab 2 to upload a `.bin` and flip between Inspector sub-tabs `[1. DEPLOY]` and `[2. HEALTH & LOGS]`, whereas `H4` Mode 1 puts drag-and-drop `.bin` + deploy + live logs on one screen."* |
| **`alex-2`**<br>*(PlatformIO E-Ink, 1 `★dev` + 11 sleepy 30m frames)* | (a) Onboarding (1st & Nth board)<br>(b) Daily Code $\to$ Deploy $\to$ Verify<br>(c) Triage & Rollback Recovery<br>(d) Network / Secrets / Sleepy Mgmt<br>(e) Cognitive Load & Visual Clarity<br>**Mean** | 6.5<br>5.5<br>6.5<br>5.0<br>5.5<br>**5.80** | 8.5<br>7.5<br>8.0<br>8.0<br>7.5<br>**7.90** | **9.0**<br>**9.0**<br>**9.5**<br>**8.5**<br>**9.0**<br>**9.00** | 7.5<br>8.0<br>7.5<br>7.0<br>6.0<br>**7.20** | 8.5<br>9.5<br>8.5<br>8.0<br>7.0<br>**8.30** | *"Because `H4` `[1. BENCH / MAKER]` only displays one single board, it makes my other 6 sleeping wall frames completely invisible while I'm coding! `H2` keeps all 7 boards visible on the left while pinning my `★ dev` board in the Right Inspector—just merge `H4`'s single-view Deploy + Logs card into `H2`'s Right Inspector and add a bounded `⚡ stay awake: 30m` lease so a sleepy frame never drains its LiPo overnight."* |

#### Persona 2: Elena — Field Robotics & Drone Swarm Lead (`elena-1` & `elena-2`)
| Evaluator | Criterion | Base | `H1` | `H2` | `H3` | `H4` | Key Verbatim Critique from Round 1 Evaluation |
| :--- | :--- | :-: | :-: | :-: | :-: | :-: | :--- |
| **`elena-1`**<br>*(Swarm Commander, 5 UGVs + 20 Drones)* | (a) Safe-Window (`stage-in-air`)<br>(b) Canary & Wave Blast-Radius<br>(c) Companion Cargo & `sub_versions`<br>(d) Tethered Dock & `age_ms` Replay<br>(e) High-Stress Field Ergonomics<br>**Mean** | 4.0<br>3.0<br>4.0<br>2.0<br>4.0<br>**3.40** | 7.0<br>6.0<br>7.0<br>7.0<br>6.0<br>**6.60** | 8.0<br>8.0<br>8.0<br>8.0<br>**9.0**<br>**8.20** | 7.0<br>**10.0**<br>**10.0**<br>7.0<br>8.0<br>**8.40** | 8.0<br>7.0<br>8.0<br>**9.0**<br>7.0<br>**7.80** | *"`deployOutcome()` in `deploy.ts:153` checks `device.fw_version === d.artifact_version` for ALL artifacts—so confirming `model_weights` or `mcu_fw` falsely triggers `drift`! Also, in my field truck while 15 drones are flying over Wi-Fi and 2 are plugged into USB-C docks, switching full-screen to `H4` Mode 3 completely hides the 15 airborne drones. Embed `H4`'s Dock view inside `H2`'s Right Inspector and add 2-phase `Hold at staged (on_command)` $\to$ `[▶ Apply All Grounded]`."* |
| **`elena-2`**<br>*(Carrier-Deck & Data-Mule Lead, 3 UGVs + 18 Quads)* | (a) Safe-Window (`stage-in-air`)<br>(b) Canary & Wave Blast-Radius<br>(c) Companion Cargo & `sub_versions`<br>(d) Tethered Dock & `age_ms` Replay<br>(e) High-Stress Field Ergonomics<br>**Mean** | 4.5<br>4.0<br>4.5<br>2.0<br>4.0<br>**3.80** | 6.5<br>6.0<br>7.0<br>6.5<br>6.0<br>**6.40** | 7.5<br>8.5<br>7.5<br>8.0<br>**9.0**<br>**8.10** | 7.0<br>**9.0**<br>**9.5**<br>6.0<br>6.5<br>**7.60** | 7.5<br>7.5<br>8.0<br>**9.0**<br>8.5<br>**8.10** | *"A scout quad on a 15-minute radio-silent data-mule sortie (`reason: 'sortie'`, `expected_back_s: 900`) is NOT offline—we need `█ DOCK (@ugv-01)`, `◎ SORTIE · ETA 11m`, and `!! OVERDUE (+04m past lease)` (Priority Score 0), plus `Group by: Parent Gateway (parent_device_id)`, `[x] Leapfrog (max 1 apply per parent carrier)`, and inline `age_ms` timestamps (`12:14:02 [drained +15m02s via age_ms]`) instead of desktop-only hover tooltips."* |

#### Persona 3: Marcus — Commercial IoT & AgTech Startup CTO (`marcus-1` & `marcus-2`)
| Evaluator | Criterion | Base | `H1` | `H2` | `H3` | `H4` | Key Verbatim Critique from Round 1 Evaluation |
| :--- | :--- | :-: | :-: | :-: | :-: | :-: | :--- |
| **`marcus-1`**<br>*(AgTech OEM CTO, 240 boards, 6 projects)* | (a) Multi-Project & 240-Board Scale<br>(b) Staged Ring Rollout (`Alpha->Prod`)<br>(c) Batch Provisioning & RMA Swap<br>(d) Auditability (`Sent by`, KPIs)<br>(e) Team RBAC & `ff_sec` Rotation<br>**Mean** | 4.0<br>4.0<br>5.0<br>5.0<br>5.0<br>**4.60** | 6.0<br>7.0<br>8.0<br>8.0<br>7.0<br>**7.20** | **9.0**<br>8.0<br>**9.0**<br>8.0<br>8.0<br>**8.40** | 8.0<br>**10.0**<br>8.0<br>**9.0**<br>**9.0**<br>**8.80** | 6.0<br>6.0<br>7.0<br>7.0<br>8.0<br>**6.80** | *"A single bad OTA that bricks a sensor on a grain silo costs us a $400 truck roll. `H3`'s visual `0. DEV -> 1. CANARY -> 2. BETA -> 3. PROD` pipeline rail with `[✗ HALTED — CIRCUIT BREAKER TRIPPED]` is executive gold, while `H2`'s fixed `40px` Split-Pane handles 240 sensors with zero scroll fatigue. Fuse `H3`'s Top Pipeline Ribbon onto `H2`'s Split-Pane, add a Multi-Project Health Switcher Bar, and enforce role-aware locks (`viewer`/`operator`/`owner`)."* |
| **`marcus-2`**<br>*(Factory Provisioning & RMA Ops, 250+ boards)* | (a) Multi-Project & 250-Board Scale<br>(b) Staged Ring Rollout (`Alpha->Prod`)<br>(c) Batch Provisioning & RMA Swap<br>(d) Auditability (`Sent by`, KPIs)<br>(e) Team RBAC & `ff_sec` Rotation<br>**Mean** | 3.5<br>4.5<br>5.0<br>5.5<br>5.0<br>**4.70** | 6.5<br>7.5<br>7.5<br>8.0<br>6.5<br>**7.20** | **9.5**<br>8.5<br>**9.0**<br>8.5<br>**8.5**<br>**8.80** | 7.5<br>**9.5**<br>7.5<br>**9.0**<br>8.0<br>**8.30** | 6.5<br>6.5<br>7.0<br>7.0<br>8.0<br>**7.00** | *"Every Tuesday my bench technician unboxes 40 ESP32-S3 boards. `H2`'s side-by-side Tab 3 layout (Web Serial config on left, `BoardConsole` on right) is great, but we also need a **Batch CLI Token Generator (`N=40` tokens + CSV & `flash-batch.sh` download)** for our headless `esptool.py` factory jig, Cross-Project RMA `409` transfer in `PreflightCard`, `[□ RMA Archive]`, and lifting `LIST_LIMIT = 200` to `500` with a `[! Weak RSSI (< -75 dBm)]` filter."* |

#### Persona 4: Siddharth — Firmware Release & DevSecOps Lead (`siddharth-1` & `siddharth-2`)
| Evaluator | Criterion | Base | `H1` | `H2` | `H3` | `H4` | Key Verbatim Critique from Round 1 Evaluation |
| :--- | :--- | :-: | :-: | :-: | :-: | :-: | :--- |
| **`siddharth-1`**<br>*(CI/CD Release Pipeline Architect, 10 targets)* | (a) Git Provenance Traceability<br>(b) CD Config & Gate Diagnostics<br>(c) Security & Token Governance<br>(d) Secret Rotation & OTLP Config<br>(e) API / CLI Parity (`fleetforge.cli`)<br>**Mean** | 4.5<br>5.0<br>5.5<br>5.0<br>3.0<br>**4.60** | 6.5<br>7.0<br>7.5<br>8.0<br>6.0<br>**7.00** | 7.5<br>7.5<br>7.5<br>**8.5**<br>7.0<br>**7.60** | **9.0**<br>**9.0**<br>**8.0**<br>**8.5**<br>6.0<br>**8.10** | 7.0<br>7.5<br>**8.0**<br>**8.5**<br>4.5<br>**7.10** | *"`build_url` and `repo_url` are fetched in `api.ts` but never rendered as clickable links! Also, our CI builds 10 chip targets per commit—group `UploadedBuilds` by `version @ commit_sha` with target sub-pills (`esp32s3 ✓`, `esp32c6 ✓`), surface the `Last CD Gate Verdict` + expandable `skipped` dictionary + `Webhook Health: ✗ HTTP 401` banner, and add a 1-click `[📋 Copy CLI / CI Snippet ▾]` drawer."* |
| **`siddharth-2`**<br>*(Security, Secrets & OTel Engineer)* | (a) Git Provenance Traceability<br>(b) CD Config & Gate Diagnostics<br>(c) Security & Token Governance<br>(d) Secret Rotation & OTLP Config<br>(e) API / CLI Parity (`fleetforge.cli`)<br>**Mean** | 5.5<br>6.0<br>4.5<br>4.5<br>4.0<br>**4.90** | 7.5<br>7.5<br>7.0<br>8.0<br>7.0<br>**7.40** | 8.0<br>8.0<br>8.0<br>**8.5**<br>**7.5**<br>**8.00** | **9.0**<br>**9.0**<br>7.5<br>8.0<br>7.0<br>**8.10** | 7.5<br>8.0<br>**8.0**<br>**8.5**<br>**7.5**<br>**7.90** | *"Four security blind spots must be closed: (1) expose the ECDSA P-256 2-key rotation ring (`[K1_pub, K2_pub]`) and `[! UNSIGNED]` warnings; (2) add `TTL` (`expires_in_days`), the missing `enroll` scope checkbox, `last_used_at`, and `[Copy]` to `ffa_...` tokens; (3) make `sec:r4 (35/38 synced)` clickable to list the exact 3 sleepy boards still on `rev 3` before I revoke the old MQTT key; (4) add `[Clear Headers]` and `[Send Test Ping ⚡]` to OTLP."* |

#### Persona 5: Sarah — Hardware-in-the-Loop & QA Test Engineer (`sarah-1` & `sarah-2`)
| Evaluator | Criterion | Base | `H1` | `H2` | `H3` | `H4` | Key Verbatim Critique from Round 1 Evaluation |
| :--- | :--- | :-: | :-: | :-: | :-: | :-: | :--- |
| **`sarah-1`**<br>*(High-Throughput HIL Rack Lead, 60 mixed boards)* | (a) 60-Board Visibility & 0-Jump<br>(b) `selftest` & `returned_to` Speed<br>(c) Heap, Fault & `[prev-boot]` Crash<br>(d) Fast Controls (`Reboot`/`Rollback`)<br>(e) Keyboard & Multi-Board Compare<br>**Mean (Wtd)** | 3.0<br>2.5<br>3.5<br>4.0<br>2.0<br>**3.00** | 6.5<br>8.0<br>7.5<br>7.5<br>5.5<br>**7.10** | **9.5**<br>7.5<br>**9.0**<br>**8.5**<br>**9.0**<br>**8.68** | 5.0<br>7.0<br>6.0<br>6.5<br>5.0<br>**5.95** | 6.5<br>7.0<br>7.5<br>7.5<br>6.0<br>**6.88** | *"`H2` is the only design with `0 px` layout shift across 60 boards, but its Master Grid truncated `DEPLOY` to `✗ rolled`—forcing me to press `j` 4 times to read 4 rolled-back boards' `deploy.selftest.reason` and `returned_to` slots (`ota_0`/`ota_1`). Render the verbatim `✗ selftest: i2c 0x48 timeout → ota_1` right in the fixed `32px` row (`< 1.5s`, 0-click scan!) and make `Rollback saves: 4` open a side-by-side `[Compare / Diff (4)]` table!"* |
| **`sarah-2`**<br>*(HIL Soak-Test, Memory-Leak & Crash-Triage)* | (a) 60-Board Visibility & 0-Jump<br>(b) `selftest` & `returned_to` Speed<br>(c) Heap, Fault & `[prev-boot]` Crash<br>(d) Fast Controls (`Reboot`/`Rollback`)<br>(e) Keyboard & Multi-Board Compare<br>**Mean** | 2.5<br>4.5<br>3.5<br>4.0<br>2.0<br>**3.30** | 6.0<br>7.0<br>6.0<br>7.0<br>4.0<br>**6.00** | **9.0**<br>**8.0**<br>**8.0**<br>**9.0**<br>**8.0**<br>**8.40** | 5.0<br>7.0<br>5.0<br>6.0<br>5.0<br>**5.60** | 7.0<br>6.0<br>6.0<br>7.0<br>5.0<br>**6.20** | *"During a 24-hour soak test, I need `free_heap / min_free_heap` visible on EVERY row (`148K/110K ✓`, `!wdt 62K/14K!`) with 1-click column sorting by `min_free_heap ASC` or `heap_delta DESC`, 1-bit `TelemetrySeries` sparklines (`▇▇▆▅▄▃▂▁ leak slope -2.1K/m`), `!! SAFE MODE (3×)` (`R5-fw-6`) at Priority Score `-1`, an explicit `[↶ Rollback to ota_0]` button (`R5-be-2`), and server-filtered `?prefix=[prev-boot]` so post-boot `I` logs never evict overnight crashes."* |

#### Persona 6: Chloe — Competition Pit & Contested-Venue Engineer (`chloe-1` & `chloe-2`)
| Evaluator | Criterion | Base | `H1` | `H2` | `H3` | `H4` | Key Verbatim Critique from Round 1 Evaluation |
| :--- | :--- | :-: | :-: | :-: | :-: | :-: | :--- |
| **`chloe-1`**<br>*(FRC Pit Crew & Robot Cart Lead, `CUJ-3` 90s Pit)* | (a) Venue Offline / `E301` Visibility<br>(b) USB-C `dock` Stage & Ring Drain<br>(c) `data_partition` & Mesh Skew<br>(d) Hotel CI/CD & `ff_sec` Prep<br>(e) 90-Second Pit Turnaround<br>**Mean** | 2.0<br>1.0<br>4.0<br>5.0<br>2.0<br>**2.80** | 7.5<br>8.0<br>7.0<br>8.0<br>7.5<br>**7.60** | 8.5<br>8.0<br>8.0<br>8.5<br>8.0<br>**8.20** | 8.0<br>8.0<br>9.5<br>8.5<br>6.0<br>**8.00** | **10.0**<br>**10.0**<br>**10.0**<br>**8.5**<br>**9.8**<br>**9.66** | *"`H4` Mode 3 (`[3. PIT / USB-C DOCK]`) is a dream 90-second pit cockpit: `RULE E301` 3-pill radio posture (`802.11 OFF`, `Improv BLE OFF`, `Shared NimBLE LIVE`), zero-click auto-drain, 3-peripheral `ff_selftest` (`SX1262 LoRa`, `CAN`, `NimBLE`), 1-click `[data_partition]` vs `[app]` stage, and the 3-node Peer Mesh Skew Matrix all on one screen! Just add an `[✓ Pit Cache Ready in IndexedDB]` badge and hard-separate USB-C `dock` from destructive `esptool` full flash."* |
| **`chloe-2`**<br>*(Contested-RF Scouting Chief & Telemetry Strategist)* | (a) Venue Offline / `E301` Visibility<br>(b) USB-C `dock` Stage & Ring Drain<br>(c) `data_partition` & Mesh Skew<br>(d) Hotel CI/CD & `ff_sec` Prep<br>(e) 90-Second Pit Turnaround<br>**Mean** | 1.0<br>1.5<br>3.0<br>4.5<br>2.0<br>**2.40** | 7.5<br>8.0<br>7.0<br>7.5<br>7.5<br>**7.50** | 8.5<br>8.0<br>7.5<br>8.5<br>8.0<br>**8.10** | 7.5<br>7.5<br>9.0<br>**9.0**<br>5.0<br>**7.60** | **10.0**<br>**9.5**<br>**10.0**<br>8.5<br>**10.0**<br>**9.60** | *"In `H2`, the Right Inspector only showed `sub_versions` for the 1 selected board and forced sub-tab switching between `[1. DEPLOY]` and `[2. HEALTH & LOGS]` during a 90s match window. We need `H4` Mode 3's unified 3-Node LoRa Mesh Skew Matrix (`pit-node`, `robot-cart-node`, `scouting-chief` at `-78 dBm` flagging `! v14 SKEW`) and a `dock-sync` Replay Verification Receipt (`Reconstructed timestamps via age_ms · OTLP exported: 200 OK ✓`)."* |


---

## 6. Hypothesis Evolution — Round 2 Converged Designs (`H5` & `H6`) & How Round 1 Feedback Reshaped the Architecture

### 6.1 The 6 Inter-Persona Design Tensions & How Round 2 Resolved Them

Synthesizing all 12 Round 1 evaluations (`04_round1_synthesis.md`) revealed that `H2`, `H3`, and `H4` were not mutually exclusive—each solved a different layer of the embedded operations stack. Below is the architectural traceability matrix showing how Round 2 (`H5` & `H6`) resolved every inter-persona conflict without compromise:

| # | Inter-Persona Tension in Round 1 | Competing Personas | Architectural Resolution in Round 2 (`H5` & `H6`) |
| :-: | :--- | :--- | :--- |
| **1** | **Zero-Clutter Maker Simplicity vs. Enterprise Ring & Cargo Governance** | `Alex-1` (`5.30` on `H3`)<br>$\leftrightarrow$<br>`Marcus-1` (`8.80`), `Elena-1` (`8.40`), `Siddharth-1..2` (`8.10`) | **3-Tier Progressive `ReleaseConvergenceRibbon.tsx` + `appImage.projectName`-Scoped Convergence:**<br>• **Tier 1 (`groups.length === 0`):** Collapses to a 1-line Maker Bar (`5/5 safe · 3 sketches (coop_door v1.2, garden_soil v2.0, porch_led v1.0 — 0 drift)`) that groups version drift by `appImage.projectName` so heterogeneous sketches never trigger false drift.<br>• **Tier 2 (`groups.length > 0` or CD active):** Expands to the 4-stage Release Pipeline Rail (`0. Dev -> 1. Canary -> 2. Beta -> 3. Prod`) with circuit-breaker halt banners, `DeployKpiStrip`, `Cargo sub_versions`, and clickable `sec:r4 ([! 3 on r3 ▸])`, plus (`H6`) a 1-click `[▼ Expand Full Release & Cargo Matrix]` drawer. |
| **2** | **Single-Screen Bench Speed (`H4` Mode 1) vs. Zero-Tab-Ping-Pong Multi-Board Workbench (`H2`)** | `Alex-1` (`9.10` on `H4`)<br>$\leftrightarrow$<br>`Alex-2` (`9.00` on `H2`), `Sarah-1..2`, `Elena-1` | **Unified Single-View Right Inspector (`UnifiedTriageBenchView` — `[A. Triage & Bench]`):**<br>Keeps the 58% Left Fleet Grid visible while merging `H4` Mode 1's **Embedded `.bin`/Cargo Drag-and-Drop Uploader** (with auto-suggested patch bump `1.0.4 → 1.0.5` & inline 1-click `[Adopt flash map]`), `PrecheckCard`, `DeployTimeline`, `DeployResultCard` (`returned_to`), `[↶ Rollback to ota_0]`, bounded lease `[⚡ stay awake: 28m left ▾]`, Vitals/`TelemetrySeries` sparklines (`▇▇▆▅▄▃▂▁`), and server-filtered `DeviceLog` (`?prefix=[prev-boot]` + pinned 4-line `RTC_NOINIT_ATTR` crash box) into **one continuous vertical screen** (zero sub-tab switching!). |
| **3** | **0-Click 60-Board Failure & Heap Triage vs. Fixed `40px`/`32px` Zero-Jitter Rows** | `Sarah-1` (`8.68`), `Sarah-2` (`8.40`)<br>$\leftrightarrow$<br>`H2` truncated `DEPLOY` cells | **Verbatim Fixed-Height Exception Cells + Clickable `[Rollback saves: 4 ▸]` $\to$ `[Compare / Diff (4)]`:**<br>• Every `32px`/`40px` row in `HEALTH / HEAP` always displays `free/min` heap + fault status (`148K/110K ✓`, `!wdt 62K/14K!`, `!! SAFE MODE`), sortable by `min_free_heap ASC`, `heap_delta DESC`, or `fault_resets DESC`.<br>• `DEPLOY / VERDICT` renders verbatim 63-byte self-test failure reasons + fallback slots (`✗ selftest: i2c 0x48 timeout → returned_to ota_1`) with `0 px` layout shift.<br>• Clicking `[Rollback saves: 4 ▸]` filters the grid and opens `[C. Compare / Diff (4)]` stacking all 4 boards' `VERDICT & SLOT`, `SELFTEST / RESET`, `FREE / MIN HEAP` sparklines, and `[PREV-BOOT] CRASH` lines side-by-side. |
| **4** | **Full-Width 90-Second Pit Dock Cockpit (`H4` Mode 3) vs. 21–60 Node Fleet Visibility (`H2`)** | `Chloe-1` (`9.66`), `Chloe-2` (`9.60`)<br>$\leftrightarrow$<br>`Elena-1..2` (25 nodes), `Sarah-2` (60 nodes) | **Dual-Access `RULE E301` USB-C / Carrier Dock (In-Inspector `[B. 🔌 Dock]` + Full-Width Tab 3C Cockpit with Pinned Mini-Fleet Strip):**<br>• On Tab 1, `[B. 🔌 Dock]` opens the 1-screen Dock & EMCON view inside the 42% Right Inspector while keeping all 21–60 swarm/rack rows visible in the 58% Left Grid (auto-hidden when no board uses `link_type: "dock"` or `offline_mode`).<br>• Clicking `[⤢ Full-Screen Pit Cockpit]` (or opening Tab 3C `PitDockCockpit.tsx`) renders `H6`'s **Full-Width 3-Column Pit Cockpit** topped by a **Pinned Live Mini-Fleet & Peer Mesh Strip** (`Zero Mode Blindness!`). |
| **5** | **Flat Table vs. Parent-Child Carrier Topology & 6-State Presence/Sortie/Sleep Taxonomy** | `Elena-1..2`, `Alex-2`, `Chloe-1..2` | **6-State `StatusCell` + `Group by: [None | Ring | Parent UGV]` + 2-Phase Cohort Rollout:**<br>• Distinguishes `█ WIFI (-58dBm ▂▄▆█)`, `█ DOCK (@ugv-01 / USB-C)`, `◐ ASLEEP ~12m(30m)`, `◎ SORTIE ETA 11m (@ugv-01)`, `⊘ VENUE EMCON · 802.11 LOCKED` (inverse pill), and `!! OVERDUE (+04m past lease)` (Priority Score `0`).<br>• Cohort Rollout Controller adds `Hold at Staged (on_command)`, `[x] Pause after Canary`, `[x] Leapfrog (max 1/parent)`, and 1-click `[▶ Apply N Grounded]`. |
| **6** | **4-Tab RBAC Separation (`H5`) vs. 3-Tab Consolidation (`H6`) for Factory, CI & Security** | `Marcus-1..2`, `Siddharth-1..2`, `Alex-2`<br>$\leftrightarrow$<br>`Alex-1`, `Sarah-1` | **Keep `H5`'s 4-Tab IA (`1. FLEET WORKBENCH`, `2. RELEASES & CI`, `3. ADD BOARD & DOCK`, `4. SECURITY & GOVERNANCE`) + Fold in `H6`'s Tab 1 Matrix Drawer:**<br>Preserves a strict RBAC boundary between daily `operator` release promotion (`Tab 2`) and `owner`/`superadmin`-only security governance (`Tab 4`), while giving Tab 1 operators `H6`'s 1-click `[▼ Expand Matrix]` drawer. |

---

### 6.2 Converged Round 2 Wireframes (`H5` Spine + `H6` Enhancements)

#### Wireframe A: Multi-Project Health Bar & 3-Tier Progressive Convergence Ribbon (with `H6` Slide-Down Matrix Drawer)
```
(1) TIER 1 — COMPACT MAKER STATE (Auto-rendered when groups.length === 0 & no active wave — e.g., Alex-1's 5 boards):
┌────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ [UI 0.7.2 · API 0.7.2]  PROJECT: [■ homestead ▾ (✓ 5/5)] [epaper (✓ 12/12)]  Role: owner  [Ctrl+K] [32px] [☀] │
│ [■ 1. FLEET WORKBENCH (5)]   [■ 2. RELEASES & CI (4)]   [■ 3. ADD BOARD & DOCK]   [■ 4. SECURITY & GOVERNANCE] │
├────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ FLEET STATUS: 5/5 safe (100% Safety) · 3 sketches (coop_door v1.2, garden_soil v2.0, porch_led v1.0 — 0 drift) │
│ Quick Actions: [📥 Drop .bin in Right Inspector to Upload & Deploy]  [+ Create Cohort / Ring]  [⚡ Fast Bench] │
└────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘

(2) TIER 2/3 — MULTI-RING / CI / SWARM / EMCON STATE (With On-Demand `[▼ Expand Matrix]` Drawer from H6):
┌────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ [UI 0.7.2 · API 0.7.2]  PROJECTS: [■ agri-oem ▾ (! 1 ✗)] [valley-ag (✓ 40)] [flux-hub (⊘ EMCON)]   Role: owner │
│ [■ 1. FLEET WORKBENCH (60)]  [■ 2. RELEASES & CI (12)]  [■ 3. ADD BOARD & DOCK]   [■ 4. SECURITY & GOVERNANCE] │
├────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ ┌─ RELEASE PIPELINE & CONVERGENCE: Git main@a1b2c3d (↗ CI #418 ✓tests ✓sig K1) ──► Target: app v1.5.0 ───────┐ │
│ │  ┌───────────────────┐   ┌──────────────────────┐   ┌────────────────────────┐   ┌───────────────────────┐ │ │
│ │  │ 0. DEV / BENCH (4)│──►│ 1. CANARY RING (2)   │──►│ 2. BETA RING (14)      │──►│ 3. PROD RING (40)     │ │ │
│ │  │ ████████ 4/4 ✓    │   │ ████████ 2/2 ✓ (16s) │   │ █████▒▒░✗ 10/14 (!1 ✗) │   │ ░░░░░░░░ 0/40 (v1.4.2)│ │ │
│ │  │ Auto-CD: main ON  │   │ Gate: ✓ selftest ok  │   │ ■ HALTED on beta-s3-04 │   │ 🔒 Locked (Beta halt) │ │ │
│ │  └───────────────────┘   └──────────────────────┘   └────────────────────────┘   └───────────────────────┘ │ │
│ │  ! CIRCUIT BREAKER HALTED WAVE 2: `beta-s3-04` rolled back (`selftest: i2c 0x48 timeout → ota_1`).         │ │
│ │    3 boards staged & grounded (`awaiting_safe_window`).  [▶ Apply 3 Grounded]  [✗ Cancel Rollout]          │ │
│ │  KPIs (30d): Delivery [93.3%] (>=90% ✓) · Fleet Safety [100.0%] (60/60 safe ✓) · [Rollback saves: 4 ▸]     │ │
│ │  Cargo: mcu_fw=v3 (60/60 ✓) · weights=v8 (59/60, !1 skew) │ Secrets: sec:r4 ([! 3 on r3 ▸])  [▼ Matrix Drawer]│
│ │  ┌─ SLIDE-DOWN ARTIFACT, CARGO & SECRET MATRIX DRAWER (Expanded via `[▼ Matrix Drawer]`) ────────────────┐ │ │
│ │  │ ARTIFACT / CARGO          TARGET / SLOT     0. DEV (4)   1. CANARY (2)  2. BETA (14)      3. PROD (40)│ │ │
│ │  │ [app] v1.5.0 (@a1b2c3d)   s3/c6 (56% slot)  4/4 ✓        2/2 ✓ (16s)    10/14 (■ HALT !1) 0/40 (🔒)   │ │ │
│ │  │ [mcu_fw] stm32_coproc:v3  128KB UART        4/4 ✓        2/2 ✓          14/14 ✓           40/40 ✓     │ │ │
│ │  │ [model_weights] yolo:v8   410KB LittleFS    4/4 ✓        2/2 ✓          13/14 (!1 skew)   40/40 ✓     │ │ │
│ │  │ [ff_sec] Secrets rev 4    NVS AES-256-GCM   4/4 ✓        2/2 ✓          14/14 ✓           37/40 (!3)  │ │ │
│ │  └───────────────────────────────────────────────────────────────────────────────────────────────────────┘ │ │
│ └────────────────────────────────────────────────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

#### Wireframe B: 58%/42% Split-Pane Workbench (`Left Zero-Jitter Grid` + `Right Unified Single-View Inspector`)
```
┌───────────────────────────────────────────────────────────────────┬────────────────────────────────────────────┐
│ Filter: [All 60] [! Attn 4] [↺ Active 3] [≠ Drift 3] [! RSSI 2]   │ INSPECTOR: ▶ beta-s3-04 [✎]  (9e417ad42ca4)│
│ Group by: [• None (Flat) | ○ Ring | ○ Parent UGV]  Density:[32px] │ esp32s3 · Map: ab-4m-v1 · Grp: [2. beta ▾] │
│ [x] Freeze sort on hover   [x] Fast Bench (★dev)   [🔍 Search...] │ [★ dev] [⚡ stay awake: 28m left ▾]         │
│                                                                   │ View: [■ A. Triage & Bench] [□ B. 🔌 Dock] │
│ ☑ ST / LINK          DEVICE         FW & CARGO   HEALTH / HEAP    │       [□ C. Compare (4)]   [□ D. Config]   │
│   DEPLOY / VERDICT (Single-Line Verbatim — 0px Shift)             │ ══════════════════════════════════════════ │
│ ═════════════════════════════════════════════════════════════════ │ 1. QUICK UPLOAD / DEPLOY & VERDICT         │
│ ☑ !! SAFE MODE (3×)  ▶rack-s3-01    1.4.2 @9f8   !! SAFE MODE     │ ┌────────────────────────────────────────┐ │
│   ✗ 3× boot panic latch → halted in safe_mode (ota_0)             │ │ [📥 Drop .bin / cargo here] or select: │ │
│ ───────────────────────────────────────────────────────────────── │ │ Target: [app: 1.5.0 (@a1b2c3d ✓K1) ▾]  │ │
│ ☑ █ WIFI -58dBm ▂▄▆█ ▶beta-s3-04★   1.4.2 @9f8   !wdt 62K/14K!    │ │ Slot: [██████░░░░] 1.11/1.97MB (56%)   │ │
│   ✗ selftest: i2c 0x48 timeout → returned_to ota_1 (18s ago)      │ │ [x] Hold at staged  [Deploy 1.5.0 ▸]   │ │
│ ───────────────────────────────────────────────────────────────── │ └────────────────────────────────────────┘ │
│ ☐ !! OVERDUE (+04m)  quad-04 @ugv1  1.5.0 @a1b   last: 138K/92K   │ LAST UPDATE: ✗ ROLLED BACK (18s ago · 34s) │
│   ! Sortie lease expired 4m ago (expected_back_s=900s)            │ • Verdict: 1.5.0 failed selftest; board    │
│ ───────────────────────────────────────────────────────────────── │   auto-rolled back to `ota_1` (1.4.2).     │
│ ☐ █ DOCK @ugv-01     quad-01 @ugv1  1.4.2 !sub   148K/110K ✓      │ • Self-test: ✗ `i2c 0x48 sensor timeout`   │
│   ◷ staged → 1.5.0 (disarmed on carrier dock — ready to Apply)    │ • Sent by: siddharth (CI @a1b2c3d ↗Run)    │
│ ───────────────────────────────────────────────────────────────── │ • Commands: [Apply] [Cancel] [Reboot (r)]  │
│ ☐ ◎ SORTIE ETA 11m   quad-02 @ugv1  1.4.2 +2sub✓ 144K/104K ✓      │             [↶ Rollback to ota_0 (b)]      │
│   ◷ staged → 1.5.0 (in-air safe_window=false · holds until dock)  │ ────────────────────────────────────────── │
│ ───────────────────────────────────────────────────────────────── │ 2. HARDWARE HEALTH & TELEMETRY SPARKLINES  │
│ ☐ ◐ ASLEEP ~12m(30m) epaper-02      1.4.2 !sec3  112K/88K ✓       │ • Reset: [! task_wdt] · Boot: [✓ confirmed]│
│   ◷ queued → 1.5.0 (wakes in ~12m · sec:r3→r4 pending wake)       │ • Faults 24h: task_wdt ×2, panic ×1 (18s)  │
│ ───────────────────────────────────────────────────────────────── │ • Heap: 62 KB free / [! 14 KB min (-48KB)] │
│ ☐ ⊘ VENUE EMCON      pit-node       1.5.0 +3sub✓ 156K/120K ✓      │   free_heap: ▇▇▆▅▄▃▂▁ (leak slope -2.1K/m) │
│   ✓ confirmed 1.5.0 (local ff_selftest pass · 802.11 LOCKED)      │   temp_c:    ▂▃▃▄▅▅▆▆ 44.2 °C · rssi: -58  │
│ ───────────────────────────────────────────────────────────────── │ • Sub-versions: mcu_fw:v3 ✓ · weights:v7 ! │
│ ☐ █ WIFI -82dBm ▂░░░ vine-node-19   1.5.0 +1sub✓ !rssi 134K/96K   │ • Clock: ✓ gateway_ntp · TLS: ✓ ff_cfg CA  │
│   ✓ confirmed 1.5.0 (took 41s · weak signal warning)              │ ────────────────────────────────────────── │
│ ───────────────────────────────────────────────────────────────── │ 3. LIVE & [PREV-BOOT] CRASH LOGS (5,000)   │
│ ▶ ✓ 48 more healthy on-target boards (`j`/`k` to step)            │ [● Net up/log] [○ USB] · OTLP: ✓ 200 OK    │
│   [Page 1 of 1 · Showing 60 of 60 · Cursor limit: 500 ▾]          │ [All] [! Err/Warn] [■ [prev-boot] (4)] [🔍]│
│                                                                   │ ┌─ PINNED [PREV-BOOT] RTC_NOINIT_ATTR (4)┐ │
│                                                                   │ │12:29:04 E [prev-boot] i2c: addr 0x48 TO│ │
│                                                                   │ │12:29:04 E [prev-boot] ff_selftest: FAIL│ │
│                                                                   │ │12:29:05 W [boot] Rolled back -> ota_1  │ │
│                                                                   │ └────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────┴────────────────────────────────────────────┘
```

#### Wireframe C: Multi-Board `[Compare / Diff Failures (4)]` & `[Bulk Actions]` Inspector (`sarah-1..2`, `elena-1..2`)
```
┌─ RIGHT INSPECTOR: 4 BOARDS SELECTED (`rack-s3-01`, `beta-s3-04`, `rack-s3-19`, `rack-c6-08`) ──────────────────┐
│ Sub-View: [■ 1. COMPARE / DIFF FAILURES (4)]   [□ 2. BULK ACTIONS & COHORT ROLLOUT (4)]   [📌 Pin Baseline]    │
│ ══════════════════════════════════════════════════════════════════════════════════════════════════════════════ │
│ PATTERN DETECTED: [Filter: i2c 0x48 timeout (2)] · [Filter: safe_mode OOM (1)] · [Filter: confirm timeout (1)] │
│                                                                                                                │
│ BOARD ID       CHIP     VERDICT & SLOT     SELFTEST / RESET REASON        FREE / MIN HEAP   [PREV-BOOT] CRASH  │
│ ────────────────────────────────────────────────────────────────────────────────────────────────────────────── │
│ rack-s3-01     esp32s3  !! SAFE MODE       reset: panic (3× boot loop)    41 KB / ! 8 KB    Guru Meditation:   │
│                         halted on ota_0    selftest: not reached          ▅▄▃▂▁ (OOM)       LoadProhibited 0x0 │
│ ────────────────────────────────────────────────────────────────────────────────────────────────────────────── │
│ beta-s3-04     esp32s3  ✗ ROLLED BACK      selftest: `i2c 0x48 timeout`   62 KB / ! 14 KB   E (412) i2c: bus 0 │
│                         → returned ota_1   reset: task_wdt                ▇▆▅▄▃▂            addr 0x48 NACK     │
│ ────────────────────────────────────────────────────────────────────────────────────────────────────────────── │
│ rack-s3-19     esp32s3  ✗ ROLLED BACK      selftest: `i2c 0x48 timeout`   64 KB / ! 15 KB   E (415) i2c: bus 0 │
│                         → returned ota_1   reset: sw_cpu_reset            ▇▆▅▄▃▂            addr 0x48 NACK     │
│ ────────────────────────────────────────────────────────────────────────────────────────────────────────────── │
│ rack-c6-08     esp32c6  ✗ ROLLED BACK      confirm timeout (300s window)  118 KB / 94 KB ✓  W (1820) mqtt: TLS │
│                         → returned ota_0   reset: power_on                ▇▇▇▇▇▇            handshake timeout  │
│ ────────────────────────────────────────────────────────────────────────────────────────────────────────────── │
│ BULK RECOVERY ACTIONS: [🔄 Reboot All 4 (R)] [↶ Rollback All 4 (B)] [✗ Cancel In-Flight (C)] [📋 Export (E)]   │
└────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

#### Wireframe D: Full-Width 3-Column `RULE E301` Pit & Carrier Dock Cockpit with Pinned Mini-Fleet Strip (`Tab 3C` / `[⤢ Expand Pit Cockpit]`)
```
┌────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ ██████████████████████████████████████████████████████████████████████████████████████████████████████████████ │
│ █  ⊘ VENUE OFFLINE / EMCON MODE ACTIVE (RULE E301) · [⊘ 802.11: OFF] [⊘ Improv BLE: OFF] [✓ NimBLE: LIVE]    █ │
│ █  [✓ Pit Cache Ready: v1.5.1 + batteries-v15 in IndexedDB]          [☁ 2 Dock Bundles Queued for Replay ▸]  █ │
│ ██████████████████████████████████████████████████████████████████████████████████████████████████████████████ │
│ PINNED LIVE MINI-FLEET & PEER MESH STRIP (Always visible above Pit Cockpit — Zero Mode Blindness!):            │
│ ┌────────────────────────────────────┬────────────────────────────────────┬──────────────────────────────────┐ │
│ │ 1. pit-node (94a9..09a4) [pit]     │ 2. ▶ robot-cart-node (a4cf..ef01)  │ 3. scouting-chief (7b12..c309)   │ │
│ │ ⊘ EMCON · USB-C (2m ago) · boot ok │ █ LIVE USB-C DOCK (/dev/ttyACM0)   │ ⊘ EMCON · LoRa 915MHz (-78 dBm)  │ │
│ │ app: 1.5.0 ✓ · bat:v15 ✓ · sec:r4 ✓│ app: 1.5.0 ✓ · bat:v15 ✓ · sec:r4 ✓│ ! app:1.4.2 SKEW · ! bat:v14 SKEW│ │
│ └────────────────────────────────────┴────────────────────────────────────┴──────────────────────────────────┘ │
├──────────────────────────────────────┬────────────────────────────────────┬────────────────────────────────────┤
│ PANEL 1: AUTO-DRAINED FIELD RINGS    │ PANEL 2: STAGE SIGNED BUNDLE       │ PANEL 3: PERIPHERAL `ff_selftest`  │
│ ════════════════════════════════════ │ ══════════════════════════════════ │ ══════════════════════════════════ │
│ Connected: `robot-cart-node`         │ Safe Window: [✓ IDLE / DISARMED]   │ Last Local Verification (+2s ago): │
│ • `up/log`: ✓ 184 lines drained      │                                    │  [✓ PASS] SX1262 915MHz LoRa SPI   │
│   (0 `[prev-boot]` crashes)          │ (•) [data_partition] batteries-v15 │           (v0x22 · RSSI -74 dBm)   │
│ • `up/telemetry`: ✓ 96 samples       │     14 KB · ✓ ECDSA sig K1         │  [✓ PASS] SN65HVD230 CAN Loopback  │
│   (`12:08:11 [drained +22m14s]`)     │     -> In-place LittleFS (0 reboot)│           (Bus OK · Pack 24.8V)    │
│ • `lora_rssi`: ▆▇▇▆▅▆▇ (-74 dBm)     │                                    │  [✓ PASS] Shared NimBLE GATT       │
│ • `can_vbat`:  ▇▇▇▇▆▆▆ (12.6 V)      │ ( ) [app] v1.5.1-pit-hotfix        │           (`ble_hs_synced() == 1`) │
│                                      │     1.12 MB · ✓ ECDSA sig K1       │                                    │
│ [ ☁ Replay 2 Bundles to Server ]     │     -> A/B Slot + Selftest Confirm │ [ 🔄 Re-Run Local Self-Test ]      │
│ [ 💾 Save `.dock.json` to Disk ]     │                                    │ [ 🔧 Factory Full Flash (Locked🔒)]│
│ └─ Last Receipt: ✓ 2 bundles · OTLP✓ │ [ ▶ PUSH OVER USB-C DOCK (1-Click)]│                                    │
└──────────────────────────────────────┴────────────────────────────────────┴────────────────────────────────────┘
```

---

## 7. Round 2 Multi-Persona Verification Matrix (12 Evaluators' Final Scores & Sign-Offs)

### 7.1 Complete 12-Evaluator Round 2 Verification Table

| Evaluator | Persona & Sub-Profile | Baseline (`v0.7.2`) | R1 Best Score | R2 `H5` Score | R2 `H6` Score | R1 Blockers Resolved | Final Evaluator Verdict & Sign-Off |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :--- |
| **`alex-1`** | P1: Weekend Arduino Homestead Maker (5 boards) | `4.80` | `9.10` (`H4`) | `9.82` | **`9.86`** | **5 / 5 (`100%`)** | **APPROVED (`9.86`)** — Embedded `.bin` dropzone in Inspector + Tier 1 `projectName`-scoped Maker Bar + `[⚡ stay awake: 28m ▾]` + auto-hidden `[B. 🔌 Dock]` when `offline_mode` off. |
| **`alex-2`** | P1: PlatformIO E-Ink & Sleepy-Frame Tinkerer (12 boards) | `5.80` | `9.00` (`H2`) | **`9.96`** | `9.66` | **5 / 5 (`100%`)** | **APPROVED (`9.96`)** — `H5` 4-Tab Split-Pane keeps 11 sleeping frames visible beside `★dev` Inspector, SSE auto-reloads builds, shows `◐ ASLEEP ~12m(30m)`, and adds `[ff_sec]` matrix row. |
| **`elena-1`** | P2: Air-Ground Drone Swarm Flight Commander (25 nodes) | `3.40` | `8.40` (`H3`) | `9.50` | **`9.70`** | **5 / 5 (`100%`)** | **APPROVED (`9.70`)** — 2-Phase `Hold at Staged` $\to$ `[▶ Apply 3 Grounded]`, cargo-aware `deployOutcome`, `H6` Slide-Down Cargo Matrix Drawer, and `H5` In-Inspector `[B. 🔌 Dock]`. |
| **`elena-2`** | P2: Autonomous Carrier-Deck & Data-Mule Lead (21 nodes) | `3.80` | `8.10` (`H2`/`H4`) | **`9.60`** | `9.54` | **5 / 5 (`100%`)** | **APPROVED (`9.90` Hybrid)** — 6-State `StatusCell` (`█ DOCK`, `◎ SORTIE ETA 11m`, `!! OVERDUE`), `Group by: Parent UGV`, `[x] Leapfrog (max 1/parent)`, inline `age_ms` timestamps, and `Clock/CA` health. |
| **`marcus-1`** | P3: AgTech & Commercial Sensor Startup CTO (240 boards) | `4.60` | `8.80` (`H3`) | **`9.88`** | `9.76` | **5 / 5 (`100%`)** | **APPROVED (`9.90` Hybrid)** — `H5` 4-Tab RBAC boundary (`Tab 2 CI` vs `Tab 4 Security`), Multi-Project Health Switcher, permanent `DeployKpiStrip`, and `H6`'s `[▼ Expand Matrix]` toggle. |
| **`marcus-2`** | P3: Factory Provisioning & Field RMA Ops Lead (250+ boards) | `4.70` | `8.80` (`H2`) | **`9.70`** | `9.54` | **6 / 6 (`100%`)** | **APPROVED (`9.70`)** — Batch CLI Token Generator (`N=40` + `tokens-40.csv` & `flash-batch.sh`), Cross-Project RMA `409` transfer, `[□ RMA Archive]`, 2-step `RMA Swap`, and `limit=500` + `[! RSSI]`. |
| **`siddharth-1`** | P4: CI/CD Release Pipeline & Provenance Architect (10 targets) | `4.60` | `8.10` (`H3`) | **`9.84`** | `9.68` | **5 / 5 (`100%`)** | **APPROVED (`9.84`)** — Clickable `[↗ CI #418]` & `main@a1b2c3d ↗`, 10-target release grouping, `Last CD Gate Verdict` + expandable `skipped` dict + `Webhook HTTP 401` alert, and `[📋 Copy CLI Snippet ▾]`. |
| **`siddharth-2`** | P4: Security, Secrets & OTLP Platform Engineer | `4.90` | `8.10` (`H3`) | **`9.78`** *(9.80 wtd)* | `9.58` | **5 / 5 (`100%`)** | **APPROVED (`9.80` wtd)** — ECDSA P-256 2-key ring (`K1`, `K2`) + `[! UNSIGNED]`, `ffa_...` token `TTL`/`enroll`/`last_used_at`/`[Copy]`, clickable `[! 3 on sec:r3 ▸]`, and OTel `[Clear Headers]` + `[Test Ping]`. |
| **`sarah-1`** | P5: High-Throughput HIL Regression Rack Lead (60 boards) | `3.00` | `8.68` (`H2`) | `9.85` | **`9.93`** | **5 / 5 (`100%`)** | **APPROVED (`9.93`)** — `< 1.5s` 0-click verbatim `✗ selftest: i2c 0x48 timeout → ota_1` in `32px` Master Grid, 1-click `[Rollback saves: 4 ▸]` $\to$ `[Compare / Diff (4)]`, and `H6` Slide-Down Matrix. |
| **`sarah-2`** | P5: HIL Soak-Test, Memory-Leak & Crash-Triage Engineer | `3.30` | `8.40` (`H2`) | **`9.80`** | `9.60` | **5 / 5 (`100%`)** | **APPROVED (`9.80`)** — Always-visible sortable `free/min` heap, `!! SAFE MODE (3×)` at Priority `-1`, 1-bit `TelemetrySeries` sparklines (`-2.1K/m`), pinned `[prev-boot]` box, and `[↶ Rollback to ota_0]`. |
| **`chloe-1`** | P6: FRC Pit Crew & Robot Cart Hardware Lead (`CUJ-3`) | `2.80` | `9.66` (`H4`) | `9.72` | **`9.98`** | **5 / 5 (`100%`)** | **APPROVED (`9.98`)** — 1-screen `RULE E301` 3-Column Pit Cockpit + Pinned Live Mini-Fleet Strip, zero-click auto-drain, 3-peripheral `ff_selftest`, `[✓ Pit Cache Ready]`, and locked factory flash. |
| **`chloe-2`** | P6: Contested-RF Scouting Chief & Telemetry Strategist (`CUJ-3`) | `2.40` | `9.60` (`H4`) | `9.76` | **`9.96`** | **5 / 5 (`100%`)** | **APPROVED (`9.96`)** — 3-node 915 MHz LoRa Peer Mesh `sub_versions` Skew Matrix, `H6` Slide-Down `Artifact × Ring × ff_sec` Matrix Drawer, and `dock-sync` Cloud Replay Verification Receipt. |
| **Grand Mean** | **All 12 Evaluators (6 Personas $\times$ 2 Sub-Profiles)** | **`4.08`** | **`8.73`** | **`9.77`** | **`9.73`** | **`61 / 61 (100%)`** | **`9.88 / 10` Converged (`12 / 12 APPROVED`)** |

---

## 8. Final Recommended UI/UX Specification & Prioritized Implementation Roadmap

### 8.1 Canonical Component Architecture Map (`frontend/src/`)

```
frontend/src/
├── index.css                          # Strict 7-step monochrome pixel palette (.stitch/DESIGN.md),
│                                      # .workbench-split (58fr/42fr), fixed 40px/32px rows, [data-theme="pit-contrast"]
├── api.ts                             # Add dockSync(), rollbackDevice(), testOtelPing(), testCiWebhook(),
│                                      # deviceLogs(id, {limit, level, prefix}), listDevices({includeDecommissioned, limit:500}),
│                                      # issueToken({expires_in_days}), safe_mode & parent_device_id fields
├── deploy.ts                          # Fix deployOutcome() for non-'app' cargo (check device.sub_versions[key]);
│                                      # Add 7-tier deterministic devicePriorityScore() (safe_mode = -1, overdue = 0)
├── hooks/useFleet.ts                  # Trigger artifacts.reload() when SSE build/deploy event arrives; support limit=500
├── App.tsx                            # 4-Tab Shell (`1. FLEET WORKBENCH`, `2. RELEASES & CI`, `3. ADD BOARD & DOCK`,
│                                      # `4. SECURITY & GOVERNANCE`), pass `groups={groups}` to FlashBoard & EnrollBoard
└── components/
    ├── ProjectBar.tsx                 # (Extracted from ProjectAccess.tsx) Multi-project health badges (`! 1 ✗`, `⊘ EMCON`),
    │                                  # active RBAC role pill, `[✓ Pit Cache Ready]`, `[32px Rack]`, `[☀ Pit Contrast]`, `[Ctrl+K]`
    ├── ReleaseConvergenceRibbon.tsx   # (New) 3-Tier Progressive Ribbon: Tier 1 Maker Bar (`projectName`-scoped) vs
    │                                  # Tier 2/3 4-Stage Pipeline Rail + DeployKpiStrip (clickable `[Rollback saves: N ▸]`) +
    │                                  # clickable `[! N on sec:r3 ▸]` + H6 Slide-Down `Artifact × Ring × ff_sec` Matrix Drawer
    ├── FleetView.tsx                  # Left 58% Fixed-Height (40px/32px) Master Grid (`0px` shift, `[x] Freeze sort on hover`),
    │                                  # 6-State `StatusCell`, sortable `HEALTH / HEAP` (`free/min` + `!! SAFE MODE`),
    │                                  # verbatim 1-line `DEPLOY / VERDICT` cell (`✗ selftest: <reason> → ota_1`),
    │                                  # `Group by: [None | Ring | Parent UGV]`, search & filter chips (`! RSSI`, `□ RMA Archive`)
    ├── DeviceInspector.tsx            # (New) Right 42% Contextual Inspector Drawer:
    │   ├── UnifiedTriageBenchView     #   [A. Triage & Bench]: Embedded `.bin` dropzone + Precheck + DeployTimeline +
    │   │                              #   DeployResultCard (`returned_to`) + `[↶ Rollback]` + `[⚡ stay awake: 28m ▾]` +
    │   │                              #   Vitals + 1-bit `TelemetrySeries` sparklines + Pinned `[prev-boot]` Crash Box & Logs
    │   ├── DockInspectorView          #   [B. 🔌 Dock]: In-context `RULE E301` Radio Posture + Auto-Drain + `ff_selftest` +
    │   │                              #   1-Click Stage + Peer Mesh `sub_versions` Skew Matrix + `dock-sync` Replay Receipt
    │   ├── CompareBoardsView          #   [C. Compare (N)]: Side-by-side table of 2–6 failed/soak boards with clickable
    │   │                              #   `PATTERN DETECTED` chips, `[📌 Pin Baseline]`, and `[R]/[B]/[C]/[E]` hotkeys
    │   └── BulkActionsView            #   [D. Bulk / Config]: Cohort Rollout (`Hold at staged`, `Pause after Canary`,
    │                                  #   `Leapfrog max 1/parent`, `[▶ Apply Grounded]`), Bulk Reboot/Rollback, & `RMA Swap`
    ├── UploadedBuilds.tsx             # Group multi-target releases by `version @ commit_sha`, render clickable `build_url`
    │   & ContinuousDeploymentCard.tsx # (`[↗ CI #418]`) & `repo_url` (`main@a1b2c3d ↗`), CD Gate Verdict + expandable `skipped`
    │                                  # dict + Webhook error banner, and `[📋 Copy CLI / CI Snippet ▾]` drawer
    ├── FlashBoard.tsx, EnrollBoard.tsx# Pass `groups={groups}`, side-by-side 2-column Flash + `BoardConsole`, Assembly-Line
    │   & PitDockCockpit.tsx           # prefix auto-increment, Cross-Project RMA `409` transfer in `PreflightCard`, Batch CLI
    │                                  # Token Generator (`N=40` + CSV/`flash-batch.sh`), and Tab 3C Full-Width Pit Cockpit
    │                                  # with Pinned Live Mini-Fleet & Peer Mesh Strip
    └── ProjectAccess.tsx              # Role-aware locks (`viewer`/`operator`/`owner`), ECDSA P-256 2-Key Ring (`K1`/`K2`),
        & PartitionProfiles.tsx        # `ffa_...` token `TTL`/`enroll`/`last_used_at`/`[Copy]`, clickable non-converged `ff_sec`
                                       # board list + inline `[Rotate]`, and OTLP `[Clear Headers]` + `[Send Test Ping ⚡]`
```

---

### 8.2 Prioritized Phased Implementation Roadmap (`P0` $\to$ `P1` $\to$ `P2`)

#### Phase `P0` — High-Impact Quick Wins & Bug Fixes (Zero Structural Risk, Immediate Score Lift)
| ID | Target File(s) in `frontend/src/` | Exact Implementation Change | Resolves Persona Pain Points |
| :--- | :--- | :--- | :--- |
| **`P0-1`** | `index.css` (`L16–47`) | Restore strict `.stitch/DESIGN.md` 7-step monochrome pixel palette (`#0c0c0c`, `#141414`, `#3a3a3a`, `#6a6a6a`, `#8a8a8a`, `#d8d8d8`, `#ffffff`), `2px solid` borders, `3px 3px 0 #3a3a3a` tactile button offset shadows, `tabular-nums`, and add `[data-theme="pit-contrast"]` (`21:1` `#000000`/`#ffffff`). | All 12 evaluators (`.stitch/DESIGN.md` compliance + outdoor/arena contrast) |
| **`P0-2`** | `deploy.ts` (`L153–175`) | Make `deployOutcome()` **cargo-aware**: when `d.artifact_type && d.artifact_type !== 'app'`, compare `d.artifact_version` against `device.sub_versions?.[d.sub_version_key]` instead of `device.fw_version`. | `elena-1..2`, `chloe-1..2`, `siddharth-1` (fixes false `drift` after cargo deploy) |
| **`P0-3`** | `FleetView.tsx` (`L161–171`, `L266`) | Hoist `<DeployKpiStrip>` out of the collapsible `<details>` drawer into the permanent top header; make `[Rollback saves: N ▸]` a clickable filter chip; scope version drift by `device.app_name` (`appImage.projectName`) or cohort ring when `groups.length === 0`. | `alex-1` (stops false drift on heterogeneous sketches), `sarah-1`, `marcus-1..2` |
| **`P0-4`** | `App.tsx` (`L219–225`), `FlashBoard.tsx`, `EnrollBoard.tsx`, `UploadBuild.tsx` (`L290`) | Pass `groups={groups}` to `FlashBoard` and `EnrollBoard` so new boards can join a cohort ring during onboarding; remove `size={Math.min(artifacts.length, 4)}` from the `<select>` in `UploadBuild.tsx` (`L290`); auto-suggest next patch version (`1.0.4 → 1.0.5`) if `.bin` version matches an existing build. | `alex-1..2`, `marcus-1..2` (fixes Flow 1 group assignment & 45px dropdown bug) |
| **`P0-5`** | `hooks/useFleet.ts` (`L56–88`) | Trigger `artifacts.reload()` when SSE events indicate a new build or CI auto-deploy; raise `listDevices` limit from `200` to `500`. | `alex-2`, `siddharth-1`, `marcus-2` |
| **`P0-6`** | `api.ts`, `DeviceLog.tsx`, `FleetView.tsx` | Wire `safe_mode: boolean` (`R5-fw-6`), `POST /v1/devices/{id}/rollback` (`rollbackDevice`), and `GET /v1/devices/{id}/logs?level=&prefix=&limit=500` in `api.ts`; pin `[prev-boot]` `RTC_NOINIT_ATTR` crash lines in a dedicated top callout box inside `DeviceLog`. | `sarah-1..2`, `elena-1..2`, `alex-1..2` (prevents 200-line slice from evicting crashes) |
| **`P0-7`** | `UploadedBuilds.tsx`, `ContinuousDeploymentCard.tsx`, `ProjectAccess.tsx` | Render `a.build_url` (`[↗ CI Run]`) and `a.repo_url` (`branch@sha ↗`) as clickable links; display `res.skipped` per-device reasons and `ci_webhook_triggered === false` error banner; add `expires_in_days` (`TTL`), `enroll` scope checkbox, `last_used_at` / `expires_at` columns, `[Copy]` button, and OTLP `[Clear Headers]` (`otel_headers: {}`) in `ProjectAccess.tsx`. | `siddharth-1..2`, `marcus-1..2` |

#### Phase `P1` — Structural Split-Pane Workbench, Multi-Board Diff & Release Pipeline Ribbon
| ID | Target File(s) in `frontend/src/` | Exact Implementation Change | Resolves Persona Pain Points |
| :--- | :--- | :--- | :--- |
| **`P1-1`** | `FleetView.tsx`, `DeviceInspector.tsx` *(new)*, `index.css` | Replace inline `<tr className="row-detail">` expansion and bottom-of-page `DeviceLog` with `.workbench-split` (`58%` fixed `40px`/`32px` Master Grid + `42%` pinned `DeviceInspector.tsx`; responsive drawer stacking below `1100px`). Add `[x] Freeze sort on hover`, `j`/`k` keyboard stepping, always-visible sortable `HEALTH / HEAP` (`148K/110K ✓`, `!wdt 62K/14K!`, `!! SAFE MODE`), and verbatim 1-line `DEPLOY / VERDICT` exception cells (`✗ selftest: <reason> → returned_to ota_1`). | `sarah-1..2` (`< 1.5s` 0-click scan, `0 px` layout shift), `alex-1..2`, `marcus-1..2`, `elena-1..2` |
| **`P1-2`** | `DeviceInspector.tsx` (`UnifiedTriageBenchView`) | Implement Mode **`[A. Triage & Bench]`**: single continuous vertical view containing (1) Embedded `.bin`/Cargo Drag-and-Drop Uploader + inline 1-click `[Adopt flash map]` + `PrecheckCard` + `DeployTimeline` + `DeployResultCard` (`returned_to` + `Sent by`) + `[Apply] [Cancel] [Reboot (r)] [↶ Rollback (b)]` + bounded `[⚡ stay awake: 28m left ▾]` lease; (2) Vitals + 1-bit `TelemetrySeries` sparklines (`▁▂▃▄▅▆▇█` wired to `api.deviceTelemetry`) + `ff_cfg` vs `ff_net` SSID provenance + `[Provision Wi-Fi via Improv BLE 📡]`; (3) Server-filtered `DeviceLog` + pinned `[prev-boot]` crash box. | Eliminates Inspector sub-tab ping-pong across all 12 evaluators |
| **`P1-3`** | `DeviceInspector.tsx` (`CompareBoardsView` & `BulkActionsView`) | Implement Mode **`[C. Compare / Diff (N)]`** (side-by-side table of 2–6 selected/rolled-back boards across `VERDICT & SLOT`, `SELFTEST / RESET`, `FREE / MIN HEAP` sparklines, and `[PREV-BOOT] CRASH`, with clickable `PATTERN DETECTED` filter chips, `[📌 Pin Baseline]`, and `[R]/[B]/[C]/[E]` hotkeys) and Mode **`[D. Bulk / Config]`** (`Hold at Staged [on_command]`, `Pause after Canary`, `Leapfrog max 1/parent`, `[▶ Apply Grounded]`, Bulk `Reboot`/`Rollback`/`Cancel`, and 2-step `Decommission & Replace (RMA Swap)`). | `sarah-1..2`, `elena-1..2`, `marcus-1..2` |
| **`P1-4`** | `ReleaseConvergenceRibbon.tsx` *(new)*, `ProjectBar.tsx` *(new)* | Build the Multi-Project Health Switcher Bar (`ProjectBar.tsx` with per-project status pills & role indicator) and the 3-Tier Progressive `ReleaseConvergenceRibbon.tsx` (Tier 1 Maker Bar vs. Tier 2/3 4-Stage Pipeline Rail `0. Dev -> 1. Canary -> 2. Beta -> 3. Prod` + Circuit Breaker + `DeployKpiStrip` + clickable `[! N on sec:r3 ▸]` + `H6`'s Slide-Down `Artifact × Ring × ff_sec` Matrix Drawer). | `marcus-1..2`, `siddharth-1..2`, `elena-1..2`, `alex-1..2`, `chloe-1..2` |
| **`P1-5`** | `UploadedBuilds.tsx`, `ProjectAccess.tsx` | Group multi-target CI builds by `version @ commit_sha` with per-chip target/slot-fit pills (`[esp32s3 ✓ 56%] [esp32c6 ✓ 52%]`); add `[📋 Copy CLI / CI Snippet ▾]` drawer; add ECDSA P-256 2-Key Ring (`Active K1`, `Rotation K2`, `[! UNSIGNED]` badge), role-aware UI locks (`viewer`/`operator`/`owner`), and `[Send Test OTel Ping ⚡]`. | `siddharth-1..2`, `marcus-1..2`, `sarah-1` |

#### Phase `P2` — Contested-Venue `RULE E301` Pit Dock, Swarm Presence Taxonomy & Factory Batch Jig
| ID | Target File(s) in `frontend/src/` | Exact Implementation Change | Resolves Persona Pain Points |
| :--- | :--- | :--- | :--- |
| **`P2-1`** | `PitDockCockpit.tsx` *(new)*, `DeviceInspector.tsx` (`DockInspectorView`), `api.ts` | Wire `POST /v1/devices/{id}/dock-sync` (`api.dockSync`) and Web Serial `ff_ota_dock_stage` / ring drain. Render **both** In-Inspector Mode `[B. 🔌 Dock]` on Tab 1 (auto-hidden when no board uses `link_type: "dock"` or `offline_mode`) **and** Tab 3C Full-Width 3-Column `PitDockCockpit.tsx` topped by `H6`'s **Pinned Live Mini-Fleet & Peer Mesh Strip**: 3-pill `RULE E301` radio posture (`802.11 OFF`, `Improv BLE OFF`, `Shared NimBLE LIVE`), zero-click auto-drain (`up/log` + `age_ms` `up/telemetry` with inline dual timestamps), 3-peripheral `ff_selftest` (`SX1262 LoRa`, `SN65HVD230 CAN`, `NimBLE`), `IndexedDB` Pit Cache (`[✓ Pit Cache Ready]`), 3-Node Peer Mesh `sub_versions` Skew Matrix, and `dock-sync` Replay Verification Receipt. | `chloe-1..2` (`9.98 / 10` & `9.96 / 10`), `elena-1..2`, `marcus-2` |
| **`P2-2`** | `FleetView.tsx`, `deploy.ts` | Implement the **6-State Presence, Sleep, Sortie & EMCON Taxonomy (`StatusCell`)**: `█ WIFI (-58dBm ▂▄▆█)`, `█ DOCK (@ugv-01 / USB-C)`, `◐ ASLEEP ~12m(30m)`, `◎ SORTIE ETA 11m (@ugv-01)`, `⊘ VENUE EMCON · 802.11 LOCKED` (inverse pill), and `!! OVERDUE (+04m past lease)` (Priority Score `0`), plus `Group by: [None | Ring | Parent UGV]` (`parent_device_id`) and parent safe-window inhibit reasons (`◷ held: child quad-09 on approach`). | `elena-1..2`, `alex-2`, `chloe-1..2` |
| **`P2-3`** | `FlashBoard.tsx`, `EnrollBoard.tsx`, `PreflightCard.tsx` | Refactor Tab 3 (`3. ADD BOARD & DOCK`) into a 2-column layout (4-step Web Serial config on left, `BoardConsole` on right) with Assembly-Line `Name Prefix` auto-increment (`gh-node-01..40`), Cross-Project RMA `409` transfer in `PreflightCard`, `[□ RMA Archive]` filter (`?include_decommissioned=true`), and Mode **`[B. BATCH CLI TOKEN & FACTORY JIG (N=40)]`** (`Mint N Single-Use Tokens` + 1-click `tokens-40.csv` & `flash-batch.sh` download). | `marcus-1..2`, `alex-1..2` |
