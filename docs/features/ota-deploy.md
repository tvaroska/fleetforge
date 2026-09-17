# OTA Deploy & Auto-Rollback

**Status:** Planned
**Priority:** P0
**Target:** R1 (deploy), R2 (safe deploy ⭐)
**Depends on:** Enrollment (enrollment.md) — R0
**Flow:** [flows.md](../../spec/flows.md) → Flow 2

## Overview

Push new firmware to a registered board from the dashboard (R1), then make it **safe**:
a bad build is caught and any device that gets one **recovers itself** via A/B slot +
auto-rollback (R2). R2 is the single most important milestone — the whole gamble.

Artifacts are **opaque + versioned**; the server stores/targets/tracks but never parses
them. Users build the `.bin` with their own toolchain (idf.py / PlatformIO / Arduino) —
the server never builds.

Size limits, deploy-duration targets and the confirm-timeout default:
[prd.md](../../spec/prd.md) → *Requirements & targets*. Channel split and signed URLs:
[design/architecture.md](../../design/architecture.md) → *Transport*.

**Storage is already in place** (R0-be-6): `fleetforge.storage` is the
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
`api/deps.get_object_store`. Nothing in R0 calls it; R1-BE-1 (upload), R1-BE-2 (`stage`
carrying a URL) and R2's pruning all do, and getting artifact-URL authorization wrong is
cheapest to fix before any of them exist.

Both SDKs are imported **inside** the factory branch that needs them and every network
call runs in `asyncio.to_thread` under an `asyncio.timeout` — the SDKs are blocking, and
neither `aioboto3` nor `gcloud-aio-storage` earns a dependency for a path that runs a
handful of times per deploy. `signed_url` is `async def` anyway, even though V4 signing
is local CPU, so a future IAM-`signBlob` backend is not a Protocol change.

**There are four verbs and no `list`.** `list` is also the one verb an IAM prefix
condition cannot constrain, so adding it would silently widen the production grant.

**The prefix is confined twice, independently.** `gs://btvaroska` is a *shared* bucket —
it holds this estate's `.env` backups under `secrets/` and the boris podcast audio — and
object keys arrive from an HTTP request body. So `storage/objectstore.resolve_key()`
**rejects and never repairs** (`..`, a leading `/`, `//`, backslashes, control or
non-ASCII bytes, `?`/`#`, over 512 characters), the rule `identity.py` established for
device IDs and for the same reason: a normalised key is a string two readers can read
differently. Independently, the production service account holds `objectAdmin` under an
IAM condition on `…/objects/fleetforge/…`. Either alone is one bug away from writing
next to `secrets/`; the condition is also what makes a signed URL for an out-of-prefix
object worthless, since GCS evaluates the *signer's* permissions at redemption. A bad
key raises `ObjectKeyError`, which is a `ValueError` and deliberately not an
`ObjectStoreError` — the caller is wrong, so it is a 4xx, and retrying it is pointless.

**A presigned S3 URL signs the `Host` header, so there are two S3 endpoints.**
`S3_ENDPOINT_URL` (`minio:9000`) is what the API reads and writes through;
`S3_PUBLIC_ENDPOINT_URL` (`localhost:9000` in dev) is what URLs are *signed against*,
because the device is not on the compose network and rewriting the host after signing
invalidates the signature. There is no post-hoc fix, which is why the split exists at
signing time and why a unit test asserts the generated URL's host — the failure works
perfectly from inside the network and only shows up on a real board.

**The GCS half has never been round-tripped against the real service.** `btvaroska`
inherits `constraints/iam.disableServiceAccountKeyCreation`, so the service-account key
the adapter requires cannot be minted; the service account and its conditional binding
exist, the credential does not. The adapter deliberately has **no ADC fallback** —
Application Default Credentials on a GCE VM carry no private key (so no V4 signing) and
resolve to the project-wide compute default SA, the exact credential the prefix
condition exists to avoid — so a missing key file fails loudly at construction. Closing
this is a prerequisite for R1; the two options (impersonation + `signBlob`, or an org
policy exemption) are in
[runbooks/artifact-storage.md](../runbooks/artifact-storage.md).

Operationally: `python -m fleetforge.storage selftest` (`just storage-check`) round-trips
whichever backend the environment selects and prints the bucket and prefix but never a
credential. Unconfigured storage is one startup WARNING plus a 503 at use time, never a
crash — `create_app()` stays constructible with no environment at all — and both
backends configured at once is refused rather than resolved by a precedence rule.

## The update transaction (4-verb contract, from design/architecture.md)

`stage → apply → confirm → rollback` — server orchestrates, never knows *how* **nor when**.

**Two authority rules, both device-side:**
- **The device owns the reboot.** `stage` delivers and verifies; the device applies only
  in a self-declared safe window and may sit in `awaiting_safe_window` indefinitely — a
  vehicle in motion or an airborne drone must not reboot on the server's schedule.
  Rollback reboots obey the same rule.
- **The device owns the rollback.** The confirm timer is armed on the device before the
  reboot. A board that cannot reach the broker is exactly the board that must roll back,
  and it will never receive a server command saying so. The server observes and records;
  it never triggers a rollback.

ESP32 adapter: write OTA1 partition → broker reconnect + self-test → switch to OTA0.

