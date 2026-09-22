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

---

## ota-library — where does a library user's config live?

**The agent's answer does not transfer.** Broker URL, API origin, Wi-Fi credentials and
the enrollment token live in `ff_cfg`, a 4 KB flash partition at `0x12000` that the
browser flasher writes per board (`design/partitions.md` → §3). That works because the
agent owns its whole flash layout. A maker who adds the library to an Arduino sketch does
not: an Arduino IDE build uses its board definition's partition scheme, which has no
`ff_cfg`, and overwriting a custom partition table from the IDE is a routine accident.
A partition cannot be added by OTA, so this cannot be fixed after the first flash.

Two candidate answers, neither chosen:

- **(a) Ship a packaged partition table + board definition and require it.** Mostly
  packaging. But the library then only works for users who adopt `ab-4m-v1` exactly, and
  the failure mode for getting a flash-time immutable wrong is a physical recall
  (`CRITICAL.md`).
- **(b) Add an NVS-backed config path** so the library runs on a stock Arduino partition
  scheme. Far more users, but it is a real change to `ff_cfg` and the enrollment flow,
  and NVS is not a partition the flasher can write before first boot — so the
  credentials have to arrive some other way (a serial handshake, or Improv).

Not resolved because the answer depends on what an Arduino build actually does to the
table in practice, which nobody here has measured. `R3-fw-1` is a spike whose only output
is that measurement plus a recommendation. Filed 2026-09-22 (R3).
