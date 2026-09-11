# Decisions

Append-only log of product/technical decisions and learnings. Newest first.
Each entry: what was decided, why, and where the details live. Never rewrite
history — supersede an old decision with a new entry that references it.

---

## 2026-09-11 — the console resets the board itself so the boot is never missed (S0-fe-5)

The panel's automatic reset is not a fix for a dropped session — it is the *designed
behaviour*. The flasher and the console are separate sessions at different bauds, and the
window between `hard_reset` and the console opening at 115200 is long enough (up to 8 s on
native-USB parts) to lose the entire boot. So every `watch()` pulses EN once, turning "the
board was silent when we arrived" into "the board prints its first line while we are
listening".

- **The pulse happens on every path that opens the port, automatic or manual.** A board
  watched five minutes after flash has the same silence; a purely-automatic pulse would leave
  the manual case (click **Watch a board** without flashing first) broken. Both paths pulse.
- **`commandedReset` travels in-band as an event property, not as a second argument.** The
  reboot-loop suppression depends on knowing which boot the panel asked for.
  `summarizeConsole` is pure over `events` alone (every test builds summaries from arrays,
  the hook memoises on `[events, now]`), so the flag rides in the event.
- **Order is load-bearing in `watch()`: append notice synchronously, attach the reader
  immediately, let the 150 ms EN pulse run concurrently.** Awaiting `session.reboot()` first
  would leave the port unread for 150 ms—Chromium's default 255-byte read buffer is ~22 ms
  at 115200 baud, so the panel can overrun. Appending the notice *after* the pulse makes its
  position in the stream racy, breaking the one-boot-deep suppression.
- **A failed pulse degrades to a notice, never to an error.** `setSignals` can be
  unsupported or wired differently. The operator sees a log region with "could not reset the
  board. Press 'Reboot the board'", never a red fault panel.
- **Native-USB re-enumeration is acknowledged but not solved here.** C3/C6/S3 drop off the
  bus on reset and return as a new `SerialPort`. The disconnect message now says "dropped off
  the USB bus when reset — some boards re-enumerate. Press 'Watch a board' to pick it up
  again." Automatic re-acquire is S0-test-2, blocked on hardware.
- **Deliberate residual: a board mid-OTA-download gets restarted.** The panel has no way to
  know an OTA is in progress (the agent prints no such line), and waiting to find out would
  recreate the silence problem. Acceptable at R0 (the console is a bench tool, and an
  interrupted OTA is discarded rather than committed). Documented so it's not later reported
  as a mystery.
- **Gotcha for the next console change: the summary is now a function of the clock.**
  `useBoardConsole` ticks once a second while watching (not idle). In tests, `vi.useFakeTimers()`
  + `vi.advanceTimersByTimeAsync()` inside `act()` drives both the tick and `Date.now`, which
  is the only way to test a deadline with no new line arriving.

Details: `docs/features/enrollment.md` → *The boot appears automatically (S0-fe-5)*.

## 2026-09-11 — a milestone is a claim about now; a fault is a record of what happened (S0-fe-4)

Implements the first layer of the standard set by the entry below. Three things were
decided while fixing it that are not obvious from the task text.

- **The parser must match the whole line, not the message.** `hintFor` was keyed on the
  ESP-IDF tag, which is why `E BOD:` was invisible. The fix could have been "add a rule for
  tagless lines"; instead every bare rule is matched against the **cleaned whole line**, so
  the same rule fires whether ESP-IDF prints `E BOD: …` early-boot style or `E (403) BOD: …`
  through the normal logger. A classifier keyed on a *format* is what broke; keying the
  content match on the format again would rebuild the same trap one layer down.
- **`reached` is cleared by a reboot and `fault` is not, and that asymmetry is the point.**
  A milestone is a positive claim that must be true *now* — a stale ✓ actively misdirects,
  which is precisely what cost the bench session. A fault is a description of something that
  happened; the reset it caused does not make it untrue, and a panic prints its cause
  immediately *before* the reset that would otherwise erase it. So progress clears a fault
  (unchanged) but a boot boundary does not.
- **"Twice is proof of a loop" needed a second clause, or the happy path cries wolf.** The
  filed task said seeing `boot` twice proves a reset loop. Literally true on a stranded
  board, false the moment the operator presses **Reboot the board** on a healthy one — and
  S0-fe-5 is about to make the panel reboot boards by itself. `rebootLoop` is therefore
  raised only when a boot begins while the *previous* boot had not reached the fleet, and is
  cleared by one that does. Same detection on the failure case, silent on the success case.
- **A generic hint is the right answer more often than it looks.** `Backtrace:` and
  `SW_CPU_RESET` both *look* like specific diagnoses and are both consequences printed after
  the line that actually names the cause. Classifying them `kind: 'generic'` reuses the
  existing "never overwrite a named cause" rule instead of adding ordering logic. Worth
  reaching for whenever a line is real but derivative.
- **Deadlines are grounded in the agent's own constants, not chosen.** `NET_TIMEOUT_MS` 30 s,
  `SNTP_TIMEOUT_MS` 15 s, `ENROLL_TIMEOUT_MS` 30 s, each plus room for one retry, and each
  measured from the *previous* milestone. A deadline shorter than the board's own patience
  would report a fault the board has not had yet. The numbers are not in `spec/prd.md`'s
  targets table; proposed for it rather than written there.
- **Gotcha for whoever tests the next layer:** the summary is now a function of the clock, so
  `useBoardConsole` ticks once a second **while watching only** — an idle panel must not
  re-render forever. In jsdom, `vi.advanceTimersByTimeAsync` inside `act()` drives both the
  tick and `Date.now`, which is the only way to test a stall that arrives with no new line.
- **The bench log is a reconstruction, and the fixture says so in its header.** The real
  capture was pasted into a chat and never committed. That is not a documentation lapse to
  be tidied up; it is the exact failure S0-fe-7 exists to remove, so it is recorded rather
  than glossed.

Details: `docs/features/enrollment.md` → *The console always names a diagnosis (S0-fe-4)*.

## 2026-09-11 — the console panel is the diagnostic surface of record for onboarding

The first hardware bench found no server bug and three onboarding bugs. A DevKit v1
brownouts during Wi-Fi PHY calibration and resets forever; it printed `E BOD: Brownout
detector was triggered` on every cycle, and the panel showed a stale green **Network
up** instead. The fault was found by pasting a UART log into a chat window.

Nothing server-side could have helped — a board that never associates is invisible to
`device_progress` by construction, which `progress.py` already says out loud. The
board's UART is the only witness and the browser is the only listener. So the panel,
not the server, is held responsible for explaining everything between "flashed" and "on
the fleet", against a technician who does not know what a brownout is. Anything the
board says that the panel cannot explain is now a defect.

Gates R0, because R0's stated risk *is* onboarding. Full reasoning and the rejected
alternative (structured faults from firmware) in
`design/decisions/enrollment-console-is-the-diagnostic-surface.md`; requirements in
`spec/standards.md`; the parser-versus-firmware trade and the missing CUJs in
`spec/open-questions.md`.

## 2026-09-11 — `arrivals` needed a second clause, not a tweak (S0-fe-3)

Closes the item filed at the end of the S0-fw-1 entry below. The question was whether "not
currently online" should also exclude boards that have *been* in the fleet, and the answer
is yes — but the interesting part is what the second clause had to be made of.

- **"Not online" was never the right question; "has this board already arrived?" is.**
  A board on its way up and a board that came up an hour ago and lost power are both
  offline with a recent stage. One clause cannot separate them, and the symptom was a
  completed arrival re-entering the list labelled *stalled at `mqtt_connected`* for the
  rest of the 900 s window, duplicating an offline row directly above it. The new predicate
  `progress.has_already_arrived` asks the second question, and it lives in `progress.py`
  next to `stalled` rather than inline in the router, so there is one place that decides it.
- **`broker_provisioned_at`, not `enrolled_at`.** The task's suggested shape named both.
  `enrolled_at` is `NOT NULL` with a default, so testing it decides nothing — a conjunct
  that is always true reads like a safeguard and is not one. The provisioning timestamp is
  the real end of the arrival sequence.
- **The re-flash case works for free because `last_seen` is monotonic and re-enrolment does
  not touch it.** Two decisions made elsewhere and for other reasons — `registry.py` leaving
  `last_seen` alone on re-enrol, `ingestor/store.py` advancing it with `GREATEST` — mean a
  re-flashed board's fresh stages are *necessarily* newer than its stale `last_seen`. So
  `stage_at <= last_seen` distinguishes "this stage belongs to the arrival that already
  finished" from "this board is arriving again" without a re-flash flag, a generation
  counter or a new column. **Worth noticing as a pattern: when a new rule needs to tell two
  situations apart, check whether an existing monotonic timestamp already does it.**
- **A deliberate residual, so nobody reports it as a regression.** A board that reports
  `mqtt_connected` and dies before any live message advances `last_seen` past that report
  still reads as arriving. It never completed a heartbeat. Fixing it would require deciding
  how many messages count as "arrived", which is a worse rule than the honest edge.
- **The live vacuity check found nothing but is why the evidence is trustworthy.** Neutering
  the clause in the running api reproduced the original duplicate row verbatim against the
  same database state that had just shown `arrivals: []`. That is a stronger statement than
  a passing test, because it proves the empty list came from the rule rather than from the
  progress window having quietly expired.

Details: `docs/features/enrollment.md` → *An arrival that finished stops arriving (S0-fe-3)*.

## 2026-09-11 — the stage reporter has now run on a board, and one acceptance was wrong (S0-fw-1)

Supersedes the 2026-09-10 S0-fw-1 entry below, which recorded the server half and said
`ff_progress.c` had exactly one guarantee: that it compiles. It has now been executed.
The design decisions in that entry all stand; these are what running it added.

- **Acceptance 1 asked for something the feature does not claim, and that is a spec bug,
  not a test bug.** "A board flashed with a deliberately wrong PSK shows a stalled stage
  rather than nothing at all" — but a wrong PSK means no link, and `ff_progress.h`'s
  header already states that a board with no route reports nothing. The criterion and the
  interface contradicted each other, and the interface is right. Owner-confirmed
  substitution: the two cases the feature *does* claim, a board stalled at `enrolling`
  (broker down, so `/v1/enroll` 503s on provisioning) and one stalled at `mqtt_refused`
  (credential rotated out from under it). Both now pass, driven by `ff_progress.c` rather
  than by curl. **When an acceptance and an interface disagree, check which one was
  written after the thing was understood.**
- **The invisible case is real, measured, and belongs in the operator's head.** A board
  whose token is refused reports *nothing*: the progress 401 self-disables the reporter,
  so the `halted` that `park()` tries to send never leaves the board. Verified with a
  revoked token — zero rows, silent dashboard, and only the `ff-progress` warning on the
  console. This is the designed behaviour and it is also the feature's ceiling: [[S0-fe-1]]
  (serial) and this task cover disjoint failures, and neither is a substitute for the other.
- **`progress_stall_s` (60 s) and `ENROLL_RETRY_MIN_MS` (60 s) are the same number, so a
  freshly-stalled board flickers.** The row alternates `stalled=false/true` for the first
  couple of minutes and only settles once the backoff has doubled past the threshold.
  Observed, not theorised. Left alone rather than tuned: the flicker is honest (the board
  *is* alternating between reporting and waiting) and changing either constant to fix a
  cosmetic wobble would trade a real property for a UI one. Worth knowing before someone
  reports it as a bug.
- **A trap that cost a full round of acceptance evidence:
  `docker ps --filter ancestor=espressif/idf:v5.5.5` matches nothing when the image is
  digest-pinned.** The filter compares the reference you typed, not the image the container
  runs, so it exits 0 having killed nothing — indistinguishable from "no emulators are
  running". Killing the `just` process does not help either: without `-it`, `docker run`
  leaves the container alive. Six emulators accumulated, all claiming `000000000000`, all
  enrolling and heartbeating over each other, and the first set of results had to be
  thrown away. Fixed at the source rather than in a doc: `agent-qemu` names its container
  `ff-qemu-<target>` and **refuses to start a second one**, and `agent-qemu-stop` exists.
  Vacuity-checked both ways. **Apply the general rule: if a cleanup command can fail
  silently, the thing it cleans up needs a name.**
- **A board is visible as `arriving` before it exists in the fleet at all** — the
  `link_up` arrival lands before any `devices` row does. That is the whole point of the
  feature and it is worth stating as an observed fact rather than an intent.
