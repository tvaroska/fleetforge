# Fleetforge — Device Protocol v1

*The device-facing thin waist, on the wire. Companion to [design/architecture.md](../design/architecture.md) (the abstract contract) and [prd.md](prd.md).*

> **This document is load-bearing and near-frozen.** An agent flashed at R0 speaks this
> protocol until someone physically retrieves the board. Additive change is cheap;
> anything else is a recall. Design accordingly.

## Channels

| Channel | Carries | Why |
|---|---|---|
| **MQTT** over TLS (8883) | control plane: identity, presence, commands, status, telemetry | small messages, server-initiated push, LWT presence |
| **HTTPS** | data plane: artifact bytes | range requests, resumable, no broker buffering |

MQTT never carries payload bytes. Both validate against the public CA bundle
(`esp_crt_bundle`) — the hosted server uses Let's Encrypt, so devices need no baked CA.

## Topic namespace

```
ff/v1/d/{device_id}/up/{topic}     device publishes  →  server subscribes
ff/v1/d/{device_id}/dn/{topic}     server publishes  →  device subscribes
```

`device_id` = eFuse MAC, lowercase hex, no separators (`a4cf12b3de90`).
`v1` is the **protocol** version, not the product version.

### Why the up/dn split — the topic design *is* the authz design

MQTT username **= `device_id`**, which makes the entire fleet ACL two pattern rules:

```
pattern write  ff/v1/d/%u/up/#
pattern read   ff/v1/d/%u/dn/#
```

No per-device ACL rows, nothing to provision at enrolment, and a leaked credential lets
a device impersonate only itself. Interleaving directions in one subtree would have cost
two ACL rules *per device*, maintained forever.

The namespace is deliberately **flat** — a drone behind a V3 gateway still owns its own
top-level subtree. Nesting children under parents (`.../d/{parent}/c/{child}/...`) would
bake hierarchy into the namespace and break the moment a drone is reassigned. See
*Hierarchy* below.

## Topics

### Device → server (`up/`)

| Topic | QoS | Retain | Payload |
|---|:--:|:--:|---|
| `up/announce` | 1 | **yes** | full identity; republished on every boot |
| `up/presence` | 1 | **yes** | `{"online":true}`; the LWT publishes `{"online":false}` |
| `up/hb` | 1 | no | heartbeat |
| `up/status` | 1 | **yes** | current update-transaction state |
| `up/ack` | 1 | no | the answer to a command other than `stage` (R5) |
| `up/telemetry` | 0 | no | user-defined metrics, batched samples (R4) |
| `up/log` | 0 | no | firmware log lines, batched (R4) |

### Server → device (`dn/`)

| Topic | QoS | Retain | Payload |
|---|:--:|:--:|---|
| `dn/cmd` | 1 | **never** | a command |
| `dn/cfg` | 1 | **yes** | configuration (intervals, thresholds) |

## Retain and durability — the rule that prevents a class of bugs

**Command durability comes from persistent sessions, never from retained messages.**

A retained `dn/cmd` would be re-delivered to the device on every reconnect — so a board
that reboots after a deploy would replay the stage command that just completed, forever.
Instead the broker holds undelivered QoS-1 commands in the device's **persistent session**
(`clean_session=false`), delivering them once when it next connects. This is precisely
why persistent sessions were chosen, and it is what lets a sleeping e-paper frame receive
a command issued while it was asleep.

Retain is for **state**, not events: `announce` (identity), `presence`, `status` (current
transaction), `cfg`. All are things whose *latest value* a restarting server should learn
immediately without waiting for a device to wake.

*Decommissioning:* retained messages outlive the device. Removing a board means publishing
an empty retained payload to each of its retained topics, or the registry resurrects ghosts.

## Presence — derived, never a socket state

- **`power_class: always_on`** → LWT on `up/presence` is authoritative. Offline within seconds.
- **`power_class: sleepy`** → LWT fires on every normal sleep and means nothing. Presence is
  `now - last_seen < tolerance × expected_wake_interval_s` (tolerance in [prd.md](prd.md) → *Timing*).

Both surface identically through the public API. The server records `last_seen` from **its
own receipt time**, never the device's timestamp — see *Clock* below.

