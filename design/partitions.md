# Flash layout `ab-4m-v1` — the partition table and what depends on it

**Area:** firmware · **Scope:** the flash map every Fleetforge board carries, the
`ff_cfg` blob that makes two boards flashed from one bundle different, and the rules for
changing any of it.

This is the narrative owner for the flash layout. The **authoritative machine-readable
copy is [`agent/partitions.csv`](../agent/partitions.csv)**; the wire form is
[`device-protocol.md`](../spec/device-protocol.md) (`partition_layout`, `ota_slot_size`).
Where this document and those two disagree, they are right and this is stale.

It exists because the layout is a contract between six files in four languages, and until
now the reasoning was distributed across all six. It also answers a question nothing else
here does: **what someone outside this repo must reproduce to run the protocol on their
own firmware** — see *Reproducing this layout* at the end.

---

## 1. The map

A 4 MB part. Offsets little-endian, as ESP-IDF writes them.

```
0x000000  ┌────────────────────────┐
          │ (bootloader)           │  0x1000 on ESP32, 0x0 on S3/C3/C6 — chip fact,
0x008000  ├────────────────────────┤  never typed in this repo, read from
          │ partition table   4 KB │  build/flasher_args.json
0x009000  ├────────────────────────┤
          │ nvs              24 KB │  device credential, RF calibration cache
0x00F000  ├────────────────────────┤
          │ otadata           8 KB │  which slot booted, and the PENDING_VERIFY flag
0x011000  ├────────────────────────┤
          │ phy_init          4 KB │  unused in our build (S0-fw-4); calibration is in nvs
0x012000  ├────────────────────────┤
          │ ff_cfg            4 KB │  per-board flash-time config — §3
0x013000  ├────────────────────────┤
          │ (alignment)      52 KB │  app partitions must start 64 KB-aligned
0x020000  ├────────────────────────┤
          │ ota_0          1920 KB │  0x1E0000 = 1 966 080 = ota_slot_size
0x200000  ├────────────────────────┤
          │ ota_1          1920 KB │  same size, always — the server's capability
0x3E0000  ├────────────────────────┤  check assumes one slot size per layout
          │ (unallocated)   128 KB │  §5
0x400000  └────────────────────────┘
```

Identical for every target (`esp32`, `esp32s3`, `esp32c3`, `esp32c6`). Only the
bootloader offset differs, and nothing in this repo hardcodes it.

### Why each row is the way it is

- **No `factory` partition, deliberately.** A factory-only board can never OTA its way to
  A/B — it would need a table rewrite, which OTA cannot do. Ship the full A/B map from
  the first USB flash even though nothing writes `ota_1` until R2.
  `make_manifest.py` refuses to emit a bundle whose decoded table contains one.
- **Two slots of *equal* size.** `up/announce` reports a single `ota_slot_size`, and the
  server's pre-flight capability check compares an artifact against that one number. Slots
  of different sizes would make that check true for one slot and false for the other.
- **`ff_cfg` reserved at R0, before its payload format existed.** A partition cannot be
  added later, so the space had to be claimed before anyone knew exactly what went in it.
- **`nvs` is guarded, not wiped.** The device credential obtained at enrollment lives
  here, and so does the RF calibration cache. The flasher proves it is not writing into
  `nvs` by parsing the table it is flashing in the same operation
  (`frontend/src/partitionTable.ts`) rather than by hardcoding `0x9000` — which would be
  right today and silently wrong for the next layout.
- **1920 KB per slot on a 4 MB part** is the practical maximum once the data partitions
  and 64 KB alignment are paid for. The connect-only agent builds to ~1.08 MB of it (55%)
  before R2 adds OTA and R6 adds signature verification.

---

## 2. The three flash-time immutables

An OTA image writes *into* a partition. It cannot rewrite the table, add a partition, or
change a bootloader build-time option. Three things are therefore fixed at first USB
flash — get one wrong and every deployed board needs physical retrieval, which is the
exact intervention this product exists to remove. Full rationale in
[`architecture.md`](architecture.md) → *Flash-time immutables*.

| Immutable | Where | Value |
|---|---|---|
| Partition table | `agent/partitions.csv` | `ab-4m-v1`, §1 |
| Bootloader rollback | `agent/sdkconfig.defaults` | `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y` — R2's auto-rollback depends on a decision made at R0 |
| eFuse burns | `agent/sdkconfig.defaults` | Anti-rollback, Secure Boot v2, flash encryption: **all off**, each for a stated reason |

