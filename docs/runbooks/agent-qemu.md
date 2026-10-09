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
| Recipes | `just agent-qemu-smoke`, `just agent-cfg`, `just agent-qemu`, `just agent-qemu-otadata`, `just agent-qemu-flaky`, `just agent-qemu-clean` |

**Start with `just agent-qemu-smoke`**. It answers "is the harness alive?" in about
17 seconds with no enrollment token, no running stack and no board. It is the first
thing to run before believing any claim that the emulator is broken — see
[What we know about the boot-loop panic](#what-we-know-about-the-boot-loop-panic).

`.qemu/` is not a cache. `ff_cfg.bin` contains a live single-use enrollment token. `flash-esp32.bin` contains the NVS the emulated board wrote its **broker password** into.
Treat the directory the way you treat `.sim/` and `.env`: 0700, never committed, deleted
with `just agent-qemu-clean` when you are done.

## Why an emulator is worth a runbook

The agent's first ten seconds are the part of the product that cannot be tested by unit
tests and cannot recover through an OTA: eFuse MAC → `device_id`, the `ff_cfg` partition, the
clock, a single-use token that is gone once it is spent. A credential that exists
exactly once in one HTTP response body. QEMU runs that sequence end-to-end against the
same API and the same broker a real board talks to, as often as you like, for free.

What it does **not** prove: Wi-Fi (QEMU has no radio — the emulated board uses Ethernet,
which is what the `link` field selects), power/deep-sleep behavior, flash wear, and
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

`10.0.2.2` is not a typo and not this machine's LAN address. It is QEMU's slirp gateway,
the address the *guest* uses for the host running the emulator. `--network host` on the
container is what makes that host this dev box. The guest always gets `10.0.2.15`.

Traefik routes by `Host`, so the board's `Host: 10.0.2.2:8080` needs a router of its own —
`ff-qemu` in `docker-compose.override.yml`, dev-only. Without it every `POST /v1/enroll`
comes back **404 from Traefik**, having never reached the API. It reads exactly like a
firmware bug. If you see `enroll 404` with an empty api log, that router is what is
missing.

### For an OTA run, the download URLs must be `10.0.2.2` too (R1-be-3)

The same gateway rule applies to firmware downloads, and it bites in two places. This is because there are two hops. Export both **before `just up`**:

```bash
FF_PUBLIC_BASE_URL=http://10.0.2.2:8080 FF_S3_PUBLIC_ENDPOINT_URL=http://10.0.2.2:9000 just up
docker compose exec -T api env | grep -E 'PUBLIC_BASE_URL|S3_PUBLIC'   # both must say 10.0.2.2
```

* `PUBLIC_BASE_URL` (compose reads `FF_PUBLIC_BASE_URL`, default
  `http://localhost:8080`) is the origin the **device** is told to get from. It goes
  into the `stage` command's `artifact.url`. A guest cannot resolve the host's
  `localhost`.
* `S3_PUBLIC_ENDPOINT_URL` (compose reads `FF_S3_PUBLIC_ENDPOINT_URL`, default
  `http://localhost:${FF_MINIO_PORT:-9000}`) is the origin the API's **307 redirect**
  points at. Getting the first one right and leaving this one at `localhost` fails one hop
  later, which looks like a working deploy and a board that cannot download. The log line is an `esp_https_ota` connect failure against `127.0.0.1`, and nothing server-side is
  wrong. A presigned URL **signs the Host header**, so this cannot be patched up after the
  fact. It has to be right before the URL mints.

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
ff/v1/d/+/dn/#` to that role alone. A denied publish is silent —
`just mqtt-pub 'ff/v1/d/000000000000/dn/cmd' "$(cat cmd.json)" 0 "$MQTT_COMMAND_USERNAME"
"$MQTT_COMMAND_PASSWORD"`.

### The emulator cannot survive `esp_restart()`

**Everything up to the reboot works. The reboot itself does not**. After the agent applies
an update and calls `esp_restart()`, the next boot panics before `app_main`:

```
rst:0xc (SW_CPU_RESET) … I spi_flash: flash io: dio
Guru Meditation Error: Core 0 panic'ed (InstrFetchProhibited).  PC : 0x00000000
  _xt_lowint1 ← vPortExitCritical ← esp_intr_alloc_intrstatus_bind
  ← esp_timer_impl_init (esp_timer_impl_lac.c:263) ← do_system_init_fn ← call_start_cpu0
```

An interrupt is already pending when `esp_timer` installs its LAC handler. Thus, the first
`rsil` after `esp_intr_alloc` dispatches it to a handler slot that is still zero. This is
the machine, not the image: the **rolled-back `0.3.0`** (the same bytes that booted
cleanly from power-on minutes earlier) panics at the identical PC, and killing QEMU and
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

`esp_ota_set_boot_partition()` marked the slot `NEW`, so the cold boot runs it
as `PENDING_VERIFY` exactly as the self-reboot would have. The agent confirms it:
`this image was written by OTA and is now CONFIRMED`. Do **not** let it panic first — one
panic in `PENDING_VERIFY` is what the bootloader's rollback is for. It will (correctly)
put the old slot back.

**Racing `apply: "auto"` does not work** (R1-fw-2): `staged and bootable` and
`esp_restart()` are **40 ms** apart in the log. Thus, a `sleep 0.2` poll loses every time. The
board then soft-resets into the new slot, panics as above, and the bootloader retires the
`PENDING_VERIFY` image — you end up back on the old slot with `otadata` marked `aborted`
and nothing wrong with the image you were trying to prove. `apply: "on_command"` deletes
the race instead of trying to win it.

### Read the version back (R1-fw-2)

Every boot prints which image is executing and what the bootloader thinks of it:

```
I ff-agent: running partition: ota_1 type=0 subtype=17 offset=0x200000 size=1966080
I ff-agent: running image: fw_version 0.3.2, ota state pending_verify — this is what
            up/announce and up/hb report
```

`ota state` is the whole diagnostic: `pending_verify` on the first boot of an OTA'd slot,
`valid` once `ff_mqtt.c` confirms it, `none (serially flashed)` on a board that was never written by OTA, and `aborted` on the slot the bootloader has just given up on. Assert
the server agrees — the field an operator actually reads:

```bash
dev() { curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/v1/devices" \
        | jq -r '.devices[] | select(.device_id=="000000000000") | .fw_version'; }
dev                                    # => 0.3.2 after an applied update
```

After a **failed** apply (a hand-published `stage` with a corrupted digest) the same three
readings must all still say the old version — board log, `up/hb`, and `dev`. That is the
half worth running first. This is because it is the one nobody checks.

Since agent 0.4.1 (R2-fw-1) the digest is checked before the boot pointer moves. The
mismatch line ends `the boot partition was never moved`, and `boot partition put back`
must **not** appear. Older agents logged that second line. To check that otadata was not
touched, read it with the board stopped (`just agent-qemu-stop esp32`) before and after
the corrupt stage. The two values must be identical:

```bash
otadata() { dd if=.qemu/flash-esp32.bin bs=4096 skip=15 count=2 status=none | sha256sum | cut -c1-16; }
```

A `--fresh` board is not all-0xFF. Its first boot marks ota_0 valid, so expect
`8ba3b110139f4544` both times, not the all-0xFF hash.

Once otadata holds two entries, a hash says only *that* something changed. Decode each
sector instead (QEMU stopped). Since R2-test-1 the recipe is `just agent-qemu-otadata esp32`,
which prints the same lines from the bundle manifest's offset. The shell function below
stays, because older transcripts quote it. otadata is at 0xF000 (sector 0) and 0x10000 (sector 1), one
32-byte `{seq, label[20], state, crc}` entry each:

```bash
otadecode() { python3 - "${1:-.qemu/flash-esp32.bin}" <<'EOF'
import struct, sys, zlib
S = {0:'NEW',1:'PENDING_VERIFY',2:'VALID',3:'INVALID',4:'ABORTED',0xFFFFFFFF:'UNDEFINED'}
f = open(sys.argv[1], 'rb')
for i, off in enumerate((0xF000, 0x10000)):
    f.seek(off); seq, _label, state, crc = struct.unpack('<I20sII', f.read(32))
    if seq == 0xFFFFFFFF: print(f'sector{i}: empty'); continue
    ok = zlib.crc32(struct.pack('<I', seq), 0xFFFFFFFF) == crc
    print(f'sector{i}: seq={seq} -> ota_{(seq-1)%2} state={S.get(state, hex(state))} crc={"ok" if ok else "BAD"}')
EOF
}
```

A fresh board decodes as `sector0: seq=1 -> ota_0 state=VALID crc=ok` and
`sector1: empty`. If that first entry says `crc=BAD`, the decoder is wrong, not the board.
**Two sectors with the same seq is the R2-fw-2 brick.** Both entries then name one slot,
and a rollback has nowhere to go.

Since agent 0.4.2 (R2-fw-2) a stage is refused before anything is fetched or erased in
two cases. On the topic each one is exactly `staging, failed "<detail>"`:

| Board log (ERROR) | `detail` |
|---|---|
| `update <id>: refused — ota_0 is still pending_verify (it confirms at its announce ack or rolls back); nothing was fetched or erased` | `the running image is not confirmed yet` |
| `update <id>: refused — the boot partition names ota_1 (ota state new) while ota_0 is running; nothing was fetched or erased; the staged image boots at the next reset` | `an update is already staged and waits for a reboot` |

The second case is every stage that follows an `apply: "on_command"` stage, until the
board is power-cycled. After a failed `finish()` (for example `image validation failed`),
0.4.2 logs `boot partition still names ota_0; nothing to undo — otadata was not touched`.
`boot partition put back` must **not** appear, and otadata must be byte-identical.

### Driving an outcome: `confirmed` and `rolled_back` (R2-be-1)

Since agent 0.4.0 the board records the transaction at `staged`
(`ff-txn: transaction <cmd_id> recorded`). It reports the outcome after the reboot, and
each boot names its decision in one line: `ff-mqtt: transaction <cmd_id>: confirming on
ota_1` or `… rolled_back (returned to ota_1; ota_0 did not confirm)`. Every image in the
run must contain this code: board **A**, the confirm artifact **B**, and the rollback
artifact **R**. R is built with `--build-arg FF_ROLLBACK_TEST=1` to a scratch dir per
[rollback-test.md](rollback-test.md). Upload all three as `target=esp32` artifacts, boot A
with `--fresh`, and watch `up/status`.

* **`confirmed`.** Deploy B with `apply: "on_command"`. Wait for `is staged and bootable`
  in the log **and** `staged` on the topic, then `sleep 1` and power-cycle (stop, start).
  Expect the board to log `ota state pending_verify`, then `confirming on ota_1`, then
  `CONFIRMED`, then `ff-txn: … closed — record cleared`. Expect the rows to end
  `staged, confirming, confirmed` (terminal). Power-cycle once more: there is no
  transaction line, and no new rows.
* **`rolled_back`, by the bootloader.** Deploy A back with the default `apply: "auto"`.
  After `rebooting` the emulator soft-resets into the new slot and hits the
  `esp_timer_impl_init` panic described above while in `PENDING_VERIFY`, so the bootloader
  aborts it. Wait ~10 s, then stop and start. The board on the old slot reports
  `rolled_back`, with a `detail` naming the slots.
* **`rolled_back`, by the confirm timer** (CRITICAL path). Deploy R with
  `apply: "on_command"` and power-cycle after `staged`. R logs `confirming`, ignores its
  announce ack, and at 60 s logs `no working session … rolling back`. `rolling_back`
  reaches the topic **before** the reset (a 2 s grace timer bounds it). Then come the panic
  loop and a stop/start, and the old slot reports `rolled_back`. Expect rows `… confirming,
  rolling_back, rolled_back`. If the board is still on `-rbtest` after ~2 min, that is a P0.

This box runs the stack on **8088**. The dev stack may also have been started from
`docker-compose.yml` plus a local override rather than the dev override (check
`docker inspect fleetforge-frontend --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'`).
In that case the `ff-qemu` Traefik router is missing and the board's enroll gets a
**404**. Recreate `api` and `frontend` with the same `-f` files plus a throwaway override
that adds only the `ff-qemu` labels, and set `FF_PUBLIC_BASE_URL=http://10.0.2.2:8088
FF_S3_PUBLIC_ENDPOINT_URL=http://10.0.2.2:9000` on the command. Put both back afterwards
with the original `-f` set.


### Driving a failure: boot loop, power cut, torn otadata, hang (R2-test-1)

Four more outcomes, each from a **real** OTA'd image or a state a power cut really
produces. Never forge partition state by hand (`rollback-test.md` → *Why it needs a special
build*). The fault images come from `FF_FAULT_TEST` (`agent/CMakeLists.txt`), built to a
scratch dir exactly like the rbtest image, with `agent/version.txt` set to an unused
version for the build and checked out again afterwards:

```bash
DOCKER_BUILDKIT=1 docker build --target export --output type=local,dest=/tmp/ff-bootloop-esp32 \
  --build-arg IDF_IMAGE="$(just --evaluate idf_image)" --build-arg IDF_TARGET=esp32 \
  --build-arg SOURCE_COMMIT=$(git rev-parse HEAD) --build-arg FF_FAULT_TEST=bootloop agent
python3 agent/tools/verify_bundle.py /tmp/ff-bootloop-esp32     # agent <ver>-bltest
```

`hang` gives `-hangtest`. Any other value, or `FF_FAULT_TEST` together with
`FF_ROLLBACK_TEST=1`, fails the build. `config_sha256` equals the normal bundle's. Both
fault apps are about 150 KB, because everything after the hook is dead code. Upload as
artifacts only: `just agent-publish` refuses the suffixes.

**Power cut = `just agent-qemu-stop esp32`** (SIGKILL). Flash writes done before the kill
persist in `.qemu/flash-esp32.bin`. **The limit:** QEMU completes each SPI flash command
atomically, so a page program torn mid-command is never produced. That is why a torn
otadata sector is written offline:

```bash
just agent-qemu-otadata esp32                                     # decode (QEMU stopped)
just agent-qemu-otadata esp32 tear --sector newest --mode erased  # cut after the erase
just agent-qemu-otadata esp32 tear --sector newest --mode partial # cut before the crc word
```

The tool (`agent/tools/otadata.py`) takes the otadata offset from the bundle manifest,
refuses while the board runs, prints the decode before and after, and touches the 4096
bytes of one sector and nothing else. It writes only the two shapes IDF's
erase-then-program can leave. **A torn sector is a state power loss produces. Forging any
other state is not a test.**

**Assert on the bootloader, not only on the app.** Each boot prints
`I (…) boot: Loaded app from partition at offset 0x20000` (ota_0) or `0x200000` (ota_1).
That line is written after the bootloader has made its otadata decision, so a QEMU soft-reset
panic in the app that follows cannot confound it.

| Mode | Recipe | Pass |
|---|---|---|
| Boot loop | Deploy the `-bltest` artifact with `apply: "on_command"`. At `staged`: stop, start, wait ~20 s, stop, start. | `FF_FAULT_TEST=bootloop` appears **once**, after `ota state pending_verify`, then `abort() was called`. Every later `Loaded app` names the old slot. The old image logs `rolled_back (returned to ota_0; ota_1 did not confirm)`. otadecode: the bad slot is `ABORTED` (the bootloader did it, not our timer: that would be `INVALID`). Rows `requested, staging, downloading, verifying, staged, rolled_back`, no `confirming`. |
| Power cut mid-download | Stop the board when `update <cmd>: 30%` appears. | Cold boot: same slot, `ota state valid`, no `transaction` line. The first 4096 bytes of the target slot are the new `app.bin`, the full prefix is not. Rows end at `downloading`. A repeat POST gets `reused: true` with the same cmd_id, and runs through to `confirmed`. |
| Power cut during the read-back | Stop the board the moment `verifying` for the cmd reaches `up/status` (there is no serial line at read-back start). | No `matches what is on flash` line (if there is one you were late; repeat). Same cold boot as above. Rows end at `verifying`. |
| Torn otadata | Stage any normal version with `on_command`, stop, `cp .qemu/flash-esp32.bin /tmp/ff-staged.bin`. Tear `newest` erased, start. Restore, tear `newest` partial, start. Restore untouched, start (the control). | Torn: the previous slot boots `valid` and logs `stale transaction record for <cmd> — discarded`; one `rst:` banner. Control: the new slot boots `pending_verify` and confirms. |
| Hang before the session | Deploy a `-hangtest` artifact built from agent ≥ 0.4.3 with `on_command`. At `staged`: stop, start, then hands off for **≥ 330 s**. | R2-fw-4: `ff-mqtt: OTA boot: 300 s …` is logged **before** `ota state pending_verify`, then `FF_FAULT_TEST=hang` every 30 s. At ≈ 300 s of log time: `no working session 300 s after an OTA boot`. `Loaded app` names the old slot after the reset (or, after an `esp_timer_impl_init` panic, after a stop/start). otadecode: the hang slot is **`INVALID`** (our timer), not `ABORTED` (a reset). Rows `requested, staging, downloading, verifying, staged, rolled_back`, no `confirming`, no `rolling_back`. A `-hangtest` built before 0.4.3 (the dev catalog's `0.4.22-hangtest`) is the negative control: it sits in `PENDING_VERIFY` until a stop/start. |

**`otadata` moves at the start of every stage.** IDF's `esp_ota_begin()` erases the
**inactive** otadata sector when it names a slot other than the running one
(`esp_ota_invalidate_inactive_ota_data_slot`). A power cut mid-download therefore leaves
the active sector byte-identical, but not necessarily the whole partition. Compare per
sector, not one hash over both.

### Driving a flaky link (R2-test-2)

Slirp has no impairment of its own: no loss, no delay, no outage. `just agent-qemu-flaky`
puts a host-side TCP proxy (`agent/tools/flaky_link.py`, standard library only) between
the board and the stack, and changes its behaviour on a schedule. Results:
`docs/features/ota-deploy.md` → *Flaky link (R2-test-2)*.

**The fidelity limit.** Slirp is the guest's TCP peer. It ACKs the guest's segments and
answers its keepalive probes whatever the proxy does. An outage made on the host side
therefore always looks like **"the far end is alive but silent"** to the board. That is
the worst case for stall detection and right for timing. It does **not** model lwIP
retransmission and loss, Wi-Fi disassociation, DHCP renewal or `ff_net_wifi.c`'s TX-power
ladder (openeth has no radio). Proven in QEMU, never "on hardware". The bench procedure is
`rollback-test.md` → *Marginal radio*, owed.

**Wiring (all of it reversible).** Both download hops and both board endpoints must go
through the proxy ports:

```bash
ss -ltn | grep -E ':(18088|18883|19000) '        # must be empty
FF_PUBLIC_BASE_URL=http://10.0.2.2:18088 FF_S3_PUBLIC_ENDPOINT_URL=http://10.0.2.2:19000 \
  docker compose up -d --no-deps api             # + ADMIN_PASSWORD_HASH from .env.example if
docker compose exec -T api env | grep -E 'PUBLIC_BASE_URL|S3_PUBLIC'   # login with the dev one fails
just agent-qemu-flaky "0=pass" >> /tmp/flaky-pass.log 2>&1 &
just agent-cfg --api-base http://10.0.2.2:18088 --mqtt-uri mqtt://10.0.2.2:18883 \
      --link ethernet --hb 10 --ntp pool.ntp.org --token "$FFE"
```

The default listens are `18088 → 8088` (Traefik `web`; this box's `FF_HTTP_PORT`, use 8080
on a default box), `18883 → 8883` (Traefik `mqtt`) and `19000 → 9000` (MinIO). A
presigned URL signs the `Host` header, and the proxy passes `10.0.2.2:19000` through
unchanged, so the signature holds. Traefik's `ff-qemu` router matches ``Host(`10.0.2.2`)``
whatever the port. SNTP and DHCP go through slirp directly. Afterwards: stop the board,
`pkill -f flaky_link.py`, `docker compose up -d --no-deps api` with no override env, and
check `grep PUBLIC` shows `localhost` again.

**Modes** (every connection, live and new, on every listener of the process):

| Mode | What the board sees |
|---|---|
| `pass` | Forward both ways as fast as possible. |
| `throttle:N` | Forward, each direction capped at N bytes/s. |
| `blackhole` | Nothing forwarded, nothing closed. The proxy stops reading, so backpressure builds as on a dead radio. A new connection is accepted and held, and its upstream connect waits for the blackhole to end. If the client closes it first, it **never** reaches upstream (its SYN "never got through"). Held bytes on a live connection arrive late, intact and in order. |
| `reset` | An action: abort every live pair at that instant. The previous mode stays in effect. Slirp turns it into a FIN/RST, and `esp_http_client_read` errors. |

Schedules are `T=MODE,…` in seconds since the proxy started, first entry at `0`;
`--repeat P` loops them. Listens given after the schedule replace the three defaults, so a
second process can impair the store alone: `just agent-qemu-flaky "0=throttle:8192"
19000:127.0.0.1:9000`. **Change a schedule only while the board is stopped.** Killing the
proxy under a live board is a `reset` of every connection. Write `date +%s.%N` before each
proxy and each QEMU start. The proxy's `[s]` and the app's `I (ms)` differ by the gap
between the two starts plus the boot (2-4 s in the R2-test-2 runs).

| # | Scenario | Recipe | Pass |
|---|---|---|---|
| D1 | Slow download | api/mqtt `0=pass`; store `0=throttle:8192`. Deploy with `on_command`. | `10%`…`100%` over ≥ 100 s, `staged`. No `OTA boot: 300 s` line and no `no working session` on the running image. Stop/start: `confirmed`. |
| D2 | Outage mid-download, link returns | store `0=throttle:16384,20=blackhole,50=throttle:16384` (a 30 s outage, inside the stall budget). Start the board, deploy **at once** (the download must be running before t=20). | Progress stops ≈ 30 s, resumes, `matches what is on flash`, `staged`. No `no bytes for`, no `failed`. Stop/start: `confirmed`. See the openeth panic below. |
| D3 | Silent far end | store `0=throttle:16384,20=blackhole,240=pass`. Deploy at once. | Agent ≥ 0.4.4 (R2-fw-5): `E ff-ota: update <cmd>: no bytes for 60 s at <n> bytes — abandoning the download …` then `update <cmd> failed: download stalled`, ≈ 80 s after the last byte (60 s at the earliest), which is up to ~90 s after the last `N%` line, because progress is only logged every 10 %. `failed` / `download stalled` on the topic, deploy terminal. No reset, no transaction line. The active otadata sector is unchanged (the inactive one erased). The slot is free: after t=240 a new POST (a **new** cmd_id) runs to `staged`, without a reboot. Before 0.4.4 the download never ended by itself (600 s hold). |
| D4 | Far end closes | a `reset` inside the stall budget, e.g. store `0=throttle:16384,20=blackhole,40=reset` (R2-test-2 measured it as the reset after D3's 600 s hold, on agent 0.4.3). | `data read -1, errno 128`, `failed` / `download failed`, the board stays on its image. |
| P1 | Outage after the reboot, shorter than the timer | Stage with all-pass, stop, then ONE process: `just agent-qemu-flaky "0=blackhole,200=pass"`, start at once. | `OTA boot: 300 s` and `confirming on …` before the session. `announce acknowledged` < 300 000 ms, `CONFIRMED`, no `no working session`. |
| P2 | Outage after the reboot, longer than the timer | As P1, `"0=blackhole,330=pass"`. | `no working session 300 s after an OTA boot` at ≈ 300 000 ms, then the reset. After a stop/start past 330 s: the old slot, `rolled_back`. otadecode: the new slot `INVALID`. |
| P3 | Flapping link after the reboot | As P1, `"0=pass,5=blackhole" --repeat 30`. | Terminal either way (`confirmed` before 300 s, or the P2 shape), never `PENDING_VERIFY` past 302 s. |

**Any store outage of 60 s or more now ends `download stalled`** (agent ≥ 0.4.4,
R2-fw-5). The guard compares the image length each time `esp_https_ota_perform()`
returns, and a read in flight when the link goes silent returns its partial bytes only at
its 20 s timeout, which counts as progress. So the abort lands ≈ 80 s after the last byte
(60 s at the earliest), and the logged `no bytes for 60 s` counts from that last return.
Keep a "link returns" recipe's outage under 60 s, or it tests the stall guard instead.

**The openeth "RX frame dropped" panic is a harness artifact.** One D2 run in three
panicked a second or so after the link came back:
`Guru Meditation Error: Core 0 panic'ed (Cache error)`, decoded as
`emac_opencores_isr_handler (esp_eth_mac_openeth.c:66)` ← `_xt_lowint1` ←
`spi_flash_op_block_func`. Line 66 is the driver's `ESP_EARLY_LOGW(… "RX frame dropped"
…)`. Its format string lives in flash, and the ISR ran while the other core had the cache
disabled for an OTA flash write. openeth is the QEMU-only NIC (`CONFIG_ETH_USE_OPENETH`,
esp32 only). No board runs it, so it is not a product finding. The board soft-reset onto
its VALID image and the row parked at `downloading`. A re-POST (`reused: true`) ran to
`confirmed`. If you see it, repeat the run.

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

Since agent 0.4.6 (R2b-fw-1) the announce carries `"ssid":null,"known_networks":null`
right after `"link_type":"ethernet"`, and a config written with `--net` adds one line after
`link`: `networks  N in ff_cfg, unused (link is ethernet)`. That line is the only QEMU proof
that the known-networks parser ran; there is no radio to select with.

Since agent 0.4.7 (R2b-fw-2) the announce also carries the three board measurements right
after `ota_slot_size`: `"flash_chip_size":4194304,"partition_table_sha256":"1fa67e6b…59ed","rollback_capable":null`
on a serially flashed board. The boot prints one line after `device_id`:
`I ff-id: board: flash chip 4194304 bytes (physical), partition table sha256 1fa67e6b…59ed, rollback_capable unknown`.
An OTA'd image that boots in `pending_verify` logs
`W ff-id: rollback_capable: true — this OTA-written image booted in pending_verify, so this board's bootloader rolls back (stored)`
before `transaction <cmd>: confirming on ota_N`, and every later boot announces
`"rollback_capable":true` with no `(stored)` line, until a re-flash with a new token clears
it. A boot that finds its recorded slot running in state `new` (a bootloader that never
armed rollback) now ends `confirmed` with the detail `the bootloader never armed rollback
for this image` instead of `stale transaction record … discarded`. That cannot happen in
QEMU: our bootloader always arms rollback, and forging otadata is not a test. The torn
otadata row above is unchanged: a torn newest sector boots the *previous* slot, so it is
still `stale … discarded`.

**No token and no password appear anywhere in that transcript, by design**. If one ever
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
flushes. Give it a file or a terminal.

## Every emulated board is `000000000000`

QEMU's default eFuse image has a zero MAC. `device_id` is the eFuse MAC. Thus, every
board booted this way enrolls as `000000000000` and they would collide with each other.
The agent says so, loudly, once per boot. Consequences worth knowing:

* Two emulators at once are one device as far as the fleet is concerned.
* `000000000000` in `/v1/devices` on a real deployment means someone pointed an emulator
  at it, not that a board is broken.
* A real ESP32 always has a burned MAC. There is no code path that special-cases this.

`agent-qemu` now refuses to start a second board rather than trusting you to remember:
the container appears by name `ff-qemu-<target>`. A second `just agent-qemu esp32` exits 1
telling you to stop the first. **Two boards do not produce two sets of results — they
produce one unreadable set**, because both enroll, heartbeat and report stages as the
same `device_id`, and nothing on the server can tell them apart. S0-fw-1 discarded a
full round of acceptance evidence to this.

> **The trap that let it happen, worth knowing on its own:**
> `docker ps --filter ancestor=espressif/idf:v5.5.5` **matches nothing here**. `idf_image`
> pins by digest, and the `ancestor` filter compares the reference you typed, not the
> image the container actually runs. The command exits 0 having killed nothing, which
> reads exactly like "no emulators are running". Use `just agent-qemu-stop`, or match on
> the image column:
> ```bash
> docker ps --format '{{.ID}} {{.Image}}' | awk '$2=="espressif/idf:v5.5.5"{print $1}'
> ```
> Killing the `just` process does not help either: without `-it`, `docker run` leaves the
> container (and the emulator inside it) running.

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
already-used token triggers a refusal with a 409 and the log names *used, revoked or expired*.

**`--fresh` is not how a board is made to re-enroll any more** (S0-fw-4). It throws the
whole image away, credential *and* the cached RF calibration in IDF's `phy` namespace —
which is the very thing the flasher used to do on every flash and no longer does. What
clears the credential now is a **new enrollment token**: `ff_store_sync_token()` compares a
fingerprint of the `ff_cfg` token against the one stored beside the credential and erases
the `ff` namespace, and only that namespace, when they differ. So `--fresh` is the "brand
new board" button. The section below is the "re-flashed board" one.

## Re-flash ff_cfg without erasing NVS

`just agent-qemu-recfg <target>` writes `.qemu/ff_cfg.bin` into an **existing** flash image
at the offset the bundle manifest declares, leaving NVS alone. It is the emulator's
equivalent of re-flashing a board's config in the field. It is the only way to exercise
the S0-fw-4 path: a board holding a live credential, handed a token it did not see.

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

**To prove the calibration survives**, seed a `phy` namespace before the first boot (QEMU
has no radio, so the board never writes one itself) and read it back afterwards. Use IDF's
own tools. Do **not** `grep`/`strings` the region, because NVS marks entries erased in a
state bitmap and leaves the key bytes in flash until compaction. Thus, a hit proves nothing.

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

## Prove the clock rule

`spec/device-protocol.md` → *Clock — SNTP before TLS* only means something. This is because the
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

There is a third direction, added by S0-fw-2. It is the one that proves stage reports
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
same clock tick. `id` is what keeps them in the order the board produced them. Over
`http://` nothing is ever held. `link_up` goes out at link time even with `--no-ntp`.

## Kill it the two different ways

| How | What the fleet sees |
|---|---|
| `Ctrl-A x`, or `just agent-qemu-stop esp32` | an ungraceful death — the broker publishes the **LWT**, retained `{"online":false}` on `up/presence`, and `/v1/devices` flips to `"online": false` within ~1.5 × keepalive (measured ≈18 s at `--hb 10`, S0-fw-1) |
| nothing (just leave it) | heartbeats every `--hb` seconds, forever |

There is no goodbye publish. A board that is dying has no way to send one. Thus, the agent
does not pretend it can. The will is the mechanism.

## Reproduce a board that gets partway

The three failures worth being able to summon on demand. All are S0-fw-1's acceptance
evidence. All of them are what an operator is actually looking at when they say a
board "never appeared". Watch `GET /v1/devices` → `arrivals`, not the serial log.

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

Note the interaction, because it makes the flag flicker. `progress_stall_s` is 60 s and
the *first* retry interval is also 60 s. Thus, the row alternates `stalled=false/true` for
the first couple of minutes and only settles once the backoff doubled past 60 s.

**Stalled at `mqtt_refused` — enrolled. But the broker will not have it**. Rotate the
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

**Invisible — the enrollment token itself triggers a refusal**. Mint a token, revoke it, flash
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
`qemu_program` definition in the justfile. Thus, a green smoke run says something about the
recipe it guards rather than about a lookalike. It asserts only what the first seconds
can honestly prove:

1. no panic (`Guru Meditation`, `LoadProhibited`, `abort()`),
2. exactly one ROM `rst:0x` banner — **the boot-loop check**,
3. the `ff-agent` banner, so `app_main` ran,
4. `ff_cfg v1 loaded`, so it read its config partition.

It deliberately proves **nothing** about enrollment or MQTT: those need a live token and
`just up`, and they belong to `agent-qemu` and the transcript above. It uses a tokenless
throwaway config aimed at a closed port and its own `flash-<target>-smoke.bin`. Thus, it
spends no token and never disturbs the NVS your real emulated board is accumulating.

## The library example in QEMU (R3-fw-3)

The Arduino library (`agent/components/fleetforge/library.json` + `src/Fleetforge.cpp`)
gets the same proof as the agent: its Basic example boots in QEMU against the dev stack.
This box's traefik is on **8088** (searxng holds 8080), so the board's API base is
`http://10.0.2.2:8088`.

```bash
just agent-cfg --api-base http://10.0.2.2:8088 --mqtt-uri mqtt://10.0.2.2:8883 \
      --link ethernet --hb 10 --token "$FFE"     # the same blob as the agent's
just lib-bundle esp32-qemu      # lib-qemu/.pio/bundle/esp32-qemu (first run ~10 min)
just lib-qemu --fresh           # same Ctrl-A x / tty rules as agent-qemu
#   I (7656) ff-lib: fleetforge library 0.4.7, firmware 1.0.0
#   I (9110) ff-net: eth link up, ip 10.0.2.15 gw 10.0.2.2 mask 255.255.255.0
#   I (10732) ff-enroll: enroll 200 http://10.0.2.2:8088/v1/enroll
#   I (11459) ff-mqtt: mqtt connected as 000000000000 (mqtt://10.0.2.2:8883)
#   I (11567) ff-mqtt: announce acknowledged by the broker
just agent-qemu-stop esp32      # stops it: it is the same ff-qemu-esp32 board
```

What differs from `agent-qemu esp32`:

- **The flash image is `.qemu/flash-lib-esp32.bin`.** An agent image's NVS and offsets
  belong to `ab-4m-v1` and must never be mixed with `ab-4m-arduino-v1`. Here `ff_cfg` sits at
  `0x3D0000`, read from the bundle's manifest like every other offset. `--fresh` wipes it.
- **The container name is the same `ff-qemu-esp32`, and so is the refusal.** An agent
  board and a library board at once are one `000000000000` twice.
- **The NIC delta.** QEMU's esp32 has only the OpenCores NIC, and the stock Arduino
  libraries are built without its driver. `lib-qemu/platformio.ini` is the example's
  `env:esp32` plus `custom_sdkconfig = CONFIG_ETH_USE_OPENETH=y`. pioarduino then rebuilds
  the Arduino libraries from ESP-IDF ("hybrid compile"). It does that inside the package
  directory, so the recipe gives it its own `PLATFORMIO_CORE_DIR`
  (`~/.platformio-fleetforge-qemu`, override with `FF_LIB_QEMU_PIO_CORE`). Without that,
  every `just lib-build` after it deletes and re-downloads the Arduino framework.
- **The resolved config of that rebuild is not the stock one.** It differs beyond the NIC:
  no SPIRAM, no Matter or camera components, 240 MHz, DIO flash. A green QEMU run proves the
  library's C on the core's IDF, not the persona's exact binary. `just lib-build esp32` is
  that binary's compile proof.
- `GET /v1/devices` shows `partition_layout: ab-4m-arduino-v1`, `partition_table_sha256:
  05528998…1fc4`, `fw_version: 1.0.0` (the sketch's, from `Fleetforge.begin`), and
  `agent_version` = the library version.

## The worked example, end to end (R3-fw-4, R3-test-1)

`just lib-quickstart` plays the worked example's README quickstart:
`agent/components/fleetforge/examples/Basic/README.md` (Arduino) and
`examples/basic_idf/README.md` (ESP-IDF). Every command a reader types for a build sits in a
fenced block whose first line is `# quickstart: <name>`. `scripts/lib_quickstart.py` runs
those blocks **verbatim** (`bash -euo pipefail`, from the tree root) in a clean temp copy of
the tree (`git ls-files --cached --others --exclude-standard`, so uncommitted work is in,
ignored build output is out). A README that loses a block fails the run by name.

```bash
just lib-quickstart --build-only   # ~7 min: no stack, no QEMU
# the same README blocks on an EMPTY PlatformIO core (R3-fw-7): one-time ~4.5 GB download,
# ~6 min with --skip-idf (measured: arduino-build 284 s from nothing, own project 59 s)
just lib-quickstart --build-only --skip-idf --fresh-pio-core
# the api must hand the board 10.0.2.2 origins for the OTA half (see above). On this box:
FF_PUBLIC_BASE_URL=http://10.0.2.2:8088 FF_S3_PUBLIC_ENDPOINT_URL=http://10.0.2.2:9000 \
  docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --no-deps api
just lib-quickstart                # ~25-30 min: builds, enroll → heartbeat → OTA → confirm → broken build → rollback
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --no-deps api  # restore
```

`--no-deps` is this box's quirk: its MinIO runs from a local override, and without the
flag compose tries to pull `minio/minio` and fails. The script refuses (exit 2, printing
the fix) when the api's `PUBLIC_BASE_URL`/`S3_PUBLIC_ENDPOINT_URL` are not `10.0.2.2`, when
the API does not answer, or when an `ff-qemu-esp32` is already running. It checks all of
that before it mints a token.

What it does:

1. **Builds** (`--build-only` stops here). Arduino: `arduino-build` (esp32, esp32s3),
   `arduino-edit` (the README's `sed`: `"SOS"` → `"HELLO"`, `"1.0.0"` → `"1.1.0"`), and
   `arduino-build-b`. B must differ from A. ESP-IDF: `idf-build` and `idf-build-s3`
   inside the pinned `idf_image`, on a container-local copy, so nothing is written to the
   host. Every build must have zero `warning:` lines from the library or the example and an
   app < 1966080 B. Each IDF build's `partition-table.bin` is decoded and must equal
   `agent/partitions.csv`. Its resolved `sdkconfig` must have rollback on and no eFuse burn
   (exact option names, like `verify_bundle.py`). Its app descriptor must say `1.0.0`.
2. **QEMU bundles.** `just lib-bundle esp32-qemu` in the temp tree for A. Then
   `arduino-edit` again, plus one run-unique substitution, `"1.1.0"` →
   `"1.1.0-qs<epoch>"`: a `(target, version)` label is a promise about bytes, and a rerun
   uploading rebuilt `1.1.0` bytes would get 409. Then the B bundle. A is copied back,
   because `--fresh` flashes from the bundle dir (*Two builds* above). A fresh `lib-qemu/`
   has no `sdkconfig.defaults`, so pioarduino reinstalls the framework and does the hybrid
   compile (~11 min) on every run. `just lib-bundle` runs `pio run` twice, because that
   first build otherwise makes the `-t idedata` call wipe the build dir.
3. **The board steps.** Log in (password from `$FF_ADMIN_PASSWORD` or `.env`, never
   printed or put in argv). Mint a token. `just agent-cfg` (ethernet, `--hb 10`). Then
   `just lib-qemu --fresh` stands in for the USB flash. Wait for `ff-enroll: enroll 200`,
   `announce acknowledged by the broker` and `morse: SOS (firmware 1.0.0)`, then
   `GET /v1/devices`: online, `fw_version 1.0.0`, `ab-4m-arduino-v1`, `capabilities
   ["ota"]`.
4. **The OTA.** Upload bundle B's `app.bin` (`partition_layout=ab-4m-arduino-v1`). Deploy
   with **`apply: "on_command"`**: QEMU cannot survive the self-reboot (*The emulator cannot
   survive `esp_restart()`*), and a real board takes the dashboard default `auto`. Wait for
   `is staged and bootable` and deploy state `staged`, then power-cycle (`agent-qemu-stop`,
   `lib-qemu`). Wait for `CONFIRMED` and `morse: HELLO (firmware 1.1.0-qs…)`, then the API:
   `fw_version` == B, deploy `confirmed`, `is_terminal: true`.
5. **The broken build (R3-test-1).** Bundle R is the committed sketch with `"SOS"` →
   `"BAD"` and `"1.0.0"` → `1.2.0-qs<epoch>-rbtest`, built with
   `PLATFORMIO_BUILD_FLAGS=-DFF_ROLLBACK_TEST=1` (fault injection, never a README step; A
   and B are built with that variable removed). It switches on `ff_mqtt.c`'s hook: the
   announce is published but its PUBACK ignored, so the session never confirms, and the
   confirm timer is 60 s. R's `app.bin` must contain the hook's log line before it is
   uploaded (A's and B's must not). Upload, deploy `on_command`, wait for `update <cmd>:
   ota_0 is staged and bootable` (R's slot must differ from B's), power-cycle. R's console
   must show, in order, `OTA boot: 60 s from now …`, `ff-lib … firmware <R>`, `confirming on
   ota_0`, `FF_ROLLBACK_TEST: ignoring the announce ack` and `no working session 60 s after
   an OTA boot`, plus `morse: BAD (firmware <R>)`. The board then marks R invalid and
   reboots by itself; QEMU panics on that `esp_restart()` (`rst:`), so a second power cycle
   stands in for the reset a real board does. B's boot logs `transaction <cmd>: rolled_back
   (returned to ota_1; ota_0 did not confirm)` and `morse: HELLO (firmware <B>)`, and the
   API must show deploy `rolled_back`, terminal, `fw_version` == B, `confirming` and
   `rolled_back` in the steps. **Any `confirmed` fails the run at once**: in an API poll
   (state or steps), on R's console, or as R's version on the final row. `rolling_back` is
   reported, not required (best effort). R's bundle takes ~30 s: the changed flags rebuild
   the project and the library, not the framework.
6. **Credentials.** The token must occur 0 times, and no `"mqtt_password":"` value at all,
   in every QEMU log, every build log and the script's own output. Then unplug, and report
   the LWT.

**The OTA artifact must be the QEMU build** (bundle B's `app.bin`), never the persona
`firmware.bin` from step 1. The persona build has no driver for QEMU's only NIC: OTA'd into
the emulator, it would never reach the fleet and would, correctly, roll back.

`--keep` leaves the temp tree for inspection. It then holds a live credential in `.qemu/`,
so delete it. `--skip-idf` is a dev shortcut; acceptance runs without it.

The board half of the first passing run (2026-10-09, 1301 s in all, 742 s of which went to
bundle A's hybrid compile), trimmed:

```
I (7046) ff-lib: fleetforge library 0.4.7, firmware 1.0.0
I (10267) ff-enroll: enroll 200 http://10.0.2.2:8088/v1/enroll
I (11069) ff-mqtt: announce acknowledged by the broker
morse: SOS (firmware 1.0.0)
GET /v1/devices 000000000000   online True, fw_version 1.0.0, ab-4m-arduino-v1, ['ota'], agent_version 0.4.7
POST /v1/artifact              201, version 1.1.0-qs1791521470 (bundle B app.bin, 1176160 B)
POST …/deploy on_command       202
I (63266) ff-ota: update 7416d299…: ota_1 is staged and bootable      (staged 48 s after the deploy)
--- power cycle ---
W (13796) ff-mqtt: this image was written by OTA and is now CONFIRMED: the broker accepted us …
morse: HELLO (firmware 1.1.0-qs1791521470)
GET /v1/devices                fw_version 1.1.0-qs1791521470, deploy confirmed (is_terminal True), 68 s after the deploy
credentials                    token 0 occurrences, mqtt_password 0 (13 files)
```

The rollback half of the first passing three-run pass (2026-10-09, 1512 s in all; bundle R
32 s), trimmed:

```
POST /v1/artifact              201, version 1.2.0-qs1791532201-rbtest (bundle R app.bin, 1177040 B, carries the hook)
POST …/deploy on_command       202, cmd_id 5f87a826…
I (69853) ff-ota: update 5f87a826…: ota_0 is staged and bootable      (staged 49 s after the deploy; B on ota_1)
--- power cycle ---
W (2120) ff-mqtt: OTA boot: 60 s from now to reach the fleet or roll back
I (9380) ff-lib: fleetforge library 0.4.7, firmware 1.2.0-qs1791532201-rbtest
W (12207) ff-mqtt: transaction 5f87a826…: confirming on ota_0
E (12346) ff-mqtt: FF_ROLLBACK_TEST: ignoring the announce ack on purpose — this image must roll back in 60 s
morse: BAD (firmware 1.2.0-qs1791532201-rbtest)
E (69425) ff-mqtt: no working session 60 s after an OTA boot — marking this image invalid and rolling back to the previous slot
rst: …                                                                (the esp_restart panic)
--- power cycle ---
W (14381) ff-mqtt: transaction 5f87a826…: rolled_back (returned to ota_1; ota_0 did not confirm)
morse: HELLO (firmware 1.1.0-qs1791532201)
GET /v1/devices                deploy rolled_back (is_terminal True), fw_version 1.1.0-qs1791532201, steps requested, staging, downloading, verifying, staged, confirming, rolling_back, rolled_back; 141 s after the deploy
credentials                    token 0 occurrences, mqtt_password 0 (19 files)
```

## A wrong flash layout is refused (R3-fw-5)

The library example's real firmware, booted on a deliberately wrong partition table
(`tests/fixtures/wrong-layout-partitions.csv`: the Arduino offsets, but 0x1C0000 = 1835008-B
slots instead of 1966080). The board must announce `partition_layout: "unknown"`, say so
loudly on the console, and the server must refuse every deploy to it, naming the fix. No
board, no USB: the swapped table is a file in a temp copy of the bundle.

```bash
cd /home/boris/products/fleetforge && just up
BASE=http://localhost:8088
# TOKEN = an admin session (POST /v1/auth/login with the dev FF_ADMIN_PASSWORD from .env;
# the ff_session cookie value is the bearer). newtok mints a single-use enrollment token.
newtok() { curl -sS -X POST "$BASE/v1/enrollment-tokens" -H "Authorization: Bearer $TOKEN" \
           -H 'content-type: application/json' -d '{}' | jq -r .token; }

just lib-bundle esp32-qemu        # the bundle with this tree's component
S=$(mktemp -d); cp -r lib-qemu/.pio/bundle/esp32-qemu "$S/bundle"
cp tests/fixtures/wrong-layout-partitions.csv "$S/"
docker run --rm -u $(id -u):$(id -g) -v "$S:/s" --entrypoint bash <idf_image> -c \
  '. $IDF_PATH/export.sh >/dev/null 2>&1; python $IDF_PATH/components/partition_table/gen_esp32part.py /s/wrong-layout-partitions.csv /s/bundle/partition-table.bin'
just agent-cfg --api-base http://10.0.2.2:8088 --mqtt-uri mqtt://10.0.2.2:8883 \
      --link ethernet --hb 10 --token "$(newtok)"
rm -f .qemu/flash-lib-wrong-esp32.bin   # its own flash file: never mix it with flash-lib-esp32.bin
# the lib-qemu recipe's docker run line, with only FLASH and the /d mount changed:
docker run --rm --name ff-qemu-esp32 --network host -u $(id -u):$(id -g) \
  -e TARGET=esp32 -e FLASH=/q/flash-lib-wrong-esp32.bin -e FFCFG=/q/ff_cfg.bin \
  -e QEMU_SHA256=<qemu_sha256> -e IDF_IMAGE_REF=<idf_image> \
  -v "$PWD/.qemu:/q" -v "$S/bundle:/d:ro" -v "$PWD/agent/tools:/t:ro" \
  --entrypoint bash <idf_image> -c "$(just --evaluate qemu_program)" > /tmp/wrong.log 2>&1 &
```

`qemu_image.py` places every part by the manifest's offset and does not re-hash it, so the
swapped table lands at 0x8000. What the first run (2026-10-09) printed:

```
E (7708) ff-id: this board's partition table is not a layout this firmware knows: its OTA slot is 1835008 bytes, and this build expects ab-4m-arduino-v1, whose two OTA slots are 1966080 bytes each. It announces partition_layout "unknown" and the server refuses every update. Fix: build with the partitions.csv for ab-4m-arduino-v1 (the library's examples/Basic/partitions.csv, beside the sketch) and flash it once over USB; a partition table never changes over the air.
I (7720) ff-id: board: flash chip 4194304 bytes (physical), partition table sha256 47db53920359cfb4581532a293d8e563401f3abe37f9b282cc713039ac937c4c, layout unknown, rollback_capable unknown
I (12651) ff-enroll: enroll 200 http://10.0.2.2:8088/v1/enroll
I (13484) ff-mqtt: announce acknowledged by the broker
```

Then:

- `GET /v1/devices` shows `000000000000` with `partition_layout: "unknown"`,
  `ota_slot_size: 1835008` and `partition_table_sha256: 47db5392…7c4c`.
- Upload any esp32 app with `partition_layout=ab-4m-arduino-v1`. `POST
  /v1/devices/000000000000/deploy/precheck` gives `deployable: false`, `unsupported_layout`,
  and `POST …/deploy` gives 409 with the same sentence word for word. The sentence names
  `unknown`, `1835008`, the fingerprint, `ab-4m-arduino-v1`, `1966080`,
  `examples/Basic/partitions.csv` and USB.
- `grep -c ff-ota /tmp/wrong.log` is 0: nothing was staged.
- Stop it with `just agent-qemu-stop esp32`. Positive control: `just agent-cfg … --token
  "$(newtok)"`, then `just lib-qemu --fresh`. The row goes back to `ab-4m-arduino-v1` /
  `05528998…1fc4`, with no `E … ff-id` line, and the precheck is deployable.

The server half alone, with no QEMU: `just sim --platform-type esp32 --partition-layout
unknown --ota-slot-size 1835008 --partition-sha 47db…7c4c --capabilities ota …` gives the
same refusal.

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
  matching the transcript above line for line.
* Five further boots, three of them under eight busy-loops on a four-core box, testing
  the theory that host starvation fires a spurious watchdog interrupt. Zero panics.
* Every one of those runs carries the `wdt_disable` flag. Thus, the flag is not the cause.

Two real defects turned up while proving that, and the first explains how a working
harness became an unrunnable one.

**1. The recipe could not run without a terminal**. `agent-qemu` passed `docker run -it`
unconditionally:

```
$ just agent-qemu esp32
cannot attach stdin to a TTY-enabled container because stdin is not a terminal
```

Every agent session, script and CI shell is non-interactive. Thus, the recipe could not run by the things that most need to run it. The way round it is to hand-roll a
`docker run`. This is exactly where an emulator invocation acquires a wrong `-M`, `-m`
or `-global` and starts panicking in the watchdog. That is the most probable origin of
the filed backtrace. Fixed: `-it` is now passed only when stdin is a TTY.

**2. The emulator was pinned but never checked**. `idf_image` pins by sha256 and
each bundle manifest records the same digest, which reads as a guarantee that the
emulator has a fix. It is not — **Docker checks a digest on `pull`, not on `run`**. Thus, a
locally damaged or replaced layer runs in silence. `qemu-system-xtensa` lives in
that image. The pinned image was in fact **absent from this box's Docker store** when
the investigation started and had to be re-pulled (2.4 GB), on a disk sitting at 85%.

Every other input to a boot is content-addressed and deterministic: the bundle (per-part
sha256 in `manifest.json`), the eFuse blob (IDF's own `default_efuse` bytes), the flash
merge (`esptool merge_bin` over manifest offsets). Identical inputs cannot produce two
different behaviors — so at failure time one input was not what it claimed. The
local emulator image is the only one verifiably in a different state since. Unprovable
after the fact. Closed going forward: `qemu_sha256` in the justfile pins the hash of the
emulator **binary**, checked inside the container before every boot. Its version is printed into every transcript.

The honest limit: hashing that one binary is not a full integrity check of the image
(shared libraries and the Python tooling are not covered). It covers the file whose
behavior decides whether a boot means anything. This is the part that was in doubt.

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

## Troubleshoot

| Symptom | Cause |
|---|---|
| `cannot attach stdin to a TTY-enabled container` | an old `agent-qemu` that passes `-it` unconditionally. The recipe now only does so when stdin is a terminal |
| `the emulator is not the one this repo pins` | the local copy of the ESP-IDF image is damaged or replaced. Re-pull it by digest. Docker does not re-check on run |
| `SMOKE FAILED: the board reset N times` | a genuine boot loop — a corrupt `app.bin` does exactly this. `just agent-verify <target>` re-hashes the bundle against its manifest |
| `enroll 404` and the api log shows nothing | the `ff-qemu` Traefik router is missing or the frontend is unhealthy — the 404 is Traefik's, not the API's |
| `no .qemu/ff_cfg.bin` | run `just agent-cfg …` first. The recipe refuses to boot a board with no config |
| `qemu_image: … is missing — run: just agent-build <target>` | no bundle on disk |
| `enroll 409` on a fresh image | that token is spent and outside the grace window — mint another |
| board sits at `retrying enrollment in 60 s` | read the line above it: 401/409/422 are permanent and say so, anything else retries |
| `ff_cfg: crc32 mismatch` | the blob was corrupted or truncated. Regenerate it, then `--fresh` |
| no `link_up` row against an `https://` base, but the later stages are there | the firmware predates S0-fw-2: the report went out at epoch 0 and its TLS handshake failed certificate validity. Re-build the bundle (`just agent-build esp32`) |
| clock stays 1970 | no DNS or no outbound UDP/123 from this box; every `https://`/`mqtts://` endpoint then fails validation |
| everything fails with `connection refused` against `10.0.2.2:18088`/`18883` | the flaky-link proxy is not running, and ff_cfg points at it (`just agent-qemu-flaky`, or re-cfg onto 8088/8883) |
| QEMU exits instantly with an efuse error | delete `.qemu/efuse.bin` and let it be regenerated from the IDF pin |

## Related

* `docs/runbooks/agent-build.md` — where `agent/dist/<target>` comes from
* `docs/runbooks/dev-stack.md`. The stack this boots against, admin login, `just sim`
* `spec/device-protocol.md` — the wire contract every line of that transcript implements
