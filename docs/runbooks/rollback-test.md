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

## Known gaps this test does not close

- **The server never learns.** At R1 the agent's reported walk ends at `rebooting`
  (`ff_ota.h`), so `confirming`/`confirmed`/`rolling_back`/`rolled_back` are never
  published. A rollback is therefore inferred from the announced version changing back,
  not read from a deploy state — and the deploy row stays non-terminal either way. R2's
  to fix.
- **One failure mode, not the family.** This covers "boots, joins, never confirms". It
  does not cover a boot loop, a brownout mid-write, or a flaky radio — the last of which
  is the separate R2 spike tracked in `docs/features/ota-deploy.md`.
