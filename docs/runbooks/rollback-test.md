# Proving the confirm/rollback pair on real hardware

The live test of `agent/main/ff_mqtt.c`'s confirm/rollback pair — CRITICAL.md's
*"Device-side confirm timer / rollback path"*, described there as **"the whole bricking
gamble. A bug here means a board that cannot recover itself — the one failure the product
must never have."**

**Run this at the bench, with USB in reach.** Its failure mode is a board that does not
come back, and the only recovery is a serial re-flash. That is not a reason to skip it;
it is the reason to do it while you can still recover, because the alternative is
discovering the same thing remotely on a board you cannot reach.

## Why it needs a special build

Both halves of the pair are guarded by `ESP_OTA_IMG_PENDING_VERIFY`, which only an image
written **by OTA** ever enters. Every serially flashed board boots `ota_0` in state
`UNDEFINED` with no rollback timer armed, so on a bench board the whole mechanism is
inert — you cannot provoke it by rebooting, power-cycling or re-flashing.

The positive branch (`confirm_this_image()`) first ran in production on 2026-09-23, when
`R1-test-1` deployed to `94a990dd09a4`. The negative branch — `confirm_timeout_cb()` →
`esp_ota_mark_app_invalid_rollback_and_reboot()` — is what this runbook exists to
exercise. Do **not** try to provoke it by forcing the partition state by hand; that tests
a state the bootloader never produces.

## The injected fault

`FF_ROLLBACK_TEST=1` is a build flag, OFF by default and absent from normal builds
(`agent/CMakeLists.txt`, `agent/main/CMakeLists.txt`, `agent/Dockerfile`). It changes two
things and nothing else:

1. **The announce ack is discarded.** `ctx->announce_msg_id = -1` after the publish, so
   the PUBACK can never match and `session_confirmed` stays false. The announce itself is
   still published and still retained — the board genuinely joins the fleet on the bad
   version, which is what makes the rollback visible in the dashboard rather than only on
   a console.
2. **`CONFIRM_TIMEOUT_S` drops 300 → 60.** This does not weaken the test: the timer under
   test is the one compiled into the *new* image, so the mechanism is identical and only
   the wait is shorter.

The flag also appends `-rbtest` to `PROJECT_VER`, which travels through
`esp_app_get_description()->version` into every `up/announce`. One switch, so there is no
half-configured state, and a test image cannot be mistaken for a shippable one.

The resolved sdkconfig is **byte-identical** to a normal build (`config_sha256` matches),
so the bootloader posture — `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`, no eFuse burns — is
the production one. That is what makes the result transferable to the fleet.

## Procedure

Build to a scratch directory, **not** `agent/dist/`, so the real bundle stays publishable:

```bash
DOCKER_BUILDKIT=1 docker build \
  --target export --output type=local,dest=/tmp/rbtest-esp32s3 \
  --build-arg IDF_IMAGE="$(just --evaluate idf_image)" \
  --build-arg IDF_TARGET=esp32s3 \
  --build-arg SOURCE_COMMIT=$(git rev-parse HEAD) \
  --build-arg FF_ROLLBACK_TEST=1 \
  agent

python3 agent/tools/verify_bundle.py /tmp/rbtest-esp32s3   # expect "agent <ver>-rbtest"
```

Upload as a deployable **artifact** only:

```bash
FF_TARGET=esp32s3 FF_BIN=/tmp/rbtest-esp32s3/app.bin \
  FF_VERSION=<version>-rbtest docs/runbooks/upload-artifact.sh
```

> **Never `just agent-publish` a rollback-test build.** That writes the flasher catalog,
> which is the USB onboarding path — it would make a deliberately broken image the one
> every new board gets flashed with. The artifact store and the flasher catalog are
> different stores for exactly this reason (`artifact-storage.md`).

Then deploy it to the board from the dashboard, and watch.

## What a pass looks like

```
t+0      deploy <version>-rbtest
t+~15s   downloading -> rebooting
t+~25s   board returns announcing <version>-rbtest   <- bad image live, PENDING_VERIFY
t+~85s   no acked session -> mark_app_invalid_rollback_and_reboot()
t+~95s   board returns announcing the PREVIOUS version
```

**Pass = the board comes home on the previous version, unattended.** Nobody touches it.

Still on `-rbtest` after ~2 minutes means the negative branch did not fire, and the board
is running an image the bootloader was waiting on. Recover over USB and treat it as a P0:
until it is fixed, no board can be deployed to unless someone can physically reach it.

## Boot loop (R2-test-1)

The second failure mode: an image that panics on **every** boot, before any of our code
runs. Nothing of ours takes part in this recovery. The bootloader turned the OTA'd image
NEW → PENDING_VERIFY on its first boot, and the reset the panic causes makes it mark the
slot ABORTED and load the previous one. Proven in QEMU (esp32): exactly one `abort()`, then
every bootloader load names the old slot, and the old image reports `rolled_back`
(`docs/features/ota-deploy.md` → *Remaining failure modes*). **The bench replay is owed.**