**Planned absence (reserved).** A device about to go quiet on purpose, for example a drone
leaving its dock, may first publish its own retained presence:
`{"online":false,"reason":"sortie","expected_back_s":1200}`. `reason` is an open vocabulary
the server shows as-is. `expected_back_s` is how many seconds after receipt it expects to be
back (receipt time, see *Clock*). The device then closes its session with a clean MQTT
`DISCONNECT`: an unclean drop fires the LWT, whose bare `{"online":false}` replaces the
retained lease. No v1 agent sends either field, and a v1 server treats the message as plain
`online:false`. Both are additive under *Evolution rules*, rule 2, and `proto` stays `1`.
Telling "away" from "overdue" is a later server's job. A parent publishing for its child is
V3 (*Hierarchy*).

## Messages

### `up/announce` — identity

```json
{
  "proto": 1,
  "device_id": "a4cf12b3de90",
  "platform_type": "esp32c6",
  "fw_version": "1.4.2",
  "agent_version": "0.3.2",
  "link_type": "wifi",
  "ssid": "shed",
  "known_networks": 2,
  "power_class": "always_on",
  "expected_wake_interval_s": null,
  "parent_device_id": null,
  "partition_layout": "ab-4m-v1",
  "ota_slot_size": 1966080,
  "flash_chip_size": 4194304,
  "partition_table_sha256": "1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed",
  "rollback_capable": true,
  "capabilities": ["ota", "apply", "cancel", "rollback", "reboot", "selftest", "identify"],
  "reset_reason": "poweron",
  "lib_marker": 1
}
```

`partition_layout` + `ota_slot_size` are what let the server perform the capability check
[flows.md](flows.md) promises ("reject on chip / partition-size mismatch") — without them that check
has no data source. `partition_layout` also lets the server detect and quarantine boards
flashed with a superseded layout.

`capabilities` lists what the device can be asked to do. `ota` means it carries out
`stage`. From R5, `apply`, `cancel`, `rollback` and `reboot` name the commands it
implements, `selftest` means the running image has registered a custom self-test, and
`safe_window` means it has registered a safe-window rule. The server stores a capability it
does not know as-is. A device lists only what is true of the image it runs.

`safe_mode` is `true` while the device holds itself in recovery after repeated crashes:
three `panic`, `int_wdt`, `task_wdt` or `wdt` resets within 60 s on a confirmed image. In
safe mode only the agent runs. It connects, announces and sends heartbeats, carries out
`stage`, `apply`, `rollback` and `reboot`, and never runs the firmware's own code. The device
leaves safe mode after a successful update, on a `reboot` command, or at a power cycle. The
field is absent otherwise, and the server treats absent and `false` alike. Both fields are
additive under *Evolution rules*, rule 2, and `proto` stays `1`.

`flash_chip_size`, `partition_table_sha256` and `rollback_capable` are measurements, where
`partition_layout` is a name. `flash_chip_size` is the physical flash chip size in bytes, not
the size in the image header; a device that cannot read it omits the field.
`partition_table_sha256` is the lowercase hex SHA-256 of the device's decoded partition
table: one line `{type}:{subtype}:{offset}:{size}\n` per entry the table contains,
all four in decimal, sorted by offset ascending, with labels and flags left out. The
expected value for each layout id is in the *Partition layouts* table below.
`rollback_capable` is `true` once the board has booted an OTA-written image that its
bootloader put into `PENDING_VERIFY`, `false` once it has booted one that its bootloader
never put into `PENDING_VERIFY` (the bootloader cannot roll back), and `null` until either
has been observed, which includes every board that has never completed an OTA. It is
never derived from how the firmware was built. All
three are additive under *Evolution rules*, rule 2, and `proto` stays `1`. An absent
field means unknown, and the server never refuses a device for omitting one. The enroll
body carries the same fields, and the server stores a malformed value as null rather
than refusing the request.

`ssid` and `known_networks` say which network the board is on, never how it joined it.
`ssid` is the SSID of the network this broker session runs over, as written in `ff_cfg`.
`known_networks` is how many networks the board will try (after any it dropped for room).
Both are `null` when `link_type` is `ethernet` and absent from agents older than the
known-networks list. The server treats absent and `null` alike, as "not reported". The
announce is republished on every broker connect, so a board that moved to another network
reports it in its next session. The passphrase and the other networks' SSIDs are never
sent.

