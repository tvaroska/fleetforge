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

---

## protocol — proposals from the 2026-10-02 spec review

Filed as proposals, not edits: `device-protocol.md` is near-frozen (`CRITICAL.md`).

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
the server-side regex needs to widen later; no device recall is involved. The 2026-10-15 focus group proposes `^[a-z0-9_-]{1,32}$`; that shape admits the broker's
service usernames, so any widening needs a reserved-name rule first (R4-spec-1, not built).

**Attestation before the first OTA.** `rollback_capable` is measured by an OTA, so a
board's first OTA runs unprotected if its bootloader lacks rollback, and nothing on the
wire says so beforehand. Candidate: a `bootloader_sha256` in `up/announce`, compared
with a catalogue of known-good bootloader digests (the agent bundle's, and the Arduino
core's per core version). Blocked on a bench measurement of whether the on-flash digest
equals the bundle file's, since esptool rewrites the image header's flash parameters at
write time. The same digest would invalidate a stale persisted `rollback_capable` after
an Arduino IDE upload.

**Bootloader-armed watchdog for an OTA image that does not contain the agent.** The confirm
timer lives in the agent (`ff_mqtt.c::confirm_timeout_cb`), so an image without it arms no
timer. The library marker read from the image at upload ([device-protocol.md](device-protocol.md)
→ *Library marker*, R3-spec-3) catches the plain-sketch mistake at pre-check as the gating
warning `no_library_marker`, and the R5 custom self-test (`device-protocol.md` → `up/status`
→ *Custom self-test*) covers an image that links the agent but fails its own checks. Still
open: a hardware watchdog the bootloader arms before entering a `PENDING_VERIFY` image and
only the agent's confirm path disarms, so a silent image that links the library without
starting it resets and rolls back. It needs a custom bootloader (a one-time USB flash, not
OTA-able), applies only where we own the bootloader (not Arduino's), and it is unverified
whether IDF's startup disables that watchdog before the app runs.