Build `FF_FAULT_TEST=bootloop` to a scratch directory (same as above, with
`--build-arg FF_FAULT_TEST=bootloop` in place of `FF_ROLLBACK_TEST=1`; the two are
exclusive and the build refuses both together). `verify_bundle.py` must say
`agent <ver>-bltest`, and `config_sha256` must equal the normal bundle's. The app is about
150 KB, not 1 MB: everything after the `abort()` is dead code and the linker drops it.
Upload it as an artifact only, then deploy it with the default `apply: auto`.

> **Never `just agent-publish` a `-bltest` build, and it is worse than `-rbtest`.** A
> serially flashed image boots UNDEFINED with no rollback armed. A `-rbtest` image flashed
> over USB merely never confirms, which is harmless when nothing is waiting on it. A
> `-bltest` image flashed over USB aborts on every boot, **forever**, and the only way out
> is another serial flash. `firmware/publish.py` refuses all three suffixes (`-rbtest`,
> `-bltest`, `-hangtest`) before it writes anything.

Pass, unattended:

- the console (PuTTY on COM3, 115200) shows `FF_FAULT_TEST=bootloop` and
  `abort() was called` **exactly once**, after `ota state pending_verify`;
- the next boot is the previous slot and version, and logs
  `transaction <cmd>: rolled_back (returned to ota_N; ota_M did not confirm)`;
- the dashboard says `rolled back`, and the rows are `… staged, applying, rebooting,
  rolled_back`. There is **no** `confirming`: the bad image never reached the broker.

A second `abort()` line means the bootloader booted the bad slot twice. That is a P0.

## Pull the plug mid-download (R2-test-1)

The third: power lost while the inactive slot is half-written. Since R2-fw-1 the boot
pointer moves only after the sha256 read-back, so a cut anywhere before that leaves the
boot pointer where it was. Proven in QEMU (a SIGKILL at `30%` and again during
`verifying`). **The bench replay is owed.**

1. PuTTY on COM3 at 115200, then deploy any normal version.
2. Pull the USB cable when the console prints `update <cmd>: 30%`, then plug it back.
3. Pass: the board boots the previous slot and version, `ota state valid`, and logs no
   `transaction` line at all. The server row stays at `downloading` (non-terminal, by
   design: `deploys.py` rule 1).
4. Re-deploy the same version. Within the signed-URL TTL (1800 s) the API answers
   `reused: true` with the **same** `cmd_id`. The board downloads again (its dedupe is RAM
   only and died with the power), stages, reboots and reports `confirmed`.

`otadata` is not strictly untouched by a stage that dies early: IDF's `esp_ota_begin()`
erases the **inactive** sector before the first byte when that sector names a slot other
than the running one (`esp_ota_invalidate_inactive_ota_data_slot`). The active sector, the
one that names the running image, is byte-identical. QEMU showed exactly that.

**Transition gap.** Prod's board runs 0.3.1. It must first be taken to ≥ 0.4.2 with a normal
deploy, which parks at `rebooting` because 0.3.1 never records the transaction (the R2-be-1
entry in DECISIONS.md). Only the deploys after that one run these procedures.

## Hang before the session (R2-fw-4)

The fourth: an OTA'd image that never reaches its broker session — a network that never
comes up, an enrollment that retries forever, a `park()`. Since agent 0.4.3 the confirm
timer is the first thing `app_main` does, so such an image rolls itself back 300 s after it
started executing, unattended. Proven in QEMU (esp32): the rollback line at a log timestamp
of 302 s, otadata `INVALID` (our timer, not a reset) and `rolled_back` from the old image
(`docs/features/ota-deploy.md` → *Arm the confirm timer at boot*). **The bench replay is
owed.**

Build `FF_FAULT_TEST=hang` to a scratch directory (as for the boot loop, with
`--build-arg FF_FAULT_TEST=hang`), from code ≥ 0.4.3 — the timer that matters is the one
compiled into the image being deployed. `verify_bundle.py` must say `agent <ver>-hangtest`.
Upload it as an artifact only (`firmware/publish.py` refuses `-hangtest`). Deploy it with
`apply: "on_command"`, and at `staged` power-cycle the board yourself (the QEMU recipe;
the default `apply: auto` works on the bench too, and adds `applying, rebooting` to the
rows).

Pass, unattended (no human action after the power cycle):

- the console shows `ff-mqtt: OTA boot: 300 s from now …` **before** `ota state
  pending_verify`, then `FF_FAULT_TEST=hang` every 30 s;
- at about 300 s of uptime: `no working session 300 s after an OTA boot — marking this image
  invalid and rolling back`, then a reset;
- the next boot is the previous slot and version, and logs `transaction <cmd>: rolled_back
  (returned to ota_N; ota_M did not confirm)`;
- the dashboard says `rolled back`; the rows end `… verifying, staged, rolled_back`,
  with **no** `confirming` and no `rolling_back` (no session ever existed).