## Phase 1: R1 — Upload new code (OTA deploy)

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R1-BE-1 | Artifact upload `POST /v1/artifact` (opaque blob + version + platform_type); reject anything over the target layout's `ota_slot_size` | P0 | 1d |
| R1-BE-2 | Deploy orchestration: `stage → apply` (per-device), carrying a short-lived signed artifact URL | P0 | 1.5d |
| R1-BE-3 | Artifact download endpoint: signed-URL verification + HTTP range support | P0 | 1d |
| R1-BE-4 | Write every deploy outcome to `deploy_events` — the KPI history R5 computes from | P0 | 0.5d |
| R1-FW-1 | Agent gains `esp_https_ota` + "update" command handler | P0 | 2d |
| R1-FW-2 | Agent reports firmware version after reboot | P0 | 0.5d |
| R1-FE-1 | Per-device Deploy button + version-change feedback | P0 | 1d |
| R1-TEST-1 | E2E: push firmware → board version changes in dashboard | P0 | 1d |

> ⚠️ Not yet safe — a broken build stays broken until R2.

### R1-BE-0 — a production GCS credential that is not a key file — **LANDED 2026-09-15**

**Delivered by S0-infra-5.** R1 no longer needs to solve this; read the answers below
rather than re-deriving the question.

* **The credential is `GCS_IMPERSONATE_SERVICE_ACCOUNT`**, an impersonation over the
  runtime's ADC targeting `fleetforge-artifacts@btvaroska.iam.gserviceaccount.com`.
  Mutually exclusive with `GCS_CREDENTIALS_FILE`; neither set is still a refusal, so there
  is no silent ADC fallback.
* **`signBlob` WORKS, measured not assumed.** `just storage-check --backend gcs --blob`
  ends `SELFTEST OK` against `gs://btvaroska` with no key file anywhere: the V4 URL carries
  `X-Goog-Credential=fleetforge-artifacts@…` and an unauthenticated GET returns the bytes.
  There is no private key in the process, so the signature can only have come from the IAM
  API. **R1-BE-3's signed-URL delivery rests on a verified mechanism.**
* **Containment is real**, measured through the adapter with `GCS_PREFIX=` empty: a `put`
  to `secrets/…` fails `Forbidden` from the IAM condition alone.
* **Signing is a network call now, and it is on R1's latency budget.** `signed_url` runs in
  a thread under `OBJECT_STORE_TIMEOUT_S` (the adapter cannot tell a key file from an
  impersonation, so there is one path). One extra Google round trip per URL handed to a
  device, and it can rate-limit. If R1-BE-3 hands out URLs per range request, cache them.
* **Still owed:** the same selftest **from the prod container**. Prod's identity
  `mainsite@sites-470716` holds the tokenCreator grant, so it is expected to pass, but its
  metadata server and egress are its own. S0-infra-6 wires the container and runs it.

Original filing, 2026-09-11, kept for the reasoning:

`storage/factory.py` requires `GCS_CREDENTIALS_FILE` and never falls back, but
`btvaroska` inherits `constraints/iam.disableServiceAccountKeyCreation` and will not
issue a key. GCS has therefore **never been round-tripped against the real service** —
`R0-be-6`'s T2 AC5/AC6 are unexecuted, and the adapter's GCS path is exercised only by
unit tests. A green dev stack proves MinIO, not production.

Add `GCS_IMPERSONATE_SERVICE_ACCOUNT` to `storage/factory.py`, mutually exclusive with
`GCS_CREDENTIALS_FILE` so there is still no silent ADC fallback, and grant prod's
`mainsite@sites-470716` the role `roles/iam.serviceAccountTokenCreator` on
`fleetforge-artifacts@btvaroska`. V4 signing then routes through IAM `signBlob`;
`impersonated_credentials.Credentials` is a `Signer`, so `generate_signed_url` is
unchanged.

**Impersonation is required for containment, not just for signing.** Prod's attached
identity is the estate's shared VM service account and can read all of `gs://btvaroska`
including `secrets/` — so plain ADC would hand fleetforge every other app's secrets.
Impersonating `fleetforge-artifacts` is what keeps the existing prefix condition real.

Signing stops being local and free: every `signed_url` becomes an IAM API call, so it
needs a timeout and can rate-limit.

**Acceptance:** `just storage-check` completes against **real GCS** from the prod
container — put / get / sha256 / signed URL fetched over HTTPS / delete / `ObjectNotFound`
/ idempotent second delete — with no key file present anywhere; and an out-of-prefix key
is still refused. Closes `R0-be-6`'s unexecuted AC5/AC6.

**Unverified going in:** that `mainsite` can `signBlob` at all. Both probes were refused
by the dev-box sandbox on 2026-09-11 — confirm it first, since the whole approach rests
on it. *(Resolved: `signBlob` verified 2026-09-15 under `devserver@btvaroska`, which holds
the same grant. See the summary above.)* Details:
[docs/runbooks/artifact-storage.md](../runbooks/artifact-storage.md) →
*Verified against real GCS*.

### Artifact upload — `POST /v1/artifact` (R1-be-1) — **LANDED 2026-09-16**

**What shipped.** The endpoint that gets a user's `.bin` into the system, so R1-BE-2 has
something to hand a device a URL to. Admin-authenticated, raw body (not multipart), with
`target`, `version` and an optional `partition_layout` as query parameters; it returns
the digest, the size and a `created` flag. It is the **first writer of the `artifacts`
table** — `firmware/publish.py` already wrote *blobs* for the agent bundles S0-infra-6
moved into the store, so this is the user-facing half of a storage model that already
existed rather than a new one.

**`artifact_versions`: a label layer, because `artifacts` had nowhere to put a version.**
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

