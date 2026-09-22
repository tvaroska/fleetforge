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
| `up/telemetry` | 0 | no | user-defined metrics (R4) |
| `up/log` | 0 | no | optional diagnostics |

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

## Messages

### `up/announce` — identity

```json
{
  "proto": 1,
  "device_id": "a4cf12b3de90",
  "platform_type": "esp32c6",
  "fw_version": "1.4.2",
  "agent_version": "0.3.0",
  "link_type": "wifi",
  "power_class": "always_on",
  "expected_wake_interval_s": null,
  "parent_device_id": null,
  "partition_layout": "ab-4m-v1",
  "ota_slot_size": 1966080,
  "capabilities": ["ota", "selftest", "identify"]
}
```

`partition_layout` + `ota_slot_size` are what let the server perform the capability check
[flows.md](flows.md) promises ("reject on chip / partition-size mismatch") — without them that check
has no data source. `partition_layout` also lets the server detect and quarantine boards
flashed with a superseded layout.

### `up/hb` — heartbeat

```json
{"fw_version":"1.4.2","uptime_s":81234,"rssi":-61,"free_heap":142000,"boot_ok":true}
```

Interval from `dn/cfg`; defaults in [prd.md](prd.md) → *Requirements & targets → Timing*.
A sleepy device sends one per wake.

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
```

`awaiting_safe_window` is not decoration — **the device owns the reboot** ([design/architecture.md](../design/architecture.md)
principle 5). A vehicle in motion or an airborne drone sits here indefinitely, reporting
honestly, until it judges the moment safe. A rollback reboot obeys the same rule.

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
retry backoff. It is not an application configuration channel. Pushing user application
settings through it would quietly turn Fleetforge into a config-management product — see
[prd.md](prd.md) → *Explicitly out of scope*. Adding an application-config channel
later is additive; letting `dn/cfg` sprawl into one is not reversible.

## Open items for R0

- Whether `up/log` ships in R0 or waits for R4.
- Mosquitto dynamic-security vs. an HTTP auth hook, once the credential-provisioning
  volume is known. Dynsec is the recommended start.

*Heartbeat interval, presence tolerance and confirm timeout are no longer open — they are
specified in [prd.md](prd.md) → *Requirements & targets*.*