`lib_marker` is the `format` of the library marker (*Library marker* below) in the image
the board is running, an integer. It is absent when that image has no marker, which
includes every agent built before the library. It is additive under *Evolution rules*,
rule 2, and `proto` stays `1`. The server never refuses or warns on a device for it: the
pre-check reads the marker from the image being sent, not from the board. The enroll body
carries it like every other announce field, and the server stores a malformed value as
null rather than refusing the request.

`reset_reason` is why the chip last reset, as `esp_reset_reason()` reports it at
boot, lowercased without the `ESP_RST_` prefix: `poweron`, `ext`, `sw`, `panic`,
`int_wdt`, `task_wdt`, `wdt`, `deepsleep`, `brownout`, `sdio`, `usb`, `jtag`, `efuse`,
`pwr_glitch`, `cpu_lockup`, or `unknown`. It describes the current boot, so a
reconnect inside one boot repeats it. It is additive under *Evolution rules*, rule 2,
and `proto` stays `1`. It is absent from agents that predate it, and the server treats
absent and `null` alike. The server never refuses or warns on a device for it and
logs a value it does not know as-is. The enroll body carries it like every other
announce field.

`link_type` names the link this broker session runs over. The agent sends `wifi` or
`ethernet`. It is an open vocabulary: the server stores a value it does not know as-is.
`dock` is reserved for a wired bus between a device and the carrier or gateway it docks on
(V3). A link that is not Wi-Fi reports `ssid` and `known_networks` as `null`, as
`ethernet` does.

`sub_versions` is reserved for a device that updates processors besides its own: an object
mapping a component name to the version string that component runs, one level deep, for
example `{"fc_fw": "4.5.1", "vision_model": "0.2.0"}`. It never repeats `fw_version`. No v1
agent sends it. Absent means none reported. It is additive under *Evolution rules*, rule 2,
and `proto` stays `1`; the server ignores it until a release defines what it stores.

`secrets_rev` is the non-negative integer revision of the project/device secrets bundle the
board currently holds in NVS (`ff_sec`). It is absent when the board has not yet pulled a
secrets bundle. It is additive under *Evolution rules*, rule 2, and `proto` stays `1`.

#### Partition layouts

A layout id names a **whole flash map**, and it is a flash-time immutable: no OTA can
change it. A new map is a new id, never an edit to an existing row
([design/partitions.md](../design/partitions.md) §6). Two boards in one fleet may carry
different layouts, and the server supports every id below for as long as boards carrying
it exist.

| `partition_layout` | `ota_slot_size` | `partition_table_sha256` | Who flashes it | Map |
|---|---|---|---|---|
| `ab-4m-v1` | 1966080 | `1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed` | the prebuilt agent (browser flasher) | [design/partitions.md](../design/partitions.md) §1 |
| `ab-4m-arduino-v1` | 1966080 | `05528998ae17fb6a7a5741443f9a7a4720c766f370fefc30814cbc3e391c1fc4` | the OTA library, as a sketch-local `partitions.csv` | [design/decisions/arduino-gets-its-own-layout-id.md](../design/decisions/arduino-gets-its-own-layout-id.md) |

The two share a slot size and differ in offsets. `ab-4m-v1`'s offsets cannot be reached
from the Arduino upload recipe, which writes `boot_app0` at `0xe000` and the app at
`0x10000` whatever table it flashes.

**Flash-time configuration lives in `ff_cfg`**, a 4 KB data partition with subtype `0x40`:
at `0x12000` on `ab-4m-v1`, and at `0x3D0000` on `ab-4m-arduino-v1`. It carries what the
board needs before it has ever spoken to the server: API origin, broker URI, link
credentials and the enrollment token. The flasher writes it per board. A board finds it
by subtype through the partition table, never by a hardcoded offset.

**Known networks.** A Wi-Fi board may know several networks:

```
"ssid": "home", "psk": "…", "nets": [{"ssid": "shed", "psk": "…"}, {"ssid": "bench"}]
```

- The top-level `ssid` / `psk` are the first, highest-priority network, so an old blob is a
  list of one. `nets` (optional) holds the rest, in priority order.
- At most 4 networks in all. Writers refuse more. A reader that finds more uses the first
  ones it has room for, logs a warning, and never refuses to boot over length.
- `psk` absent or `""` is an open network. Unknown keys inside an entry are ignored. A
  malformed `nets` (not an array, an entry that is not an object, a missing or empty
  `ssid`, a non-string field, an over-length `ssid` / `psk`) is a bad config: the board
  idles.
