# Runbook — refresh the prod fleetforge database (drop and recreate)

The procedure that ships P1 (projects and users) to production: release v0.7.0, drop and
recreate **only** the `fleetforge` database inside the shared prod Postgres, delete the
dynsec device clients and the user-artifact blobs, deploy, and let the api migrate
`0001` → `0010` on an empty database. Why it is a recreate and not a migration:
`DECISIONS.md` 2026-10-19 (S0-rel-1).

**It is destructive and needs the owner's explicit go-ahead.** Every enrolled device row,
group, enrollment token, admin token, artifact row and deploy history on prod is lost. The
owner accepted that on 2026-10-19 because prod holds only test boards. A blanket
`/implement-all` run, an orchestrator or a plan is not a go-ahead.

Reuse it for any future release that rewrites an already-applied migration in place.

## Never

- **Never touch the postgres volume** (`postgres_prod`) and never restart or recreate the
  `postgres` service. It is shared: `content_mgmt_db` and the default `postgres` database
  live in the same container. The only statements this runbook runs against it are
  `DROP DATABASE fleetforge` and `CREATE DATABASE fleetforge`.
- **Never take the stack down**: no `docker compose` `down`, with or without volumes, and no
  `rm` of any service other than `fleetforge-api` and `fleetforge-ingestor`.
- **Never restart, recreate or re-init mosquitto**, never touch the `mosquitto_data`
  volume, never edit `dynamic-security.json`, never re-run `mosquitto/bootstrap.sh`. Dynsec
  is changed only over `$CONTROL` against the running broker (ops-log F-2026-09-23-002,
  CRITICAL.md dynsec row).
- **Never delete anything under `agent/`** in the object store, and never wipe the
  `gs://btvaroska/fleetforge/` prefix. Agent bundles and user artifacts share
  `blobs/sha256/`; a prefix wipe deletes the flasher's agent and breaks the onboarding this
  refresh depends on. Only the digests the prune script marks `DELETE` go.
- **Never edit `services/prod/.env`** as part of this runbook. P1 needs no new setting
  (the three `FIREBASE_*` settings are optional and unset on prod).
- Never print or write the prod admin password. It is not the dev `fleetforge-dev-only`.

## Preconditions

- The owner has said, in their own words, to run the prod refresh.
- Every task the release depends on is `- [x]` in `TODO.md`, and `just test` is green.
- `uv run alembic heads` prints exactly one head (`0010 (head)` for v0.7.0).
- `curl -fsS https://bingo.tvaroska.sk/v1/healthz` answers, and you have read its version.
- The owner's admin password is exported in your shell as `FF_ADMIN_PASSWORD` (only for the
  verification steps; never echoed). Avoid failed attempts: the login is rate-limited.

All remote commands run from this box over `ssh prod`. Multi-line remote work uses a
**quoted** heredoc (`<<'EOF'`) so `$POSTGRES_USER` and `$MOSQUITTO_ADMIN_*` expand
**inside** the containers and no secret crosses the wire or lands in a file here.

```bash
mkdir -p /tmp/s0-rel-1
```

Helpers, defined inside each remote heredoc that needs them:

```bash
cd /opt/boris/prod
pg() { docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -At "$@"' sh "$@" </dev/null; }
dsec() { docker compose exec -T mosquitto sh -c 'mosquitto_ctrl -h 127.0.0.1 -p 1883 -u "$MOSQUITTO_ADMIN_USERNAME" -P "$MOSQUITTO_ADMIN_PASSWORD" dynsec "$@"' sh "$@" </dev/null; }
```

## 1. Release build (local)

Follow the `/release` skill's fleetforge steps up to and including the push, but **do not
deploy**:

- Bump **minor** (`v0.6.0` → `v0.7.0`) in all five version files: `pyproject.toml`,
  `uv.lock`, `frontend/package.json`, `frontend/package-lock.json` (both fields) and
  `src/fleetforge/__init__.py`. Commit, then tag `v0.7.0` (annotated).
- `just build` (runs `just test`, the frontend build and tests, builds, verifies and
  pushes both images). If `tests/test_agent_power_and_size.py::test_every_built_bundle_is_within_budget`
  fails, stop: do not skip it.
