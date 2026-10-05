# Known-networks bench: move a board between two networks (R2b-test-4)

Agent 0.4.6 learned to carry up to four Wi-Fi networks and to pick among them (R2b-fw-1), the
flasher writes them (R2b-fe-12), the server stores the one the board is on (R2b-be-5), and the
fleet row shows it (R2b-fe-13). QEMU has no radio, so none of the selection has run on metal.
This runbook is the one bench session that does, on the ESP32-S3 `94a990dd09a4` (COM3, native
USB), and it grades every step from the server's own record with `just bench-net`.

**The bar.** S1 passes: flashed with two networks and powered with only network 2 in range, the
console shows `joined "<net2>" (known network 2 of 2)` and `just bench-net 94a990dd09a4 "<net2>" 2`
prints `NET PASS`. S2-S4 also pass, which proves the board moves between the two networks and the
row follows, with no re-flash. S5 is run if network 2 can be switched off. The *why* of the
selection rules is in [spec/device-protocol.md](../../spec/device-protocol.md) → *Known networks*
and [enrollment.md](../features/enrollment.md) → *Known networks: built in the agent*. This page
does not repeat them.

## Why it needs a bench

A scan, an association, a DHCP lease and a real access point going away are properties of a
radio. The Linux dev box (a GCP VM) has none, QEMU has none, and the simulator only *announces* a
network. The simulator did prove the server half (see DECISIONS 2026-10-05, R2b-test-4): the row
follows a board that moves, even when its old broker session is still half open. What is left is
the board.

## The checker

```
just bench-net <device> <ssid> <known> [prod|dev] [timeout]
just bench-net 94a990dd09a4 "bench-ap" 2             # prod, polls up to 300 s
just bench-net 94a990dd09a4 "bench-ap" 2 prod 0      # one read, for the record sheet
```

It reads one row of `devices` (psql over ssh for prod, read-only, no admin credential), prints the
row in the fleet row's own words **each time it changes**, and ends in one line:

| Last line | Exit | Meaning |
|-----------|------|---------|
| `NET PASS on "<ssid>" (known network count N)` | 0 | online, wifi, on that exact SSID, knows N |
| `NET FAIL <why>` | 1 | a wrong flash: link not wifi, agent below 0.4.6, or the wrong known count. Waiting will not help |
| `NET TIMEOUT after N s: <why>` / `NET WAIT <why>` | 3 | right board, not there yet (timeout 0 prints `NET WAIT`) |
| (a message on stderr) | 2 | refused device id, no such device, ssh or psql failed |

The SSID is compared exactly: case-sensitive, byte for byte, as the agent matches it. Type each
SSID as it is broadcast. `this server has no ssid column: the R2b server (migration 0005) is not
on it` means prod does not have the release yet.

## Before the run (STOP until all hold)

1. **R2b is on prod.**
   ```bash
   curl -s https://bingo.tvaroska.sk/v1/healthz          # the commit
   git merge-base --is-ancestor 1dfe042 <that commit> && echo yes
   just bench-net 94a990dd09a4 x 2 prod 0                 # must not say "no ssid column"
   ```
   `1dfe042` (R2b-fe-13) descends from R2b-be-5 and R2b-fe-12, so one check covers all three.
   *Snapshot 2026-10-05:* prod answers 0.4.2, commit `9200e0f`. It does not hold.
2. **`S0-bug-1` is resolved.** This flash erases the evidence its diagnosis needs. The board has
   been offline since 2026-10-04 14:24 UTC. Coordinate with the other re-flashing bench work,
   `S0-test-1` Check E and `S0-test-2` Check F. Two networks also suit R2b-test-2's baseline (it
   needs agent 0.4.5 or later on a normal image), but see the radio-setup note in
   [bench-replay.md](bench-replay.md): with two networks on the board, "AP off" fails over.
3. **Agent 0.4.6 or later on the flash path**, by one of two routes (section *Flash*):
   - Route A (the real Flow 3): the prod flasher, after `S0-infra-10` publishes.
     `just agent-check-prod` must print `CHECK-VERSION OK`. *Snapshot 2026-10-05:* the flasher
     serves esp32s3 0.3.2. A 0.3.x agent ignores the networks after the first, so it does not hold.
   - Route B (fallback, needs only the R2b release): esptool from the bench with the bundle built
     here and an `ff_cfg` blob.
