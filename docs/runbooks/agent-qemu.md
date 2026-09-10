# Runbook — the agent in QEMU (a board with no board)

How to boot the real `agent/dist/<target>` firmware, unmodified, against the local dev
stack: enroll over HTTP, connect to Mosquitto, announce, go present, heartbeat. No
hardware, no serial cable, no flashing. This is the harness R0-fw-1 was accepted with and
the one to reach for before blaming a board.

|  |  |
|---|---|
| Emulator | `qemu-system-xtensa` 9.2.x, shipped **inside** `espressif/idf:v5.5.5` (digest-pinned) |
| Target | `esp32` only — it is the one with an emulated NIC (`openeth`) |
| Working dir | `.qemu/` — gitignored, mode 0700, **holds live credentials** |
| Recipes | `just agent-cfg`, `just agent-qemu`, `just agent-qemu-clean` |

`.qemu/` is not a cache. `ff_cfg.bin` contains a live single-use enrollment token, and
`flash-esp32.bin` contains the NVS the emulated board wrote its **broker password** into.
Treat the directory the way you treat `.sim/` and `.env`: 0700, never committed, deleted
with `just agent-qemu-clean` when you are done.

## Why an emulator is worth a runbook

The agent's first ten seconds are the part of the product that cannot be tested by unit
tests and cannot be fixed by an OTA: eFuse MAC → `device_id`, the `ff_cfg` partition, the
clock, a single-use token that is gone once it is spent, and a credential that exists
exactly once in one HTTP response body. QEMU runs that sequence end-to-end against the
same API and the same broker a real board talks to, as often as you like, for free.

What it does **not** prove: Wi-Fi (QEMU has no radio — the emulated board uses Ethernet,
which is what the `link` field selects), power/deep-sleep behaviour, flash wear, and
anything about a specific chip revision.

## Setup

```bash
just up                                  # seven services healthy
just agent-build esp32                   # ends in: BUNDLE OK: esp32

BASE=http://localhost:8080
TOKEN=$(curl -sSi -X POST "$BASE/v1/auth/login" -H 'content-type: application/json' \
        -d '{"password":"fleetforge-dev-only"}' \
        | grep -i '^set-cookie:' | sed -E 's/.*ff_session=([^;]+).*/\1/')
FFE=$(curl -sS -X POST "$BASE/v1/enrollment-tokens" -H "Authorization: Bearer $TOKEN" \
      -H 'content-type: application/json' -d '{}' | jq -r .token)

just agent-cfg --api-base http://10.0.2.2:8080 --mqtt-uri mqtt://10.0.2.2:8883 \
      --link ethernet --hb 10 --ntp pool.ntp.org --token "$FFE"
just agent-qemu esp32                    # Ctrl-A x quits
```

`10.0.2.2` is not a typo and not this machine's LAN address: it is QEMU's slirp gateway,
the address the *guest* uses for the host running the emulator. `--network host` on the
container is what makes that host this dev box. The guest always gets `10.0.2.15`.

Traefik routes by `Host`, so the board's `Host: 10.0.2.2:8080` needs a router of its own —
`ff-qemu` in `docker-compose.override.yml`, dev-only. Without it every `POST /v1/enroll`
comes back **404 from Traefik**, having never reached the API, and it reads exactly like a
firmware bug. If you see `enroll 404` with an empty api log, that router is what is
missing.

## What a first boot looks like

Recorded from the R0-fw-1 acceptance run, trimmed to the agent's own lines:

```
I ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 02:46:05
I ff-agent: running partition: ota_0 type=0 subtype=16 offset=0x20000 size=1966080
I ff-cfg: ff_cfg v1 loaded (crc ok), 209 byte payload from 0x12000
I ff-cfg:   api_base  http://10.0.2.2:8080
I ff-cfg:   mqtt_uri  mqtt://10.0.2.2:8883
I ff-cfg:   link      ethernet
I ff-cfg:   ntp       pool.ntp.org
I ff-cfg:   hb_s      10
I ff-cfg:   secrets   token 80 chars, passphrase 0 chars (never printed)
W ff-id: eFuse MAC is all zeros — this is an emulator, never a board. …
I ff-id: device_id 000000000000
I ff-eth: openeth started (qemu -nic user,model=open_eth)
I ff-net: eth link up, ip 10.0.2.15 gw 10.0.2.2 mask 255.255.255.0
I ff-time: sntp: 1970-01-01T00:00:02Z -> 2026-09-10T02:57:37Z (via pool.ntp.org)
I ff-enroll: enroll 200 http://10.0.2.2:8080/v1/enroll
I ff-store: credential stored in NVS
I ff-mqtt: mqtt connected as 000000000000 (mqtt://10.0.2.2:8883)
I ff-mqtt: subscribe ff/v1/d/000000000000/dn/# (msg_id 10375)
I ff-mqtt: publish ff/v1/d/000000000000/up/announce (qos 1, retain, msg_id 31664)
I ff-mqtt: publish ff/v1/d/000000000000/up/presence (qos 1, retain, msg_id 58245) {"online":true}
I ff-mqtt: announce acknowledged by the broker
I ff-mqtt: publish ff/v1/d/000000000000/up/hb (qos 1, no retain, msg_id 21411, uptime 9 s)
```