- Pin the digests in `services/prod/docker-compose.yml`: the `fleetforge` image appears
  **twice** (api and ingestor), `fleetforge-frontend` once.
  `docker inspect --format='{{index .RepoDigests 0}}' <image>:v0.7.0` gives each digest.
- Commit in `services/` only: `chore: update fleetforge image digests to v0.7.0`.

## 2. Capture the before-state (read-only)

```bash
curl -fsS https://bingo.tvaroska.sk/v1/healthz | tee /tmp/s0-rel-1/healthz-before.json
just agent-check-prod | tee /tmp/s0-rel-1/agent-before.txt      # flasher catalog; must be IDENTICAL after
for u in https://update.tvaroska.sk/ https://download.tvaroska.sk/ https://boris.tvaroska.sk/; do
  printf '%s %s\n' "$u" "$(curl -sS -o /dev/null -w '%{http_code}' "$u")"; done | tee /tmp/s0-rel-1/others-before.txt
ssh prod bash -s <<'EOF' | tee /tmp/s0-rel-1/prod-before.txt
cd /opt/boris/prod
pg() { docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -At "$@"' sh "$@" </dev/null; }
dsec() { docker compose exec -T mosquitto sh -c 'mosquitto_ctrl -h 127.0.0.1 -p 1883 -u "$MOSQUITTO_ADMIN_USERNAME" -P "$MOSQUITTO_ADMIN_PASSWORD" dynsec "$@"' sh "$@" </dev/null; }
echo "== databases";       pg -d postgres -c "SELECT datname FROM pg_database ORDER BY 1"
echo "== alembic";         pg -d fleetforge -c "SELECT version_num FROM alembic_version"
echo "== devices";         pg -d fleetforge -c "SELECT device_id, platform_type, agent_version FROM devices ORDER BY 1"
echo "== builds";          pg -d fleetforge -c "SELECT count(*) FROM builds"
echo "== dynsec clients";  dsec listClients
EOF
```

- `alembic` must read `0008`. If it reads anything else, **stop**: the reason for the
  recreate assumed v0.6.0's head.
- If a SELECT errors on a column name, check it with `\d devices` and adjust. These are
  read-only.

The user-artifact digests (plus any build outputs, which name artifact rows by convention):

```bash
ssh prod bash -s <<'EOF' > /tmp/s0-rel-1/artifact-digests.txt
set -euo pipefail
cd /opt/boris/prod
docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -At -d fleetforge' <<'SQL'
SELECT sha256 FROM artifacts
UNION
SELECT o.value->>'sha256' FROM builds, jsonb_each(builds.outputs) AS o WHERE o.value->>'sha256' IS NOT NULL
ORDER BY 1;
SQL
EOF
wc -l < /tmp/s0-rel-1/artifact-digests.txt
grep -cvE '^[0-9a-f]{64}$' /tmp/s0-rel-1/artifact-digests.txt   # must print 0
```

The SQL goes in on stdin rather than in `-c "…"` because a string literal does not survive
two levels of shell quoting (`ssh '…'` around `sh -c '…'`).

Backup (cheap insurance, even though the loss is accepted):

```bash
ssh prod 'cd /opt/boris/prod && docker compose exec -T postgres sh -c '\''pg_dump -U "$POSTGRES_USER" -Fc fleetforge'\''' > /tmp/s0-rel-1/fleetforge-pre-s0-rel-1.dump
ls -l /tmp/s0-rel-1/fleetforge-pre-s0-rel-1.dump   # must be non-empty
```

## 3. Remove the fleetforge processes (remove, do not just stop)

```bash
ssh prod 'cd /opt/boris/prod && docker compose rm -sf fleetforge-api fleetforge-ingestor'
```

**Gotcha.** `docker rollout` on a service whose container is only *stopped* takes its
"not running" branch, `compose up --no-recreate`, and can bring back the **old** container
on the **old** image. Removing them avoids that. The frontend stays up and answers 502 on
`/v1` for a few minutes.

## 4. Drop and recreate ONLY the fleetforge database

