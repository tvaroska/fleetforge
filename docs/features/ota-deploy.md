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
