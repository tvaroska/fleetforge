# Fleetforge — Critical User Journeys

THE WHAT, told as a person's day rather than a system's behaviour. Status-free by
construction: no counts, no progress, no dates. Live task state lives only in `TODO.md`;
as-built design lives in `design/`.

Protected. `/implement` proposes changes here, it does not make them.

Every feature should support at least one journey below, and every journey should be
walkable by a real person without guidance. A CUJ that only an engineer who has read this
repo could complete has already failed.

Each CUJ carries, on top of Description / Steps / Success Criteria / Supported By, the two
fields the verification ladder's T3 tier consumes:

- **Driver** — how the journey is played end to end.
- **Judge** — how a run is scored: deterministic must-pass assertions plus hard-fail
  traps, and the rubric handed to the external LLM judge (`jeep`) against the Success
  Criteria. **Both judges must pass.**

**On segmented Drivers.** A journey may cross several releases, in which case no single
harness can play it yet. Such a CUJ names a harness **per segment** and declares which
segments are gradeable; a segment with no harness is not graded and is not a pass either.
This is a deliberate departure from a single `Driver:` command — the alternative is a CUJ
that is ungradeable in full until its last release lands, which makes T3 inert for
everything it already covers.

---

## CUJ-1: A sketch on the desk becomes a board in the field that fixes itself

**Persona:** Alex, the solo maker (`docs/personas/PERSONAS.md` §1). Writes firmware in the
Arduino IDE or PlatformIO on a laptop, does not use ESP-IDF, and has three to fifteen
boards — most of them already sealed in boxes across a property.

**Description:** Alex has written a sketch that does something they care about, and it
works on a DevKit on the desk. They want that board to go live somewhere awkward and
still be updatable — without adopting Fleetforge's prebuilt agent, without porting the
project to ESP-IDF, and without ever again crawling under a porch with a USB cable.

This is the journey the product exists for. Everything else Fleetforge does is in service
of it: the prebuilt agent is the demo of this, not the substitute for it.

**Steps:**

1. Alex has a working sketch on a DevKit, plugged into the laptop.
2. They add the Fleetforge library to the sketch — an include and a few lines in
   `setup()` and `loop()` — and it compiles in the IDE they already have open.
3. They flash the board once over USB and enter their Wi-Fi details. The board appears in
   the fleet list, online, reporting the version they just built.
4. Alex unplugs it and installs it wherever it is going to live.
5. They change something visible in the sketch, build, and upload that `.bin` from the
   dashboard. The board takes the update and starts doing the new thing; the version in
   the list is the one they uploaded.
6. Later they deploy a build that is broken. The board recovers on its own, comes back on
   the version that worked, and says so. Alex does not get up.

**Success Criteria:**

- Alex never reads a raw UART log — not on the happy path, and not on any failure path.
  When something goes wrong, the product names the cause in plain language and gives a
  next action.
- Alex never opens ESP-IDF, a serial monitor, or the Fleetforge source.
- No board is ever physically retrieved. A bad push comes back by itself; that is the
  whole claim, and a brick is a product failure rather than a missed percentage
  (`prd.md` → *Success criteria*, Fleet safety = 100%).
- A deploy over a healthy link finishes within **5 min** for a 1.5 MB image, and the
  dashboard reflects a state change within **2 s** of the server receiving it
  (`prd.md` → *Requirements & targets*).
- The board that ends up in the field is as recoverable as one running the prebuilt
  agent — same A/B slots, same rollback-enabled bootloader, same device-armed confirm.
  A configuration that cannot roll back fails at build or at enroll; it does not warn.
- The worked example Alex copied from is short enough to read in one screen.

**Supported By:**

- `standards.md` → *ota-library* → **Embeddable OTA** — steps 2, 5 and 6: the library,
  its safety posture, and honest capability reporting.
- `standards.md` → *enrollment* → **Unaided onboarding** — step 3: flash to green in the
  fleet list, with no UART log in any path.
- `standards.md` → *dashboard* → **Getting firmware in is a dashboard operation** —
  step 5's first half. Alex uploads the `.bin` their IDE just built from the dashboard's
  upload form (`flows.md` Flow 2).
