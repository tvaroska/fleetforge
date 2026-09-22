# Fleetforge across chips — ESP32 family, STM32, FPGA

**Date:** 2026-09-21
**Status:** product review and recommendation — not a spec, not a plan.
**Audience:** anyone reading “ESP32 first, architected to grow to Raspberry Pi
and eventually FPGAs” and asking what that actually costs.
**Companions:** [`HOBBYIST.md`](HOBBYIST.md) · [`SWARM.md`](SWARM.md)

This is a reading of the current aim, documentation and feature set against
one question: *what would it take for a board that is not an ESP32 to be a
first-class member of the fleet — including the STM32s that actually fly, and
the FPGAs that cannot run an agent at all?* Requirements stay in `spec/`; live
tasks stay in `TODO.md`. Landing any of the recommendations below is a
`/new-feature` (or a release), not an edit of this file.

---

## 0. The waist, and where it already leaks

The claim in [`design/architecture.md`](../design/architecture.md):

> The server is **platform-blind**; every platform difference lives in a
> swappable adapter, so ESP32 → Pi → FPGA reuse one core.

The contract that is supposed to never change: identity, derived presence,
four verbs, optional parent, opaque artifact. The table that is supposed to
be the whole adapter:

| Platform | Artifact | Apply | Confirm | Rollback |
|---|---|---|---|---|
| ESP32 | `.bin` | write OTA1 | broker reconnect + self-test | switch to OTA0 |
| Raspberry Pi (later) | OS image / container | flash B slot / pull image | systemd/health OK | boot A slot |
| FPGA (later, via companion CPU) | bitstream | reload fabric / partial reconfig | fabric ID + self-test | reload prior bitstream |

**STM32 is not in the table.** It appears once in the whole tree, as a
paid Velxio emulation overlay that was *rejected*. For a product that wants
hobbyists and airframes, that is the loudest silence in the architecture:
STM32 is the default MCU on flight controllers (PX4, ArduPilot, Betaflight),
on a large fraction of “serious” maker boards, and on most industrial
sensors that are not Espressif.

The waist is real **on the server**. A `.bin`, an OS image and a bitstream
are already the same blob to `ObjectStore` and to `stage`. The leaks are
everywhere the device is onboarded, identified, or laid out in flash:

| v1 fact | ESP32-shaped? | Breaks on STM32 / FPGA |
|---|---|---|
| `device_id` = eFuse MAC, 12 hex chars | Yes | STM32 has a 96-bit UID, not a MAC. An FPGA fabric has no stable ID. |
| `partition_layout: "ab-4m-v1"` + `ota_slot_size: 1966080` | Yes | MCUBoot slots, dual-bank STM32, SPI-flash bitstream A/B are not 1.9 MB IDF OTA partitions. |
| Enrollment flasher = `esptool-js` / Web Serial | Yes | STM32 is DFU / ST-Link / OpenOCD. FPGA is JTAG / `iceprog` / Vivado. |
| Artifact cap 1.9 MB | Yes (the slot) | STM32H7 images can exceed it; a mid-range FPGA bitstream often does. |
| SNTP before TLS | Yes (no RTC) | STM32 often *has* an RTC; an FPGA companion in a valley still does not (see [`SWARM.md`](SWARM.md)). |
| Flash-time immutables = IDF partition table + `APP_ROLLBACK_ENABLE` + eFuse | Yes | Different bootloader, different one-way burns, different recall story. |
| `platform_type` = `CONFIG_IDF_TARGET` | Yes | Fine as a string; currently the only values that exist are `esp32` / `s3` / `c3` / `c6`. |

Principle 1 of the design is “the fewer assumptions in the core, the cheaper
a wrong guess about Pi/FPGA.” The protocol is marked near-frozen. **The
guesses that will hurt are already on the wire:** identity format, layout
id, slot size. Additive fields are cheap. Re-typing `device_id` is a recall.

