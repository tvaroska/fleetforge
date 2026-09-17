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
| Recipes | `just agent-qemu-smoke`, `just agent-cfg`, `just agent-qemu`, `just agent-qemu-clean` |

**Start with `just agent-qemu-smoke`.** It answers "is the harness alive?" in about
17 seconds with no enrollment token, no running stack and no board, and it is the first
thing to run before believing any claim that the emulator is broken — see
[What we know about the boot-loop panic](#what-we-know-about-the-boot-loop-panic).

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

### For an OTA run, the download URLs must be `10.0.2.2` too (R1-be-3)

The same gateway rule applies to firmware downloads, and it bites in two places because
there are two hops. Export both **before `just up`**:

```bash
FF_PUBLIC_BASE_URL=http://10.0.2.2:8080 FF_S3_PUBLIC_ENDPOINT_URL=http://10.0.2.2:9000 just up
docker compose exec -T api env | grep -E 'PUBLIC_BASE_URL|S3_PUBLIC'   # both must say 10.0.2.2
```

* `PUBLIC_BASE_URL` (compose reads `FF_PUBLIC_BASE_URL`, default
  `http://localhost:8080`) is the origin the **device** is told to fetch from; it goes
  into the `stage` command's `artifact.url`. A guest cannot resolve the host's
  `localhost`.
* `S3_PUBLIC_ENDPOINT_URL` (compose reads `FF_S3_PUBLIC_ENDPOINT_URL`, default
  `http://localhost:${FF_MINIO_PORT:-9000}`) is the origin the API's **307 redirect**
  points at. Getting the first one right and leaving this one at `localhost` fails one hop
  later, which looks like a working deploy and a board that cannot download — the log line
  is an `esp_https_ota` connect failure against `127.0.0.1`, and nothing server-side is
  wrong. A presigned URL **signs the Host header**, so this cannot be patched up after the
  fact: it has to be right before the URL is minted.

Symptom either way: `staging → downloading` and then `failed`, with the API log
showing a 307 (or nothing at all). Neither value affects production, where both origins
are the real domain.

### Two builds, because a deploy needs a second image (R1-fw-1)

The board runs one image and stages another, so build twice and keep them apart:

```bash
just agent-build esp32 && rm -rf /tmp/ff-A && cp -r agent/dist/esp32 /tmp/ff-A  # A: the board
printf '0.3.2\n' > agent/version.txt
just agent-build esp32 && rm -rf /tmp/ff-B && cp -r agent/dist/esp32 /tmp/ff-B  # B: the artifact
printf '0.3.1\n' > agent/version.txt                            # restore; commit 0.3.1
cp -r /tmp/ff-A/. agent/dist/esp32/                             # `--fresh` flashes dist/
```

Upload B (`POST /v1/artifact?target=esp32&version=0.3.2`, 201), boot A, then
`POST /v1/devices/000000000000/deploy -d '{"version":"0.3.2"}'` → **202**. A **409** there
means the announce still carries `capabilities: []`. Watch the transaction with
`PYTHONUNBUFFERED=1 just mqtt-sub 'ff/v1/d/+/up/status' | tee /tmp/status.log` — without
`PYTHONUNBUFFERED` Python block-buffers into the pipe and the file stays empty for
minutes.

Hand-built commands (a corrupted `sha256`, a duplicate `id`) must be published as the
**commander**, not as the dynsec admin: `mosquitto/acl` grants `publishClientSend
ff/v1/d/+/dn/#` to that role alone, and a denied publish is silent —
`just mqtt-pub 'ff/v1/d/000000000000/dn/cmd' "$(cat cmd.json)" 0 "$MQTT_COMMAND_USERNAME"
"$MQTT_COMMAND_PASSWORD"`.

### The emulator cannot survive `esp_restart()`

**Everything up to the reboot works; the reboot itself does not.** After the agent applies
an update and calls `esp_restart()`, the next boot panics before `app_main`:

```
rst:0xc (SW_CPU_RESET) … I spi_flash: flash io: dio
Guru Meditation Error: Core 0 panic'ed (InstrFetchProhibited).  PC : 0x00000000
  _xt_lowint1 ← vPortExitCritical ← esp_intr_alloc_intrstatus_bind
  ← esp_timer_impl_init (esp_timer_impl_lac.c:263) ← do_system_init_fn ← call_start_cpu0
```

An interrupt is already pending when `esp_timer` installs its LAC handler, so the first
`rsil` after `esp_intr_alloc` dispatches it to a handler slot that is still zero. This is
the machine, not the image: the **rolled-back `0.3.0`** — the same bytes that booted
cleanly from power-on minutes earlier — panics at the identical PC, and killing QEMU and
starting it again (a POWERON reset) boots either slot fine. Same family as the panic
recorded below: a peripheral that QEMU does not reset with the CPU.

Workaround for an OTA acceptance run, which is what proves the *applied* image boots — ask
for `apply: "on_command"`, so the agent stages the slot and **parks** instead of rebooting
itself (`ff_ota.c`: "apply=on_command — staged and waiting"), then power-cycle it yourself:

```bash
curl -sS -X POST "$BASE/v1/devices/000000000000/deploy" -H "Authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' -d '{"version":"0.3.2","apply":"on_command"}'
until grep -q 'is staged and bootable' /tmp/qemu.log; do sleep 0.5; done
just agent-qemu-stop esp32     # SIGKILL == pulling the power
just agent-qemu esp32          # cold start: the bootloader takes the NEW slot
```

`esp_ota_set_boot_partition()` has already marked the slot `NEW`, so the cold boot runs it
as `PENDING_VERIFY` exactly as the self-reboot would have, and the agent confirms it:
`this image was written by OTA and is now CONFIRMED`. Do **not** let it panic first — one
panic in `PENDING_VERIFY` is what the bootloader's rollback is for, and it will (correctly)
put the old slot back.

**Racing `apply: "auto"` does not work** (R1-fw-2): `staged and bootable` and
`esp_restart()` are **40 ms** apart in the log, so a `sleep 0.2` poll loses every time. The
board then soft-resets into the new slot, panics as above, and the bootloader retires the
`PENDING_VERIFY` image — you end up back on the old slot with `otadata` marked `aborted`
and nothing wrong with the image you were trying to prove. `apply: "on_command"` removes
the race instead of trying to win it.

### Reading the version back (R1-fw-2)

Every boot prints which image is executing and what the bootloader thinks of it:

```
I ff-agent: running partition: ota_1 type=0 subtype=17 offset=0x200000 size=1966080
I ff-agent: running image: fw_version 0.3.2, ota state pending_verify — this is what
            up/announce and up/hb report
```

`ota state` is the whole diagnostic: `pending_verify` on the first boot of an OTA'd slot,
`valid` once `ff_mqtt.c` confirms it, `none (serially flashed)` on a board that has never
been written by OTA, and `aborted` on the slot the bootloader has just given up on. Assert
the server agrees — the field an operator actually reads:

```bash
dev() { curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/v1/devices" \
        | jq -r '.devices[] | select(.device_id=="000000000000") | .fw_version'; }
dev                                    # => 0.3.2 after an applied update
```

After a **failed** apply (a hand-published `stage` with a corrupted digest) the same three
readings must all still say the old version — board log, `up/hb`, and `dev`. That is the
half worth running first, because it is the one nobody checks.

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

`agent-qemu` now refuses to start a second board rather than trusting you to remember:
the container is named `ff-qemu-<target>`, and a second `just agent-qemu esp32` exits 1
telling you to stop the first. **Two boards do not produce two sets of results — they
produce one unreadable set**, because both enroll, heartbeat and report stages as the
same `device_id`, and nothing on the server can tell them apart. S0-fw-1 threw away a
full round of acceptance evidence to this.

> **The trap that let it happen, worth knowing on its own:**
> `docker ps --filter ancestor=espressif/idf:v5.5.5` **matches nothing here.** `idf_image`
> is pinned by digest, and the `ancestor` filter compares the reference you typed, not the
> image the container actually runs. The command exits 0 having killed nothing, which
> reads exactly like "no emulators are running". Use `just agent-qemu-stop`, or match on
> the image column:
> ```bash
> docker ps --format '{{.ID}} {{.Image}}' | awk '$2=="espressif/idf:v5.5.5"{print $1}'
> ```
> Killing the `just` process does not help either: without `-it`, `docker run` leaves the
> container — and the emulator inside it — running.

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

**`--fresh` is not how a board is made to re-enrol any more** (S0-fw-4). It throws the
whole image away, credential *and* the cached RF calibration in IDF's `phy` namespace —
which is the very thing the flasher used to do on every flash and no longer does. What
clears the credential now is a **new enrollment token**: `ff_store_sync_token()` compares a
fingerprint of the `ff_cfg` token against the one stored beside the credential and erases
the `ff` namespace, and only that namespace, when they differ. So `--fresh` is the "brand
new board" button, and the section below is the "re-flashed board" one.

## Re-flash ff_cfg without erasing NVS

`just agent-qemu-recfg <target>` writes `.qemu/ff_cfg.bin` into an **existing** flash image
at the offset the bundle manifest declares, leaving NVS alone. It is the emulator's
equivalent of re-flashing a board's config in the field, and it is the only way to exercise
the S0-fw-4 path: a board holding a live credential, handed a token it has not seen.

```bash
just agent-qemu-stop esp32        # QEMU writes the image back on exit; it must not be running
FFE2=$(curl -sS -X POST "$BASE/v1/enrollment-tokens" -H "Authorization: Bearer $TOKEN" \
       -H 'content-type: application/json' -d '{}' | jq -r .token)
just agent-cfg --api-base http://10.0.2.2:8080 --mqtt-uri mqtt://10.0.2.2:8883 \
      --link ethernet --hb 10 --token "$FFE2"
just agent-qemu-recfg esp32       # wrote ff_cfg at 0x12000 — NVS untouched
just agent-qemu esp32
#   W ff-store: the ff_cfg enrollment token has changed (7e8c7d9ba055eb70 -> 264ea1ed45b0b379):
#               erasing the stored credential so this board re-enrolls. ONLY the 'ff'
#               namespace is erased — the cached RF calibration lives in the 'phy' namespace …
#   I ff-enroll: enroll 200 http://10.0.2.2:8080/v1/enroll
#   I ff-store: credential stored in NVS
```

Boot it a second time with the *same* config and the fingerprint matches: `reusing the
stored credential (no enrollment)`, no HTTP request, no token spent.

**To prove the calibration survives**, seed a `phy` namespace before the first boot — QEMU
has no radio, so the board never writes one itself — and read it back afterwards. Use IDF's
own tools; do **not** `grep`/`strings` the region, because NVS marks entries erased in a
state bitmap and leaves the key bytes in flash until compaction, so a hit proves nothing.

```bash
printf 'key,type,encoding,value\nphy,namespace,,\ncal_data,data,string,ff-s0-fw-4-canary\n' \
    > .qemu/phy.csv
# Build the image WITHOUT booting, then generate a 24 KB nvs image carrying the canary.
docker run --rm -u $(id -u):$(id -g) -v "$PWD/.qemu:/q" -v "$PWD/agent/tools:/t:ro" \
  -v "$PWD/agent/dist/esp32:/d:ro" --entrypoint bash <the pinned idf_image> -c '
    . $IDF_PATH/export.sh >/dev/null 2>&1
    python3 /t/qemu_image.py flash --bundle /d --config /q/ff_cfg.bin --out /q/flash-esp32.bin
    python3 $IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py \
        generate /q/phy.csv /q/phy.bin 0x6000'
# nvs is at 0x9000, size 0x6000 -> 6 sectors of 4096 from sector 9. Both numbers are
# `ab-4m-v1` values, not universal ones: check them against `just agent-verify esp32`.
dd if=.qemu/phy.bin of=.qemu/flash-esp32.bin bs=4096 seek=9 count=6 conv=notrunc status=none

# … boot, re-cfg with a new token, boot again … then read the region back:
dd if=.qemu/flash-esp32.bin of=.qemu/nvs.bin bs=4096 skip=9 count=6 status=none
docker run --rm -u $(id -u):$(id -g) -v "$PWD/.qemu:/q" --entrypoint bash <the pinned idf_image> -c \
  '. $IDF_PATH/export.sh >/dev/null 2>&1 \
   && python3 $IDF_PATH/components/nvs_flash/nvs_partition_tool/nvs_tool.py /q/nvs.bin -d written'
```

The dump must still list namespace `phy` with `cal_data = ff-s0-fw-4-canary`, alongside the
`ff` namespace's *new* `tok_fp` and credential. That is S0-fw-4 demonstrated: the board
erased its own credential and kept its calibration.

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

There is a third direction, added by S0-fw-2, and it is the one that proves stage reports
survive the rule above. Against `https://` **with** `--ntp`, `link_up` is in
`device_progress` and ordered **before** `time_synced` — the agent holds a pre-clock
report and flushes it, oldest first, on the first report made after the sync, so its `at`
is a couple of seconds late but its order is exact:

```bash
just agent-cfg --api-base https://bingo.tvaroska.sk --mqtt-uri mqtts://bingo.tvaroska.sk:8883 \
      --link ethernet --hb 30 --ntp pool.ntp.org --token "$FFE"
just agent-qemu esp32 --fresh          # let it reach `mqtt connected`
ssh prod "cd /opt/boris/prod && docker compose exec -T postgres psql -U fleetforge fleetforge \
  -At -c \"SELECT at, stage, detail FROM device_progress WHERE device_id='000000000000' \
  AND at > now() - interval '15 minutes' ORDER BY at, id;\""
#   … link_up|ethernet      <- first, held over the sync
#   … time_synced|
```

`ORDER BY at, id` matters: a held stage and the report that flushed it can land in the
same clock tick, and `id` is what keeps them in the order the board produced them. Over
`http://` nothing is ever held — `link_up` goes out at link time even with `--no-ntp`.

## Killing it the two different ways

| How | What the fleet sees |
|---|---|
| `Ctrl-A x`, or `just agent-qemu-stop esp32` | an ungraceful death — the broker publishes the **LWT**, retained `{"online":false}` on `up/presence`, and `/v1/devices` flips to `"online": false` within ~1.5 × keepalive (measured ≈18 s at `--hb 10`, S0-fw-1) |
| nothing (just leave it) | heartbeats every `--hb` seconds, forever |

There is no goodbye publish: a board that is dying has no way to send one, so the agent
does not pretend it can. The will is the mechanism.

## Reproducing a board that gets partway

The three failures worth being able to summon on demand. All are S0-fw-1's acceptance
evidence, and all of them are what an operator is actually looking at when they say a
board "never showed up". Watch `GET /v1/devices` → `arrivals`, not the serial log.

**Stalled at `enrolling` — the server is up but cannot provision a broker credential.**

```bash
docker compose stop mosquitto
just agent-cfg --api-base http://10.0.2.2:8080 --mqtt-uri mqtt://10.0.2.2:8883 \
      --link ethernet --hb 10 --ntp pool.ntp.org --token "$FFE"
just agent-qemu esp32 --fresh
#   api:  POST /v1/enroll -> 503, "broker provisioning failed … token is burned"
#   fleet: ARRIVING stage=enrolling
```

The agent retries at 60 s, then 120, 240, … (`ENROLL_RETRY_MIN/MAX_MS`), reporting
`enrolling` each time, and **spends no second token** — the 600 s grace window lets the
same board re-present the same one. `docker compose start mosquitto` and it recovers to
`enrolled` → `mqtt_connected` → `online` on its own, with no reflash.

Note the interaction, because it makes the flag flicker: `progress_stall_s` is 60 s and
the *first* retry interval is also 60 s, so the row alternates `stalled=false/true` for
the first couple of minutes and only settles once the backoff has doubled past 60 s.

**Stalled at `mqtt_refused` — enrolled, but the broker will not have it.** Rotate the
credential out from under a board that has one in NVS, then reboot it *without*
`--fresh`:

```bash
set -a; source .env; set +a
docker compose exec -T mosquitto mosquitto_ctrl -h localhost -p 1883 \
      -u "$MQTT_DYNSEC_USERNAME" -P "$MQTT_DYNSEC_PASSWORD" \
      dynsec setClientPassword 000000000000 something-else
just agent-qemu esp32
#   board: broker refused the connection (return code 5)
#   fleet: ARRIVING stage=mqtt_refused detail='broker connack 5'
```

**Invisible — the enrollment token itself is refused.** Mint a token, revoke it, flash
it. This one is a *negative* result and the reason the serial console exists:

```bash
curl -sS -X POST "$BASE/v1/enrollment-tokens/$TID/revoke" -H "Authorization: Bearer $TOKEN"
just agent-qemu esp32 --fresh
#   W ff-progress: the server refused this board's enrollment token (progress 401);
#                  stage reporting is off for this boot…
#   E ff-agent: halted: this board's enrollment token was refused for good…
#   fleet: nothing. zero rows in device_progress.
```

The 401 disables the reporter for the boot by design (`ff_progress.c`), so `halted` never
leaves the board. A board with a bad credential is invisible to the dashboard and visible
only on the console — see `docs/features/enrollment.md` → *Serial console after flashing*.

## Is the harness alive?

```bash
just agent-qemu-smoke                # esp32, ~17 s
just agent-qemu-smoke esp32 180      # a slower box: raise the deadline
#   QEMU emulator version 9.2.2 (esp_develop_9.2.2_20260417)
#   I (3688) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built …
#   I (4808) ff-cfg: ff_cfg v1 loaded (crc ok), 83 byte payload from 0x12000
#   HARNESS OK: esp32 boots, reads its config, and does not loop.
```

It boots **the same machine `agent-qemu` does** — both recipes splice the single
`qemu_program` definition in the justfile, so a green smoke run says something about the
recipe it guards rather than about a lookalike. It asserts only what the first seconds
can honestly prove:

1. no panic (`Guru Meditation`, `LoadProhibited`, `abort()`),
2. exactly one ROM `rst:0x` banner — **the boot-loop check**,
3. the `ff-agent` banner, so `app_main` ran,
4. `ff_cfg v1 loaded`, so it read its config partition.

It deliberately proves **nothing** about enrolment or MQTT: those need a live token and
`just up`, and they belong to `agent-qemu` and the transcript above. It uses a tokenless
throwaway config aimed at a closed port and its own `flash-<target>-smoke.bin`, so it
spends no token and never disturbs the NVS your real emulated board is accumulating.

## What we know about the boot-loop panic

**S0-infra-1 filed this harness as dead** (2026-09-10): `just agent-qemu esp32`
boot-looping on a `LoadProhibited` panic decoded as
`main_task → esp_task_wdt_init → esp_task_wdt_impl_timer_allocate → esp_intr_alloc →
task_wdt_isr` — the task watchdog's own interrupt firing inside the allocation that
installs it. The suspect on the ticket was the
`-global driver=timer.esp32.timg,property=wdt_disable,value=true` flag.

**It did not reproduce, and the suspect is innocent** (investigated 2026-09-11). What
was tried, all green:

* a full run from a wiped flash image and a fresh token — `enroll 200` → `credential
  stored in NVS` → `mqtt connected` → retained announce/presence → `hb` for 100 s,
  matching the transcript above line for line;
* five further boots, three of them under eight busy-loops on a four-core box, testing
  the theory that host starvation fires a spurious watchdog interrupt. Zero panics;
* every one of those runs carries the `wdt_disable` flag, so the flag is not the cause.

Two real defects turned up while proving that, and the first explains how a working
harness became an unrunnable one.

**1. The recipe could not run without a terminal.** `agent-qemu` passed `docker run -it`
unconditionally:

```
$ just agent-qemu esp32
cannot attach stdin to a TTY-enabled container because stdin is not a terminal
```

Every agent session, script and CI shell is non-interactive, so the recipe could not be
run by the things that most need to run it — and the way round it is to hand-roll a
`docker run`, which is exactly where an emulator invocation acquires a wrong `-M`, `-m`
or `-global` and starts panicking in the watchdog. That is the most probable origin of
the filed backtrace. Fixed: `-it` is now passed only when stdin is a TTY.

**2. The emulator was pinned but never verified.** `idf_image` is pinned by sha256 and
each bundle manifest records the same digest, which reads as a guarantee that the
emulator is fixed. It is not — **Docker verifies a digest on `pull`, not on `run`**, so a
locally damaged or replaced layer is used in silence, and `qemu-system-xtensa` lives in
that image. The pinned image was in fact **absent from this box's Docker store** when
the investigation started and had to be re-pulled (2.4 GB), on a disk sitting at 85%.

Every other input to a boot is content-addressed and deterministic: the bundle (per-part
sha256 in `manifest.json`), the eFuse blob (IDF's own `default_efuse` bytes), the flash
merge (`esptool merge_bin` over manifest offsets). Identical inputs cannot produce two
different behaviours — so at failure time one input was not what it claimed, and the
local emulator image is the only one verifiably in a different state since. Unprovable
after the fact. Closed going forward: `qemu_sha256` in the justfile pins the hash of the
emulator **binary**, checked inside the container before every boot, and its version is
printed into every transcript.

The honest limit: hashing that one binary is not a full integrity check of the image
(shared libraries and the Python tooling are not covered). It covers the file whose
behaviour decides whether a boot means anything, which is the part that was in doubt.

### If it boot-loops again

```bash
just agent-qemu-smoke                # 17 s: names a loop, a panic, or a bad emulator
docker rmi  $IDF_IMAGE && docker pull $IDF_IMAGE   # the digest from justfile `idf_image`
just agent-qemu-clean                # .qemu/ holds live credentials — this deletes them
just agent-cfg … --token "$FFE"      # a fresh token; the old one is spent
just agent-qemu esp32 --fresh
```

Do that **before** decoding a backtrace. If the smoke check is green and only your own
run loops, the difference is in `.qemu/` or your config, not in the emulator. If the
smoke check is red, it already told you which of the four assertions failed.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `cannot attach stdin to a TTY-enabled container` | an old `agent-qemu` that passes `-it` unconditionally; the recipe now only does so when stdin is a terminal |
| `the emulator is not the one this repo pins` | the local copy of the ESP-IDF image is damaged or replaced. Re-pull it by digest; Docker does not re-verify on run |
| `SMOKE FAILED: the board reset N times` | a genuine boot loop — a corrupt `app.bin` does exactly this. `just agent-verify <target>` re-hashes the bundle against its manifest |
| `enroll 404` and the api log shows nothing | the `ff-qemu` Traefik router is missing or the frontend is unhealthy — the 404 is Traefik's, not the API's |
| `no .qemu/ff_cfg.bin` | run `just agent-cfg …` first; the recipe refuses to boot a board with no config |
| `qemu_image: … is missing — run: just agent-build <target>` | no bundle on disk |
| `enroll 409` on a fresh image | that token is spent and outside the grace window — mint another |
| board sits at `retrying enrollment in 60 s` | read the line above it: 401/409/422 are permanent and say so, anything else retries |
| `ff_cfg: crc32 mismatch` | the blob was corrupted or truncated; regenerate it, then `--fresh` |
| no `link_up` row against an `https://` base, but the later stages are there | the firmware predates S0-fw-2: the report went out at epoch 0 and its TLS handshake failed certificate validity. Re-build the bundle (`just agent-build esp32`) |
| clock stays 1970 | no DNS or no outbound UDP/123 from this box; every `https://`/`mqtts://` endpoint then fails validation |
| QEMU exits instantly with an efuse error | delete `.qemu/efuse.bin` and let it be regenerated from the IDF pin |

## Related

* `docs/runbooks/agent-build.md` — where `agent/dist/<target>` comes from
* `docs/runbooks/dev-stack.md` — the stack this boots against, admin login, `just sim`
* `spec/device-protocol.md` — the wire contract every line of that transcript implements
