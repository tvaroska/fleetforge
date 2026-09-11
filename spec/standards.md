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