So the chip question is not “can the server store a bitstream.” It can.
The chip question is: **what must be true of a new platform before it can
speak v1 without a protocol break**, and **which platforms are agents vs
cargo**.

---

## 1. Aim — three different kinds of “chip”

Treat these as different products that share a server, not as rows in one
table.

### A. Agents — the chip runs Fleetforge

Needs IP + TLS, an A/B (or equivalent) story, a stable id, and a port of
the four verbs.

| Family | In v1 | Notes |
|---|---|---|
| ESP32 / S3 / C3 / C6 | **Yes** — bundles, flasher, `platform_type` | The only implemented adapter. |
| ESP32-S2 / C2 / H2 / P4 | Mapped in `make_manifest.py`, not built | Cheap. H2 is the Thread radio; P4 is the high-end. 2 MB H2 variants may not hold Thread + mbedTLS in `ab-4m-v1`. |
| STM32 (F4/F7/G4/H7/U5, WB/WL) | Absent | Needs an agent port and a *link*. Almost no STM32 has Wi-Fi on die. |
| nRF52 / 53, RP2040 / 2350 | Absent | Same shape as STM32 if they have IP (nRF usually Thread/BLE, not Wi-Fi). MCUBoot is the portable A/B. |
| Raspberry Pi / Linux SBC | Named, not built | Different artifact (image/container), still an agent. The swarm vehicle. See [`SWARM.md`](SWARM.md) #3. |

### B. Cargo — the chip cannot run the agent

Needs a **companion** that already is an agent. The core never talks to the
cargo. Same shape as the V3 drone-behind-gateway and as non-IP radios.

| Family | Companion | Artifact |
|---|---|---|
| Discrete FPGA (iCE40, ECP5, Artix, Tang Nano, Cyclone) | ESP32 / STM32 / Pi on the same board | Bitstream in SPI flash, two slots |
| SoC FPGA (Zynq, PolarFire SoC, Agilex) | The hard ARM/RISC-V is the agent | Bitstream *and* a Linux image — two artifact types, one device |
| Radio / sensor die (SX1262, a separate FC STM32, a camera ISP) | The board’s MCU | Vendor image, often via a vendor protocol (MAVLink fw update, etc.) |

[`design/architecture.md`](../design/architecture.md) already says this for
FPGA: “FPGAs can't run an agent, so they're managed **through their
companion CPU** (real FPGA products are SoCs anyway).” It is the right
sentence. It is also the sentence that makes a first-class `platform_type:
fpga` a mistake. The registry row is the companion. The bitstream is a
second artifact *on that row*, or a child with `parent_device_id` if you
insist on tracking fabric identity separately.

### C. The ESP32 family itself is already “different chips”

Classic Xtensa, S3 Xtensa, C3/C6 RISC-V, different bootloader offsets
(`0x1000` vs `0x0` — `firmware/manifest.py` exists so nobody re-derives
that), native USB vs bridge UART, 4 MB vs 8 MB flash. v1 paid for this
and it is the proof that `platform_type` + per-target bundles work. It is
**not** the proof that a different vendor works: every one of those still
speaks ESP-IDF, still flashes through `esptool-js`, still announces an
eFuse MAC, still uses `ab-4m-v1`.

---

## 2. Documentation — a table pretending to be a plan

### What is good

- Opaque artifacts + four verbs is the correct waist. Vivado is already
  named as a valid *producer* in principle 2, next to `idf.py` and
  PlatformIO. That sentence is how an FPGA bitstream enters the product
  without a special path.
- Companion-CPU = FPGA companion = V3 gateway = non-IP radio gateway.
  Three times the docs refuse to put a second protocol in the core. Keep
  refusing.
- Renode is the named growth simulator (Cortex-A / RISC-V / Xtensa).
  Verilator/cocotb for FPGA. `qemu-system-aarch64` for Pi. The sim-runner
  contract is pluggable on purpose because no engine spans this ladder.