Two notes that are easy to get backwards:

- **Anti-rollback is off because it would block R2.** It burns a monotonic version
  counter that forbids booting an older image — which is precisely what rollback does.
- **Secure Boot v2 and R6's app-level signing are not interchangeable.** Secure Boot
  burns a key digest to eFuse and needs a re-signed bootloader, so it can never be
  enabled on already-deployed boards; it is post-v1 and new-devices-only. R6 verifies a
  signature over the artifact in software, which is deliverable over OTA.

Every safety option lives in the common `sdkconfig.defaults`, never in a per-target file.
The per-target files carry hardware facts only (console peripheral, PSRAM, the OpenCores
NIC QEMU emulates) and each says so in its header.

---

## 3. `ff_cfg` — the per-board blob

The one thing that differs between two boards flashed from the same bundle: broker URL,
API origin, Wi-Fi credentials, and the single-use enrollment token. It is a partition
rather than a build input so that N devices can share one artifact
([`artifacts.md`](artifacts.md)).

**Format** — 16-byte little-endian header, compact UTF-8 JSON, `0xff` to the end of the
4 KB partition:

```
0x000  4  magic        "FFCF"
0x004  2  version      u16 = 1
0x006  2  reserved     u16 = 0
0x008  4  payload_len  u32, 1 … 4080
0x00c  4  crc32        u32, IEEE (the polynomial zlib.crc32 uses)
0x010  N  payload      compact JSON object
```

Payload keys, in emit order: `api_base`, `mqtt_uri`, `token`, `ssid`, `psk`, `link`,
`ntp`, `hb_s`, `power`, `wake_s`. `api_base` and `mqtt_uri` are the two the firmware
cannot invent a default for.

**Three implementations, one contract:** `agent/tools/ff_cfg.py` (writer; its module
docstring is the authoritative prose), `agent/main/ff_cfg.c` (firmware reader),
`frontend/src/ffcfg.ts` (browser writer). `frontend/src/ffcfg.vector.json` is a golden
vector both test suites assert against, ASCII-only on purpose — Python's `json.dumps`
defaults to `ensure_ascii=True` and `JSON.stringify` does not, so a non-ASCII SSID
produces different bytes from the two writers even though both decode to the same object.
The contract is the decoded object; the vector can only pin bytes where they agree.

**Failure posture: loud and terminal.** Any error — bad CRC, wrong version, erased,
missing key — makes the agent idle rather than fall back to a compiled-in default. A
board that boots with a bad config and does nothing is diagnosable from its serial log;
one that invents a server URL connects somewhere unexpected and is not.

---

## 4. The same numbers, written down six times

`0x1E0000` / `1966080` / `1920K` / `1.9 MB` appear in six places. Five of those copies are
deliberate; one is a defect.

| Where | Form | Why it is a copy |
|---|---|---|
| `agent/partitions.csv` | `0x1E0000` | **The source.** |
| `spec/device-protocol.md` | `1966080` | The wire contract. Frozen. |
| `src/fleetforge/firmware/manifest.py` | `EXPECTED_OTA_SLOT_SIZE` | Server-side capability check. Retyped, not imported — the two ends of a contract must be able to disagree. |
| `tests/test_agent_partitions.py` | literal | **The tripwire.** A test that imports the value it guards proves nothing. |
| `docs/runbooks/agent-build.md` | `1920K` | Expected `gen_esp32part.py` decode, for eyeballing a build. |
| `spec/prd.md` → *Requirements & targets* | `≤ 1.9 MB` | **Defect.** The rounded form reads as an independent second cap; it invites an implementation with two thresholds a few kilobytes apart and a rejection nobody can explain. Tracked in [`open-questions.md`](../spec/open-questions.md); the fix is to cite `ota_slot_size` instead. |

**Offsets, by contrast, are never typed.** ESP-IDF writes the authoritative
`{offset: file}` map to `build/flasher_args.json`; `make_manifest.py` reads it and the
manifest carries the numbers through untouched to the API, the browser flasher and the
QEMU harness. `ota_slot_size` itself is decoded from the generated
`partition-table.bin` — the table the bootloader will actually read — not copied from the
CSV that was its input.

