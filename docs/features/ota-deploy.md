# OTA Deploy & Auto-Rollback

**Priority:** P0
**Target:** R1 (deploy), R2 (safe deploy ⭐)
**Depends on:** Enrollment (enrollment.md) — R0
**Flow:** [flows.md](../../spec/flows.md) → Flow 2

## Overview

Push new firmware to a registered board from the dashboard (R1), then make it **safe**:
a bad build triggers a catch and any device that gets one **recovers itself** via A/B slot +
auto-rollback (R2). R2 is the single most important milestone — the whole gamble.

Artifacts are **opaque + versioned**. The server stores/targets/tracks but never parses
them. Users build the `.bin` with their own toolchain (idf.py / PlatformIO / Arduino) —
the server never builds.

Size limits, deploy-duration targets and the confirm-timeout default:
[prd.md](../../spec/prd.md) → *Requirements & targets*. Channel split and signed URLs:
[design/architecture.md](../../design/architecture.md) → *Transport*.

**Storage is already in place** (R0-be-6). `fleetforge.storage` is the
`put`/`get`/`signed_url`/`delete` seam that R1-BE-1 uploads into and R1-BE-2 hands to a
device as a short-lived signed URL — MinIO in dev, GCS in production, chosen by
configuration. See *Artifact object store (R0-be-6)* below.

## Phase 0: R0 — where the bytes live

### Artifact object store (R0-be-6)

`fleetforge.storage` is the third adapter seam in the codebase, the same shape as
`fleetforge.broker`: one `ObjectStore` `Protocol` with four verbs (`put`, `get`,
`signed_url`, `delete`), an error taxonomy that decides the caller's HTTP status, two
real adapters in sibling modules (`storage/s3.py` → MinIO and any S3, `storage/gcs.py`
→ `gs://btvaroska/fleetforge/`), and selection in `storage/factory.py` behind
`api/deps.get_object_store`. Nothing in R0 calls it. R1-BE-1 (upload), R1-BE-2 (`stage`
carrying a URL) and R2's pruning all do, and getting artifact-URL authorization wrong is
cheapest to fix before any of them exist.

Both SDKs are imported **inside** the factory branch that needs them and every network
call runs in `asyncio.to_thread` under an `asyncio.timeout`. The SDKs are blocking, and
neither `aioboto3` nor `gcloud-aio-storage` earns a dependency for a path that runs a
handful of times per deploy. `signed_url` is `async def` anyway, even though V4 signing
is local CPU. Thus, a future IAM-`signBlob` backend is not a Protocol change.

**There are four verbs and no `list`**. `list` is also the one verb an IAM prefix
condition cannot constrain, so adding it would silently widen the production grant.