```bash
ssh prod bash -s <<'EOF'
set -euo pipefail
cd /opt/boris/prod
docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE fleetforge WITH (FORCE)" -c "CREATE DATABASE fleetforge OWNER fleetforge"'
docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -At -d postgres -c "SELECT datname FROM pg_database ORDER BY 1"'
EOF
```

- The database list must equal the `== databases` list from step 2. If `content_mgmt_db`
  or any other database is missing, **stop and escalate to the owner immediately**.
- The `fleetforge` role and its password are kept (it was created by hand;
  `postgres/01-init.sh` only runs on a fresh volume). On PG15+ the `public` schema belongs
  to `pg_database_owner`, so the owner can create the tables. No extension is needed.
- `WITH (FORCE)` (PG13+) terminates any leftover connection.

## 5. Delete the dynsec device clients (live broker, over `$CONTROL`)

```bash
ssh prod bash -s <<'EOF'
set -eu
cd /opt/boris/prod
docker compose exec -T mosquitto sh -c '
  c="mosquitto_ctrl -h 127.0.0.1 -p 1883 -u $MOSQUITTO_ADMIN_USERNAME -P $MOSQUITTO_ADMIN_PASSWORD dynsec"
  for u in $($c listClients | grep -E "^[0-9a-f]{12}$"); do $c deleteClient "$u" && echo "deleted $u"; done
  echo "== remaining"; $c listClients'
EOF
```

- Device clients are exactly the usernames matching `^[0-9a-f]{12}$`
  (`identity.DEVICE_ID_RE`). The `ffff…` ones are the selftest's throwaway clients and are
  recreated by `just deploy-check-prod`.
- The remaining list must be exactly the three service clients (`$MQTT_DYNSEC_USERNAME`,
  `$MQTT_INGESTOR_USERNAME`, `$MQTT_COMMAND_USERNAME`). Keep the roles `device`,
  `ingestor` and `commander`.
- `listClients` prints one username per line (the unencrypted-connection warning goes to
  stderr). If the format differs, stop and adapt the parser; do not widen the pattern.

## 6. Deploy v0.7.0

```bash
cd /home/boris/products && just deploy --yes --service fleetforge
ssh prod 'cd /opt/boris/prod && docker compose logs --no-color --tail=200 fleetforge-api | grep -E "alembic|Running upgrade|entrypoint"'
# expect "Running upgrade  -> 0001" ... "-> 0010"
ssh prod 'cd /opt/boris/prod && docker compose restart fleetforge-ingestor'    # deploy-order gotcha
```

**Gotcha (DECISIONS R2b-be-5).** The scoped deploy runs `up -d traefik postgres
fleetforge-ingestor` *before* the api rollout migrates, so the new ingestor first meets an
empty database. Restart it once the api is healthy. `fleetforge-api` has
`RUN_MIGRATIONS=true` (`docker/entrypoint.sh` runs `alembic upgrade head`); the ingestor
never migrates.

## 7. Prune the user-artifact blobs

Runs inside the **new** api container, which holds the GCS identity
(`fleetforge-artifacts@btvaroska`, `roles/storage.objectAdmin` on `fleetforge/` only). It
reads the flasher's `agent/index.json`, builds the KEEP set (every entry's manifest, every
`superseded` manifest, every `parts[].sha256` of each), and marks each argv digest `KEEP` or
`DELETE`. **DRY-RUN by default**; only `--apply` deletes, and only `blob_key(<digest>)`. An
unreadable index or manifest aborts with exit 2: never guess the KEEP set. An agent `app.bin`
was uploaded as a user artifact during R1-test-1, so the overlap is real.

Write the script:

```bash
cat > /tmp/s0-rel-1/prune_blobs.py <<'PY'
"""Delete user-artifact blobs that no agent bundle references (S0-rel-1).

    python - [--apply] <sha256> ...      # piped into the api container, digests as argv

DRY-RUN unless `--apply`. The KEEP set is everything reachable from the flasher's
`agent/index.json`: each entry's manifest, each `superseded` manifest, and every part of
each such manifest. Agent bundles and user artifacts share `blobs/sha256/`, so a digest in
that set is never deleted. An unreadable index or manifest aborts the run (exit 2).
Only `blob_key(<digest>)` is ever deleted; nothing under `agent/` is touched.
"""

import asyncio
import sys

from fleetforge.config import Settings
from fleetforge.firmware.catalog import parse_index
from fleetforge.firmware.manifest import BundleManifest
from fleetforge.storage.blobs import SHA256_HEX, blob_key
from fleetforge.storage.factory import create_object_store
from fleetforge.storage.objectstore import ObjectNotFound, ObjectStore


async def agent_reachable(store: ObjectStore, index_key: str) -> dict[str, str]:
    """Every digest the flasher can reach, with a label saying why."""
    index = parse_index(await store.get(index_key))
    keep: dict[str, str] = {}
    for entry in index.bundles:
        manifests = [(entry.manifest_sha256, entry.agent_version)]
        manifests += [(s.manifest_sha256, s.agent_version) for s in entry.superseded]
        for digest, version in manifests:
            where = f"{entry.target}/{entry.partition_layout} {version or '?'}"
            keep[digest] = f"{where} manifest"
            manifest = BundleManifest.model_validate_json(await store.get(blob_key(digest)))
            for part in manifest.parts:
                keep.setdefault(part.sha256, f"{where} part {part.name}")
    return keep


async def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    digests = [a for a in argv if a != "--apply"]
    bad = [d for d in digests if not SHA256_HEX.match(d)]
    if bad:
        print(f"ABORT: not a lowercase 64-hex digest: {bad}", flush=True)
        return 2
    settings = Settings()
    store = create_object_store(settings)
    try:
        keep = await agent_reachable(store, settings.agent_index_key)
    except Exception as exc:  # noqa: BLE001 - any doubt about the KEEP set stops the run
        print(f"ABORT: cannot read {settings.agent_index_key} or a manifest: {exc!r}", flush=True)
        return 2
    print(
        f"mode {'APPLY' if apply else 'DRY-RUN'}; {len(keep)} agent-reachable digests", flush=True
    )
    for digest, why in sorted(keep.items(), key=lambda kv: kv[1]):
        print(f"AGENT {digest} ({why})", flush=True)
    deleted = 0
    for digest in dict.fromkeys(digests):
        if digest in keep:
            print(f"KEEP {digest} (agent: {keep[digest]})", flush=True)
            continue
        print(f"DELETE {digest}", flush=True)
        if apply:
            try:
                await store.delete(blob_key(digest))
                deleted += 1
            except ObjectNotFound:
                print(f"  already gone {digest}", flush=True)
    print(
        f"deleted {deleted}" if apply else "dry run: nothing deleted (re-run with --apply)",
        flush=True,
    )
    return 0


sys.exit(asyncio.run(main(sys.argv[1:])))
PY
```

Dry run, read it, then apply:

```bash
ssh prod "cd /opt/boris/prod && docker compose exec -T fleetforge-api python - $(tr '\n' ' ' < /tmp/s0-rel-1/artifact-digests.txt)" < /tmp/s0-rel-1/prune_blobs.py | tee /tmp/s0-rel-1/prune-dry.txt
# read it: every AGENT digest that also appears in artifact-digests.txt must be a KEEP line
ssh prod "cd /opt/boris/prod && docker compose exec -T fleetforge-api python - --apply $(tr '\n' ' ' < /tmp/s0-rel-1/artifact-digests.txt)" < /tmp/s0-rel-1/prune_blobs.py | tee /tmp/s0-rel-1/prune-apply.txt
```

`ObjectStore.delete` is idempotent, so a digest that is already gone is not an error.

Validated on dev on 2026-10-19 (MinIO, an esp32 bundle published to the dev store, then
the index removed again): an agent part printed `KEEP … (agent: esp32/ab-4m-v1 0.5.0 part app)`,
a real throwaway blob printed `DELETE` and still existed after the dry run, `--apply`
deleted exactly that blob and left the agent part, a non-hex argument aborted with exit 2,
and a missing `agent/index.json` aborted with exit 2.

## 8. Verify

All must hold. Do not record the task done until they do.

1. Schema at head: `uv run alembic heads` → `0010 (head)`, and
   `ssh prod 'cd /opt/boris/prod && docker compose exec -T postgres sh -c '\''psql -U "$POSTGRES_USER" -At -d fleetforge -c "SELECT version_num FROM alembic_version"'\'''` → `0010`.
