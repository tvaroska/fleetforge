# Fleetforge — Architecture & Tech Stack

*The concrete topology: what runs where, on what, and why. [design/architecture.md](architecture.md) holds the platform-agnostic contracts; this file holds the actual components. Requirements it must satisfy: [prd.md](../spec/prd.md) → Requirements & targets.*

## Deployment context

Fleetforge deploys **into the existing `products` prod stack** on the `prod` VM, not onto
a machine of its own. It reuses the shared Traefik and the shared Postgres, following the
same conventions as `content` and `boris` (Artifact Registry image, digest-pinned,
`env_file`, healthcheck, resource limits, `frontend` + `backend` networks).

**Domain: `bingo.tvaroska.sk`**, inherited from the retired bingo app (see *Retiring
bingo* below). Expedient, not branded — **Fleetforge is not a public product until V3**,
so the domain is an operational detail. A proper domain lands when it goes public.

*This does not weaken the single-tenant hosted model:* it is still a real public domain
with a real Let's Encrypt certificate, which is what the devices and Web Serial need. It
is "not public" in the product sense — no signup, one admin — which SPEC already states.

## Components

| Component | Tech | Notes |
|---|---|---|
| **API** | FastAPI (Python 3.12), uvicorn | house standard; async fits SSE |
| **Ingestor** | Python, `aiomqtt` | **separate process** — see *The single-subscriber rule* |
| **Frontend** | React + Vite, nginx-served | mirrors `bingo-frontend`'s nginx-proxy shape |
| **Broker** | Mosquitto + dynamic-security plugin | **new to this host** — nothing here runs a broker today |
| **Database** | shared Postgres 16 + **TimescaleDB** | own role + schema, as bingo had |
| **Artifact store** | **GCS** (`gs://btvaroska/fleetforge/`) behind an adapter | MinIO for self-host later |
| **Ingress** | shared Traefik v3.6 | needs a new entrypoint — see *Port 8883* |
| **Migrations** | Alembic, `RUN_MIGRATIONS=true` | bingo's pattern |
| **Agent build** | pinned ESP-IDF container | `R0-INFRA-2`; images served by the API |
| **Tests** | pytest, pytest-asyncio; `pytest-embedded` from R4 | plus the Python device simulator (`R0-TEST-1`) |

**TimescaleDB is already preloaded on the shared Postgres** (`shared_preload_libraries=timescaledb`).
R3 telemetry is a hypertable and SPEC's 30-day retention is a Timescale retention policy —
not a cron job. This is free and was not previously noticed.

## The single-subscriber rule

**The ingestor is a separate, single-instance process. The API never subscribes to MQTT.**

If the API ran N uvicorn workers and each opened its own MQTT subscription, every message
would be ingested N times, and an SSE client attached to worker A would never see an event
ingested by worker B. Both failures are silent until the worker count goes above one.

```
mosquitto ──► ingestor (exactly 1) ──► Postgres  (durable state)
                                   └─► NOTIFY    (fan-out)
              api workers (N) ──── LISTEN ───────┘ ──► SSE ──► dashboard / CLI / HA
```

One writer, no duplicate ingestion, fan-out over Postgres `LISTEN/NOTIFY` — no Redis.
This mirrors the existing `content-api` / `content-worker` split already running on this
host. Commands flow the other way: API publishes to the broker directly (publishing is
stateless and safe from any worker).

*Cost of getting this wrong:* it is cheap now and a rewrite after R3, once telemetry
volume forces a second worker.

## Same origin, two backends

DESIGN requires the dashboard and API on **one origin** (no CORS, SameSite=Strict is then
sufficient against CSRF). nginx in the frontend container serves the SPA and proxies
`/v1/*` to the API — the same shape `bingo-frontend` used with `BACKEND_URL`.

```
bingo.tvaroska.sk:443  ──► Traefik ──► frontend (nginx) ──┬── /        → SPA
                                                          └── /v1/*    → api:8000
bingo.tvaroska.sk:8883 ──► Traefik TCP router ────────────────────────► mosquitto:1883
```