- `partition_layout` is an opaque string with a registry
  (`SUPPORTED_LAYOUTS`). A new layout is a new id, never an edit of
  `ab-4m-v1`. That *mechanism* ports. The *only entry* does not.
- Flash-time immutables are written down as a class of decision, not as
  “the ESP32 partition CSV.” The class is what STM32/FPGA need a page of.

### What is missing or wrong

**STM32 is not a platform in the product.** Not in the adapter table, not
in out-of-scope (which lists “Raspberry Pi & FPGA implementations” and
skips the MCU that would actually show up next), not in the agent, not in
open questions. A swarm builder with a Pixhawk will assume they are
unsupported. They are.

**FPGA is a row, not a design.** No bitstream slot layout, no “companion
announces a `capabilities: ["fpga"]` and accepts a second artifact type,”
no size cap other than 1.9 MB (too small for anything past ice40/Artix-7
35T), no confirm story beyond “fabric ID,” no note that reloading the
fabric of a flight computer is an `awaiting_safe_window` event even when
the companion CPU does not reboot.

**Identity is specified as eFuse MAC.**
[`spec/device-protocol.md`](../spec/device-protocol.md) says
`device_id` = eFuse MAC, lowercase hex, no separators, and the topic
namespace is built on it. STM32 UID is 96 bits. A Pi can use a MAC, an
FPGA cannot. This has to become “opaque identifier, 1–32 safe chars, unique
per device, announced at enroll” *before* a second silicon vendor, while
the only agents in the field still *happen* to send a MAC. Additive
tolerance (server accepts not-a-MAC) is cheap today; a format change after
R0 boards exist is a recall.

**`ota_slot_size` is doing two jobs.** It is the capability check *and* the
v1 1.9 MB product cap. A Pi image, an STM32H7, a Kintex bitstream all fail
a check that is really “will this fit in `ab-4m-v1`.” The check must key
off `(platform_type, partition_layout) → max size`, which
`SUPPORTED_LAYOUTS` already almost is — if a Pi layout and an `fpga-2m-v1`
layout can be rows. [`spec/open-questions.md`](../spec/open-questions.md)
already wants the PRD line folded into that map. Fold it before the second
platform, not after.

**Enrollment is the ESP on-ramp, not the product on-ramp.** Unaided
onboarding via Web Serial is a real v1 standard
([`spec/standards.md`](../spec/standards.md)) and it is *correctly*
ESP-only. A STM32/FPGA “flasher page” would be a different component
(WebDFU, a local OpenOCD bridge, or a CLI). Do not stretch `esptool-js`
until it pretends to be a universal programmer. Document CLI/factory as
the non-ESP enroll path on day one of the first non-ESP adapter, or that
adapter has no onboarding story.

**Pi is named more often than it is designed.** Roadmap “Beyond,”
standards (bundle combinatorics), SWARM #3. Still no artifact type, no
A/B scheme (RAUC / Mender / dual rootfs / container tag?), no
`platform_type` value, no enroll (you do not Web-Serial a Pi). For mixed
fleets the Pi is the next *agent*, not an FPGA-class companion.

---

## 3. Feature set — what each family actually needs

### ESP32 family (now)

Working: four targets built, flasher picks a bundle by `chip_family`,
capability check rejects the wrong `platform_type`, QEMU for classic
esp32. Not working: H2/P4/S2 bundles, native-USB re-acquire on C3/C6/S3
(`S0-test-2`), brownout on the one classic esp32 in hand (`S0-fw-3`).

Adding a new Espressif target is a `sdkconfig.defaults.<target>`, a
builder run, and a `make_manifest.py` line that already exists for H2/P4.
It is not a platform adapter. Do it when Thread (H2) or a high-end
companion (P4) is a real board on the desk, not to prove the waist.

### STM32 (absent)

An STM32 can be an **agent**. It cannot use the ESP32 adapter.