**The prefix applies only twice, independently**. `gs://btvaroska` is a *shared* bucket (it holds this estate's `.env` backups under `secrets/` and the boris podcast audio) and
object keys arrive from an HTTP request body. So `storage/objectstore.resolve_key()`
**rejects and never fixes** (`..`, a leading `/`, `//`, backslashes, control or
non-ASCII bytes, `?`/`#`, over 512 characters), the rule `identity.py` established for
device IDs and for the same reason. A normalized key is a string two readers can read
differently. Independently, the production service account holds `objectAdmin` under an
IAM condition on `…/objects/fleetforge/…`. Either alone is one bug away from writing
next to `secrets/`. The condition is also what makes a signed URL for an out-of-prefix
object worthless, since GCS evaluates the *signer's* permissions at redemption. A bad
key raises `ObjectKeyError`, which is a `ValueError` and deliberately not an
`ObjectStoreError`. The caller is wrong, so it is a 4xx, and retrying it is pointless.

**A presigned S3 URL signs the `Host` header, so there are two S3 endpoints**.
`S3_ENDPOINT_URL` (`minio:9000`) is what the API reads and writes through.
`S3_PUBLIC_ENDPOINT_URL` (`localhost:9000` in dev) is what URLs are *signed against*.
This is because the device is not on the compose network and rewriting the host after signing
invalidates the signature. There is no post-hoc fix. That is why the split exists at
signing time and why a unit test asserts the generated URL's host. The failure works
perfectly from inside the network and only appears on a real board.

**The GCS half was never round-tripped against the real service**. `btvaroska`
inherits `constraints/iam.disableServiceAccountKeyCreation`. Thus, the service-account key
the adapter requires has no mint path. The service account and its conditional binding
exist, the credential does not. The adapter deliberately has **no ADC fallback** —
Application Default Credentials on a GCE VM carry no private key (so no V4 signing) and
resolve to the project-wide compute default SA, the exact credential the prefix
condition exists to prevent. Thus, a missing key file fails loudly at construction. Closing
this is a prerequisite for R1. The two options (impersonation + `signBlob`, or an org
policy exemption) are in
[runbooks/artifact-storage.md](../runbooks/artifact-storage.md).

Operationally: `python -m fleetforge.storage selftest` (`just storage-check`) round-trips
whichever backend the environment selects and prints the bucket and prefix but never a
credential. Unconfigured storage is one startup WARNING plus a 503 at use time, never a
crash (`create_app()` stays constructible with no environment at all) and both
backends configured at once triggers a refusal rather than resolved by a precedence rule.

## The update transaction (4-verb contract, from design/architecture.md)

`stage → apply → confirm → rollback` — server orchestrates, never knows *how* **nor when**.

**Two authority rules, both device-side:**
- **The device owns the reboot**. `stage` delivers and checks. The device applies only
  in a self-declared safe window and can sit in `awaiting_safe_window` indefinitely — a
  vehicle in motion or an airborne drone must not reboot on the server's schedule.
  Rollback reboots obey the same rule.
- **The device owns the rollback**. The agent arms the confirm timer on the device before the
  reboot. A board that cannot reach the broker is exactly the board that must roll back.
  It will never receive a server command saying so. The server observes and records.
  It never triggers a rollback.

ESP32 adapter: write OTA1 partition → broker reconnect + self-test → switch to OTA0.

## Phase 1: R1 — Upload new code (OTA deploy)

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R1-BE-1 | Artifact upload `POST /v1/artifact` (opaque blob + version + platform_type). Reject anything over the target layout's `ota_slot_size` | P0 | 1d |
| R1-BE-2 | Deploy orchestration: `stage → apply` (per-device), carrying a short-lived signed artifact URL | P0 | 1.5d |
| R1-BE-3 | Artifact download endpoint: signed-URL verification + HTTP range support | P0 | 1d |
| R1-BE-4 | Write every deploy outcome to `deploy_events` — the KPI history R6 computes from | P0 | 0.5d |
| R1-FW-1 | Agent gains `esp_https_ota` + "update" command handler | P0 | 2d |
| R1-FW-2 | Agent reports firmware version after reboot | P0 | 0.5d |
| R1-FE-1 | Per-device Deploy button + version-change feedback | P0 | 1d |
| R1-TEST-1 | E2E: push firmware → board version changes in dashboard | P0 | 1d |

> ⚠️ Not yet safe — a broken build stays broken until R2.

> **R1-TEST-1 unblocked, 2026-09-22**. It depended in practice on `R0-test-2` — a board
> that cannot enroll cannot be deployed to. That passed on 2026-09-19: device
> `94a990dd09a4`, an ESP32-S3 running agent 0.2.0 (see [enrollment.md](enrollment.md) →
> *E2E on real hardware*). Every other R1 task landed, so this is the only thing
> between R1 and done.
> Two practical notes. The board is **not currently online** — `presence_reported = f`,
> `last_seen` 2026-09-19 — so re-plug and let it re-announce before deploying. And its
> `ota_slot_size` is 1966080 with layout `ab-4m-v1`, matching the frozen contract. Thus, the
> 1.9 MB artifact cap applies as written. Deploy only to a board you can physically reach:
> there is no checksum gate and no confirm timer until R2.

### R1-BE-0 — a production GCS credential that is not a key file — **LANDED 2026-09-15**

**Delivered by S0-infra-5**. R1 no longer needs to solve this. Read the answers below
rather than re-deriving the question.

* **The credential is `GCS_IMPERSONATE_SERVICE_ACCOUNT`**, an impersonation over the
  runtime's ADC targeting `fleetforge-artifacts@btvaroska.iam.gserviceaccount.com`.
  Mutually exclusive with `GCS_CREDENTIALS_FILE`. Neither set is still a refusal. Thus, there
  is no silent ADC fallback.
* **`signBlob` WORKS, measured not assumed**. `just storage-check --backend gcs --blob`
  ends `SELFTEST OK` against `gs://btvaroska` with no key file anywhere: the V4 URL carries
  `X-Goog-Credential=fleetforge-artifacts@…` and an unauthenticated GET returns the bytes.
  There is no private key in the process. Thus, the signature can only came from the IAM
  API. **R1-BE-3's signed-URL delivery rests on a checked mechanism.**
* **Containment is real**, measured through the adapter with `GCS_PREFIX=` empty. A `put`
  to `secrets/…` fails `Forbidden` from the IAM condition alone.
* **Signing is a network call now. It is on R1's latency budget**. `signed_url` runs in
  a thread under `OBJECT_STORE_TIMEOUT_S` (the adapter cannot tell a key file from an
  impersonation, so there is one path). One extra Google round trip per URL handed to a
  device, and it can rate-limit. If R1-BE-3 issues URLs per range request, cache them.
* **Still owed:** the same selftest **from the prod container**. Prod's identity
  `mainsite@sites-470716` holds the tokenCreator grant. Thus, it must pass, but its
  metadata server and egress are its own. S0-infra-6 wires the container and runs it.

Original filing, 2026-09-11, kept for the reasoning.

`storage/factory.py` requires `GCS_CREDENTIALS_FILE` and never reverts, but
`btvaroska` inherits `constraints/iam.disableServiceAccountKeyCreation` and will not
issue a key. GCS has thus **never been round-tripped against the real service** —
`R0-be-6`'s T2 AC5/AC6 are unexecuted. The adapter's GCS path is exercised only by
unit tests. A green dev stack proves MinIO, not production.

Add `GCS_IMPERSONATE_SERVICE_ACCOUNT` to `storage/factory.py`, mutually exclusive with
`GCS_CREDENTIALS_FILE` so there is still no silent ADC fallback, and grant prod's
`mainsite@sites-470716` the role `roles/iam.serviceAccountTokenCreator` on
`fleetforge-artifacts@btvaroska`. V4 signing then routes through IAM `signBlob`.
`impersonated_credentials.Credentials` is a `Signer`, so `generate_signed_url` stays the same.

**Impersonation is necessary for containment, not just for signing**. Prod's attached
identity is the estate's shared VM service account and can read all of `gs://btvaroska`
including `secrets/` — so plain ADC would hand fleetforge every other app's secrets.
Impersonating `fleetforge-artifacts` is what keeps the existing prefix condition real.

Signing stops being local and free: every `signed_url` becomes an IAM API call. Thus, it
needs a timeout and can rate-limit.

**Acceptance:** `just storage-check` completes against **real GCS** from the prod
container (put / get / sha256 / signed URL got over HTTPS / delete / `ObjectNotFound`
/ idempotent second delete) with no key file present anywhere. And an out-of-prefix key
still triggers a refusal. Closes `R0-be-6`'s unexecuted AC5/AC6.

**Unverified going in:** that `mainsite` can `signBlob` at all. Both probes triggered a refusal
by the dev-box sandbox on 2026-09-11 — confirm it first, since the whole approach rests
on it. *(Resolved: `signBlob` checked 2026-09-15 under `devserver@btvaroska`, which holds
the same grant. See the summary above.)* Details:
[docs/runbooks/artifact-storage.md](../runbooks/artifact-storage.md) →
*Checked against real GCS*.

### Artifact upload — `POST /v1/artifact` (R1-be-1) — **LANDED 2026-09-16**

**What shipped**. The endpoint that gets a user's `.bin` into the system, so R1-BE-2 has
something to hand a device a URL to. Admin-authenticated, raw body (not multipart), with
`target`, `version` and an optional `partition_layout` as query parameters. It returns
the digest, the size and a `created` flag. It is the **first writer of the `artifacts`
table**. `firmware/publish.py` already wrote *blobs* for the agent bundles S0-infra-6
moved into the store. Thus, this is the user-facing half of a storage model that already
existed rather than a new one.

**`artifact_versions`: a label layer, because `artifacts` had nowhere to put a version**.
S0-infra-4 froze `artifacts` with `sha256` as the primary key and no `version` column,
while `spec/device-protocol.md` (`artifact.version` in the `stage` payload) and
`spec/prd.md` → *Retention* ("20 stored versions per platform") both need one. A column
was not available: one digest would then carry exactly one label, and re-tagging
byte-identical firmware would be a primary-key collision rather than the ordinary thing
it is. So migration `0004` adds `artifact_versions` — `(target, version)` PK, `sha256`
with a **real FK** to `artifacts.sha256` `ON DELETE RESTRICT`, `created_at`, and an index
on `(target, created_at)` for the R2 pruner's only query. Mutable pointers over immutable
bytes, the same split S0-infra-6 chose when it made `agent/index.json` the one mutable key
over `blobs/sha256/…`. `artifacts` stays exactly as frozen.

`0003` has no foreign keys. But that was a hard constraint rather than chosen — `builds.outputs`
names artifacts inside JSONB and PostgreSQL cannot FK into JSONB. Here the reference is a
plain column. Thus, the constraint is available. `RESTRICT` is what will stop R2's pruner
deleting bytes a label still points at.

**Three statuses, because a content-addressed store collapses two success cases**. A new
label is **201**. Re-uploading identical bytes under the same `(target, version)` is
**200** with `created: false`, since a re-`put` of the same key is a no-op by
construction. The same label over *different* bytes is **409** and the label keeps
pointing at the original digest. A version is a promise about which image it is, so
silently re-pointing it would make every `deploy_events` row that mentions it ambiguous.
Re-tagging the same bytes under a second label is fine and costs no storage: both labels
name one object.

**One size limit, not two**. The task as filed called for two rejections (the target's
`ota_slot_size` and "the SPEC cap") but `prd.md`'s **1.9 MB** *is* `ota_slot_size`
**1966080** rounded (1966080 B = 1.875 MiB). They are one number written twice. The
implementation uses the authoritative one, `firmware/manifest.py::SUPPORTED_LAYOUTS`.
This is because that is the mapping tied to the partition table a board actually carries and it
is already what agent-bundle validation reads — so an upload and a bundle cannot disagree
about how big a slot is. A second, slightly different cap would were a rejection
nobody could explain. Filed as a spec clarification in `spec/open-questions.md` rather
than resolved by inventing a number.

**Nothing reaches the store until it proves acceptable**. `Content-Length` is necessary (411 without it) and checked before the body is read at all. The stream read is
then capped again so a lying header cannot spend memory either. An upload that writes
3 MB and then apologises has already paid for the object. Writes go **blob first, then
rows**, matching `publish.py`'s crash posture. A failure between them leaves an
unreferenced content-addressed blob. This is inert and re-`put`-able, where the reverse
order would leave a row naming bytes that do not exist.

**The label triggers a rejection, never normalized** (`identity.py`'s rule):
`^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$`, no semver requirement. The vocabulary is the
user's, and a date or a CI number is fine. But it travels into a `dn/cmd` JSON payload and
a dashboard list, so `1.5.0` and `1.5.0 ` must not become two spellings of one release.
There is deliberately **no DB CHECK** on `version`. A constraint there would be this
project deciding what a customer can call their firmware.

**The bytes stay opaque**. No ELF check, no image-header validation, no "is this really an
ESP32 app?" — users build with their own toolchain, and a server that understood the
format would be a server with opinions about which toolchains are allowed.

**Verification**. T1: `ruff` + `ruff format` + type-check green, 728 tests pass, including
`alembic check` (model and migration cannot drift) and `test_migration_downgrades_cleanly`.

T2 ran against the **rebuilt dev container**, which applied `0003 → 0004` on start, using
the real 993696-byte `agent/dist/esp32/app.bin`:

* Upload → **201**, `sha256` equal to the local `sha256sum`, and the object is at
  `blobs/sha256/2484cb76…` in MinIO. `mc cat` of the stored object re-digests to the same
  value (key, contents and response all agree) and `mc stat` shows
  `Cache-Control: public, max-age=31536000, immutable`.
* `artifacts` holds one row: digest, 993696, `kind=user_firmware`, `target=esp32`,
  `partition_layout=ab-4m-v1`.
* Same bytes, same label → **200** `created: false`, still one blob and one row.
* Same label, different bytes → **409** naming the digest it already points at. The label
  stays the same.
* Second label `1.5.1`, same bytes → **201**, two `artifact_versions` rows pointing at one
  digest and one object.
* 2 MB body → **413** naming both numbers, and the bucket gained no object. Empty → 400.
  `partition_layout=ab-16m-v9` → 400 naming `ab-4m-v1`. `version=1.5.0␠` → 400.
  Unauthenticated → 401.

The test module (`tests/test_api_artifact_upload.py`) asserts the oversize and malformed
cases on `store.puts == []`, not merely on the status code — "rejected" and "rejected
before it cost anything" are different promises and only the second is the one this
endpoint makes.

**Decisions & gotchas**. See `DECISIONS.md` 2026-09-16. Two for whoever writes R1-be-2/3.
The 411-on-missing-`Content-Length` rule means a chunked upload design refuses it, and
since S0-infra-5 `signed_url` is a network round trip, so issue a URL per range
request needs a cache.

### Deploy orchestration — `POST /v1/devices/{id}/deploy` (R1-be-2) — **LANDED 2026-09-17**

**What shipped**. The first command the server ever sends a board: `stage`, carrying a
short-lived signed URL for the artifact R1-be-1 uploaded, published to
`ff/v1/d/<device>/dn/cmd`. Admin-authenticated, body `{"version": "1.5.0"}` plus an
optional `apply`, answered **202** with `cmd_id`, `sha256`, `size_bytes`, `apply`,
`reused` and `device_online`. Plus the two things that make it auditable: `deploy_events`
gets its first writer, and the simulator gained a real stage executor so the whole path
can be exercised without a board.

**A fourth broker credential, `commander`, and why the `device` role stayed empty**.
`mosquitto/configure.sh` creates a dynsec role with exactly one ACL,
`publishClientSend 'ff/v1/d/+/dn/#' allow`, and one client holding it
(`MQTT_COMMAND_USERNAME`, `ff-commander` in dev). It deliberately has no
`subscribePattern` and no `publishClientReceive`. The ingestor must remain the only
subscriber. A leaked deploy credential must not be able to forge `up/status`. It is
also not the dynsec admin. That credential is broker-root over `$CONTROL`, a privilege
publishing a command does not need.

Delivery works because **both ACL backends run and allow wins**: dynsec denies
`publishClientReceive` by default, and `mosquitto/acl`'s `pattern read ff/v1/d/%u/dn/#`
grants the board its own downlink. So no change to `mosquitto/acl` and no rule on the
`device` role — a `+` rule there would let every board read every other board's commands.
This is the breach `CRITICAL.md` names. `just broker-check` now proves the matrix live.
The commander publishes to A, **A receives it and B does not**, and the commander's
publish to `ff/v1/d/A/up/status` drops.

**MQTT 3.1.1 has no deny feedback — a limitation, not a bug**. A refused publish is
indistinguishable from a delivered one: no PUBACK reason code, no error, nothing in the
publisher's logs. Every negative assertion in the selftest is thus "nothing arrived
at a subscriber watching `#`", never "the publish raised". If a command silently never
arrives, the reason code is obtainable only over MQTT 5 from inside the container
(`docs/runbooks/dev-stack.md` → failure 5).

**`deploy_events` has exactly one writer**. Everything that inserts goes through
`src/fleetforge/deploys.py`. `tests/test_invariants.py` fails the build if any other
module under `src/` mentions `DeployEvent(` or the table in SQL. The table carries the retain flag
forever and both v1 KPIs are computed over the terminal event of each
`(device_id, cmd_id)` transaction. Thus, a row in the wrong shape is not a bug that appears
today. It is a KPI that is quietly wrong in R6. R1-be-4's `up/status` ingestion adds its
writer *there*, not in `ingestor/handlers.py`.

**The server authors one state, `requested`, and one terminal exception**. `requested`
records "we published a command", which no device can report. Without it an abandoned
deploy is invisible and R1-be-4 cannot map an incoming `cmd_id` back to the intended
version. The exception is `failed` with `detail={"reason": "publish_failed"}`. The
broker refused, so the board provably never saw the command. The transaction gets
closed instead of hanging open forever (the API answers 503). Beyond that the server
never writes a state for a command a device received, and **never expires
`awaiting_safe_window`**: the device owns the reboot. A vehicle in motion can park
there indefinitely. There is no sweeper, no timeout task and no `asyncio.sleep` on this
path, and the test module asserts their absence in the source rather than trusting a
review.

**A retry inside the URL's TTL is the same transaction**. The device deduplicates on the
command `id`. Thus, a retried `stage` must reuse it or the board downloads the same firmware
twice. A POST for the same `(device_id, sha256)` matching the newest `requested` row for
that device (younger than `SIGNED_URL_TTL_S` and with no terminal event) reuses that
`cmd_id`, signs a **fresh** URL, republishes and answers `reused: true`, writing **no
second row**. A different artifact is always a new intent. Past the TTL the first URL expired, so a board that never acted on it cannot act on it now. A new intent is the
honest record.

**The signed URL is a bearer credential**. It appears in exactly one place: the `stage`
payload on the wire. Never in the 202 body, never in `deploy_events.detail` (sha256, size,
target, apply — that is all), never in a log line. The publisher logs `id` and `type`
only. The simulator's transcript prints it redacted. The adapter signs status-checked URLs once per
accepted deploy — since S0-infra-5 signing is an IAM round trip on GCS. Thus, it is not free.

**Refusals are specific, and none of them write an event**. 400 malformed version (the
regex is imported from `api/routers/artifacts.py`, not retyped). 404 Unknown device. 404
No artifact under that label **for this device's chip** — the target is the board's chip,
not a choice. 409 If the device never announced the `ota` capability. 409 If the artifact
row or blob is missing. A device being offline is **reported, not enforced**. `dn/cmd` is
never retained (a retained command re-stages on every reconnect, forever) — durability
comes from the board's persistent session. Thus, the deploy passes and `device_online`
tells the operator what to expect.

**The simulator can now execute a deploy** (`--safe-window auto|hold`): decode, dedup on
`id`, then `staging → downloading → verifying → staged → applying → rebooting`, adopting
the new `fw_version`, publishing each state retained on `up/status`. `hold` stops at
`awaiting_safe_window` and stays there. `apply: on_command` stops at `staged`. It stays
import-pure (stdlib `urllib`, never the API's httpx client) and re-types every protocol
constant, both enforced by existing tripwires.

**Verification**. T1: `ruff` + `ruff format` + `mypy` green, **811 tests pass**,
`just stack-check` clean.

T2 ran against the live dev stack with the real 993696-byte `agent/dist/esp32/app.bin`:

* `just up` healthy. `docker compose logs mosquitto-init | grep -i commander` shows
  `createRole` / `addRoleACL` / `createClient` / `addClientRole` for `ff-commander` on the
  **pre-existing** dynsec store, with 13 "already exists" lines and `bootstrap: done`. The
  bootstrap is still idempotent.

  > **This bullet is the one that lied, and it remains here as the lesson**. Those log
  > lines came from the throwaway broker on `127.0.0.1:1884` that `bootstrap.sh` stood
  > up for itself, not from `fleetforge-mosquitto`. The commander started, correctly,
  > on a broker that then exited. Reading a bootstrap's own log is not evidence that a
  > running broker received anything. Only `listClients` against the live broker — or
  > `just broker-check`, which connects *as* the commander — is. On prod this credential
  > never existed and every deploy answered `Not authorized` for five days.
  > Fixed 2026-09-23 by splitting the bootstrap in two. Ops-log F-2026-09-23-002.
* `just broker-check` → `SELFTEST OK`, including
  `allow ff-commander -> ff/v1/d/ffff00000001/dn/cmd delivered to ffff00000001`,
  `deny ffff00000002 received nothing while ffff00000001 was commanded`, and
  `deny ff-commander -> ff/v1/d/ffff00000001/up/status dropped`. The R0 deny cases
  unchanged.
* Upload → **201** (993696 bytes, `sha256 2484cb76…`). Deploy → **202**
  `reused: false`. The immediate repeat → **202** with the **same** `cmd_id` and
  `reused: true`, and `deploy_events` holds **one** row.
* The simulator printed the redacted payload (exactly the spec keys, `type: "stage"`,
  `confirm_timeout_s: 300`, `artifact.size` equal to the uploaded byte count) then walked
  `staging → downloading (993696 bytes) → verifying (sha256 matches) → staged → applying →
  rebooting → fw 1.5.0`, and dropped the duplicate command.
* `--safe-window hold` parked in `awaiting_safe_window` and was still parked minutes
  later, with one `requested` row and nothing server-side expiring it.
* Hygiene: no row in `deploy_events.detail` contains `http`, and 20 minutes of API logs
  contain no signature or endpoint string.
* Refusals live: wrong-chip label → 404, no `ota` capability → 409, unknown version →
  404, unknown device → 404, `version=../etc` → 400.

**Production prerequisite (not done — `services/` is a different repo and prod env never changes without asking):** `services/prod/.env` must gain `MQTT_COMMAND_USERNAME` and
`MQTT_COMMAND_PASSWORD` before the next prod deploy. Both are `:?`-mandatory in compose, so
without them `mosquitto-config` refuses to start. If the API alone lacks them it selects
`NullCommandPublisher` and every deploy answers 503 (with one startup WARNING).

**Spec proposals (filed, not applied — `spec/` has protection):** `spec/device-protocol.md`
says nothing about a server-authored `requested` marker, nor about a retried `stage`
having to reuse `id`, though both follow from "the device deduplicates on `id`". Propose
one sentence making the reuse rule explicit, a note that `deploy_events.state` can carry
server-authored values outside the device state machine, and confirmation that
`artifact.sig` is optional while R1 has no signer.

**Decisions & gotchas**. See `DECISIONS.md` 2026-09-17.

### Artifact download — `GET /v1/artifact/{sha256}/bin` (R1-be-3) — **LANDED 2026-09-17**

**What shipped**. The link R1-be-2 puts in the `stage` command is now **ours**:
`GET /v1/artifact/<sha256>/bin?exp=<unix-seconds>&sig=<base64url>`, the exact shape
`spec/device-protocol.md` already fixed, on `PUBLIC_BASE_URL` instead of the object
store's host. The endpoint is **public** (the second unauthenticated one in the app
after `POST /v1/enroll`) checks the signature, and answers **307** to a short-lived
store URL. Three new modules: `artifact_urls.py` (mint/check + the `mint` CLI behind
`just artifact-url`), `storage/urlcache.py` (`SignedUrlCache`), and
`api/routers/artifact_download.py`.

**The signature is the authorization**. HMAC-SHA256 over `v1\n<sha256>\n<exp>`, keyed by
`ARTIFACT_URL_SECRET`. `hmac.compare_digest`, never `==`. The handler checks the MAC **before**
`exp` so "expired" and "forged" are not an oracle (both are one 403 body,
`this download link is not valid`). The digest triggers a rejection, never normalized. `exp` has
one spelling only. Nothing above the signature check touches the object store, so an
anonymous caller cannot drive an IAM `signBlob` call or learn which digests exist. `store.signed_urls == []` has an assertion next to every refusal in the unit suite, because
"refused" and "refused before it cost anything" are different promises.

**Redirect, not proxy**. `design/production.md` promises artifacts are served without
touching the API process. A proxy would put an HTTP client in the production image
(`httpx` is a dev dependency) and hold a uvicorn threadpool slot per board for a 1.9 MB
transfer. So `Range`, `Content-Range`, suffix ranges and 416 (the R6 resume path) are
the **store's** RFC-correct implementation, proven against real MinIO in
`tests/test_artifact_download_minio.py`. `Cache-Control: no-store` on the redirect: its
target is a credential with minutes of life.

**One upstream signature per artifact per cache lifetime**. `SignedUrlCache` (modeled on
`CatalogCache`: per app, I/O-free to construct, one `asyncio.Lock`, `time.monotonic()`) is
now the **only** caller of `ObjectStore.signed_url` in the application. The tripwire in
`tests/test_api_deploy.py` was retargeted rather than deleted. A URL stays in use only while
it still has `ARTIFACT_URL_REFRESH_MARGIN_S` (300 s) of life left. Thus, no board receives a
link that dies mid-transfer. The cache never stores failures.

**No database, and 404 never 422**. The download path issues no query at all: it keeps
working during Postgres degradation. The signature already carries the authorization. A
validly signed digest with nothing behind it ends as the store's own 404 after the
redirect. A malformed digest is 404. A validation-error body is an oracle.

**A deploy no longer fails fast on a store outage (behavior change)**. `deploys.py` mints
locally and no longer touches the store. Thus, the old `ObjectStoreError → 503` branch is
gone. It gained a **503 when `ARTIFACT_URL_SECRET` or `PUBLIC_BASE_URL` is unset**, before
any row exists: a 202 whose URL no board can redeem is the lie `NullCommandPublisher`
refuses to tell. `create_app()` emits a fifth startup WARNING for the same condition.

**Verification**. T1: `ruff` + `ruff format --check` + `mypy` green, **888 tests pass**
with MinIO up, `just stack-check` clean.

T2 ran against the live dev stack with a 204800-byte random artifact
(`sha256 db7370c9…`, uploaded 201):

```
URL=$(just artifact-url "$SHA")
curl -sSL -D /tmp/h -o /tmp/got.bin "$URL"   -> HTTP/1.1 307, then HTTP/1.1 200
sha256sum /tmp/got.bin                        -> db7370c9…  (matches)
```

* **Range:** `Range: bytes=1000-1099` → 307 then **206**,
  `Content-Range: bytes 1000-1099/204800`, and the body `cmp`-equal to
  `dd skip=1000 count=100` (SLICE-OK). Suffix `bytes=-64` equals `tail -c 64`
  (SUFFIX-OK). `bytes=999999999-` → **416**.
* **Refusals:** a `--ttl 1` link after `sleep 2` → **403**. The last signature character
  flipped → **403**. No `exp`/`sig` at all → **403**. `/v1/artifact/NOPE/bin` → **404**.
  The API log records `refused: the link expired` / `refused: signature does not match` /
  `refused: no signature` with the digest and never the signature.
* **Signing budget:** after `docker compose restart api`, ten sequential ranged `curl`s
  (all **206**) produced exactly **1** `signed a fresh artifact URL` line.
* **End to end:** `just sim-fleet 1 --capabilities ota` + `POST /v1/devices/{id}/deploy`
  → **202** (no `url` key in the body). The simulator walked
  `staging → downloading (204800 bytes, download #1) → verifying (sha256 matches) →
  staged → applying → rebooting`, printing the URL redacted as `http://localhost:8080/…`.
  `select detail from deploy_events` contains no `http`.
* **Hygiene:** no application log line contains `sig=`. See the residual below.

**Residual (recorded, not fixed): uvicorn's access log prints the signature**. The
application never logs a URL, a signature or the secret, but the access line
(`GET /v1/artifact/<sha>/bin?exp=…&sig=… 307`) contains the full request target, so anyone
who can read container logs can replay a link for its remaining life. Acceptable at v1 (log access already implies host access, and the link expires) and the fix (an access-log
formatter that strips the query string, or turning `--access-log` off in prod) belongs
with the observability work.

**Production prerequisite (not done — `services/` is a different repo and prod env never changes without asking):** before the next prod deploy, `services/prod/.env` needs a
fresh `ARTIFACT_URL_SECRET` (`just artifact-secret`, 32-byte hex, **not** the dev value)
and `PUBLIC_BASE_URL=https://bingo.tvaroska.sk`. The prod compose must pass both to
the `api` service. Without them every deploy answers 503 (with one startup WARNING) and
no board can download firmware. Rotating the secret later invalidates every link in
flight — at most `SIGNED_URL_TTL_S` of staged deploys, which simply re-deploy.

**QEMU (R1-fw-1 will need this):** a QEMU guest cannot reach `localhost` on the host. Its
gateway is `10.0.2.2`. Export `FF_PUBLIC_BASE_URL=http://10.0.2.2:8080` (and
`S3_PUBLIC_ENDPOINT_URL=http://10.0.2.2:9000`, since the 307 target is a `localhost` URL
too) before `just up`, or the board gets a link it cannot resolve.
`docs/runbooks/agent-qemu.md` carries the detail.

**Spec proposals: none**. The URL shape, `exp`/`sig` query parameters and the 403/404
answers all conform to `spec/device-protocol.md` as written. Nothing under `spec/` was
touched.

**Decisions & gotchas**. See `DECISIONS.md` 2026-09-17 (newest entry).

### Deploy outcomes from `up/status` (R1-be-4) — **LANDED 2026-09-17**

**What shipped**. The device half of the story R1-be-2 started: every state a board
reports on `up/status` becomes a `deploy_events` row. One new function,
`deploys.record_observed_status`, which is still the table's **only** writer
(`tests/test_invariants.py` holds that tripwire). A `StatusPayload` in
`ingestor/protocol.py`. A `STATUS` branch in `ingestor/handlers.py`. And
`EventType.DEVICE_DEPLOY` on the SSE channel, emitted only when a row was written. No
migration — `deploy_events` already had the shape.

**The retained topic is the whole design**. `up/status` carries the retain flag and the ingestor
re-`subscribe`s on every connect. Thus, every reconnect replays the last status of every
board. Retained status is still **ingested** (unlike telemetry and log, which the ingestor drops when retained) (it is how an outcome published while the ingestor was down
arrives at all) so the duplicate is the writer's problem: `record_observed_status`
deduplicates on `(device_id, cmd_id, state)`. No unique index, deliberately: a repeated
state is legal data inside a retry. A replay records its state and does **not** move
`last_seen`.

**The row's shape is the KPI**. `is_terminal` comes from `TERMINAL_DEPLOY_STATES`, never
a literal. `artifact_version` and `from_version` are copied off the transaction's
`requested` row. `at` is the server's receipt time, never the device `ts`. A `cmd_id`
with no `requested` row is still recorded (versions NULL, INFO line). A state nobody has
heard of goes into the record and is not terminal. A status with **no `cmd_id`** writes nothing
and only proves liveness — otherwise a booting board adds a row per boot to a table kept
forever. A board claiming `requested` gets a WARNING and no row.

**`detail` undergoes sanitization**. `{"pct", "detail"}` (the wire's own key names) with control
characters stripped, 200 chars max, and `https?://\S+` redacted to `<url>`: the signed
link is a bearer credential and this table stays forever. The wire model **coerces**
instead of raising (a non-int `pct` drops, a non-string `detail` is stringified),
because a `ValidationError` on a retained topic loses the same outcome on every single
reconnect.

**Cancel**. R1 ships no `cancel` command, so "cancel" here is the device's own
`rolled_back`/`failed`. A superseded in-flight deploy gets **no** server-authored
terminal row — see `DECISIONS.md` 2026-09-17 (R1-be-4), decision 4.

**Verification**. T1: `just test` — ruff, `ruff format --check`, mypy and **906 tests**
green, including the deliberately updated `is_terminal=` source tripwire in
`tests/test_api_deploy.py` (now three assignments: `False`, the server's
`publish_failed`, and the device's `state in TERMINAL_DEPLOY_STATES`) and
`test_only_deploys_py_writes_deploy_events`.

T2 against the live dev stack, `app.bin` uploaded as `1.5.0/esp32c6`
(`sha256 2484cb76…`), simulated board `92a9cd2d4251`,
`cmd 5ba290af0ae9463eac4f1ef92fe9d47e`:

```
SELECT state,is_terminal,artifact_version,from_version FROM deploy_events WHERE cmd_id=$CMD
 requested   | f | 1.5.0 | 1.4.2
 staging     | f | 1.5.0 | 1.4.2
 downloading | f | 1.5.0 | 1.4.2
 verifying   | f | 1.5.0 | 1.4.2
 staged      | f | 1.5.0 | 1.4.2
 applying    | f | 1.5.0 | 1.4.2
 rebooting   | f | 1.5.0 | 1.4.2      <- each state exactly once, none terminal
```

* **Success:** publishing `{"state":"confirmed","pct":100}` as the board added exactly
  one row — `confirmed | t | 1.5.0 | 1.4.2 | {"pct": 100}`.
* **Replay:** `count(deploy_events)` = 26 before `docker compose restart ingestor`, 26
  after, and 26 after a second restart. The logs show the retained status arriving
  (`retain=True`) and being ingested as `device.seen` — the deduped event type.
* **Failure:** a second deploy minted a **new** `cmd_id` (`reused: false`, the terminal
  row closed the reuse window). `{"state":"failed","detail":"sha256 mismatch"}` →
  `failed | t | detail='sha256 mismatch'`.
* **Cancel/abandon:** `{"cmd_id":"cmd-abandon-1","state":"rolled_back"}` →
  `rolled_back | t`, versions NULL (unmapped `cmd_id`, recorded anyway). An
  `awaiting_safe_window` row published by hand still had **0** terminal rows minutes
  later — nothing server-side expires it.
* **Forgery:** `{"state":"requested"}` from the board → row count unchanged (35 → 35),
  one ingestor WARNING: `device 92a9cd2d4251 reported the server-authored state
  'requested' … ignoring`.
* **Hygiene:** a status whose `detail` was
  `GET https://host/v1/artifact/abc/bin?exp=1&sig=SECRETSIG failed` stored
  `GET <url> failed`, and
  `SELECT count(*) FROM deploy_events WHERE detail::text LIKE '%http%'` → **0**.

**Spec proposal (not applied — `spec/` has protection)**. `spec/device-protocol.md`
must say that `up/status` `cmd_id` is **required** for a state belonging to a
transaction (a status with no `cmd_id` is unrecordable and only proves liveness). That a server can record device-reported states **idempotently** — a device
republishing the same `(cmd_id, state)` is a no-op. This is what makes the retained
topic safe to replay.

**Decisions & gotchas**. See `DECISIONS.md` 2026-09-17 (R1-be-4, newest entry).

### The device half — `esp_https_ota` + the `stage` handler (R1-fw-1) — **LANDED 2026-09-17**

**What shipped**. `agent/main/ff_ota.{c,h}`: a board that receives `stage` on `dn/cmd`
downloads the artifact through the signed link, writes the inactive A/B slot, checks
the digest against flash, switches the boot partition and reboots into it — walking
`staging → downloading → verifying → staged → applying → rebooting` on `up/status`, the
same walk `simulator/device.py` was publishing all along. Around it: seven
`FF_STATUS_*` constants and `ff_mqtt_publish_status()` in `ff_mqtt.{c,h}` (QoS 1,
**retained**, `{cmd_id,state,pct,detail}`), an `on_stage()` parser next to the existing
`on_command()`, `capabilities: ["ota"]` in `ff_identity.c`, and `esp_https_ota` in the
component's REQUIRES. `agent/version.txt` → `0.3.0`.

**Four properties that are silent wrong answers if you get them backwards** (the file's
own header says the same thing to the next editor):

1. **`downloading` publishes once, not per chunk**. `deploy_events` is a log of
   transitions and `record_observed_status` dedups on `(device_id, cmd_id, state)`. Thus, a
   per-chunk publish writes nothing and costs the broker a message per 4 KB. Progress is
   a serial log line every 10 %.
2. **The digest is taken by reading the partition BACK, after `esp_https_ota_finish()`**.
   Hashing the stream would hash bytes that were never on flash: `esp_ota_write` withholds
   the image header's first 16 bytes until the write completes. Reading the slot back is
   the only check that covers the flash write itself.
3. **A mismatch puts the boot partition back**. `finish()` called
   `esp_ota_set_boot_partition()` by the time we hash. Thus, the undo is not tidiness —
   without it a board with a rejected image boots into it at the next power cut.
   *(Points 2 and 3 were superseded on 2026-10-03 by R2-fw-1. The 16-byte claim does not
   hold for IDF v5.5.5, so the hash now runs before `finish()` and a mismatch never moves
   the pointer. See "Checksum verify before the boot switch" below.)*
4. **The URL never appears in a log line or a `detail`.** It is the authorization
   (R1-be-3), so failures are described without it: "cannot open the artifact", not the
   link that could not be opened.

**One IDF defect had to be worked around** (`resolve_artifact_url()` in `ff_ota.c`).
`esp_https_ota` follows our `/v1/artifact/{sha}/bin` **307** by itself, but IDF v5.5.5
rebuilds the `Host` header wrong on a redirect: `esp_http_client_init()` uses
`_get_host_header(host, port)` (with `:port`), while the redirect path,
`esp_http_client_set_url()`, sets `Host` to the bare host **and only when the host string
changed**. Thus, a redirect that keeps the host and changes only the port keeps hop one's
`Host` verbatim. An S3-compatible presigned URL signs `host`, so the store answers
**403 SignatureDoesNotMatch**, which surfaces as `esp_https_ota: File not found(403)`. The
agent thus resolves the single hop itself (one header-only `GET` with
`disable_auto_redirect`, capturing `Location` from `HTTP_EVENT_ON_HEADER`) and hands the
final URL to `esp_https_ota_begin()`. Production (GCS on :443, which signs a portless
Host) never saw this. A self-hosted MinIO on :9000 (V2's shape) fails every deploy.

**Scope**. This stops at `rebooting` → `esp_restart()`. No `confirming`/`confirmed`, no
`cmd_id` persisted across the reboot: the image that comes back simply announces, and the
confirm/rollback pair already in `ff_mqtt.c` (dormant since R0) goes live as a consequence.
`apply: "on_command"` stages and stops — deliberately **not** `awaiting_safe_window`, which
an always-on board would never leave. A second `stage` while one runs triggers a refusal and
reported `failed` on the new `cmd_id`. `confirm_timeout_s` parses, logged if it differs
from the firmware's own 300 s, and otherwise ignored until R2.

**Verification**. T1: `just test` — ruff, `ruff format --check`, mypy and **909 tests**
green (two new tripwires in `tests/test_ff_cfg.py`: every state the firmware can publish
exists in the spec's machine, and the walk it does is exactly the seven declared
states), plus `just agent-build esp32` → `BUNDLE OK`. The esp32 app grew 993,696 →
1,010,912 B, ratcheted in `tests/test_agent_power_and_size.py`. That is 51 % of the
1,966,080-byte slot. Thus, the image can still download its own replacement.

T2 was a real OTA on the QEMU board (`000000000000`) against the dev stack, board running
`0.3.0`, artifact `0.3.1` (`sha256 7b2a5868…`, 1,010,912 B), `cmd
4bff6498068d4c83872fa3c72375f9fd`:

```
ff/v1/d/000000000000/up/status  staging(0) downloading(0) verifying staged(100) applying(100) rebooting(100)
deploy_events: requested staging downloading verifying staged applying rebooting   <- one row per state
```

* `POST /v1/devices/000000000000/deploy` → **202** (it is 409 until the announce carries
  `ota`).
* The log shows the 307 followed (`artifact link redirected (307) to the object store`),
  progress 0→100 %, and `sha256 7b2a5868… matches; ota_1 is staged and bootable`.
* `grep -c 'sig=' /tmp/qemu*.log` → **0**.
* The board booted `ota_1`, announced `fw_version 0.3.1` (the server row agrees) and the
  pre-existing confirm path fired: *"this image was written by OTA and is now CONFIRMED"*.
  No rollback.
* **Negative:** the same artifact staged under a deliberately wrong `sha256` →
  `sha256 MISMATCH, flash holds 7b2a5868…ee8c, the command says …dead`, `boot partition
  put back to ota_1`, `failed` with `detail: "sha256 mismatch"` — and a cold restart still
  started on the old image.
* **Duplicate:** re-publishing the identical `dn/cmd` → `duplicate command id=… — ignored
  (QoS 1 redelivery)`, no second download.

**The one thing QEMU cannot show:** `esp_restart()` itself. The emulator panics on the
next boot, in IDF's own `esp_timer_impl_init → esp_intr_alloc`, *before* `app_main` and in
whichever image it lands on — including the pre-OTA `0.3.0` that boots fine from power-on.
It is a soft-reset defect of the machine, not of the firmware. The runbook has the decoded
backtrace and the cold-restart workaround used above.

**Spec proposal (not applied — `spec/` has protection)**. `spec/device-protocol.md` lists
`artifact.sig` and `broker/commands.py::stage_payload()` never emits it. Either the spec
drops the field or R2 implements it. Until then the agent parses it as
optional-and-ignored. Second, smaller: the spec's machine must say that
`awaiting_safe_window` is for boards that *have* a window — an always-on agent staging
under `apply: "on_command"` stops at `staged`.

**Decisions & gotchas**. See `DECISIONS.md` 2026-09-17 (R1-fw-1, newest entry).

### The reported version is the one that BOOTED (R1-fw-2) — **LANDED 2026-09-17**

**What shipped**. `ff_identity_fw_version()` — one accessor, returning
`esp_app_get_description()->version`, that is,the descriptor embedded in the image that is
*executing*. `up/announce` and `up/hb` both take `fw_version` from it and from nothing
else. `agent_version` stays a separate expression, because from R1 the agent can be a
component inside a user firmware and only `fw_version` moves. `log_boot_facts()` now also
prints the running image's version **and its OTA state**. `tests/test_ff_cfg.py::TestTheReportedVersionIsTheRunningOne` pins all of it.
`agent/version.txt` → `0.3.1`. The esp32 app grew 1,010,912 → 1,011,216 B (+304 B of
`.rodata`), ratcheted in `tests/test_agent_power_and_size.py`.

**This was a seam, not a behavior change**. The happy path already read the running
descriptor. What did not exist was anything stopping the *plausible* refactor (reporting
`ff_ota_cmd_t::version`, the version the server asked for) which is right on every deploy
that worked and wrong on every deploy that did not. The failure is invisible at runtime
(the board reports confidently, just falsely) and the fix would ship by OTA to a fleet
whose OTA reporting is the broken thing. Hence three text tripwires: one source for the
version, both payloads using it, and `ff_ota.c` neither emitting `"fw_version"` nor
including `ff_identity` — the command seam stays one-directional.

**The new boot line**, which is what tells an applied update from a rolled-back one in a
serial log with no server attached:

```
I ff-agent: running partition: ota_1 type=0 subtype=17 offset=0x200000 size=1966080
I ff-agent: running image: fw_version 0.3.2, ota state pending_verify — this is what
            up/announce and up/hb report
```

Read-only, and deliberately **not** merged with `ff_mqtt.c`'s reader of the same otadata:
two small readers is the cheap outcome, a shared helper someone later "improves" is a
bricked fleet (CRITICAL.md → *Device-side confirm timer / rollback path*).

**Verification**. T1: `just test` — ruff, `ruff format --check`, mypy, **912 tests** green.
`just agent-build esp32` → `BUNDLE OK` (the component is `-Wall -Wextra -Werror`, and
dropping the now-unused `app` local in the heartbeat builder is part of why) and
`just agent-verify esp32`.

T2 on the QEMU board `000000000000`, dev stack with guest-reachable origins, board A
`0.3.1`, artifact B `0.3.2` (`sha256 c5cf59a6…`, 1,011,216 B). **Negative first**, because
it needs the board still on A:

```
# told 0.3.2, digest corrupted by hand, published as the commander role
E ff-ota: sha256 MISMATCH, flash holds c5cf59a6…8f6e, the command says c5cf59a6…dead
E ff-ota: boot partition put back to ota_0: the staged image was rejected and will NOT be booted
up/status (retained)  {"state":"failed","detail":"sha256 mismatch"}
up/hb                 {"fw_version":"0.3.1", …}      <- after the failure, repeatedly
GET /v1/devices       000000000000 -> 0.3.1          <- the load-bearing assertion
```

and after a cold restart: `running image: fw_version 0.3.1, ota state pending_verify`
(the undo re-wrote otadata, so the old slot re-confirms itself), `CONFIRMED`, row `0.3.1`.

**Positive**, the same artifact deployed properly:

```
I ff-ota: sha256 c5cf59a6… matches; ota_1 is staged and bootable
I boot: Loaded app from partition at offset 0x200000     (cold start)
I ff-agent: running partition: ota_1 …
I ff-agent: running image: fw_version 0.3.2, ota state pending_verify
W ff-mqtt: this image was written by OTA and is now CONFIRMED
up/announce (retained) {"fw_version":"0.3.2","agent_version":"0.3.2", …}
up/hb                  {"fw_version":"0.3.2", …}
GET /v1/devices        000000000000 -> 0.3.2
```

The dashboard row moved `0.3.1 → 0.3.2` with **no code anywhere that writes a commanded
version into an identity payload**. That is the whole claim.

**QEMU note (runbook updated)**. The positive half was driven with
`apply: "on_command"` + a power-cycle rather than `apply: "auto"`. R1-fw-1's workaround (poll for `staged and bootable`, then `docker kill` before the agent reboots itself) loses
a **40 ms** race: the board soft-resets into ota_1, hits the emulator's known
`esp_timer_impl_init` panic, and the bootloader correctly retires the `PENDING_VERIFY`
image, leaving you on the old slot with `otadata` `aborted`. That run is itself a fourth
data point for this task. A board that ran `0.3.2` for a few hundred milliseconds and was
rolled back reports `0.3.1` again, with no state of ours involved.

**Spec proposal (not applied — `spec/` has protection)**. `spec/device-protocol.md:104`'s
example payload shows `"agent_version": "0.3.0"`, now two releases stale. Cosmetic, an
example rather than a contract, and worth a refresh the next time that file is opened for
a real reason.

### The dashboard half (R1-fe-1) — **LANDED 2026-09-17**

> Superseded 2026-10-03 by R2-fe-1: a finished deploy no longer reads `done — running the new version`. It says `good` or `rolled back` (see *The dashboard shows the outcome*).

R1's user-visible claim — *"push firmware from the dashboard and watch the version
change"* — closes here. Every piece behind it existed. Nothing on screen reached it.

**Two read endpoints, no new machinery.**

* `GET /v1/artifact` (admin) lists every deployable label: `target`, `version`, `sha256`,
  `size_bytes`, `partition_layout`, `kind`, `created_at`, ordered `(target, created_at
  DESC, version)`. **There is no `url` in it**. A download link is a short-lived bearer
  credential minted per deploy, never a field in a list. It lives on
  `api/routers/artifacts.py` (admin-only). The same `/v1/artifact` prefix is *also* the
  public download route in `api/routers/artifact_download.py`, where the signature is the
  authorization. Thus, the list route asserts a 401 both bare and with a `?exp=&sig=` bolted
  on. Two labels over one digest (an esp32 `1.5.0` and an esp32c6 `1.5.0` built from the
  same bytes) are two rows with one `sha256`. This is ordinary.
* `DeviceSummary.deploy` carries the newest deploy transaction's current state (`cmd_id`, `state`, `at`, `is_terminal`, `artifact_version`, `from_version`, `pct`,
  `detail`) or `null` for a board never deployed to. It is a column of the fleet read
  model, not an endpoint: see `DECISIONS.md` 2026-09-17. The SQL
  (`DISTINCT ON (device_id) … ORDER BY at DESC, id DESC`) lives in `deploys.py`, the
  module that owns `deploy_events`. `devices.py` calls it exactly the way it already
  calls `progress.latest_progress()`.

**The cell**. `frontend/src/DeployCell.tsx` (rendering + the POST) and
`frontend/src/deploy.ts` (the label table + one `GET /v1/artifact` on mount, no poll — an
artifact appears when a human uploads one and there is no event type for it). The picker
offers only the versions whose `target` is this board's `platform_type`, newest
preselected. This is because the server refuses a mismatch and offering one is offering a 409.
The live line comes from `device.deploy` and from nothing this component remembers.
That is why a reload and a second tab agree.

Wording is most of the value here: `downloading the image`, `image written, waiting to
reboot`, `done — running the new version`. A lookup with an `?? raw` fallback, the same
idiom as `STAGE_LABELS`, because `deploy_events.state` is TEXT with no CHECK and an agent
newer than this dashboard must render as itself rather than vanish. `pct` is text and
never a bar. `awaiting_safe_window` gets a full sentence and no error styling.

**T2 evidence** (dev stack, three simulated esp32c6 boards, Chromium via Playwright):

```
GET /v1/artifact          200 authed, grouped by target, newest-first, no `url` key
GET /v1/artifact          401 bare
GET /v1/artifact?exp=…&sig=AAAA
                          401  — a download signature buys nothing on the list route
deploy_events (otabench)  requested → staging → downloading → verifying → staged
                          → applying → rebooting, artifact_version 1.6.0 throughout
Firmware column           1.5.0 → 1.6.0 on its own, with no reload
after F5                  the same live state — it came from the read model
noota row → Deploy        role=alert, verbatim: "this device did not announce the `ota`
                          capability, so it has no agent that can stage an update.
                          It announced: nothing."
Deploy twice in 30 s      "already in flight — the board deduplicates and will not
                          download twice"; the simulator's download count is unchanged
hold board, +300 s        "waiting for a safe moment — the board decides when, and may
                          wait indefinitely", unstyled, no spinner, no aria-busy, and
                          SELECT count(*) … WHERE is_terminal = 0
revoke the session        the login gate, not an error banner in the cell
role="progressbar"        0 on the whole page
```

One correction to the acceptance script for whoever runs it next: a simulated board
announces its new `fw_version` on its next **connect**, not on its next heartbeat. The
heartbeat payload does not carry the field, and `device.py` says as much ("announced on
the next connect"). The version flip is observed by making the board reconnect
(`docker compose restart mosquitto`). This is what a rebooting board does anyway.

> **Superseded 2026-09-23 by S0-test-4, and half of it was never true**. The heartbeat
> *does* carry `fw_version` (`DeviceIdentity.heartbeat`, and `ingestor/store.py` reads it
> from there). It carried the **stale** one. This is a different thing and is the whole
> defect. And the manual `docker compose restart mosquitto` is no longer needed: the
> simulator now ends its own session on apply. Thus, the version converges unaided. See
> *The simulator never reconnected after `apply`* below.

**Deliberately not in R1:** an upload UI (curl only), a deploy history/timeline, group
deploy, cancel (there is no server-authored cancel), and a rollback button (R2).

**Spec proposal (not applied — `spec/` has protection)**. `spec/device-protocol.md` must
say outright that `up/status.state` is an **open** vocabulary. The server stores it as TEXT
with no CHECK, `DeployState` is advisory, and both the server and this dashboard treat an
unrecognised value as a legitimate state to record and render verbatim. That is already
the behavior on both sides. The spec only implies it by listing examples.

### E2E on real hardware — push firmware, board version changes (R1-test-1) — **PASSED 2026-09-23**

**R1's "Done when", met on metal**. Device `94a990dd09a4` (ESP32-S3) went `0.3.2 → 0.3.1`
on a deploy driven from the dashboard API. The walk was `202 Accepted`, then `downloading`
(pct 0), `rebooting` (pct 100), and back online reporting `agent_version` and `fw_version`
0.3.1. About 25 s end to end. `cmd_id a1d8ed965208447fb9cbce4bb4dd6504`, artifact
`4c8529eb…` (991344 bytes, layout `ab-4m-v1`). R1 closes.

**`rebooting` as the last reported state is correct, not a stuck deploy**. `ff_ota.h:8-9`
ends the R1 agent's walk at `rebooting` → `esp_restart()`. `confirming`/`confirmed` are R2.
The device still *does* the validation. `ff_mqtt.c:109` calls
`esp_ota_mark_app_valid_cancel_rollback()` on announce-ack. So the slot is marked valid and
there is no rollback exposure. The cost is cosmetic and belongs to R2. `is_terminal` stays
`false` forever. Thus, the dashboard shows every successful deploy as still in flight.

**The written definition of the task was wrong. The gap is the finding**. It said "the
target is already on the fleet. Thus, the run is a deploy and a version check". In practice the
fleet board was on 0.2.0, which predates OTA. Thus, the run needed a bootstrap USB re-flash
*first*. Then it exposed three production defects that nothing else could caught
(`../../../docs/ops-log.md` F-2026-09-23-001/002/003):

- **Deploy had never worked on prod**. `ARTIFACT_URL_SECRET` and `PUBLIC_BASE_URL` were
  never wired into the prod compose. Both are `None`-defaulted so `create_app()` stays
  constructible. The API booted, `/v1/healthz` was green and the Deploy button rendered
  anyway. Fixed in services `90205ed`. `S0-infra-9` later made `readyz` fail on exactly
  this.
- **`mosquitto-init` had never reached the running broker**. It wrote dynsec JSON
  underneath a live broker that never reloads. Thus, the `commander` client did not exist in
  the broker's memory. Fixed structurally by splitting it into two one-shots,
  `bootstrap.sh` (before the broker) and `configure.sh` (over `$CONTROL` after it). See
  `DECISIONS.md` and `CRITICAL.md`.
- **A mutable URL was served with `max-age=3600`**. That aborted a flash on a bogus sha256
  mismatch. Fixed in `c4fa7d2`.

All three have the same shape: a component reports healthy because nothing exercises the one
path that is broken. **Write any future "E2E on hardware" task as bootstrap-flash *then*
deploy, and treat it as the only thing that exercises the deploy chain at all**. Keeping
this task bench-gated rather than QEMU-passable (`DECISIONS.md` 2026-09-16) is what surfaced
the defects.

**What a remote deploy risks, as of R1's close**. There are three failure modes:

1. *Image fails to boot*. The bootloader's own rollback handles it
   (`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`, checked in every bundle by `verify_bundle.py`).
   This is standard ESP-IDF and we have not exercised it.
2. *Boots, joins, never confirms.* **Proven recoverable on metal** the same day. See
   *Phase 2* below and [`../runbooks/rollback-test.md`](../runbooks/rollback-test.md).
3. *Boots, the announce IS acked. But the image is broken in some other way*. It confirms
   itself and **nothing recovers it automatically**. That is the residual gamble, which R2
   narrows. Until then, roll to one board at a time.

**The CUJ-1 gate blocked R1 → R2 the same day**. `/replan` ran the CUJ-1 verification suite.
CUJ-1's driver uses segments, and only segments with a harness are graded:

| Steps | Segment | Result (2026-09-23, run twice) |
|---|---|---|
| 1–2 | Sketch compiles with the library | not graded — R3, no harness |
| 3 | One flash → board on the fleet | **pass** — `agent-qemu-smoke`, `tests/test_enroll.py` 29 passed, board online |
| 5 | OTA a changed build → new version reported | **FAIL** — `fw_version` never converged |
| 6 | A bad build recovers itself | not graded — no QEMU harness. Proved on metal instead |
| — | A wrong flash layout triggers a refusal | not graded — R3 (`R3-fw-5`) |

Segment 5 was the simulator, not the server. We fixed it as `S0-test-4` (next entry), and
the gate's own reproduction converges afterwards. The gate itself still has to be re-run by
`/replan` before R2 opens. The same run surfaced `S0-infra-8` (the ingestor probe measured
traffic), which is written up in `infrastructure.md`.

### The simulator never reconnected after `apply` (S0-test-4) — **LANDED 2026-09-23**

**Why this lives as a filed item under OTA deploy rather than under the simulator**. The simulator is
the only hardware-free check of R1's central claim ("the version the board reports
afterwards is the one that was uploaded") and it had never once checked it. CUJ-1
segment 5 is graded by exactly this harness. Thus, the defect is what blocked the R1→R2
transition at the T3 gate, twice on 2026-09-23.

**What was wrong**. `StageRunner._stage` finished the walk, rebound
`self.identity = replace(self.identity, fw_version=version)` and logged *"apply now
running fw_version 1.6.0 (announced on the next connect)"*. Every word of that is true
and none of it is observable: `run_session` captured the **old frozen**
`DeviceIdentity` as its local `device`. Its heartbeat loop kept publishing from it.
On an `always_on` board nothing ever ends the MQTT session, so the promised next connect
never came and `GET /v1/devices` reported the pre-deploy version indefinitely. The server
was never at fault. A real board reboots (the TCP session dies and it re-announces under
its own power) which is exactly why `R1-test-1` passed on metal (0.3.2 → 0.3.1) while
this path silently did not.

**What shipped**. `StageRunner` grew a `reboot: asyncio.Event`, set unconditionally once
the walk publishes `rebooting` (the reboot is a consequence of the apply, not of the
payload naming a version). `run_session` races it alongside `stop` as a second exit
condition and skips the goodbye when it fires — a restarting board stops mid-sentence, it
does not report itself offline. `run_always_on` treats that return as a **third kind of
session end**, distinct from a broker error and from the broker closing the stream. It clears the flag, resets `boot_monotonic` so uptime restarts, waits `REBOOT_DELAY_S`
(2.0 s, standing in for bootloader + Wi-Fi + CONNECT) and reconnects with
`stage.identity`. This is how the new version legitimately reaches the server.
`run_sleepy` does the same minus the delay and re-announces on its next wake.

**The one deviation that remains, recorded rather than fixed**. `aiomqtt` has no public
API for dropping a connection (module docstring, property 5). Thus, the reboot still exits
through a clean DISCONNECT and the LWT does not fire. Retained presence thus stays
`{"online":true}` across the reboot instead of flapping offline for the length of a boot
— the benign direction, and the same class of limitation already recorded for sleepy mode.

**A stale note corrected**. The R1-fe-1 write-up above told the next reader that the
heartbeat does not carry `fw_version`. It does. It was carrying the stale one.

**Verification**. T1: `just lint` (ruff + `ruff format --check`), `just typecheck`
(mypy, 71 source files) and `just test` — **941 tests pass**.

T2, both halves of the acceptance criterion:

1. *Asserted by a test, not read by hand*. Four new tests in `tests/test_simulator.py`,
   and the first ones in the suite to drive the reconnect loops at all:
   `test_an_apply_ends_the_session_and_the_next_one_reports_the_new_version` (the
   reproduction — announce **and** heartbeat carry `1.6.0` in the second session, the
   first carried `1.4.2`, the first published no goodbye), the `sleepy` twin, an apply
   with no version (still reboots), and a tripwire that a session which applies nothing is
   not cut short and still owes its goodbye. Confirmed discriminating: with
   `self.reboot.set()` deleted, the reproduction test fails on its 5 s deadline.
2. *The literal reproduction from the T3 gate*, dev stack, same board the second `/replan`
   run used:

```
just sim-fleet 1 --capabilities ota   → sim-01 -> 92a9cd2d4251, fw_version 1.4.2, online
POST /v1/devices/92a9cd2d4251/deploy {"version":"1.6.0"}
                                      → 202, cmd_id 1068a68bea70410da823598188885012,
                                        sha256 5c0e4851…, 230000 bytes
transcript                            staging → downloading → verifying (sha256 matches)
                                        → staged → applying → rebooting
                                      → reboot   restarting into fw_version 1.6.0
                                      → boot     back in 2s on 1.6.0
                                      → connect / announce / presence / hb
GET /v1/devices (≈20 s later)         {"device_id":"92a9cd2d4251","fw_version":"1.6.0",
                                       "online":true}
```

   This is the exact check that returned `fw_version: 1.4.2` on both gate runs.

**Still true after this, and still R2:** the deploy parks at `rebooting` with
`is_terminal: false` forever, because nothing writes `CONFIRMED` — confirm reporting is
`R2-BE-1`/`R2-FW-3`. The simulator's stage walk deliberately ends where the R1 agent's
does.

## Phase 2: R2 — Safe deploy: check + auto-rollback ⭐

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R2-FW-1 | Checksum check before apply | P0 | 1d |
| R2-FW-2 | A/B slot apply (OTA0/OTA1), atomic switch | P0 | 1.5d |
| R2-BE-1 | Observe confirm/rollback outcome. Record the result to `deploy_events` | P0 | 1d |
| R2-FW-3 | **Device-side** confirm timer armed pre-reboot → `esp_ota` self-rollback on timeout | P0 | 1d |
| R2-FE-1 | Dashboard shows `good` versus `rolled-back` per device | P0 | 0.5d |
| R2-TEST-1 | Push a *deliberately broken* build → board auto-recovers | P0 | 1d |

**Done when:** a deliberately broken build deploys → the board auto-recovers to the
previous version.

**R2-FW-3 and most of R2-TEST-1 landed early, in R1**. The device-side confirm timer
shipped with the R1 agent (`ff_mqtt.c`, `confirm_timeout_cb` →
`esp_ota_mark_app_invalid_rollback_and_reboot`), and on 2026-09-23 it was exercised on
real hardware for the first time: `94a990dd09a4` deployed a deliberately broken
`0.3.2-rbtest` image, joined the fleet on it, and returned on 0.3.1 71 s later unattended.
Procedure and its limits: [`../runbooks/rollback-test.md`](../runbooks/rollback-test.md).
What remained of R2-TEST-1 was the *other* failure modes — boot loop, brownout mid-write,
flaky radio — none of which that test covers. R2-test-1 (2026-10-03) took the first two,
plus a torn otadata write, in QEMU: all land on the previous image. It also found one that
did **not** recover: an image that hangs before its broker session. R2-fw-4 (agent 0.4.3)
fixed it by arming the confirm timer first thing in `app_main`, proven in QEMU. The flaky
radio (R2-test-2) cannot outrun the confirm timer, because the download and the timer
never overlap. A silent peer mid-download used to hold the update slot until a power
cycle; since agent 0.4.4 (R2-fw-5) it ends `failed` / `download stalled` after 60-80 s.
Proven in QEMU, bench replay owed. See *Remaining failure modes (R2-test-1)*, *Arm the
confirm timer at boot (R2-fw-4)*, *Flaky link (R2-test-2)* and *A stalled download fails
(R2-fw-5)* below.

What that result does **not** do is retire R2-BE-1. The R1 agent's reported walk ends at
`rebooting` (`ff_ota.h`), so the rollback is only inferable from the announced version
changing back; the server never sees `rolling_back`/`rolled_back` and the deploy row stays
non-terminal. R2-FE-1's `good` vs `rolled-back` column has nothing to read until BE-1
lands. (It landed on 2026-10-03. See *R2-be-1* below.)

**A fourth failure mode surfaced alongside it: a board that was never protected**. Rollback
needs a bootloader that supports it, and Fleetforge OTA replaces the app, not the
bootloader. Thus, a device flashed from an old bootloader can never gain the safety net from
us, and announces identically to one that has it. Step 1 of
[board-profiles.md](board-profiles.md) adds the `rollback_capable` field to `up/announce`
that makes the difference visible, together with a measured partition-table fingerprint to
cross-check the `partition_layout` name `_check_compatible()` currently takes on trust.
Both are R2-sized and belong with the safe-deploy work.

### Confirm/rollback outcome reaches `deploy_events` (R2-be-1) — **LANDED 2026-10-03**

**What shipped.** The server half already existed. `record_observed_status` (R1-be-4)
records any state, and `confirmed`, `rolled_back` and `failed` are terminal. The gap was
that nothing published those states, because the `cmd_id` died with the image that
received the `stage`. This task closes the gap on the device and in the simulator. The
server code is unchanged, and so is the spec.

- **`agent/main/ff_txn.{h,c}` (new).** One NVS record, `(cmd_id, target slot address)`,
  in its own namespace `ff_txn`. It offers `save`, `load` and `clear_if(cmd_id)` under one
  static mutex.
- **`ff_ota.c`** saves the record once the read-back sha256 matches, before the
  `staged and bootable` line and before `staged` is published. Slot selection, `finish()`
  and `restore_boot_partition()` are byte-for-byte unchanged.
- **`ff_mqtt.c`** classifies the boot once, before connecting, from otadata plus the
  record. It then reports:
  - `confirming` once per boot, on connect;
  - `confirmed` at the announce PUBACK, only when marking the image valid succeeded;
  - `rolled_back` from the image the board returned to, with a detail naming the slots
    (`returned to ota_1; ota_0 did not confirm`);
  - `rolling_back`, best-effort, when the confirm timer fires.

  The record is cleared at the terminal state's PUBACK. `confirm_timeout_cb` never touches
  the MQTT client: it starts a pre-created 2 s `ff_rollback` timer and hands the report to a
  one-shot task. If the timer cannot start, or there is nothing to report, it rolls back
  immediately, as before. An announce ack that arrives inside the grace window does not
  confirm an image that has already been sentenced.
- **Agent `0.4.0`** (was 0.3.2). The image grew 4,208 B on esp32 and 4,352 B on esp32s3,
  and the budgets in `tests/test_agent_power_and_size.py` were raised to the measured byte.
- **Simulator.** `StageRunner` gains `pending` (the NVS analogue) and `rolled_back`, plus
  `on_boot()`, which the session calls right after announce + presence. There are two new
  CLI flags: `--confirm auto|never` (`never` = an `FF_ROLLBACK_TEST` image) and
  `--confirm-timeout SECONDS` (default 300). This is CUJ-1 segment 6's harness.

**Why the outcome is device-reported, and the transition gap.** See `DECISIONS.md`
2026-10-03. In short: the server does not infer a rollback from the announced version,
and a deploy issued to an R1 agent (prod's board is on 0.3.1) gets no outcome report even
if the new image is R2. **Rollout note:** the first deploy that carries this agent to a
board stays at `rebooting`, as before. Every deploy *from* a 0.4.x board reports its
outcome. Do not read a stuck `rebooting` on that first hop as a defect.

**Verification.** T1: `just test` is green: ruff, `ruff format --check`, mypy and **992
tests**. That includes the updated `test_the_walk_the_agent_performs_is_declared` (now 11
states) and the new firmware tripwires in `tests/test_agent_txn.py`:
- `confirm_timeout_cb`'s body, extracted by brace matching, has no `esp_mqtt_client_`;
- `ff_txn_save(` sits between the digest check and `FF_STATUS_STAGED`;
- the outcome states appear in `ff_mqtt.c` and never in `ff_ota.c`;
- the record is cleared only through `clear_if`.

There are also new ingestor R2-walk tests and simulator confirm/rollback tests.
`just agent-build esp32` and `just agent-build esp32s3` both end in `BUNDLE OK` under
`-Werror`, and `just agent-qemu-smoke esp32` ends in `HARNESS OK`.

T2-A, the simulator on the live dev stack. The artifact was `1.5.0/esp32c6`
(`sha256 2484cb76…`). `good` = `760e607624d6` ran with the default `--confirm auto`, and
`bad` = `2e05d4b689d2` ran with `--confirm never --confirm-timeout 5`.

```
good  cmd f628952d37bf417eb2d7c839d56abaf7
 requested|f|1.5.0|1.4.2   staging|f   downloading|f   verifying|f   staged|f
 applying|f   rebooting|f   confirming|f|1.5.0|1.4.2   confirmed|t|1.5.0|1.4.2
 GET /v1/devices -> fw_version 1.5.0, deploy.state confirmed, is_terminal true

bad   cmd 743166db33584dc58fc64f49149fc412   (rolled_back 9 s after the POST)
 requested … rebooting|f   confirming|f   rolling_back|f
 rolled_back|t|1.5.0|1.4.2|{"detail": "returned to 1.4.2; the new image did not confirm"}
 GET /v1/devices -> fw_version 1.4.2, deploy.state rolled_back, is_terminal true
```

Every state appeared exactly once, and every row carried both versions. **Replay:**
`count(deploy_events)` was 144 before and stayed at 144 after each of two
`docker compose restart ingestor`. **Reuse:** a repeat POST to `good` minted
`bcb92f7c…` with `reused: false`, and that deploy also walked to `confirmed`.

T2-B, the real agent in QEMU (`docs/runbooks/agent-qemu.md` → *Driving an outcome*).
The record below is the run on the **final** code. An earlier run on images `0.4.0`/
`0.4.1`/`0.4.2-rbtest` passed the same three cases before the late-ack guard and the
immediate no-record rollback were added. The board was A′ `0.4.0`, B′ was `0.4.3`, and R′
was `0.4.4-rbtest` (`FF_ROLLBACK_TEST=1`, an artifact only). All three images contain this
code.

```
B1 confirmed   cmd 761416d5…  0.4.3, apply on_command, power-cycled after `staged`
  board: ff-txn: transaction 761416d5… recorded (target slot at 0x00200000)
         running image: fw_version 0.4.3, ota state pending_verify
         ff-mqtt: transaction 761416d5…: confirming on ota_1
         … is now CONFIRMED …  -> state=confirmed queued -> ff-txn: … closed — record cleared
  rows:  requested staging downloading verifying staged confirming|f confirmed|t|0.4.3|0.4.0
  fw_version 0.4.3. One more power cycle: `ota state valid`, no transaction line,
  rows 7 -> 7.

B2 rolled back by the bootloader   cmd 62febfdb…  0.4.4-rbtest, apply auto
  soft reset into ota_0 -> the known QEMU esp_timer_impl_init panic in PENDING_VERIFY
  -> the bootloader aborts ota_0. Cold boot on ota_1:
  board: ff-mqtt: transaction 62febfdb…: rolled_back (returned to ota_1; ota_0 did not confirm)
  rows:  … applying rebooting|f   rolled_back|t|0.4.4-rbtest|0.4.3|returned to ota_1; ota_0 did not confirm
  fw_version stays 0.4.3.

B3 rolled back by the confirm timer   cmd f39ead85…  0.4.4-rbtest, on_command + power cycle
  board: running image: fw_version 0.4.4-rbtest, ota state pending_verify
         transaction f39ead85…: confirming on ota_0
         FF_ROLLBACK_TEST: ignoring the announce ack on purpose …
  (69882) no working session 60 s after an OTA boot — marking this image invalid …
  (69892) publish … state=rolling_back          <- on the broker at 13:11:32, BEFORE the reset
  (72882) esp_ota_ops: Rollback to previously worked partition.
  cold boot on ota_1: transaction f39ead85…: rolled_back (returned to ota_1; ota_0 did not confirm)
  rows:  … staged|f   confirming|f   rolling_back|f   rolled_back|t|0.4.4-rbtest|0.4.3
  fw_version back at 0.4.3.
```

B3 is the run that proves the board still rolls back with the reporting code in place: the
grace timer fired the rollback 3 s after the report, and nothing waited on the MQTT
client.

**Spec proposals (not applied — `spec/` is protected).** For `spec/device-protocol.md` →
`up/status`:

1. "`confirming` is reported by the new image once it has a broker session, and
   `confirmed` once it has marked itself valid. `rolled_back` is reported by the image the
   device **returned to**, on its first session after the rollback. `rolling_back` is
   best-effort and may never arrive. A device must therefore persist the `cmd_id` across
   the apply reboot."
2. "A device MAY republish a terminal state after a reset, and recording stays
   idempotent."
3. An outcome can only be reported by firmware that implements (1), so transactions
   issued to older agents may legitimately never reach a terminal state.

**Out of scope.** A `stage` that arrives while the running image is still
`PENDING_VERIFY` would target the rollback slot. That belongs to R2-fw-2 (see
`DECISIONS.md`). Resolved by R2-fw-2 (agent 0.4.2): the board refuses that stage before
any I/O. See *A/B slot apply and the atomic switch (R2-fw-2)* below.

### Checksum verify before the boot switch (R2-fw-1)

**What R1 had.** The agent already checked the sha256 against flash, but only *after*
`esp_https_ota_finish()`, which is the call that moves the boot pointer. A mismatch then
put the pointer back. For the whole read-back, otadata named an image nobody had
verified. A power cut in that window booted it.

**What changed (agent 0.4.1).**

- `ff_ota.c::ota_task`: `verifying` → read back + hash → on a mismatch or read error,
  `esp_https_ota_abort()` and `failed`. Nothing has moved. On a match, the log says
  `… matches what is on flash in ota_1; switching the boot partition`, then `finish()`, then
  `ff_txn_save`, then `… is staged and bootable`, then `staged`. The mismatch line now ends
  `the boot partition was never moved`. `restore_boot_partition()` runs only for a failed
  `finish()`. Flash encryption is a compile-time `#error`, because the premise (no write
  buffering) depends on it being off. See DECISIONS 2026-10-03 for the IDF v5.5.5
  evidence.
- Slot-size guard: `size > ota slot` publishes `staging, failed "artifact larger than the
  ota slot"` before anything is fetched or erased.
- `ff_mqtt.c::on_stage`: `artifact.sha256` must be exactly 64 lowercase hex. Anything else
  gets one `failed "artifact sha256 malformed"`, with no `staging` and no download. The
  value is never normalised.
- The simulator refuses the same two commands in the same order.

**T1.** `just test` passed: 1003 tests, ruff, ruff format, and mypy. It includes the new
`tests/test_agent_verify.py` tripwires, which fail on the 0.4.0 source. It also includes
the reordered `test_agent_txn` check (verify < finish < `ff_txn_save` < `staged and
bootable` < `STAGED`) and the simulator tests for a malformed digest (four spellings) and
an oversize artifact. `just agent-build esp32` and `esp32s3` both ended in `BUNDLE OK`.
The app grew by +368 B and +400 B, and the budgets were raised to exactly those bytes.
`just agent-qemu-smoke esp32` gave `HARNESS OK`.

**T2 (real agent in QEMU, esp32, against the dev stack on 10.0.2.2:8088).** otadata is
`.qemu/flash-esp32.bin` at 0xF000/0x2000, read with QEMU stopped:
`dd if=.qemu/flash-esp32.bin bs=4096 skip=15 count=2 status=none | sha256sum | cut -c1-16`.
A `--fresh` board is not all-0xFF here. On its first boot it marks ota_0 VALID, giving
`seq=1 state=VALID` = `8ba3b110139f4544`. The comparison is before and after, so this does
not matter.

```
T2-0  negative control: saved 0.4.0 bundle, stage sha256 "b"*64, apply auto
  topic: staging, downloading, verifying, failed "sha256 mismatch"
  board: (35464) state=verifying
         (35504-37254) esp_image ×2 (finish(): esp_ota_end + set_boot_partition)
         (44504) sha256 MISMATCH, flash holds 5d2e6347…, the command says bbbb…
         (45604) boot partition put back to ota_0: the staged image was rejected …
  otadata 8ba3b110139f4544 -> f30f1c544a73a3fa  (seq 1 -> seq 2 = ota_1, seq 3 = ota_0)
  => R1 rewrote otadata twice; the pointer named the unverified ota_1 for ~8 s.

T2-1  0.4.1 (A), --fresh, same corrupt stage
  topic: staging, downloading, verifying, failed "sha256 mismatch"   (no staged/applying)
  board: (40974) state=verifying
         (48064) sha256 MISMATCH, flash holds 5d2e6347…, the command says bbbb… — the
                 boot partition was never moved
         'put back' count: 0; no esp_image lines after verifying (finish() never ran)
  otadata 8ba3b110139f4544 -> 8ba3b110139f4544, cmp of the 8192 bytes: BYTE-IDENTICAL
  cold boot: running partition: ota_0 … fw_version 0.4.1; GET /v1/devices: 0.4.1
  rows: staging, downloading, verifying, failed
  The pre-finish() read-back hashed to the artifact's real sha256 (5d2e6347…): every
  byte was on flash before finish(), as the IDF source says.

T2-2  uppercase B_SHA, then "abc"
  topic: exactly one status each: failed "artifact sha256 malformed"
  docker compose logs api --since t0 | grep -c 'GET /v1/artifact'  ->  0
  rows: failed / failed

T2-3  size 1966081, otherwise valid
  topic: staging, failed "artifact larger than the ota slot"
  board: the artifact is 1966081 bytes, slot ota_1 holds 1966080
  GET /v1/artifact since the publish -> 0

T2-4  happy path: POST /v1/devices/000000000000/deploy {"version":"0.4.2","apply":"on_command"} -> 202
  board: (79063) sha256 5d2e6347… matches what is on flash in ota_1; switching the boot partition
         (81453) ff-txn: transaction b75ac8d0… recorded (target slot at 0x00200000)
         (81453) ota_1 is staged and bootable
  otadata 8ba3b110139f4544 -> 8ee76b96c568c878  (seq 2 = ota_1: the switch, after verify)
  power cycle: running image: fw_version 0.4.2, ota state pending_verify
               transaction b75ac8d0…: confirming on ota_1 -> CONFIRMED -> record cleared
  rows: requested, staging, downloading, verifying, staged, confirming, confirmed|t
  GET /v1/devices: 0.4.2

T2-5  simulator against the live stack: just sim-fleet 1 --capabilities ota, deploy 1.5.0
  verify sha256 … matches -> staged -> applying -> rebooting -> confirming -> confirmed
```

**Spec proposal (not applied).** For `spec/device-protocol.md` → `dn/cmd`:
"`artifact.sha256` is exactly 64 lowercase hex characters. A device refuses any other
spelling with `failed` before downloading, and refuses an `artifact.size` larger than its
`ota_slot_size` before writing."

**Not covered here.** Real hardware. The prod board runs 0.3.1, and this was not deployed
there. A metal check can be a later bench item.

### A/B slot apply and the atomic switch (R2-fw-2)

**What R1 had (0.4.1, confirmed by reading the code and IDF v5.5.5).** `ota_task` took
`esp_ota_get_next_update_partition(NULL)` as `target`, checked its size and hashed it.
`esp_https_ota_begin()` was given no `partition.staging`, so it picked a slot again on its
own (`esp_https_ota.c` ~l.487-491). The first `perform()` ran `esp_ota_begin()`, which
refuses the running slot and, with rollback on, a running image in `PENDING_VERIFY`. Both
checks come before any erase. A failed `finish()` called `restore_boot_partition()`, which
called `esp_ota_set_boot_partition(running)` unconditionally. The switch itself is atomic
because of IDF: otadata is two sectors, and `esp_rewrite_ota_data()` writes the inactive
one. **That holds only while the two sectors name different slots.**

**The gaps.**

- **G1.** A stage during `PENDING_VERIFY` was refused late. It came after `downloading`
  was published and the signed URL was fetched, and it was reported as
  `"download failed"`. It is realistic: commands queued in the persistent session drain
  before the announce whose PUBACK confirms.
- **G2 (the defect).** A second `stage` after an `apply: "on_command"` stage wrote the slot
  the boot pointer already named. `esp_rewrite_ota_data()` picks the new seq with
  `while (seq > (id+1)%N + i*N) i++`, and equality stops that loop. So `finish()` wrote the
  **same** seq into the other sector, which was the running image's entry. Both sectors
  then named the staged slot. A rollback booted the broken image again, and the next
  timeout hit `ESP_ERR_OTA_ROLLBACK_FAILED`. The board was stuck.
- **G3.** The slot was chosen twice, by `ff_ota.c` and by esp_https_ota, and nothing held
  the two equal.
- **G4.** The undo after a failed `finish()` (`ESP_ERR_OTA_VALIDATE_FAILED`) ran although
  the pointer had never moved. The same equal-seq path wrote a duplicate `seq n → running`
  entry in state NEW over the previous image's entry.

**What changed (agent 0.4.2).**

- `ff_ota.c::choose_target_slot()` is now the one place the slot is chosen, and it runs
  before any I/O, right after `staging`. It does four things in this order:
  1. It refuses on exactly `ESP_OTA_IMG_PENDING_VERIFY` with
     `failed "the running image is not confirmed yet"`. A failed state read is allowed, so
     a serially flashed board stays deployable.
  2. It reads `esp_ota_get_boot_partition()`. If that is not the running slot, it refuses
     with `failed "an update is already staged and waits for a reboot"`, and logs the boot
     slot's label and ota state. A NULL boot partition fails closed.
  3. It takes `esp_ota_get_next_update_partition(NULL)`. This is the only call left in the
     file.
  4. It rejects a target that is the running slot or not an OTA app slot, as
     `"no spare ota slot"`.

  It never touches `ff_txn`, so the waiting stage's record stays valid.
- `.partition = {.staging = target}` goes to `esp_https_ota_begin()`. The slot that is
  written, hashed, switched to and recorded is one pointer. The `writing slot` log line now
  also prints the address.
- `restore_boot_partition()` reads the boot partition first. If it still names the running
  slot, the board logs `boot partition still names ota_0; nothing to undo — otadata was not
  touched` and writes nothing.
- The simulator (`StageRunner`) refuses the same two stages, after `staging` and before
  the size guard, with the same details (`pending`, and a new `staged` field that nothing
  clears).

**T1.** `just test` passed: 1012 tests, ruff, ruff format, and mypy. It includes the new
`tests/test_agent_ab_slots.py` (6 tripwires) and three simulator tests: a stage over an
`on_command` stage, a stage during `--confirm never`, and a stage accepted after the
rollback. `test_agent_verify.py` and `test_agent_txn.py` pass unchanged.
`just agent-build esp32` and `esp32s3` both ended in `BUNDLE OK`. The app grew by
+1,616 B and +1,664 B, and the budgets were raised to exactly those bytes.
`just agent-qemu-smoke esp32` gave `HARNESS OK` on 0.4.2. The `otadecode` helper from the
runbook decodes that fresh board as `sector0: seq=1 -> ota_0 state=VALID crc=ok`,
`sector1: empty`, hash `8ba3b110139f4544`, so the CRC formula is right.

**T2 (QEMU esp32 against the dev stack on :8088, 2026-10-03).** T2 ran with the api
recreated on the `.env.example` dev hash; `.env` untouched. Images: old = the saved 0.4.1
bundle (app sha256 `870004e9…`), A = 0.4.2 (this code, board only), B = this code as
`0.4.5` (`a236da1e…`), X = B with byte `0x40000` flipped, uploaded as `0.4.5-x`
(`08547f47…`), R = this code as `0.4.6-rbtest` (`FF_ROLLBACK_TEST=1`, `80699672…`).
otadata is decoded per sector with the runbook's `otadecode`, always with QEMU stopped.

```
T2-0  negative control: 0.4.1 (old), --fresh
  fresh board:  sector0: seq=1 -> ota_0 state=VALID crc=ok / sector1: empty  (hash 8ba3b110139f4544)
                -> the CRC formula is right
  G4  deploy 0.4.5-x on_command (reused:false)
      topic: staging, downloading, verifying, failed "image validation failed"
      board: (62968) sha256 08547f47… matches what is on flash in ota_1; switching the boot partition
             (63898) esp_image: Checksum failed. Calculated 0xd3 read 0x2c
             (63908) esp_ota_ops: New image failed verification
             (64998) boot partition put back to ota_0: the staged image was rejected …
      otadecode: sector0: seq=1 -> ota_0 state=VALID crc=ok
                 sector1: seq=1 -> ota_0 state=NEW crc=ok        <- duplicate entry, the pointer never moved
  G2  deploy 0.4.5 on_command -> staged (writing slot ota_1; Writing to <ota_1> partition at offset 0x200000)
      deploy 0.4.3 on_command (reused:false)
      board: writing slot ota_1 … Writing to <ota_1> partition at offset 0x200000 … staged and bootable
      topic: staging, downloading, verifying, staged;  GET /v1/artifact since the POST -> 1
      otadecode: sector0: seq=2 -> ota_1 state=NEW crc=ok
                 sector1: seq=2 -> ota_1 state=NEW crc=ok        <- both sectors name ota_1; ota_0's entry is gone
  => G2 and G4 were real on 0.4.1, exactly as the IDF source predicts.

T2-1  0.4.2 (A), --fresh (new token), deploy 0.4.5-x on_command
  fresh board: running image: fw_version 0.4.2, ota state valid; otahash 8ba3b110139f4544
  topic: staging, downloading, verifying, failed "image validation failed"   (no staged)
  board: (27732) writing slot ota_1 at 0x00200000 (1966080 bytes)
         (28002) esp_https_ota: Writing to <ota_1> partition at offset 0x200000
         (73792) sha256 08547f47… matches what is on flash in ota_1; switching the boot partition
         (74882) esp_image: Checksum failed. Calculated 0xd3 read 0x2c
         (74912) failed: image validation failed
         (74992) boot partition still names ota_0; nothing to undo — otadata was not touched
         'boot partition put back' count: 0
  otadecode: sector0: seq=1 -> ota_0 state=VALID crc=ok / sector1: empty
  otahash 8ba3b110139f4544 -> 8ba3b110139f4544: BYTE-IDENTICAL
  cold boot: running partition: ota_0, fw_version 0.4.2, ota state valid

T2-2  stage over a staged image (D2)
  deploy 0.4.5 on_command (reused:false) -> staged
  board: (26613) writing slot ota_1 at 0x00200000 (1966080 bytes)
         (26733) esp_https_ota: Writing to <ota_1> partition at offset 0x200000   <- same slot (D3)
         (63153) ff-txn: transaction 0925c853… recorded (target slot at 0x00200000)
  deploy 0.4.3 on_command (reused:false)
  topic: exactly staging, failed "an update is already staged and waits for a reboot"
  board: (108643) refused — the boot partition names ota_1 (ota state new) while ota_0 is
                  running; nothing was fetched or erased; the staged image boots at the next reset
  GET /v1/artifact since t0 -> 0
  otadecode: sector0: seq=1 -> ota_0 state=VALID crc=ok
             sector1: seq=2 -> ota_1 state=NEW crc=ok          <- two distinct seqs (compare T2-0 G2)
  cold boot: running partition: ota_1, fw_version 0.4.5, ota state pending_verify
             transaction 0925c853…: confirming on ota_1 -> announce ack -> record cleared
  rows: 0925c853 requested, staging, downloading, verifying, staged, confirming, confirmed|t
        575ca5ff requested, staging, failed|t "an update is already staged and waits for a reboot"

T2-3  stage while the running image is unconfirmed (D1), rollback still lands
  deploy 0.4.6-rbtest on_command -> writing slot ota_0 at 0x00020000; Writing to <ota_0> …
         0x20000; ff-txn: … recorded (target slot at 0x00020000); staged
  otadecode (before the boot): sector0: seq=3 -> ota_0 state=NEW / sector1: seq=2 -> ota_1 state=VALID
  cold boot: fw_version 0.4.6-rbtest, ota state pending_verify; confirming on ota_0;
             FF_ROLLBACK_TEST: ignoring the announce ack on purpose
  deploy 1.6.0 on_command (reused:false) at ~9 s into the 60 s window
  topic: exactly staging, failed "the running image is not confirmed yet"
  board: (9444) refused — ota_0 is still pending_verify (it confirms at its announce ack or
                rolls back); nothing was fetched or erased
         no esp_ota_begin failed, no Writing to <ota_1>
  GET /v1/artifact since t0 -> 0
  (68394) no working session 60 s after an OTA boot — … rolling back; rolling_back on the topic
  The emulator survived this esp_restart (SW_CPU_RESET), so no manual power cycle was needed:
         running partition: ota_1, fw_version 0.4.5, ota state valid
         transaction a4d134c5…: rolled_back (returned to ota_1; ota_0 did not confirm)
  GET /v1/devices: 0.4.5
  rows: a4d134c5 … staged, confirming, rolling_back, rolled_back|t "returned to ota_1; ota_0 did not confirm"
        036ebbbd requested, staging, failed|t "the running image is not confirmed yet"

T2-4  after the rollback, the next deploy targets the rejected slot
  otadecode: sector0: seq=3 -> ota_0 state=INVALID crc=ok / sector1: seq=2 -> ota_1 state=VALID crc=ok
  deploy 0.4.3 on_command -> reused:true (cmd 6062f183, left open at staged by T2-0's
         0.4.1 board, which never rebooted; this flash had never seen that id, so it ran)
  board: (27456) writing slot ota_0 at 0x00020000 (1966080 bytes)
         (27586) esp_https_ota: Writing to <ota_0> partition at offset 0x20000
         (62626) ff-txn: transaction 6062f183… recorded (target slot at 0x00020000); staged
  otadecode: sector0: seq=3 -> ota_0 state=NEW crc=ok          <- INVALID entry replaced, seq above ota_1's 2
             sector1: seq=2 -> ota_1 state=VALID crc=ok        <- unchanged
  cold boot: fw_version 0.4.3, ota state pending_verify; confirming on ota_0; record cleared
  rows: 6062f183 … confirming, confirmed|t

T2-5  simulator against the live stack (api on localhost URLs + dev hash)
  sim-fleet 1 --capabilities ota (rfw2a-01 = 565e32227121)
    1.5.0 on_command: staging, downloading, verifying, staged (download #1)
    1.6.0 on_command: staging, failed "an update is already staged and waits for a reboot"
                      (stage 315e1ef3… is staged and waits for a reboot — refused, nothing fetched)
  sim-fleet 1 --confirm never --confirm-timeout 30 (rfw2b-01 = 22a07db944ee)
    1.5.0 auto: … rebooting, confirming
    1.6.0 on_command inside the window: staging, failed "the running image is not confirmed yet"
    then rolling_back, rolled_back|t "returned to 1.4.2; the new image did not confirm"; 1 download total
```

**Spec proposal (not applied).** For `spec/device-protocol.md` → `dn/cmd` (`stage`): "A
device refuses a `stage` with `failed`, before downloading anything, while its running
image has not yet confirmed (`confirming`), or while an image it staged earlier is waiting
for a reboot. The server may re-issue the deploy once the device reports a terminal state
or has rebooted."

### The dashboard shows the outcome (R2-fe-1) — **LANDED 2026-10-03**

Frontend only. `GET /v1/devices` already carried `deploy.state` `confirmed` / `rolled_back`
with `is_terminal: true` (R2-be-1); the cell said the wrong thing about them.

**Three defects fixed.** (1) A rollback read as a success: `rolled back to the previous
version → 1.5.0` points at the version that FAILED. (2) A confirm printed `100%`, because the
agent and simulator publish `pct: 100`. (3) There was no glanceable verdict, and
`done — running the new version` was printed even when the board's announce said otherwise
(the CUJ-1 hard-fail trap in `spec/cujs.md`).

**Rendering** (`deploy.ts::deployOutcome`, `DeployCell.tsx::LiveState`), only when the server
says `is_terminal` and the state has an entry in `DEPLOY_OUTCOMES`:

| Case | `deploy-state` | rest |
|---|---|---|
| `confirmed`, `fw_version === artifact_version` | `good` (`ok`) | ` — running {v}`, no arrow, no pct |
| `rolled_back` | `rolled back` (`bad`) | ` — {v} did not confirm`, plus `; back on {from}` only if the announce equals `from` |
| `confirmed`, announce differs (drift) | `confirmed by the board` (unstyled) | ` → {v}` and a `deploy-drift` line saying what the board reports now |
| anything else | label, as before | arrow and pct as before |

**Drift drops the verdict.** `good` is present tense; a board re-flashed over USB is no longer
running what it confirmed. The client does not turn that into a `rolled back` either, since
the server authors outcomes. `DEPLOY_STATE_LABELS.confirmed` is now `confirmed by the board`.

**Deliberately not done.** No per-device history, no last outcome kept across a new in-flight
deploy, no outcome for transition-gap `rebooting` rows (nothing is inferred from `fw_version`
for a non-terminal deploy), and no verdict word for `failed` (its label already is the word).

**T2 evidence** (dev stack, real Chromium via Playwright, dashboard opened before the deploy and
not reloaded). Artifact `esp32c6/2.0.0-fe1` (204800 random bytes, 201); `fe1good-01 =
b26a938324ab`, `fe1bad-01 = ea7905b589a8 --confirm never --confirm-timeout 10`.

```
{"device_id":"b26a938324ab","fw_version":"2.0.0-fe1","s":"confirmed","t":true,"v":"2.0.0-fe1","from":"1.4.2","pct":100,"detail":null}
{"device_id":"ea7905b589a8","fw_version":"1.4.2","s":"rolled_back","t":true,"v":"2.0.0-fe1","from":"1.4.2","pct":null,"detail":"returned to 1.4.2; the new image did not confirm"}
in flight (bad):  confirming the new image → 2.0.0-fe1 (just now)
good, no reload:  good — running 2.0.0-fe1 (12 s ago)                 class ok,  Firmware 2.0.0-fe1
bad,  no reload:  rolled back — 2.0.0-fe1 did not confirm; back on 1.4.2 (just now)
                  returned to 1.4.2; the new image did not confirm    class bad, Firmware 1.4.2
after F5:         same two verdicts. No progressbar, no null/undefined in any cell.
drift (good board restarted with --fw-version 1.4.2, no reload, ~15 s):
                  confirmed by the board → 2.0.0-fe1 (36 s ago)       no class
                  the board has since reported 1.4.2, so this is not what it runs now; Firmware 1.4.2
```

Dev-stack note: the `frontend` container had been created from the production nginx image, not
the Vite dev target, so edits were not served. `docker compose up -d --no-deps --build frontend`
recreated it on the dev override.


### Remaining failure modes (R2-test-1)

**A test task: fault-injection builds, a flash-tear tool, tripwires and QEMU evidence. No
firmware defect is fixed here.** Proof status: **proven in QEMU (esp32, dev stack), bench
replay owed** (`../runbooks/rollback-test.md` → *Boot loop*, *Pull the plug
mid-download*). QEMU completes every SPI flash command atomically, so a SIGKILL is a
power cut between flash commands, never inside one. That is why the torn otadata write
(F4) is produced offline with `just agent-qemu-otadata`.

| # | Mode | Observed in QEMU | Reported to the server |
|---|------|------------------|------------------------|
| F1 | Boot loop: the new image aborts at every boot (`FF_FAULT_TEST=bootloop`) | **Recovers after ONE abort.** The bootloader marks the slot `ABORTED` and loads the old one. | `rolled_back`, terminal |
| F2 | Power cut mid-download (30 %) | **Recovers.** Same slot, `valid`, no transaction line. | No. The row parks at `downloading`. A repeat POST reuses the cmd_id and runs to `confirmed`. |
| F3 | Power cut during the sha256 read-back (`verifying`) | **Recovers.** As F2. | No. The row parks at `verifying`. |
| F4 | Power cut inside an otadata write (a torn sector, erased or crc-less) | **Lands on the previous VALID image.** One `rst:` banner, no loop. | No. `stale transaction record … discarded`, and the row parks at `staged`. |
| F4d | The same cut while mark-valid rewrites the confirmed entry | **Silently reverts** to the image that ran before. | No. The server keeps the last thing it heard (`confirmed` here; in a real cut, `confirming`). R2-fe-1's drift line shows the mismatch. |
| F5 | Hang before the broker session (`FF_FAULT_TEST=hang`) | **DOES NOT RECOVER.** 333 s in `PENDING_VERIFY`, no reset, no rollback. Only the next power cycle rescues it. | Nothing until that power cycle, then `rolled_back` → **R2-fw-4** → fixed by R2-fw-4 (0.4.3), see below |

**Liveness gaps, accepted.** F2, F3 and F4 leave the server row non-terminal. No sweeper
or server-side expiry closes it, because `deploys.py` rule 1 says the server records only
what a board said. The operator sees a row that stopped. A repeat deploy within the
signed-URL TTL reuses the cmd_id and finishes it (F2). F4d is the uglier one: a board
that was about to confirm reverts silently. That is safe, but not reported.

**Two IDF facts the run surfaced (v5.5.5).**

- **otadata is rewritten in place more often than not.** `rewrite_ota_seq()` erases a
  4 KB sector, then programs 32 bytes. `esp_rewrite_ota_data()` (in `finish()`) aims it at
  the INACTIVE sector. `esp_ota_current_ota_is_workable()`, behind both mark-valid and
  mark-invalid, aims it at the **ACTIVE** sector. The bootloader's own `write_otadata()`
  (`bootloader_utility.c`: NEW → PENDING_VERIFY on the active sector, PENDING_VERIFY →
  ABORTED in place) does the same. A cut inside any of those leaves the other sector, which
  names the image that ran when the stage happened. F4 proves that is where the board lands.
- **otadata moves at the START of every stage.** With rollback enabled, `esp_ota_begin()`
  calls `esp_ota_invalidate_inactive_ota_data_slot()`. That erases the inactive sector
  whenever it names a slot other than the running one, before the first byte is
  downloaded. The plan predicted a byte-identical otadata after a mid-download cut. What
  holds is narrower: the **active** sector is byte-identical (F3: `27d37171903e3637` before
  and after), and the inactive one is erased (F2: `seq=2 -> ota_1 ABORTED` → empty; F3:
  `seq=1 -> ota_0 VALID` → empty). It is safe, because the erased entry never names the
  running image.

**T1.** `just test` passed: ruff, ruff format, mypy and the full pytest. That includes
`tests/test_agent_fault_injection.py` (9 tripwires: the switch's two values and its
FATAL_ERRORs, the defines' guards, the Dockerfile default, every `abort()` inside the
preprocessor region, the hook's position in `app_main`, no other source naming the switch,
no recipe building a fault image) and `tests/test_agent_otadata_tool.py` (8). The crc test
pins `0x4743989A`, read off the fresh board's flash (`xxd -s 0xF000 -l 32`:
`0100 0000 ffff … 0200 0000 9a98 4347`). It also includes the publish guard
(`TestFaultTestBuildsAreNeverPublished`, one case per suffix with zero store writes, plus
0.4.2 still publishing). The size budgets in `test_agent_power_and_size.py` passed
**unchanged**, so the hook is absent from normal builds. `just agent-build esp32` and
`esp32s3` both gave `BUNDLE OK` (0.4.2). Scratch builds compiled under `-Werror`:
`bootloop`/`hang` for esp32 (`0.4.21-bltest`, `0.4.22-hangtest`) and for esp32s3
(`0.4.2-bltest`, `0.4.2-hangtest`). `just agent-qemu-smoke esp32` gave `HARNESS OK`.

**T2-build.** `config_sha256` is `8c8ae96b473a6209…` for `agent/dist/esp32` and for
every esp32 scratch build (both fault builds, 0.4.20, 0.4.23, 0.4.24). It is `d10f52d642b57435…`
for `agent/dist/esp32s3` and both s3 fault builds. `FF_FAULT_TEST=bogus` failed with
`FF_FAULT_TEST must be 'bootloop' or 'hang', got 'bogus'`. `FF_FAULT_TEST=hang` with
`FF_ROLLBACK_TEST=1` failed with `FF_FAULT_TEST and FF_ROLLBACK_TEST are exclusive: one fault
per image`. `python -m fleetforge.firmware publish` on the bootloop bundle verified it, then
refused: `esp32 agent 0.4.21-bltest is a -bltest build: fault-test builds are deployable
artifacts only, never flasher catalog bundles`. `just agent-list` was unchanged. Both fault
apps are about 150 KB (151,424 and 151,440 B), not 1 MB. Everything after the hook is dead
code, and `--gc-sections` drops it.

**T2 (QEMU esp32 against the dev stack on :8088, 2026-10-03).** The api was recreated on
the `.env.example` dev hash with the `10.0.2.2` origins, and put back afterwards. `.env`
was not touched. Board A = 0.4.2 of this code, `--fresh`. Artifacts: B `0.4.20`
(`20360062…`), L `0.4.21-bltest` (`a80d8837…`), H `0.4.22-hangtest` (`60ea55ba…`),
C `0.4.23` (`b4ecaa4c…`), D `0.4.24` (`50f4898d…`). otadecode = `just agent-qemu-otadata
esp32`, always with QEMU stopped.

```
A     fresh: ota_0, fw 0.4.2, ota state valid
      otadecode: sector0: seq=1 -> ota_0 state=VALID crc=ok / sector1: empty

F1    deploy 0.4.21-bltest on_command (cmd 8660927a) -> staged (ota_1); stop, start
        boot: Loaded app from partition at offset 0x200000
        ff-agent: running partition: ota_1 … / fw_version 0.4.21-bltest, ota state pending_verify
        E ff-agent: FF_FAULT_TEST=bootloop: aborting on purpose, on every boot. …
        abort() was called at PC 0x400d59eb on core 0
        rst:0xc (SW_CPU_RESET)
        boot: Loaded app from partition at offset 0x20000          <- the bootloader put ota_0 back
        ff-agent: running partition: ota_0 / fw_version 0.4.2, ota state valid
        W ff-mqtt: transaction 8660927a…: rolled_back (returned to ota_0; ota_1 did not confirm)
        ff-txn: transaction 8660927a… closed — record cleared
      (no esp_restart() panic this time, so the first cycle already reported)
      stop, start: Loaded app … 0x20000, ota_0 valid, no transaction line
      'FF_FAULT_TEST=bootloop' count: 1;  'no working session': 0
      every Loaded app after the abort: 0x20000 (2 of 2)
      otadecode: sector0: seq=1 -> ota_0 state=VALID crc=ok
                 sector1: seq=2 -> ota_1 state=ABORTED crc=ok     <- ABORTED: the bootloader, not our timer
      rows: requested, staging, downloading, verifying, staged, rolled_back|t
            "returned to ota_0; ota_1 did not confirm"           (no confirming)
      GET /v1/devices: 0.4.2

F2    otadata before: sector1 = seq=2 ota_1 ABORTED (F1)
      deploy 0.4.20 on_command (cmd 1c5fb04a); stop at "update 1c5fb04a…: 30% (308224 bytes)"
      otadecode: sector0: seq=1 -> ota_0 state=VALID crc=ok / sector1: empty
        (sector0 unchanged; sector1 erased by esp_ota_begin before the download, see above)
      ota_1's first 4096 B == B's app.bin;  full prefix differs at byte 311297  <- a real mid-write cut
      cold start: Loaded app … 0x20000, ota_0, fw 0.4.2, ota state valid, no transaction line
      rows: requested, staging, downloading                       <- non-terminal, by design
      retry: same POST -> reused:true, cmd 1c5fb04a; downloads again -> staged; stop, start
        ota_1 pending_verify -> confirming on ota_1 -> CONFIRMED -> record cleared
      rows: requested, staging, downloading, verifying, staged, confirming, confirmed|t (each once)
      GET /v1/devices: 0.4.20

F3    otadecode before: sector0: seq=1 -> ota_0 VALID / sector1: seq=2 -> ota_1 VALID
        per-sector hashes 9749381a19fe46e3 / 27d37171903e3637
      deploy 0.4.23 on_command (cmd 09de97e2); stop the moment up/status says verifying
      board: last line "update 09de97e2…: 100% (1017408 bytes)"; 'matches what is on flash': 0
      ota_0 holds all of C: the cut landed inside the read-back
      otadecode: sector0: empty / sector1: seq=2 -> ota_1 state=VALID crc=ok
        per-sector hashes f47a8ec3e9aff231 / 27d37171903e3637    <- active sector byte-identical
      cold start: Loaded app … 0x200000, ota_1, fw 0.4.20, valid, no transaction line
      rows: requested, staging, downloading, verifying            <- non-terminal

F4    deploy 0.4.24 on_command (cmd be12816f) -> staged into ota_0; stop; snapshot
      otadecode: sector0: seq=3 -> ota_0 state=NEW crc=ok / sector1: seq=2 -> ota_1 state=VALID crc=ok
  (a) tear --sector newest --mode erased -> sector0: empty
      boot: Loaded app … 0x200000; ota_1, fw 0.4.20, valid
            W ff-mqtt: stale transaction record for be12816f… — discarded;  rst: banners 1
  (b) restore; tear --sector newest --mode partial -> sector0: seq=3 -> ota_0 state=NEW crc=BAD
      boot: identical to (a);  rst: banners 1
  (c) restore untouched (the control)
      boot: Loaded app … 0x20000; fw 0.4.24, ota state pending_verify
            confirming on ota_0 -> CONFIRMED -> record cleared
      rows: … staged, confirming, confirmed|t;  GET /v1/devices: 0.4.24
      otadecode: sector0: seq=3 -> ota_0 state=VALID crc=ok      <- rewritten IN PLACE
  (d) tear --sector newest --mode erased (the confirmed entry)
      boot: Loaded app … 0x200000; ota_1, fw 0.4.20, valid, no transaction line; rst: banners 1
      GET /v1/devices: 0.4.20, while the row still says confirmed|t   <- silent revert

F5    deploy 0.4.22-hangtest on_command (cmd f0d0e7e8) -> staged into ota_0; stop, start
        boot: Loaded app … 0x20000; fw 0.4.22-hangtest, ota state pending_verify
        E (3707) ff-agent: FF_FAULT_TEST=hang: app_main is stuck before the mqtt session, …
        … every 30 s … E (333707) ff-agent: FF_FAULT_TEST=hang: …   (12 lines)
      at 333 s: container up, rst: banners 1, 'no working session' / 'Rollback to': 0
      rows: requested, staging, downloading, verifying, staged    <- nothing more, ever
      stop -> otadecode: sector0: seq=3 -> ota_0 state=PENDING_VERIFY crc=ok   <- never resolved
      start (the human rescue): Loaded app … 0x200000; ota_1, fw 0.4.20, valid
        transaction f0d0e7e8…: rolled_back (returned to ota_1; ota_0 did not confirm)
      rows: … staged, rolled_back|t
```

**F5 is filed as R2-fw-4 (P0).** `arm_confirm_timeout()` runs in `ff_mqtt_run()` and
nowhere else. Before that, `app_main` can wait forever: `ff_net_bring_up` retries
forever, enrollment retries forever, and every `park()` loops forever.
`CONFIG_ESP_TASK_WDT_PANIC` is not set, so a wedged task does not reset either. This
contradicts `spec/prd.md` Flow 2 step 3 ("must reconnect within a timeout … else
auto-rollback"). The fix touches the CRITICAL confirm path, so it gets its own task and
review.

### Arm the confirm timer at boot (R2-fw-4)

**The F5 fix. Proof status: proven in QEMU (esp32, dev stack), bench replay owed**
(`../runbooks/rollback-test.md` → *Hang before the session*). Decision and rejected
alternatives: DECISIONS.md 2026-10-03 (R2-fw-4).

**What changed (agent 0.4.3).**

- `ff_mqtt_arm_confirm_timer()` (ff_mqtt.c, the old `arm_confirm_timeout()` made public) is
  the **first statement of `app_main`**, before `log_power_fault()`, `log_boot_facts()` and
  the fault hook. `ff_mqtt_run()` no longer arms it. Nothing in app_main can now outwait the
  timer: network bring-up, enrollment and `park()` all run after it.
- It needs only esp_timer and otadata, and stays inert unless the running image is
  `PENDING_VERIFY`. A serially flashed or confirmed board still never reboots on failure.
- A timeout before `classify_txn()` has run sees `TXN_NONE` and rolls back immediately and
  unreported. The image the board returns to reports `rolled_back` from the record the
  previous image wrote at `staged`.
- Two additions inside the arm: an idempotence guard, and an `ESP_LOGE` when the confirm
  timer cannot be created (silent before). The boot line now reads `OTA boot: %d s from now
  to reach the fleet or roll back`. The confirm/rollback decisions themselves are unchanged.
- **Behaviour change:** the 300 s now counts from the moment the image starts executing. A
  good image whose AP or broker stays down for more than 300 s after its first boot rolls
  back (a miss, not a brick). `FF_ROLLBACK_TEST`'s 60 s counts from boot too.
- `CONFIG_ESP_TASK_WDT_PANIC` was considered and not enabled (DECISIONS). No
  `sdkconfig.defaults*` changed: `config_sha256` is still `8c8ae96b…3bd49a` (esp32) and
  `d10f52d6…438e32` (esp32s3). The simulator is deliberately unchanged.

**T1.** `just test` passed (ruff, ruff format, mypy, 1040 tests). New tripwires in
`tests/test_agent_txn.py`: the arm is the first statement of `app_main` and occurs once;
`ff_mqtt_run()` no longer arms it and the old name is gone; only `agent_main.c` calls it;
its body touches no `nvs_`, `ff_txn_`, `esp_mqtt_client_`, `s_ctx.cfg`, `s_ctx.client` or
`ff_progress_`, and still checks `pending_verify()` before `esp_timer_start_once`; it is
guarded by `s_confirm_armed`. In `tests/test_agent_fault_injection.py`: the arm comes
before `#if FF_FAULT_TEST_BOOTLOOP`, which is what makes `-hangtest` the regression image.
`just agent-build esp32` and `esp32s3` gave `BUNDLE OK`, agent 0.4.3, `-Werror` clean.
App sizes 1,017,696 B (esp32, +288) and 998,064 B (esp32s3, +304); the budgets were raised
to those bytes.

**T2 (QEMU esp32 against the dev stack on :8088, 2026-10-03).** The api was recreated on
the `.env.example` dev hash with the `10.0.2.2` origins, and put back afterwards. All images
were built from this code: board A `0.4.3` (`--fresh`), H `0.4.31-hangtest` (`09ae896f…`,
200,064 B), B `0.4.30` (`23bd1316…`), R `0.4.32-rbtest` (`2c7fbca7…`). otadecode =
`just agent-qemu-otadata esp32`, always with QEMU stopped.

```
A     fresh: Loaded app … 0x20000; fw 0.4.3, ota state valid; enroll 200; mqtt connected
      'OTA boot:' count: 0                                         <- inert on a serially flashed board

T2-1  THE FIX. deploy 0.4.31-hangtest on_command (cmd 1a9cac56, reused:false)
        -> ota_1 is staged and bootable; stop
      otadecode: sector0: seq=1 -> ota_0 state=VALID / sector1: seq=2 -> ota_1 state=NEW
      start, then hands off:
        I (1669)   boot: Loaded app from partition at offset 0x200000
        W (1993)   ff-mqtt: OTA boot: 300 s from now to reach the fleet or roll back
        I (2003)   ff-agent: fleetforge agent 0.4.31-hangtest …
        I (2043)   ff-agent: running image: fw_version 0.4.31-hangtest, ota state pending_verify …
        E (2053)   ff-agent: FF_FAULT_TEST=hang: app_main is stuck before the mqtt session; …
        … every 30 s … E (272053) ff-agent: FF_FAULT_TEST=hang: …   (10 lines)
        E (302053) ff-mqtt: no working session 300 s after an OTA boot — marking this image
                   invalid and rolling back to the previous slot
        I (302983) esp_ota_ops: Rollback to previously worked partition.
        rst:0xc (SW_CPU_RESET)                                     <- no esp_timer_impl_init panic this time
        I (17391)  boot: Loaded app from partition at offset 0x20000
        I (17739)  ff-agent: fleetforge agent 0.4.3 … ota state valid
        W (26179)  ff-mqtt: transaction 1a9cac56…: rolled_back (returned to ota_0; ota_1 did not confirm)
        I (27129)  ff-txn: transaction 1a9cac56… closed — record cleared
      stop -> otadecode: sector0: seq=1 -> ota_0 state=VALID crc=ok
                         sector1: seq=2 -> ota_1 state=INVALID crc=ok  <- INVALID: our timer, not a reset
      rows: requested, staging, downloading, verifying, staged, rolled_back|t
            "returned to ota_0; ota_1 did not confirm"            (no confirming, no rolling_back)
      GET /v1/devices: fw 0.4.3

T2-2  deploy 0.4.30 on_command (cmd 61915e31, reused:false) -> staged into ota_1; stop, start
        I (1866)   boot: Loaded app from partition at offset 0x200000
        W (2164)   ff-mqtt: OTA boot: 300 s from now to reach the fleet or roll back
        I (2214)   … fw_version 0.4.30, ota state pending_verify
        W (7934)   ff-mqtt: transaction 61915e31…: confirming on ota_1
        W (8224)   ff-mqtt: this image was written by OTA and is now CONFIRMED …
        I (8964)   ff-txn: transaction 61915e31… closed — record cleared
        I (330384) ff-mqtt: publish …/up/hb (… uptime 322 s)      <- past 320 s of log time
      'no working session': 0;  rst: banners: 1 (the power-on)
      rows: … staged, confirming, confirmed|t;  GET /v1/devices: 0.4.30, online
      stop -> otadecode: sector1: seq=2 -> ota_1 state=VALID crc=ok

T2-3  deploy 0.4.32-rbtest on_command (cmd c8623628, reused:false) -> staged into ota_0; stop, start
        I (2003)   boot: Loaded app from partition at offset 0x20000
        W (2372)   ff-mqtt: OTA boot: 60 s from now to reach the fleet or roll back
        I (2432)   … fw_version 0.4.32-rbtest, ota state pending_verify
        W (10252)  ff-mqtt: transaction c8623628…: confirming on ota_0
        E (10392)  ff-mqtt: FF_ROLLBACK_TEST: ignoring the announce ack on purpose …
        E (62412)  ff-mqtt: no working session 60 s after an OTA boot — …
        I (62422)  ff-mqtt: publish …/up/status (qos 1, retain, queued …) state=rolling_back
        I (65432)  esp_ota_ops: Rollback to previously worked partition.
        rst:0xc -> Loaded app … 0x200000 -> esp_timer_impl_init panic loop (the known QEMU limit)
      topic: … staged, confirming, rolling_back                   <- rolling_back before the reset
      stop -> otadecode: sector0: seq=3 -> ota_0 state=INVALID crc=ok / sector1: seq=2 -> ota_1 state=VALID
      start: Loaded app … 0x200000; fw 0.4.30, valid
        W (9585)   ff-mqtt: transaction c8623628…: rolled_back (returned to ota_1; ota_0 did not confirm)
      rows: … staged, confirming, rolling_back, rolled_back|t;  GET /v1/devices: 0.4.30
```

`confirming` still arrived well inside the 60 s (at 10 s), so behaviour change 4 did not
show. The negative control (T2-4, the pre-0.4.3 `0.4.22-hangtest` sitting in
`PENDING_VERIFY` past 330 s) was not re-run: it is F5 above.

### Flaky link (R2-test-2)

**A spike: a link impairment tool, QEMU evidence, two new tasks. No firmware changed.**
Proof status: **proven in QEMU (esp32, dev stack), bench replay owed**
(`../runbooks/rollback-test.md` → *Marginal radio*). Decision: DECISIONS.md 2026-10-03
(R2-test-2). Replay: `../runbooks/agent-qemu.md` → *Driving a flaky link*.

**The question as asked has a structural answer: a marginal radio cannot stall the
download past the confirm timer, because the two never run at the same time.**

1. The download runs on the **running** image, which is VALID (or UNDEFINED, serially
   flashed). `ff_mqtt_arm_confirm_timer()` arms the 300 s timer only when
   `pending_verify()` is true, so no timer exists during a download.
2. R2-fw-2 forbids the overlap from the other side. `ff_ota.c::choose_target_slot()`
   refuses a `stage` while the running image is `PENDING_VERIFY`, before any I/O.
3. The timer runs only in the **new** image, from its first instruction (R2-fw-4). There a
   flaky link matters differently: the image must reach its broker session within 300 s
   of boot or it rolls back. Download speed plays no part in that.

What a flaky link really does, measured:

| # | Scenario (proxy schedule) | Observed in QEMU | Safety | Liveness |
|---|---|---|---|---|
| D1 | Slow download (store `throttle:8192`) | 124.7 s download, `staged`. No `OTA boot` line, no `no working session` on the running image. Confirmed after stop/start. | safe | fine |
| D2 | Outage mid-download, 90 s (store blackhole 20→110 s) | Progress paused 90 s, resumed, `matches what is on flash`, `staged`, no `failed`. No `esp_transport_read` warning in the stall: the 20 s read timeouts are `-ESP_ERR_HTTP_EAGAIN`, logged at debug. Confirmed. *(1 of 3 runs hit the QEMU-only openeth panic instead. See below. Run 3 also passed: 100 % at 161.3 s, confirmed.)* | safe | fine (slow) |
| D3 | Silent far end, 600 s (store blackhole 20→620 s, then `reset`) | **The download never ended by itself.** 600 s at 10 %, no `failed`. The update slot stayed taken: a deploy of another version got `failed: another update is already in progress`. Only the reset ended it (D4). → **R2-fw-5** | safe | **lost until something closes the socket** |
| D4 | Far end closes mid-download (the D3 reset) | `data read -1, errno 128` → `update <cmd> failed: download failed`. Same slot, `ota state valid`, no transaction line. The next deploy (a new cmd_id) ran to `confirmed`. | safe | fine |
| P1 | New image boots into a 200 s outage | `OTA boot` and `confirming on ota_1` before the session. Announce acked at 196.9 s of log time, ~0.1 s after the link returned. `CONFIRMED`. | safe | fine |
| P2 | New image boots into a 330 s outage | `no working session 300 s after an OTA boot` at 302 218 ms, `rolling_back … is not reported`, reset. Old slot reports `rolled_back`. otadecode: new slot `INVALID`. | safe | good image rolled back (a miss) |
| P3 | New image boots into a flapping link (5 s up / 25 s down) | Confirmed in the first up-window: acked at 26 983 ms. | safe | fine |

**The invariant held in every scenario.** The board always ended on a VALID image. No
image stayed `PENDING_VERIFY` past 302 s, and no human action was needed beyond the
documented QEMU stop/start.

| # | Final slot (otadecode) | Terminal row | Longest `PENDING_VERIFY` |
|---|---|---|---|
| D1 | ota_1 VALID (0.4.50) | `confirmed` | 11.6 s (the confirm boot) |
| D2 | ota_0 VALID (0.4.51) | `confirmed` | 13.1 s |
| D3/D4 | ota_0 VALID (0.4.51), then ota_1 VALID (0.4.50) after the recovery deploy | `failed` (see the re-POST finding), recovery `confirmed` | 0 during the hold; 10.9 s for the recovery |
| P1 | ota_1 VALID (0.4.50) | `confirmed` | 197 s |
| P2 | ota_1 VALID (0.4.50); ota_0 INVALID | `rolled_back` | 305.3 s (timer at 302.2 s + the reset path) |
| P3 | ota_0 VALID (0.4.51) | `confirmed` | 27.2 s |

**Measured numbers.**

| What | Value |
|---|---|
| Announce ack after an all-pass OTA boot (t_base) | 11.4 s (D1), 12.9 s (D2), 10.7 s (D3 recovery) |
| D1 download, 1 017 696 B at 8192 B/s | 124.7 s (0 % at 8.6 s, 100 % at 133.3 s) |
| D2 download, 16 384 B/s with a 90 s hole | 152 s (0 % at 10.2 s, 100 % at 162.5 s); progress resumed ≤ 0.6 s after the link |
| D3 hold with no progress and no failure | 600 s (until the reset) |
| Boot path in progress reports during an outage | ~10 s (two 5 s `ff_progress` timeouts before `connecting`) |
| esp-mqtt retry cadence during an outage | an attempt every 20–25 s (10 s connect timeout + reconnect wait) |
| P1 reconnect latency after the link returned | ~0.1 s (an attempt was in flight); worst case ≈ one retry gap, ≤ ~15 s |
| P2 timer fire vs arm | arm 2.18 s, fire 302.22 s (300.04 s), reset at 305.27 s |

So the threshold is plain: an outage after the reboot that ends later than about 300 s
minus one retry gap (≈ 285 s) after boot rolls a good image back. That is the R2-fw-4
behaviour change 2, a miss and not a brick.

**Transcripts (trimmed).** Proxy lines are `[s since proxy start]`. App lines are
`I (ms since boot)`. The two clocks are 2-4 s apart.

```
D1  store: throttle:8192
      I (8599)   ff-ota: update 2482a5de…: 0% (1024 bytes)
      I (133279) ff-ota: update 2482a5de…: 100% (1017696 bytes)
      I (141549) ff-ota: … sha256 617316…ab71 matches what is on flash in ota_1; switching the boot partition
      I (144259) ff-ota: … ota_1 is staged and bootable
      'OTA boot: 300' / 'no working session' in this boot: 0
    stop, start (all-pass)
      I (1941)   boot: Loaded app from partition at offset 0x200000
      W (2261)   ff-mqtt: OTA boot: 300 s from now to reach the fleet or roll back
      W (11261)  ff-mqtt: transaction 2482a5de…: confirming on ota_1
      I (11401)  ff-mqtt: announce acknowledged by the broker              <- t_base
      W (11571)  ff-mqtt: this image was written by OTA and is now CONFIRMED …
    rows: requested, staging, downloading, verifying, staged, confirming, confirmed|t

D2  store: throttle:16384, 20=blackhole, 110=throttle:16384   (run 2; reused cmd 103a6884)
      [  14.343] conn 1 open 19000 -> 127.0.0.1:9000
      [  20.002] mode throttle:16384 -> blackhole
      I (10231)  ff-ota: update 103a6884…: 0% (1024 bytes)
      … nine heartbeats, nothing else …
      [ 110.082] mode blackhole -> throttle:16384
      I (106571) ff-ota: update 103a6884…: 10% (105021 bytes)
      I (162501) ff-ota: update 103a6884…: 100% (1017696 bytes)
      I (170621) ff-ota: … sha256 f939f6…4c8f matches what is on flash in ota_0; …
      I (173441) ff-ota: … ota_0 is staged and bootable
      [ 176.097] conn 1 closed (up 420 B, down 1018329 B)
    stop, start: confirming on ota_0 (12747), announce acknowledged (12917), CONFIRMED
    rows: … verifying, staged, confirming, confirmed|t

D3  store: throttle:16384, 20=blackhole, 620=reset   (cmd 8dc3771c, 0.4.50)
      [  12.338] conn 1 upstream connected
      [  20.003] mode throttle:16384 -> blackhole
      I (15337)  ff-ota: update 8dc3771c…: 10% (103424 bytes)          <- the last progress line
    hold +60 s: POST 0.4.24 -> 202, cmd 87760698, reused:false
      topic: 87760698 failed "another update is already in progress"
    hold +65 s: POST 0.4.50 -> 202, cmd 8dc3771c, reused:true
      I (82487)  ff-mqtt: ff/v1/d/000000000000/dn/cmd id=8dc3771c… type=stage
      topic: 8dc3771c failed "another update is already in progress"   <- NOT deduplicated: see below
    presence online and up/hb every 10 s throughout (MQTT is not impaired)
      [ 620.102] reset: aborted 1 connections (mode stays blackhole)
      W (616747) HTTP_CLIENT: esp_transport_read returned:-1 and errno:128
      E (616747) esp_https_ota: data read -1, errno 128
      E (616757) ff-ota: update 8dc3771c… failed: download failed
      topic: 8dc3771c failed "download failed"                          <- dropped: the row was already terminal
    stop -> otadecode: sector0: seq=3 -> ota_0 state=VALID crc=ok / sector1: empty
    start: Loaded app … 0x20000, fw 0.4.51, ota state valid, no transaction line
    POST 0.4.50 -> cmd dca1527a, reused:false -> staged -> stop/start -> confirmed|t

P1  stage 0.4.50 (cmd 783e76da) all-pass, stop; one proxy "0=blackhole,200=pass"; start
      W (2262)   ff-mqtt: OTA boot: 300 s from now to reach the fleet or roll back
      W (17402)  ff-mqtt: transaction 783e76da…: confirming on ota_1
      [  14.138] conn 1 abandoned by the client during the blackhole (304 B never reached upstream)
      [  30.730] conn 3 abandoned … (138 B …)    … one per esp-mqtt attempt, every 20-25 s …
      [ 190.858] conn 10 open 18883 -> 127.0.0.1:8883
      [ 200.005] mode blackhole -> pass
      [ 200.007] conn 10 upstream connected
      I (196892) ff-mqtt: announce acknowledged by the broker
      W (197042) ff-mqtt: this image was written by OTA and is now CONFIRMED …
    rows: … staged, confirming, confirmed|t

P2  stage 0.4.51 (cmd c50655fe) all-pass, stop; one proxy "0=blackhole,330=pass"; start
      otadecode at staged: sector0: seq=7 -> ota_0 state=NEW / sector1: seq=6 -> ota_1 state=VALID
      I (1852)   boot: Loaded app from partition at offset 0x20000
      W (2178)   ff-mqtt: OTA boot: 300 s from now to reach the fleet or roll back
      W (17758)  ff-mqtt: transaction c50655fe…: confirming on ota_0
      E (302218) ff-mqtt: no working session 300 s after an OTA boot — marking this image invalid and rolling back …
      W (302228) ff-mqtt: no broker session: rolling_back for c50655fe… is not reported
      I (305268) esp_ota_ops: Rollback to previously worked partition.
      rst:0xc -> Loaded app … 0x200000 -> esp_timer_impl_init panic loop (the known QEMU limit)
      [ 330.031] mode blackhole -> pass
    stop -> otadecode: sector0: seq=7 -> ota_0 state=INVALID crc=ok   <- our timer, not a reset
                       sector1: seq=6 -> ota_1 state=VALID crc=ok
    start: Loaded app … 0x200000, fw 0.4.50, valid
      W (9018)   ff-mqtt: transaction c50655fe…: rolled_back (returned to ota_1; ota_0 did not confirm)
    rows: … verifying, staged, rolled_back|t   (no confirming: the new image queued it in RAM,
          and the reset dropped it; only the old image's report arrived)
    GET /v1/devices: fw 0.4.50

P3  stage 0.4.51 (cmd acacc9f2), stop; one proxy "0=pass,5=blackhole" --repeat 30; start
      [   5.006] mode pass -> blackhole
      W (18113)  ff-mqtt: transaction acacc9f2…: confirming on ota_0
      [  30.010] mode blackhole -> pass
      I (26983)  ff-mqtt: announce acknowledged by the broker
      W (27153)  ff-mqtt: this image was written by OTA and is now CONFIRMED …
    rows: … staged, confirming, confirmed|t
```

**Findings.**

- **D3 → R2-fw-5 (P1).** A peer that goes silent mid-download holds the update slot
  until a power cycle or until something closes the socket. IDF v5.5.5:
  `esp_http_client_read()` returns `-ESP_ERR_HTTP_EAGAIN` when the transport times out with
  nothing read. `esp_https_ota_perform()` maps that to `ESP_ERR_HTTPS_OTA_IN_PROGRESS` with
  no stall counter, and `ff_ota.c` loops while IN_PROGRESS with no deadline. It is safe
  (the running image stays VALID) but not live. On a real board, TCP keepalive
  (`keep_alive_enable`, 5 s / 5 s / 3) may close a socket whose radio is really gone and
  turn it into D4. That is the bench question. → fixed by R2-fw-5 (agent 0.4.4).
- **The re-POST of an in-flight deploy can fail it (R2-fw-6, P2).** The agent deduplicates
  on the **last** command id only (`last_command_id`). After any other command, a
  re-delivery of the in-flight one reaches `ff_ota_start()`, gets `ESP_ERR_INVALID_STATE`,
  and publishes `failed` / `another update is already in progress` **against the cmd that
  is running**. The server marks that row terminal, and drops the real outcome when it
  arrives (`download failed` here; a `staged` would be lost the same way). The board itself
  is unaffected. It is a reporting defect in CRITICAL `ff_mqtt.c`. → fixed by R2-fw-6 (agent 0.4.5).
- **The QEMU openeth panic (harness, not product).** One D2 run panicked about a second
  after the link returned: `Cache error`, decoded against the image's ELF (`0e8f70a9a`) to
  `emac_opencores_isr_handler (esp_eth_mac_openeth.c:66)` ← `_xt_lowint1` ←
  `spi_flash_op_block_func`. Line 66 is an `ESP_EARLY_LOGW` ("RX frame dropped") whose
  format string is in flash, run while the other core had the cache off for an OTA flash
  write. openeth is the QEMU-only NIC, so no board runs this code. The board soft-reset
  onto its VALID image and the row parked at `downloading`. The re-POST (`reused: true`,
  same cmd_id; the RAM dedupe died with the reset) is the run quoted above.
- **A proxy fidelity fix found by P1.** The first P1 run replayed the connections esp-mqtt
  had already abandoned during the blackhole. At 200 s, eight stale CONNECTs reached the
  broker at once, a session takeover kicked the live one (`transport_read(): EOF`), and the
  ack came at 208.4 s, ~10 s after the link. A real SYN into a dead link never arrives, so
  the proxy now drops a deferred connection whose client closed first. That is the
  `abandoned` line, and it has a test. The re-run is the transcript above. Both runs
  confirmed.

**T1.** `just lint` and `just test` green (ruff, ruff format, mypy, full pytest), plus
`tests/test_flaky_link_tool.py` (22 tests, ~3 s): schedule parsing and refusals, the
taken port exits 2, `pass` round-trips, a blackhole holds bytes and closes nothing then
delivers them in order, a connection opened in a blackhole reaches upstream only after it,
an abandoned one never does, `reset` aborts both sides and keeps the mode (also from inside
a blackhole), `throttle` slows the stream, and the scheduler switches on time.
`just --list` shows `agent-qemu-flaky`. No firmware, sdkconfig, partitions, spec/,
alembic/, mosquitto/ or env file changed.

**T2 setup.** The api was recreated with `FF_PUBLIC_BASE_URL=http://10.0.2.2:18088`,
`FF_S3_PUBLIC_ENDPOINT_URL=http://10.0.2.2:19000` and the `.env.example` dev hash, and put
back afterwards. `.env` was not touched. ff_cfg: `--api-base http://10.0.2.2:18088
--mqtt-uri mqtt://10.0.2.2:18883`. Board A = 0.4.3 (`just agent-build esp32`, `--fresh`),
enrolled through the proxy (conns on 18088 and 18883), otadecode `sector0: seq=1 -> ota_0
state=VALID`. Artifacts: B1 `0.4.50` (`61731613…`) and B2 `0.4.51` (`f939f606…`), both
normal builds of this tree. T2-0, the tool smoke against MinIO with `0=pass,3=blackhole,8=pass`:
at t≈1 s `200 0.003 s`; at t≈4 s `200 4.09 s`, with `conn 2 open` at 3.9 s and `upstream
connected` at 8.0 s.

### A stalled download fails (R2-fw-5)

**A download that stops making progress for 60 s is abandoned and reported `failed` /
`download stalled`, and the update slot is free for the next `stage`.** Agent 0.4.4.
Proof status: **proven in QEMU (esp32, dev stack), bench replay owed**
(`../runbooks/rollback-test.md` → *Marginal radio*). Decision: DECISIONS.md 2026-10-03
(R2-fw-5). Replay: `../runbooks/agent-qemu.md` → *Driving a flaky link*, D2/D3. Fixes D3
of *Flaky link (R2-test-2)* above.

**The change (`agent/main/ff_ota.c`, the only firmware file).** The perform loop keeps the
last image length it saw and the `esp_timer_get_time()` at which it last grew. The clock
starts at `downloading`, right after `esp_https_ota_begin()`, so the wait for the first
body byte counts too. When the length has not grown for `OTA_STALL_MS` (60 000;
`_Static_assert` ≥ 2 × `OTA_HTTP_TIMEOUT_MS`), the loop breaks. The stall branch comes
before the `download failed` branch (after the break `err` is still IN_PROGRESS) and has
the same posture: `esp_https_ota_abort()`, `fail(cmd, "download stalled")`, `goto done`.
No `finish()`, no otadata write, no transaction record, no URL in the log. `done:` clears
`s_running`. The check runs before the loop's size-less `continue`, so a command without a
size is guarded too. Property 6 in the file header says this.

**Timing, measured.** The check runs each time `perform()` returns, which while stalled is
every 20 s. The read in flight when the link goes silent returns its partial bytes only at
its 20 s timeout, and that return counts as progress. So the abort lands **≈ 80 s after the
last byte** (60 s at the earliest, when the silence starts on a read boundary), and the
logged `no bytes for 60 s` counts from that last return. Both S1 runs: 80.1 s and 80.2 s
after the last progress line.

| # | Scenario (store proxy schedule) | Observed in QEMU | Pass |
|---|---|---|---|
| S1 | Silent far end (`0=throttle:16384,20=blackhole,240=pass`), board 0.4.4 fresh, B = 0.4.60 `on_command` | Last progress `30%` at 29 138 ms, `no bytes for 60 s at 310587 bytes` at 109 258 ms (proxy t ≈ 100), `failed` / `download stalled` on the topic, API `is_terminal: true`. Online, heartbeating, no reset, no transaction line. After t=240, **without a reboot**, a new POST got a fresh cmd (`reused: false`) → `staged` → stop/start → `confirmed` on 0.4.60. | yes |
| S1b | Same, repeated from 0.4.4 on ota_0 (otadata evidence) | `30%` at 31 831 ms, stall at 112 071 ms, `download stalled`. otadecode before: `sector0: seq=3 -> ota_0 VALID / sector1: seq=2 -> ota_1 VALID`; after: `sector0: seq=3 -> ota_0 VALID / sector1: empty` (the active sector unchanged; `esp_ota_begin()` erased the inactive one, as documented). Cold boot: `Loaded app … 0x20000`, `ota state valid`, no transaction line. | yes |
| S2 | 30 s outage (`0=throttle:16384,20=blackhole,50=throttle:16384`), 0.4.60 → 0.4.4 | `30%` at 39 472 ms, `40%` at 75 582 ms (resumed), `100%` at 113 002 ms, `matches what is on flash`, `staged`. No `no bytes for`, no `failed`. Stop/start → `confirmed`. | yes |
| S3 | Healthy (store `pass`), the S1 recovery deploy | 0 % → 100 % in 31.4 s, `staged`, no `no bytes for`. `confirmed` after stop/start. | yes |

**Transcripts (trimmed).** Proxy lines are `[s since proxy start]`, app lines `I (ms since
boot)`; the proxy started ≈ 10-13 s after the board's clock.

```
S1  store: throttle:16384, 20=blackhole, 240=pass   (cmd 78863f93…, 0.4.4 -> 0.4.60)
      [   0.983] conn 1 open 19000 -> 127.0.0.1:9000
      I (10338)  ff-ota: update 78863f93…: 0% (1024 bytes)
      I (29138)  ff-ota: update 78863f93…: 30% (308224 bytes)        <- the last progress line
      [  20.001] mode throttle:16384 -> blackhole
      E (109258) ff-ota: update 78863f93…: no bytes for 60 s at 310587 bytes — abandoning the download; the boot partition was never moved
      E (109268) ff-ota: update 78863f93… failed: download stalled
      topic: 78863f93 staging, downloading, failed "download stalled"
      GET /v1/devices: online true, fw 0.4.4, deploy {state failed, is_terminal true, detail "download stalled"}
      … up/hb every 10 s, no rst:, no ff-txn line …
      [ 240.098] mode blackhole -> pass
    same boot, POST 0.4.60 -> cmd 994910ef…, reused:false
      I (268148) ff-ota: update 994910ef…: staging version 0.4.60 …
      I (300018) ff-ota: update 994910ef…: 100% (1018016 bytes)
      I (311248) ff-ota: update 994910ef…: ota_1 is staged and bootable
    stop, start: Loaded app … 0x200000, pending_verify, confirming on ota_1, CONFIRMED

S1b (cmd e6ab3211…, 0.4.4 on ota_0 -> 0.4.60)
      I (31831)  ff-ota: update e6ab3211…: 30% (308224 bytes)
      E (112071) ff-ota: update e6ab3211…: no bytes for 60 s at 312225 bytes — abandoning the download; …
      E (112081) ff-ota: update e6ab3211… failed: download stalled
    stop -> otadecode: sector0: seq=3 -> ota_0 state=VALID crc=ok / sector1: empty

S2  store: throttle:16384, 20=blackhole, 50=throttle:16384   (cmd 96bcf01e…, 0.4.60 -> 0.4.4)
      [  20.001] mode throttle:16384 -> blackhole
      I (39472)  ff-ota: update 96bcf01e…: 30% (308224 bytes)
      [  50.027] mode blackhole -> throttle:16384
      I (75582)  ff-ota: update 96bcf01e…: 40% (407253 bytes)
      I (113002) ff-ota: update 96bcf01e…: 100% (1018016 bytes)
      I (121152) ff-ota: … sha256 505f45…ffcb matches what is on flash in ota_0; …
      I (124082) ff-ota: … ota_0 is staged and bootable
      'no bytes for' in this boot: 0
    stop, start: Loaded app … 0x20000, confirming on ota_0, CONFIRMED
```

**T1.** `tests/test_agent_download_stall.py` (6 text tripwires over the comment-stripped
source: the budget is `#define`d, ≥ 2 × the read timeout and ≤ 120 s, with the
`_Static_assert`; the clock starts after `begin()` and before the loop; the stall check
precedes the size-less `continue`; the stall branch aborts and never finishes, saves a
transaction, touches the boot partition or names the URL; it precedes `download failed`;
`done:` clears `s_running`). `just agent-build esp32` and `esp32s3` (`-Werror`) end
`BUNDLE OK`; `APP_SIZE_BUDGET_BYTES` raised to the measured bytes (esp32 1 018 016,
+320 B; esp32s3 998 352, +288 B). `just test` green.

**Accepted behaviour change.** R2-test-2's D2 (a 90 s silent outage mid-download, then the
link returns) used to resume and reach `staged`. It now ends `failed` / `download
stalled`. An outage shorter than the budget still resumes (S2). The recovery is a
re-deploy, a new POST after the terminal `failed`.

**Known residual, not fixed.** IDF's `read_header()` runs inside the **first**
`perform()` call and loops on `-ESP_ERR_HTTP_EAGAIN` until it has the first 1024 body bytes.
A peer that goes silent before the first 1 KB of body never returns control to
`ota_task`, so this guard cannot see it. The response-header phase is bounded (a
`fetch_headers()` timeout fails `esp_https_ota_begin()`). A trickle peer (1 byte every
19 s) counts as progress and is not caught either. The simulator is unchanged: its
`urlopen(timeout=30)` already ends a silent download as `download failed: TimeoutError`.

### A re-delivered stage is ignored, not failed (R2-fw-6)

**A `stage` whose `id` is the update the board is already carrying out is logged and
ignored: no status, nothing parsed, nothing started.** "Carrying out" means the download in
flight, or the image staged by `apply: "on_command"` that waits for a reboot. Agent 0.4.5.
Proof status: **proven in QEMU (esp32, dev stack)**. No bench replay is owed beyond a normal
deploy, because this is command-seam logic with no radio dependency. Decision: DECISIONS.md
2026-10-03 (R2-fw-6). Fixes the *re-POST* finding of *Flaky link (R2-test-2)* above.

**The change.** `ff_ota.c` gains a read-only predicate, `ff_ota_is_handling(cmd_id)`. It
answers true if `s_running` is set and `cmd_id` is the id `ff_ota_start()` recorded in
`s_running_cmd_id` (a single writer, set just before `s_running = true`). It also answers
true if the boot pointer names a slot other than the running one and the ff_txn record is
for `cmd_id`. A running update for a different id answers false and never reaches the
staged check. `ff_mqtt.c::on_stage()` calls it right after the id length check, before the
artifact is parsed, and returns with one WARN line. `on_command()`'s one-id dedupe,
`ota_task()`, `choose_target_slot()` and the confirm timer are untouched. A different id
still gets its honest `failed` in both cases. A reset clears the in-flight answer, and a
booted image (boot == running) never matches, so a reused id after a reset runs again.

| # | Scenario | Observed in QEMU | Pass |
|---|---|---|---|
| S1 | In flight. Board 0.4.5 fresh, store `throttle:8192`. POST B (0.4.70, `on_command`) → X. At 10 %: POST C (0.4.71) → Y; POST B again. | Y: `failed` / `another update is already in progress`, terminal (unchanged). Re-POST: `reused: true`, X. Serial: `stage id=X is the update this board is already carrying out — ignored (re-delivery)`. No `failed` for X on the topic or in rows. The download went on to `matches what is on flash` and `staged`. Stop/start: `confirming`, `confirmed\|t`, fw 0.4.70. | yes |
| S2 | Staged and waiting. Board 0.4.70, store `pass`. POST C `on_command` → X2 `staged`. POST B → Y2; POST C again. | Y2: `staging, failed "an update is already staged and waits for a reboot"` (unchanged). Re-POST: `reused: true`, X2, `… already carrying out — ignored`. No `staging` and no `failed` for X2. Stop/start: `confirmed\|t`, fw 0.4.71. | yes |
| S3 | A reused id after a reset still runs. Board 0.4.71, store `throttle:8192`. POST B `on_command` → X3. Stop at 20 %, start, POST B again. | `reused: true`, X3. No `already carrying out` this boot. `staging version 0.4.70`, 0 % → 100 %, `staged`. Stop/start: `confirmed\|t`, fw 0.4.70. | yes |

**Transcripts (trimmed).** App lines are `I (ms since boot)`; cmd ids shortened.

```
S1  store throttle:8192   X = 64b980cf (0.4.70), Y = 889379d8 (0.4.71)
      I (63311)  ff-mqtt: …/dn/cmd id=64b980cf… type=stage
      I (76611)  ff-ota: update 64b980cf…: 10% (103424 bytes)
      I (77511)  ff-mqtt: …/dn/cmd id=889379d8… type=stage          <- POST C, reused:false
      I (81661)  ff-mqtt: …/dn/cmd id=64b980cf… type=stage          <- POST B again, reused:true
      W (81671)  ff-mqtt: stage id=64b980cf… is the update this board is already carrying out — ignored (re-delivery); its outcome is reported when it ends
      I (89201)  ff-ota: update 64b980cf…: 20% (205824 bytes)
      I (188781) ff-ota: update 64b980cf…: 100% (1018304 bytes)
      I (199711) ff-ota: update 64b980cf…: sha256 50f6f2fb… matches what is on flash in ota_1; switching the boot partition
      I (202701) ff-ota: update 64b980cf…: ota_1 is staged and bootable
      topic: 64b980cf staging, downloading | 889379d8 failed "another update is already in progress" | 64b980cf verifying, staged
    stop, start: Loaded app … 0x200000, 0.4.70 pending_verify, confirming on ota_1, CONFIRMED
      rows 64b980cf: requested, staging, downloading, verifying, staged, confirming, confirmed|t
      rows 889379d8: requested, failed|t

S2  store pass   X2 = 45c9e138 (0.4.71), Y2 = 17f1a80e (0.4.70)
      W (122237) ff-ota: update 45c9e138…: apply=on_command — staged and waiting …
      I (124647) ff-mqtt: …/dn/cmd id=17f1a80e… type=stage          <- POST B
      E (125557) ff-ota: update 17f1a80e…: refused — the boot partition names ota_0 (ota state new) while ota_1 is running; …
      I (128727) ff-mqtt: …/dn/cmd id=45c9e138… type=stage          <- POST C again, reused:true
      W (128967) ff-mqtt: stage id=45c9e138… is the update this board is already carrying out — ignored (re-delivery); …
    stop, start: Loaded app … 0x20000, 0.4.71 pending_verify, confirming on ota_0, CONFIRMED
      rows 45c9e138: requested, staging, downloading, verifying, staged, confirming, confirmed|t
      rows 17f1a80e: requested, staging, failed|t

S3  store throttle:8192   X3 = 485d5872 (0.4.70)
      I (75404)  ff-ota: update 485d5872…: 20% (205824 bytes)       <- just agent-qemu-stop
    start (0.4.71, ota state valid, no transaction line), POST B again -> reused:true
      I (22130)  ff-mqtt: …/dn/cmd id=485d5872… type=stage
      I (22190)  ff-ota: update 485d5872…: staging version 0.4.70 …
      I (147470) ff-ota: update 485d5872…: 100% (1018304 bytes)
      I (161200) ff-ota: update 485d5872…: ota_1 is staged and bootable
      'already carrying out' in this boot: 0
    stop, start: Loaded app … 0x200000, confirming on ota_1, CONFIRMED
      rows 485d5872: requested, staging, downloading, verifying, staged, confirming, confirmed|t
```

**T1.** `tests/test_agent_redelivery.py` has 7 text tripwires over the comment-stripped
source:
- the predicate is declared in `ff_ota.h`;
- `on_stage` calls it before any `ff_mqtt_publish_status`, before `ff_ota_start` and before
  the artifact parse, and the ignore branch publishes nothing, starts nothing and does not
  name the URL;
- a different id still gets `another update is already in progress`;
- the predicate reads `s_running`, `s_running_cmd_id`, both partitions and `ff_txn_load`,
  never calls `fail`, a publish, `ff_txn_save`/`ff_txn_clear_if`, a boot-partition write,
  `esp_ota_begin`, an erase or `esp_restart`, and never assigns `s_running`;
- the in-flight check comes first and returns there;
- `s_running_cmd_id` has exactly one writer, in `ff_ota_start` before `s_running = true`,
  and `ota_task` never names it;
- `ff_ota_start` still returns `ESP_ERR_INVALID_STATE` while running.

`just agent-build esp32` and `esp32s3` (`-Werror`) end `BUNDLE OK`.
`APP_SIZE_BUDGET_BYTES` is raised to the measured bytes (esp32 1 018 304, +288 B; esp32s3
998 672, +320 B). `just test` is green.

**T2 setup.** The flaky-link rig, the same one R2-fw-5 used. The api was recreated with
`FF_PUBLIC_BASE_URL=http://10.0.2.2:18088`, `FF_S3_PUBLIC_ENDPOINT_URL=http://10.0.2.2:19000`
and the `.env.example` dev hash, and put back afterwards. `.env` was not touched. api and
mqtt went through an all-pass proxy, and the store went through its own process, changed
only while the board was stopped. A = 0.4.5 (`--fresh`), B = 0.4.70 (`50f6f2fb…`),
C = 0.4.71 (`58f5e5a7…`), all normal builds of this tree.

### Deploy pre-check, `POST /v1/devices/{id}/deploy/precheck` (R2b-be-2), LANDED 2026-10-04

Same body as `/deploy`, answers 200 with every refusal and warning at once and sends
nothing (no URL minted, no row, no publish). 400 bad version and 404 unknown or
decommissioned device keep their status. `src/fleetforge/deploy_precheck.py` is the one
home of every sentence; `/deploy` raises the first refusal from it, unchanged.

| kind | code | when |
|---|---|---|
| refusal | `no_artifact_for_target` | no label for the device's chip (404 on deploy) |
| refusal | `layout_mismatch` | both layouts known and differ (409) |
| refusal | `slot_too_small` | image larger than a known slot (409) |
| refusal | `no_ota_capability` | `ota` not announced (409) |
| warning | `never_connected` | no `last_seen` and no presence report |
| warning | `offline` | not online (never both with `never_connected`) |
| warning | `sleepy` | power class sleepy; names the wake interval |

Warnings never block `/deploy`; no override field yet (that belongs to gating warnings,
which do not exist). Not yet: weak RSSI (none stored, R4), `rollback_capable: false`
(decided R2b-spec-2; R2b-be-7 builds it), merged-binary refusal (refused at upload instead, R2b-be-3), R3 library
marker, and URL-configuration readiness (`/v1/readyz` owns it; the pre-check answers 200
without it). The response carries `confirm_timeout_s` for the card. T2 on the dev stack:
same-layout 200 deployable; Arduino label `layout_mismatch`; unknown label
`no_artifact_for_target` with null sha; offline `[offline]`; never connected
`[never_connected]`; sleepy `[sleepy]` ("about every 60 s"); unknown device 404; bad
version 400; no auth 401; the real deploy of the refused label 409 with the identical
sentence; `deploy_events` count unchanged.

## Operator-flow additions (2026-10-04, planned, nothing built)

Implementation notes behind `spec/flows.md` Flow 2 and the 2026-10-04 entries in
`DECISIONS.md`.

- **Merged binary (R2b-be-3, LANDED 2026-10-04).** A `*.merged.bin` carries bootloader,
  partition table and app from offset `0x0`. `POST /v1/artifact` refuses it with a 422
  before anything is stored or any row is written, so no merged label can reach the
  pre-check or a deploy. Detection (`fleetforge/merged_image.py`) is two negative
  signatures: `0xFF` x 4 KiB then `0xE9` at `0x1000` (esp32, esp32s2), or a partition
  table at `0x8000` (magic `AA 50`, aligned non-zero offset, non-zero size; the only
  signature that catches esp32s3/c3/c6, whose merged file starts with `0xE9` like an app).
  The sentence and code `merged_binary` live in `deploy_precheck.py`, not in `refusals()`.
  The 413 sentence also names the merged trap. Gaps, by design: artifacts uploaded before
  this change are not re-checked, and `merge_bin --target-offset 0x1000` files are not
  recognised. Fixtures: heads of real images in `tests/fixtures/firmware/`. T2 on the dev
  stack: full esptool-merged esp32s3 and esp32 images got HTTP 422 (the sentence naming
  "the partition table at 0x8000", and "the bootloader at 0x1000 and the partition table at
  0x8000"), no rows written; the real app.bin files got 201; 1966081 bytes got 413 with
  "merged".
- **`rollback_capable`.** A warning, not a refusal. It is `null` before a board's first
  OTA and the `false` reading is unbenched (`DECISIONS.md` 2026-10-03), so it cannot guard
  a first OTA. The override must be explicit in the UI and in the API. Decided shape
  (R2b-spec-2; built by R2b-be-7, after R2b-be-6 stores the field): warning code
  `rollback_incapable`, a **gating** warning. The pre-check reports it with
  `PrecheckFinding.needs_override: true` (the existing warnings stay `false`), and
  `POST /v1/devices/{id}/deploy` answers 409 with the same sentence unless the body
  carries `override: ["rollback_incapable"]` (`DeployRequest.override:
  list[Literal["rollback_incapable"]] = []`, per code, never a blanket `force`; an unknown
  code is a 422; refusals are never overridable). `null` or absent never warns.
  Details: `board-profiles.md` → *Server semantics*.
- **Library marker.** R3 only. How it is encoded in the app binary is open; until it is,
  no refusal can be implemented.
- **Crash reason after a rollback.** Open question, not a design. After the bootloader
  rolls back, the surviving slot boots a fresh `esp_reset_reason()`, which may describe the
  rollback reset rather than the failed boot's panic or watchdog. A reason and last milestone
  would need to be written by the failed boot to a store that survives the reset (RTC
  memory does not survive a brownout or power loss). Treat the field as best effort and
  establish it on a bench before it goes on the wire.
- **Send again.** Safe after a failure before reboot because sends are deduplicated on the
  board (R2-fw-6).

## De-risking

Run a **throwaway OTA + auto-rollback spike during R0–R1** on real flaky Wi-Fi —
prove auto-rollback saves a bad build before relying on it. (Tracked as parallel work
in TODO.md.)

**Partly discharged 2026-09-23** by the rollback test above — but on a bench-adjacent
link, not "real flaky Wi-Fi". The spike's actual question (does a marginal radio break
the mechanism, for example,by stalling the download past the confirm timer?) is still open, and
is the reason remote deploys are still one board at a time rather than fleet-wide.

**Discharged in QEMU 2026-10-03 (R2-test-2).** A marginal radio cannot stall the download
past the confirm timer, because they never overlap. The download runs on a VALID image,
which arms no timer (`pending_verify()` gate). The timer runs only in the new image, and a
stage is refused while one is pending (R2-fw-2). What a flaky link does instead: a slow or
interrupted download finishes or fails cleanly (D1, D2, D4). An outage right after the
reboot rolls a good image back once it outlasts ~300 s (P2, a miss). A silent peer
mid-download holds the update slot until a power cycle (D3 → R2-fw-5). This retires the
flaky radio as the reason for one board at a time. It does **not** retire one board at a
time: an image that boots, confirms and is broken anyway is still recovered by nothing.
Bench replay owed (`../runbooks/rollback-test.md` → *Marginal radio*), above all for the
question QEMU cannot answer: does a really dead radio end D3 through TCP keepalive?
