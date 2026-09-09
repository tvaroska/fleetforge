# Authentication & Authorization

## Admin Authentication (R0-be-1)

The admin authentication system provides the credential machinery for all protected endpoints in the API. It implements opaque bearer token authentication with dual transport (HTTP cookies and Authorization headers), instant revocation, and rate-limited login.

### Authentication flow

Three endpoints form the authentication surface:

**POST /v1/auth/login** - Accepts a password in the request body, verifies it against the admin password hash from configuration, issues a new admin token, and returns it in an HttpOnly/Secure/SameSite=Strict cookie. The response body contains only an expiration timestamp - the token value never appears in response bodies or logs. Login attempts are rate-limited per client IP with a global backstop to prevent distributed brute force attacks.

**GET /v1/auth/me** - Returns the authenticated principal's details (token ID, subject, scopes, expiration). This endpoint serves as both the proof-of-work for the authentication system and the mechanism the dashboard uses to determine whether to show the login form or the main interface.

**POST /v1/auth/logout** - Revokes the presenting token by setting its revoked_at timestamp and clears the session cookie. Revocation is instant - the very next request with that token will receive a 401 regardless of any cached state.

### One credential type, two transports

The login cookie carries the same token value a CLI would send in an Authorization: Bearer header. Both transports converge on a single verification path in the require_admin dependency, which checks the header first and falls back to the cookie. This design choice means revoking a dashboard session and revoking a CLI token are identical operations - a single UPDATE statement against the admin_tokens table.

There is no separate session table and no second credential kind. The wire format is `ffa_{uuid-hex}.{secret-b64url}` where the UUID is the admin_tokens row primary key (enabling indexed lookup since argon2 hashes are unsearchable) and only the secret half is verified against the stored hash.

### Instant revocation via caching discipline

The verification cache memoizes hash comparisons for 60 seconds but never caches authorization decisions. Every authenticated request reads the admin_tokens row and checks revoked_at and expires_at before allowing the request through. Only the expensive argon2 verification (approximately 40ms) is cached, keyed by (token_id, sha256(secret)).

This discipline preserves the instant revocation property that was the original reason JWT was rejected for this application. Setting revoked_at in the database causes the very next request to fail with no restart and no waiting. The verification path explicitly checks revocation before calling argon2 verify, which also prevents revoked tokens from burning CPU.

### Argon2id tuning for container memory limits

The API container has a 256MB memory limit and runs on a production host that already swaps under load. The argon2-cffi library defaults (time_cost=3, memory_cost=65536 which is 64MB, parallelism=4) combined with Starlette's default 40-thread threadpool would peak at 40 × 64MB ≈ 760MB - guaranteed OOM kill.

The implementation pins argon2id to OWASP's second recommended profile: time_cost=2, memory_cost=19456 (19MB), parallelism=1. Every argon2 call (both hash and verify) runs in a background thread via anyio.to_thread.run_sync with a dedicated anyio.CapacityLimiter(2) that bounds peak memory to approximately 38MB. This keeps the blocking C calls off the event loop while preventing the threadpool multiplication that would exceed container limits.

### Configuration and deployment gotchas

The ADMIN_PASSWORD_HASH setting holds an argon2id PHC string that looks like `$argon2id$v=19$m=19456,t=2,p=1$...`. Docker Compose interprets the dollar signs as variable interpolation, turning the hash into `=19=19456` unless it is single-quoted in the .env file. This was verified empirically and the failure mode is silent - login never succeeds but the reason is not logged.

The setting is optional in the Settings model (to keep create_app() constructible with no environment, which existing tests depend on) but mandatory in docker-compose.yml via `${ADMIN_PASSWORD_HASH:?...}`. When unconfigured, /v1/auth/login returns 503 and create_app() logs one WARNING at startup. The readiness check deliberately does not fail on missing login configuration - readiness is about the database, and a configuration problem should not trigger container restart loops.

A command-line tool is provided: `python -m fleetforge.auth hash-password` prompts for a password twice and prints only the PHC string, with a stderr hint showing the single-quoted .env line to paste. This is the only way admin password hashes should be generated.

### Security properties

**Secure cookies work over http://localhost.** Chrome and Firefox both treat http://localhost as a secure context, so the Secure cookie attribute is unconditional - no dev/prod switch for anyone to flip in production. The dashboard is same-origin by construction (nginx in the frontend container proxies /v1 requests), which is why no CORS middleware ever appears and no CSRF token is needed.

**Uniform 401 responses.** Every authentication failure returns the same response body and status: 401 with `{"detail": "invalid credentials"}` and a `WWW-Authenticate: Bearer` header. The distinction between expired/revoked/unknown/malformed tokens appears only in logger.info calls with the token ID (never the token value).

**Rate limiting is per-process.** The FixedWindowLimiter instance lives on app.state and is created fresh per app, so limits are per uvicorn worker. V1 runs one worker per container so the limit is container-scoped. Only login failures are counted and both the per-IP and global buckets are checked before any argon2 work, preventing failed logins from being a CPU exhaustion vector.

**Secrets never stored or logged in plaintext.** Admin passwords are hashed before storage, token secrets are hashed before storage, and logs may contain only token IDs. The single-quoted PHC string in .env.example uses an obviously fake dev password (`fleetforge-dev-only`) to prevent accidental production credential leaks.

### Files touched

Created the entire auth package (src/fleetforge/auth/) containing hashing.py (argon2 wrapper), tokens.py (wire format), cache.py (verification cache), ratelimit.py (fixed-window limiter), and __main__.py (password hashing CLI). Created api/deps.py with the require_admin dependency, api/routers/auth.py with the three endpoints, and api/schemas.py for request/response models. Modified api/main.py to include the auth router and create per-app cache and limiter instances. Added argon2-cffi to dependencies and seven new settings to config.py. Deleted db/base.py::get_session() which bypassed dependency overrides. Moved test fixtures from test_api_health.py into conftest.py and added comprehensive test coverage in test_api_auth.py. Updated docker-compose.yml, .env.example, justfile, docs/runbooks/dev-stack.md, and README.md.

### Gotchas learned

The db/base.get_session() function was deleted because it called the lru_cached get_sessionmaker() directly, which meant dependency_overrides had no effect on it. Tests would have silently hit the developer's real dev database instead of the test database. All API database access now funnels through Depends(get_sessionmaker) which is the single override point tests use.

The last_used_at timestamp on admin tokens is throttled to one write per token per 60 seconds to avoid turning every authenticated read into a database write. The throttle is implemented as a single conditional UPDATE that compares the current value to now() - throttle_interval, committed in the same session as the request. Failures here are logged but never fail the request since the timestamp is telemetry, not authorization.

The leftmost X-Forwarded-For header entry is used as the rate limit key because nginx sets it and the API sees only nginx's IP directly. However, Traefik appends to XFF rather than replacing it, so a malicious client can spoof the header. This is why there is a global rate limit bucket as a backstop - the per-IP limit is best-effort defense against distributed attacks, not a security boundary.