4. **Two 2.4 GHz networks with internet** that reaches `bingo.tvaroska.sk` on 443 and 8883.
   Suggested: network 1 is a phone hotspot (its switch is "network 1 off") and network 2 is the
   bench AP, which stays on. The ESP32-S3 is 2.4 GHz only: on an iPhone turn on *Maximize
   Compatibility*, on Android set the hotspot band to 2.4 GHz. Use WPA2 or WPA2/WPA3 transition.
   If you can, place the board nearer network 2's AP, so that S3 shows priority beating signal
   strength. Record which AP was nearer.
5. **One console reader**: the dashboard's console, or PuTTY on COM3 at 115200 logging to a file.
   Not both.

## Flash (hotspot OFF: the flash's first boot is S1)

### Route A: the prod flasher

On the dashboard flash page choose Wi-Fi. Network 1 is the hotspot's SSID and passphrase. Use *Add
another network* for network 2, the bench AP. Record Y or N for the R2b-fe-12 items still owed:

- Chrome offered to save each network's passphrase.
- A Wi-Fi field was **not** autofilled with the dashboard password (the known autofill hazard,
  DECISIONS 2026-10-05, R2b-fe-12).
- With two networks, the old-agent warning is absent.

### Route B: esptool, with a bundle built on the dev box

Copy these to the bench host (Windows), from `agent/dist/esp32s3/` on the dev box: `bootloader.bin`,
`partition-table.bin`, `ota-data-initial.bin`, `app.bin` and `manifest.json`. Check them first with
`python3 agent/tools/verify_bundle.py agent/dist/esp32s3`, which must print `agent 0.4.7`.

1. **Mint a token** (on the dev box, against prod; the admin password is read, not typed into history):
   ```bash
   BASE=https://bingo.tvaroska.sk
   read -rs FFPW; echo
   TOKEN=$(curl -sSi -X POST "$BASE/v1/auth/login" -H 'content-type: application/json' \
           -d "{\"password\":\"$FFPW\"}" | grep -i '^set-cookie:' \
           | sed -E 's/.*ff_session=([^;]+).*/\1/' | tr -d '\r'); unset FFPW
   FFE=$(curl -sS -X POST "$BASE/v1/enrollment-tokens" -H "Authorization: Bearer $TOKEN" \
         -H 'content-type: application/json' -d '{}' | jq -r .token)
   ```
2. **Build the blob** (the same dev box; both passphrases are read, not typed into history):
   ```bash
   NET1="<hotspot ssid>"; NET2="<bench ap ssid>"
   read -rs PSK1; echo; read -rs PSK2; echo
   mkdir -p -m 700 /tmp/ff-t4
   python3 agent/tools/ff_cfg.py --out /tmp/ff-t4/ff_cfg.bin \
       --api-base https://bingo.tvaroska.sk --mqtt-uri mqtts://bingo.tvaroska.sk:8883 \
       --link wifi --ssid "$NET1" --psk "$PSK1" --net "$NET2" "$PSK2" --token "$FFE"
   unset PSK1 PSK2 FFE TOKEN
   ```
   The MQTT URI is what the flasher writes by default (`FlashBoard.tsx::defaultMqttUri`:
   `mqtts://<host>:8883`). `--hb`, `--ntp` and `--power` are left at their defaults, as the flasher
   leaves them. It prints `wrote 4096 bytes` and never prints a passphrase. Copy `ff_cfg.bin`
   to the bench host beside the bundle.
3. **Flash**, with the offsets from the manifest (`agent/dist/esp32s3/manifest.json`), not from
   memory. Never write `nvs`:
   ```
   esptool --chip esp32s3 -p COM3 write_flash ^
       0x0 bootloader.bin 0x8000 partition-table.bin 0xf000 ota-data-initial.bin ^
       0x20000 app.bin 0x12000 ff_cfg.bin
   ```
4. **Delete `ff_cfg.bin`** on both machines afterwards (`rm -rf /tmp/ff-t4` on the dev box). It
   holds a live token and both passphrases.

*Snapshot 2026-10-05 of the route-B bundle* (`just agent-build esp32s3`, then `verify_bundle.py`;
the agent sources are unchanged since, so it stays valid until `agent/` changes):

```
source_commit   8cf4475df2cd4181b1096307221cd56566274497 (built 2026-10-05T23:01:03Z)
verify_bundle   esp32s3 / ESP32-S3 · agent 0.4.7 · idf v5.5.5 · ab-4m-v1 · slot 1966080 B · rollback enabled, no eFuse burns
app.bin sha256  675f540114c89af0e0aa347b7f21c87b3d23dd3cbd0101ff8f437b941bd80a92
manifest        bootloader 0x0 · partition-table 0x8000 · ota-data 0xf000 · app 0x20000 · ff_cfg 0x12000
```

Run `sha256sum agent/dist/esp32s3/app.bin` on the dev box before you copy it. If it differs, rebuild
and replace this snapshot.

