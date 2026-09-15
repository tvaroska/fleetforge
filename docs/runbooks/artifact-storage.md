# Runbook — artifact storage

The object store behind `put` / `get` / `signed_url` / `delete`
(`src/fleetforge/storage/`). Two backends, one Protocol: **MinIO** in the dev stack and
for V2 self-hosting, **GCS** in production.

| | Bucket | Prefix | Credential |
|---|---|---|---|
| dev (`just up`) | `fleetforge` (MinIO, in-stack) | *(none — dedicated bucket)* | `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`, obviously fake, in `.env.example` |
| production | `gs://btvaroska` | `fleetforge/` | service account `fleetforge-artifacts@btvaroska`, reached by **impersonation** over the runtime's ADC (`GCS_IMPERSONATE_SERVICE_ACCOUNT`) — **no key file**, because `btvaroska` forbids minting one |

## `gs://btvaroska` is a SHARED bucket

It already holds this estate's `secrets/` (the `.env` backups from root `CLAUDE.md`),
`podcasts/` and `audio/` (the `boris` private podcast feed), `backup/`, `production/`
and `data/`. **Fleetforge owns exactly one prefix: `fleetforge/`.**

A key reaches the adapter from an HTTP request body (R1's upload endpoint), so the
prefix is confined **twice**, and both must exist:

1. **In the adapter** — `storage/objectstore.py::resolve_key` rejects (never repairs)
   any key that could escape: `..`, a leading `/`, `//`, a backslash, control or
   non-ASCII characters, `?`/`#`, anything over 512 characters.
2. **In IAM** — `fleetforge-artifacts@btvaroska` holds `roles/storage.objectAdmin` under a
   **condition** limiting it to `…/objects/fleetforge/…`. Confirmed present on the bucket
   policy on 2026-09-15 (title `fleetforge-prefix-only`, expression
   `resource.name.startsWith("projects/_/buckets/btvaroska/objects/fleetforge/")`), and
   measured through our own adapter the same day: a `put` to `secrets/…` with
   `GCS_PREFIX=` empty — confinement (1) deliberately disabled — fails `Forbidden`.

Neither alone is enough. (1) is defeated by a future caller that bypasses the helper;
(2) is what still holds when that happens. GCS also evaluates the *signer's* permissions
when a signed URL is redeemed, so a signed URL for an out-of-prefix object is worthless
even if one were somehow generated.

## Key layout — where an artifact actually lands

Artifacts are content-addressed (S0-infra-4, `src/fleetforge/storage/blobs.py`). The key
a caller hands to `ObjectStore` is **store-relative**: the `fleetforge/` half of the
production path is the store's prefix, applied by `resolve_key`, and is never part of
the key.

| | Prefix | Key handed to the store | Resulting object |
|---|---|---|---|
| dev (MinIO) | *(none)* | `blobs/sha256/<hex>` | `blobs/sha256/<hex>` in bucket `fleetforge` |
| production (GCS) | `fleetforge/` | `blobs/sha256/<hex>` | `gs://btvaroska/fleetforge/blobs/sha256/<hex>` |

| both | *(as above)* | `agent/index.json` | the one **mutable** object in the store |

So in a bucket listing both deployments read the same way, and a key that already starts
with `fleetforge/` is a bug (it would store `fleetforge/fleetforge/blobs/…`) — the
adapters refuse it rather than repairing it. The digest is lowercase hex, always; an
uppercase spelling is rejected, never normalised, because it would be a second object
holding one artifact.

Blobs are written with `Cache-Control: public, max-age=31536000, immutable` as real
object metadata — that is what a device's GET through the signed URL receives. Prove
both against the configured backend:

```bash
just storage-check --blob
```

It writes a payload at `blobs/sha256/<that payload's digest>`, fetches it over the signed
URL and prints the `cache-control:` line it actually got back. To check the header
yourself, add `--keep` and `curl` the printed URL with **GET**, not `curl -I`:

```bash
curl -sS -D - -o /dev/null "$URL" | grep -i cache-control
```

`curl -I` sends `HEAD`, and SigV4 signs the HTTP method — a URL presigned for `GET`
answers `403` to a `HEAD`. That 403 means the method is wrong, not the signature.

## Provisioning the production service account

Run once, from a shell authenticated as an owner (`devserver@btvaroska` works).

```bash
PROJECT=btvaroska
SA=fleetforge-artifacts

gcloud iam service-accounts create $SA --project=$PROJECT \
  --display-name="Fleetforge artifact store"

# Objects under fleetforge/ ONLY. Never grant this unconditionally, and never reuse
# devserver@ or the compute default SA — those can read gs://btvaroska/secrets/.
gcloud storage buckets add-iam-policy-binding gs://btvaroska \
  --member="serviceAccount:$SA@$PROJECT.iam.gserviceaccount.com" \
  --role=roles/storage.objectAdmin \
  --condition='expression=resource.name.startsWith("projects/_/buckets/btvaroska/objects/fleetforge/"),title=fleetforge-prefix-only,description=Objects under fleetforge/ only'

# The credential. NOT a key file: `keys create` is refused by an org policy (below), and
# a key would be the wrong answer anyway. Grant every runtime that must reach the store
# permission to MINT A TOKEN for this SA, and set GCS_IMPERSONATE_SERVICE_ACCOUNT to its
# email. Run once per runtime principal.
for MEMBER in \
  "serviceAccount:mainsite@sites-470716.iam.gserviceaccount.com" \
  "serviceAccount:devserver@btvaroska.iam.gserviceaccount.com" ; do
  gcloud iam service-accounts add-iam-policy-binding \
    $SA@$PROJECT.iam.gserviceaccount.com --project=$PROJECT \
    --member="$MEMBER" --role=roles/iam.serviceAccountTokenCreator
done

# Read it back — this is the only part an agent may run; the grants above are the owner's.
gcloud iam service-accounts get-iam-policy \
  $SA@$PROJECT.iam.gserviceaccount.com --project=$PROJECT --format=json
```

The service account, the conditional binding and **both tokenCreator grants above already
exist** in `btvaroska` (SA and binding from R0-be-6; the grants added 2026-09-14/15).
`mainsite@sites-470716` is prod's attached identity and `devserver@btvaroska` is this dev
VM's. No key file exists, and none can be minted — see the next section.

IAM conditions are only allowed on buckets with uniform bucket-level access;
`gs://btvaroska` already has it, along with `public_access_prevention: enforced`.
**PAP is not a problem and must not be "fixed":** a V4 signed URL is not public access.
Granting `allUsers` is exactly what PAP exists to block.

## `btvaroska` forbids keys — which is why the credential is an impersonation

**RESOLVED 2026-09-15 (S0-infra-5).** This section used to read *BLOCKED*. The blocker was
two facts and only one of them was about GCP: the org will not issue a key, **and the
adapter accepted nothing else**. The second half is fixed — `storage/factory.py` now takes
`GCS_IMPERSONATE_SERVICE_ACCOUNT` — and the whole path has been round-tripped against the
real bucket (see *Verified against real GCS* below). The org-policy fact below is
unchanged and is not going away.

`gcloud iam service-accounts keys create` on the SA above fails:

```
Key creation is not allowed on this service account.
constraints/iam.disableServiceAccountKeyCreation
```

The constraint is **inherited and enforced** (`gcloud resource-manager org-policies
describe constraints/iam.disableServiceAccountKeyCreation --project=btvaroska
--effective` → `enforced: true`; there is no project-level override). The org is
`tvaroska.altostrat.com`, where this constraint is on by default.

Do not "fix" this by turning the constraint off. The answer taken is:

1. **Impersonation (keyless) — IMPLEMENTED.** The runtime principal holds
   `roles/iam.serviceAccountTokenCreator` on
   `fleetforge-artifacts@btvaroska.iam.gserviceaccount.com` and impersonates it.
   V4 signing goes through the IAM `signBlob` API instead of a local private key —
   `google.auth.impersonated_credentials.Credentials` is a `Signer`, so
   `blob.generate_signed_url(version="v4")` works with no extra kwargs. Set
   `GCS_IMPERSONATE_SERVICE_ACCOUNT` (an email, not a credential; safe to commit). It is
   **mutually exclusive** with `GCS_CREDENTIALS_FILE`, and neither set is still a refusal
   — there is no silent ADC fallback.
2. **An exemption**, if this project is ever meant to hold keys: add a project-level
   `constraints/iam.disableServiceAccountKeyCreation` override, mint the key, and treat
   the exemption as the thing to review at rotation time. Not taken, and not needed: the
   key-file path in `factory.py` still works and is kept only for a self-hosted V2
   deployment in a project with no such constraint.

Either way the conditional binding stays exactly as it is — it constrains the SA, not
how the SA is authenticated.

### Verified against real GCS, 2026-09-15 (S0-infra-5)

From this dev box, with **no key file anywhere** and the VM's attached identity
(`devserver@btvaroska`) as the impersonation source:

```bash
GCS_BUCKET=btvaroska GCS_PREFIX=fleetforge/ \
GCS_IMPERSONATE_SERVICE_ACCOUNT=fleetforge-artifacts@btvaroska.iam.gserviceaccount.com \
  just storage-check --backend gcs --blob      # -> SELFTEST OK
```

* `backend  gcs bucket=btvaroska prefix=fleetforge/ creds=impersonated(fleetforge-artifacts@…)`
* put → get (sha256 matches) → signed URL → **fetched with no credentials at all** →
  `cache-control: public, max-age=31536000, immutable` → delete → `ObjectNotFound` →
  idempotent second delete.
* **`signBlob` is verified, not assumed.** The URL carries
  `X-Goog-Credential=fleetforge-artifacts@btvaroska.iam.gserviceaccount.com/…/goog4_request`
  and an unauthenticated GET returns the bytes. There is no private key in the process, so
  that signature can only have come from the IAM API.
* **Containment measured through the adapter**, with `GCS_PREFIX=` empty so the in-process
  confinement is deliberately off and the *IAM* condition is what answers:
  `--key "secrets/ff-impersonation-probe-$(uuidgen).bin"` exits non-zero with
  `SELFTEST FAILED: ObjectStoreError: gcs put of secrets/… failed: Forbidden`.

Still owed: the same run **on prod**, whose runtime identity is `mainsite@sites-470716`
(granted, so it is expected to pass, but its metadata server and egress are its own). It
is S0-infra-6's first act; the probe is in
`.claude/plans/S0-infra-5-gcs-credential-that-is-not-a-key-file.md` → AC3a.

## The credential already exists — it is just not a key

Measured from `prod` on 2026-09-11:

* The VM's attached identity is **`mainsite@sites-470716.iam.gserviceaccount.com`**,
  scope `cloud-platform`.
* It reads `gs://btvaroska` **cross-project** today: `gcloud storage ls gs://btvaroska/`
  lists every prefix. `gs://btvaroska/fleetforge/` returns *"matched no objects"* — an
  empty prefix, **not** a 403.
* Read off the bucket policy on 2026-09-15: `mainsite@sites-470716` holds
  `roles/storage.objectAdmin` on `gs://btvaroska` **unconditionally, whole bucket**. That
  is the containment argument below, now a policy fact rather than an inference from a
  listing.
* **`signBlob` is VERIFIED** as of 2026-09-15 — from the dev box, through our own adapter,
  under `devserver@btvaroska`. See *Verified against real GCS* above. The prod identity
  `mainsite@sites-470716` holds the same tokenCreator grant, so the same run on prod is
  expected to pass; it has not been executed yet.

So the two consumers are **not** blocked to the same degree, and the difference is worth
keeping straight:

| Consumer | Needs | Status |
|---|---|---|
| Agent bundles from the object store (`docs/features/infrastructure.md`) | authenticated **reads** only — the flasher is a browser on an admin session, the API streams the bytes, no signed URL in the path | **Unblocked** (S0-infra-5). Wiring the prod container is S0-infra-6. |
| R1 artifact delivery | **V4 signing** — a device holds no GCP identity, so the signature *is* the authorization | **Unblocked and verified** 2026-09-15: a `signBlob`-signed URL was redeemed with no credentials. Not yet exercised from prod. |

### But do not simply switch on ADC

`mainsite` is the **shared** VM service account for the whole estate, and the listing
above is the proof: it can read `secrets/` (this estate's `.env` backups), `podcasts/`,
`backup/` — everything. Plain ADC would hand fleetforge read access to every other app's
secrets and make the prefix condition on `fleetforge-artifacts` decorative.

This is a **stronger** reason to refuse ADC than the signing one, and it survives now that
`signBlob` is known to work. Option 1 above (impersonation) is therefore the answer for
**containment first** and signing second: `mainsite` impersonates `fleetforge-artifacts`,
which is scoped to `fleetforge/` by the condition that already exists. `factory.py` and
`storage/gcs.py` both say so in their docstrings, in that order.

## Nothing secret enters git

Production has no key file at all, and `GCS_IMPERSONATE_SERVICE_ACCOUNT` is a service
account **email** — not a credential, and exactly what proves no key was used, which is
why `describe` (and therefore the selftest, and the logs) prints it in full.

Where a key file is still used (self-hosted V2), `secrets/` is in `.gitignore`. A
committed key is a full compromise of `gs://btvaroska`, including the `.env` backups under
`secrets/`. `.env.example` carries the *path*, commented out; only `GCS_CREDENTIALS_FILE`
ever names it, and nothing logs its contents. Check before every commit:

```bash
git status --short | grep -i secret     # must print nothing
```

## Gotcha: this credential cannot LIST, and that is correct

`storage.objects.list` is evaluated against the **bucket**, not against an object, so a
resource-name condition can never match it. Every
`gcloud storage ls gs://btvaroska/fleetforge/` as `fleetforge-artifacts` returns 403. The
adapter has no `list` verb, so nothing is broken — but expect to lose an hour to it
otherwise. Use the `devserver` credentials for any manual listing:

```bash
gcloud storage ls gs://btvaroska/fleetforge/          # devserver@, works
```

## Gotcha: on this dev box, ADC is a USER, not `devserver@` — and it cannot impersonate

`gcloud config` here shows the active account as `devserver@btvaroska`, but
`google.auth.default()` returns a `google.oauth2.credentials.Credentials` — an
`authorized_user` left behind by `gcloud auth application-default login`, because the ADC
**file** wins over the metadata server. That user principal has no tokenCreator binding,
so `just storage-check --backend gcs` with impersonation fails:

```
SELFTEST FAILED: ObjectStoreError: could not impersonate fleetforge-artifacts@…:
the runtime identity was refused a token. Grant it roles/iam.serviceAccountTokenCreator …
```

The fix is not a grant. Take the ADC file out of the search path for one command and the
metadata server answers with this VM's attached identity, which **is** granted:

```bash
mkdir -p /tmp/no-gcloud-adc
CLOUDSDK_CONFIG=/tmp/no-gcloud-adc GCS_BUCKET=btvaroska GCS_PREFIX=fleetforge/ \
GCS_IMPERSONATE_SERVICE_ACCOUNT=fleetforge-artifacts@btvaroska.iam.gserviceaccount.com \
  just storage-check --backend gcs --blob        # -> SELFTEST OK
```

That is the dev-box fast loop against real GCS. It matters beyond convenience: the same
precedence bites anywhere an ADC file exists next to a service identity, and the symptom
(a 403 on `iam.serviceAccounts.getAccessToken`) names neither cause.

## Gotcha: every `signed_url` is now an IAM API call

Under impersonation `generate_signed_url(version="v4")` POSTs to the IAM `signBlob`
endpoint — it is **not** local CPU any more, and `storage/gcs.py` says so. Consequences:

* It is subject to `OBJECT_STORE_TIMEOUT_S` like every other verb. The adapter holds an
  opaque `bucket_factory` and cannot tell a key file from an impersonation, so it runs
  signing through `asyncio.to_thread` under `_guard` on **both** paths — one thread hop
  wasted with a key file, a hung API avoided without one.
* It has latency and a quota. A per-request signed URL is one extra round trip to Google.
* **IAM propagation can 403 a fresh grant.** Root `docs/ops-log.md` F-2026-08-18-001: the
  `boris` podcast feed 500'd once with `403 PERMISSION_DENIED: iam.serviceAccounts.signBlob`
  right after a deploy and recovered by itself two requests later. Retry for ~5 minutes
  before believing a negative result. Which 403 you got matters: `getAccessToken` denied
  means the *caller* is not a granted member; `signBlob` denied means propagation or a
  missing role on the target.

## Rotation

**There is nothing to rotate in production.** The credential is an impersonation, so the
only long-lived thing is an IAM grant, and the access token it mints lives an hour and
refreshes itself. To revoke, remove the member:

```bash
gcloud iam service-accounts remove-iam-policy-binding \
  fleetforge-artifacts@btvaroska.iam.gserviceaccount.com --project=btvaroska \
  --member="serviceAccount:mainsite@sites-470716.iam.gserviceaccount.com" \
  --role=roles/iam.serviceAccountTokenCreator
```

Where a key file *is* used (self-hosted V2):

```bash
gcloud iam service-accounts keys list \
  --iam-account=fleetforge-artifacts@btvaroska.iam.gserviceaccount.com
gcloud iam service-accounts keys create secrets/fleetforge-artifacts-new.json \
  --iam-account=fleetforge-artifacts@btvaroska.iam.gserviceaccount.com
# point GCS_CREDENTIALS_FILE at the new file, restart the api, verify:
just storage-check --backend gcs
gcloud iam service-accounts keys delete <OLD_KEY_ID> \
  --iam-account=fleetforge-artifacts@btvaroska.iam.gserviceaccount.com
```

Signed URLs already issued keep working until they expire (30 min by default,
`SIGNED_URL_TTL_S`) — revoking a key or a grant does not revoke outstanding URLs, which is
why the TTL is short.

## Verifying the store

```bash
just minio-up && just storage-check              # dev MinIO, from the host
just storage-check --backend gcs                 # GCS, needs GCS_* in .env
docker compose exec -T api python -m fleetforge.storage selftest   # from the container
```

The selftest prints the backend, bucket, prefix and credential **mode** (`creds=key-file`
or `creds=impersonated(<email>)`) — **never a credential, and never a file path** — then
does
put → get (sha256) → `signed_url` (fetched over HTTP) → delete → `ObjectNotFound` →
a second, idempotent delete, and ends `SELFTEST OK`.

**Reading the container run:** the printed URL's host is `localhost:9000`, *not*
`minio:9000`, and the selftest says it could not fetch it from inside the network. That
is correct. A presigned URL signs the `Host` header, so the URL a device is handed must
be generated against an endpoint reachable from outside the compose network
(`S3_PUBLIC_ENDPOINT_URL`); rewriting the host after signing invalidates the signature.
The selftest re-signs against the internal endpoint to prove signing itself works, and
prints the device-facing URL for you to `curl` from the host. An **HTTP 403/404** from
the URL is a real failure; a **connection refused** from inside the container is not.

`just storage-check --key '../escape.bin'` must exit non-zero with `ObjectKeyError`.
`just storage-check --blob` additionally checks the content-addressed key and the
`Cache-Control` header (see *Key layout* above); `--key` and `--blob` are mutually
exclusive, because a blob's key is its digest and nothing else.

## Publishing agent bundles (S0-infra-6)

The agent bundles the browser flasher writes to a board are ordinary artifacts in this
store. The app image ships none, so **publishing is how a firmware fix reaches a board** —
there is no redeploy in the path:

```bash
just agent-publish esp32          # verify, upload, re-point the index
just agent-list                   # what is current, and what can be rolled back to
just agent-rollback esp32 <manifest-digest>
```

What lands in the store:

* every part and the manifest at `blobs/sha256/<digest>`, immutable, shared between
  targets when the bytes are identical (the partition table and ota-data usually are);
* `agent/index.json` — the **only mutable object in the scheme**: current manifest digest
  per `(target, partition_layout)`, plus up to 20 superseded digests per target, written
  with `Cache-Control: no-store`. Blobs go up first and the index last, so an interrupted
  publish is invisible rather than half-applied.

The API re-reads the index at most once per `AGENT_CATALOG_TTL_S` (default 60 s), so a
publish is visible within a minute with nothing restarted. Nothing is ever deleted: a
rollback re-points the index at a digest that is still there, which is why it is an index
write and not a rebuild.

### Publishing to production's GCS from this box

```bash
mkdir -p /tmp/no-gcloud-adc
CLOUDSDK_CONFIG=/tmp/no-gcloud-adc \
OBJECT_STORE_BACKEND=gcs GCS_BUCKET=btvaroska GCS_PREFIX=fleetforge/ \
GCS_IMPERSONATE_SERVICE_ACCOUNT=fleetforge-artifacts@btvaroska.iam.gserviceaccount.com \
just agent-publish esp32
```

`CLOUDSDK_CONFIG=/tmp/no-gcloud-adc` (any empty directory) is not optional and is the same gotcha as *on this dev box,
ADC is a USER* above: the ADC **file** here is a user principal with no
`roles/iam.serviceAccountTokenCreator` on `fleetforge-artifacts`, while an empty config
dir makes google-auth fall through to the metadata server, which answers with
`devserver@btvaroska` — the identity that *is* granted it (DECISIONS.md 2026-09-15).

**Order matters on a release:** publish the bundles *before* deploying an app image that
no longer carries them, or the flasher answers 503 in the window between. And never run
`just agent-build*` on `prod` — bundles are built here and published there.

## Production configuration

**Still not wired on the running container as of S0-infra-6.** `services/prod/` lives in
the `services` repo and its env is protected (root `CLAUDE.md`: ask first), so S0-infra-6
PROPOSES the change rather than applying it — and **until it is applied, prod's flasher
answers 503**, because the deployed app image will no longer carry the bundles. Four env
lines wire it, and **nothing is mounted**:

```yaml
      OBJECT_STORE_BACKEND: gcs
      GCS_BUCKET: btvaroska
      GCS_PREFIX: fleetforge/
      GCS_IMPERSONATE_SERVICE_ACCOUNT: fleetforge-artifacts@btvaroska.iam.gserviceaccount.com
```

* **Ask before changing `services/prod/.env` or the prod compose** (root `CLAUDE.md`), and
  run `just backup` afterwards if a value lands in `.env`. None of the four lines above is
  a secret, so they belong in the compose file, not in `.env`.
* No key file, no mount, no `GCS_CREDENTIALS_FILE`. The container reaches the GCE metadata
  server over the default bridge; if that fails the symptom is
  `SELFTEST FAILED: ObjectStoreError: …Application Default Credentials…` — a
  credential-resolution failure is an `ObjectStoreError` (503), never an
  `ObjectStoreConfigError` (500), and never a hang: it runs under the verb's timeout.
* Verify from the container with no key anywhere:
  `docker compose exec -T fleetforge-api python -m fleetforge.storage selftest --backend gcs --blob`.

Setting **both** `S3_*` and `GCS_*` is refused rather than resolved by precedence —
"which bucket did my firmware go to?" must not be answered by reading a factory. Unset
one, or set `OBJECT_STORE_BACKEND` explicitly. The same rule applies to the credential:
`GCS_CREDENTIALS_FILE` and `GCS_IMPERSONATE_SERVICE_ACCOUNT` together is
`ObjectStoreConfigError: … mutually exclusive …`, and neither set is a refusal too.