- The `ff_cfg` header `version` stays `1`. Readers refuse any other version, so a bump
  would idle every fielded board; readers that predate `nets` ignore it and join the
  top-level network.
- Selection is fixed priority: the board joins the first known network its scan sees, and
  the strongest AP of that SSID. It never leaves a working association for a
  higher-priority network; it re-selects only after losing the link. A visible network
  that does not get it an address does not block the next one. Networks the scan did not
  see are then tried directly (hidden SSIDs), and it rescans. With one network it
  connects directly, with no scan.
- With no known network in range the board says so on its console once per attempt cycle
  and keeps trying on the 1 s → 30 s backoff. It never reboots, never opens an access
  point and never falls back to anything else. With no link it cannot tell the server,
  which sees an offline board and nothing more.
- Passphrases are never logged, announced or sent. In v1 the agent never writes the list;
  only the flasher does. An agent built with Improv Wi-Fi over BLE support (`WIFI-BLE-1`)
  reads a writable NVS overlay (`ff_net`, up to 4 networks) ahead of `ff_cfg`, and opens a
  bounded 120 s Improv BLE provisioning window when all known networks fail to associate.

### `up/hb` — heartbeat

```json
{"fw_version":"1.4.2","uptime_s":81234,"rssi":-61,"free_heap":142000,"min_free_heap":98000,"boot_ok":true}
```

`min_free_heap` is the lowest free heap since this boot, in bytes
(`esp_get_minimum_free_heap_size()`), beside the current `free_heap`. A value that keeps
falling across heartbeats is the leak a board shows before it resets. It is additive under
*Evolution rules*, rule 2, and `proto` stays `1`. It is absent from agents that predate it,
and the server treats absent and `null` alike.

Interval from `dn/cfg`; defaults in [prd.md](prd.md) → *Requirements & targets → Timing*.
A sleepy device sends one per wake.

### `up/log` — firmware log lines

```json
{"dropped":0,"lines":[{"age_ms":5120,"level":"I","text":"morse: HELLO (firmware 1.0.1)"},{"age_ms":0,"level":"W","text":"sensor: no reply"}]}
```

Log lines the firmware chose to send, batched, oldest first. A device sends nothing here
unless its firmware asks the agent to: which lines go out is the device's choice, never
the server's.

- `lines`: one entry per line, oldest first, at least one.
  - `text`: one line of UTF-8, no line break, at most 200 bytes (the device truncates).
  - `level`: the ESP-IDF letter, `E`, `W`, `I`, `D` or `V`. An open vocabulary; absent
    means `I`.
  - `age_ms`: milliseconds on the device's monotonic clock from the line being logged to
    this payload being built. The server stamps the line `receipt time − age_ms` (see
    *Clock*); it is never a wall-clock time. Absent means `0`.
- `dropped`: lines the device discarded since its last `up/log` publish, because its
  buffer filled while offline or it hit its own rate limit. Absent means `0`. A QoS 0
  publish lost in flight is not counted, because the device cannot know about it.

A device keeps one payload under **2048 bytes** and publishes at most one every **5 s**
while lines are flowing. It buffers in RAM while disconnected and flushes on reconnect,
so a reboot loses lines not yet sent. QoS 0, because a log line is not worth a retransmit
or a persistent-session slot. A log line never carries a passphrase, credential, enrolment
token or signed URL.

The server treats `text` as untrusted: it strips control characters, redacts URLs and
bounds the length. It drops a payload over 8192 bytes, or one it cannot decode, whole and
without refusing the device. It keeps lines as [prd.md](prd.md) → *Retention* says. Like
every live `up/` message, an `up/log` moves `last_seen`. The fields are additive under
*Evolution rules*, rule 2, and `proto` stays `1`.

### `up/telemetry` — user-defined metrics

```json
{"dropped":0,"samples":[{"age_ms":13600,"name":"morse_loops","value":41},{"age_ms":6800,"name":"morse_loops","value":42},{"age_ms":0,"name":"battery_v","value":3.71}]}
```

Numbers the firmware chose to report, batched, oldest first. A device sends nothing here
unless its firmware records a sample (`ff_telemetry()` in ESP-IDF, `Fleetforge.metric()` on
Arduino): which metrics exist is the device's choice, never the server's.

