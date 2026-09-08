# Critical Paths

Registry of protected/sensitive paths. Any `/implement` task touching a path here
**auto-escalates**: stronger model + mandatory review before commit. `spec/` is
protected — agents PROPOSE changes to spec, they do not edit it during `/implement`.

Fleetforge has an unusual property for a software project: **some mistakes cannot be
fixed by shipping new software.** An agent flashed onto a board carries a partition
table, a bootloader and a protocol version that no OTA can change. Getting those wrong
means physically retrieving every deployed device — the exact intervention this product
exists to remove. The first two rows below are that class of mistake.

| Path | Why it's critical |
|------|-------------------|
| `spec/device-protocol.md` | **Near-frozen.** An R0 agent speaks this protocol until someone physically retrieves the board. Additive change is cheap; anything else is a recall. Topic tree, QoS/retain semantics and payload schemas are all load-bearing. |
| Partition table, `sdkconfig` bootloader options, eFuse burns (agent firmware) | **Flash-time immutables** — not changeable by OTA. Wrong at R0 = physical recall of the fleet. See `design/architecture.md` → *Flash-time immutables*. |
| `spec/` | THE WHAT — requirements, targets, contracts. Status-free; changes are proposals, reviewed. |
| `spec/prd.md` → *Requirements & targets* | Downstream docs and code resolve against this table. Changing a number here silently changes behaviour in the agent, the ingestor and the dashboard. |
| Device-side confirm timer / rollback path (agent firmware) | The whole bricking gamble. A bug here means a board that cannot recover itself — the one failure the product must never have. |
| A/B slot apply logic (agent firmware) | Writing the wrong slot, or a non-atomic switch, bricks the device. |
| Mosquitto ACL configuration / dynsec provisioning | Two pattern rules are the entire fleet authz. A wrong pattern lets any device impersonate any other. |
| Enrolment token issuance & burn (`R0-be-2`, `R0-be-4`) | A token that fails to burn lets anyone with one board enrol arbitrary devices into the fleet. |
| Admin auth (token table, login, cookie flags) | Single admin credential on a public-facing API. Bypass = full fleet control. |
| Artifact signing keys & `signed_url` generation | Signature *is* the authorization for artifact download; signing keys are what make R5's verification meaningful. |
| Alembic migrations (`alembic/versions/`) | Irreversible schema/data changes. |
| Traefik entrypoints / TCP router (in the `services` repo) | Shared ingress. A change here affects every other app on the host, not just fleetforge. |
| Secrets / env (`.env*`, GCS service-account key, broker credentials) | Never commit real values. Prod env lives in `services/prod/.env` — confirm before changing. |