- **Filed, not fixed: an offline board reappears in `arrivals`.** Once presence decays,
  a board that got all the way to `mqtt_connected` and then died shows up as "arriving,
  stalled at `mqtt_connected`" for the rest of the 900 s window, duplicating a row the
  fleet list already shows as offline. The `arrivals` rule (recent stage + not online) is
  doing exactly what it says; whether "not online" should also exclude boards that have
  *been* in the fleet is a dashboard decision, so it is **S0-fe-3** rather than a quiet
  change here.

Details: `docs/features/enrollment.md` → *Boot & enrol stage reports (S0-fw-1)*,
`docs/runbooks/agent-qemu.md` → *Reproducing a board that gets partway*.

---

## 2026-09-11 — the QEMU harness was never broken; it was unrunnable and unverifiable (S0-infra-1)

- **The filed root cause was wrong and the named suspect is innocent.** S0-infra-1
  reported a `LoadProhibited` boot loop inside `esp_task_wdt_init` and suspected the
  `-global driver=timer.esp32.timg,property=wdt_disable,value=true` flag. It did not
  reproduce: a full run from a wiped flash image and a fresh token reached `enroll 200`
  → `mqtt connected` → heartbeats, and five further boots — three under eight busy-loops
  on a four-core box — produced zero panics. Every one of those runs carries the flag.
  **Recorded so nobody re-decodes that backtrace**: `docs/runbooks/agent-qemu.md` →
  *What we know about the boot-loop panic*.
- **`docker run -it` in a recipe is a bug, not a convenience.** `agent-qemu` passed it
  unconditionally, so the recipe died with "cannot attach stdin to a TTY-enabled
  container" in every agent session, script and CI shell — i.e. it could not be run by
  the things that most need to run it, including the acceptance criterion of the task
  filed against it. Whoever hits that hand-rolls a `docker run`, and a hand-rolled
  emulator invocation is where a wrong `-M`/`-m`/`-global` and an inexplicable watchdog
  panic come from. This is the most probable origin of the reported backtrace. `-it` is
  now conditional on `[ -t 0 ]`. **Apply this to any recipe that shells into a
  container.**
- **A digest pin does not pin what you run. Docker verifies a digest on `pull`, not on
  `run`.** A damaged or replaced local layer is used in silence, and
  `qemu-system-xtensa` lives inside the pinned image — which was in fact *absent* from
  this box's store when the investigation began and had to be re-pulled, with `/` at
  85%. Since every other input to a boot is content-addressed (bundle sha256s, IDF's own
  `default_efuse` bytes, `esptool merge_bin` over manifest offsets), identical declared
  inputs produced different behaviour, so one input was not what it claimed. New
  `qemu_sha256` hashes the emulator **binary** inside the container before every boot and
  prints its version into every transcript. Limit stated rather than papered over: one
  binary is not the whole image.
- **The two QEMU recipes splice one `qemu_program` definition.** A smoke check that
  assembles its own machine guards a lookalike, not the recipe. Everything that varies
  arrives as an environment variable so the argv cannot drift.
- **`just agent-qemu-smoke` — the cheap answer to "is the harness alive?"** ~17 s, no
  token, no stack, no board. Asserts only the first seconds, most-specific first: no
  panic, exactly one ROM `rst:0x` banner, the `ff-agent` banner, `ff_cfg v1 loaded`. It
  deliberately proves nothing about enrolment or MQTT. **This task cost a decoded
  backtrace and a blocked firmware task to answer a question worth seventeen seconds.**
  Vacuity-checked both ways: a wrong `qemu_sha256` trips the integrity guard, and 256
  scribbled bytes in `app.bin` produce a real loop — `the board reset 27 times`.
- **Consequence for [[S0-fw-1]]: it is unblocked, and its firmware has already run.** The
  bundle in `agent/dist/esp32` is the S0-fw-1 build (`ff_progress` in `app.bin`,
  `source_commit f81d6f1` + dirty tree) and it boots and enrols. The two firmware
  acceptances it could not reach are now executable on this box.

Details: `docs/features/infrastructure.md` → *The QEMU harness, re-verified*,
`docs/runbooks/agent-qemu.md`.

---

## 2026-09-10 — the theme is monochrome, so every state encodes twice (S0-fe-2)

- **One hue ramp means colour is no longer available as a carrier of meaning, and that is
  a functional consequence rather than a stylistic one.** On this palette green and red
  are the same grey. So `.ok` and `.bad` both go bright + bold and `.bad` additionally
  underlines; `.warn` goes body-weight + bold; `.muted` stays dim + normal. Every state
  in the app now differs from its alternative on at least two of {glyph, weight,
  lightness}. WCAG 1.4.1.
- **The audit came before the restyle, and it is why this touched one component.** Every
  `.ok`/`.bad`/`.warn`/`.muted` site was checked for whether colour was its *only*
  carrier. Nearly all already carried their own text — "active"/"used", "Live", full
  sentences, the checklist's `✓`/`…`/`·`, and log lines that begin with ESP-IDF's own
  `E (…)`/`W (…)`/`I (…)`. Exactly one was colour-alone: `FleetView`'s `StatusCell`,
  which drew the same `●` for online and offline. It is now `█` vs `░`. If a future
  change introduces a second such site, the audit — not the palette — is what has to be
  re-run.
- **`.log .bad` cancels the underline that `.bad` carries everywhere else.** Inside a log
  panel the level is already in the text, so the underline adds no information and
  destroys the monospace grid. A rule that exists to be *absent* in one place is worth
  the two lines of comment it has.
- **Two font stacks, and the split is load-bearing.** Press Start 2P is confined to short
  chrome (`h1`/`h2`/`h3`/`th`/`legend`/`button`); device ids, table data, inputs and both
  log panels stay on a real monospace at full size. The task's constraint was that a
  pixel font must not cost the legibility of the two things operators actually read, and
  a scoped display face is how that is *met* rather than hoped for. Do not unify them: a
  12-hex device id in Press Start 2P is ~2.5x wider and the face has no bold, so the log
  panel would lose its weight ramp too.
- **The font is self-hosted, latin subset only.** `@fontsource/press-start-2p`, 12 KB
  woff2, bundled by Vite and verified present in the production nginx image. A Google
  Fonts `<link>` would have been fewer characters and would have added a third-party
  fetch, a CSP consideration and a hard dependency on the box having a route out — for a
  single-tenant appliance whose V2 promise is self-hosting, that is the wrong trade.
- **The greyscale check runs as a live CSS filter on the page, not as a post-hoc PNG
  conversion.** Converting the screenshot only proves the screenshot is grey. Filtering
  at render time also catches anything that would have reintroduced hue — an accent, an
  emoji, a form control drawn by the UA. It passes trivially today precisely because the
  palette is achromatic, and that triviality is the point.
- **Gotcha, cost twenty minutes: a Docker named volume seeds from the image exactly
  once.** After adding the font to `frontend/package.json`, `just rebuild frontend` built
  a correct image that the stale `ff_node_modules` volume then masked, and Vite reported
  `Failed to resolve import` for a package plainly installed on the host. The volume must
  be *deleted*. The override file's comment said "rebuild after changing package.json",
  which is true and insufficient; it now carries the four-line recipe.
- **The frontend test suite cannot regress on a pure restyle, and it is worth knowing
  why.** Only `main.tsx` imports `index.css` and vitest never renders styles, so CSS is
  invisible to the 108 tests. The `StatusCell` glyph was the one change with any reach,
  and nothing asserts on it — the tests assert roles and labels. A restyle that *does*
  break a test has changed the markup more than it meant to.

Details: `docs/features/dashboard.md` → S0-fe-2 (new capability area, registered in
`docs/roadmap.md`). T2 harness: `frontend/scripts/theme-shots.mjs`.

---

## 2026-09-10 — a boot stage is reported over HTTPS under the enrollment token (S0-fw-1, attempted)

**Status: the server half shipped and is verified; the firmware half is written, compiles
and has never run.** S0-fw-1 stays `- [!]` in TODO.md for that reason. Read the last bullet
before trusting `agent/main/ff_progress.c`.

- **PROPOSED protocol addition, deliberately NOT written into `spec/`.** A board between
  "flashed" and "online" holds no MQTT credential — that is the thing it is trying to
  obtain — so a stage report cannot travel on MQTT. The only credential it has is the
  `ffe_` enrollment token it was flashed with, and the only channel is the HTTPS one
  `/v1/enroll` already uses. Hence `POST /v1/device-progress`, token in the body, `202`.
  `spec/device-protocol.md` is the frozen v1 wire contract and stays frozen until this is
  accepted; the endpoint's module docstring says so at the top so the two cannot silently
  diverge in the reader's head.
- **The token is verified and never burned, and that distinction is the whole security
  argument.** `auth.enrollment.BURN_SQL` is not imported by the progress router and must
  never be: a board reports `enrolling` several times before it succeeds, and a reporting
  path that spent tokens would turn a debugging aid into a way to strand boards.
- **An already-burned token is still accepted, but only from `used_by_device_id` and only
  before `expires_at`.** Without that exception the two most valuable stages — `enrolled`
  and `mqtt_connected`, which by definition happen *after* the burn — could never be
  reported at all. It widens nothing: the predicate names one device, and the endpoint
  issues no credential, provisions nothing and writes no `devices` row.
- **`stalled` is derived on read, never stored** (`progress_stall_s`, 60 s), in one
  function, exactly as `online` is derived by `presence.is_online`. A stored `stalled`
  would need a sweeper and would be wrong between sweeps.
- **`arrivals` rides on `GET /v1/devices` instead of getting an endpoint.** The dashboard
  already re-reads that on every `ff_events` hint, so arriving boards cost no second fetch
  and no poll. Arrivals are boards with a recent stage that are **not currently online**,
  decided by the same `is_online` call that fills the rows above them — so a board leaves
  the arriving list at the exact moment it really joins the fleet.
- **The table is bounded on write, not by a sweeper**: 20 rows per device, trimmed in the
  same transaction as the insert. A board retrying enrolment every 60 s reports forever,
  and a debugging table must not be able to outgrow the fleet it describes. Known bound,
  accepted: an unspent valid token can name any `device_id`, so it can seed rows for
  arbitrary ids at the rate limiter's ceiling — small rows, 24 h token TTL, single-tenant.
- **No PG enum and no CHECK on `stage`.** The R0 agent is flash-baked; the server must
  tolerate an agent it can never update, including one that invents a stage. The API
  bounds the string's *shape* (`^[a-z][a-z0-9_]{0,31}$`, no control characters in
  `detail`), never its vocabulary. `ProgressStage` in `db/models.py` is advisory, and
  `FF_PROGRESS_*` in `ff_progress.h` are plain strings for the same reason. Verified: an
  unknown stage (`teleported`) is stored and rendered as itself.
- **The SSE frame carries no `detail`.** `detail` is device-controlled free text; the
  event is a hint and the client re-reads, as for every other event type.
- **The honest limit is designed for, not around: a board with no route to the server
  reports nothing.** Stated in `ff_progress.h`'s header so nobody builds on a promise it
  cannot keep. This is for boards that get *partway*, and never a substitute for the
  serial console ([[S0-fe-1]]).
- **The firmware could not be run, and the harness is why.** `just agent-qemu esp32`
  boot-loops on a `LoadProhibited` panic inside `esp_task_wdt_init` before `app_main` —
  **reproduced at unmodified HEAD**, so it is not this change. The Mac is the flashing
  bench, so there is currently no way to execute agent firmware on this box at all. Filed
  as **S0-infra-1** with the decoded backtrace. Until it is fixed, `ff_progress.c` has
  exactly one guarantee: it compiles under `-Wall -Wextra -Werror`.

Details: TODO.md → S0-fw-1 (`- [!]`) and S0-infra-1.

---

## 2026-09-10 — the board's console belongs in the browser, as a second session (S0-fe-1)

- **`flash.ts`'s Rule 4 stays: the port is always released in a `finally`.** The task was
  filed as "keep the serial port after flashing", and that framing is wrong. esptool-js's
  `Transport` owns the port at the *flash* baud, which is not the console baud, so a held
  flasher would have to be reconfigured anyway — and unwinding the `finally` would leak a
  port on every error path in a file whose whole discipline is that it never does. The
  console is instead a **separate session against the same physical port**, opened after
  the flasher lets go.
- **It works with no user gesture because nothing ever calls `port.forget()`.** That rule
  was written into `esptoolFlasher.ts` for a different reason (not making the operator
  re-pick the chooser for each board), and it is what now lets an effect call
  `navigator.serial.getPorts()` after a flash and get the port back. Two features rest on
  that one line; do not "tidy" it away.