- `samples`: one entry per sample, oldest first, at least one.
  - `name`: 1 to 31 characters of `a-z`, `0-9` and `_`, starting with a letter (a valid
    Prometheus metric name). Names starting `ff_` are reserved for the agent's own metrics.
  - `value`: a finite JSON number.
  - `age_ms`: milliseconds on the device's monotonic clock from the sample being taken to
    this payload being built. The server stamps the sample `receipt time − age_ms` (see
    *Clock*); it is never a wall-clock time, so a board needs no clock to report one. Absent
    means `0`; a negative value is read as `0`.
- `dropped`: samples the device discarded since its last `up/telemetry` publish, because its
  buffer filled while offline or samples came faster than it may publish. Absent means `0`.
  A QoS 0 publish lost in flight is not counted, because the device cannot know about it.

A device keeps one payload under **2048 bytes**. While connected it publishes at most one every
**5 s**; right after a connect it drains what it buffered in consecutive payloads. It buffers in
RAM while disconnected, keeping the newest samples, so samples taken offline arrive after the
reconnect with their original spacing, and a reboot loses samples not yet sent. QoS 0, because
a sample is not worth a retransmit or a persistent-session slot.

The server skips a sample it cannot read (a name outside the rule, a value that is not a finite
number) without losing the rest of the payload, and drops a payload over 8192 bytes, or one it
cannot decode, whole and without refusing the device. It keeps samples as [prd.md](prd.md) →
*Retention* says. Like every live `up/` message, an `up/telemetry` moves `last_seen`. The fields
are additive under *Evolution rules*, rule 2, and `proto` stays `1`.

### `dn/cmd` — commands

```json
{
  "id": "018f3a...",
  "type": "stage",
  "artifact": {
    "url": "https://fleet.example/v1/artifact/9c1f/bin?exp=...&sig=...",
    "sha256": "…", "size": 1183744, "version": "1.5.0", "sig": "…"
  },
  "apply": "auto",
  "confirm_timeout_s": 300
}
```

Types: `stage` · `apply` · `cancel` · `rollback` · `identify` · `reboot` · `set_cfg`.

- `artifact.url` is a **short-lived signed URL**. The artifact endpoint is public, so the
  signature is the authorization; no long-lived bearer token in the command.
- `apply: "auto"` — the device applies as soon as *it* judges the window safe.
  `apply: "on_command"` — the device stages and waits for an explicit `apply`.
- **Every command carries `id`, and the device must deduplicate on it.** QoS 1 is
  at-least-once; a duplicated `stage` mid-download would otherwise corrupt the transfer.
- **A retried command reuses its `id`.** A repeat of the same deploy (same device,
  same artifact) while the first `artifact.url` is still valid is re-sent with the same
  `id`, so a device that already has the transaction treats it as a duplicate rather than
  as a second deploy. Once that URL has expired the first command can no longer be acted
  on, and a repeat is a new transaction with a new `id`.
- **`artifact.sig` is optional until R6** (artifact signing). A server without a signer
  omits it, and a device that receives one it cannot verify ignores it. From R6 it is
  required on boards that announce signature verification.
- **`artifact.type` is absent in v1, and absent means `"app"`**: the device's own
  application image, written to its inactive OTA slot. `mcu_fw` (a co-processor's
  firmware), `model_weights` and `fpga_bitstream` are reserved for artifacts a device hands
  to another processor or store it manages. A device that predates the field ignores it and
  would treat any artifact as an app, so a server sends a type other than `app` only to a
  device whose `capabilities` names a handler for it. It is unrelated to the command's own
  `type`.

**`apply`, `cancel`, `rollback` and `reboot` (R5).** Each carries its own `id`, like every
command, and the device deduplicates on it. `apply` and `cancel` also carry `cmd_id`, the
`id` of the `stage` whose transaction they act on. The device refuses one that names any
other transaction, so a late `apply` never releases an image nobody chose.

```
{"id": "018f3b...", "type": "apply", "cmd_id": "018f3a..."}
{"id": "018f3c...", "type": "cancel", "cmd_id": "018f3a..."}
{"id": "018f3d...", "type": "rollback"}
{"id": "018f3e...", "type": "reboot"}
```

- `apply` releases a transaction that stopped at `staged` under `apply: "on_command"`. It
  then continues exactly as `apply: "auto"` does. An `apply` for a transaction already past
  `staged` changes nothing. One with nothing staged under that `cmd_id` is refused.