`0003` has no foreign keys, but that was forced rather than chosen — `builds.outputs`
names artifacts inside JSONB and PostgreSQL cannot FK into JSONB. Here the reference is a
plain column, so the constraint is available, and `RESTRICT` is what will stop R2's pruner
deleting bytes a label still points at.

**Three statuses, because a content-addressed store collapses two success cases.** A new
label is **201**; re-uploading identical bytes under the same `(target, version)` is
**200** with `created: false`, since a re-`put` of the same key is a no-op by
construction; the same label over *different* bytes is **409** and the label keeps
pointing at the original digest — a version is a promise about which image it is, so
silently re-pointing it would make every `deploy_events` row that mentions it ambiguous.
Re-tagging the same bytes under a second label is fine and costs no storage: both labels
name one object.

**One size limit, not two.** The task as filed called for two rejections — the target's
`ota_slot_size` and "the SPEC cap" — but `prd.md`'s **1.9 MB** *is* `ota_slot_size`
**1966080** rounded (1966080 B = 1.875 MiB). They are one number written twice. The
implementation uses the authoritative one, `firmware/manifest.py::SUPPORTED_LAYOUTS`,
because that is the mapping tied to the partition table a board actually carries and it
is already what agent-bundle validation reads — so an upload and a bundle cannot disagree
about how big a slot is. A second, slightly different cap would have been a rejection
nobody could explain. Filed as a spec clarification in `spec/open-questions.md` rather
than resolved by inventing a number.

**Nothing reaches the store until it is known to be acceptable.** `Content-Length` is
required (411 without it) and checked before the body is read at all; the stream read is
then capped again so a lying header cannot spend memory either. An upload that writes
3 MB and then apologises has already paid for the object. Writes go **blob first, then
rows**, matching `publish.py`'s crash posture: a failure between them leaves an
unreferenced content-addressed blob, which is inert and re-`put`-able, where the reverse
order would leave a row naming bytes that do not exist.