- `spec/device-protocol.md` — the wire contract both the library and the agent speak.
- Releases: R0 (enroll) · R1 (OTA transport) · R2 (auto-rollback) · R3 (the library
  itself). See `docs/releases.md`.

**Driver:** segmented — this journey crosses four releases. Each segment names the harness
that plays it and whether it is graded; a segment whose harness does not exist yet is
**not graded**, and neither is a half that names a procedure rather than a command.

| Steps | Segment | Harness | Graded |
|---|---|---|---|
| 1–2 | Sketch compiles with the library added | `just lib-quickstart --build-only`: the worked example's README build steps, run verbatim in a clean copy of the tree — Arduino (PlatformIO) for esp32 and esp32s3 plus the edited second build, and ESP-IDF for esp32 and esp32s3 | yes |
| 3 | One flash → board on the fleet | `just lib-quickstart`, phase 2a (enroll): the example, flashed once in QEMU in place of the USB flash, enrolls and heartbeats against the dev stack and is online in the fleet list on the version it was built with; `pytest tests/test_enroll.py` for the `POST /v1/enroll` surface | yes |
| 5 | OTA a changed build → new version reported | On the device: `just lib-quickstart`, phase 2b (OTA): the edited build is uploaded, deployed and confirmed, and the board reports its version. Server and dashboard: `just update-e2e`, scenarios `upload-good` and `deploy-good` — the build is uploaded from the dashboard form, sent, and the result card says good | yes |
| 6 | A bad build recovers itself | On the device: `just lib-quickstart`, phase 2c (rollback): a build that never confirms is deployed, and the run requires `rolled_back` and the previous `fw_version` and fails on any `confirmed`. Server and dashboard: `just update-e2e`, scenario `deploy-broken` | yes |
| — | A wrong flash layout is refused, not flashed | Server and dashboard: `just update-e2e`, scenario `precheck-wrong-layout` — a refusal card with no Send, and a direct deploy is a 409 that sends nothing. On the device: `docs/runbooks/agent-qemu.md` → *A wrong flash layout is refused* (a procedure) | server and dashboard half |

One full `just lib-quickstart` run plays segments 1–2, 3, 5 and 6 on the device, in that
order, on one emulated board; the API must give that board `10.0.2.2` origins
(`docs/runbooks/agent-qemu.md` → *The worked example, end to end*). QEMU stands in for two
things a board does by itself: the one USB flash, and the reboot after an update (an
`on_command` apply and a power cycle). `just update-e2e` plays the dashboard against
simulated boards that report the versions they were told, so the `fw_version` assertions
of steps 5 and 6 are graded on the `lib-quickstart` run, never on it.

Step 4 (physical install) and the unaided half of step 3 are done by a person; no harness
plays them and T3 does not grade them.

**Judge:**

- *Deterministic (must-pass):*
  - After step 3, the device has a row in the fleet list and is online.
  - After step 5, the `fw_version` the device reports equals the version of the build that
    was uploaded — read from the running image itself (its app descriptor, or in a library
    build the version compiled into it and handed to the library at start), not from what
    it was told to install.
  - After step 6, the device reports `rolled_back` and its `fw_version` is the pre-deploy
    value.
  - A duplicated deploy command produces one download, not two.
- *Hard-fail traps (cap the score at 0):*
  - Any step required reading a raw UART log, or opening ESP-IDF or this repo.
  - Any board needed physical retrieval, or ended in a state it could not boot out of.
  - An enrolment token, Wi-Fi passphrase or MQTT credential appeared in any log, any
    diagnostic bundle, or any URL.
  - A build whose partition table disagreed with its announced `partition_layout` was
    flashed rather than refused.
  - A milestone was shown as reached while it was no longer true, or a wait had no
    deadline.
- *LLM-judge rubric (graded by `jeep` against the Success Criteria):*
  - Every failure the run encountered was explained in plain language and paired with a
    next action — not a log line, not an error code.
  - The steps Alex performed are the steps the documentation says to perform.
  - The worked example is readable in one screen and does not assume ESP-IDF knowledge.
  - Nothing in the run required knowledge only available from the Fleetforge source.