2. Projects: the same with `SELECT slug FROM projects` → exactly `default`.
3. App up: `curl -fsS https://bingo.tvaroska.sk/v1/healthz` → version `0.7.0`, commit =
   the `v0.7.0` tag's commit; `curl -fsS -o /dev/null -w '%{http_code}' https://bingo.tvaroska.sk/v1/readyz` → `200`.
4. Deploy gate: `just deploy-check-prod` → `SELFTEST OK` twice, exit 0.
5. Login, as superadmin. The env admin (`ADMIN_PASSWORD_HASH`, already in prod env) gets a
   userless, unpinned `admin` session, which `deps.py` treats as superadmin. No `users`
   row is needed and none is created.
   ```bash
   curl -sS -c /tmp/s0-rel-1/c -H 'content-type: application/json' \
     -d "{\"username\":\"admin\",\"password\":\"$FF_ADMIN_PASSWORD\"}" -o /dev/null -w '%{http_code}\n' https://bingo.tvaroska.sk/v1/auth/login   # 200
   curl -sS -b /tmp/s0-rel-1/c https://bingo.tvaroska.sk/v1/auth/me   # "is_superadmin": true, projects include default
   ```
   The owner also confirms the dashboard login in a browser. A named superadmin is
   optional and only on the owner's request:
   `docker compose exec -T fleetforge-api python -m fleetforge.auth create-user --username <u> --email <e> --superadmin --stdin`.
6. A re-enrolled board reaches "On the fleet" (`frontend/src/serverWatch.ts`: `online`,
   `last_seen > enrolled_at`, `broker_provisioned_at` set). No bench (DECISIONS
   2026-10-08), so the simulator against prod, with a **fresh** state dir (a dev `.sim/`
   identity would present dev credentials to prod):
   ```bash
   cd /home/boris/products/fleetforge
   PYTHONPATH=src uv run python -m fleetforge.simulator fleet --count 1 \
     --api-base https://bingo.tvaroska.sk --host bingo.tvaroska.sk --port 8883 --tls \
     --state-dir /tmp/s0-rel-1/sim --heartbeat-interval 5 --duration 90 &
   sleep 30
   curl -sS -b /tmp/s0-rel-1/c 'https://bingo.tvaroska.sk/v1/devices?project=default'
   rm -f /tmp/s0-rel-1/c
   ```
   The simulator reads `$FF_ADMIN_PASSWORD` to mint its enrollment token. There is no
   decommission API, so the sim device stays in the fleet, identifiable by agent version
   `0.1.0-sim`. Re-flashing the real test boards from the browser is the owner's step.
7. Dynsec is clean: `dsec listClients` shows only the three service clients, the sim's
   12-hex id and the `ffff…` selftest ids from check 4.
8. The flasher is intact: `just agent-check-prod` equals `/tmp/s0-rel-1/agent-before.txt`
   (same rows, same exit status), and the prune transcripts show every agent-reachable
   digest that was a candidate as `KEEP`.
9. The other apps are untouched: the database list equals the before list, and the three
   URLs in `others-before.txt` return the same codes.

## Rollback (migrations fail, or verification cannot pass)

1. `git -C /home/boris/products/services revert HEAD` (the digest commit), so the next
   deploy brings back the v0.6.0 images.
2. `ssh prod 'cd /opt/boris/prod && docker compose rm -sf fleetforge-api fleetforge-ingestor'`.
3. Drop and create the database again, exactly as step 4.
4. Restore the dump:
   ```bash
   ssh prod 'cd /opt/boris/prod && docker compose exec -T postgres sh -c '\''pg_restore -U "$POSTGRES_USER" -d fleetforge --no-owner --role=fleetforge'\''' < /tmp/s0-rel-1/fleetforge-pre-s0-rel-1.dump
   ```
5. `cd /home/boris/products && just deploy --yes --service fleetforge`, then
   `ssh prod 'cd /opt/boris/prod && docker compose restart fleetforge-ingestor'`. The v0.6.0
   api's `alembic upgrade head` is a no-op on the restored `0008`.

Deleted dynsec clients and blobs cannot be restored. That is accepted: only test boards are
affected, and they re-enrol.
