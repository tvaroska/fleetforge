# Bench replay of the R2 recovery paths (R2b-test-2)

The R2 recovery paths were proven in QEMU, not on metal: boot loop, power cut mid-download,
hang before the session, a store that stops answering, and a marginal radio. Flow 2's
"rolls back on its own" rests on them. This runbook is the one ordered bench session that
replays them on the ESP32-S3 `94a990dd09a4` (COM3, native USB) and grades every step from the
server's own record with `just bench-judge`.

**The bar.** Every step below ends in `JUDGE PASS <scenario>`, the console lines it names were
seen, and nobody touched the board inside a rollback window. The *why* of each fault, and what
QEMU showed, is in [rollback-test.md](rollback-test.md); this page does not repeat it.

## Why it needs a bench

QEMU has no radio, no USB power to pull, and an `esp_restart()` that panics in
`esp_timer_impl_init`, so every QEMU proof drove the board with `apply: on_command` and a
stop/start. The bench runs the real path: `apply: auto`, a real reset, a real bootloader, a
real Wi-Fi link. The fault images go onto the board **as deployable artifacts only**.

## The judge

```
just bench-judge <scenario> <ref>          # prod: a read-only SELECT over ssh
just bench-judge <scenario> <ref> dev      # the dev stack
python3 scripts/bench_judge.py --list      # the scenarios
```

`ref` is a `cmd_id` (32 hex) or the device id (12 hex, meaning that board's newest
transaction). Anything else is refused before any SQL exists. It prints the transaction's
rows with relative times, one `check:` line per rule, and a last line `JUDGE PASS|FAIL|INCOMPLETE
<scenario>`. Exit 0 PASS, 1 FAIL, 2 refused / no rows, 3 INCOMPLETE (the transaction is still
open: run it again in a minute). The judge reads `deploy_events`, not the API: `GET /v1/devices`
carries only the newest transaction's summary, not every row.

## Before the run (STOP until all hold)

1. **The board is online on prod, and `S0-bug-1` is resolved.**
   ```bash
   ssh prod "cd /opt/boris/prod && docker compose exec -T postgres psql -U fleetforge fleetforge \
     -At -c \"SELECT device_id, fw_version, agent_version, last_seen FROM devices WHERE device_id='94a990dd09a4';\""
   ```
   `last_seen` within the last minute (or the dashboard shows it online).
   *Snapshot 2026-10-04:* offline since 14:24 UTC (`S0-bug-1`, the power cycle is owed).
2. **The running agent is 0.4.5 or later, on a normal image (no `-rbtest`/`-bltest`/`-hangtest`).**
   Every step needs it: 0.4.0 so the returned-to image reports `rolled_back`, 0.4.3 so a hang
   image can roll back, 0.4.4 for the download stall guard. Two routes:
   - a normal dashboard deploy of `0.4.5` (`agent/dist/esp32s3/app.bin` as an artifact). On a
     0.3.x board it parks at `rebooting`: that is the transition gap in rollback-test.md, and it
     is expected. After the reboot the board announces 0.4.5;
   - a USB flash from the prod flasher, once `S0-infra-10` publishes 0.4.5 (it serves 0.3.2 today).
   The same SELECT shows `fw_version`. *Snapshot 2026-10-04:* 0.3.x, offline.
3. **An upload path on prod.** The dashboard's *Upload a build* form exists if
   `git merge-base --is-ancestor 392bf28 <prod commit>` holds (`392bf28` is R2b-fe-7; the prod
   commit is in `/healthz`). Otherwise use curl, from this box:
   ```bash
   BASE=https://bingo.tvaroska.sk
   read -rs FFPW                                   # the prod admin password, never in history
   TOKEN=$(curl -sSi -X POST "$BASE/v1/auth/login" -H 'content-type: application/json' \
           -d "{\"password\":\"$FFPW\"}" | grep -i '^set-cookie:' \
           | sed -E 's/.*ff_session=([^;]+).*/\1/' | tr -d '\r')
   curl -sS -X POST "$BASE/v1/artifact?target=esp32s3&version=0.4.5-rbtest" \
        -H "Authorization: Bearer $TOKEN" -H 'content-type: application/octet-stream' \
        --data-binary @/tmp/ff-bench-esp32s3-rbtest/app.bin
   #   {"sha256":"…","size_bytes":…,"target":"esp32s3","version":"0.4.5-rbtest",…,"created":true}
   ```
   `version` must be the bundle's own version (`verify_bundle.py` prints it). A 409 means that
   label already names different bytes: never reuse a label for new bytes.
   *Snapshot 2026-10-04:* prod is 0.4.2 / `9200e0f`, the form is **not** there; curl it is.
   The same curl was proven on the dev stack (see DECISIONS.md, R2b-test-2).
4. **The console and the hands.** PuTTY on COM3 at 115200, logging to a file. Close the
   dashboard's serial console first: one reader per port. USB in reach. A recovery path: the
   prod flasher (0.3.2 until `S0-infra-10`), or `esptool` with `agent/dist/esp32s3`.