### What enforces it

- `tests/test_agent_partitions.py` — no toolchain needed; catches overlapping slots, a
  drifting slot size, a `factory` row creeping back, `ff_cfg` disappearing, the rollback
  option dropped, any eFuse option turned on.
- `just agent-verify <target>` — decodes the real `partition-table.bin` with IDF's own
  `gen_esp32part.py` and greps `sdkconfig.resolved`. Needs the IDF container, so it is not
  part of `just test`.
- `make_manifest.py` — refuses to emit a non-conforming bundle at build time.
- `frontend/src/partitionTable.ts` — refuses to write a plan that lands in `nvs`.

---

## 5. The unallocated tail

`0x3E0000`–`0x400000`, 128 KB, is unallocated on every board in the field. It is **not
reserved for anything** — it is what is left after two 1920 KB slots. Recording it here
because it is the space any future in-flash recovery mechanism would want, and 128 KB is
too small for a usable recovery app. A both-slots-bad recovery partition does not fit on
`ab-4m-v1` and cannot be retrofitted; see [`docs/HOBBYIST.md`](../docs/HOBBYIST.md) §4.2
for what that rules in and out.

---

## 6. Changing any of this

**A new layout is a new id** (`ab-4m-v2`, `ab-8m-v1`, …) plus an entry in
`SUPPORTED_LAYOUTS` plus a `device-protocol.md` change — **never an edit to an existing
row.** Boards already flashed keep `ab-4m-v1` until someone physically retrieves them, so
both layouts must be supported simultaneously, and the layout version is what lets the
server detect and quarantine a board carrying a superseded one.

Per [`CRITICAL.md`](../CRITICAL.md), any task touching `agent/partitions.csv`,
`agent/sdkconfig.defaults` or `agent/sdkconfig.defaults.<target>` auto-escalates:
stronger model, mandatory review before commit.

Procedure for the build side: [`runbooks/agent-build.md`](../docs/runbooks/agent-build.md)
→ *Changing the partition table*.

---

## 7. Reproducing this layout outside the agent build

Everything above describes boards flashed from a Fleetforge agent bundle. Anyone
embedding the protocol in their **own** firmware — the thin OTA library named in
[`prd.md`](../spec/prd.md) — has to reproduce it, and the current design assumes they
will not by accident.

To announce `"partition_layout": "ab-4m-v1"` truthfully, a build must have:

1. A ≥ 4 MB part with `CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y`.
2. A custom partition table byte-identical to §1 — including `ff_cfg` at `0x12000` with
   subtype `0x40`, and no `factory` row.
3. `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`, which is a **bootloader** option and
   therefore cannot be added later.
4. None of the three eFuse options enabled.

Anything short of that is a different layout and must announce a different id. The server
currently accepts exactly one (`SUPPORTED_LAYOUTS` has a single entry), so a mismatch is
a rejected deploy, not a silent brick — which is the intended failure.

**The unsolved part is Arduino.** An Arduino IDE build uses a board definition's own
partition scheme and has no `ff_cfg` partition, so a library user has nowhere to put the
config this design puts in flash. Overwriting a custom table from the Arduino IDE is also
a routine accident. Two ways out, neither yet chosen:

- **Ship a packaged board definition / `partitions.csv`** and require it. Cheapest, but
  the library then only works for users who adopt our flash layout exactly.
- **Add an NVS-backed config path** so the library runs on a stock partition scheme.
  A real change to `ff_cfg` and the enrollment flow, not packaging.

This is a prerequisite for the OTA-library release, not a detail of it.

---

## Related

- [`agent/partitions.csv`](../agent/partitions.csv) — the authoritative table
- [`architecture.md`](architecture.md) → *Flash-time immutables* — why these three things
- [`artifacts.md`](artifacts.md) — `ff_cfg` as a generated per-device bundle part
- [`spec/device-protocol.md`](../spec/device-protocol.md) — `partition_layout` and
  `ota_slot_size` on the wire
- [`CRITICAL.md`](../CRITICAL.md) — escalation rules for these paths
- [`runbooks/agent-build.md`](../docs/runbooks/agent-build.md) — building and verifying a bundle
- [`runbooks/agent-qemu.md`](../docs/runbooks/agent-qemu.md) — booting a flash image with a
  generated `ff_cfg`