## Port 8883 — a change to the *shared* Traefik

The existing Traefik exposes only 80/443 and uses the **HTTP-01** ACME challenge. MQTT
over TLS needs:

1. A new entrypoint `--entrypoints.mqtt.address=:8883` and `8883:8883` published on the
   shared Traefik service.
2. A GCP firewall rule opening 8883.
3. A Traefik **TCP router** with `tls.certresolver=myresolver` on
   ``HostSNI(`bingo.tvaroska.sk`)`` → `mosquitto:1883` (plaintext internally, on the
   `backend` network only).

HTTP-01 still issues the certificate over 80/443; the TCP router reuses it, so no DNS-01
and no new credentials. **But this is the first non-HTTP port in this stack** — the change
lands in the `services` repo and touches every app's ingress path. It is not a
fleetforge-local edit.

## Artifact storage — GCS now, MinIO later

Artifacts live in **GCS** (`gs://btvaroska/fleetforge/`), consistent with the rest of the
estate, and GCS **signed URLs are exactly the mechanism** the `stage` command already
specifies — short-lived, signature-as-authorization, range-capable, and served without
touching the API process.

Access goes through a narrow **object-store adapter** (`put`, `get`, `signed_url`,
`delete`) so nothing above it knows which backend is underneath. **MinIO is the
self-hosted backend** when V2's turnkey self-hosting arrives: S3-compatible, so it is a
second adapter implementation rather than a redesign — the same move as the device-side
platform adapters.

*Consequence, stated plainly:* v1 has a cloud dependency for artifact storage. That is
acceptable while v1 is a single hosted instance, and MinIO is what removes it. The
adapter boundary is what keeps that a configuration change.

*Consequence for the box:* artifact bytes never occupy the VM's 5.5 GB of free disk, and
device downloads do not consume the VM's bandwidth. This meaningfully de-risks the
capacity problem below.

Since **R1-be-3** the device is not handed the store's URL directly: it gets
`GET /v1/artifact/{sha256}/bin?exp=…&sig=…` on **our** origin, whose HMAC signature is its
authorization, and the API answers **307** to a cached store URL rather than proxying the
bytes — so the backend stays invisible to the fleet while "served without touching the API
process" remains true, and range/resume stays the store's own RFC-correct implementation.
It needs `ARTIFACT_URL_SECRET` and `PUBLIC_BASE_URL` in the production environment.

## Retiring bingo

Bingo is unfinished and is being retired to make room. Removing it frees **384 MB** of
declared container limits (`bingo` 256 M + `bingo-frontend` 128 M) and the
`bingo.tvaroska.sk` route.

Removal steps (in the `services` repo, plus the registry):
1. Drop the `bingo` and `bingo-frontend` services from `services/prod/docker-compose.yml`.
2. Remove `bingo.env` and `BINGO_PASSWORD` from `db.env` / prod env; `just backup`.
3. Remove `bingo` from the deploy registry variables in `scripts/deploy.sh`.
4. **Back up the bingo database before dropping the role/schema** — the app is unfinished,
   not worthless, and the drop is the one irreversible step here.
5. Leave the `bingo` git repo and its Artifact Registry images alone; retiring the
   deployment is not deleting the project.

## Capacity — Measured 2026-09-10 (R0-infra-4)

**Measurement tool:** `scripts/capacity_snapshot.py` (see `docs/runbooks/capacity.md`).

**Key insight:** Swap *used* is a stock, not a flow. A gigabyte of cold anonymous pages
parked in swap and never read back costs nothing; what costs is the *rate* of `pswpin` /
`pgmajfault`. The definitive under-provisioning signal is `memory.events max` — it counts
forced reclaims at the limit, long before an OOM kill.

### Dev Box (2026-09-10, Production Shape)

`just up-prod` (built images, nginx, no --reload) + idle:

| Service | Peak (MiB) | Limit (MiB) | Peak % | memory.events max |
|---|---|---|---|---|
| api | 67.5 | 256 | 26% | 0 |
| ingestor | 51.5 | 128 | 40% | 0 |
| frontend (nginx) | 5.0 | 64 | 8% | 0 |
| mosquitto | 6.8 | 64 | 11% | 0 |
| **Total declared** | | **512** | | |

**Fleetforge footprint at idle:** ~131 MiB peak across the four services, 512 MiB declared.

**Note:** The frontend figure (5 MiB) is nginx-shaped. `just up` (dev) runs Vite and reads
~48 MiB — measure in the production shape or the verdict is wrong.

### Prod Host (2026-09-09, Before R0-infra-4)

VM `main`, `e2-medium` (2 vCPU / 4 GB), zone `us-central1-c`, kernel 6.1, Debian 12.
**Baseline after retiring bingo** (R0-infra-0):

```
Mem   3924 MiB total · 1742 MB used · MemAvailable 2274 MiB
Swap  2047 MiB total · 783 MB used (down from 1038 MB pre-retirement)
Disk  25 G, 7.0 G free (71%)
10 containers running (down from 12), 0 restarts, 0 OOM kills
```

**Projected fleetforge add:** 131 MiB measured peak + ~20 MiB marginal Postgres (estimated
for 2 api workers + ingestor connections) = **~151 MiB**. Net addition after retiring bingo
(384 MiB) is **-233 MiB** — fleetforge is smaller than what it replaced.

**Declared over-commit:** Adding fleetforge's 512 MiB declared to the current 3392 MiB =
**3904 MiB / 3924 MiB MemTotal = 99.5% committed**. Normal and acceptable — declared limits
are not actual usage, and measured peaks are what matter.

### Verdict (R0-infra-4)

**No resize needed** at R0. The 151 MiB measured footprint fits in the 384 MiB headroom
bingo freed. Declared over-commit is 99.5%, but the measured add is net-negative and no
container has `memory.events max > 0` under idle load.

**Follow-up:** Re-run `just capacity-check-prod` after `R0-infra-5` deploys the app to prod,
to confirm this projection against live measurements. The dev-box measurement is in the
production *shape* but not at production *scale* — a real 25-board fleet may differ.

**Triggers for resize:**
- Any container shows `memory.events max > 0` (reclaim at the limit)
- `MemAvailable` floor < 512 MiB during normal load
- An OOM kill

See `docs/runbooks/capacity.md` for the resize procedure (requires owner, Cloud Shell).

Disk is helped considerably by artifacts living in GCS; the remaining growth is the
ESP-IDF builder image, which is large (~2–3 GB). **Build agent images off-box** (locally
or in CI, pushed to Artifact Registry) rather than building on the prod VM.

## Two deployment artifacts, deliberately

SPEC promises a turnkey Compose stack; reality is a fragment of a shared stack. Both are
real and both are maintained:

| Artifact | Used by | Contains |
|---|---|---|
| `docker-compose.yml` (repo root) | dev loop, and the V2 self-host promise | **everything**: own Traefik, own Postgres, own MinIO, mosquitto, api, ingestor, frontend |
| service fragment in `services/prod` | production | api, ingestor, frontend, mosquitto only — Traefik, Postgres and GCS are shared/external |

The standalone stack will rot if only the fragment is ever deployed, so **the standalone
stack is the dev loop** — it is exercised every day by construction. That is the whole
reason to make it the dev environment rather than a documented afterthought.

## Open decisions

- Mosquitto persistence volume sizing and whether `persistence true` is needed at all
  (persistent sessions must survive a broker restart, so: yes).
- Whether the ingestor and API ship as one image with two entrypoints (simpler CI, one
  build) or two images. **One image, two commands** is the recommendation.
- GCS service-account scoping: a dedicated key limited to `gs://btvaroska/fleetforge/`,
  not a reuse of content's credentials.