5. **Radio setup, for step 5 only.** Easiest: a phone hotspot. Its hotspot switch is "AP off";
   its mobile-data switch is "WAN cut, AP still up". The board's `ff_cfg` must then carry the
   hotspot's SSID, which needs a re-flash, which re-enrols the board (a new device baseline).
   That is why the radio steps come last. The alternative is the home AP: its power plug is
   "AP off", its WAN cable is "WAN cut".

## The images

Four esp32s3 images, each built into a scratch directory and **never** into `agent/dist/`.
No just recipe builds them, on purpose (`tests/test_agent_fault_injection.py` fails the suite
if one does).

```bash
C=$(git rev-parse HEAD); IMG="$(just --evaluate idf_image)"
# the normal "bench" image: a copied context, so agent/version.txt never changes
rm -rf /tmp/ff-bench-ctx && cp -r agent /tmp/ff-bench-ctx \
  && rm -rf /tmp/ff-bench-ctx/dist /tmp/ff-bench-ctx/build /tmp/ff-bench-ctx/sdkconfig \
  && printf '0.4.5-bench\n' > /tmp/ff-bench-ctx/version.txt
for f in bench rbtest bltest hangtest; do
  ctx=agent; arg=
  case $f in bench) ctx=/tmp/ff-bench-ctx;; rbtest) arg='--build-arg FF_ROLLBACK_TEST=1';;
    bltest) arg='--build-arg FF_FAULT_TEST=bootloop';; hangtest) arg='--build-arg FF_FAULT_TEST=hang';; esac
  DOCKER_BUILDKIT=1 docker build --target export --output type=local,dest=/tmp/ff-bench-esp32s3-$f \
    --build-arg IDF_IMAGE="$IMG" --build-arg IDF_TARGET=esp32s3 --build-arg SOURCE_COMMIT=$C $arg $ctx
  python3 agent/tools/verify_bundle.py /tmp/ff-bench-esp32s3-$f     # "agent 0.4.5-<f>"
done
jq -r .config_sha256 /tmp/ff-bench-esp32s3-*/manifest.json agent/dist/esp32s3/manifest.json | sort -u
#   one line: every image carries the production bootloader posture
```

> **Never `just agent-publish` any of these, and never USB-flash a `-bltest`.** The flasher
> catalog is the onboarding path; a `-bltest` flashed over USB boot-loops with nothing to
> return to (rollback-test.md). `firmware/publish.py` refuses the three suffixes anyway.

Upload all four as artifacts (`esp32s3`, the version `verify_bundle.py` printed), plus `0.4.5`
itself (`agent/dist/esp32s3/app.bin`) if prod does not have it.

> **Trap:** `/tmp/ff-hang-esp32s3` on the dev box is `0.4.2-hangtest` (built at `5fa5c33`),
> the documented *negative control*: a pre-0.4.3 hang image never rolls back. Do not upload it
> for the positive run. The judge fails it by name.

*Snapshot 2026-10-04 (built at `1a00f7e1f00c`, idf v5.5.5, `/tmp/ff-bench-esp32s3-<f>` on the dev box):*

| Image | `verify_bundle.py` | app.bin bytes | app.bin sha256 |
|-------|--------------------|---------------|----------------|
| `bench` | agent 0.4.5-bench | 998672 | `fd43e6ff131c0ae47bf26b2c4f8e9ea3e60a3853aae3adead34d0dfe786f7c0a` |
| `rbtest` | agent 0.4.5-rbtest | 998816 | `2b2edd497a7ef8f2eb157ed6d33f88cdfb1fbeba7e90da2ae00f4db586ca98b6` |
| `bltest` | agent 0.4.5-bltest | 230000 | `c37b58f9419ea519db6e7d72c346d2ba960efced5743883d8cd536afae99f22b` |
| `hangtest` | agent 0.4.5-hangtest | 230032 | `8f7c55d67047c99a0e960fdcf8c8dc272fcbc964b95f0f1f074f8862113bc496` |

All four carry `config_sha256 d10f52d642b5…`, the same as `agent/dist/esp32s3` (0.4.5): the
production bootloader posture. `/tmp` does not survive a reboot; if they are gone, rebuild
with the loop above and compare (a different commit gives different hashes, which is fine as
long as `verify_bundle.py` and the `config_sha256` line hold).

## The session

Least destructive first; every step ends on a normal image. All deploys go through the
dashboard, which always sends `apply: auto` (on metal `esp_restart()` works, unlike QEMU, and
auto means a staged image applies itself, so no step is blocked by "an update is already staged
and waits for a reboot"). **Write down each step's cmd_id** (the deploy response, or the judge's
header line).

0. **Baseline.** Deploy `0.4.5-bench`.
   `just bench-judge confirmed 94a990dd09a4` -> `JUDGE PASS confirmed`.
