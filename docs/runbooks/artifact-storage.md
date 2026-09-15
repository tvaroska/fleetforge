# Runbook — artifact storage

The object store behind `put` / `get` / `signed_url` / `delete`
(`src/fleetforge/storage/`). Two backends, one Protocol: **MinIO** in the dev stack and
for V2 self-hosting, **GCS** in production.

| | Bucket | Prefix | Credential |
|---|---|---|---|
| dev (`just up`) | `fleetforge` (MinIO, in-stack) | *(none — dedicated bucket)* | `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`, obviously fake, in `.env.example` |
| production | `gs://btvaroska` | `fleetforge/` | service account `fleetforge-artifacts@btvaroska`, key file, **never committed** |

## `gs://btvaroska` is a SHARED bucket

It already holds this estate's `secrets/` (the `.env` backups from root `CLAUDE.md`),
`podcasts/` and `audio/` (the `boris` private podcast feed), `backup/`, `production/`
and `data/`. **Fleetforge owns exactly one prefix: `fleetforge/`.**

A key reaches the adapter from an HTTP request body (R1's upload endpoint), so the
prefix is confined **twice**, and both must exist:

1. **In the adapter** — `storage/objectstore.py::resolve_key` rejects (never repairs)
   any key that could escape: `..`, a leading `/`, `//`, a backslash, control or
   non-ASCII characters, `?`/`#`, anything over 512 characters.
2. **In IAM** — the service-account key holds `roles/storage.objectAdmin` under a
   **condition** limiting it to `…/objects/fleetforge/…`.

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

mkdir -p secrets && chmod 700 secrets
gcloud iam service-accounts keys create secrets/fleetforge-artifacts.json \
  --project=$PROJECT --iam-account=$SA@$PROJECT.iam.gserviceaccount.com
chmod 600 secrets/fleetforge-artifacts.json
```

The service account and the conditional binding above **already exist** in `btvaroska`
(created by R0-be-6). The **key does not** — see the next section.

IAM conditions are only allowed on buckets with uniform bucket-level access;
`gs://btvaroska` already has it, along with `public_access_prevention: enforced`.
**PAP is not a problem and must not be "fixed":** a V4 signed URL is not public access.
Granting `allUsers` is exactly what PAP exists to block.

## BLOCKED: the adapter is key-only, and `btvaroska` forbids keys

**Read this section together with *The credential already exists* below.** The blocker is
two facts, and only one of them is about GCP: the org will not issue a key, **and the
adapter accepts nothing else**. The second half is ours to fix and is ordinary backend
work. Do not carry this forward as "GCS is unreachable from prod" — that is not what was
measured.

`gcloud iam service-accounts keys create` on the SA above fails:

```
Key creation is not allowed on this service account.
constraints/iam.disableServiceAccountKeyCreation
```

The constraint is **inherited and enforced** (`gcloud resource-manager org-policies
describe constraints/iam.disableServiceAccountKeyCreation --project=btvaroska
--effective` → `enforced: true`; there is no project-level override). The org is
`tvaroska.altostrat.com`, where this constraint is on by default. So **GCS has never been
round-tripped against the real service** — the adapter's GCS path is exercised only by
unit tests. Do not read a green dev stack as evidence that production storage works.

Do not "fix" this by turning the constraint off. Pick one:

1. **Workload-identity / impersonation (preferred, keyless).** Grant the runtime
   principal `roles/iam.serviceAccountTokenCreator` on
   `fleetforge-artifacts@btvaroska.iam.gserviceaccount.com`, and let it impersonate.
   V4 signing then goes through the IAM `signBlob` API instead of a local private key —
   `google.auth.impersonated_credentials.Credentials` is a `Signer`, so
   `blob.generate_signed_url(credentials=…, version="v4")` works unchanged. **The
   adapter does not implement this yet**: it needs a `GCS_IMPERSONATE_SERVICE_ACCOUNT`
   setting in `storage/factory.py`, kept mutually exclusive with
   `GCS_CREDENTIALS_FILE` so there is still no silent ADC fallback. Note that signing
   stops being local and free: every `signed_url` becomes an IAM API call, so it needs a
   timeout and it can rate-limit.
2. **An exemption**, if this project is meant to hold keys: add a project-level
   `constraints/iam.disableServiceAccountKeyCreation` override, mint the key, and treat
   the exemption as the thing to review at rotation time.

Either way the conditional binding stays exactly as it is — it constrains the SA, not
how the SA is authenticated.

## The credential already exists — it is just not a key

Measured from `prod` on 2026-09-11:

* The VM's attached identity is **`mainsite@sites-470716.iam.gserviceaccount.com`**,
  scope `cloud-platform`.
* It reads `gs://btvaroska` **cross-project** today: `gcloud storage ls gs://btvaroska/`
  lists every prefix. `gs://btvaroska/fleetforge/` returns *"matched no objects"* — an
  empty prefix, **not** a 403.
* **`signBlob` is unverified.** Both probes (a metadata-token `POST …:signBlob` and
  `gcloud iam service-accounts sign-blob`) were refused by the dev-box sandbox before
  reaching prod. Treat V4-signing-without-a-key as *expected to work*, not *known to
  work*, until someone runs it.

So the two consumers are **not** blocked to the same degree, and the difference is worth
keeping straight:

| Consumer | Needs | Status |
|---|---|---|
| Agent bundles from the object store (`docs/features/infrastructure.md`) | authenticated **reads** only — the flasher is a browser on an admin session, the API streams the bytes, no signed URL in the path | **Available today.** Blocked only on adapter support for a non-key credential. |
| R1 artifact delivery | **V4 signing** — a device holds no GCP identity, so the signature *is* the authorization | Needs the `signBlob` path, and needs it verified. |

### But do not simply switch on ADC

`mainsite` is the **shared** VM service account for the whole estate, and the listing
above is the proof: it can read `secrets/` (this estate's `.env` backups), `podcasts/`,
`backup/` — everything. Plain ADC would hand fleetforge read access to every other app's
secrets and make the prefix condition on `fleetforge-artifacts` decorative.

This is a **stronger** reason to refuse ADC than the signing one `factory.py` cites, and
it survives even if `signBlob` turns out to work perfectly. Option 1 above
(impersonation) is therefore the answer for **containment first** and signing second:
`mainsite` impersonates `fleetforge-artifacts`, which is scoped to `fleetforge/` by the
condition that already exists.

## The key file never enters git

`secrets/` is in `.gitignore`. A committed key is a full compromise of `gs://btvaroska`,
including the `.env` backups under `secrets/`. `.env.example` carries the *path*,
commented out; only `GCS_CREDENTIALS_FILE` ever names it, and nothing logs its contents.
Check before every commit:

```bash
git status --short | grep -i secret     # must print nothing
```

## Gotcha: this key cannot LIST, and that is correct

`storage.objects.list` is evaluated against the **bucket**, not against an object, so a
resource-name condition can never match it. Every
`gcloud storage ls gs://btvaroska/fleetforge/` with this credential returns 403. The
adapter has no `list` verb, so nothing is broken — but expect to lose an hour to it
otherwise. Use the `devserver` credentials for any manual listing:

```bash
gcloud storage ls gs://btvaroska/fleetforge/          # devserver@, works
```

## Gotcha: no ADC fallback, on purpose

The GCS adapter **requires** `GCS_CREDENTIALS_FILE` and raises `ObjectStoreConfigError`
at construction when it is unset or missing. Application Default Credentials on a GCE VM
come from the metadata server and carry no private key, so V4 signing would silently
need an IAM `signBlob` round trip — and ADC would pick up the project-wide compute
default service account, which is the credential the prefix condition exists to avoid.
**The second half of that sentence is the load-bearing one**, and it was measured on
2026-09-11: on `prod` that identity is `mainsite@sites-470716`, and it can read the whole
of `gs://btvaroska` including `secrets/`. See *The credential already exists* above.
A related symptom: with no credentials configured at all, the google client dials the
metadata server and *hangs for seconds* on a non-GCP host.

## Rotation

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
`SIGNED_URL_TTL_S`) — revoking a key does not revoke outstanding URLs, which is why the
TTL is short.

## Verifying the store

```bash
just minio-up && just storage-check              # dev MinIO, from the host
just storage-check --backend gcs                 # GCS, needs GCS_* in .env
docker compose exec -T api python -m fleetforge.storage selftest   # from the container
```

The selftest prints the backend, bucket and prefix — **never a credential** — then does
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

## Production configuration

Production (R0-infra-3 / R1) sets `GCS_BUCKET=btvaroska`, `GCS_PREFIX=fleetforge/` and
`GCS_CREDENTIALS_FILE` in `services/prod/.env`, and mounts the key read-only. Two things
to get right:

* **Ask before changing `services/prod/.env`** (root `CLAUDE.md`), and run `just backup`
  afterwards so the value reaches `gs://btvaroska/secrets/`.
* The container runs as non-root `appuser`, so a **0600 root-owned key mounted into it
  is unreadable**. Mount it 0644 or chown it to the runtime user. The symptom is
  `ObjectStoreConfigError` at first use, not at startup.

Setting **both** `S3_*` and `GCS_*` is refused rather than resolved by precedence —
"which bucket did my firmware go to?" must not be answered by reading a factory. Unset
one, or set `OBJECT_STORE_BACKEND` explicitly.
