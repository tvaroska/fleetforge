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

**Supported By:** [`cujs.md`](cujs.md) → *CUJ-1*, step 3 — the one flash that turns a
board on the desk into a board in the fleet list. The criteria below are that step's
parts; CUJ-1 is what they add up to.

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

---

## ota-library

### Embeddable OTA: the four verbs in the user's own firmware

**Requirement:** A maker must be able to add Fleetforge OTA to firmware **they wrote**,
without adopting the prebuilt agent and without migrating their project to ESP-IDF. The
device-facing contract — enroll, announce, heartbeat, and the four verbs
`stage → apply → confirm → rollback` — must be consumable as a library from both ESP-IDF
and Arduino, and the library must carry the same safety posture as the agent: A/B slots,
a bootloader with rollback enabled, and a device-armed confirm.

**One protocol, two consumers.** The library and the prebuilt agent speak the same
`spec/device-protocol.md` and are built from the same C. A behaviour that exists in one
and not the other is a defect, not a feature tier — the agent is the library's first
consumer, not its privileged sibling.

**The safety posture is not optional.** A board running the library must be as
recoverable as a board running the agent. A library configuration that produces a device
which cannot roll back is a configuration the library must refuse to build or refuse to
enroll, not one it warns about — the whole product claim is that a bad push comes back
by itself.

**Honest capability reporting.** A build that does not reproduce `ab-4m-v1` exactly must
announce a different `partition_layout`, and be rejected by the server's capability
check. A rejected deploy is the intended failure; a silent brick is not.

**Supported By:** [`cujs.md`](cujs.md) → *CUJ-1* — the journey this standard exists to
make possible, end to end: Alex's own sketch becomes a fleet member (steps 1–3), updates
itself (step 5), and recovers from a bad build unaided (step 6).

**Acceptance Criteria:**

- [ ] **A sketch becomes a fleet member.** Starting from an ordinary Arduino sketch that
      does something visible, a maker adds the library, flashes once over USB, and the
      board appears in the fleet list — with no Fleetforge agent involved.
- [ ] **Their own firmware updates itself.** A second build of *that* sketch, with a
      visible behaviour change, is deployed from the dashboard and the board runs the new
      behaviour and reports the new version.
- [ ] **A bad build of their own firmware recovers itself.** A deliberately broken build
      is deployed to a library-based board; it rolls back unaided and reports
      `rolled-back`.
- [ ] **The example is the documentation.** A worked example — enroll → heartbeat →
      handle `stage` → report version — builds unmodified from a clean checkout on both
      ESP-IDF and Arduino, and is short enough to read in one screen.
- [ ] **A wrong flash layout fails loudly.** A build whose partition table does not match
      the layout it announces is rejected at deploy time with a message that names what
      is wrong, rather than being flashed and bricked.
- [ ] **Written as a journey.** `spec/cujs.md` exists and carries the "sketch and a
      DevKit on the desk" CUJ, and *Unaided onboarding* above references it.