- `cancel` ends a transaction before its reboot: in `staging`, `downloading`, `verifying`,
  `staged`, or `awaiting_safe_window` before `applying`. The device stops the transfer and
  points its boot slot back at the image it is running, and the transaction ends
  `cancelled`. From `applying` on, and against a rollback the device has decided, it is
  refused.
- `rollback` returns the board to its other slot. While a transaction is `confirming` or
  waits to roll back, it ends that transaction. Otherwise the rollback is a transaction of
  its own under the command's `id`: `rolling_back`, then `rolled_back` from the image the
  board returned to. The device returns only to an image that booted and was confirmed in
  that slot, or was flashed there over USB. It never returns to one that failed
  verification, was rolled back or never booted. Without such an image, and while an update
  is between `staging` and `rebooting`, it refuses.
- `reboot` restarts the device on the image it runs. It opens no transaction. The next
  announce's `reset_reason`, `sw`, shows it happened. It is refused while a transaction is
  between `staging` and `confirmed`: once an image is staged, a reboot boots it, and a reset
  while `confirming` rolls the image back.
- **Every reboot waits for the device's safe window**: an apply, a rollback and a reboot,
  commanded or the device's own (*`up/status`*). The server requests, and it never forces.
- **A command that reboots the device is never repeated by its own redelivery.** The device
  acts on it only after it has acknowledged the command to the broker, never while that
  command is still being delivered. It also remembers, across the reboot, the `id` of the
  last command it rebooted for. Otherwise a persistent session would redeliver an
  unacknowledged `reboot` after every boot.
- The device answers each of these on `up/ack`. Their effect on a transaction appears on
  `up/status` under the transaction's `cmd_id`.
- A device lists the ones it implements in `capabilities` (*`up/announce`*). A server sends
  these commands, and `apply: "on_command"`, only to a device that lists them. An agent that
  predates them ignores the command and sends no answer, and an `on_command` stage then
  waits at `staged` until the board next restarts.

### `up/status` — the update transaction

```json
{"cmd_id":"018f3a...","state":"downloading","pct":42,"detail":null}
```

```
idle → staging → downloading → verifying → staged
     → awaiting_safe_window → applying → rebooting
     → confirming → confirmed
                  ↘ rolling_back → rolled_back
     ↘ failed
     ↘ cancelled
```

**`cmd_id` is required on every state that belongs to a transaction.** A status without
one cannot be attributed to a deploy and only proves the device is alive. Recording is
**idempotent**: a device republishing the same `(cmd_id, state)` is a no-op, which is what
makes the retained topic safe to replay on every reconnect.

**`state` is an open vocabulary.** The machine above lists the states a device reports.
A server records any value it receives, including ones it does not recognise, and shows
it as-is. It may also author states of its own outside the device's machine (for example
`requested`, written when a deploy is issued and before the device has answered).
Neither side may treat an unknown state as an error.

`awaiting_safe_window` is not decoration — **the device owns the reboot** ([design/architecture.md](../design/architecture.md)
principle 5). A vehicle in motion or an airborne drone sits here indefinitely, reporting
honestly, until it judges the moment safe. A rollback reboot obeys the same rule.
It is only for devices that *have* a window. An always-on agent staging under
`apply: "on_command"` stops at `staged` and waits for the `apply` command.

**The confirm deadline.** The image that boots into a transaction has `confirm_timeout_s`
seconds from the moment it starts to confirm. It must reach the broker, have its retained
announce acknowledged, and pass its custom self-test if it has one. The device keeps the
value from the `stage` with the transaction, uses 300 s when it is absent ([prd.md](prd.md)
→ *Timing*), and holds it within 30-3600 s. Agents that predate R5 always use 300 s.

**Custom self-test.** An image may register one self-test. The device runs it after the
announce is acknowledged and confirms only if it passes before the deadline. `confirmed`
then carries `detail` `selftest passed`. If it fails, the board rolls back (after the safe
window, below), and the image it returns to reports `rolled_back` with `detail`
`selftest failed: <reason>; returned to <slot>`. If no result came before the deadline,
that `detail` is `selftest timed out; returned to <slot>`. `<slot>` is the partition label,
for example `ota_0`. The whole `detail` is at most 63 bytes: the device shortens `<reason>`,
never the rest, and sends it as printable ASCII without `;`. An image without a custom
self-test confirms on the acknowledged announce, as before. A board that returns to an agent
that predates R5 reports `rolled_back` in that agent's words, without the reason.