**Link.** The contract is IP + TLS, never “Wi-Fi.” STM32 needs one of:

- Ethernet PHY (F7/H7 Nucleo, industrial boards) — closest to “it just
  works,” and the swarm vehicle’s sibling.
- A Wi-Fi coprocessor (ESP-Hosted, AT, ATWINC, Cypress) — common on maker
  boards, and a second firmware *on the same PCB* (cargo).
- STM32WB/WL — BLE/sub-GHz on die, not IP. Those are gateway-mediated
  non-IP, i.e. the second product, not an agent.
- Cellular modem — IP, fine, ugly.

Without a link story, an STM32 agent is a library that cannot enroll.

**A/B.** Three different STM32 realities:

| Hardware | Rollback story |
|---|---|
| Dual-bank flash (many G0/G4/H7/U5) | Swap banks; closest to ESP OTA0/OTA1 |
| Single bank + external SPI flash | MCUBoot slots; portable, also covers nRF |
| Single bank, no extra flash | Not safely OTA-able. Do not pretend. |

MCUBoot (Zephyr / NCS / a Cube port) is the adapter worth writing once.
A Cube-only dual-bank driver is a second adapter the week someone shows
up with an F4.

**Identity.** 96-bit `UID` → a 24-hex `device_id` (or a hash truncated to
the current 12-hex if you refuse to widen). Spec must stop saying “eFuse
MAC.” Topics and ACLs do not care what the string *means*, only that it
is stable and matches `%u`.

**Apply / confirm / rollback.** Same four verbs. Apply = MCUBoot mark
secondary pending + reboot (device-owned). Confirm =
`boot_write_img_confirmed()`. Rollback = unconfirmed reboot, MCUBoot
reverts. This is R2’s ESP-IDF `esp_ota_mark_app_valid_cancel_rollback`
under another name. The self-test entrypoint (R4) ports as a function
the agent calls after MQTT is up.

**Enroll.** Not Web Serial esptool. Options, in decreasing fantasy:

1. Factory CLI (`openocd` / `st-flash` / `dfu-util`) writing agent +
   `ff_cfg`-equivalent into MCUBoot slots. Honest, matches “CLI flasher
   is post-v1.”
2. USB DFU from the browser (WebUSB). Chromium-only again, different
   component from the ESP flasher.
3. First flash with ST-Link at the desk, every later image over OTA —
   the actual industrial path.

Unaided onboarding as specified (`standards.md`) is an ESP standard.
Do not block an STM32 adapter on a technician flashing from Chrome.

**Artifact size.** `SUPPORTED_LAYOUTS["mcuboot-2m-v1"] = …` (or whatever
the slot is). Do not shove STM32 images through `ab-4m-v1`.

**Flight controllers.** PX4/ArduPilot already have a bootloader and a
MAVLink firmware-update path. Competing with that *on the FC* is a
political and safety fight Fleetforge will lose. The winning STM32 in a
swarm is either (a) a non-FC board (sensor, radio companion, payload) or
(b) **cargo**: the Pi/ESP32 companion stages a PX4 `.px4` and applies it
through MAVLink, confirm = the FC reports a version. That is the FPGA
pattern with a different blob. See top 3.

### FPGA (named, empty)

An FPGA should **not** be an agent. The architecture is right; the row
in the adapter table is the part that misleads.

**Discrete FPGA + MCU/SBC.** One registry device: the companion.
Capabilities include `fpga` (or `payload`). A second artifact type
(`bitstream`) is `stage`d to the same device with a different `type` on
the blob. Apply = companion writes SPI-flash slot B, pulses PROGRAM_B /
does iceprog, does *not* reboot itself unless it must. Confirm = read
back device ID / a user register / a self-test in the fabric. Rollback =
load slot A. Safe window still applies: reconfiguring the fabric that
is the radio or the motor driver is an in-flight reboot by another name.

