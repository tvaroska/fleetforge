# Fleetforge — Open Questions

Material things that are undecided. Recorded rather than guessed. Answering one means
moving it into `spec/` proper and deleting it here.

---

## enrollment — unaided onboarding

**How much of the diagnosis belongs to firmware rather than the panel?** The panel
classifies text the board happens to print, which is a parser chasing log strings — it
broke on 2026-09-11 precisely because `E BOD:` does not match the ESP-IDF log format.
An alternative is for the agent to report structured faults it already knows about
(`esp_reset_reason()` at boot is one line of C, and would make "this board is in a
reset loop" a fact rather than an inference). Not resolved because it trades a fragile
parser for a firmware round-trip on every new fault, and firmware updates are the thing
that is hardest to ship to a board that will not come online.

**What is the diagnostic bundle's format, and is it ever transmitted?** Copy-to-
clipboard as text is assumed for now. A structured format that the server could accept
would make fleet-wide onboarding failure rates measurable, but it turns a local
debugging aid into an API and a data-retention question. Deliberately deferred.

**Does the repeat path deserve its own design?** Onboarding board #2..#N is currently
identical to board #1 — re-enter Wi-Fi, re-detect chip, re-flash. Remembered profiles
and batch flashing are listed under Post-v1, but if the first fleet is realistically
more than a handful of boards, that is a v1 concern rather than a later one.

**Proposed: a second CUJ for the prebuilt-agent path.** `cujs.md` → *CUJ-1* is the
maker's journey and reaches the fleet list through the **library**, so the flasher-page
path that R0 actually built is only covered as one of its steps. The agent path is a
journey in its own right — "I want to see this thing work before I commit to it" — and
unlike CUJ-1 it is fully playable today, which would give T3 something to grade before
R3 lands. Not written here because `R3-spec-1` scoped itself to the first CUJ.
Filed 2026-09-22 (R3-spec-1).

---

## protocol — proposals from the 2026-10-02 spec review

Filed as proposals, not edits: `device-protocol.md` is near-frozen (`CRITICAL.md`).

**`artifact.type` on `dn/cmd` `stage` — document the default now.** `design/artifacts.md`
carries an advisory `kind`; the wire command has no counterpart. Adding an optional field
is additive, so it can wait for a platform that needs it. What cannot wait is the
sentence "absent means `app`", so the meaning of absence is fixed before anyone assigns
another. FPGA bitstreams and PX4 payloads reach the device through a companion CPU
(`docs/PLATFORMS.md`), so the type may never need to cross the wire.

**Does `ff_cfg` need room for an optional CA root?** `ff_cfg` carries up to 4080 payload
bytes and its reader ignores unknown keys (`agent/main/ff_cfg.c`), so an optional `ca`
key (PEM or DER) is additive with no version bump. One RSA root is ~1.3–2 KB, an ECDSA
root well under 1 KB, against an `ssid`/`psk`/`token` baseline of a few hundred bytes —
it fits, tightly for RSA. Open: whether the V3 gateway's device-facing hop is part of the
`device-protocol.md` contract at all (gateway "republishes, core stays unaware"), in which
case the CA root and local clock are the gateway's problem and not the agent's. Decide
in the V3 design (`docs/SWARM.md`), but confirm the 4 KB budget before `ff_cfg` v1 ships
on more boards.

**`device_id` format — leave the regex, state the rule.** `DEVICE_ID_RE`
(`^[0-9a-f]{12}$`, `identity.py`) and `FF_DEVICE_ID_LEN` (`ff_identity.h`) are
trust-boundary validators; widening them now buys V1 nothing. Proposed prose: the server
treats `device_id` as opaque, the platform defines its derivation, and a non-ESP32
platform with a different id takes a new tree or a `proto` bump (evolution rule 1). Only
the server-side regex needs to widen later; no device recall is involved.

**Confirm semantics for sleepy / airborne devices — narrower than reported.** The confirm
timer is device-armed after the reboot, which only happens inside the safe window, so an
airborne drone does not roll back for being in the air. The real question is whether a
sleepy node confirms within its first wake; `confirm_timeout_s` is already per-command.
Needs a statement in `device-protocol.md`, not new fields.

**Attestation before the first OTA.** `rollback_capable` is measured by an OTA, so a
board's first OTA runs unprotected if its bootloader lacks rollback, and nothing on the
wire says so beforehand. Candidate: a `bootloader_sha256` in `up/announce`, compared
with a catalogue of known-good bootloader digests (the agent bundle's, and the Arduino
core's per core version). Blocked on a bench measurement of whether the on-flash digest
equals the bundle file's, since esptool rewrites the image header's flash parameters at
write time. The same digest would invalidate a stale persisted `rollback_capable` after
an Arduino IDE upload.

**Field Wi-Fi change.** Partly answered (R2b-spec-1). A board flashed with a list of
known networks moves between them with no re-flash ([device-protocol.md](device-protocol.md)
→ *Known networks*, `flows.md` Flow 3). A network not on the list still needs a re-flash,
and whether that holds once boards are sealed in boxes (CUJ-1) is open; see the *repeat
path* question above. The writable store (NVS or `ff_cfg`), and `dn/cmd` `set_cfg` versus
Improv as the way in, move to R2b-spec-3. Not decided.

**An OTA image that does not contain the agent.** The confirm timer lives in the agent
(`ff_mqtt.c::confirm_timeout_cb`), so an image without it arms no timer. The bootloader
holds it in `PENDING_VERIFY`, and it is rolled back only if the board resets; otherwise it
runs unconfirmed and offline, the deploy stays at `rebooting`, and no remote action
reaches it. The pre-check refuses a merged binary and a wrong layout or slot size. Two
candidates. (1) Decided for detection (R3-spec-3): the library marker read from the image
at upload ([device-protocol.md](device-protocol.md) → *Library marker*), shown at
pre-check as the gating warning `no_library_marker`. It catches the plain-sketch mistake,
not a library that is linked but never started, and not broken logic. (2) Open: a watchdog
the bootloader arms before it enters a `PENDING_VERIFY` image and only the agent's confirm
path disarms, so a silent image resets and rolls back. It needs a custom bootloader (a
one-time USB flash, so not OTA-able), applies only where we own the bootloader (not
Arduino's), and it is unverified whether IDF's startup disables that watchdog before the
app runs. Neither covers an image that has the agent and confirms but whose own logic is
wrong; that is the R5 custom self-test.