### After either route

The boot console shows `networks  2 known: <net1>, <net2>` (the `ff_cfg` log) and `wifi sta
starting, 2 known networks; scanning`.

## The session

One `just bench-net` per step, run on the dev box. Write down every line.

- **S1: only network 2 in range (first boot, hotspot off).** Console: `scan: A access points,
  1 of 2 known networks in range`, `trying "<net2>" (known network 2 of 2)`, `joined "<net2>"
  (known network 2 of 2)`, then enrolment and `announce acknowledged by the broker`. Run
  `just bench-net 94a990dd09a4 "<net2>" 2` and expect `NET PASS`. Fleet row: `online, on: <net2>` and
  `knows 2 networks`. The result card's Link row names the network and the count.
- **S2: network 1 comes back, the board stays.** Turn the hotspot on and wait 3 minutes. Console:
  no `disconnected` and no `scan:` line. `just bench-net 94a990dd09a4 "<net2>" 2 prod 0` is still
  `NET PASS`. This proves the spec's rule that a board never leaves a working association.
- **S3: priority on a fresh boot.** Replug USB with both networks on. Console: `2 of 2 known
  networks in range`, `trying "<net1>" (known network 1 of 2)`, `joined "<net1>" (known network 1
  of 2)`. `just bench-net 94a990dd09a4 "<net1>" 2` is `NET PASS`, and the row changes to `on: <net1>`.
- **S4: the move.** Turn the hotspot off while the board is joined to it. Console: `disconnected
  (reason N); link to "<net1>" lost; re-selecting in … ms`, a scan with `1 of 2`, then `joined
  "<net2>" (known network 2 of 2)`. `just bench-net 94a990dd09a4 "<net2>" 2 prod 180` is `NET PASS`.
  The watch prints the row's transitions, which might go `online, on: net1`, then `offline, last
  on: net1`, then `online, on: net2`. Copy them into the record sheet. **Watch for:** the console
  shows `announce acknowledged` on network 2 but the row stays offline for more than 60 s. That is
  the old session's last-will landing after the new session's presence, a server or broker
  defect, so it gets an S0 task.
- **S5 (if network 2 can be switched off): nothing in range.** Turn network 2 off, hotspot still
  off. Console: `link to "<net2>" lost`, then once per cycle `no known network in range (2 known);
  scanning again in N s`, with N growing to 30. **No reboot**: no reset banner, and the console's
  boot counter stays put. `just bench-net 94a990dd09a4 "<net2>" 2 prod 0` is `NET WAIT`, status
  `offline, last on: "<net2>"`, exit 3. That is the expected result here. The dashboard card says
  "none of its 2 known networks is in range" (R2b-fe-13). Turn network 2 on: within about 35 s
  `joined "<net2>"`, and `bench-net` is `NET PASS`.

**Still unproven after this session**, plainly: the 20 s DHCP watchdog (it needs an AP that
associates but gives no lease), the strongest-AP choice within one SSID (it needs a mesh), and
the direct tries of hidden SSIDs.

## What a failure means

- The board never joins network 2 in S1, leaves network 2 in S2, picks network 2 in S3 with both
  in range, or reboots in S5: file a **Sprint 0 fw task** (agent, `ff_net_wifi.c`) with the console
  log. Recover by re-flashing with one network.
- The console says joined and the announce was acknowledged, but `bench-net` never passes: file a
  **Sprint 0 be task** (ingest and presence) with the `bench-net` transitions and
  `docker compose logs fleetforge-ingestor | grep 94a990` from prod.
- `NET FAIL` (agent below 0.4.6, or a wrong count) is a wrong flash, not a defect. Re-flash.

## Record sheet

| Step | Time | Console lines seen (Y/N each) | `bench-net` last line | Fleet-row text | Verdict | Notes |
|------|------|-------------------------------|-----------------------|----------------|---------|-------|
| S1 only net 2 | | | | | | |
| S2 net 1 back | | | | | | |
| S3 both, fresh boot | | | | | | which AP was nearer: |
| S4 the move | | | | | | transitions: |
| S5 nothing in range | | | | | | |

R2b-fe-12 items (Route A): Chrome offered to save each passphrase Y/N; no autofill with the
dashboard password Y/N; old-agent warning absent with two networks Y/N.

**Human follow-up, not part of the run:** tick `R2b-test-4` in `TODO.md`. Replace "Wi-Fi selection
is unproven until R2b-test-4" in `docs/features/enrollment.md` with the bench result. Note fe-12's
Chrome save-bubble result there. Add a DECISIONS line if anything failed.