**SoC FPGA (Zynq et al.).** The PS *is* a Pi-class agent. The PL is
cargo on the same chip. Two artifacts, one `device_id`, two confirm
paths. Partial reconfiguration is a third artifact type; ignore it until
someone has a full-bitstream path that is boring.

**Size.** ice40up5k ~100 KB, Artix-7 35T ~1.6 MB (squeaks under 1.9 MB),
Artix-7 100T ~4 MB, anything Xilinx mid-range is tens of MB. The v1 cap
is an ESP slot, not a law of the product. A bitstream layout with a
32 MB cap is a `SUPPORTED_LAYOUTS` row, plus Range download (already in
R1) and probably deltas (V3, and actually load-bearing here).

**Sim.** Verilator/cocotb as named. That is the R8 sim-runner for this
adapter, not QEMU. Do not wait for it: a bitstream that enumerates and
passes a fabric ID is a confirm; HDL sim is the V2 gate.

**Enroll.** You flash the companion, not the FPGA. The companion’s
enroll path (ESP Web Serial, STM32 CLI, Pi image) is the only onboarding.
The first bitstream can be baked into the companion image or staged on
first contact.

**Identity.** Fabric serial is optional metadata. The device the server
knows is the companion. If you need to track “this physical FPGA was
swapped onto a new companion,” that is an inventory problem, not a
protocol one — and out of scope the same way application config is.

---

## 4. Top 3 features for a multi-chip fleet

Ranked by *unlock*: the smallest change that makes a non-ESP32 board a
member rather than a hope. The OTA library in [`HOBBYIST.md`](HOBBYIST.md)
is the ESP32 extraction; this list is what that extraction has to leave
behind so a second port is possible.

### 1. Depin the protocol from Espressif — while only ESP32s exist

**Do this before any STM32 or FPGA work.** The protocol is near-frozen;
every ESP32 already in the field has to keep working. All of these are
additive if done now, breaking if done later.

1. **`device_id` is an opaque token**, 1–32 of the existing safe charset.
   ESP32 agents keep sending eFuse MAC. The spec stops *requiring* that.
   STM32 UID and a Pi MAC then fit without a v2 topic tree.
2. **`SUPPORTED_LAYOUTS` is the size cap**, keyed by
   `(platform_type or layout id)`. `ab-4m-v1 → 1966080` stays. The PRD
   “≤ 1.9 MB” line dies (already proposed in
   [`spec/open-questions.md`](../spec/open-questions.md)). A future
   `mcuboot-2m-v1` or `bitstream-32m-v1` is a row, not a product argument.
3. **Artifact `type` is real.** Architecture already says the blob has a
   declared type. v1 effectively has one (`app` / ESP `.bin`). Add
   `app` | `os-image` | `bitstream` | `payload` as an opaque string the
   core stores and the *adapter* interprets. Capability check: a device
   without `fpga` cannot be staged a bitstream.
4. **`partition_layout` stays an opaque string.** Do not invent
   STM32-shaped fields. The adapter on the device knows what its layout
   id means; the server only knows the id and the slot size.
5. **Enrollment path is per-adapter.** The public API (`POST /v1/enroll`,
   token, baked config) stays. The *bytes-on-the-wire-to-flash* are an
   ESP client today. Spec that a platform adapter may enroll via CLI /
   DFU / pre-baked image, and that unaided-onboarding-in-Chrome is an
   ESP-family standard, not a product-wide one.

Without this, the first STM32 port either lies (`device_id` is a fake
MAC), or breaks the frozen protocol. The work is documentation plus a
few validators. It is the cheapest recall-prevention in the repo.

### 2. STM32 as an agent — MCUBoot, IP via Ethernet or a coprocessor, CLI enroll