Still on `-hangtest` 330 s after the power cycle is the original defect, and a P0. A
`-hangtest` built from code before 0.4.3 (e.g. the dev catalog's `0.4.22-hangtest`) is the
negative control: it never rolls back by itself.

## Marginal radio (R2-test-2)

The fifth: a link that is slow, drops out mid-download, or drops out right after the
reboot. Proven in QEMU through a host-side proxy (`docs/runbooks/agent-qemu.md` →
*Driving a flaky link*). Results: `docs/features/ota-deploy.md` → *Flaky link
(R2-test-2)*. **The bench replay is owed.** QEMU's limit is the reason it is owed: slirp
answers the board's TCP whatever the proxy does, so every QEMU outage is "the far end is
alive but silent". A real radio that is gone is a different picture.

Set-up: the S3 at the edge of the AP's range, or the AP's TX power turned down, or a phone
hotspot walked away until the RSSI in `up/hb` is marginal. PuTTY on COM3 at 115200. Use a
normal artifact, and note the cmd_id of each deploy.

1. **Outage mid-download, short.** Deploy with `apply: "on_command"`. At `update <cmd>:
   20%`, pull the AP's power for 60 s, then restore it. Pass: progress resumes and reaches
   `staged`, or the deploy ends `failed` / `download failed` and the board stays on its
   image. Either way the board is on a VALID image, and a re-deploy works.
2. **Outage mid-download, long.** As 1, with the AP off for 6 min. Pass: the board ends
   `failed` / `download failed` (TCP keepalive ended it, the D4 path) and a re-deploy works.
   **The P0 for R2-fw-5 is the other outcome:** the row parks at `downloading`, no
   `failed` for ≥ 5 min after the AP is back, and every new deploy gets `another update is
   already in progress` until a power cycle (the D3 path).
3. **Outage after the reboot, short.** Deploy with `apply: "auto"`. Kill the AP the moment
   the console prints `rebooting`, and bring it back after 2 min. Pass: `OTA boot: 300 s …`,
   then `confirming`, `CONFIRMED`, rows `… confirming, confirmed`.
4. **Outage after the reboot, long.** As 3, with the AP off for 6 min. Pass: `no working
   session 300 s after an OTA boot` at ≈ 300 s of uptime, then a reset onto the previous
   slot. Once the AP is back: `rolled_back`, and the dashboard shows the old version. A good
   image rolled back is a miss, not a brick (DECISIONS 2026-10-03, R2-fw-4, change 2).

**The one question only metal answers** (step 2): when the radio is really gone, does the
download end by itself through TCP keepalive (`keep_alive_enable`, IDF defaults 5 s idle,
5 s interval, 3 probes: `download failed` after ~20 s with no ACKs), or does it sit in the
EAGAIN loop QEMU showed for 600 s? The answer decides whether R2-fw-5 is a QEMU-only
"silent peer" fix or a field fix.

## Known gaps this test does not close

- ~~**The server never learns.**~~ **Closed for R2→R2 deploys (R2-be-1, agent 0.4.0).**
  The agent now records the transaction at `staged`, and reports the outcome after the
  reboot. For an rbtest run, expect these `deploy_events` rows for the cmd_id:
  `… staged, applying, rebooting, confirming, rolling_back, rolled_back`. Only
  `rolled_back` is terminal, and its `detail` names the slots (`returned to ota_1; ota_0
  did not confirm`). `rolling_back` comes from the rbtest image ~2 s before its reboot, and
  it is best-effort. `rolled_back` comes from the image the board returned to.
  **Transition gap:** this needs agent 0.4.0 or later on **both** sides of the
  transaction. A board still running an R1 agent (≤ 0.3.x, e.g. prod's 0.3.1) neither
  records the deploy nor reports a rollback *to* itself. For that board this gap is still
  open, and the rollback is still only inferable from the announced version.
- **The family, as of R2-test-1.** "Boots, joins, never confirms" is this runbook's first
  procedure. A **boot loop** and a **power cut mid-download** are the two above: proven in
  QEMU, bench replay owed. A power cut **inside an otadata write** lands on the previous
  image, but it is QEMU-only (an offline tear, `just agent-qemu-otadata`) and the outcome
  is never reported. **A hang before the broker session is covered from agent 0.4.3
  (R2-fw-4): proven in QEMU, bench replay owed** (*Hang before the session* above). Both
  the image being deployed and the image being replaced must be ≥ 0.4.3 to count on it.
  Strictly, the hang image must be ≥ 0.4.3 to roll back at all, and the image it returns
  to must be ≥ 0.4.0 to report `rolled_back`. An image that boots, confirms and is broken
  anyway is still not covered: deploy one board at a time with USB in reach.
- **A flaky radio, as of R2-test-2: covered in QEMU, with its limit; bench replay owed**
  (*Marginal radio* above). The download and the confirm timer never overlap, so a slow or
  stalled download cannot trip the timer. An outage after the reboot that outlasts the
  300 s rolls a good image back (a miss). A far end that goes silent mid-download holds the
  update slot until a power cycle (R2-fw-5). QEMU cannot say whether a really dead radio
  ends the download by keepalive instead.