- **The classifier ranks hints by specificity, not recency.** First cut showed the most
  recent explained line. On a stranded board that is always `agent_main.c:167`'s "no
  network yet; waiting for the link", reprinted every 5 s, which buried the `reason 201`
  above it. Hints carry `hintKind: 'generic' | 'specific'`; generic fills an empty slot and
  never displaces a named cause. Caught by a test written from the real 2026-09-10 log —
  the value of using a genuine failure as a fixture rather than an invented one.
- **A milestone clears the fault.** Wi-Fi retries are normal on a busy AP; leaving "the PSK
  is wrong" on screen after the link came up would be a lie the operator would act on.
- **No inactivity timeout, and this is load-bearing.** `agent_main.c:166` retries forever
  at 5 s, so the failure mode has no window — any timeout would drop precisely the slow
  failure the panel exists to find. Documented in the interface, not just the code.
- **Hardware properties are a separate task, not a hand-wave.** Four things (port
  re-acquisition after `hard_reset` on native-USB parts, 115200 decoding, the EN pulse
  landing in the app rather than the ROM loader, `screen` getting the device after Release)
  are properties of a bridge chip and an OS and cannot be proven in jsdom. Filed as
  **S0-test-1** with the specific failure signature to look for in each, rather than left
  as an implied "should work". The bench is the Mac.
- **Every log string the classifier matches is quoted in `boardConsole.test.ts`.** It is a
  contract with `agent/main/*.c` that nothing else enforces: reword an `ESP_LOGW` and the
  panel would silently stop diagnosing. The tests fail instead.

Details: [docs/features/enrollment.md](docs/features/enrollment.md) →
*Serial console after flashing (S0-fe-1)*.

## 2026-09-10 — fleetforge goes live on prod: an alias network, an infra ingestor, a TLS simulator (R0-infra-5)