1. **rbtest** (boots, joins, never confirms). Deploy `0.4.5-rbtest`. Hands off for 4 minutes.
   Console: `FF_ROLLBACK_TEST: ignoring the announce ack on purpose`, then
   `no working session 60 s after an OTA boot`, then the cold boot on the previous slot with
   `rolled_back (returned to ota_N; ota_M did not confirm)`.
   `just bench-judge rbtest 94a990dd09a4` -> `JUDGE PASS rbtest`. This is the first metal run
   with the R2-be-1 outcome rows (the 2026-09-23 metal run was an R1 agent).
2. **Power cut.** Deploy the normal version the board is *not* running (`0.4.5` or
   `0.4.5-bench`). Pull USB when the console prints `update <cmd>: 30%`; wait 10 s; replug.
   `just bench-judge power-cut 94a990dd09a4` -> `JUDGE PASS power-cut` (the transaction stays
   open on purpose). Then send the **same** version again within 30 minutes: the response says
   `reused: true` with the same cmd_id, and the board downloads again.
   `just bench-judge confirmed <cmd_id>` -> `JUDGE PASS confirmed`.
3. **Boot loop.** Deploy `0.4.5-bltest`. Console: `abort() was called` **exactly once**, then
   the bootloader loads the previous slot and `rolled_back (returned to ota_N; …)`.
   `just bench-judge bootloop 94a990dd09a4` -> `JUDGE PASS bootloop`.
4. **Hang before the session.** Deploy `0.4.5-hangtest`. Hands off for at least 330 s.
   Console: `OTA boot: 300 s from now to reach the fleet or roll back` before
   `ota state pending_verify`, the `FF_FAULT_TEST=hang` line every 30 s, then
   `no working session 300 s after an OTA boot`.
   `just bench-judge hang 94a990dd09a4` -> `JUDGE PASS hang` (it prints the gap, ≥ 300 s).
5. **Radio** (after the re-flash onto the hotspot, precondition 5). Each deploy is a normal
   image the board is not running.
   - R1: AP off for 30 s at `20%` -> `just bench-judge outage-short 94a990dd09a4`.
   - R2: AP off for 6 minutes at `20%` -> `just bench-judge stall 94a990dd09a4`. **Record which
     detail** it prints: `download failed` (keepalive/close got there first, the expected metal
     outcome) or `download stalled` (the R2-fw-5 guard). This is the one question only metal
     answers.
   - S: WAN cut with the AP up, at `20%`, for 6 minutes -> `stall` (see below).
   - R3: AP off at `rebooting`, back after 2 minutes -> `just bench-judge confirmed 94a990dd09a4`.
   - R4: AP off at `rebooting` for 6 minutes -> `just bench-judge reboot-outage-long 94a990dd09a4`.
     A good image rolled back is a miss, not a brick (DECISIONS 2026-10-03, R2-fw-4).

`INCOMPLETE` means the transaction is still open: wait and run the judge again. It is never a
pass, and `power-cut` never returns it.

### "Silent store" on metal, honestly

QEMU's D3/S1 shape, a far end that is alive but says nothing, cannot be produced at the bench
without a proxy in the board's path. The bench gets the two real-world shapes instead: the radio
gone (R2) and the far end gone with the radio up (S, the WAN cut). Either way the pass is that the
slot is freed (`failed` with `download stalled` or `download failed`) and the next deploy runs.
"Alive but silent" stays QEMU-only, and so does the pre-1 KB residual (ota-deploy.md, R2-fw-5
*Known residual*).

## What a failure means

- Still on `-rbtest` or `-hangtest` after the window, or a second `abort()` in step 3: a **P0
  Sprint 0 task**. This is CRITICAL.md's confirm/rollback path. Recover over USB.
- `stall` still `INCOMPLETE` ~100 s after the outage, with every new deploy answered `another
  update is already in progress`: the pre-0.4.4 regression. A Sprint 0 task.
- Any other `JUDGE FAIL`: a new Sprint 0 task (`/new-task`) carrying the judge output and the
  console lines.

## Record sheet

| Step | cmd_id | from -> to | Console key lines seen (Y/N each) | `JUDGE` line | Operator verdict | Notes |
|------|--------|-----------|-----------------------------------|--------------|------------------|-------|
| 0 baseline | | | | | | |
| 1 rbtest | | | | | | |
| 2 power cut | | | | | | |
| 2 re-deploy | | | | | | |
| 3 boot loop | | | | | | |
| 4 hang | | | | | | |
| 5 R1 | | | | | | |
| 5 R2 | | | | | | which detail: |
| 5 S | | | | | | which detail: |
| 5 R3 | | | | | | |
| 5 R4 | | | | | | |

Afterwards: write the results into the bench section of `docs/features/ota-deploy.md`; flip the
"bench replay owed" lines in rollback-test.md and TODO.md *Where this stands*; tick
`R2b-test-2`.