**The safe window gates the rollback too.** If the deadline passes, or the self-test fails,
while the device judges the moment unsafe, the device decides to roll back and keeps that
decision, and only the reboot waits. The transaction reports `awaiting_safe_window` with
`detail` `rollback waits for a safe window`, and the board keeps running the unconfirmed
image until the moment is safe. An announce acknowledged or a self-test passed after that
decision does not confirm the image. A reset in the meantime still returns the board to the
previous image, because the bootloader never boots an unconfirmed image twice. Like the wait
before an apply, this wait has no deadline. A device without a safe-window rule is always
safe.

**`cancelled`** ends a transaction that the `cancel` command stopped before its reboot. The
board keeps the image it ran, and the staged image will not boot.

**A sleepy device stays awake through its confirm.** A deep-sleep wake is a reset, and the
bootloader rolls back an image that is still unconfirmed at a reset. A sleepy device
therefore does not sleep while a transaction is between `staging` and `rebooting`, nor while
the image it runs is unconfirmed. The first wake after an apply lasts until `confirmed` or
`rolled_back`, at most `confirm_timeout_s`. That is why `confirm_timeout_s` does not depend
on `power_class`.

### `up/ack` — command answers

```json
{"id":"018f3b...","type":"apply","result":"accepted","cmd_id":"018f3a...","detail":null}
```

The device sends one answer per `apply`, `cancel`, `rollback` or `reboot` it receives. It is
QoS 1 and never retained, because an answer is an event, not state. `id` and `type` echo the
command. `result` is one of three values:

- `accepted`: the device is acting on the command now.
- `deferred`: the device accepted it and will act at its next safe moment. A `reboot` that
  waits shows the wait only here.
- `refused`: nothing changed, and `detail` says why.

`cmd_id` names the transaction the command acts on, or is `null` when there is none.
`detail` is at most 63 bytes of printable text and never a URL. A device that sends
`up/ack` answers `refused` with `detail` `not implemented` to any other command type it does
not carry out, except `stage`, which answers on `up/status`. An answer is never a
transaction state, so the server does not record it as one. It keeps the first answer for an
`id`, and it allows for an agent that never answers.

## Enrolment happens over HTTPS, not MQTT

Flow 1 originally had the device present its enrolment token *to the broker*. That would
force the broker to authenticate clients it has never heard of, against a group-scoped
token — which Mosquitto's dynamic-security plugin does not naturally do, and which would
have meant a custom auth plugin bridging the broker to the control plane.

Instead:

```
1. Agent boots with baked config: server URL, link creds, enrolment token
2. POST https://…/v1/enroll   { token, <the announce identity payload> }
3. Server validates the token → creates the registry entry
                              → provisions a broker credential (dynsec)
                              → BURNS the token (single-use)
4. Response: { device_id, mqtt_username, mqtt_password }
5. Agent stores them in NVS and connects to the broker — already credentialed
6. Agent publishes up/announce; the device is live
```

The broker then only ever sees fully-credentialed clients, its auth is a plain credential
store the control plane writes to, and its authz is the two pattern ACLs above. The agent
already needs an HTTPS client for artifact download, so this costs nothing on the device.

**Tokens are single-use.** A group-scoped token that survived in flash would let anyone
with physical access to one board enrol arbitrary devices into the fleet — and on a
public-facing broker there is no LAN perimeter behind which to hide. Exchanging it once
for a per-device credential also makes per-device mTLS a drop-in replacement later.

## Clock — SNTP before TLS

An ESP32 has no RTC across power loss and boots at epoch zero. **TLS certificate
validation fails against a wrong clock** ("certificate not yet valid") — a classic and
confusing first-boot failure. The agent must SNTP-sync *before* its first TLS handshake,
on both channels.

Consequently device timestamps are advisory at best: **the server timestamps by its own
receipt time.** Device `ts` fields, where present, are for ordering only.

## Hierarchy (V3)

A drone behind a gateway keeps its own flat `ff/v1/d/{drone_id}/…` subtree; the gateway
republishes in both directions. The core stays unaware, exactly as with the FPGA
companion CPU.

The one place the flat namespace needs help is authz: the `%u` pattern ACL grants a
gateway access only to *its own* subtree. A gateway therefore receives explicit
additional ACL entries for each child, granted when that child enrols with a
`parent_device_id`. That is a V3 concern; nothing in v1 needs to anticipate it beyond
keeping the namespace flat and reserving the field.

