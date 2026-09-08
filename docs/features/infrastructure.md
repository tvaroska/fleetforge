# Infrastructure & Production Environment

## Bingo Retirement (R0-infra-0)

To prepare the production environment for fleetforge deployment, the unfinished bingo application was fully retired from production on 2026-09-08. This freed critical resources on a memory-constrained host and made the `bingo.tvaroska.sk` domain available for fleetforge.

### What was removed

- **Containers**: `bingo` (backend) and `bingo-frontend` services stopped and removed from prod docker-compose
- **Database**: `bingo_db` database and `bingo_user` role dropped after verified backup to `gs://btvaroska/retired/bingo/`
- **Memory**: Freed 384 MB of declared container limits (host was swapping ~1 GB before retirement)
- **Domain**: `bingo.tvaroska.sk` Traefik route removed, domain ready for fleetforge use
- **Deployment integration**: Removed from deploy scripts, smoke tests, validation scripts, and disaster recovery runbooks

### What was kept

The bingo git repository and its Artifact Registry images (`us-central1-docker.pkg.dev/sites-470716/containers/bingo*`) were intentionally preserved as historical artifacts. Only the production deployment was retired.

### Key gotchas learned

1. **Container removal order matters**: Removing services from docker-compose.yml does not stop running containers. They must be explicitly stopped before syncing new config, otherwise `docker compose stop <service>` can no longer address them by name.

2. **Smoke test configuration**: Deploy script smoke tests must be updated in the same commit that removes services, otherwise the deploy fails its health checks and auto-rolls back.

3. **Database backup before drop**: The database drop is irreversible. A dedicated, verified single-database dump was taken and shipped to GCS before any destructive operations. The nightly cluster backup exists but requires full-cluster restore.

4. **Tracked secrets in git**: Discovered that `prod/bingo.env` was tracked in git despite being listed in `.gitignore` (gitignore does not apply to already-tracked files). The bingo credentials became dead the moment the role was dropped, so removal was safe. Similar issues with other env files were filed as separate security tasks.

5. **Init script inertness**: Postgres init scripts only run on fresh volumes. Removing database creation from init scripts does not drop existing databases - that requires explicit `DROP` commands. Conversely, leaving a `CREATE USER` line with an undefined variable creates a user with an empty password on disaster recovery.

### Files touched

Primary changes in `services/` repository:
- `prod/docker-compose.yml`: Removed bingo service definitions and Traefik labels
- `scripts/deploy.sh`: Removed bingo from service filter, smoke tests, and usage text
- `scripts/validate-config.sh`: Removed bingo environment validation
- `prod/postgres/01-init.sh`: Removed bingo database/user creation
- `prod/.env`: Removed `BINGO_PASSWORD` variable
- `prod/bingo.env`: Removed from git tracking
- `docs/runbooks/disaster-recovery.md`: Removed all bingo references

Secondary changes in `products/` root:
- `justfile`: Removed bingo.env from backup/restore loops
- `boris/main.py`: Removed bingo service health tile from dashboard
- `.claude/skills/release/SKILL.md`: Removed bingo app configuration block

### Production verification

Post-retirement checks confirmed:
- Container count reduced from 12 to 10
- Memory usage reduced, swap pressure decreased
- Traefik route returns 404 for bingo.tvaroska.sk
- Database and role fully removed, other databases unaffected
- Backup verified restorable via `pg_restore -l`
- Full deploy pipeline green with bingo removed
- Other services (boris, content, download) unaffected

The Let's Encrypt certificate for `bingo.tvaroska.sk` was intentionally kept in Traefik's `acme.json` to avoid a fresh ACME challenge when fleetforge reuses the domain.