**The label is rejected, never normalised** (`identity.py`'s rule):
`^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$`, no semver requirement — the vocabulary is the
user's, and a date or a CI number is fine. But it travels into a `dn/cmd` JSON payload and
a dashboard list, so `1.5.0` and `1.5.0 ` must not become two spellings of one release.
There is deliberately **no DB CHECK** on `version`: a constraint there would be this
project deciding what a customer may call their firmware.

**The bytes stay opaque.** No ELF check, no image-header validation, no "is this really an
ESP32 app?" — users build with their own toolchain, and a server that understood the
format would be a server with opinions about which toolchains are allowed.

**Verification.** T1: `ruff` + `ruff format` + type-check green, 728 tests pass, including
`alembic check` (model and migration cannot drift) and `test_migration_downgrades_cleanly`.

T2 ran against the **rebuilt dev container**, which applied `0003 → 0004` on start, using
the real 993696-byte `agent/dist/esp32/app.bin`:

* Upload → **201**, `sha256` equal to the local `sha256sum`, and the object is at
  `blobs/sha256/2484cb76…` in MinIO. `mc cat` of the stored object re-digests to the same
  value — key, contents and response all agree — and `mc stat` shows
  `Cache-Control: public, max-age=31536000, immutable`.
* `artifacts` holds one row: digest, 993696, `kind=user_firmware`, `target=esp32`,
  `partition_layout=ab-4m-v1`.
* Same bytes, same label → **200** `created: false`, still one blob and one row.
* Same label, different bytes → **409** naming the digest it already points at; the label
  is unchanged.
* Second label `1.5.1`, same bytes → **201**, two `artifact_versions` rows pointing at one
  digest and one object.
* 2 MB body → **413** naming both numbers, and the bucket gained no object. Empty → 400;
  `partition_layout=ab-16m-v9` → 400 naming `ab-4m-v1`; `version=1.5.0␠` → 400;
  unauthenticated → 401.

The test module (`tests/test_api_artifact_upload.py`) asserts the oversize and malformed
cases on `store.puts == []`, not merely on the status code — "rejected" and "rejected
before it cost anything" are different promises and only the second is the one this
endpoint makes.

**Decisions & gotchas.** See `DECISIONS.md` 2026-09-16. Two for whoever writes R1-be-2/3:
the 411-on-missing-`Content-Length` rule means a chunked upload is refused by design, and
since S0-infra-5 `signed_url` is a network round trip, so handing out a URL per range
request needs a cache.

### Deploy orchestration — `POST /v1/devices/{id}/deploy` (R1-be-2) — **LANDED 2026-09-17**

**What shipped.** The first command the server ever sends a board: `stage`, carrying a
short-lived signed URL for the artifact R1-be-1 uploaded, published to
`ff/v1/d/<device>/dn/cmd`. Admin-authenticated, body `{"version": "1.5.0"}` plus an
optional `apply`, answered **202** with `cmd_id`, `sha256`, `size_bytes`, `apply`,
`reused` and `device_online`. Plus the two things that make it auditable: `deploy_events`
gets its first writer, and the simulator gained a real stage executor so the whole path
can be exercised without a board.

**A fourth broker credential, `commander`, and why the `device` role stayed empty.**
`mosquitto/bootstrap.sh` now creates a dynsec role with exactly one ACL,
`publishClientSend 'ff/v1/d/+/dn/#' allow`, and one client holding it
(`MQTT_COMMAND_USERNAME`, `ff-commander` in dev). It deliberately has no
`subscribePattern` and no `publishClientReceive`: the ingestor must remain the only
subscriber, and a leaked deploy credential must not be able to forge `up/status`. It is
also not the dynsec admin — that credential is broker-root over `$CONTROL`, a privilege
publishing a command does not need.

Delivery works because **both ACL backends are consulted and allow wins**: dynsec denies
`publishClientReceive` by default, and `mosquitto/acl`'s `pattern read ff/v1/d/%u/dn/#`
grants the board its own downlink. So no change to `mosquitto/acl` and no rule on the
`device` role — a `+` rule there would let every board read every other board's commands,
which is the breach `CRITICAL.md` names. `just broker-check` now proves the matrix live:
the commander publishes to A, **A receives it and B does not**, and the commander's
publish to `ff/v1/d/A/up/status` is dropped.

**MQTT 3.1.1 has no deny feedback — a limitation, not a bug.** A refused publish is
indistinguishable from a delivered one: no PUBACK reason code, no error, nothing in the
publisher's logs. Every negative assertion in the selftest is therefore "nothing arrived
at a subscriber watching `#`", never "the publish raised". If a command silently never
arrives, the reason code is obtainable only over MQTT 5 from inside the container
(`docs/runbooks/dev-stack.md` → failure 5).

**`deploy_events` has exactly one writer.** Everything that inserts goes through
`src/fleetforge/deploys.py`; `tests/test_invariants.py` fails the build if any other
module under `src/` mentions `DeployEvent(` or the table in SQL. The table is retained
forever and both v1 KPIs are computed over the terminal event of each
`(device_id, cmd_id)` transaction, so a row in the wrong shape is not a bug that shows up
today — it is a KPI that is quietly wrong in R5. R1-be-4's `up/status` ingestion adds its
writer *there*, not in `ingestor/handlers.py`.

**The server authors one state, `requested`, and one terminal exception.** `requested`
records "we published a command", which no device can report; without it an abandoned
deploy is invisible and R1-be-4 cannot map an incoming `cmd_id` back to the intended
version. The exception is `failed` with `detail={"reason": "publish_failed"}` — the
broker refused, so the board provably never saw the command, and the transaction gets
closed instead of hanging open forever (the API answers 503). Beyond that the server
never writes a state for a command a device received, and **never expires
`awaiting_safe_window`**: the device owns the reboot, and a vehicle in motion may park
there indefinitely. There is no sweeper, no timeout task and no `asyncio.sleep` on this
path, and the test module asserts their absence in the source rather than trusting a
review.

**A retry inside the URL's TTL is the same transaction.** The device deduplicates on the
command `id`, so a retried `stage` must reuse it or the board downloads the same firmware
twice. A POST for the same `(device_id, sha256)` matching the newest `requested` row for
that device — younger than `SIGNED_URL_TTL_S` and with no terminal event — reuses that
`cmd_id`, signs a **fresh** URL, republishes and answers `reused: true`, writing **no
second row**. A different artifact is always a new intent. Past the TTL the first URL has
expired, so a board that never acted on it cannot act on it now, and a new intent is the
honest record.

**The signed URL is a bearer credential.** It appears in exactly one place: the `stage`
payload on the wire. Never in the 202 body, never in `deploy_events.detail` (sha256, size,
target, apply — that is all), never in a log line; the publisher logs `id` and `type`
only, and the simulator's transcript prints it redacted. Exactly one URL is signed per
accepted deploy — since S0-infra-5 signing is an IAM round trip on GCS, so it is not free.

**Refusals are specific, and none of them write an event.** 400 malformed version (the
regex is imported from `api/routers/artifacts.py`, not retyped); 404 unknown device; 404
no artifact under that label **for this device's chip** — the target is the board's chip,
not a choice; 409 if the device never announced the `ota` capability; 409 if the artifact
row or blob is missing. A device being offline is **reported, not enforced**: `dn/cmd` is
never retained (a retained command re-stages on every reconnect, forever) — durability
comes from the board's persistent session, so the deploy is accepted and `device_online`
tells the operator what to expect.

**The simulator can now execute a deploy** (`--safe-window auto|hold`): decode, dedup on
`id`, then `staging → downloading → verifying → staged → applying → rebooting`, adopting
the new `fw_version`, publishing each state retained on `up/status`. `hold` stops at
`awaiting_safe_window` and stays there; `apply: on_command` stops at `staged`. It stays
import-pure (stdlib `urllib`, never the API's httpx client) and re-types every protocol
constant, both enforced by existing tripwires.

**Verification.** T1: `ruff` + `ruff format` + `mypy` green, **811 tests pass**,
`just stack-check` clean.

T2 ran against the live dev stack with the real 993696-byte `agent/dist/esp32/app.bin`:

* `just up` healthy; `docker compose logs mosquitto-init | grep -i commander` shows
  `createRole` / `addRoleACL` / `createClient` / `addClientRole` for `ff-commander` on the
  **pre-existing** dynsec store, with 13 "already exists" lines and `bootstrap: done` — the
  bootstrap is still idempotent.
* `just broker-check` → `SELFTEST OK`, including
  `allow ff-commander -> ff/v1/d/ffff00000001/dn/cmd delivered to ffff00000001`,
  `deny ffff00000002 received nothing while ffff00000001 was commanded`, and
  `deny ff-commander -> ff/v1/d/ffff00000001/up/status dropped`; the R0 deny cases
  unchanged.
* Upload → **201** (993696 bytes, `sha256 2484cb76…`). Deploy → **202**
  `reused: false`; the immediate repeat → **202** with the **same** `cmd_id` and
  `reused: true`, and `deploy_events` holds **one** row.
* The simulator printed the redacted payload — exactly the spec keys, `type: "stage"`,
  `confirm_timeout_s: 300`, `artifact.size` equal to the uploaded byte count — then walked
  `staging → downloading (993696 bytes) → verifying (sha256 matches) → staged → applying →
  rebooting → fw 1.5.0`, and dropped the duplicate command.
* `--safe-window hold` parked in `awaiting_safe_window` and was still parked minutes
  later, with one `requested` row and nothing server-side expiring it.
* Hygiene: no row in `deploy_events.detail` contains `http`, and 20 minutes of API logs
  contain no signature or endpoint string.
* Refusals live: wrong-chip label → 404, no `ota` capability → 409, unknown version →
  404, unknown device → 404, `version=../etc` → 400.

**Production prerequisite (not done — `services/` is a different repo and prod env is
never edited without asking):** `services/prod/.env` must gain `MQTT_COMMAND_USERNAME` and
`MQTT_COMMAND_PASSWORD` before the next prod deploy. Both are `:?`-mandatory in compose, so
without them `mosquitto-init` refuses to start; if the API alone lacks them it selects
`NullCommandPublisher` and every deploy answers 503 (with one startup WARNING).

**Spec proposals (filed, not applied — `spec/` is protected):** `spec/device-protocol.md`
says nothing about a server-authored `requested` marker, nor about a retried `stage`
having to reuse `id`, though both follow from "the device deduplicates on `id`". Propose
one sentence making the reuse rule explicit, a note that `deploy_events.state` may carry
server-authored values outside the device state machine, and confirmation that
`artifact.sig` is optional while R1 has no signer.

**Decisions & gotchas.** See `DECISIONS.md` 2026-09-17.

### Artifact download — `GET /v1/artifact/{sha256}/bin` (R1-be-3) — **LANDED 2026-09-17**

**What shipped.** The link R1-be-2 puts in the `stage` command is now **ours**:
`GET /v1/artifact/<sha256>/bin?exp=<unix-seconds>&sig=<base64url>`, the exact shape
`spec/device-protocol.md` already fixed, on `PUBLIC_BASE_URL` instead of the object
store's host. The endpoint is **public** — the second unauthenticated one in the app
after `POST /v1/enroll` — verifies the signature, and answers **307** to a short-lived
store URL. Three new modules: `artifact_urls.py` (mint/verify + the `mint` CLI behind
`just artifact-url`), `storage/urlcache.py` (`SignedUrlCache`), and
`api/routers/artifact_download.py`.

**The signature is the authorization.** HMAC-SHA256 over `v1\n<sha256>\n<exp>`, keyed by
`ARTIFACT_URL_SECRET`. `hmac.compare_digest`, never `==`; the MAC is checked **before**
`exp` so "expired" and "forged" are not an oracle (both are one 403 body,
`this download link is not valid`); the digest is rejected, never normalised; `exp` has
one spelling only. Nothing above the signature check touches the object store, so an
anonymous caller cannot drive an IAM `signBlob` call or learn which digests exist — and
`store.signed_urls == []` is asserted next to every refusal in the unit suite, because
"refused" and "refused before it cost anything" are different promises.

**Redirect, not proxy.** `design/production.md` promises artifacts are served without
touching the API process; a proxy would put an HTTP client in the production image
(`httpx` is a dev dependency) and hold a uvicorn threadpool slot per board for a 1.9 MB
transfer. So `Range`, `Content-Range`, suffix ranges and 416 — the R5 resume path — are
the **store's** RFC-correct implementation, proven against real MinIO in
`tests/test_artifact_download_minio.py`. `Cache-Control: no-store` on the redirect: its
target is a credential with minutes of life.

**One upstream signature per artifact per cache lifetime.** `SignedUrlCache` (modelled on
`CatalogCache`: per app, I/O-free to construct, one `asyncio.Lock`, `time.monotonic()`) is
now the **only** caller of `ObjectStore.signed_url` in the application — the tripwire in
`tests/test_api_deploy.py` was retargeted rather than deleted. A URL is reused only while
it still has `ARTIFACT_URL_REFRESH_MARGIN_S` (300 s) of life left, so no board is handed a
link that dies mid-transfer; failures are never cached.

**No database, and 404 never 422.** The download path issues no query at all: it keeps
working while Postgres is degraded, and the signature already carries the authorization. A
validly signed digest with nothing behind it ends as the store's own 404 after the
redirect. A malformed digest is 404 — a validation-error body is an oracle.

**A deploy no longer fails fast on a store outage (behaviour change).** `deploys.py` mints
locally and no longer touches the store, so the old `ObjectStoreError → 503` branch is
gone. It gained a **503 when `ARTIFACT_URL_SECRET` or `PUBLIC_BASE_URL` is unset**, before
any row exists: a 202 whose URL no board can redeem is the lie `NullCommandPublisher`
refuses to tell. `create_app()` emits a fifth startup WARNING for the same condition.

**Verification.** T1: `ruff` + `ruff format --check` + `mypy` green, **888 tests pass**
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
* **Refusals:** a `--ttl 1` link after `sleep 2` → **403**; the last signature character
  flipped → **403**; no `exp`/`sig` at all → **403**; `/v1/artifact/NOPE/bin` → **404**.
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

**Residual (recorded, not fixed): uvicorn's access log prints the signature.** The
application never logs a URL, a signature or the secret, but the access line
(`GET /v1/artifact/<sha>/bin?exp=…&sig=… 307`) contains the full request target, so anyone
who can read container logs can replay a link for its remaining life. Acceptable at v1 —
log access already implies host access, and the link expires — and the fix (an access-log
formatter that strips the query string, or turning `--access-log` off in prod) belongs
with the observability work.

**Production prerequisite (not done — `services/` is a different repo and prod env is
never edited without asking):** before the next prod deploy, `services/prod/.env` needs a
fresh `ARTIFACT_URL_SECRET` (`just artifact-secret`, 32-byte hex, **not** the dev value)
and `PUBLIC_BASE_URL=https://bingo.tvaroska.sk`, and the prod compose must pass both to
the `api` service. Without them every deploy answers 503 (with one startup WARNING) and
no board can download firmware. Rotating the secret later invalidates every link in
flight — at most `SIGNED_URL_TTL_S` of staged deploys, which simply re-deploy.

**QEMU (R1-fw-1 will need this):** a QEMU guest cannot reach `localhost` on the host — its
gateway is `10.0.2.2`. Export `FF_PUBLIC_BASE_URL=http://10.0.2.2:8080` (and
`S3_PUBLIC_ENDPOINT_URL=http://10.0.2.2:9000`, since the 307 target is a `localhost` URL
too) before `just up`, or the board gets a link it cannot resolve.
`docs/runbooks/agent-qemu.md` carries the detail.

**Spec proposals: none.** The URL shape, `exp`/`sig` query parameters and the 403/404
answers all conform to `spec/device-protocol.md` as written; nothing under `spec/` was
touched.

**Decisions & gotchas.** See `DECISIONS.md` 2026-09-17 (newest entry).

### Deploy outcomes from `up/status` (R1-be-4) — **LANDED 2026-09-17**

**What shipped.** The device half of the story R1-be-2 started: every state a board
reports on `up/status` becomes a `deploy_events` row. One new function,
`deploys.record_observed_status`, which is still the table's **only** writer
(`tests/test_invariants.py` holds that tripwire); a `StatusPayload` in
`ingestor/protocol.py`; a `STATUS` branch in `ingestor/handlers.py`; and
`EventType.DEVICE_DEPLOY` on the SSE channel, emitted only when a row was written. No
migration — `deploy_events` already had the shape.

**The retained topic is the whole design.** `up/status` is retained and the ingestor
re-`subscribe`s on every connect, so every reconnect replays the last status of every
board. Retained status is still **ingested** (unlike telemetry and log, which are
dropped when retained) — it is how an outcome published while the ingestor was down
arrives at all — so the duplicate is the writer's problem: `record_observed_status`
deduplicates on `(device_id, cmd_id, state)`. No unique index, deliberately: a repeated
state is legal data inside a retry. A replay records its state and does **not** move
`last_seen`.

**The row's shape is the KPI.** `is_terminal` comes from `TERMINAL_DEPLOY_STATES`, never
a literal; `artifact_version` and `from_version` are copied off the transaction's
`requested` row; `at` is the server's receipt time, never the device `ts`. A `cmd_id`
with no `requested` row is still recorded (versions NULL, INFO line). A state nobody has
heard of is recorded and is not terminal. A status with **no `cmd_id`** writes nothing
and only proves liveness — otherwise a booting board adds a row per boot to a table kept
forever. A board claiming `requested` gets a WARNING and no row.

**`detail` is sanitised.** `{"pct", "detail"}` — the wire's own key names — with control
characters stripped, 200 chars max, and `https?://\S+` redacted to `<url>`: the signed
link is a bearer credential and this table is kept forever. The wire model **coerces**
instead of raising (a non-int `pct` is dropped, a non-string `detail` is stringified),
because a `ValidationError` on a retained topic loses the same outcome on every single
reconnect.

**Cancel.** R1 ships no `cancel` command, so "cancel" here is the device's own
`rolled_back`/`failed`. A superseded in-flight deploy gets **no** server-authored
terminal row — see `DECISIONS.md` 2026-09-17 (R1-be-4), decision 4.

**Verification.** T1: `just test` — ruff, `ruff format --check`, mypy and **906 tests**
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
  row closed the reuse window); `{"state":"failed","detail":"sha256 mismatch"}` →
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

**Spec proposal (not applied — `spec/` is protected).** `spec/device-protocol.md`
should say that `up/status` `cmd_id` is **required** for a state belonging to a
transaction (a status with no `cmd_id` is unrecordable and only proves liveness), and
that a server may record device-reported states **idempotently** — a device
republishing the same `(cmd_id, state)` is a no-op, which is what makes the retained
topic safe to replay.

**Decisions & gotchas.** See `DECISIONS.md` 2026-09-17 (R1-be-4, newest entry).

### The device half — `esp_https_ota` + the `stage` handler (R1-fw-1) — **LANDED 2026-09-17**

**What shipped.** `agent/main/ff_ota.{c,h}`: a board that receives `stage` on `dn/cmd`
downloads the artifact through the signed link, writes the inactive A/B slot, verifies
the digest against flash, switches the boot partition and reboots into it — walking
`staging → downloading → verifying → staged → applying → rebooting` on `up/status`, the
same walk `simulator/device.py` has been publishing all along. Around it: seven
`FF_STATUS_*` constants and `ff_mqtt_publish_status()` in `ff_mqtt.{c,h}` (QoS 1,
**retained**, `{cmd_id,state,pct,detail}`), an `on_stage()` parser next to the existing
`on_command()`, `capabilities: ["ota"]` in `ff_identity.c`, and `esp_https_ota` in the
component's REQUIRES. `agent/version.txt` → `0.3.0`.

**Four properties that are silent wrong answers if you get them backwards** (the file's
own header says the same thing to the next editor):

1. **`downloading` is published once, not per chunk.** `deploy_events` is a log of
   transitions and `record_observed_status` dedups on `(device_id, cmd_id, state)`, so a
   per-chunk publish writes nothing and costs the broker a message per 4 KB. Progress is
   a serial log line every 10 %.
2. **The digest is taken by reading the partition BACK, after `esp_https_ota_finish()`.**
   Hashing the stream would hash bytes that were never on flash: `esp_ota_write` withholds
   the image header's first 16 bytes until the write completes. Reading the slot back is
   the only check that covers the flash write itself.
3. **A mismatch puts the boot partition back.** `finish()` has already called
   `esp_ota_set_boot_partition()` by the time we hash, so the undo is not tidiness —
   without it a board with a rejected image boots into it at the next power cut.
4. **The URL never appears in a log line or a `detail`.** It is the authorization
   (R1-be-3), so failures are described without it: "cannot open the artifact", not the
   link that could not be opened.

**One IDF defect had to be worked around** (`resolve_artifact_url()` in `ff_ota.c`).
`esp_https_ota` follows our `/v1/artifact/{sha}/bin` **307** by itself, but IDF v5.5.5
rebuilds the `Host` header wrong on a redirect: `esp_http_client_init()` uses
`_get_host_header(host, port)` (with `:port`), while the redirect path,
`esp_http_client_set_url()`, sets `Host` to the bare host **and only when the host string
changed** — so a redirect that keeps the host and changes only the port keeps hop one's
`Host` verbatim. An S3-compatible presigned URL signs `host`, so the store answers
**403 SignatureDoesNotMatch**, which surfaces as `esp_https_ota: File not found(403)`. The
agent therefore resolves the single hop itself — one header-only `GET` with
`disable_auto_redirect`, capturing `Location` from `HTTP_EVENT_ON_HEADER` — and hands the
final URL to `esp_https_ota_begin()`. Production (GCS on :443, which signs a portless
Host) never saw this; a self-hosted MinIO on :9000 — V2's shape — fails every deploy.

**Scope.** This stops at `rebooting` → `esp_restart()`. No `confirming`/`confirmed`, no
`cmd_id` persisted across the reboot: the image that comes back simply announces, and the
confirm/rollback pair already in `ff_mqtt.c` (dormant since R0) goes live as a consequence.
`apply: "on_command"` stages and stops — deliberately **not** `awaiting_safe_window`, which
an always-on board would never leave. A second `stage` while one runs is refused and
reported `failed` on the new `cmd_id`; `confirm_timeout_s` is parsed, logged if it differs
from the firmware's own 300 s, and otherwise ignored until R2.

**Verification.** T1: `just test` — ruff, `ruff format --check`, mypy and **909 tests**
green (two new tripwires in `tests/test_ff_cfg.py`: every state the firmware can publish
exists in the spec's machine, and the walk it performs is exactly the seven declared
states), plus `just agent-build esp32` → `BUNDLE OK`. The esp32 app grew 993,696 →
1,010,912 B, ratcheted in `tests/test_agent_power_and_size.py`; that is 51 % of the
1,966,080-byte slot, so the image can still download its own replacement.

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
  came up on the old image.
* **Duplicate:** re-publishing the identical `dn/cmd` → `duplicate command id=… — ignored
  (QoS 1 redelivery)`, no second download.

**The one thing QEMU cannot show:** `esp_restart()` itself. The emulator panics on the
next boot, in IDF's own `esp_timer_impl_init → esp_intr_alloc`, *before* `app_main` and in
whichever image it lands on — including the pre-OTA `0.3.0` that boots fine from power-on.
It is a soft-reset defect of the machine, not of the firmware; the runbook has the decoded
backtrace and the cold-restart workaround used above.

**Spec proposal (not applied — `spec/` is protected).** `spec/device-protocol.md` lists
`artifact.sig` and `broker/commands.py::stage_payload()` never emits it. Either the spec
drops the field or R2 implements it; until then the agent parses it as
optional-and-ignored. Second, smaller: the spec's machine should say that
`awaiting_safe_window` is for boards that *have* a window — an always-on agent staging
under `apply: "on_command"` stops at `staged`.

**Decisions & gotchas.** See `DECISIONS.md` 2026-09-17 (R1-fw-1, newest entry).

### The reported version is the one that BOOTED (R1-fw-2) — **LANDED 2026-09-17**

**What shipped.** `ff_identity_fw_version()` — one accessor, returning
`esp_app_get_description()->version`, i.e. the descriptor embedded in the image that is
*executing*. `up/announce` and `up/hb` both take `fw_version` from it and from nothing
else; `agent_version` stays a separate expression, because from R1 the agent can be a
component inside a user firmware and only `fw_version` moves. `log_boot_facts()` now also
prints the running image's version **and its OTA state**, and
`tests/test_ff_cfg.py::TestTheReportedVersionIsTheRunningOne` pins all of it.
`agent/version.txt` → `0.3.1`; the esp32 app grew 1,010,912 → 1,011,216 B (+304 B of
`.rodata`), ratcheted in `tests/test_agent_power_and_size.py`.

**This was a seam, not a behaviour change.** The happy path already read the running
descriptor. What did not exist was anything stopping the *plausible* refactor — reporting
`ff_ota_cmd_t::version`, the version the server asked for — which is right on every deploy
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

**Verification.** T1: `just test` — ruff, `ruff format --check`, mypy, **912 tests** green;
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

**QEMU note (runbook updated).** The positive half was driven with
`apply: "on_command"` + a power-cycle rather than `apply: "auto"`. R1-fw-1's workaround —
poll for `staged and bootable`, then `docker kill` before the agent reboots itself — loses
a **40 ms** race: the board soft-resets into ota_1, hits the emulator's known
`esp_timer_impl_init` panic, and the bootloader correctly retires the `PENDING_VERIFY`
image, leaving you on the old slot with `otadata` `aborted`. That run is itself a fourth
data point for this task: a board that ran `0.3.2` for a few hundred milliseconds and was
rolled back reports `0.3.1` again, with no state of ours involved.

**Spec proposal (not applied — `spec/` is protected).** `spec/device-protocol.md:104`'s
example payload shows `"agent_version": "0.3.0"`, now two releases stale. Cosmetic, an
example rather than a contract, and worth a refresh the next time that file is opened for
a real reason.

### The dashboard half (R1-fe-1) — **LANDED 2026-09-17**

R1's user-visible claim — *"push firmware from the dashboard and watch the version
change"* — closes here. Every piece behind it existed; nothing on screen reached it.

**Two read endpoints, no new machinery.**

* `GET /v1/artifact` (admin) lists every deployable label: `target`, `version`, `sha256`,
  `size_bytes`, `partition_layout`, `kind`, `created_at`, ordered `(target, created_at
  DESC, version)`. **There is no `url` in it** — a download link is a short-lived bearer
  credential minted per deploy, never a field in a list. It lives on
  `api/routers/artifacts.py` (admin-only); the same `/v1/artifact` prefix is *also* the
  public download route in `api/routers/artifact_download.py`, where the signature is the
  authorization, so the list route asserts a 401 both bare and with a `?exp=&sig=` bolted
  on. Two labels over one digest (an esp32 `1.5.0` and an esp32c6 `1.5.0` built from the
  same bytes) are two rows with one `sha256`, which is ordinary.
* `DeviceSummary.deploy` carries the newest deploy transaction's current state —
  `cmd_id`, `state`, `at`, `is_terminal`, `artifact_version`, `from_version`, `pct`,
  `detail` — or `null` for a board never deployed to. It is a column of the fleet read
  model, not an endpoint: see `DECISIONS.md` 2026-09-17. The SQL
  (`DISTINCT ON (device_id) … ORDER BY at DESC, id DESC`) lives in `deploys.py`, the
  module that owns `deploy_events`, and `devices.py` calls it exactly the way it already
  calls `progress.latest_progress()`.

**The cell.** `frontend/src/DeployCell.tsx` (rendering + the POST) and
`frontend/src/deploy.ts` (the label table + one `GET /v1/artifact` on mount, no poll — an
artifact appears when a human uploads one and there is no event type for it). The picker
offers only the versions whose `target` is this board's `platform_type`, newest
preselected, because the server refuses a mismatch and offering one is offering a 409.
The live line is read from `device.deploy` and from nothing this component remembers,
which is why a reload and a second tab agree.

Wording is most of the value here: `downloading the image`, `image written, waiting to
reboot`, `done — running the new version`. A lookup with an `?? raw` fallback, the same
idiom as `STAGE_LABELS`, because `deploy_events.state` is TEXT with no CHECK and an agent
newer than this dashboard must render as itself rather than vanish. `pct` is text and
never a bar; `awaiting_safe_window` gets a full sentence and no error styling.

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
announces its new `fw_version` on its next **connect**, not on its next heartbeat — the
heartbeat payload does not carry the field, and `device.py` says as much ("announced on
the next connect"). The version flip is observed by making the board reconnect
(`docker compose restart mosquitto`), which is what a rebooting board does anyway.

**Deliberately not in R1:** an upload UI (curl only), a deploy history/timeline, group
deploy, cancel (there is no server-authored cancel), and a rollback button (R2).

**Spec proposal (not applied — `spec/` is protected).** `spec/device-protocol.md` should
say outright that `up/status.state` is an **open** vocabulary: the server stores it as TEXT
with no CHECK, `DeployState` is advisory, and both the server and this dashboard treat an
unrecognised value as a legitimate state to record and render verbatim. That is already
the behaviour on both sides; the spec only implies it by listing examples.

## Phase 2: R2 — Safe deploy: verify + auto-rollback ⭐

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R2-FW-1 | Checksum verify before apply | P0 | 1d |
| R2-FW-2 | A/B slot apply (OTA0/OTA1), atomic switch | P0 | 1.5d |
| R2-BE-1 | Observe confirm/rollback outcome; record the result to `deploy_events` | P0 | 1d |
| R2-FW-3 | **Device-side** confirm timer armed pre-reboot → `esp_ota` self-rollback on timeout | P0 | 1d |
| R2-FE-1 | Dashboard shows `good` vs `rolled-back` per device | P0 | 0.5d |
| R2-TEST-1 | Push a *deliberately broken* build → board auto-recovers | P0 | 1d |

**Done when:** a deliberately broken build deploys → the board auto-recovers to the
previous version.

## De-risking

Run a **throwaway OTA + auto-rollback spike during R0–R1** on real flaky Wi-Fi —
prove auto-rollback saves a bad build before relying on it. (Tracked as parallel work
in TODO.md.)