## Library marker

An image built with the Fleetforge OTA library (the ESP-IDF component or the Arduino
library, and so the prebuilt agent built from it) contains one 64-byte constant, the
library marker. The server looks for it in an uploaded image. It is not in `esp_app_desc_t`
and not at a fixed offset. Every field is bytes or chars, so byte order does not matter.

| Offset | Size | Field | Value |
|---|---|---|---|
| 0 | 16 | `magic` | `14 a9 48 d1 8f 12 cf dd 46 46 4f 54 41 4c 49 42` (hex `14a948d18f12cfdd46464f54414c4942`; the last 8 bytes are ASCII `FFOTALIB`) |
| 16 | 1 | `format` | `1` |
| 17 | 3 | reserved | `0` |
| 20 | 32 | `lib_version` | the library's version: printable ASCII, NUL-terminated, non-empty |
| 52 | 12 | reserved | `0` |

**Reading it.** Scan the whole image for `magic`. Take each occurrence in order and accept
the first that has at least 64 bytes from the start of the magic to the end of the image,
`format` of at least `1`, and a `lib_version` that is a non-empty string of printable ASCII
(`0x20`-`0x7e`) ended by a NUL within its 32 bytes. Read fields beyond `lib_version` only
when `format` says they exist. No occurrence, or no valid one, means the image has no
marker; a malformed marker is no marker, never an error.

**Writing it.** The marker is defined once, in the library, and is present in an image
exactly when the library's code is linked into it; installing the library without using it
does not mark an image. It grows only additively: a new field takes reserved bytes and a
higher `format`, and existing fields never move or change type.

**What it proves.** That the library's code, including its confirm timer, is linked into
the image. Not that the firmware starts the library, not that its flash layout is right
(`partition_layout` and `partition_table_sha256` check that), and not that its own logic
works.

An image without the marker gets a warning with an explicit override at pre-check
([flows.md](flows.md) Flow 2, step 2), never a refusal.

## Evolution rules

The R0 agent cannot be changed remotely, so the server must tolerate old agents forever.

1. **Version lives in the topic** (`ff/v1/…`). A `v2` agent uses a parallel tree; the
   server speaks both for as long as v1 hardware exists.
2. **Payload changes are additive only.** Both sides ignore unknown fields. Never
   repurpose or re-type an existing field.
3. **`proto` in `announce`** lets the server adapt per device.
4. **JSON** for v1 — debuggable, and cJSON ships with ESP-IDF. Payloads stay flat and
   small so **CBOR is a drop-in** for constrained V3 links: same data model, one encoder
   swap, no topic changes.

## Scope of `dn/cfg` — a boundary, not an oversight

`dn/cfg` tunes **the agent**: heartbeat interval, presence expectations, confirm timeout,
retry backoff, development sleep-hold (`stay_awake`), and the revision pointer for rotating
credentials (`secrets_rev`). It is not a general application configuration channel — see
[prd.md](prd.md) → *Explicitly out of scope*.

```json
{"proto":1,"secrets_rev":4,"stay_awake":false}
```

- **`stay_awake` (boolean, default `false`):** When `true`, `ff_is_ready_for_sleep()` returns
  `false` so a sleepy development board stays connected on the bench for rapid OTA iteration
  instead of entering deep sleep between refreshes.
- **`secrets_rev` (integer, default `0`):** Monotonic revision of the project/device secrets
  bundle. When a board observes `secrets_rev > local_rev` (stored in NVS `ff_sec`), it holds
  `ff_is_ready_for_sleep()`, fetches `GET /v1/device-secrets` over HTTPS authenticated with
  its per-device broker credential (`Authorization: Basic base64(device_id:mqtt_password)`),
  persists the key-value bundle and `secrets_rev` in NVS `ff_sec`, and reports the new
  `secrets_rev` on `up/announce`. Secret values never cross MQTT or sit in Mosquitto's
  retained store.

## Open items for R0

- Mosquitto dynamic-security vs. an HTTP auth hook, once the credential-provisioning
  volume is known. Dynsec is the recommended start.

*Heartbeat interval, presence tolerance and confirm timeout are no longer open — they are
specified in [prd.md](prd.md) → *Requirements & targets*.*
