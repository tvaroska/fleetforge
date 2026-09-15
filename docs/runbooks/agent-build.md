# Runbook — agent firmware build

How the ESP32 agent binaries that the browser flasher writes to a board are produced,
proved and shipped. Everything here lives in `agent/` and in the `Agent firmware` section
of the `justfile`; the API side is `src/fleetforge/firmware/` + `/v1/agent/*`.

|  |  |
|---|---|
| Toolchain | `espressif/idf:v5.5.5`, **pinned by digest** (`sha256:a9231d06…65cf2`) |
| Targets | `esp32`, `esp32s3`, `esp32c3`, `esp32c6` (`agent_targets` in the justfile) |
| Output | `agent/dist/<target>/` — 4 binaries + `sdkconfig.resolved` + `manifest.json` |
| Consumed by | `COPY agent/dist /app/agent` in the app image → `AGENT_IMAGES_DIR` → `/v1/agent/*` |
| Registry | `us-central1-docker.pkg.dev/sites-470716/containers/fleetforge-agent-<target>` |

**Never run any of this on `prod`.** The production VM cannot hold a ~9 GB toolchain
image and a build there would starve the broker. This is a developer-box pipeline whose
*output* is baked into the app image.

## Build

```bash
just agent-build esp32        # one target, then verifies it
just agent-build-all          # all four, pruning build cache between them
just agent-verify esp32       # re-prove an existing bundle
just agent-clean              # delete bundles (keeps the ESP-IDF image)
```

A build takes a few minutes per target on a cold cache. `--output type=local` is what
writes `agent/dist/<target>/`: BuildKit exports the `FROM scratch` stage as the invoking
user, so nothing root-owned lands in the working tree (a bind-mounted `docker run` would).

`just agent-build` ends in `just agent-verify`, which is the gate. A build that prints
anything other than `BUNDLE OK: <target>` did not produce a flashable bundle.

A bundle that verifies is not yet a bundle that boots. **`docs/runbooks/agent-qemu.md`
runs this exact output in an emulator** — enroll, MQTT, announce, presence, heartbeat,
against the local stack and with no hardware. It is the cheapest way to find out that a
firmware change broke the first ten seconds, which is the part no unit test covers and no
OTA can repair.

## Disk is the number-one failure mode

`espressif/idf:v5.5.5` unpacks to **~8.9 GB**, and the pull needs headroom on top of
that. A pull that runs out of space fails ten minutes in with

```
failed to register layer: ... no space left on device
```

and leaves a partial image behind. Reclaim with exactly these, then check:

```bash
docker builder prune -af      # build cache only
docker container prune -f     # stopped containers
docker image prune -f         # DANGLING only
df -h /                       # want >= 12 G before the first pull
```

**Never `docker image prune -a`, `docker system prune -a` or `docker volume prune` on
this box.** It hosts other projects' images and their volumes; deleting them is not this
repo's call. If the three safe prunes are not enough, the next safe things are
regenerable caches that belong to nobody's data: `uv cache prune`, `npm cache clean
--force`, `go clean -modcache`, `sudo apt-get clean`,
`sudo journalctl --vacuum-size=100M`.

Between targets, each build leaves 150–300 MB of BuildKit cache — `agent-build-all`
prunes it in the loop, which on this box is the difference between four targets and two.

## What a bundle contains, and why

```
agent/dist/esp32/
  bootloader.bin          @ 0x1000   (0x0 on C3/C6/S3 — read the manifest, never assume)
  partition-table.bin     @ 0x8000
  ota-data-initial.bin    @ 0xF000
  app.bin                 @ 0x20000
  sdkconfig.resolved      the config the build ACTUALLY used
  manifest.json           offsets, sizes, sha256s, chip family, provenance