- **A dedicated `fleetforge` network exists solely to carry the alias `api`.** The
  frontend's nginx has `proxy_pass http://api:8000` compiled in, and the production box
  already runs a `content-api`. The alternatives were rebuilding the image with a renamed
  upstream (couples every future frontend build to one deployment's naming) or templating
  the nginx config at start-up (a whole mechanism for one string). A third network that
  only these three containers join is cheaper than both, keeps `backend` clean, and the
  next fleetforge image works unmodified.
- **The ingestor is `INFRA_SERVICES`, never `APP_SERVICES`.** `docker rollout` runs two
  copies during the swap; the ingestor is the fleet's sole MQTT subscriber, so that
  duplicates every telemetry row. Same reasoning that already keeps `mosquitto` out. This
  is a correctness constraint on the deploy script, not a preference — it is commented at
  both sites in `deploy.sh` and in the compose file.
- **The api migrates; the ingestor must not.** `RUN_MIGRATIONS=true` on one container
  only. Two processes racing `alembic upgrade head` deadlock on a slow migration.
- **Gotcha — `01-init.sh` is disaster recovery, not deployment.** `docker-entrypoint-initdb.d`
  runs only on an empty data directory. Adding the `fleetforge` role there created nothing
  on the running box; it had to be made by hand with `psql`. The file must be kept in sync
  with what was created manually, and now says so.
- **Gotcha — a missing smoke-test case rolls back a healthy deploy.** `--service fleetforge`
  had no entry in `smoke_endpoint_for_service`, so the check curled an empty URL, got HTTP
  000 and fired the auto-rollback while every container was in fact healthy. Adding a
  service to the filter without adding its endpoint is a trap; commented at the function.
- **Gotcha — argon2id in `.env` must be single-quoted**, or compose eats the `$argon2id`/
  `$v`/`$m` segments and login can never succeed. The R0-infra-3 hash had exactly this
  problem and its plaintext was unrecoverable, so the admin password was reminted here.
  Verify with `docker compose config | grep -i ADMIN_PASSWORD_HASH`; a literal `$$` there
  is correct.
- **The simulator learned TLS (`--tls`), verification only, no pinning.** Acceptance needed
  a board over `mqtts://…:8883` and R0-test-1 had left TLS as an explicit TODO. Off by
  default because the dev broker is plaintext behind Traefik. Worth knowing: without the
  flag against a TLS listener the connect does not error, it *hangs* — which reads like a
  firewall problem and sent the first attempt down the wrong path.
- **No object store, on purpose.** `constraints/iam.disableServiceAccountKeyCreation`
  blocks minting the GCS key and no R0 route touches the store. R1 is blocked on it;
  tracked in docs/runbooks/artifact-storage.md.

Details: docs/features/infrastructure.md → *The app on prod*.

---

## 2026-09-10 — Flashing from the browser: offsets, ordering and one more `ff_cfg` writer (R0-fe-3)

- **No offset is ever derived, only read.** Every address the flasher writes comes from
  `GET /v1/agent/manifest` — `builds[].parts[].offset` and `config_partition.offset`. The
  bootloader is at `0x1000` on ESP32 and `0x0` on the RISC-V parts; a hardcoded offset
  flashes cleanly and never boots, which is the most expensive failure this feature can
  have. `planWrite` is the single place a write is constructed.
- **The enrollment token is minted LAST and revoked on failure.** Validate → manifest →
  chip and flash-size checks → download and sha256-verify every part → *then* mint. It is
  a single-use fleet-join credential: minting first spends one on every failed attempt,
  and a live one baked into a half-flashed board is an orphan nobody is tracking. On any
  failure after the mint the flasher revokes it and says which id, never the plaintext.
- **The form re-runs the engine's own validation on every keystroke.** Not redundancy:
  `power=sleepy` with no wake interval is refused by `POST /v1/enroll` (422), and reaching
  that refusal costs a token. The Flash button is dead until the config would be accepted.
- **A third implementation of `ff_cfg` needed a shared golden vector.** `ff_cfg.py`
  (writer), `ff_cfg.c` (firmware reader) and now `frontend/src/ffcfg.ts` (browser writer)
  must agree byte for byte. `frontend/src/ffcfg.vector.json` carries fields plus the
  sha256 the **Python** writer produced for them, and both suites assert it from their own
  side, so neither writer can move alone. The vector is ASCII-only — `json.dumps` defaults
  to `ensure_ascii=True` and `JSON.stringify` does not, and that is the only region where
  the two are guaranteed to produce identical bytes.
- **`explainFlashError` belongs to the seam, not the adapter.** The commonest failure of
  all is `requestPort()` rejecting because the operator dismissed the chooser — thrown
  *before* an adapter object exists, so it is caught by the engine, which must not import
  esptool-js. Found in T2 against a real Chromium, where the page said `Failed to execute
  'requestPort' on 'Serial': No port selected by the user.` instead of "No board
  selected.". Related: `DOMException instanceof Error` is **true** in Chromium and
  **false** across realms (jsdom's), so the translator reads `name`/`message` off the
  value rather than testing `instanceof`.
- **Erase-on-by-default is a correctness setting.** A re-flashed board whose NVS still
  holds a broker credential reuses it (R0-fw-1: "reusing the stored credential"), so the
  freshly minted token baked into it is never spent and the board never re-registers.
- **`flashSize: 'keep'` costs us esptool-js's fit check, so we do it ourselves.** All three
  of `flashMode`/`flashFreq`/`flashSize` are `'keep'` to stop esptool-js rewriting the
  bootloader's flash-parameter byte and recomputing the image SHA; the price is that a
  2 MB board would silently accept the 4 MB A/B layout. `checkFlashable` derives the bound
  from the manifest — never the string `4MB`, never `ab-4m-v1`.
- **No MD5 read-back.** esptool-js can verify flash contents with MD5; Web Crypto has no
  MD5 and pulling in `crypto-js` to get one is the wrong trade. The sha256 of every part is
  verified against the manifest *before* the first byte is written, which catches the
  failure that actually happens (a truncated download), and esptool-js checksums every
  block on the wire.
- **esptool-js 0.6.1 has no `romBaudrate` option** (the plan assumed one). It is fixed at
  115200 inside `ESPLoader`; tutorials that pass one target a different major. `baudrate`
  is what the stub raises the link to after connecting.
- **The dev container's `node_modules` volume does not re-seed itself.** Adding
  `esptool-js` to `package.json` is invisible to the running Vite server until the named
  volume is removed (`docker compose stop frontend && docker compose rm -f frontend &&
  docker volume rm fleetforge_ff_node_modules && just up`) — otherwise the browser gets
  "Failed to resolve import", which reads like a code bug.

---

## 2026-09-10 — The live device list, and why a stream is not a source of truth (R0-fe-2)

- **The dashboard never patches a row from an event payload.** `GET /v1/devices` is the
  record; a frame on `/v1/events` only says "go re-read". The payload carries an `online`
  snapshot and using it is the obvious free optimisation — it is also how the table starts
  disagreeing with the server about a board. The frame is parsed only far enough to reject
  garbage, and is never rendered or logged.
- **The plain 10 s re-read is a correctness requirement, not a fallback.** A sleepy board
  goes offline with **no event at all** (presence expires on read, `2.5 × wake`, and
  publishes nothing). A purely event-driven list shows it as online forever and passes
  every test one would naturally write. `fleet.test.tsx` carries the test that fails if
  the interval is removed, and T2 proved it end to end: after the will, the event stream
  showed only `: keepalive`, and the row still flipped 29 s later.
- **`EventSource` cannot be trusted to notice a dead stream.** Found in T2, not review:
  behind the Vite dev proxy, `docker compose stop api` leaves the socket open and
  `onerror` never fires — the page said `Live` at a stream that was gone and did not
  recover when the api came back. Fix: the read model is the detector. A failed poll marks
  the stream suspect; the next successful read discards the source and rebuilds it. This
  is also the only reconnect path that survives a proxy holding a half-open socket.
- **CLOSED is not CONNECTING.** A network blip leaves `EventSource` CONNECTING and the
  browser owns the retry (`retry: 2000` from the server). A non-200 — 401, or
  `sse_max_clients` 503 — leaves it permanently CLOSED and needs a manual reconnect,
  1 s → 30 s, deliberately mirroring `RECONNECT_INITIAL_DELAY`/`RECONNECT_MAX_DELAY` in
  `api/eventstream.py`. Treating the two alike either hammers the API or hangs forever.
- **A dead API is not a dead session.** Only a 401 renders the login form; a transport
  failure keeps the last list under a banner. Extends the R0-fe-1 rule to a long-lived
  connection, where the temptation is stronger because the failure is continuous.
- **`just now` must be narrower than the heartbeat interval.** A 5 s "just now" bucket
  exactly swallowed the 5 s heartbeat: last-seen never moved, and a frozen column reads
  exactly like a page that has stopped updating. Single seconds, pinned by
  `format.test.ts`. Caught by running AC1, not by any unit test written before it.
- **The test seam is an injectable `EventSourceFactory`, because jsdom has no
  `EventSource`.** A structural interface a real `EventSource` satisfies without a cast —
  no polyfill, no new npm dependency, and the fake can drive `readyState` 0 vs 2, which is
  the distinction above.
- **`just frontend-test` is separate from `just frontend-build` and both are in `just
  build`.** Vitest config lives in `frontend/vitest.config.ts` because the frontend
  container never reads that file (see R0-fe-1); `npm run build` therefore cannot run the
  tests, so the pipeline runs them itself.
- **Details:** `docs/features/enrollment.md` → *Live device list (R0-fe-2)*;
  `frontend/src/fleet.ts` (the refresh engine and the three triggers);
  `.claude/plans/R0-fe-2-live-device-list-sse.md` (the plan).

---

## 2026-09-10 — What the prod box can actually hold (R0-infra-4)

- **Swap *used* is a stock, not a flow.** The TODO's "already swapping ~1 G" was a point
  sample, and it was already wrong (the box rebooted and swap-used dropped to 11 MB by the
  next measurement). A gigabyte of cold anonymous pages parked in swap and never read back
  costs nothing; what costs is the *rate* of `pswpin` / `pgmajfault`. A verdict built on
  "swap used is 1 G, therefore resize" would be wrong. A verdict built on window deltas
  over 15 minutes is defensible. This is the reusable insight.
- **`memory.events max` is the real under-provisioning signal.** It counts forced reclaims
  *at* the limit — which happen long before an OOM kill and are otherwise invisible. A
  container with `max > 0` is under-provisioned even if it never crashes. `memory.peak`
  alone is not enough; `docker stats` and `memory.current` both include reclaimable page
  cache. The harness reads cgroup v2 directly and reports both.
- **Measuring on dev in the production shape transfers.** Fleetforge's app is not on prod
  yet (R0-infra-5 is blocked on permissions), so the measurement splits: footprint of api /
  ingestor / frontend → dev box in the production shape (`just up-prod`: built images,
  nginx not Vite, same limits); host headroom → `prod` over a sustained window. Container
  RSS for these workloads is set by the workload, not the host. The projection is then:
  measured prod headroom − measured fleetforge footprint − margin. It is a projection and
  must say so; the harness is the acceptance instrument for R0-infra-5 to confirm it.
- **The shared-Postgres cost must be counted explicitly.** Fleetforge on prod does not
  bring its own Postgres — it adds a database and connections to the shared `postgres-prod`
  (1 GiB limit, 199 MiB in use pre-fleetforge). Every backend is ~5–10 MB of private RSS:
  api pool connections × api processes, plus one dedicated `LISTEN` connection per API
  process (`application_name='fleetforge-events'`, from R0-be-5), plus the ingestor's pool.
  This is not in the 512 M declared-limit table and must be measured directly rather than
  estimated.
- **Verdict for R0: no resize needed.** The 151 MiB measured footprint (131 MiB app + ~20 MiB
  marginal Postgres) fits in the 384 MiB headroom bingo freed. Declared over-commit is
  99.5% (3904 / 3924 MiB MemTotal), but measured peaks are what matter and the net add is
  negative. Follow-up: re-run `just capacity-check-prod` after R0-infra-5 lands to confirm
  this projection against live measurements.
- **Details:** `docs/runbooks/capacity.md` (how to re-run it, what the numbers mean, the
  resize procedure); `design/production.md` → *Capacity — Measured 2026-09-10* (replaces
  the stale sample with the as-built measurement + verdict);
  `.claude/plans/R0-infra-4-prod-capacity-check.md` (the plan).

---

## 2026-09-09 — The connect-only agent, and how it is proved without hardware (R0-fw-1)

- **`ff_cfg` is a CRC-headered JSON blob, not a struct.** 16-byte little-endian header
  (`FFCF`, version, reserved, payload length, CRC32) followed by compact UTF-8 JSON,
  0xFF-filled to the 4 KB partition reserved by R0-infra-2. A packed C struct would be a
  second wire format to version, and the flasher that writes it is a **browser**
  (R0-fe-3) — JSON is the one encoding both ends already have. The CRC is what turns a
  half-written partition into one refusal line instead of a board that connects
  somewhere unexpected. `agent/tools/ff_cfg.py` and `agent/main/ff_cfg.c` are the two
  ends of that contract and `tests/test_ff_cfg.py` holds them together (it greps the C
  source for every key the Python writer emits).
- **The config keys are `ssid`/`psk`/`mqtt_pass`, and the spelling is not cosmetic.**
  `tests/test_agent_partitions.py::test_agent_holds_no_credential` fails the build if
  anything under `agent/` puts `wifi_password`, `mqtt_password` or an `ffe_…` literal
  next to a quoted value. That tripwire is worth more than pretty names, so the names
  moved. Where a long name was unavoidable (`ff_enroll.c` parsing the enroll response)
  the literal is split — `"mqtt_" "password"` — with a comment saying why it must not
  be "tidied".
- **`ff_net` is a seam with two adapters, and that is what makes a hardware-free T2
  possible.** `ff_net_wifi.c` is what ships on a board; `ff_net_openeth.c` drives QEMU's
  OpenCores NIC and compiles to a refusal stub wherever `CONFIG_ETH_USE_OPENETH` is off.
  `link` in `ff_cfg` picks one at runtime. Same idiom as the local/GCP seams in the
  Python side.
- **`CONFIG_ETH_USE_OPENETH=y` lives in `sdkconfig.defaults.esp32` only.** The emulated
  NIC exists on no real board and on no other target; the common defaults file stays the
  one place the safety posture is read from.
- **`CONFIG_MBEDTLS_HAVE_TIME_DATE=y` — the one that was silently missing.** ESP-IDF
  defaults it **off**, and with it off mbedTLS never looks at `notBefore`/`notAfter`: an
  expired certificate validates, and `spec/device-protocol.md` → *Clock — SNTP before
  TLS* describes a failure that cannot happen. Found by AC5 doing the opposite of what
  the plan predicted — a board with a 1970 clock completed a real TLS handshake against
  `bingo.tvaroska.sk`. Enabling it makes the spec's rule true, and costs a board whose
  SNTP never answers its TLS channels (accepted; the SNTP client keeps retrying in the
  background while the enroll ladder backs off). `verify_bundle.py` and
  `test_agent_partitions.py` both require it now, in the **resolved** config.
- **`CONFIG_MBEDTLS_CERTIFICATE_BUNDLE=y`, no pinned CA.** Both channels terminate at
  Traefik with a Let's Encrypt certificate (R0-infra-3), so the Mozilla root bundle is
  the trust store. Proved in QEMU against the real production hostname.
- **The confirm call is guarded by `ESP_OTA_IMG_PENDING_VERIFY`, and R0 must not "fix"
  that.** `esp_ota_mark_app_valid_cancel_rollback()` runs only for an image the
  bootloader is actually watching; a serially flashed board never enters that state, so
  the timer is inert today. Calling it unconditionally at boot would compile, look
  correct, pass every R0 test — and disable R2's auto-rollback on the entire fleet.
- **No goodbye publish.** A board that is dying cannot send one. Presence-off is the
  broker's job via a retained LWT, which is what `/v1/devices` and the dashboard already
  key off (R0-test-1 established the same posture for the simulator; the simulator sends
  a goodbye because it is a process, not a board).
- **The token stays in `ff_cfg` after it is spent.** Erasing it would mean writing to a
  partition the firmware otherwise only reads, on every first boot, to remove a string
  that is already dead server-side. The credential in NVS is what stops a second
  enrollment, and `ff_store_load()` distinguishes *absent* from *corrupt* so a torn write
  parks the board instead of burning another token.
- **QEMU's eFuse MAC is all zeros, so every emulated board is `000000000000`.** The agent
  warns once per boot and does not paper over it: `device_id` is the eFuse MAC on real
  silicon and there is no special case anywhere in the code. Two emulators are one device
  as far as the fleet is concerned.
- **`.qemu/` is a credential directory, not a cache.** 0700, gitignored: `ff_cfg.bin`
  holds a live single-use token and `flash-esp32.bin` holds, inside NVS, the broker
  password that board was issued. QEMU writes the image back (`if=mtd`), which is exactly
  what makes "a reboot burns no second token" testable — and what makes `--fresh` a
  credential deletion.
- **Gotcha, ~30 min:** the dev stack routes by `Host`, and the emulated board addresses
  this box as slirp's `10.0.2.2` — so every enroll came back **404 from Traefik**, having
  never reached the API, which reads exactly like a firmware bug. Fixed with a dev-only
  `ff-qemu` router in `docker-compose.override.yml` (and `10.0.2.2` added to Vite's
  `allowedHosts`, which rejects unknown Hosts for the same reason).
- **Gotcha:** `just` drops empty arguments when it splices `*args` into a recipe, so
  `--ntp ''` cannot be expressed through `just agent-cfg`. "No NTP" is therefore a flag
  (`--no-ntp`) — a test knob for the clock rule, not a setting.
- Details: `docs/runbooks/agent-qemu.md` (worked transcript), `docs/features/enrollment.md`,
  `.claude/plans/R0-fw-1-esp32-agent-connect-only.md`.

---

## 2026-09-09 — Agent firmware: what is frozen at flash time (R0-infra-2)

- **`ab-4m-v1` is frozen, and it is a three-way contract.** `agent/partitions.csv`
  (`ota_0`/`ota_1` at `0x1E0000`), `spec/device-protocol.md`
  (`"ota_slot_size": 1966080`, `"partition_layout": "ab-4m-v1"`) and
  `tests/test_agent_partitions.py` (which retypes both literally and greps the spec)
  move together or not at all. A partition table cannot be changed by OTA, so a new
  layout is a NEW ID plus a server that understands both — never an edit to this one.
- **No `factory` partition, deliberately.** A factory-only board can never OTA its way
  to an A/B layout. `make_manifest.py` refuses to emit a bundle whose built table has
  one, is missing `ota_1`, or whose slots differ in size.
- **`ff_cfg` (data, subtype `0x40`, 4 KB @ `0x12000`) is reserved now, defined later.**
  It is where the browser flasher will write the per-board broker URL, Wi-Fi credentials
  and enrollment token. `R0-fw-1`/`R0-fe-3` own the payload format; reserving the space
  after boards ship is impossible, so it is reserved before anything ships.
- **Rollback on, eFuses untouched.** `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y` is safe to
  enable at R0 because only an OTA'd app enters `PENDING_VERIFY` — a serially flashed one
  never does, so nothing can brick before `R0-fw-1` exists. Anti-rollback, secure boot and
  flash encryption stay **off**: they burn eFuses per board, irreversibly, and there is no
  key-management story yet. `verify_bundle.py` fails the build if any appears enabled in
  the RESOLVED config.
- **Offsets are read from ESP-IDF, never typed.** `make_manifest.py` takes them from
  `build/flasher_args.json` by name. The bootloader really is at `0x1000` on ESP32 and
  `0x0` on the RISC-V parts; a hardcoded value flashes cleanly and never boots on half
  the fleet.
- **The ESP-IDF pin is a digest; bumping it is a decision, not a version bump.** It
  changes the bootloader and app on every board flashed afterwards while fielded boards
  keep the old one. Procedure in docs/runbooks/agent-build.md.
- **Agent bundles are baked into the app image, not routed through `ObjectStore`.** They
  are build outputs that version with the image, identical for every tenant, ~1.2 MB per
  target — and the prod GCS credential cannot currently be minted at all
  (`constraints/iam.disableServiceAccountKeyCreation`, docs/runbooks/artifact-storage.md).
  Putting them behind the object store would take a working feature and make it
  unshippable. R1's *user* artifacts still go through `ObjectStore`.
- **`fleetforge-agent-*` images are NOT compose services.** Never add them to
  `PULL_SERVICES`/`APP_SERVICES`: `docker compose pull` fails as a unit (see the
  R0-infra-5 entry below). They exist as a provenance handle; production gets the bytes
  from the app image.
- **Gotcha, cost ~40 min:** `espressif/idf:v5.5.5` unpacks to **~8.9 GB**, not the ~5.5 GB
  estimated. The pull dies with `failed to register layer: no space left on device`.
  Reclaim with `builder prune -af` / `container prune -f` / `image prune -f` and
  regenerable caches only — **never** `image prune -a`, `system prune -a` or
  `volume prune` on this box, which holds other projects' images and 31 volumes.
- **Gotcha:** matching forbidden sdkconfig options by PREFIX rejects every correct esp32
  build — `CONFIG_SECURE_BOOT_V1_SUPPORTED=y` is a SoC capability symbol, not an
  enablement. Exact names only. A safety check that fails on correct input teaches the
  next person to delete it.
- **Gotcha:** a project-root `sdkconfig` silently overrides `sdkconfig.defaults` from the
  first build onward, so a committed one would ship a bootloader whose posture no longer
  matches the tracked defaults. Gitignored and dockerignored; builds run in a container.
- **ESP-IDF builds are not byte-reproducible, and that is why provenance is in the
  manifest.** `esp_app_desc_t` embeds the compile date/time, so rebuilding the same commit
  with the same pinned toolchain yields a different `app.bin` sha256 (the partition table
  and otadata are stable). A registry pull of a pushed digest IS byte-identical — verified
  push → `rmi` → pull by digest → export → `diff -r`. `CONFIG_APP_REPRODUCIBLE_BUILD=y`
  would fix the rebuild case but changes every binary, so it is a separate decision.
- **Gotcha:** in IDF 5.x `build/config/` holds only generated `.h`/`.cmake`/`.json`
  views — the text sdkconfig is at the project root. Resolve it from
  `project_description.json["config_file"]`; a literal path breaks on the next IDF bump.

## 2026-09-09 — One app image serves both the api and the ingestor (R0-infra-5)

- **Two images, not three.** `fleetforge` runs the api and the ingestor; they are
  the same code with a different `command`, exactly as `docker-compose.yml`
  already builds them from one `fleetforge:dev`. A separate ingestor image would
  rebuild identical layers and give the pair a way to drift in production.
  `fleetforge-frontend` stays separate — different base, different build.
- **`just build` runs the T1 gate before it pushes.** Prod pulls by tag, so a
  broken build reaching the registry is a production defect, not a local one.
- **The frontend image cannot be verified with `nginx -t`.** nginx resolves
  `proxy_pass http://api:8000` at config load, so the syntax check fails with
  "host not found in upstream" anywhere there is no api container. That is the
  same real behaviour that forces the prod api service to carry the network alias
  `api` — but it makes `nginx -t` unusable as a standalone gate. `_verify-images`
  asserts the built payload instead (non-empty `index.html`, an `assets/*.js`).
- **The ingestor must never enter `APP_SERVICES`.** `docker rollout` runs two
  copies during the swap, and the ingestor is the sole MQTT subscriber
  (design/production.md → *The single-subscriber rule*): two would double every
  telemetry row and split the SSE audience. Same reason mosquitto is excluded.
  It goes in `INFRA_SERVICES`, recreated in place.
- **Gotcha:** never add a name to `PULL_SERVICES` before the compose file defines
  it. `docker compose pull` fails as a unit, so an unresolvable ref breaks the
  deploy for every other app on the box.
- The prod fragment half is not shipped — see docs/features/infrastructure.md.

## 2026-09-09 — TLS for the fleet terminates at Traefik, not at the broker (R0-infra-3)

- **The shared Traefik owns 8883.** A `mqtt` TCP entrypoint with a
  ``HostSNI(`bingo.tvaroska.sk`)`` router and `tls.certresolver=myresolver`
  terminates TLS and forwards **plaintext** to `mosquitto:1883` on an internal
  network. The alternative — TLS passthrough, or certs mounted into the broker —
  would need DNS-01 or a second renewal path for one service. HTTP-01 over :80
  already issues the certificate; the TCP router just reuses it. Nothing about
  ACME changed, and the broker container knows nothing about TLS.
- **`prod/mosquitto/` in the `services` repo is a copy, and the copy is guarded.**
  `deploy.sh` only ships `services/prod/`, so the config has to live there. `acl`
  is the entire fleet authz model, so `validate-config.sh` now diffs the two trees
  and fails the deploy on drift. A copy nobody checks is how a stale ACL reaches
  production.
- **Prod broker usernames must not look like device ids.** `ff-admin` /
  `ff-ingestor`, not 12 lowercase hex digits — the `acl_file` patterns key on `%u`,
  and `ensure_client` does create → already-exists → `setClientPassword`, so a
  hex-shaped service username could be re-keyed by enrolling that `device_id`.
  This is the R0-sec-1 reviewer note, now honoured in `services/prod/.env`.
- **The broker is excluded from `docker rollout`.** Rollout runs two copies during
  the swap; two brokers cannot share the dynsec store or the 1883 bind. New
  `INFRA_SERVICES` list in `deploy.sh` recreates it in place instead.
- **Gotcha, cost ~25 min:** with the fleetforge dev stack running on this machine,
  `just deploy` fails its staging gate with a **504 on every smoke test while all
  containers report healthy**. The staging Traefik sees the host daemon through
  socket-proxy and picks up `fleetforge-frontend`'s ``Host(`localhost`)`` rule,
  which outranks staging's `PathPrefix(/)`, then cannot reach that network. It
  reads exactly like a content-api regression. `docker compose stop` in fleetforge
  first. See docs/features/infrastructure.md → *R0-infra-3*.

## 2026-09-09 — The dashboard trusts the server's token status (R0-fe-1)

- **The frontend never recomputes token state.** `status` comes from the API, which derives it
  from the burn predicate itself (`auth/enrollment.token_status`). Recomputing it in the browser
  from `expires_at`/`used_at`/`revoked_at` looks trivial and is the bug: the two implementations
  drift, and the dashboard eventually shows "active" for a token that `POST /v1/enroll` will
  refuse — which reads, in the field, as broken enrollment rather than a stale token.
- **The issued plaintext lives in React state and nowhere else.** No `localStorage`, no
  `sessionStorage`, no URL, no error message. The server cannot re-derive it, so persisting it
  "for convenience" would be storing an un-rotatable fleet-join credential in the most readable
  place in the browser. Three tests in `EnrollBoard.test.tsx` and one browser-context assertion
  exist purely to fail if this regresses.
- **R0-fe-1 had to ship the login gate.** The task line says "generate token", but the token
  endpoints are admin-authenticated (R0-be-1), so the page was unreachable in a browser without
  one. Scope grew by a screen; the alternative was a page only `curl` could use.
- **Session state is not mirrored client-side.** The cookie is HttpOnly, so the page cannot read
  it; "signed in?" is `GET /v1/auth/me` plus a 401 watch on every later call. A mirrored boolean
  would only ever disagree with the cookie. A transport error is explicitly not treated as a
  logout — that distinction stops an operator re-typing the admin password at a dead API.
- **Gotcha, cost ~15 min: `vite.config.ts` is loaded by the container, so it may not import test
  deps.** Putting the `test` block there (via `defineConfig` from `vitest/config`) broke the
  `frontend` service with `ERR_MODULE_NOT_FOUND`, because the image's `node_modules` has no
  `vitest`. Traefik then dropped the unhealthy backend and every `/v1/*` call returned a bare
  **404** — a symptom that points at routing, not at a config import. Vitest config now lives in
  `frontend/vitest.config.ts`, which the container never reads.

## 2026-09-09 — The simulator is a device, not a test fixture (R0-test-1)

- **`python -m fleetforge.simulator` imports nothing from the server.** No `fleetforge.config`
  (and therefore no mandatory `DATABASE_URL`), no `fleetforge.db`, no `fleetforge.api` — only
  `fleetforge.identity` for `DEVICE_ID_RE`. A simulated board that needs the server's database URL
  to boot is modelling the wrong thing, and the import would drag the ORM into a process
  pretending to be an ESP32. This is a deliberate deviation from `broker/__main__.py` and
  `storage/__main__.py`, both of which do `Settings()`; there is an AST tripwire test so the
  deviation cannot rot back.
- **Retain flags are the contract, and a wrong one is a silent wrong answer.** `announce` and
  `presence` retained, `hb` **not** — a retained heartbeat would be replayed on every ingestor
  reconnect and `handlers.py` treats a retained message as a replay, so `last_seen` would quietly
  stop advancing. The LWT is retained too, or a server restart never learns a board is dead.
- **A clean disconnect does not fire the LWT**, so Ctrl-C on an `always_on` board would leave it
  online forever in the dashboard. The simulator publishes a retained `{"online":false}` goodbye on
  any clean shutdown (which is what a planned reboot should do anyway), and `--crash-after` uses
  `os._exit(1)` — a TCP FIN with no DISCONNECT — as the *only* honest way to exercise the will.
  aiomqtt has no public API for dropping a connection and `client._client` is private.
- **A sleepy wake is a fresh `aiomqtt.Client`**: entering the same client twice raises
  `MqttReentrantError` (2.5.1). Sleep is a clean disconnect rather than a simulated brownout; the
  server-visible state is identical because `is_online` ignores `presence_reported` for sleepy and
  the ingestor never advances `last_seen` on `presence:false`. Verified live: after the last wake
  the board stayed `online:true` for ~25 s (2.5 × 10 s) and then flipped with **no** message and
  **no** SSE event — presence is computed on read.
- **The device id is a locally-administered pseudo-MAC** — `(sha256(name)[0] & 0xFE) | 0x02` — so a
  simulated board is stable across runs, can never collide with a real Espressif OUI, and can never
  reach the `ffff…` ids `broker/__main__.py` reserves. Provable, and proven in a test, rather than
  unlikely.
- **The broker password is written to `.sim/<device_id>.json` at 0600, gitignored, never logged.**
  It is the NVS analogue and it exists nowhere else — same posture `spec/prd.md` already states for
  a real board. State present means **no enrollment happens**, because tokens are single-use; a
  corrupt state file is a loud error rather than a silent re-enroll that burns one.
- **Everything is validated before the token is presented** — the same ordering rule
  `POST /v1/enroll` follows internally. A `sleepy` board with no wake interval fails before the
  HTTP call, not after the burn (confirmed against a live stack: the token stayed `active`).
- **`fleet` issues its own tokens** through `POST /v1/auth/login` → `POST /v1/enrollment-tokens`,
  because typing three single-use tokens by hand is the friction this task exists to remove. Token
  **ids** are printed, plaintexts never are.
- **The agent connects with `client_id = device_id` and `clean_session = false`**, implementing the
  proposal R0-be-4 recorded: command durability comes from the persistent session, never from a
  retained `dn/cmd`, and MQTT 3.1.1 requires a non-empty client id for one. Three additive
  `spec/device-protocol.md` changes follow from this work and are **proposed, not written**: the
  LWT is published with `retain = true`; the agent's client id and clean-session flag; and a
  planned shutdown SHOULD publish a retained `{"online":false}` before disconnecting.

---

## 2026-09-08 — Broker authz: dynsec authenticates, acl_file authorises (R0-sec-1)

- **Mosquitto 2.0's dynamic-security plugin does not support `%u`/`%c` substitution.**
  Verified against 2.0.22: a `device` role holding `publishClientSend ff/v1/d/%u/up/#`
  denies the very client it names (MQTT v5 PUBACK 135), while the same role with a literal
  topic allows it, and the plugin binary contains no substitution code. `%u` is `acl_file`
  syntax. Every earlier document that says the two pattern ACLs live in a dynsec role — the
  R0-be-4 plan, `broker/provisioner.py`, `CRITICAL.md`'s wording — was wrong about the
  mechanism, not about the model.
- **So the two mechanisms are split: dynsec = authentication (who exists, what password,
  written by `/v1/enroll`), `acl_file` = the fleet ACL (the two `%u` pattern rules,
  verbatim from `spec/device-protocol.md`).** Mosquitto consults both and **allow wins**,
  proven in both directions. The spec's promise — "no per-device ACL rows, nothing to
  provision at enrolment" — survives intact; only the file it lives in changed.
- **The dynsec `device` role exists and is empty.** `createClient` requires a role name
  (`broker.DEVICE_ROLE`), and a name mismatch answers 503 on every enrolment. Moving the
  pattern rules into it does not fail loudly — it silently denies the whole fleet.
- **Read authorisation is enforced on delivery, not on SUBSCRIBE.** With `acl_file` a device
  may subscribe to `#` and gets SUBACK 0, then receives only its own `dn/` traffic (verified).
  Any test that asserts on the SUBACK code proves nothing. Same class: a forged LWT is
  accepted at CONNECT and dropped when it fires, so presence cannot be forged for another
  board (verified — the ingestor logged nothing for the impersonated device).
- **A denied publish is invisible below MQTT v5, and the broker log does not help.**
  aiomqtt/paho surface only the local `rc`, and 3.1.1 has no reason code at all. Mosquitto
  2.0.22 logs `Denied PUBLISH` at `MOSQ_LOG_DEBUG`, which `mosquitto.conf` does not enable
  (debug logs every topic — not worth the noise). The authoritative check is
  `mosquitto_pub -V 5 -d` **inside** the broker container, reading the PUBACK: `RC:135` is
  the denial, `RC:0`/`RC:16` are both "allowed" (16 only means nobody was subscribed, so an
  allowed publish reads as `RC:0` whenever the ingestor is up). `just broker-check` asserts
  on non-delivery instead, which is what the Python client can actually see.
- **`dynamic-security.json` is mutable state in the data volume, owned by uid 1883.** The
  plugin rewrites it on every enrolment; a root-owned file logs "not writable", applies the
  change in memory, and loses every device credential at the next restart — with the API
  reporting success. The bootstrap chowns and chmods it, and `docker compose logs mosquitto
  | grep -c "not writable"` is an acceptance check.
- **Bootstrap runs a throwaway broker inside a one-shot init container.** `mosquitto_ctrl
  dynsec init` is the only file-mode subcommand; everything else needs a live broker. The
  script is idempotent (create → "already exists" → `setClientPassword`), so it is safe on
  every `up`, and the broker `depends_on` it with `service_completed_successfully`.
- **The healthcheck authenticates as the dynsec admin**, the only client that exists before
  the bootstrap and the only one `dynsec init` gives `$SYS` read. The topic must be
  single-quoted (`'$$SYS/broker/uptime'`); a broken broker probe shows up as a Traefik 404,
  not as an unhealthy badge.
- **The ingestor gets its own credential and its own read-only role**
  (`subscribePattern` + `publishClientReceive` on `ff/v1/d/+/up/#`, no `$SYS`, no write).
  It is a different privilege from the API's dynsec admin and rotates separately.
- **Devices enrolled during the `NullProvisioner` era cannot be reconciled.** The broker
  password only ever existed in the enrolment response; the server cannot re-provision one
  the device would know. They must re-enrol with a fresh token — documented in
  `docs/runbooks/dev-stack.md` rather than built as a command that cannot work.

---

## 2026-09-08 — Object store: one Protocol, two adapters, and the prefix is a security boundary (R0-be-6)

- **One `ObjectStore` Protocol, two real adapters, selected by configuration** — the same
  shape as `fleetforge.broker` (`BrokerProvisioner` / Null / Dynsec). MinIO (S3) in dev
  and for V2 self-hosting, GCS in production. Both SDKs are imported **lazily inside the
  factory**, so neither is on the API's import path and an unconfigured deployment pays
  nothing. Four verbs only — `put`, `get`, `signed_url`, `delete`. **No `list`**: nothing
  in R1 needs it, and it is the one verb an IAM prefix condition cannot constrain (see
  below), so adding it would silently widen the grant.
- **`GCS_PREFIX` is a security boundary, not tidiness.** `gs://btvaroska` is *shared* —
  it holds this estate's `.env` backups under `secrets/`, plus the boris podcast audio.
  Object keys arrive from an HTTP request body (R1's upload), so the prefix is confined
  **twice and independently**: `resolve_key()` in-process, and an IAM condition on the
  service account (`resource.name.startsWith(".../objects/fleetforge/")`). Either alone
  is one bug away from writing into `secrets/`.
- **`resolve_key()` rejects, never normalises** — the rule `identity.py` already
  established for device IDs. `..`, a leading `/`, `//`, backslashes, control or
  non-ASCII bytes, `?`/`#`, over 512 chars: all `ObjectKeyError`, which is a `ValueError`
  and deliberately **not** an `ObjectStoreError`, because a bad key is a 400 (the caller
  is wrong) while everything else is a 404 or a 503 (we are). Path normalisation is how
  traversal bugs get written: `a/../../b` has an obvious "sane" reading, and acting on it
  is exactly the mistake.
- **Two S3 endpoints, because a presigned URL signs the `Host` header.**
  `S3_ENDPOINT_URL` (`minio:9000`) is what the API talks to; `S3_PUBLIC_ENDPOINT_URL`
  (`localhost:9000`) is what URLs are *signed against*, because the device is not on the
  compose network. Rewriting the host after signing invalidates the signature — there is
  no post-hoc fix, so the split has to exist at signing time. The container selftest
  therefore cannot fetch the URL it prints, and says so instead of failing.
- **Both backends configured is an error, not a precedence rule.** "Which bucket did my
  firmware go to?" must not be answered by reading a factory. Unset one or set
  `OBJECT_STORE_BACKEND`. Likewise **no ADC fallback for GCS**: ADC on a GCE VM carries
  no private key (so no V4 signing) and resolves to the project-wide compute default SA —
  the exact credential the prefix condition exists to avoid. Missing credentials fail
  loudly at construction.
- **Unconfigured is a WARNING plus a 503, never a startup crash.** `create_app()` stays
  constructible with no environment at all (the R0-be-1/R0-be-4 precedent); artifact
  routes will answer 503 until storage is configured.
- **GCS has never been round-tripped against the real service.** `btvaroska` inherits
  `constraints/iam.disableServiceAccountKeyCreation`, so the key the adapter requires
  cannot be minted, and the keyless alternative needs an IAM grant this task was not
  authorised to make. The SA and its conditional binding exist; the credential does not.
  **Do not read a green dev stack as evidence that production storage works** — the
  options (impersonation + `signBlob`, or a policy exemption) are written up in
  `docs/runbooks/artifact-storage.md`, and one of them is a prerequisite for R1.

## 2026-09-08 — SSE: one listener per worker, and a reconnect ends every stream (R0-be-5)

- **One dedicated asyncpg connection per API process, never a pooled one.** `LISTEN`
  only delivers to a backend that is between transactions, and `pool_pre_ping`/recycle
  would drop the registration with nothing in the log — the symptom is a stream that
  connects and stays empty forever. `db/base.asyncpg_dsn()` converts the SQLAlchemy
  URL; the connection sets `application_name = 'fleetforge-events'` so
  `pg_stat_activity` answers "is anything listening?" without reading code. This is the
  one documented exception to "`get_sessionmaker()` is the only door into the database
  from the API".
- **A listener reconnect closes every SSE stream.** The hub cannot know what was missed
  while the connection was down, and a client that keeps reading after a gap silently
  shows a stale fleet. Ending the stream makes `EventSource` reconnect and re-read
  `GET /v1/devices`, which is the same self-healing path as the slow-client case. That
  is also why there is no "resync" event type.
- **A slow client is disconnected, not buffered.** Bounded per-client queues
  (`sse_queue_size`); on overflow the queue is drained and a sentinel ends that one
  stream. Dropping individual events instead would leave a client silently wrong, and
  unbounded buffering is a memory leak in a 256 M container.
- **The NOTIFY payload is validated and then forwarded *verbatim*.** `fw_version` comes
  off the wire from a board, and SSE framing is newline-delimited: a payload containing
  a raw newline would let a device inject a forged event into the operator's stream. It
  cannot happen today (`model_dump_json` escapes control characters), which is why it is
  asserted rather than assumed. Forwarding the original rather than a re-serialization
  keeps the additive-evolution rule — re-serializing would strip fields a newer ingestor
  adds.
- **Auth is checked once, at connect, so a stream is capped at 15 min**
  (`sse_max_stream_s`). Instant revocation is the reason JWT was rejected (R0-be-1), and
  an unbounded stream would quietly outlive a revoked token. The cap is deliberately
  under nginx's `proxy_read_timeout 3600s`. Browser `EventSource` cannot send an
  `Authorization` header at all — the stream authenticates on the `ff_session` cookie,
  which is R0-be-1's "one credential, two transports" paying for itself. **A token in
  the query string was rejected:** nginx's access-log format logs `$request`.
- **`GET /v1/devices` shipped here, not in R0-fe-2.** `events.py` and `presence.py` both
  already define the contract as "the event is a hint; re-read `GET /v1/devices`", and
  no task owned that endpoint — an SSE stream whose documented contract is "go read an
  endpoint that 404s" is not a finished artifact. Presence is computed on read via
  `presence.is_online`, with one `now` for the whole response; `presence_reported` is
  deliberately not exposed, so no client can re-derive the rule.
- **Gotcha, and it will bite the next streaming endpoint too:** `httpx`'s
  `ASGITransport` buffers the entire response body before returning, so
  `client.stream()` against an endless SSE generator hangs the whole suite. The tests
  drive the ASGI app directly (`tests/test_events_stream.py::drive_sse`); only responses
  that never stream (401, 503) go through the normal client. Related: Starlette
  *cancels* the generator on disconnect for ASGI spec_version < 2.4 (uvicorn reports
  2.3), so the subscription is released in a `finally:` inside the generator, not after
  it — anywhere else leaks one subscriber per page reload.
- **`asyncpg.InterfaceError` is caught alongside `PostgresError`/`OSError`** in the
  listener's reconnect loop. asyncpg raises it for "connection is closed", which the
  keepalive `SELECT 1` hits when the socket died between two ticks; letting it escape
  would kill the listener task for the life of the process — the exact silent failure
  this module exists to prevent, with nothing unhealthy anywhere. asyncpg also ships no
  `py.typed`, so it gets one `ignore_missing_imports` override in `pyproject.toml`
  rather than a `# type: ignore` at every call site.
- **No Redis and no broadcaster abstraction.** One backend, and the "no Redis" decision
  is already recorded under *The ingestor is the only MQTT subscriber*. There is no
  second implementation of this boundary and none is planned, so no adapter pair.

---

## 2026-09-08 — Enrollment: commit, then provision; and the grace window (R0-be-4)

- **Verify the token secret before calling `BURN_SQL`.** The statement keys on `id`
  alone, and an `ffe_` token's id is not a secret — it is in the issuance response and
  in the api log. Burning before `averify_secret` would let anyone who has read a log
  line destroy every outstanding token: a bench full of boards that will not enroll,
  with the dashboard reporting them `used` and nothing failing loudly. Order is
  `require_admin`'s: parse → row → `dummy_verify` on a miss → verify → burn.
- **The device row is INSERTed before the burn, in the same transaction**, because
  `enrollment_tokens.used_by_device_id` is a real FK. A refused burn rolls both back.
- **The transaction commits BEFORE the broker is provisioned.** `db/models.py::Device`
  put `broker_provisioned_at` in the schema "so provisioning can be reconciled and
  retried idempotently after a partial enrollment" — the schema already chose this.
  Holding a row lock and a pooled connection across an MQTT round-trip turns a broker
  outage into `idle in transaction` on a 256 M container. The inverse failure —
  a broker credential for a device that is not enrolled — is prevented by the order,
  not by a transaction.
- **A burned token may be re-presented by the SAME `device_id` for 600 s**
  (`config.enroll_retry_window_s`) and gets a freshly provisioned password. The device
  writes NVS only after it reads the response body, so a dropped packet on first boot
  otherwise leaves a board that is enrolled and has no credential, holding a token that
  can never burn again — a re-flash, in the field. Single use is intact: the lookup
  matches on `used_by_device_id`, so one token still enrolls exactly one board forever,
  and `FOR UPDATE` keeps a concurrent revoke from racing it. **PROPOSED for
  `spec/prd.md` → *Security & data posture*** (protected, so not written there): state
  the grace window next to "cannot be replayed from a recovered board".
- **`mqtt_username` is `device_id`, unnormalised.** The `%u` pattern ACLs are the entire
  fleet authz, so the eFuse-MAC format check runs before any credential exists and a
  non-canonical `device_id` is rejected rather than lowercased. `DEVICE_ID_RE` moved to
  `fleetforge/identity.py` — the API must not import from `fleetforge.ingestor`, same
  precedent as `clock.py` leaving `api/deps.py`.
- **`NullProvisioner` leaves `broker_provisioned_at` NULL on purpose.** The dev broker
  is anonymous until `R0-sec-1`, and `WHERE broker_provisioned_at IS NULL` is then the
  honest reconcile list rather than a column that lies. Selection is by the presence of
  `MQTT_DYNSEC_USERNAME`/`_PASSWORD`, with a startup WARNING — the same shape as
  `ADMIN_PASSWORD_HASH`.
- **Dynsec gotchas, all verified against Mosquitto 2.0.22's protocol:** responses come
  back only to the issuing client on `$CONTROL/dynamic-security/v1/response`, so
  subscribe before publishing; an error is a *key in the response body*, not a transport
  failure; `correlationData` is echoed and must be matched, or a stale reply from a
  timed-out command is read as this one's success; the dynsec client id carries a random
  suffix, because two API workers sharing one kick each other off mid-command and the
  symptom is an intermittent 503. `clientid` is deliberately not bound to the credential:
  `spec/device-protocol.md` does not specify the agent's client id and `R0-fw-1` is
  unwritten. **PROPOSED for `spec/device-protocol.md`**: state that the agent connects
  with `client_id = device_id`, which would make that binding free hardening later.
- **`ingestor/store.py` finally has rows to update.** Until this task, nothing in the
  codebase inserted a device, so every published message was dropped by design.

---

## 2026-09-08 — Ingest: derived presence, and the retained-replay trap (R0-be-3)

- **`last_seen` advances only on a live message that is not `presence{online:false}`.**
  Retained `announce`/`presence` replay on every ingestor reconnect (the process
  re-`subscribe`s, so the broker re-sends the whole retained set), and the LWT is
  published by the *broker*, not the device. Either one, treated as evidence of life,
  marks a dead fleet alive — and for `sleepy` boards, where "the LWT fires on every
  normal sleep and means nothing", it never self-corrects. MQTT's `retain` flag on
  delivery is the discriminator: set only for a retained replay. Verified live —
  `docker compose restart ingestor` replays `up/presence` with `retain=True` and
  `last_seen` does not move.
- **`last_seen = GREATEST(last_seen, :at)`.** QoS 1 is at-least-once; monotonicity is
  one SQL function, not a comparison in Python.
- **The ingestor `UPDATE`s and never `INSERT`s.** The only way into the registry is a
  burned enrollment token (R0-be-4). An `INSERT … ON CONFLICT` here would make anyone
  who can publish to the broker a fleet member — the dev broker is anonymous today, so
  `ingestor/store.py` is the file that stops it. A decommissioned device is dropped by
  the same `WHERE`, with a log line, rather than resurrecting its row.
- **`pg_notify` runs in the write's transaction, via `SELECT pg_notify(:channel, :payload)`.**
  `NOTIFY` takes no bind parameters, so the string form is an injection with a
  device-controlled payload; and transactional delivery means SSE can never announce a
  row the database does not have. Payload capped at 7500 B against PostgreSQL's 8000 B
  limit, and an oversized event is skipped rather than allowed to fail the write.
  `fleetforge.events` ships `EVENTS_CHANNEL` and `DeviceEvent`; R0-be-5 imports both
  rather than restating either — a channel name spelled twice is a silently empty SSE
  stream with nothing failing loudly.
- **`presence.is_online()` is the single rule, and presence stays uncomputed in the
  database.** The event's `online` is a snapshot for the SSE consumer; the API
  recomputes on read, because a sleepy device goes offline with no message arriving at
  all. The 2.5 tolerance lives once, in `config.presence_tolerance`.
- **An announce whose `power_class` would violate a CHECK loses that field, not the
  whole message.** `fw_version` is what tells the operator the OTA landed; dropping the
  announce over a barely-used field would be the wrong trade. The pair is validated in
  Python, the CHECK stays the backstop.
- **A payload `device_id` that disagrees with the topic is dropped.** The topic is
  authoritative — it is what the `%u` pattern ACL binds to the broker username.
- **One message never kills the process.** Specific exception families
  (`SQLAlchemyError`, `OSError`, `ValueError`) around the per-message write, and the
  heartbeat file touched even on failure: liveness is broker-connectedness, and
  restarting the container does not fix Postgres. Verified by stopping Postgres under
  load — one ERROR line per message, container still healthy, full recovery on restart.
- **`now_utc()` moved to `fleetforge/clock.py`.** It lived in `api/deps.py`, and the
  ingestor must not import `fleetforge.api` — pulling FastAPI's app factory into a
  process with no HTTP server would drag its settings validation along with it.
- **Gotcha fixed in passing:** `just mqtt-pub` wrapped the payload in a double-quoted
  shell word, so the shell ate every `"` in a JSON body and the broker received
  `{proto:1,…}`. It presents as a `JSONDecodeError` from the ingestor and looks like an
  ingest bug. The payload now travels in the environment; the recipe also takes a
  `retain` argument, since retained state is most of what this task had to be tested
  against.
- **PROPOSED for `spec/` (protected, so not written there):** `spec/device-protocol.md`
  → *Open items for R0* asks whether `up/log` ships in R0 — the answer this task
  implements is "accepted and dropped: it only moves `last_seen`, storage is R3".

---

## 2026-09-08 — Enrollment token issuance: one predicate, two readers (R0-be-2)

- **`BURN_SQL` ships as an importable constant** in `fleetforge.auth.enrollment`, not
  as prose to copy. `R0-be-4` imports it, and `tests/test_invariants.py`'s four burn
  tests — including the two-connection race — now exercise the shipped statement
  rather than a duplicate of it. Supersedes the "R0-be-4 must copy this verbatim"
  instruction in the `EnrollmentToken` docstring and `R0-db-1` §12.
- **The API's derived `status` is *defined* as the burn predicate** — `active` iff the
  burn would succeed — and there is a parametrized equivalence test over all four
  states so the two cannot drift. A dashboard that says "active" about a token the
  burn rejects sends someone to the bench with a board that will not enroll. The
  database clock stays the authority: `token_status()` is a display value and never an
  authorization decision, which is always the conditional UPDATE.
- **The plaintext is in the `POST` response body on purpose**, unlike the admin login
  token. A human copies it into the flasher's baked config (`spec/flows.md` Flow 1), so
  it must be readable exactly once; it is never logged (ids only), never re-derivable,
  and the list response model has no field that could carry it.
- **24 h lives in `config.enrollment_token_ttl_hours` and nowhere else** — no DB
  default, no per-request override. The number's home is `spec/prd.md`; a `ttl_hours`
  in the request body would be a second place the rule can be violated.
- **Revoke is `POST …/revoke`, not `DELETE …`.** Revoked rows are retained 90 days
  (`spec/prd.md` → *Retention*) and `used_by_device_id` is the fleet's enrollment
  provenance; a `DELETE` verb would invite someone to actually delete it. Revocation is
  the same conditional-UPDATE idiom as admin-token revocation, so a second call is
  idempotent rather than a moved timestamp.
- **Group CRUD deliberately does not exist.** Tokens are group-scoped and the schema
  supports it, but nothing creates or lists `device_groups`, so R0 tokens are ungrouped
  in practice. That is correct for R0 (bulk deploy is V3); flagged as a follow-up task
  rather than smuggled in.
- **`bearer_scheme` / `cookie_scheme` moved to `api/deps.py`.** They were private to
  `routers/auth.py`; every protected router needs them, and one credential deserves one
  declaration. `auto_error=False` on both remains essential — with the default, FastAPI
  403s before `require_admin` runs.
- **PROPOSED for `spec/prd.md` → *Requirements & targets*** (spec is protected): promote
  the enrollment-token TTL from the PROPOSED prose in *Security & data posture* into the
  *Timing* table as **enrollment token lifetime = 24 h**, so it sits with the other
  numbers code resolves against.

---

## 2026-09-08 — Admin auth: one credential, two transports (R0-be-1)

- **One credential type.** The login cookie carries *the same* `ffa_` token a CLI
  would send in `Authorization: Bearer`, verified by one code path
  (`api/deps.py::require_admin`). There is no session table and no second credential
  kind, so revoking a dashboard session is the same single `UPDATE` as revoking a
  CLI token. Confirms `design/architecture.md` → *v1 admin auth*.
- **Argon2id pinned to `t=2, m=19 MiB, p=1` behind an `anyio.CapacityLimiter(2)`.**
  The library defaults (64 MiB, and Starlette's 40-thread threadpool) would peak
  around 760 MiB inside a 256 M container — an OOM kill under concurrent logins.
  Verification reads the parameters out of the stored PHC string, so the profile can
  change later without invalidating existing hashes.
- **The verification cache memoizes the hash comparison only.** The row is read and
  `revoked_at` / `expires_at` re-checked on **every** request; only the ~40 ms argon2
  comparison is skipped, keyed by `(token_id, sha256(secret))` for 60 s. Caching an
  `AuthContext` instead would silently break instant revocation — which is the entire
  reason JWT was rejected. Checks run parse → row → revoked/expired → verify, so a
  revoked token also cannot burn CPU.
- **`ADMIN_PASSWORD_HASH` holds the hash, never the password, and must be
  SINGLE-QUOTED in `.env`.** Verified empirically: unquoted, docker compose
  interpolates the `$argon2id` / `$v` / `$m` segments away and the container receives
  `=19=19456`; the failure mode is a login that can never succeed and a log line that
  does not say why. `python-dotenv` strips the quotes, so one quoted line serves both
  the host process and compose interpolation. `docker-compose.yml` uses
  `${ADMIN_PASSWORD_HASH:?…}` with **no default** — a shipped default admin
  credential is worse than a stack that refuses to boot.
- **`Secure` is unconditional.** `http://localhost` is a secure context, so there is
  no dev/prod cookie switch for anyone to flip in production. Cookie attributes are
  `HttpOnly; Secure; SameSite=Strict; Path=/`, set and cleared identically. No CSRF
  token: the dashboard is same-origin by construction, which is also why CORS
  middleware must never appear. Rejected `__Host-`: no subdomains, `Path=/` and
  `Secure` already fixed, and inconsistent browser behaviour over `http://localhost`.
- **Login rate limiting is per-process** (one uvicorn worker per container), keyed on
  the leftmost `X-Forwarded-For` entry with a **global backstop bucket**, because
  Traefik appends to that header rather than replacing it and the key is therefore
  client-spoofable. Only failures are counted, and both buckets are checked before
  any argon2 work.
- **`db/base.get_session()` deleted.** It called the `lru_cache`d
  `get_sessionmaker()` directly, so `dependency_overrides[get_sessionmaker]` did not
  affect it and a test would have quietly used the developer's dev database. **All**
  API database access goes through `Depends(get_sessionmaker)`. Supersedes the
  hand-off note in `.claude/plans/R0-db-1-schema.md` §12.
- **PROPOSED for `spec/prd.md` → *Requirements & targets*** (spec is protected, so
  these are not written there): session/cookie lifetime **7 days**; login rate limit
  **5 failures / 60 s per client IP, 30 / 60 s global**; argon2id profile
  **t=2, m=19 MiB, p=1**.

---

## 2026-09-08 — The standalone Compose stack is the dev environment (R0-infra-1)

- **The stack is both the dev loop and the V2 self-host artifact, and it is the
  default dev environment specifically so it cannot rot.** `spec/prd.md` promises
  "ships as one Docker Compose stack" while production is a *fragment* of a shared
  stack; the only way both stay true is to use the whole thing every day.
  `just up-prod` (base compose only: built images, nginx, no bind mounts, no
  `--reload`) is the guard that the production-shaped path still builds, and it is
  meant to be run before every commit.
- **One image, two commands.** A single root `Dockerfile`; `api` and `ingestor` are
  the same image with a different `command:`. Confirms `design/production.md` →
  *Open decisions*.
- **MQTT reaches the broker only through Traefik's `mqtt` entrypoint, even in dev.**
  Mosquitto publishes no host port, so the dev path and the prod path are the same
  path. Dev uses ``HostSNI(`*`)`` with no TLS; prod (`R0-infra-3`) uses
  ``HostSNI(`bingo.tvaroska.sk`)`` + `tls.certresolver` and forwards plaintext
  internally. Traefik therefore joins the `backend` network here, which the shared
  Traefik in `services/prod` does not yet do.
- **The API is not routed by Traefik at all.** nginx in the frontend container owns
  `/v1` on the dashboard's origin, which makes "no CORS" structural rather than
  configured. `tests/test_api_health.py::test_no_cors_headers` exists so that a
  future "quick CORS fix" fails loudly; a browser CORS error against this app means
  the nginx proxy is wrong.
- **Dev-only anonymous broker access is quarantined** in
  `mosquitto/conf.d/10-dev-anonymous.conf`, the single file `R0-sec-1` deletes.
  Nothing in `mosquitto.conf` grants or restricts topic access, so the broker's
  security posture is a directory listing rather than a config audit.
- **`.env` is the HOST configuration and is never `env_file:`d into a container.**
  It holds `localhost:5433` for alembic/pytest/just; containers get
  `postgres:5432` set explicitly. Compose still reads `.env` for `${VAR}`
  interpolation. pydantic-settings gives real environment variables precedence over
  `.env`, so an `env_file:` here would silently point the API at its own namespace.
- **TLS is deliberately absent.** `http://localhost` is a secure context, so Web
  Serial (`R0-fe-3`) and `Secure` cookies (`R0-be-1`) both work; V2's TLS problem is
  left unsolved but unobstructed (a commented ACME block in the Traefik command and
  an `FF_ACME_EMAIL` placeholder).
- **Gotchas learned, all of which cost time:** (1) the mosquitto CLI clients force
  TLS whenever the port is 8883 and cannot be talked out of it, so the plaintext dev
  broker on the prod-parity port must be exercised with paho — `just mqtt-pub` /
  `just mqtt-sub` exist for exactly this, and the failure mode (`Protocol error`)
  looks like a broken TCP router. (2) **Traefik silently skips containers that are
  not `healthy`**, so a broken healthcheck presents as a 404 from the entrypoint,
  not as an unhealthy badge; `node:22-slim` has neither `wget` nor `curl`, and nginx
  listens on IPv4 only, so container probes must use `127.0.0.1`, never `localhost`.
  (3) The production image does not chown `/app` to the runtime user — the code is
  root-owned and read-only to `appuser`.

---

## 2026-09-08 — Schema, and the conventions the codebase inherits (R0-db-1)

The first code in the repo, so these are settled for everything after it.

- **Spelling is `enrollment` / `enroll` (US), everywhere.** `spec/device-protocol.md` is
  the near-frozen wire contract and it says `POST /v1/enroll`; `TODO.md` and
  `design/architecture.md` say `/v1/enrol`. The spec wins. **Proposed correction:** fix
  those two documents to `/v1/enroll` as part of R0-be-4, which owns the endpoint.
- **No PostgreSQL ENUM types.** The server must tolerate agents it cannot update
  (`spec/device-protocol.md` → *Evolution rules*), and a PG enum needs a migration before
  it can store a value a future agent invents — an ingest that raises on an unknown
  `link_type` or `state` is a silent fleet-visibility outage. Vocabulary lives in Python
  `StrEnum`s; the columns are `TEXT`. **Sole exception:** `devices.power_class` has a
  CHECK, because derived presence is only *defined* for `always_on` / `sleepy`.
- **Token wire format is `{prefix}_{uuid-hex}.{secret-b64url}`** (`ffa_` admin, `ffe_`
  enrollment). Argon2id hashes are salted and therefore not searchable, so the row's UUID
  must ride in the token as the indexed lookup key; only the secret half is verified
  against `secret_hash`. Plaintext is never stored.
- **The enrollment burn is one conditional `UPDATE … RETURNING`**, correct under
  PostgreSQL's default READ COMMITTED; zero rows back means already burned/revoked/expired.
  Never SELECT → check → UPDATE. Statement is in the `EnrollmentToken` docstring and
  proven by a two-connection race test.
- **Devices soft-delete (`decommissioned_at`) and `deploy_events.device_id` is
  ON DELETE RESTRICT.** `deploy_events` is kept forever ("the metric history is the
  product's evidence") while every device must stay removable; RESTRICT makes destroying
  KPI history impossible rather than merely discouraged.
- **`devices.device_id` (eFuse MAC, `^[0-9a-f]{12}$`) is the natural PK**, because it is
  also the MQTT username the two `%u` pattern ACLs depend on. The format CHECK is a
  security control, not tidiness.
- **Layout: `src/fleetforge/`, uv, SQLAlchemy 2.0 async + asyncpg, Alembic revisions
  `NNNN_slug`, ruff + mypy, pytest against a real Postgres migrated by Alembic.** One
  package because one image ships two entrypoints (`api`, `ingestor`). Dev Postgres
  publishes **5433** — 5432 on this host belongs to an unrelated container.
- **`deploy_events` is deliberately not a Timescale hypertable:** forever retention, tiny
  volume, and an outgoing FK. R3's telemetry table is the hypertable case.

---

## 2026-09-08 — Bingo retirement completed (R0-infra-0)

- **Decision:** Bingo deployment fully retired from production. Containers stopped and
  removed, database backed up to `gs://btvaroska/retired/bingo/` then dropped, all
  deployment scripts and runbooks updated. Domain `bingo.tvaroska.sk` now free for
  fleetforge. Repository and Artifact Registry images intentionally kept as historical
  artifacts.
- **Why:** Freed 384 MB of declared container limits on a host swapping ~1 GB. Fleetforge
  needs ~512 MB, so net addition is ~128 MB. Also freed the domain with existing Let's
  Encrypt cert (kept to avoid fresh ACME challenge).
- **Gotchas learned:** (1) Removing services from docker-compose.yml does not stop running
  containers - must explicitly stop before deploy. (2) Smoke tests must be updated in same
  commit that removes services to avoid deploy auto-rollback. (3) Init scripts are inert
  on existing volumes - database drop requires explicit `DROP` commands. (4) Found
  `prod/bingo.env` tracked in git despite being in `.gitignore` (gitignore doesn't apply
  to already-tracked files) - filed as separate security task for other tracked env files.
- **Verification:** Post-retirement checks confirmed container count 12→10, memory freed,
  Traefik route 404, database/role dropped with backup verified restorable, full deploy
  pipeline green, other services unaffected.
- **Task:** R0-infra-0 completed 2026-09-08. Details in
  [docs/features/infrastructure.md](docs/features/infrastructure.md).

---

## 2026-09-08 — Adopted gen-3 planning layout

- **Decision:** Migrated from `PLAN.md` + `docs/` to the gen-3 layout used by every
  other repo in the estate: `TODO.md` (live status only), `spec/` (the WHAT,
  status-free), `design/` (the HOW, status-free), `docs/` (planning/ops), this file,
  and `CRITICAL.md`.
- **Moves:** `docs/SPEC.md` → `spec/prd.md`; `docs/device-protocol.md` →
  `spec/device-protocol.md`; `docs/FLOWS.md` → `spec/flows.md`; `docs/DESIGN.md` →
  `design/architecture.md`; `docs/architecture.md` → `design/production.md`;
  `docs/RELEASES.md` → `docs/releases.md`; `PLAN.md` → `TODO.md` (task IDs lowercased,
  `R0-BE-1` → `R0-be-1`, tables → checkbox items).
- **Why:** fleetforge was the last repo on the old layout, and root `CLAUDE.md` still
  documented it. The restructure done the same day had already rebuilt gen-3's
  *distinctions* (requirements vs. design vs. tasks) under the old filenames, so the
  migration was mechanical.

---

## 2026-09-08 — Artifacts in GCS, MinIO for self-hosting

- **Decision:** Artifact bytes live in GCS (`gs://btvaroska/fleetforge/`) behind a narrow
  object-store adapter (`put` / `get` / `signed_url` / `delete`). MinIO is the
  self-hosted backend, S3-compatible, arriving with V2 turnkey self-hosting.
- **Why:** GCS signed URLs *are* the mechanism the `stage` command already specifies —
  short-lived, signature-as-authorization, range-capable, served without touching the
  API process. Artifact bytes also stay off the prod VM's 5.5 G of free disk and off its
  bandwidth.
- **Cost, accepted:** v1 has a cloud dependency for artifact storage. The adapter
  boundary is what keeps removing it a configuration change. Recorded honestly in
  `spec/prd.md` → *Security & data posture*.
- **Details:** [design/production.md](design/production.md) → *Artifact storage*.

---

## 2026-09-08 — Retire bingo; fleetforge takes `bingo.tvaroska.sk`

- **Decision:** The unfinished bingo app is retired from production and fleetforge reuses
  its domain. Not a public product until V3, so the domain is an operational detail.
- **Why:** frees 384 M of declared container limits on a box already swapping ~1 G, plus
  an existing Let's Encrypt route. Fleetforge needs ~512 M, so the net addition is ~128 M.
- **Not decided:** whether to delete the bingo repo or its Artifact Registry images.
  Retiring the deployment is not deleting the project. The bingo **database must be
  backed up before the role is dropped** — the one irreversible step (`R0-infra-0`).

---

## 2026-09-08 — The ingestor is the only MQTT subscriber

- **Decision:** A single-instance ingestor process is the sole MQTT subscriber; it writes
  to Postgres and `NOTIFY`s. API workers `LISTEN` and fan out over SSE. The API never
  subscribes.
- **Why:** N uvicorn workers each holding a subscription would ingest every message N
  times, and an SSE client on worker A would never see an event ingested by worker B.
  Both failures are silent until the worker count goes above one.
- **Why not Redis:** Postgres `LISTEN/NOTIFY` is sufficient at this scale and the
  database is already there. Mirrors the `content-api` / `content-worker` split already
  running on the same host.
- **Details:** [design/production.md](design/production.md) → *The single-subscriber rule*.

---

## 2026-09-08 — Requirements & targets written down (PROPOSED)

- **Decision:** `spec/prd.md` gained a *Requirements & targets* table — capacity, timing,
  retention, KPI thresholds — marked **PROPOSED** pending review. Downstream docs resolve
  against it instead of each deciding for themselves.
- **Why:** the doc set specified mechanisms with no numbers. `device-protocol.md` had
  "heartbeat default interval" as an open item — a spec decision leaking into a protocol
  doc. Metrics existed with no thresholds, so they could not fail.
- **Consequence:** exposed three missing tasks, now in R1 — a signed-URL + range download
  endpoint (Flow 2 promised resumable download with nothing to serve it), writing
  `deploy_events` from R1 (or R5 arrives with two KPIs and no history), and enforcing
  retention rather than only ingesting.

---

## 2026-09-08 — Enrolment over HTTPS, not MQTT; tokens are single-use

- **Decision:** A device exchanges its enrolment token at `POST /v1/enrol` over HTTPS for
  a per-device broker credential, then connects to the broker already credentialed. The
  token burns on use.
- **Why:** the original flow had the device present its token *to the broker*, which
  would force Mosquitto to authenticate clients it has never heard of against a
  group-scoped token — a custom auth plugin bridging broker to control plane. Instead the
  broker only ever sees fully-credentialed clients and its authz collapses to two pattern
  ACLs. The agent already needs an HTTPS client for artifact download, so this is free.
- **Single-use:** a group-scoped token surviving in flash would let anyone with physical
  access to one board enrol arbitrary devices, and on a public-facing broker there is no
  LAN perimeter to hide behind.
- **Details:** [spec/device-protocol.md](spec/device-protocol.md).

---

## 2026-09-08 — The device owns the reboot, and the rollback

- **Decision:** Two authority rules, both device-side. The device decides *when* to apply
  and may sit in `awaiting_safe_window` indefinitely; and the confirm timer is armed on
  the device before the reboot, so rollback is the device's decision, never a server
  command.
- **Why:** a drone rebooting mid-flight falls out of the sky, and a board that cannot
  reach the broker is exactly the board that must roll back — it will never receive a
  command telling it to. The server observes and records; it never forces a reboot.
- **Details:** `design/architecture.md` principle 5; `spec/prd.md` → *Scope*.

---

## 2026-09-08 — MQTT is the control plane only

- **Decision:** MQTT carries identity, presence, commands, status and telemetry. **HTTPS
  carries artifact bytes.** MQTT never carries payload.
- **Why:** MQTT has no range requests, so any drop restarts the whole transfer, and the
  broker would buffer the image per subscriber on a group deploy. The `stage` command
  carries a short-lived signed artifact URL instead.
- **Supersedes:** the initial spec's implication that MQTT was the delivery transport.

---

## 2026-09-08 — Flash-time immutables frozen at R0

- **Decision:** The full A/B partition table (`nvs`/`otadata`/`phy_init`/`ota_0`/`ota_1`,
  4 M flash minimum), `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`, and the eFuse posture
  (Secure Boot v2 **off**, anti-rollback **off**, flash encryption **off**) ship from the
  very first flash at R0 — even though nothing writes the second slot until R2.
- **Why:** an OTA image writes *into* a partition; it cannot rewrite the partition table,
  and the bootloader is the one update with no rollback path. Wrong at R0 means
  physically retrieving every deployed board — the exact intervention this product exists
  to remove.
- **Consequence:** R5 ships **app-level signature verification**, not Secure Boot v2.
  Secure Boot v2 burns a key digest to eFuse and needs a re-signed bootloader, so it can
  never be enabled on an already-deployed board — it is post-v1 and new-devices-only.
  The two are not interchangeable.
- **Details:** [design/architecture.md](design/architecture.md) → *Flash-time immutables*.

---

## 2026-09-08 — Version structure: V1 safe OTA, V2 build, V3 swarm

- **Decision:** V1 = R0–R5, ~5 heterogeneous boards, safe OTA. V2 = R6–R10, VCS +
  server-side compile + simulation. V3 = robotic swarm (gateway + drones).
- **Moved out of v1:** groups & bulk deploy (five different builds have nothing to
  bulk-deploy) → V3; the advisory simulation gate → V2/R8.
- **Rejected as v1 scope:** a 100+ node swarm. Aspirational, not v1. What v1 *does* pay
  for is schema and shape only — `link_type` / `power_class` / `parent_device_id`,
  derived presence, device-owned reboots — never speculative machinery.
- **Rule learned:** take what is a schema or config decision; defer what is machinery.

---

## 2026-09-08 — v1 is a hosted instance; IP-bearing links only

- **Decision:** v1 ships as a single hosted, single-tenant instance on a public domain
  with Let's Encrypt TLS. Turnkey self-hosting is V2. The device contract requires an
  IP-bearing link and TLS, nothing more — never "Wi-Fi".
- **Why hosted:** onboarding friction is the make-or-break risk, and the hard part of
  self-hosting is TLS without public DNS (a local CA the browser *and* the device trust).
  Deferred, not solved — it returns in full at V2.
- **Consequence:** the broker and artifact endpoint are on the public internet from R0,
  so per-device broker credentials, topic ACLs and single-use enrolment tokens are R0
  requirements, not post-v1 hardening. There is no LAN perimeter to fall back on.
- **Gateway-mediated non-IP radios** (Zigbee/BLE/LoRa) cannot reach a hosted server at
  all and need an on-site gateway — a second product, deferred to V2+.