**The missing row.** Hobbyists will show up with Nucleos. Swarm builders
will show up with everything-except-the-FC as STM32, and the FC as
PX4 cargo (see #3).

Ship a thin port, not a CubeIDE product:

1. **Zephyr + MCUBoot agent** speaking v1 MQTT/HTTPS. Zephyr gives you
   STM32 *and* nRF with one codebase; Cube-only is a trap. The
   [`HOBBYIST.md`](HOBBYIST.md) library extraction is the API this port
   implements — if the ESP32 library is “the IDF app,” the STM32 port
   cannot share it. Extract C (or a C-shaped protocol core) first.
2. **Link adapters:** Ethernet on H7 as the first T2; ESP-Hosted/AT as
   the maker path. WB/WL are *not* v1 agents.
3. **Layout id** `mcuboot-<size>-v1`, slot size from the board’s
   `SUPPORTED_LAYOUTS` row. Dual-bank STM32 can be a second layout later.
4. **CLI enroll** (`just flash-stm32`) writing MCUBoot + agent + config.
   No Web Serial. Document it as the non-ESP onboarding path.
5. **Identity from UID**, now legal because of #1.

Do not start with a Pixhawk. PX4’s bootloader and safety model are not
MCUBoot-from-Fleetforge, and a bad OTA on an FC is the swarm crash
[`SWARM.md`](SWARM.md) exists to prevent. First STM32 should be a
boring H7 Nucleo that blinks, then a payload board.

### 3. Cargo artifacts on a companion — FPGA bitstreams, and PX4 as the same shape

**Do not add `platform_type: fpga`.** Do add “this managed device can
apply a second blob to something it owns.”

One mechanism, three customers:

| Companion | Cargo | Apply |
|---|---|---|
| ESP32 / Pi on a Tang Nano / iCE40 / Artix board | Bitstream | Write SPI slot, pulse PROGRAM |
| Pi / ESP32 next to a Pixhawk | PX4 / ArduPilot image | MAVLink (or the FC’s own bootloader) |
| Pi gateway in [`SWARM.md`](SWARM.md) | A radio module, a camera HAT, a daughter STM32 | Vendor protocol |

The core sees: a device with `capabilities` including `payload` (or
`fpga`, `fc`), an artifact with `type` ≠ `app`, a `stage` of that type,
status transitions the companion reports (`applying` means “I am writing
the fabric,” not “I am rebooting”). Confirm is companion-defined (fabric
ID, FC version, a self-test). Rollback is the companion loading the
previous cargo slot — **the companion’s own A/B is a different slot.**
A bad bitstream must not brick the agent that is supposed to roll it
back. That is the FPGA-specific flash-time immutable: the companion’s
image and the bitstream live in different storage, and only the
bitstream is in play during a fabric `stage`.

This is also how mixed air/ground fleets absorb STM32 FCs without
pretending Fleetforge is PX4. The vehicle is a Pi agent (#3 in
[`SWARM.md`](SWARM.md)); the FC is cargo; the airframe’s ESP32 radio
is a second agent or also cargo. Three chips, two adapter kinds, one
server.

Size and Range already exist; what cargo needs from #1 is `type` and a
layout whose cap is not 1.9 MB. What it needs from R2 is: rolling back
*cargo* must not reboot a flying companion, and a cargo confirm-fail
must not take the agent down with it.

---

## Honourable mentions (not top 3, not ignore)

| Feature | Why it is not #1–#3 | When it starts to matter |
|---|---|---|
| **ESP32-H2 / P4 bundles** | Still the ESP adapter. Cheap, not a waist test. | H2 when Thread is real; P4 when you want a companion that is still Espressif. |
| **Raspberry Pi agent** | Named, load-bearing for swarm, different artifact (`os-image`). | [`SWARM.md`](SWARM.md) #3 — the vehicle. Do it as an *agent* adapter, not as cargo. |
| **nRF + Thread** | Falls out of a Zephyr STM32 agent if #2 is Zephyr. | 100-node low-power flock; after deltas. |
| **RP2040 / 2350** | Hobbyist-popular, weak OTA story on 2040, no on-die IP. | After STM32; do not let it queue-jump. |
| **WebDFU / OpenOCD-in-the-browser** | Enrollment theatre. CLI is enough for v1-of-STM32. | If unaided-onboarding is ever an STM32 standard. It should not be, yet. |
| **Partial reconfiguration** | A third artifact type on the FPGA companion. | After full-bitstream cargo is boring. |
| **Renode / Verilator in R8** | Sim is a gate, not a port. | After there is an artifact the gate could reject. |
| **Vivado/IceStorm as R9 builders** | Producers behind the same upload API. | After cargo `type` exists; otherwise R9 only knows `idf.py`. |

---

## What not to do

- **A universal flasher.** `esptool-js` plus a pile of `if (stm32)` is how
  unaided onboarding dies for everyone. Per-adapter enroll, shared API.
- **`platform_type: fpga` as a registry device that MQTT-s.** It will not.
  Companion or nothing.
- **OTA on the PX4 FC as a Fleetforge agent.** You will fight the PX4
  bootloader, the safety pilot, and every GCS. Cargo via MAVLink, or
  leave the FC alone.
- **WB/WL / nRF-without-IP as v1 agents.** Non-IP is a gateway product.
- **Growing `dn/cfg` to “which bitstream.”** Artifact `type` + `stage` is
  the channel. Config is still agent tuning.
- **Pulling Pi/STM32/FPGA into v1.** v1 is five ESP32s and an unproven
  bench. Depin the protocol (#1) *during* v1 so v2/V3 ports are possible.
  Do not port during R0 brownouts.
- **One `ota_slot_size` for the product.** That number is a layout’s slot.
  Treating it as physics is what makes a bitstream look illegal.

---

## Suggested sequencing

```
v1 (now)     Depin identity / layouts / artifact type / enroll-path   ← #1
             Close R0/R1 on metal, R2 rollback on ESP32
             Extract the protocol library (HOBBYIST #1) as C, not IDF

v1-adjacent  ESP32-H2/P4 only if a board is on the desk

after R2     STM32 agent, Zephyr+MCUBoot, Ethernet first, CLI enroll  ← #2
             Cargo artifact type on an ESP32 companion + ice40        ← #3
             (smallest FPGA, proves the shape)

with swarm   Pi agent (SWARM #3)
             PX4 image as cargo on that Pi
             Bitstream cargo if the vehicle has fabric

later        nRF/Thread via the Zephyr port
             Zynq as Pi-class agent + bitstream cargo
             Renode/Verilator as R8 backends
```

The architecture can carry STM32 and FPGA — opaque blobs, four verbs,
companion-as-parent, IP-bearing link. What it cannot carry, as written,
is a second silicon vendor through an eFuse-MAC identity, a 1.9 MB slot,
and a Web Serial flasher. Fix those while the only metal in the fleet
still happens to be Espressif. Then STM32 is an agent port, FPGA is
cargo, and the table in `design/architecture.md` finally matches the
waist it claims to describe.

---

## Related

- [`docs/HOBBYIST.md`](HOBBYIST.md) — library extraction is the ESP32 side of #2
- [`docs/SWARM.md`](SWARM.md) — Pi agent, PX4-as-cargo, companion = gateway
- [`design/architecture.md`](../design/architecture.md) — adapter table, companion CPU
- [`spec/device-protocol.md`](../spec/device-protocol.md) — identity, layout, hierarchy
- [`spec/prd.md`](../spec/prd.md) — Pi & FPGA out of v1; “wrong guess” as a risk
- [`spec/open-questions.md`](../spec/open-questions.md) — 1.9 MB vs `ota_slot_size`
- [`src/fleetforge/firmware/manifest.py`](../src/fleetforge/firmware/manifest.py) — `SUPPORTED_LAYOUTS`
- [`agent/tools/make_manifest.py`](../agent/tools/make_manifest.py) — Espressif target map already includes H2/P4