**No token and no password appear anywhere in that transcript, by design.** If one ever
does, that is a bug in the firmware's logging, not a detail of the harness.

Host side, while it runs:

```bash
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/v1/devices" | jq '.devices[0]'
#   "online": true, fw_version 0.1.0, platform_type esp32,
#   partition_layout "ab-4m-v1", ota_slot_size 1966080, capabilities []
just mqtt-sub "ff/v1/d/000000000000/up/#"        # retained announce + presence replay
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/v1/enrollment-tokens" | jq '.tokens[0]'
#   status "used", used_by_device_id "000000000000"
```

`just mqtt-sub` prints nothing if you pipe it into something that dies before Python
flushes; give it a file or a terminal.

## Every emulated board is `000000000000`

QEMU's default eFuse image has a zero MAC, and `device_id` is the eFuse MAC — so every
board booted this way enrolls as `000000000000` and they would collide with each other.
The agent says so, loudly, once per boot. Consequences worth knowing:

* Two emulators at once are one device as far as the fleet is concerned.
* `000000000000` in `/v1/devices` on a real deployment means someone pointed an emulator
  at it, not that a board is broken.
* A real ESP32 always has a burned MAC; there is no code path that special-cases this.

## The flash image is the board's memory

`.qemu/flash-esp32.bin` is written once and then **written back** by QEMU (`if=mtd`), so
NVS survives a quit. That is what makes the "never enroll twice" rule testable:

```bash
just agent-qemu esp32          # second run, same image
#   I ff-store: reusing the stored credential (no enrollment): 000000000000, issued …
#   (no HTTP request at all; the token count on the server does not move)

just agent-qemu esp32 --fresh  # rebuild the image = wipe NVS = forget the credential
#   the board enrolls again, and needs a token that is still spendable
```

A re-enroll inside the server's 600 s grace window succeeds with the *same* token (that
window exists so a board that crashed after enrolling can retry). Later than that, the
already-used token is refused with a 409 and the log names *used, revoked or expired*.

## Proving the clock rule

`spec/device-protocol.md` → *Clock — SNTP before TLS* only means something because the
build sets `CONFIG_MBEDTLS_HAVE_TIME_DATE=y` (IDF leaves certificate **dates** unchecked
by default). Both directions are one command each, against a real TLS endpoint:

```bash
just agent-cfg --api-base https://bingo.tvaroska.sk --mqtt-uri mqtts://bingo.tvaroska.sk:8883 \
      --link ethernet --no-ntp --token "$FFE2"
just agent-qemu esp32 --fresh
#   W ff-time: no ntp server configured … WILL fail its certificate validity check
#   E ff-enroll: … mbedtls x509 "certificate not yet valid" — the request never happens

just agent-cfg … --ntp pool.ntp.org --token "$FFE2"
just agent-qemu esp32 --fresh
#   I esp-x509-crt-bundle: Certificate validated     (no CA baked in; the Mozilla bundle)
#   E ff-enroll: enroll 404 — a completed handshake against a server that has no /v1 yet
```

`--no-ntp`, not `--ntp ''`: `just` drops empty arguments when it splices them into a
recipe, so "no NTP" has to be a flag.

## Killing it the two different ways

| How | What the fleet sees |
|---|---|
| `Ctrl-A x` (or `docker kill`) | an ungraceful death — the broker publishes the **LWT**, retained `{"online":false}` on `up/presence`, and `/v1/devices` flips to `"online": false` within ~1.5 × keepalive (≈45 s) |
| nothing (just leave it) | heartbeats every `--hb` seconds, forever |

There is no goodbye publish: a board that is dying has no way to send one, so the agent
does not pretend it can. The will is the mechanism.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `enroll 404` and the api log shows nothing | the `ff-qemu` Traefik router is missing or the frontend is unhealthy — the 404 is Traefik's, not the API's |
| `no .qemu/ff_cfg.bin` | run `just agent-cfg …` first; the recipe refuses to boot a board with no config |
| `qemu_image: … is missing — run: just agent-build <target>` | no bundle on disk |
| `enroll 409` on a fresh image | that token is spent and outside the grace window — mint another |
| board sits at `retrying enrollment in 60 s` | read the line above it: 401/409/422 are permanent and say so, anything else retries |
| `ff_cfg: crc32 mismatch` | the blob was corrupted or truncated; regenerate it, then `--fresh` |
| clock stays 1970 | no DNS or no outbound UDP/123 from this box; every `https://`/`mqtts://` endpoint then fails validation |
| QEMU exits instantly with an efuse error | delete `.qemu/efuse.bin` and let it be regenerated from the IDF pin |

## Related

* `docs/runbooks/agent-build.md` — where `agent/dist/<target>` comes from
* `docs/runbooks/dev-stack.md` — the stack this boots against, admin login, `just sim`
* `spec/device-protocol.md` — the wire contract every line of that transcript implements