```

**Offsets come from ESP-IDF, never from this repo.** `agent/tools/make_manifest.py`
reads `build/flasher_args.json` by name (`bootloader`, `partition-table`, `otadata`,
`app`) and copies the offsets the toolchain computed. The bootloader offset genuinely
differs per chip (`0x1000` on ESP32, `0x0` on the RISC-V parts); a hardcoded value would
flash cleanly and never boot on half the fleet.

`manifest.json` also carries `idf_image` (the digest) and `source_commit`, so any bundle
on disk or in the registry can be traced back to the exact toolchain and tree.
`source_commit` is `git rev-parse HEAD` — a bundle built from a **dirty** working tree
records HEAD, not what was compiled. Commit before building anything you intend to push.

## What `just agent-verify` proves

`agent/tools/verify_bundle.py` plus one ESP-IDF invocation:

1. Every part re-hashed and re-sized against the manifest.
2. Every manifest `path` is a bare filename inside the bundle.
3. `app.bin` fits `ota_slot_size` (1 966 080 bytes).
4. `sdkconfig.resolved` has `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`,
   `CONFIG_MBEDTLS_CERTIFICATE_BUNDLE=y` and `CONFIG_MBEDTLS_HAVE_TIME_DATE=y` — a
   compiled-in trust store, and certificate *dates* actually checked (IDF's default is
   not to check them, which silently accepts an expired server certificate forever).
5. `sdkconfig.resolved` has **none** of the irreversible options enabled —
   anti-rollback, secure boot, flash encryption. These burn eFuses; a board that
   ships with one enabled by accident cannot be un-shipped.
6. `partition-table.bin` decoded by IDF's own `gen_esp32part.py` — the table the
   bootloader will actually read, not the CSV that was the input to it.

Expected decode, identical for every target:

```
nvs,data,nvs,0x9000,24K,
otadata,data,ota,0xf000,8K,
phy_init,data,phy,0x11000,4K,
ff_cfg,data,64,0x12000,4K,
ota_0,app,ota_0,0x20000,1920K,
ota_1,app,ota_1,0x200000,1920K,
```

`1920K == 0x1E0000 == 1966080` is the `ota_slot_size` `spec/device-protocol.md` promises
in `up/announce`, and the layout id is `ab-4m-v1`. **Those three facts move together or
not at all** — see *Changing the partition table* below.

## Staleness — a bundle can be correct and still be wrong

`just agent-verify` proves the bundle is correct; `just agent-check-fresh` proves it is
**current**. v0.3.0 shipped three targets (esp32c3, esp32c6, esp32s3) that predated
S0-fw-1 (the stage reporter) — not because anyone edited the code and forgot to rebuild,
but because nothing checked. A bundle built from a commit that predates the agent sources
is stale.

```bash
just agent-check-fresh    # exit 0 only if every target is fresh
```

Four verdicts, decided by **git ancestry, not mtime**:

| Verdict | Meaning |
|---|---|
| **fresh** | bundle built from a commit containing the newest agent source change |
| **STALE** | bundle predates a later commit under `agent/` (excluding `agent/dist`) |
| **UNTRACEABLE** | `source_commit` absent/unknown, or not a commit in this repo |
| **NOT BUILT** | no `manifest.json` — run `just agent-build <target>` |
| **DIRTY SOURCES** | uncommitted edits under `agent/` — a bundle records HEAD, not what was compiled |

Why git ancestry, not mtime: `git checkout`, `git pull` and branch switches rewrite
source mtimes with no content change; a clone sets them all to clone time. A check that
fires on a correct tree is the check people delete.

**The dirty-tree refusal lives in the release path only** (`just build` runs
`agent-check-fresh`). The firmware dev loop (edit → `just agent-build` → QEMU → commit)
deliberately allows a dirty build — condition 4 exists so that dirty build never
reaches an image.

When bundles move to the object store (DECISIONS.md 2026-09-11, "agent bundles are
artifacts"), this check moves to the publish step and validates the one bundle being
uploaded. The script is sited for exactly that re-point.

> Note the check on (5) matches **exact option names**. An earlier prefix match also hit
> `CONFIG_SECURE_BOOT_V1_SUPPORTED=y`, which is a SoC *capability* symbol present in every
> ESP32 build — a check that fails on a correct build teaches whoever hits it to delete
> the check.

## Serving them

The API loads `AGENT_IMAGES_DIR` **once at startup** and re-hashes every part while
loading; a bundle whose bytes disagree with its manifest, or whose
`partition_layout`/`ota_slot_size` disagree with the spec, is dropped with a WARNING
naming the target. One bad target does not stop the other three from serving.

### Catalog key and directory convention (S0-infra-7)

The catalog is keyed on `(target, partition_layout)`. A bundle directory is `<target>` or
`<target>.<layout>`:

* `agent/dist/esp32/` — the bare target form, `partition_layout = ab-4m-v1`
* `agent/dist/esp32.ab-8m-v1/` — the suffixed form, for a second layout

The dot-suffixed form is how two layouts for one chip coexist; `just agent-build` still
writes the bare `<target>` form and is unchanged. The separator is `.` because no chip
target and no layout id contains one (both match `SAFE_SEGMENT`: lowercase alnum and `-`),
so the split is unambiguous — `esp32-ab-8m-v1` would not be.

On the wire, `GET /v1/agent/{target}/{part}?layout=` selects which partition_layout when
more than one exists for a target:

* 404 for an unknown target, unknown layout or unknown part (indistinguishable: the
  404-for-everything rule means a probe learns nothing)
* 409 `"target 'esp32' has bundles for layouts ab-4m-v1, ab-8m-v1; name one with ?layout="`
  when the caller omits the parameter and more than one layout exists
* No layout specified and exactly one exists → resolves, for backward compatibility with
  the R0 case

**Adding a layout** means a `SUPPORTED_LAYOUTS` entry in `src/fleetforge/firmware/manifest.py`,
a `spec/device-protocol.md` change documenting it, and a new `agent/partitions.csv` id.
A new layout is **never** an edit to an existing row (DECISIONS.md 2026-09-09).

| Shape | Where the bytes come from |
|---|---|
| `just up` (dev) | bind mount `./agent/dist:/app/agent:ro` — **restart the api** after a rebuild; `--reload` only watches `src/` |
| `just up-prod`, production | baked by `COPY agent/dist /app/agent` |
| no bundles | startup WARNING + `/v1/agent/*` answers `503 {"detail":"no agent images available"}` |

`just build` (the app-image pipeline) runs **two gates** before anything is built:
`_require-agent-dist` (at least one manifest exists) and `agent-check-fresh` (every
bundle is current). An image with an empty flasher or with stale bundles cannot be
released by accident. On a clean clone `agent/dist/` holds only `.gitkeep` — run
`just agent-build-all` before `just build`.

## Pushing to Artifact Registry

```bash
just agent-image esp32        # build the OCI image, nothing leaves the box
just agent-push esp32         # push :<latest git tag> and :latest
```

The pushed image is the `FROM scratch` export stage: its entire payload is the bundle, so
it is kilobytes in the registry and its digest is a provenance handle. Extract one with

```bash
REPO=us-central1-docker.pkg.dev/sites-470716/containers/fleetforge-agent-esp32
# The placeholder command is REQUIRED: the image is FROM scratch with no CMD, and
# `docker create` refuses with "no command specified". Nothing is ever run.
CID=$(docker create $REPO:latest /nonexistent)
docker export "$CID" | tar -x -C /some/dir
docker rm "$CID"
```

`docker export` also emits the container-runtime stubs (`.dockerenv`, `dev/`, `etc/`,
`proc/`, `sys/`); the bundle is the six files beside them.

### Reproducibility — what "identical" means here

A pull of a pushed digest gives back the bundle **byte for byte**; that has been verified
end to end (push → `docker rmi` → pull by digest → export → `diff -r`, no differences).

**Rebuilding the same commit does not.** ESP-IDF stamps the compile date and time into
`esp_app_desc_t`, so `app.bin` and `bootloader.bin` get a new sha256 on every build even
with the toolchain pinned by digest and no source change — only `partition-table.bin` and
`ota-data-initial.bin` are stable. That is why provenance lives in the manifest
(`idf_image`, `source_commit`, `built_at`) rather than in a hash comparison, and why the
digest of a pushed `fleetforge-agent-*` image is the thing to quote.

Making rebuilds byte-identical would mean `CONFIG_APP_REPRODUCIBLE_BUILD=y`, which
changes every produced binary — a `sdkconfig.defaults` change, i.e. a `DECISIONS.md`
entry and a re-verify of all four targets. Worth doing before the fleet is large; not
done at R0.

**These are not compose services.** Never add `fleetforge-agent-*` to `PULL_SERVICES` or
`APP_SERVICES` in `services/scripts/deploy.sh`: `docker compose pull` fails as a unit, so
one unresolvable reference breaks the deploy for every other app on the box. Production
gets the binaries inside the app image, not from these.

## Bumping the ESP-IDF pin

The pin is a **digest**; the `v5.5.5` tag beside it is documentation. Changing it changes
the bootloader and the app on every board flashed afterwards, and boards already in the
field keep the old one — so:

1. Add a `DECISIONS.md` entry (what moved, why, what was re-verified). It is not a
   version bump.
2. Update `idf_image` in the `justfile` **and** the `IDF_IMAGE` default in
   `agent/Dockerfile` — they must not drift.
3. `just agent-clean && just agent-build-all` and read every `BUNDLE OK`.
4. Re-read the decoded partition table: it must be byte-for-byte the table above.
5. Diff `sdkconfig.resolved` against the previous bundle. An IDF minor release can
   change a default; the rollback option and the three forbidden postures are what
   matter, and `verify_bundle.py` fails the build if they moved.

## Changing the partition table — read this first

`agent/partitions.csv` is in `CRITICAL.md` for a reason: **a partition table cannot be
changed by OTA.** A board already in the field keeps the layout it was flashed with
forever, so a change here means a physical recall.

Three things are one contract and move together:

* `agent/partitions.csv` — `ota_0`/`ota_1` sized `0x1E0000`,
* `spec/device-protocol.md` — `"ota_slot_size": 1966080`, `"partition_layout":
  "ab-4m-v1"`,
* `tests/test_agent_partitions.py` — which retypes both literally and fails if either
  moves alone.

A new layout is a **new id** (`ab-4m-v2`, …) plus a server that understands both, never an
edit to `ab-4m-v1`. There is deliberately no `factory` partition: a factory-only board
can never OTA its way to A/B. `make_manifest.py` refuses to emit a bundle whose built
table has one, is missing `ota_1`, or whose slots differ in size.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `no space left on device` during pull | see *Disk* above; the partial image is discarded, re-pull after reclaiming |
| `make_manifest: build/config/sdkconfig is missing` | IDF 5.x keeps the text config at the project root; it is resolved from `project_description.json["config_file"]` — do not hardcode a path |
| Build succeeds but the bundle is stale | a `sdkconfig` at the project root **overrides** `sdkconfig.defaults`; it is gitignored and dockerignored, but check the build context |
| `/v1/agent/manifest` is 503 with bundles on disk | the api loads them at startup — restart it; then read the WARNING, which names the path and the dropped targets |
| A target is missing from the manifest | it was dropped at load: the WARNING names it and the reason (usually a partial `agent-build-all` after a disk failure) |
