# Fleetforge — Standards & Requirements

THE WHAT. Status-free by construction: no counts, no progress, no dates. Live task
state lives only in `TODO.md`; as-built design lives in `design/`.

Protected. `/implement` proposes changes here, it does not make them.

---

## enrollment

### Unaided onboarding: flash → on the fleet

**Requirement:** A technician with no ESP32 knowledge must be able to take a board from
the flasher page to green in the fleet list, or else learn — from the product, in plain
language — what is wrong and what to do about it. Reading a raw UART log is not an
acceptable step in any path, successful or failed.

The boundary is deliberate. Onboarding starts when the operator is on the flasher page
with a board plugged in, and ends when that board appears online in the fleet list.
Getting an account, minting a token and reaching the page are separate concerns.

**The standard the product is held to:** a board's own log is the only source of truth
about what happened to it before it reaches the broker, and the browser is the only
thing that can read it. So the console panel — not the server — is the diagnostic
surface of record for onboarding. Anything the board says that the panel cannot explain
is a defect in the panel, not an inconvenience for the operator.

**Acceptance Criteria:**

- [ ] **No silent failure.** For every failure mode the board can express on its
      console, the panel names a cause in plain language and gives a next action.
      Explicitly includes lines carrying no ESP-IDF log tag — brownout (`E BOD:`), reset
      reasons (`rst:0x…`), panics (`Guru Meditation`, `assert failed`, `Backtrace:`) —
      which are today discarded before classification and so cannot produce a fault.
- [ ] **No stale truth.** A milestone shown as reached is true *now*. A board that
      reboots retracts what the previous boot proved; a reboot loop is itself surfaced
      as the fault rather than as a frozen checklist.
- [ ] **No unbounded wait.** Every milestone has a deadline. When it passes, the panel
      says what should have happened, what usually prevents it, and what to try. A
      spinner that can spin forever is a failed acceptance.
- [ ] **The boot is never missed.** The operator sees the board's boot log from the top
      without pressing anything, on every path that ends in a board being reset.
- [ ] **Recovery in place.** Where a fault's fix is software rather than physical, the
      panel offers it as a button — retry enrol, re-flash, mint a fresh token, reboot —
      and the operator is not required to know which applies.
- [ ] **Clean escalation.** One click produces a diagnostic bundle: the full console
      log, the loaded config summary, chip and flash identification, firmware and server
      versions, and the current fault. Secrets — enrolment token, Wi-Fi passphrase, MQTT
      credentials — are redacted, and the redaction is tested, not assumed.
- [ ] **Exercised unaided.** A person who has not seen the codebase onboards a board
      end-to-end using only what is on screen, and separately diagnoses a deliberately
      induced fault — a brownout, a wrong PSK, a spent token — without assistance. This
      is the criterion that actually decides the feature; the others are its parts.

---

## infrastructure

### Agent bundles are artifacts, not image contents

**Requirement:** The agent firmware bundles the browser flasher writes to a board must
be distributed as versioned artifacts through the same object-store path as user
artifacts, not baked into the application container image. Publishing a new agent
bundle must not require rebuilding or redeploying the application.

The driver is arithmetic. Every supported chip target adds ~1.2 MB to every application
image, and the target list only grows — `esp32`, `esp32s3`, `esp32c3`, `esp32c6` today,
with ESP32-H2, a Thread path and a Raspberry Pi adapter already named in the roadmap. But
the size is the lesser half: baking the bundles welds firmware to the application's
release cadence, so an agent fix cannot ship without a full app deploy, and a bundle can
silently miss a release that the application half of the same image did not.

**One path, not two.** Fleetforge distributes exactly one kind of firmware artifact by
exactly one mechanism. That the agent bundle is a build output and a user artifact is
user data is a fact about provenance, not a reason for a second distribution path.

**What must survive the move.** Verification is not a property of the filesystem it
reads from: every bundle is sha256-verified part by part and checked for
`partition_layout` / `ota_slot_size` agreement with `spec/device-protocol.md` before it
is servable, and a bundle failing either is dropped with its target named rather than
served. Provenance — `source_commit` and the digest-pinned IDF image in the manifest —
stays attached to the bundle.

**Acceptance Criteria:**

- [ ] **The image ships no firmware.** A built application image contains no agent
      bundle, verified by inspecting the image rather than the Dockerfile.
- [ ] **Publishing is not deploying.** A new agent bundle is published and a board
      flashes it, with the running application neither rebuilt nor restarted. This is
      the decoupling the feature exists for; nothing else proves it.
- [ ] **Corruption is still refused.** A bundle whose bytes disagree with its manifest,
      and one whose `partition_layout` or `ota_slot_size` disagrees with the protocol,
      are both refused with the target named — vacuity-checked by corrupting each.
- [ ] **An unreachable store is a named fault, not a broken page.** When bundles cannot
      be fetched, the flasher says so in plain language and does not offer a manifest it
      cannot honour. Held to the same standard as *Unaided onboarding* above: a board
      that cannot be flashed must say why.
- [ ] **Self-hosting is unaffected.** The dev and self-host stacks serve bundles through
      the same interface against MinIO, with no GCS dependency.
