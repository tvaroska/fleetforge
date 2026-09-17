# Fleetforge — Open Questions

Material things that are undecided. Recorded rather than guessed. Answering one means
moving it into `spec/` proper and deleting it here.

---

## enrollment — unaided onboarding

**There are no written CUJs.** `spec/cujs.md` does not exist, so
"Unaided onboarding: flash → on the fleet" has nothing to declare a `Supported By`
against. The onboarding journey is the most obvious first CUJ the project has — R0's
whole stated risk — and writing it would give the acceptance criteria in
`standards.md` something to hang from. Filed 2026-09-11.

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

## ota-deploy — one artifact size limit, written twice

**`prd.md`'s "Artifact size ≤ 1.9 MB" is the rounded form of `ota_slot_size`, and the
two should not read as independent caps.** `spec/device-protocol.md` fixes
`ota_slot_size` at **1966080** bytes, which is 1.875 MiB — "1.9 MB" to two significant
figures. They are one number. Read as two, they invite an implementation with two
thresholds a few kilobytes apart and a rejection nobody can explain from the message.

R1-be-1 implements the authoritative one only: `firmware/manifest.py::SUPPORTED_LAYOUTS`,
because that is the mapping tied to the partition table a board actually carries, and it
is already what agent-bundle validation uses — so an upload and a bundle cannot disagree
about how big a slot is.

Proposed: `prd.md` → *Requirements & targets* should either say "≤ the target layout's
`ota_slot_size` (1966080 B for `ab-4m-v1`)" or drop the line and cite
`device-protocol.md`. Not applied here — `spec/` is protected during `/implement`.
Filed 2026-09-16 (R1-be-1).
